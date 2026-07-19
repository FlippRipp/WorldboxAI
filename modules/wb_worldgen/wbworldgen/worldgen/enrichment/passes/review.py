"""The label coherence review pass: one critique call per finished map.

An LLM reviews every named node against what it is actually near and flags
names that don't make sense in place (a place implying it belongs to an
institution that sits across the map, duplicates, names contradicting the
map). Each flagged node is relabeled with the reviewer's objection as
steering, and its description (when present) is reworked to match.

A map pass: ``triggers={"on_map_complete": "label"}`` makes the engine run
it over each map whose naming an enrichment run completes — preserving the
pre-B1 interleaving, where a map was reviewed the moment its labeling
finished, before descriptions started. Standalone runs go through
``enrich_run(phase="review")``. The repair path reuses the label/describe
pass implementations by plain import — pass-to-pass reuse is an import, not
engine plumbing.
"""

import logging

from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.enrichment.context import (
    build_enrichment_context,
    postprocess_links,
)
from wbworldgen.worldgen.enrichment.registry import PassSpec, register_pass
from wbworldgen.worldgen.enrichment.passes import describe, label

logger = logging.getLogger(__name__)


async def generate_review(services, rec: dict, compiled: dict) -> list:
    """One review LLM call for one map. Returns [{"id", "problem"}, ...];
    raises on failure."""
    adjacency: dict = {}
    for e in rec.get("edges", []) or []:
        a, b = e.get("from"), e.get("to")
        if a and b:
            adjacency.setdefault(a, []).append(b)
            adjacency.setdefault(b, []).append(a)
    by_id = {n.get("id"): n for n in rec.get("nodes", [])}
    lines = []
    for n in rec.get("nodes", []):
        if not n.get("name"):
            continue
        near = [by_id[nb]["name"] for nb in adjacency.get(n.get("id"), [])
                if nb in by_id and by_id[nb].get("name")]
        near_str = ", ".join(near[:6]) if near else "nothing named yet"
        lines.append(f'- id {n.get("id")}: "{n["name"]}" ({n.get("type", "place")}) — near: {near_str}')
    world = (compiled.get("lore") or {})
    premise = world.get("premise", "") if isinstance(world, dict) else ""

    system = services.prompts(
        "enrich_review_system",
        "You are reviewing the location names on one finished map of a game world. "
        "Flag ONLY real coherence problems; an empty list is the normal, expected outcome. "
        "Output ONLY valid JSON.",
    )
    user_msg = services.prompts(
        "enrich_review_user",
        f"""Map: {rec.get('label', rec.get('map_id', ''))} ({rec.get('level_type', 'map')})
Map description: {rec.get('description', '')}
World premise: {premise}

Named locations and what each is actually near on the map:
{chr(10).join(lines)}

Flag locations whose NAME does not make sense where it sits:
- a name implying it is part of, or belongs to, a specific place or institution that exists on this map but is NOT in its near list (e.g. a school's office far from the school)
- duplicates or trivial variations of another location's name
- a name that contradicts the map or its neighbors outright

Do NOT flag names for style, quality or taste. Output ONLY valid JSON:
{{"issues": [{{"id": "...", "problem": "one sentence on what is wrong"}}, ...]}} — empty "issues" if all names make sense.""",
        map_label=rec.get("label", ""),
        map_level=rec.get("level_type", ""),
    )
    await services.backoff.wait()
    async with services.semaphore:
        parsed = await json_retry_completion(
            services.llm,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user_msg}],
            model=services.llm.reader_model,
            temperature=0.3,
            inspector_ctx={"call_type": "world_build", "step": "enrich:review"},
            step_label=f"enrich:review:{rec.get('map_id', '')}",
            retry_attempts=services.json_retry_attempts,
        )
    issues = parsed.get("issues") if isinstance(parsed, dict) else None
    return [i for i in (issues if isinstance(issues, list) else [])
            if isinstance(i, dict)]


async def review_map(services, rec: dict, state) -> dict:
    """Review-and-repair one map. Returns this map's summary contribution
    {"reviewed_maps", "flagged", "relabeled"} — zeros when the map was
    skipped (fewer than two named nodes) or its review call failed, so the
    aggregate still records that a review was attempted."""
    result = {"reviewed_maps": 0, "flagged": 0, "relabeled": []}
    mid = rec.get("map_id", "")
    named = [n for n in rec.get("nodes", []) if n.get("name")]
    if len(named) < 2:
        return result
    try:
        issues = await generate_review(services, rec, state.compiled)
    except Exception as e:
        services.backoff.note_rate_limit(e)
        logger.warning("Label review failed for map %s: %s", mid, e)
        return result
    result["reviewed_maps"] = 1

    store = services.enrichment_store
    by_id = {str(n.get("id")): n for n in named}
    for issue in issues:
        node = by_id.get(str(issue.get("id", "")))
        problem = str(issue.get("problem", "")).strip()
        if node is None or not problem:
            continue
        result["flagged"] += 1
        old_name = node.get("name", "")
        context = build_enrichment_context(node, state.all_nodes, state.compiled,
                                           include_descriptions=False)
        used = [n["name"] for n in state.all_nodes if n.get("name")]
        name, snippet = await label.label_with_retries(
            services, node, context, used_names=used, problem_note=problem)
        if name is None:
            continue
        node_id = node.get("id")
        store.save_node_enrichment(state.world_id, node_id, "name", name)
        services.compiled.update_node(state.compiled, node_id, "name", name)
        node["name"] = name
        if snippet:
            store.save_node_enrichment(state.world_id, node_id, "label_description", snippet)
            services.compiled.update_node(state.compiled, node_id, "label_description", snippet)
            node["label_description"] = snippet
        if node.get("description"):
            # The old description narrates the rejected name — rework
            # it with the fresh one so the two never disagree.
            dctx = build_enrichment_context(node, state.all_nodes, state.compiled,
                                            include_descriptions=True)
            desc_links = await describe.describe_with_retries(
                services, node, dctx, existing_description=node.get("description", ""))
            if desc_links is not None:
                desc = postprocess_links(desc_links, node, state.all_nodes)
                store.save_node_enrichment(state.world_id, node_id, "description", desc)
                services.compiled.update_node(state.compiled, node_id, "description", desc)
                node["description"] = desc
        result["relabeled"].append(
            {"node_id": node_id, "map_id": mid, "old": old_name,
             "new": name, "problem": problem})
        await state.emit({"type": "review_fix", "map_id": mid, "node_id": node_id,
                          "old": old_name, "new": name, "problem": problem})
    if result["relabeled"]:
        store.flush_enrichment_cache(state.world_id)
    return result


# --- pass registration ------------------------------------------------------

async def _run_map(services, rec: dict, state) -> dict:
    return await review_map(services, rec, state)


SPEC = register_pass(PassSpec(
    id="review",
    label="Review names",
    description=(
        "Coherence-check the names on a finished map against what each "
        "location actually sits near; relabel flagged nodes (and rework "
        "their descriptions to match). Fires automatically whenever a run "
        "completes a map's naming; best-effort — a failed review never "
        "fails the run."
    ),
    unit="map",
    run=_run_map,
    after=("label",),
    triggers={"on_map_complete": "label"},
    requires=("maps", "labels"),
    summary_key="review",
))
