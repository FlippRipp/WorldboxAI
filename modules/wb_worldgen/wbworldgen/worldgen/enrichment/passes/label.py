"""The labeling pass: give every map node a name and one-line label.

Single-node and batched LLM calls plus the batch validation/bisect strategy
live here as module-level functions — the review pass imports them for its
repair path, and tests monkeypatch them (``generate_label``,
``generate_label_batch``) to fake LLM output. All callers resolve them
through module globals at call time, so patching works everywhere.
"""

import logging

from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.enrichment.context import build_enrichment_context
from wbworldgen.worldgen.enrichment.registry import PassSpec, register_pass
from wbworldgen.worldgen.enrichment.passes.common import (
    call_with_retries,
    connection_block,
    strip_leading_the,
    terrain_line,
)

logger = logging.getLogger(__name__)


async def generate_label(services, node: dict, context: dict, used_names=None,
                         problem_note: str = None) -> tuple:
    """One labeling LLM call. Returns (name, label_description); raises on
    failure. ``problem_note`` carries a reviewer's objection when relabeling
    a rejected name."""
    node_type = node.get("type", "waypoint")
    node_id = node.get("id", "")
    importance = node.get("importance", 0)

    world = context.get("world", {})
    layer = context.get("layer", {})
    region = context.get("region", {})
    neighbors = context.get("neighbors", [])

    neighbor_names = [n.get("name", n.get("link_id", "?")) for n in neighbors[:5]]
    neighbor_str = ", ".join(neighbor_names) if neighbor_names else "none"

    region_factions = region.get("factions", [])
    region_landmarks = region.get("landmarks", [])
    factions_str = f"- Factions: {', '.join(region_factions)}\n" if region_factions else ""
    landmarks_str = f"- Notable landmarks: {', '.join(region_landmarks)}\n" if region_landmarks else ""
    terrain_str = terrain_line(context.get("terrain", {}))

    model = services.llm.module_fast_model or services.llm.reader_model
    temperature = services.temperature or 0.9

    system = services.prompts(
        "enrich_label_system",
        "You are a world-building AI. Generate a concise, evocative name and a one-line label description for a map node.",
    )
    guidance = [
        "Do not begin the name with the word \"The\".",
        # Containment rule: independent labeling calls know nothing about
        # where other named places sit on the map, so a name that presents
        # this node as part of another place is only safe when that place
        # is verifiably right here (in the node's neighbor list).
        "Name this location as a standalone place. Only name it as a part of "
        "another location (its rooftop, gate, courtyard, storage, annex, "
        "district and the like) if that location appears in the Nearby nodes "
        "list — anything else on the map may be far away from here.",
        # Implied membership is containment too: a "Student Council Office"
        # belongs to a school even without naming one, and reads as absurd
        # if the school is across the map.
        "The same applies to implied ownership: do not invent a place that "
        "plainly belongs to a specific kind of institution or site (an "
        "office of a council, a ward of a hospital, a dock of a harbor) "
        "unless a fitting parent is in the Nearby nodes list. Otherwise "
        "pick a place that stands on its own.",
    ]
    if problem_note:
        guidance.append(
            f'This node was previously named "{node.get("name", "")}" but that '
            f"name was rejected on review: {problem_note} Author a different "
            "name that does not have this problem.")
    named_elsewhere = [str(n) for n in (used_names or []) if n]
    if named_elsewhere:
        guidance.append(
            "Places that already exist elsewhere on this map — do not reuse "
            "these names, and do not name this node as a part or sub-location "
            "of any of them (unless listed as nearby): "
            + ", ".join(named_elsewhere))
    connection_str = connection_block(context.get("connection", {}), context.get("vocab"))
    if connection_str:
        guidance.append(connection_str)
    system = system + "\n\n" + "\n".join(guidance)
    user_msg = services.prompts(
        "enrich_label_user",
        f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}
{factions_str}{landmarks_str}{terrain_str}
Node details:
- ID: {node_id}
- Type: {node_type}
- Importance: {importance}/10
- Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
- Layer description: {layer.get('description', '')}
- Nearby nodes: {neighbor_str}

Generate a unique, fitting name for this {node_type} and a short one-line description (label_description).
Output ONLY valid JSON: {{"name": "...", "label_description": "..."}}""",
        world_name=world.get('name', 'Unknown'),
        world_genre=world.get('genre', ''),
        world_tone=world.get('tone', ''),
        world_premise=world.get('premise', ''),
        node_id=node_id,
        node_type=node_type,
        node_importance=str(importance),
        layer_name=layer.get('name', 'surface'),
        layer_type=layer.get('type', 'surface'),
        layer_description=layer.get('description', ''),
        neighbor_names=neighbor_str,
        region_name=region.get('name', 'unknown'),
        region_terrain=region.get('terrain', ''),
        region_climate=region.get('climate', ''),
        region_factions=factions_str,
        region_landmarks=landmarks_str,
        node_biome=context.get("terrain", {}).get("biome", ""),
        node_elevation=context.get("terrain", {}).get("elevation_band", ""),
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    try:
        result = await json_retry_completion(
            services.llm,
            messages=messages,
            model=model,
            temperature=temperature,
            inspector_ctx={"call_type": "world_build", "step": "enrich:label"},
            step_label=f"enrich:label:{node_id}",
            retry_attempts=services.json_retry_attempts,
        )
        return strip_leading_the(result.get("name", "Unknown")), result.get("label_description", "")
    except Exception as e:
        logger.error(f"Label generation failed for node {node_id}: {e}")
        raise


async def label_with_retries(services, node: dict, context: dict, used_names=None,
                             problem_note: str = None) -> tuple:
    """Label one node with transient-error retries. (None, None) on failure."""
    result = await call_with_retries(
        services,
        lambda: generate_label(services, node, context, used_names=used_names,
                               problem_note=problem_note),
        what="Label generation", node_id=node.get("id"))
    return (None, None) if result is None else result


async def generate_label_batch(services, batch: list, contexts: dict,
                               used_names: list) -> dict:
    """One batched labeling LLM call for several nodes. Returns the parsed
    {"nodes": [{"id", "name", "label_description"}, ...]} payload; raises on
    failure."""
    model = services.llm.module_fast_model or services.llm.reader_model
    temperature = services.temperature or 0.9

    # Same world for every node in the batch.
    world = contexts.get(batch[0].get("id"), {}).get("world", {})

    lines = []
    for i, node in enumerate(batch, 1):
        ctx = contexts.get(node.get("id"), {})
        region = ctx.get("region", {})
        layer = ctx.get("layer", {})
        neighbor_names = [n.get("name") for n in ctx.get("neighbors", [])[:4] if n.get("name")]
        parts = [
            f"{i}. id: {node.get('id')}",
            f"type: {node.get('type', 'waypoint')}",
            f"importance: {node.get('importance', 0)}/10",
        ]
        if region.get("name"):
            parts.append(f"region: {region.get('name')} ({region.get('terrain', '')}, {region.get('climate', '')})")
        if layer.get("name"):
            parts.append(f"layer: {layer.get('name')} ({layer.get('type', 'surface')})")
        terrain = ctx.get("terrain", {})
        if terrain.get("biome"):
            parts.append(f"terrain: {terrain['biome']}")
        if neighbor_names:
            parts.append(f"near: {', '.join(neighbor_names)}")
        connection = ctx.get("connection", {})
        if connection:
            parts.append(f"NOTE: {connection_block(connection, ctx.get('vocab'))}")
        lines.append(" | ".join(parts))
    nodes_block = "\n".join(lines)

    avoid = [str(n) for n in used_names if n]
    avoid_block = (
        "Already-used names (do NOT reuse or lightly vary these, and do NOT name any "
        "location below as a part or sub-location of them):\n" + ", ".join(avoid) + "\n\n"
    ) if avoid else ""

    system = services.prompts(
        "enrich_label_batch_system",
        "You are a world-building AI. Name several map locations at once. Give each a concise, "
        "evocative name and a one-line label description. Names must be distinct from each other "
        "and from the already-used names; vary naming styles across the batch. Never begin a name "
        "with the word \"The\".",
    )
    # The batch is importance-ordered, i.e. spatially scattered: two entries
    # are usually nowhere near each other, and neither are the already-used
    # names. Without this rule the model happily builds name families
    # ("Northgate School" + "School Rooftop") across distant nodes.
    system += (
        "\n\nThe locations in one batch may be far apart on the map. Name each as a "
        "standalone place: only name a location as a part of another place (its "
        "rooftop, gate, courtyard, storage, annex, district and the like) if that "
        "place appears in that location's own near list. Never name one batch entry "
        "as a part of another batch entry unless they are listed as near each other. "
        "This includes implied ownership: do not invent a place that plainly belongs "
        "to a specific kind of institution or site (an office of a council, a ward "
        "of a hospital, a dock of a harbor) unless a fitting parent is in that "
        "location's near list — otherwise pick a place that stands on its own."
    )
    user_msg = services.prompts(
        "enrich_label_batch_user",
        f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

{avoid_block}Locations to name:
{nodes_block}

Generate a unique, fitting name and a short one-line label_description for EVERY location above.
Output ONLY valid JSON: {{"nodes": [{{"id": "...", "name": "...", "label_description": "..."}}, ...]}} with exactly {len(batch)} entries whose ids match the list.""",
        world_name=world.get('name', 'Unknown'),
        world_genre=world.get('genre', ''),
        world_tone=world.get('tone', ''),
        world_premise=world.get('premise', ''),
        nodes_block=nodes_block,
        used_names=", ".join(avoid),
        batch_size=str(len(batch)),
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    return await json_retry_completion(
        services.llm,
        messages=messages,
        model=model,
        temperature=temperature,
        inspector_ctx={"call_type": "world_build", "step": "enrich:label_batch"},
        step_label=f"enrich:label_batch:{len(batch)}",
        retry_attempts=services.json_retry_attempts,
    )


async def run_label_batch(services, batch: list, all_nodes: list, compiled: dict,
                          used_names: list, _depth: int = 0) -> tuple:
    """One batched labeling call. Returns (results, leftovers): results maps
    node_id -> {"name", "label_description"} for entries that validated;
    leftovers are nodes to re-run as single-node calls (missing/invalid/
    duplicate names, or the whole batch when the call itself kept failing)."""
    if len(batch) == 1:
        # Degenerate batch: the single-node path has the better retry story.
        return {}, list(batch)
    contexts = {
        n.get("id"): build_enrichment_context(n, all_nodes, compiled, include_descriptions=False)
        for n in batch
    }
    try:
        await services.backoff.wait()
        async with services.semaphore:
            parsed = await generate_label_batch(services, batch, contexts, used_names)
    except Exception as e:
        services.backoff.note_rate_limit(e)
        if _depth == 0 and len(batch) >= 4:
            logger.warning("Batch labeling failed (%d nodes), bisecting: %s", len(batch), e)
            mid = len(batch) // 2
            res_a, left_a = await run_label_batch(services, batch[:mid], all_nodes, compiled, used_names, _depth=1)
            res_b, left_b = await run_label_batch(services, batch[mid:], all_nodes, compiled, used_names, _depth=1)
            res_a.update(res_b)
            return res_a, left_a + left_b
        logger.warning("Batch labeling failed (%d nodes), falling back to single calls: %s", len(batch), e)
        return {}, list(batch)

    entries = parsed.get("nodes") if isinstance(parsed, dict) else None
    by_id = {}
    for entry in (entries if isinstance(entries, list) else []):
        if isinstance(entry, dict) and entry.get("id") is not None:
            by_id[str(entry["id"])] = entry

    results = {}
    leftovers = []
    seen = {str(n).strip().lower() for n in used_names if n}
    for node in batch:
        node_id = node.get("id")
        entry = by_id.get(str(node_id))
        name = strip_leading_the(str((entry or {}).get("name") or "")).strip()
        if not name or name.lower() in seen:
            leftovers.append(node)
            continue
        seen.add(name.lower())
        results[node_id] = {"name": name,
                            "label_description": str((entry or {}).get("label_description") or "")}
    return results, leftovers


# --- pass registration ------------------------------------------------------

def _used_names(state) -> list:
    return [n["name"] for n in state.all_nodes if n.get("name")]


async def _run_node(services, node: dict, state) -> dict:
    context = build_enrichment_context(node, state.all_nodes, state.compiled,
                                       include_descriptions=False)
    name, snippet = await label_with_retries(services, node, context,
                                             used_names=_used_names(state))
    if name is None:
        return None
    return {"name": name, "label_description": snippet}


async def _run_batch(services, batch: list, state) -> tuple:
    return await run_label_batch(services, batch, state.all_nodes,
                                 state.compiled, _used_names(state))


def _event_fields(fields: dict) -> dict:
    return {"label": fields.get("name"),
            "label_description": fields.get("label_description", "")}


SPEC = register_pass(PassSpec(
    id="label",
    label="Name locations",
    description=(
        "Give every map location a unique, setting-appropriate name and a "
        "one-line label description, most important nodes first. Runs "
        "batched (several nodes per LLM call) when batching is enabled."
    ),
    unit="node",
    run=_run_node,
    is_done=lambda n: bool(n.get("name")),
    requires=("maps",),
    produces=("labels",),
    batchable=True,
    run_batch=_run_batch,
    event_fields=_event_fields,
    summary_key="labeled",
))
