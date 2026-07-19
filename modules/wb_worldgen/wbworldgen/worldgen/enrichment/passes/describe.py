"""The description pass: atmospheric flavor text for named map nodes.

The LLM call lives here as a module-level function (``generate_description``)
— the review pass imports it to rework descriptions after a relabel, and
tests monkeypatch it to fake LLM output.
"""

import asyncio
import logging
import re

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
                               existing_description: str = "") -> str:
    """One description LLM call (with its own short-content/transient retry
    loop and a label-based fallback). ``existing_description`` switches the
    prompt into revise-and-enrich mode (rework, review repairs);
    ``context["guidance"]`` carries the run-level steering note (C1's
    guidance channel — a context key so this signature, a test patch point,
    stays stable)."""
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

    if existing_description:
        system_fallback = (
            "You are a world-building AI. Revise and enrich an existing flavor description for a "
            "map location using fresh context about its neighbors. Preserve any still-fitting "
            "details from the original but deepen it with the new context. Reference neighboring "
            "locations using their ${link_ID} syntax."
        )
        rework_block = f"\nExisting description (revise/update, don't just repeat): {existing_description}\n"
        instruction = (
            "Rewrite this into an updated 1-3 sentence flavor description of this location, weaving in "
            "the nearby locations listed above. Reference neighbors using their link IDs like "
            "${link_n_0001} or ${link_a1b2} (the same format used in the neighbor list above)."
        )
    else:
        system_fallback = "You are a world-building AI. Write a short, atmospheric flavor description for a map location. Reference neighboring locations using their ${link_ID} syntax."
        rework_block = ""
        instruction = (
            "Write a 1-3 sentence flavor description of this location. Reference neighbors using "
            "their link IDs like ${link_n_0001} or ${link_a1b2} (the same format used in the neighbor list above)."
        )

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
Output ONLY the description text, no JSON wrapper.""",
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
        node_biome=context.get("terrain", {}).get("biome", ""),
        node_elevation=context.get("terrain", {}).get("elevation_band", ""),
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    temperature = float(temperature)
    for attempt in range(3):
        try:
            content = await services.llm.simple_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                inspector_ctx={"call_type": "world_build", "step": f"enrich:description:{'retry' if attempt else 'initial'}"},
            )
            content = content.strip()
            content = re.sub(r'^```[a-zA-Z]*\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
            content = content.strip()
            if len(content) >= 10:
                return content
            logger.warning("Description too short for node %s (%d chars), retrying (attempt %d)", node_id, len(content), attempt + 1)
            temperature = min(temperature + 0.1, 1.0)
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            logger.warning("Transient error for description node %s (attempt %d): %s", node_id, attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                temperature = min(temperature + 0.1, 1.0)
                continue
            raise
        except Exception:
            raise

    if label_description:
        return label_description
    return f"A notable {node_type} within {world.get('name', 'the world')}."


async def describe_with_retries(services, node: dict, context: dict,
                                existing_description: str = ""):
    """Describe one node with transient-error retries. None on failure."""
    return await call_with_retries(
        services,
        lambda: generate_description(services, node, context,
                                     existing_description=existing_description),
        what="Description generation", node_id=node.get("id"))


# --- pass registration ------------------------------------------------------

async def _run_node(services, node: dict, state) -> dict:
    context = build_enrichment_context(node, state.all_nodes, state.compiled,
                                       include_descriptions=True)
    if state.guidance:
        context["guidance"] = state.guidance
    existing = node.get("description", "") if state.rework else ""
    desc_with_links = await describe_with_retries(services, node, context,
                                                  existing_description=existing)
    if desc_with_links is None:
        return None
    return {"description": postprocess_links(desc_with_links, node, state.all_nodes)}


SPEC = register_pass(PassSpec(
    id="describe",
    label="Describe locations",
    description=(
        "Write a short atmospheric flavor description for every named "
        "location, weaving in linked references to its neighbors. Requires "
        "names, so it runs after the label pass."
    ),
    unit="node",
    run=_run_node,
    is_done=lambda n: bool(n.get("description")),
    in_domain=lambda n: bool(n.get("name")),
    after=("label",),
    requires=("maps", "labels"),
    produces=("descriptions",),
    summary_key="described",
))
