"""wb_worldgen module backend.

Owns the WorldBuilder instance and the world/terrain API routes. Wires itself
into the engine through ``set_services`` (called by the core server after the
engine is constructed):

  * injects llm + settings into its WorldBuilder and registers the pipeline steps
  * registers a character-generation context provider (world flavour)
  * registers a "world" story-source so create_save can produce ``world_data``
  * hands the engine/session services to the route module via ``configure``

The world code itself lives under ``wbworldgen`` (relocated from
``backend.engine``); we put the module directory on sys.path so it imports as a
self-contained package independent of the core shim.
"""

import os
import sys

_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
if _MOD_DIR not in sys.path:
    sys.path.insert(0, _MOD_DIR)

from wbworldgen.worldgen import WorldBuilder, register_default_steps  # noqa: E402

import routes as _routes  # noqa: E402  (module-local sibling, importable via _MOD_DIR on sys.path)
import terrain_routes as _terrain_routes  # noqa: E402  (experimental terrain lab API)


# Single WorldBuilder instance owned by this module; built in set_services once
# the engine's llm/settings are available.
world_builder: WorldBuilder | None = None
_services: dict | None = None


def _build_world_character_context(context: dict) -> dict:
    """Context provider for character generation.

    Reads ``context['world_id']`` from the generic, module-contributed character
    context and returns world flavour fields; ignores everything else.
    """
    world_id = (context or {}).get("world_id")
    if not world_id or world_builder is None:
        return {}
    try:
        compiled = world_builder.compile_world(world_builder.load_world(world_id))
        rules = compiled.get("rules", {})
        lore = compiled.get("lore", {})
        regions_data = compiled.get("regions", {})
        return {
            "world_name": lore.get("world_name", ""),
            "premise": lore.get("premise", ""),
            "genre": rules.get("genre", ""),
            "tone": rules.get("tone", ""),
            "magic_level": rules.get("magic_level", ""),
            "tech_era": rules.get("tech_era", ""),
            "regions": [r.get("name") for r in regions_data.get("regions", [])],
            "factions": regions_data.get("factions", []),
        }
    except Exception as e:
        print(f"[wb_worldgen] Failed to load world '{world_id}' for character context: {e}")
        return {}


def _initial_adjacency(wd: dict) -> dict:
    """Build node adjacency from a compiled world for the initial fog reveal."""
    edges = wd.get("map", {}).get("edges", [])
    map_layers = wd.get("map_layers", [])
    all_edges = list(edges)
    if map_layers:
        all_edges = []
        for layer in map_layers:
            all_edges.extend(layer.get("map", {}).get("edges", []))
    adj: dict[str, list[str]] = {}
    for e in all_edges:
        fr, to = e.get("from"), e.get("to")
        if fr and to:
            adj.setdefault(fr, []).append(to)
            adj.setdefault(to, []).append(fr)
    return adj


async def create_world_story_source(*, save_id, source_id, start_preference, session_manager, engine, character_module_data=None, character_data=None) -> dict:
    """Story-source provider for create_save: turn a world_id into a playable save.

    Loads + compiles the world, picks a start location (LLM or random), seeds
    the fog-of-war reveal, persists ``World/world_data.json``, embeds the world
    into the save's RAG index, and writes the world keys into session state.
    Returns the chosen start_location (for the API response).
    """
    import json as _json
    import random as _random

    world_id = source_id
    world_state = world_builder.load_world(world_id)
    compiled = world_builder.compile_world(world_state)

    if start_preference:
        start_location = await world_builder.llm_pick_start_location(world_id, start_preference, engine.llm)
    else:
        locations = world_builder.get_start_locations(world_id)
        start_location = _random.choice(locations) if locations else None

    player_location_node_id = None
    player_location_region = None
    player_location_layer_id = None
    revealed_node_ids: list[str] = []
    if start_location:
        player_location_node_id = start_location.get("node_id")
        player_location_region = start_location.get("region")
        player_location_layer_id = start_location.get("layer_id")
        adjacency = _initial_adjacency(compiled)
        revealed = {player_location_node_id}
        frontier = [player_location_node_id]
        for _ in range(1):
            next_frontier = []
            for nid in frontier:
                for nb in adjacency.get(nid, []):
                    if nb not in revealed:
                        revealed.add(nb)
                        next_frontier.append(nb)
            frontier = next_frontier
        revealed_node_ids = list(revealed)

    state = session_manager.create_save(
        save_id,
        world_id=world_id,
        player_location_node_id=player_location_node_id,
        player_location_region=player_location_region,
        player_location_layer_id=player_location_layer_id,
        revealed_node_ids=revealed_node_ids,
        character_module_data=character_module_data,
        character_data=character_data,
    )

    save_workspace = session_manager.data_dir / "saves" / save_id
    world_dir = save_workspace / "World"
    world_dir.mkdir(parents=True, exist_ok=True)
    with open(world_dir / "world_data.json", "w", encoding="utf-8") as f:
        _json.dump(compiled, f, indent=2)

    engine.set_memory_path(session_manager.get_memory_path())
    await engine.ensure_memory()
    world_index_path = str(save_workspace / "world_index")
    engine.memory.init_world_index(world_index_path)
    entry_count = await engine.memory.embed_world(compiled, engine.llm)
    print(f"[wb_worldgen] Embedded {entry_count} world entries for world '{world_id}'")

    session_manager.state["world_data"] = compiled
    session_manager.state["world_id"] = world_id
    session_manager.state["player_location_node_id"] = player_location_node_id
    session_manager.state["player_location_region"] = player_location_region
    session_manager.state["start_preference"] = start_preference

    return {"state": state, "start_location": start_location}


def set_services(services: dict):
    """Receive shared engine services and finish wiring the module."""
    global world_builder, _services
    _services = services

    engine = services["engine"]
    settings = services.get("settings")
    registry = services.get("registry")

    world_builder = WorldBuilder()
    world_builder.set_llm_service(engine.llm)
    if settings is not None:
        world_builder.set_settings(settings)
    if registry is not None:
        world_builder.register_module_hooks(registry)
    register_default_steps(world_builder)

    # Extend the world_rules schema from other modules' hooks (was inline in server).
    for mod_id, hook in world_builder._module_hooks.get("on_world_rules_schema", []):
        try:
            extra_fields = hook({}, None)
            if isinstance(extra_fields, dict):
                world_rules_step = world_builder._steps.get("world_rules")
                if world_rules_step:
                    world_rules_step.schema.setdefault("module_data", {"type": "object", "label": "Module Data"})
                    mod_schema = world_rules_step.schema["module_data"].setdefault("properties", {})
                    mod_schema[mod_id] = {"type": "object", "label": f"{mod_id} Rules", "properties": extra_fields}
        except Exception as e:
            print(f"[wb_worldgen] Module {mod_id} on_world_rules_schema failed: {e}")

    # Wire the route module to the live instances.
    _routes.configure(
        builder=world_builder,
        engine_ref=engine,
        session_manager_ref=services["session_manager"],
    )

    # Contribute world context to character generation.
    char_builder = services.get("character_builder")
    if char_builder is not None and hasattr(char_builder, "register_context_provider"):
        char_builder.register_context_provider(_build_world_character_context)

    # Register the world story source so create_save can build world saves.
    if hasattr(engine, "register_story_source"):
        engine.register_story_source("world", create_world_story_source)


# ---------------------------------------------------------------------------
# Turn-time contributions: location context + movement.
# These were previously hardcoded in the engine (graph.py). They now live here
# and reach the engine through standard module hooks:
#   * on_gather_context   -> per-turn <current_location> context_string
#   * on_intro_context     -> richer world block for the opening scene
#   * on_mutation_schema   -> dynamic movement schema offered to the Reader
#   * on_mutate_state      -> apply a move + fog-of-war reveal
# ---------------------------------------------------------------------------


def _build_location_context(state: dict, world_data: dict) -> str:
    node_id = state.get("player_location_node_id")
    region_name = state.get("player_location_region")
    layer_id = state.get("player_location_layer_id")
    nodes = world_data.get("map", {}).get("nodes", [])
    map_layers = world_data.get("map_layers", [])
    regions = world_data.get("regions", {}).get("regions", [])
    layer_info = world_data.get("layers", [])

    if map_layers:
        all_nodes = []
        for layer in map_layers:
            all_nodes.extend(layer.get("map", {}).get("nodes", []))
        nodes = all_nodes

    current_node = None
    for n in nodes:
        if n.get("id") == node_id:
            current_node = n
            break

    current_region = None
    if region_name:
        for r in regions:
            if r.get("name") == region_name:
                current_region = r
                break

    current_layer = None
    if layer_id:
        for layer in layer_info:
            if layer.get("layer_id") == layer_id:
                current_layer = layer
                break

    if not current_node and not current_region and not current_layer:
        return ""

    parts = ["<current_location>"]
    if current_layer:
        parts.append(f"Layer: {current_layer.get('name', layer_id)} — {current_layer.get('description', '')[:300]}")
        layer_rules = world_data.get("layer_rules", [])
        for lr in layer_rules:
            if lr.get("layer_id") == layer_id:
                rules = lr.get("rules", [])
                if rules:
                    parts.append("<layer_rules>")
                    for rule in rules:
                        parts.append(f"  - {rule}")
                    parts.append("</layer_rules>")
                break
    if current_node:
        node_name = current_node.get("name", "")
        node_type = current_node.get("type", "location")
        node_desc = current_node.get("description", "")
        if node_name and node_desc:
            parts.append(f"Location: {node_name} ({node_type}) — {node_desc[:600]}")
        elif node_name:
            parts.append(f"Location: {node_name} ({node_type})")
        if current_node.get("interlayer_connection_id"):
            map_connections = world_data.get("map_connections", [])
            for lc in map_connections:
                if lc.get("id") == current_node.get("interlayer_connection_id"):
                    target_layer = lc.get("to_layer_id") if lc.get("from_layer_id") == layer_id else lc.get("from_layer_id")
                    parts.append(f"Inter-layer connection: {lc.get('connection_type', 'passage')} to layer '{target_layer}' — {lc.get('description', '')[:200]}")
                    break
    if current_region:
        parts.append(f"Region: {current_region.get('name', '')}")
        parts.append(f"Terrain: {current_region.get('terrain', 'N/A')[:400]}")
        parts.append(f"Climate: {current_region.get('climate', 'N/A')[:200]}")
        landmarks = current_region.get("landmarks", [])
        if landmarks:
            parts.append(f"Nearby Landmarks: {', '.join(landmarks[:5])}")
        factions = current_region.get("factions", [])
        if factions:
            parts.append(f"Local Factions: {', '.join(factions[:5])}")
    if not current_region and region_name:
        parts.append(f"Region: {region_name}")
    parts.append("</current_location>")
    return "\n".join(parts)


def _build_location_mutation_schema(world_data: dict) -> dict:
    nodes = world_data.get("map", {}).get("nodes", [])
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        all_nodes = []
        for layer in map_layers:
            all_nodes.extend(layer.get("map", {}).get("nodes", []))
        nodes = all_nodes
    regions = world_data.get("regions", {}).get("regions", [])
    location_options = []
    for n in nodes:
        if n.get("name"):
            location_options.append(f"{n['id']} ({n.get('name', '')})")
    if not location_options:
        location_options = ["any"]
    region_names = [r.get("name", "") for r in regions if r.get("name")]
    layers_list = world_data.get("layers", [])
    layer_options = [f"{l.get('layer_id', '')} ({l.get('name', '')})" for l in layers_list if l.get("layer_id")]
    if not layer_options:
        layer_options = ["surface"]
    return {
        "player_location_changed": {"type": "boolean", "label": "Did the player move to a new location?"},
        "player_location_node_id": {
            "type": "select",
            "label": "New location node ID",
            "options": location_options[:30],
            "description": "The node_id of the location the player moved to. Set only if player_location_changed is true."
        },
        "player_location_region": {
            "type": "select",
            "label": "New region name",
            "options": region_names[:20],
            "description": "The region the player moved into. Set only if player_location_changed is true."
        },
        "player_location_layer_id": {
            "type": "select",
            "label": "New layer ID",
            "options": layer_options[:10],
            "description": "The layer_id the player moved to (e.g., overworld, underground). Set only if the layer changed."
        },
    }


def _build_graph_adjacency(world_data: dict) -> dict:
    edges = world_data.get("map", {}).get("edges", [])
    map_layers = world_data.get("map_layers", [])
    all_edges = list(edges)
    if map_layers:
        all_edges = []
        for layer in map_layers:
            all_edges.extend(layer.get("map", {}).get("edges", []))
    adj: dict[str, list[str]] = {}
    for e in all_edges:
        fr, to = e.get("from"), e.get("to")
        if fr and to:
            adj.setdefault(fr, []).append(to)
            adj.setdefault(to, []).append(fr)
    return adj


def _reveal_bfs(start_id: str, adjacency: dict, radius: int) -> set:
    visited = {start_id}
    frontier = [start_id]
    for _ in range(radius):
        next_frontier = []
        for nid in frontier:
            for nb in adjacency.get(nid, []):
                if nb not in visited:
                    visited.add(nb)
                    next_frontier.append(nb)
        frontier = next_frontier
    return visited


async def on_gather_context(state: dict, sdk) -> dict:
    """Per-turn world context: the player's current location block."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    location_context = _build_location_context(state, world_data)
    if not location_context:
        return {}
    return {"context_string": location_context}


async def on_intro_context(state: dict, sdk) -> dict:
    """Opening-scene world block: rules + premise + current location."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    parts = []
    rules = world_data.get("rules", {})
    lore = world_data.get("lore", {})
    if rules:
        parts.append("<world_rules>")
        parts.append(f"Genre: {rules.get('genre', 'N/A')}")
        parts.append(f"Tone: {rules.get('tone', 'N/A')}")
        parts.append(f"Magic Level: {rules.get('magic_level', 'N/A')}")
        parts.append(f"Technology Era: {rules.get('tech_era', 'N/A')}")
        parts.append(f"Lethality: {rules.get('lethality', 'N/A')}/10")
        custom_rules = rules.get("custom_rules", [])
        if custom_rules:
            parts.append("Custom Rules:")
            for rule in custom_rules:
                parts.append(f"  - {rule}")
        parts.append("</world_rules>")
    if lore:
        parts.append("<world_premise>")
        world_name = lore.get("world_name", "")
        if world_name:
            parts.append(f"World: {world_name}")
        premise = lore.get("premise", "")
        if premise:
            parts.append(premise)
        central_conflict = lore.get("central_conflict", "")
        if central_conflict:
            parts.append(f"Central Conflict: {central_conflict}")
        creation_myth = lore.get("creation_myth", "")
        if creation_myth:
            parts.append(f"Creation Myth: {creation_myth}")
        eras = lore.get("historical_eras", [])
        if eras:
            parts.append("Historical Eras:")
            for era in eras:
                parts.append(f"  - {era.get('name', '')}: {era.get('summary', '')}")
        parts.append("</world_premise>")
    location_text = _build_location_context(state, world_data)
    if location_text:
        parts.append(location_text)
    if not parts:
        return {}
    return {"content": "\n".join(parts)}


async def on_mutation_schema(state: dict, sdk) -> dict:
    """Offer the Reader a dynamic movement schema derived from the world map."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    return _build_location_mutation_schema(world_data)


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict:
    """Apply a player move and reveal newly-adjacent nodes (fog-of-war)."""
    if not mutation:
        return {}
    world_data = state.get("world_data")
    if not world_data:
        return {}
    new_node_id = mutation.get("player_location_node_id")
    new_region = mutation.get("player_location_region")
    new_layer_id = mutation.get("player_location_layer_id")
    if not new_node_id or new_node_id == state.get("player_location_node_id"):
        return {}

    revealed = list(set(state.get("revealed_node_ids", [])))
    adjacency = _build_graph_adjacency(world_data)
    for nid in _reveal_bfs(new_node_id, adjacency, radius=1):
        if nid not in revealed:
            revealed.append(nid)

    return {
        "player_location_node_id": new_node_id,
        "player_location_region": new_region or state.get("player_location_region"),
        "player_location_layer_id": new_layer_id or state.get("player_location_layer_id"),
        "revealed_node_ids": revealed,
    }


def get_router():
    # Combine the world-generation routes with the experimental terrain routes
    # under one router; the core server mounts it at root so the original paths
    # (/api/world/*, /api/terrain/*) are preserved.
    combined = _routes.APIRouter()
    # Extend with the concrete route objects (not include_router) so the routes
    # are eagerly present and carry their absolute /api/* paths — the core server
    # inspects these to decide the mount prefix.
    combined.routes.extend(_routes.router.routes)
    combined.routes.extend(_terrain_routes.router.routes)
    return combined
