"""The build evaluator (D3 of the worldgen plan): rules-based critique.

One structured critique call — the world rules (the rubric), the
deterministic lint report and structural content excerpts in, findings out.
Deliberately NOT a tool-looping sub-agent (that is the recorded v2 upgrade).
Without a live LLM the evaluation degrades to the lint report alone, which
keeps the harness and its done-gate fully testable offline.

Every finding carries a stable ``key`` (``source:kind:map:node``) — the
done-gate tracks fix rounds and explicit acceptances by these keys, so the
agent can reference them in a done claim (D5's max-fix-rounds budget counts
sightings of the same key across evaluations)."""

import logging

from wbworldgen.worldgen import mapspace as _ms
from wbworldgen.worldgen.agent.lints import lint_world
from wbworldgen.worldgen.generation.llm import json_retry_completion

logger = logging.getLogger(__name__)

#: Highest-importance named nodes quoted per map in the critique excerpt
#: (structural budget, P9 — never a character cap).
_EXCERPT_NODES_PER_MAP = 12


def finding_key(source: str, kind: str, map_id=None, node_id=None) -> str:
    return f"{source}:{kind}:{map_id or '-'}:{node_id or '-'}"


def _lint_findings(lint_report: dict) -> list:
    """Lint problems as findings: deterministic ground truth, always
    severity 'problem'. Aggregate kinds (unnamed majors) key per map;
    duplicate-name groups key on their first node so distinct groups get
    distinct keys."""
    findings = []
    for p in lint_report.get("problems", []):
        node_id = p.get("node_id")
        map_id = p.get("map_id")
        if p.get("nodes"):  # duplicate_name group
            node_id = p["nodes"][0].get("node_id")
            map_id = p["nodes"][0].get("map_id")
        elif node_id is None and p.get("connection_id"):
            node_id = p["connection_id"]
        elif node_id is None and p.get("note_id"):
            # note_unbound problems key per note id, so two unbound notes
            # get distinct fix-round tracking.
            node_id = p["note_id"]
        elif node_id is None and isinstance(p.get("edge"), dict):
            node_id = f"{p['edge'].get('from')}->{p['edge'].get('to')}"
        findings.append({
            "key": finding_key("lint", p.get("kind", "problem"), map_id, node_id),
            "source": "lint",
            "kind": p.get("kind", "problem"),
            "severity": "problem",
            "map_id": map_id,
            "node_id": node_id,
            "finding": p.get("message", ""),
        })
    return findings


def _content_excerpts(compiled: dict) -> str:
    """Structural excerpt of the built content: per map, its own description
    plus the highest-importance named nodes with their content."""
    lines = []
    for mid, rec in _ms.maps_by_id(compiled).items():
        nodes = rec.get("nodes", [])
        named = sorted((n for n in nodes if n.get("name")),
                       key=lambda n: -n.get("importance", 0))
        lines.append(f"Map '{mid}' — {rec.get('label', mid)} "
                     f"({rec.get('level_type', 'map')}); {len(nodes)} locations, "
                     f"{len(named)} named.")
        if rec.get("description"):
            lines.append(f"  Map description: {rec['description']}")
        for n in named[:_EXCERPT_NODES_PER_MAP]:
            lines.append(f"  - {n['name']} ({n.get('type', 'place')}, "
                         f"importance {n.get('importance', 0)})")
            if n.get("label_description"):
                lines.append(f"    label: {n['label_description']}")
            if n.get("description"):
                lines.append(f"    description: {n['description']}")
    return "\n".join(lines)


async def generate_critique(services, rules: dict, lore: dict, lint_report: dict,
                            excerpts: str, scope_note: str = "") -> list:
    """The critique LLM call (smartest slot). Returns the parsed findings
    list; raises on failure. Module-level so tests monkeypatch it — the
    same patch-point contract as the pass modules."""
    import json as _json

    system = (
        "You are the quality evaluator for an AI-built game world. Judge the "
        "built content ONLY against the world's own rules and internal "
        "coherence — never against your taste. Flag rule violations, "
        "contradictions between locations, content that ignores the world's "
        "premise, and tonal breaks the rules forbid. Do NOT flag style, "
        "prose quality, or things the rules are silent on. An empty findings "
        "list is the normal outcome for a sound world.\n\n"
        "Severity: 'problem' = violates a rule or breaks coherence (blocks "
        "completion); 'nit' = worth noting, does not block.\n"
        "Output ONLY valid JSON: {\"findings\": [{\"kind\": \"short_slug\", "
        "\"severity\": \"problem\"|\"nit\", \"map_id\": \"...\"|null, "
        "\"node_id\": \"...\"|null, \"finding\": \"one sentence\", "
        "\"suggestion\": \"one sentence\"}, ...]}")
    user_msg = f"""World rules (the rubric this world must honor):
{_json.dumps(rules, indent=2, ensure_ascii=False)}

World: {lore.get('world_name', 'Unknown')}
Premise: {lore.get('premise', '')}

Deterministic lint summary (already reported separately — do not repeat
these; use them only as context): {lint_report.get('problem_count', 0)} problem(s).

Built content:
{excerpts}
{scope_note}
Evaluate the content against the rules. Output ONLY the JSON object."""
    parsed = await json_retry_completion(
        services.llm,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user_msg}],
        model=services.llm.storyteller_model,
        temperature=0.3,
        inspector_ctx={"call_type": "world_build", "step": "agent:evaluate"},
        step_label="agent:evaluate",
        retry_attempts=services.json_retry_attempts,
    )
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    return [f for f in (findings if isinstance(findings, list) else [])
            if isinstance(f, dict) and f.get("finding")]


async def evaluate_world(services, world_state: dict, compiled: dict,
                         map_id: str = None, major_floor: int = None) -> dict:
    """Evaluate the current build: deterministic lints plus (with a live
    LLM and authored rules) one structured critique call. Returns
    ``{"clean", "findings", "lint"}`` where findings carry stable keys and
    ``clean`` means no blocking (severity 'problem') findings."""
    lint_report = lint_world(compiled, map_id=map_id, major_floor=major_floor)
    findings = _lint_findings(lint_report)

    rules = ((world_state.get("steps", {}).get("world_rules") or {}).get("data")) or {}
    lore = compiled.get("lore") if isinstance(compiled.get("lore"), dict) else {}
    has_content = any(n.get("name") for n in _ms.all_nodes(compiled))
    llm_live = services.llm is not None and getattr(services.llm, "mode", "mock") != "mock"

    if llm_live and rules and has_content:
        scope_note = f"\nScope: evaluate only map '{map_id}'.\n" if map_id else ""
        try:
            critique = await generate_critique(
                services, rules, lore, lint_report,
                _content_excerpts(compiled), scope_note)
        except Exception as e:
            logger.warning("Evaluator critique call failed: %s", e)
            critique = []
        seen = {f["key"] for f in findings}
        for f in critique:
            key = finding_key("critique", str(f.get("kind") or "finding"),
                              f.get("map_id"), f.get("node_id"))
            while key in seen:
                key += "+"
            seen.add(key)
            findings.append({
                "key": key, "source": "critique",
                "kind": str(f.get("kind") or "finding"),
                "severity": "problem" if f.get("severity") == "problem" else "nit",
                "map_id": f.get("map_id"), "node_id": f.get("node_id"),
                "finding": str(f.get("finding", "")),
                "suggestion": str(f.get("suggestion", "")),
            })

    blocking = [f for f in findings if f["severity"] == "problem"]
    return {"clean": not blocking, "findings": findings,
            "blocking": len(blocking), "lint": lint_report}
