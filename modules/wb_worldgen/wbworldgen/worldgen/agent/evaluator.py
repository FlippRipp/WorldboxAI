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
    """Structural excerpt of the built content: the codex (reference lore
    the content must not contradict), then per map its own description
    plus the highest-importance named nodes with their content."""
    from wbworldgen.worldgen.codex import render_excerpt

    lines = []
    codex_render = render_excerpt(compiled)
    if codex_render:
        lines.append(codex_render)
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
            if n.get("additional_details"):
                lines.append(f"    storyteller details: {n['additional_details']}")
        if len(named) > _EXCERPT_NODES_PER_MAP:
            # An unmarked cutoff reads as missing content: the Ecstasy Veil
            # live run's critique flagged "20 claimed, only 12 listed" as a
            # blocking finding off exactly this truncation.
            lines.append(
                f"  (excerpt truncated: the {_EXCERPT_NODES_PER_MAP} "
                f"highest-importance of {len(named)} named locations are "
                f"shown — the other {len(named) - _EXCERPT_NODES_PER_MAP} "
                "exist and are simply not excerpted)")
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
        "list is the normal outcome for a sound world.\n"
        "The content is an EXCERPT — per-map location lists are truncated "
        "to the highest-importance entries (marked when so). Judge only "
        "what is shown; never flag counts, completeness, or absences "
        "inferred from the excerpt's own bounds.\n\n"
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
                         map_id: str = None, major_floor: int = None,
                         builder=None, world_id: str = None,
                         on_event=None) -> dict:
    """Evaluate the current build: deterministic lints, (with a live LLM and
    authored rules) one structured critique call, and (with a live LLM,
    notes in the brief, and ``builder``/``world_id`` supplied) the
    tool-looping note verifier (C5/N4). Returns ``{"clean", "findings",
    "lint", "notes"}`` where findings carry stable keys and ``clean`` means
    no blocking (severity 'problem') findings. Note findings key per note
    (``note:<id>:-:-``) so their fix-round tracking is stable across
    binding changes — and the done-gate never auto-accepts them (N6)."""
    from wbworldgen.worldgen.agent.verifier import verify_notes

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

    note_report = await verify_notes(
        services, builder, world_id, world_state, compiled,
        map_id=map_id, on_event=on_event)
    for v in note_report["verdicts"]:
        if v["verdict"] == "honored":
            continue
        findings.append({
            "key": finding_key("note", v["id"]),
            "source": "note", "kind": "note_violation",
            "severity": "problem",
            "map_id": v.get("map_id"), "node_id": None,
            "note_id": v["id"],
            "finding": (f"Agreed note {v['id']} is not honored"
                        + (f" ({v['subject']})" if v.get("subject") else "")
                        + f": {v['text']} — " + (v.get("evidence") or
                                                 "the verifier found no evidence of it")),
            "suggestion": v.get("suggestion", ""),
        })
    for nid in note_report["unverified"]:
        findings.append({
            "key": finding_key("note", nid),
            "source": "note", "kind": "note_unverified",
            "severity": "problem", "map_id": None, "node_id": None,
            "note_id": nid,
            "finding": (f"Agreed note {nid} could not be verified within the "
                        "verifier's budget. Evaluate again (or simplify what "
                        "the verifier must read)."),
            "suggestion": "",
        })

    blocking = [f for f in findings if f["severity"] == "problem"]
    honored = sum(1 for v in note_report["verdicts"] if v["verdict"] == "honored")
    return {"clean": not blocking, "findings": findings,
            "blocking": len(blocking), "lint": lint_report,
            "notes": {"skipped": note_report["skipped"],
                      "checked": len(note_report["verdicts"]),
                      "honored": honored,
                      "unverified": len(note_report["unverified"]),
                      "verdicts": note_report["verdicts"]}}
