"""The description pass: prose for named map nodes, in two channels.

One LLM call writes both ``description`` (the surface: what a visitor
standing there perceives — player-visible on the map UI) and
``additional_details`` (storyteller-only depth: history, hooks,
"Secret:"-marked facts; never rendered by player-facing UI — see
docs/design/node_info_layering_plan.md).

The LLM call lives here as a module-level function (``generate_description``)
— the review pass imports it to rework descriptions after a relabel, and
tests monkeypatch it to fake LLM output. It returns a
``(description, additional_details)`` tuple, mirroring ``generate_label``.
"""

import asyncio
import logging

from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.enrichment.context import (
    build_enrichment_context,
    postprocess_links,
)
from wbworldgen.worldgen.enrichment.registry import PassSpec, register_pass
from wbworldgen.worldgen.enrichment.passes.common import (
    call_with_retries,
    connection_block,
    terrain_line,
)

logger = logging.getLogger(__name__)


async def generate_description(services, node: dict, context: dict,
                               existing_description: str = "",
                               existing_details: str = "") -> tuple:
    """One description LLM call (with its own short-content/transient retry
    loop and a label-based fallback). Returns ``(description,
    additional_details)``; the details half may be empty (the node then
    stays pending for this pass, so a later run can fill it).
    ``existing_description`` switches the prompt into revise-and-enrich mode
    (rework, review repairs, details backfill on already-described nodes);
    ``context["guidance"]`` carries the run-level steering note (C1's
    guidance channel — a context key so this signature, a test patch point,
    stays stable across steering changes)."""
    node_id = node.get("id", "")
    node_name = node.get("name", "Unnamed")
    node_type = node.get("type", "waypoint")
    label_description = node.get("label_description", "")

    world = context.get("world", {})
    layer = context.get("layer", {})
    region = context.get("region", {})
    neighbors = context.get("neighbors", [])

    labeled_neighbors = [n for n in neighbors if n.get("name")]
    neighbor_str = ", ".join(
        [f"{n.get('name', '?')} ({n.get('type', '?')}, link_id: {n.get('link_id', '?')})" for n in labeled_neighbors[:5]]
    ) or "none"

    model = services.llm.reader_model
    temperature = services.temperature or 0.9

    channel_spec = (
        "- description: 1-3 sentences of surface flavor — what a visitor standing here perceives "
        "(sight, sound, smell). Reference neighbors using their link IDs like ${link_n_0001} or "
        "${link_a1b2} (the same format used in the neighbor list above).\n"
        "- additional_details: 2-4 sentences for the storyteller only — depth the surface doesn't "
        "show: history, who holds power here, tensions, a story hook. When one fits, include a "
        "genuinely hidden fact marked with a leading 'Secret:'."
    )
    if existing_description:
        system_fallback = (
            "You are a world-building AI. Revise and enrich the flavor prose of a map location "
            "using fresh context about its neighbors. The prose has two channels: 'description' "
            "(the surface — what a visitor perceives) and 'additional_details' (storyteller-only "
            "depth the player never reads directly). Preserve any still-fitting details from the "
            "existing text but deepen it with the new context. Reference neighboring locations "
            "using their ${link_ID} syntax. Output ONLY valid JSON."
        )
        rework_block = f"\nExisting description (revise/update, don't just repeat): {existing_description}\n"
        if existing_details:
            rework_block += f"Existing storyteller details (keep what still fits, extend or revise): {existing_details}\n"
        instruction = (
            "Rewrite this location's two prose channels, weaving in the nearby locations "
            f"listed above:\n{channel_spec}"
        )
    else:
        system_fallback = (
            "You are a world-building AI. Write flavor prose for a map location in two channels: "
            "'description' (the surface — what a visitor standing there perceives: sight, sound, "
            "smell) and 'additional_details' (storyteller-only depth the player never reads "
            "directly: history, inhabitants, tensions, story hooks, and hidden facts each marked "
            "with a leading 'Secret:'). Reference neighboring locations using their ${link_ID} "
            "syntax. Output ONLY valid JSON."
        )
        rework_block = ""
        instruction = f"Write this location's two prose channels:\n{channel_spec}"

    system = services.prompts("enrich_description_system", system_fallback)
    connection_str = connection_block(context.get("connection", {}), context.get("vocab"))
    if connection_str:
        system = system + "\n\n" + connection_str
    if context.get("guidance"):
        system = system + f"\n\nSteering note for this run: {context['guidance']}"
    if context.get("notes"):
        system = system + (
            "\n\nAgreed design notes for this map — established facts the "
            "description must fit (weave them in where they touch this "
            "place):\n" + "\n".join(f"- {n}" for n in context["notes"]))
    user_msg = services.prompts(
        "enrich_description_user",
        f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}
{terrain_line(context.get('terrain', {}))}
Location: {node_name}
Label: {label_description}
Type: {node_type}
Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
Layer description: {layer.get('description', '')}
Nearby locations: {neighbor_str}
{rework_block}
{instruction}
Output ONLY valid JSON: {{"description": "...", "additional_details": "..."}}""",
        world_name=world.get('name', 'Unknown'),
        world_genre=world.get('genre', ''),
        world_tone=world.get('tone', ''),
        world_premise=world.get('premise', ''),
        node_name=node_name,
        label_description=label_description,
        node_type=node_type,
        layer_name=layer.get('name', 'surface'),
        layer_type=layer.get('type', 'surface'),
        layer_description=layer.get('description', ''),
        neighbor_names=neighbor_str,
        region_name=region.get('name', 'unknown'),
        region_terrain=region.get('terrain', ''),
        region_climate=region.get('climate', ''),
        existing_description=existing_description,
        existing_details=existing_details,
        node_biome=context.get("terrain", {}).get("biome", ""),
        node_elevation=context.get("terrain", {}).get("elevation_band", ""),
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    temperature = float(temperature)
    for attempt in range(3):
        parsed = None
        try:
            parsed = await json_retry_completion(
                services.llm,
                messages=messages,
                model=model,
                temperature=temperature,
                inspector_ctx={"call_type": "world_build", "step": f"enrich:description:{'retry' if attempt else 'initial'}"},
                step_label=f"enrich:describe:{node_id}",
                retry_attempts=services.json_retry_attempts,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            logger.warning("Transient error for description node %s (attempt %d): %s", node_id, attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                temperature = min(temperature + 0.1, 1.0)
                continue
            raise
        except ValueError:
            # JSON retries exhausted — treat like short content below.
            logger.warning("Description JSON failed for node %s (attempt %d)", node_id, attempt + 1)
        if parsed is not None:
            description = str(parsed.get("description", "") or "").strip()
            details = str(parsed.get("additional_details", "") or "").strip()
            if len(description) >= 10:
                return description, details
            logger.warning("Description too short for node %s (%d chars), retrying (attempt %d)", node_id, len(description), attempt + 1)
        temperature = min(temperature + 0.1, 1.0)

    if label_description:
        return label_description, ""
    return f"A notable {node_type} within {world.get('name', 'the world')}.", ""


async def describe_with_retries(services, node: dict, context: dict,
                                existing_description: str = "",
                                existing_details: str = ""):
    """Describe one node with transient-error retries. Returns the
    ``(description, additional_details)`` tuple, or None on failure."""
    return await call_with_retries(
        services,
        lambda: generate_description(services, node, context,
                                     existing_description=existing_description,
                                     existing_details=existing_details),
        what="Description generation", node_id=node.get("id"))


# --- pass registration ------------------------------------------------------

async def _run_node(services, node: dict, state) -> dict:
    context = build_enrichment_context(node, state.all_nodes, state.compiled,
                                       include_descriptions=True)
    if state.guidance:
        context["guidance"] = state.guidance
    # Revise-and-enrich whenever prose already exists — rework runs, review
    # repairs, and details backfill on already-described nodes (old worlds)
    # all keep the standing description instead of clobbering it.
    result = await describe_with_retries(
        services, node, context,
        existing_description=node.get("description", ""),
        existing_details=node.get("additional_details", ""))
    if result is None:
        return None
    desc_with_links, details_with_links = result
    return {
        "description": postprocess_links(desc_with_links, node, state.all_nodes),
        "additional_details": postprocess_links(details_with_links, node, state.all_nodes),
    }


SPEC = register_pass(PassSpec(
    id="describe",
    label="Describe locations",
    description=(
        "Write each named location's two prose channels: a short surface "
        "description of what a visitor perceives (player-visible), and "
        "storyteller-only additional details — depth, hooks and "
        "'Secret:'-marked facts the player never reads directly. Weaves in "
        "linked references to neighbors. Requires names, so it runs after "
        "the label pass."
    ),
    unit="node",
    run=_run_node,
    is_done=lambda n: bool(n.get("description")) and bool(n.get("additional_details")),
    in_domain=lambda n: bool(n.get("name")),
    after=("label",),
    requires=("maps", "labels"),
    produces=("descriptions",),
    summary_key="described",
))
