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


async def create_world_story_source(*, save_id, source_id, start_preference, session_manager, engine, start_location_node_id=None, character_module_data=None, character_data=None) -> dict:
    """Story-source provider for create_save: turn a world_id into a playable save.

    Uses the start location the player already picked on the start screen
    (``start_location_node_id``) when given; otherwise picks one (LLM from the
    preference, or random). Seeds the fog-of-war reveal, persists
    ``World/world_data.json``, embeds the world into the save's RAG index, and
    writes the world keys into session state.
    Returns the chosen start_location (for the API response).
    """
    import json as _json
    import random as _random

    world_id = source_id
    world_state = world_builder.load_world(world_id)
    compiled = world_builder.compile_world(world_state)

    start_location = None
    if start_location_node_id:
        locations = world_builder.get_start_locations(world_id)
        start_location = next((l for l in locations if l.get("node_id") == start_location_node_id), None)
    if start_location is None and start_preference:
        start_location = await world_builder.llm_pick_start_location(world_id, start_preference, engine.llm)
        if start_location and start_location.get("generated"):
            # The pick authored a brand-new start location onto the map —
            # recompile so the save's world_data carries it.
            world_state = world_builder.load_world(world_id)
            compiled = world_builder.compile_world(world_state)
    if start_location is None:
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
        try:
            settings.register(
                "world.enrichment_concurrency", "slider", 3,
                label="Enrichment Concurrency",
                category="World Building",
                description="How many map nodes are labeled/described in parallel during world enrichment. Set to 1 for rate-limited providers.",
                is_global=True,
                min=1, max=6,
            )
            settings.register(
                "world.enrichment_batch_size", "slider", 8,
                label="Enrichment Label Batch Size",
                category="World Building",
                description="How many map nodes are named per LLM call during enrichment. 1 disables batching (one call per node).",
                is_global=True,
                min=1, max=10,
            )
            settings.register(
                "world.travel_turns_per_edge", "slider", 2,
                label="Travel Pace",
                category="World Building",
                description="How many story turns it takes to cross one average map route between locations. Journeys to distant places take proportionally longer. 0 = instant travel (the player jumps straight to the destination).",
                is_global=True,
                min=0, max=8,
            )
            settings.register(
                "world.upfront_detail", "select", "major_locations",
                label="Upfront World Detail",
                category="World Building",
                description="How much of the map is named/described at world creation. 'major_locations' details only settlements, landmarks and other important nodes upfront — the rest is generated silently in the background during play as the story approaches it. 'full' details every node upfront (slower, more tokens).",
                is_global=True,
                options=["major_locations", "full"],
            )
            settings.register(
                "world.backfill_per_turn", "slider", 2,
                label="Background Detail Per Turn",
                category="World Building",
                description="How many not-yet-detailed map locations are quietly named/described in the background each story turn (visited and revealed areas always come first). 0 disables the idle trickle — only places the story actually approaches get detailed.",
                is_global=True,
                min=0, max=5,
            )
            settings.register(
                "world.site_expansion_mode", "select", "prefetch",
                label="Location Interiors",
                category="World Building",
                description="When major locations (cities, ports, strongholds) get their interior detail — districts, venues, layout — generated. 'prefetch' starts generating while the player travels toward one so it's ready on arrival; 'on_arrival' generates when they get there; 'manual' only via the map's Explore button; 'off' never. Interiors are generated once per world and cached.",
                is_global=True,
                options=["prefetch", "on_arrival", "manual", "off"],
            )
            settings.register(
                "world.site_max_sublocations", "slider", 10,
                label="Interior Detail Size",
                category="World Building",
                description="Maximum sub-locations (districts, venues, notable places) generated inside a major location's interior.",
                is_global=True,
                min=4, max=16,
            )
        except Exception as e:
            print(f"[wb_worldgen] Failed to register enrichment settings: {e}")
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


def _build_travel_context(travel: dict, state: dict, world_data: dict) -> str:
    """<current_location> variant for a player who is on the road between nodes."""
    nodes_by_id = {n.get("id"): n for n in _all_map_nodes(world_data)}
    route = travel.get("route", [])
    leg_index = travel.get("leg_index", 0)
    if len(route) < 2 or leg_index >= len(route) - 1:
        return ""
    from_node = nodes_by_id.get(route[leg_index], {})
    to_node = nodes_by_id.get(route[leg_index + 1], {})
    dest_node = nodes_by_id.get(travel.get("destination_node_id"), {})

    def _label(node, fallback):
        return node.get("name") or node.get("id") or fallback

    from_name = _label(from_node, "the last waypoint")
    to_name = _label(to_node, "the next waypoint")
    dest_name = _label(dest_node, "the destination")

    leg_distance = travel.get("leg_distance") or 1.0
    pct = int(round(100 * min(travel.get("leg_progress", 0.0) / leg_distance, 1.0)))
    speed = _travel_speed(world_data)
    turns_left = None
    if speed:
        adjacency = _weighted_adjacency(world_data)
        turns_left = max(1, int(-(-_remaining_travel(travel, adjacency) // speed)))

    parts = ["<current_location>"]
    parts.append(f"Status: EN ROUTE — the player is traveling from {from_name} toward {to_name}, about {pct}% of the way along this stretch.")
    if dest_name != to_name:
        parts.append(f"Final destination: {dest_name}.")
    if turns_left is not None:
        parts.append(f"Estimated travel remaining: about {turns_left} turn(s) until arrival at {dest_name}.")
    to_desc = to_node.get("description") or to_node.get("label_description")
    if to_desc:
        parts.append(f"Ahead lies {to_name} ({to_node.get('type', 'location')}) — {to_desc[:300]}")
    region_name = from_node.get("region") or state.get("player_location_region")
    if region_name:
        regions = world_data.get("regions", {}).get("regions", [])
        current_region = next((r for r in regions if r.get("name") == region_name), None)
        parts.append(f"Region: {region_name}")
        if current_region:
            parts.append(f"Terrain: {current_region.get('terrain', 'N/A')[:400]}")
            parts.append(f"Climate: {current_region.get('climate', 'N/A')[:200]}")
    parts.append(f"The player has NOT yet arrived at {dest_name}. Narrate the journey itself — the road, terrain, weather, fellow travelers, or incidents along the way. Do not narrate arrival at {dest_name} this turn; travel completes on its own.")
    parts.append("</current_location>")
    return "\n".join(parts)


def _build_location_context(state: dict, world_data: dict) -> str:
    travel = _get_travel(state)
    if travel:
        travel_context = _build_travel_context(travel, state, world_data)
        if travel_context:
            return travel_context
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
        node_desc = current_node.get("description", "") or current_node.get("label_description", "")
        if node_name and node_desc:
            parts.append(f"Location: {node_name} ({node_type}) — {node_desc[:600]}")
        elif node_name:
            parts.append(f"Location: {node_name} ({node_type})")
        else:
            # Not yet generated (lazy world detail) — give the storyteller an
            # honest basis to improvise from the region/terrain context below.
            parts.append(
                f"Location: an unexplored {node_type} — this place has no established "
                "name or details yet. Improvise fitting local color from the region "
                "and terrain context; keep any specifics provisional.")
        if current_node.get("interlayer_connection_id"):
            map_connections = world_data.get("map_connections", [])
            for lc in map_connections:
                if lc.get("id") == current_node.get("interlayer_connection_id"):
                    target_layer = lc.get("to_layer_id") if lc.get("from_layer_id") == layer_id else lc.get("from_layer_id")
                    parts.append(f"Inter-layer connection: {lc.get('connection_type', 'passage')} to layer '{target_layer}' — {lc.get('description', '')[:200]}")
                    break
        site = (world_data.get("site_maps") or {}).get(node_id)
        if site:
            subs = site.get("sub_locations", [])
            site_position = _get_site_position(state)
            current_sub = None
            if site_position and site_position.get("parent_node_id") == node_id:
                current_sub = next(
                    (s for s in subs if s.get("id") == site_position.get("sub_location_id")), None)
            parts.append("<location_interior>")
            if current_sub:
                parts.append(
                    f"The player is currently at: {current_sub.get('name', '')} "
                    f"({current_sub.get('type', 'place')}) — {current_sub.get('description', '')[:300]}")
                adjacent_ids = set(current_sub.get("adjacent", []))
                adjacent_names = [s.get("name", "") for s in subs
                                  if s.get("id") in adjacent_ids and s.get("name")]
                if adjacent_names:
                    parts.append(f"Directly adjoining: {', '.join(adjacent_names)}")
            if site.get("layout_summary"):
                parts.append(f"Layout: {site['layout_summary']}")
            if subs:
                parts.append("Places within this location:")
                for sub in subs[:12]:
                    line = f"  - {sub.get('name', '')} ({sub.get('type', 'place')})"
                    if sub.get("description"):
                        line += f": {sub['description'][:200]}"
                    parts.append(line)
            parts.append("</location_interior>")
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


def _build_location_mutation_schema(world_data: dict, state: dict = None) -> dict:
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
    # Lazy worlds leave minor waypoints unnamed until visited; offer the
    # revealed ones as explicit "unexplored" destinations so the player can
    # still head toward them (they get detailed on approach).
    if state is not None and len(location_options) < 30:
        revealed = set(state.get("revealed_node_ids", []))
        for n in nodes:
            if len(location_options) >= 30:
                break
            if not n.get("name") and n.get("id") in revealed:
                location_options.append(f"{n['id']} (unexplored {n.get('type', 'waypoint')})")
    if not location_options:
        location_options = ["any"]
    region_names = [r.get("name", "") for r in regions if r.get("name")]
    layers_list = world_data.get("layers", [])
    layer_options = [f"{l.get('layer_id', '')} ({l.get('name', '')})" for l in layers_list if l.get("layer_id")]
    if not layer_options:
        layer_options = ["surface"]
    schema = {
        "player_location_changed": {"type": "boolean", "label": "Did the player move toward or arrive at a new location?"},
        "player_location_node_id": {
            "type": "select",
            "label": "Destination node ID",
            "options": location_options[:30],
            "description": "The node_id of the location the player moved to or set out toward. Distant destinations are fine — the journey plays out over multiple turns. Set only if player_location_changed is true."
        },
        "travel_interrupted": {
            "type": "boolean",
            "label": "Did the player pause an ongoing journey this turn (camping, resting, fighting, exploring a stop)?"
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

    # Intra-site movement: when the player's current location has an expanded
    # interior, offer its sub-locations as instant moves within the place.
    if state is not None:
        site = (world_data.get("site_maps") or {}).get(state.get("player_location_node_id"))
        if site and site.get("sub_locations"):
            sub_options = [
                f"{sub['id']} ({sub.get('name', '')})"
                for sub in site["sub_locations"][:16] if sub.get("id")
            ]
            sub_options.append("leave_site (step back out to the location as a whole)")
            schema["player_sub_location"] = {
                "type": "select",
                "label": f"Where inside {site.get('name', 'this location')} is the player now?",
                "options": sub_options,
                "description": "The specific place within the current location the player moved to. Moving between these is instant (no travel). Set only when the player moves within the location; use leave_site when they step back out.",
            }
    return schema


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


# ---------------------------------------------------------------------------
# Gradual travel. Instead of teleporting on a Reader move, the player walks the
# edge graph over multiple turns: a `travel` record in module_data tracks the
# route (node id path) and the distance covered on the current leg. Pace comes
# from the `world.travel_turns_per_edge` setting (0 = classic instant moves).
# ---------------------------------------------------------------------------


def _all_map_nodes(world_data: dict) -> list[dict]:
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        nodes = []
        for layer in map_layers:
            nodes.extend(layer.get("map", {}).get("nodes", []))
        return nodes
    return world_data.get("map", {}).get("nodes", [])


def _all_map_edges(world_data: dict) -> list[dict]:
    map_layers = world_data.get("map_layers", [])
    if map_layers:
        edges = []
        for layer in map_layers:
            edges.extend(layer.get("map", {}).get("edges", []))
        return edges
    return world_data.get("map", {}).get("edges", [])


def _weighted_adjacency(world_data: dict) -> dict:
    """{node_id: [(neighbor_id, distance), ...]} across all layers.

    Edges never cross layers, so a route search naturally stays on the
    player's layer. Missing distances fall back to node-coordinate length.
    """
    coords = {n.get("id"): (n.get("x", 0.0), n.get("y", 0.0)) for n in _all_map_nodes(world_data)}
    adj: dict[str, list[tuple[str, float]]] = {}
    for e in _all_map_edges(world_data):
        fr, to = e.get("from"), e.get("to")
        if not fr or not to:
            continue
        dist = e.get("distance")
        if not dist:
            (x1, y1), (x2, y2) = coords.get(fr, (0, 0)), coords.get(to, (0, 0))
            dist = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 or 1.0
        adj.setdefault(fr, []).append((to, float(dist)))
        adj.setdefault(to, []).append((fr, float(dist)))
    return adj


def _find_route(adjacency: dict, start: str, goal: str) -> list | None:
    """Shortest node-id path from start to goal (Dijkstra), or None."""
    import heapq
    if start not in adjacency or goal not in adjacency:
        return None
    dist = {start: 0.0}
    prev: dict[str, str] = {}
    pq = [(0.0, start)]
    visited = set()
    while pq:
        d, nid = heapq.heappop(pq)
        if nid in visited:
            continue
        visited.add(nid)
        if nid == goal:
            break
        for nb, w in adjacency.get(nid, []):
            nd = d + w
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = nid
                heapq.heappush(pq, (nd, nb))
    if goal not in visited:
        return None
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def _edge_length(adjacency: dict, a: str, b: str) -> float:
    for nb, w in adjacency.get(a, []):
        if nb == b:
            return w
    return 1.0


def _travel_speed(world_data: dict) -> float | None:
    """Map-units covered per turn, or None when travel is instant."""
    turns_per_edge = 2
    try:
        if _services is not None and _services.get("settings") is not None:
            turns_per_edge = int(_services["settings"].get("world.travel_turns_per_edge"))
    except Exception:
        turns_per_edge = 2
    if turns_per_edge <= 0:
        return None
    edges = _all_map_edges(world_data)
    distances = [e.get("distance") for e in edges if e.get("distance")]
    if not distances:
        return None
    avg = sum(distances) / len(distances)
    return avg / turns_per_edge


def _clean_option(value):
    """Mutation selects offer 'node_id (Name)' options; keep only the id."""
    if isinstance(value, str) and " (" in value:
        return value.split(" (", 1)[0].strip()
    return value


def _get_travel(state: dict):
    return (state.get("module_data", {}).get("wb_worldgen") or {}).get("travel")


def _get_site_position(state: dict):
    return (state.get("module_data", {}).get("wb_worldgen") or {}).get("site_position")


def _resolve_sub_location_move(mutation: dict, state: dict, world_data: dict) -> dict:
    """Interpret a Reader-declared move within the current location's interior.

    Returns {} (no change), {"site_position": None} (stepped back out) or
    {"site_position": {parent_node_id, sub_location_id}}. Sub-moves are
    instant and never interact with travel, fog or the node graph."""
    raw = _clean_option(mutation.get("player_sub_location"))
    if not raw:
        return {}
    if raw == "leave_site":
        return {"site_position": None} if _get_site_position(state) else {}
    current_node = state.get("player_location_node_id")
    site = (world_data.get("site_maps") or {}).get(current_node)
    if not site:
        return {}
    if not any(sub.get("id") == raw for sub in site.get("sub_locations", [])):
        return {}
    existing = _get_site_position(state)
    if existing and existing.get("sub_location_id") == raw:
        return {}
    return {"site_position": {"parent_node_id": current_node, "sub_location_id": raw}}


def _remaining_travel(travel: dict, adjacency: dict) -> float:
    """Total map-distance left from the player's position to the destination."""
    route = travel.get("route", [])
    leg_index = travel.get("leg_index", 0)
    remaining = travel.get("leg_distance", 0.0) - travel.get("leg_progress", 0.0)
    for i in range(leg_index + 1, len(route) - 1):
        remaining += _edge_length(adjacency, route[i], route[i + 1])
    return max(remaining, 0.0)


# ---------------------------------------------------------------------------
# Silent background backfill. Worlds created in "major_locations" mode leave
# ordinary waypoints unnamed/undescribed; during play they are detailed on
# demand — nodes the story approaches first (fog reveal, travel routes,
# arrival), then a low-priority idle trickle over the rest. All generation
# goes through one serialized worker so runs never overlap, and every result
# is synced into the live session, the save's world_data.json and the RAG
# world index. Node detail is generated at most once per world.
# ---------------------------------------------------------------------------

import asyncio as _asyncio
import logging as _logging

_logger = _logging.getLogger(__name__)

_backfill = {
    "task": None,       # the single running worker task, or None
    "queue": [],        # ordered node ids waiting for detail
    "queued": set(),    # membership mirror of queue + in-flight chunk
    "failed": set(),    # ids that exhausted retries this session
    "futures": {},      # node_id -> Future resolved when its detail is synced
    "disabled": False,  # set when the world template dir is gone
}


def _backfill_reset():
    """Forget session-scoped backfill state (tests / world switch)."""
    _backfill["task"] = None
    _backfill["queue"] = []
    _backfill["queued"] = set()
    _backfill["failed"] = set()
    _backfill["futures"] = {}
    _backfill["disabled"] = False
    _site_tasks.clear()


def _backfill_available(state: dict) -> bool:
    if _backfill["disabled"] or world_builder is None or _services is None:
        return False
    if not state.get("world_id"):
        return False
    llm = getattr(_services.get("engine"), "llm", None)
    return llm is not None and getattr(llm, "mode", "mock") != "mock"


def _node_needs_detail(node: dict) -> bool:
    return not node.get("name") or not node.get("description")


def _backfill_per_turn() -> int:
    try:
        if _services is not None and _services.get("settings") is not None:
            return max(0, int(_services["settings"].get("world.backfill_per_turn")))
    except Exception:
        pass
    return 2


def _queue_backfill(state: dict, node_ids: list, front: bool = False):
    """Queue nodes for background detailing and make sure the worker runs."""
    if not _backfill_available(state):
        return
    fresh = [nid for nid in node_ids
             if nid and nid not in _backfill["queued"] and nid not in _backfill["failed"]]
    if fresh:
        if front:
            _backfill["queue"][:0] = fresh
        else:
            _backfill["queue"].extend(fresh)
        _backfill["queued"].update(fresh)
    if _backfill["queue"] and (_backfill["task"] is None or _backfill["task"].done()):
        _backfill["task"] = _asyncio.create_task(_backfill_worker(state.get("world_id")))


async def _backfill_worker(world_id: str):
    """Drain the queue in small chunks: enrich via the world builder (writes
    the world template files), then sync results into the live session."""
    try:
        while _backfill["queue"]:
            chunk = _backfill["queue"][:4]
            del _backfill["queue"][:len(chunk)]
            try:
                summary = await world_builder.detail_nodes(world_id, chunk)
                _backfill["failed"].update(summary.get("failed_node_ids", []))
                _sync_enriched_nodes(world_id, chunk)
                await _embed_backfilled_nodes(world_id, chunk)
            except FileNotFoundError:
                # The world template dir is gone — nothing to generate from.
                _backfill["disabled"] = True
                _logger.warning("world '%s' missing; background detail disabled", world_id)
                return
            except Exception:
                _backfill["failed"].update(chunk)
                _logger.exception("background detail failed for nodes %s", chunk)
            finally:
                for nid in chunk:
                    _backfill["queued"].discard(nid)
                    fut = _backfill["futures"].pop(nid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(True)
    finally:
        _backfill["task"] = None


def _write_session_world_data(sm):
    """Rewrite the active save's World/world_data.json from the live session's
    world_data so a reload sees play-time generated content."""
    wd = sm.state.get("world_data")
    save_id = sm.state.get("active_save_id")
    if not wd or not save_id:
        return
    try:
        import json as _json
        world_dir = sm.data_dir / "saves" / save_id / "World"
        if world_dir.is_dir():
            with open(world_dir / "world_data.json", "w", encoding="utf-8") as f:
                _json.dump(wd, f, indent=2)
    except Exception:
        _logger.exception("failed to persist world_data for save %s", save_id)


def _sync_enriched_nodes(world_id: str, node_ids: list):
    """Merge freshly generated node fields into the live session's world_data
    and rewrite the save's World/world_data.json so a reload sees them."""
    sm = _services.get("session_manager") if _services else None
    if sm is None or sm.state.get("world_id") != world_id:
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    by_id = {n.get("id"): n for n in _all_map_nodes(wd)}
    changed = False
    for nid in node_ids:
        target = by_id.get(nid)
        if target is None:
            continue
        enriched = world_builder.get_map_node(world_id, nid)
        if not enriched:
            continue
        for field in ("name", "label_description", "description", "type", "importance"):
            value = enriched.get(field)
            if value and value != target.get(field):
                target[field] = value
                changed = True
    if changed:
        _write_session_world_data(sm)


def _node_world_entry(wd: dict, node: dict) -> dict | None:
    """RAG world-index entry for a backfilled node, matching the format
    memory._build_world_entries uses for map nodes."""
    if not node.get("name") or not node.get("description"):
        return None
    nid = node.get("id", "")
    for map_layer in wd.get("map_layers", []):
        if any(n.get("id") == nid for n in map_layer.get("map", {}).get("nodes", [])):
            layer_name = map_layer.get("name", "")
            return {
                "text": f"Location [{layer_name}]: {node['name']} ({node.get('type', 'location')}). {node['description']}",
                "source_type": "node", "source_id": nid, "region": layer_name,
            }
    return {
        "text": f"Location: {node['name']} ({node.get('type', 'location')}). {node['description']}",
        "source_type": "node", "source_id": nid, "region": node.get("name", ""),
    }


async def _embed_backfilled_nodes(world_id: str, node_ids: list):
    """Add freshly detailed nodes to the save's RAG world index."""
    sm = _services.get("session_manager") if _services else None
    engine = _services.get("engine") if _services else None
    if sm is None or engine is None or sm.state.get("world_id") != world_id:
        return
    memory = getattr(engine, "memory", None)
    if memory is None or not memory.has_world_index():
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    by_id = {n.get("id"): n for n in _all_map_nodes(wd)}
    entries = []
    for nid in node_ids:
        node = by_id.get(nid)
        if node is None:
            continue
        entry = _node_world_entry(wd, node)
        if entry:
            entries.append(entry)
    if not entries:
        return
    try:
        await memory.embed_world_entries(entries, engine.llm)
    except Exception:
        _logger.exception("failed to embed backfilled world entries")


# ---------------------------------------------------------------------------
# Lazy site expansion: major locations (cities, ports, strongholds) get their
# interior detail — layout + districts/venues — generated by ONE full-attention
# LLM call the first time the story approaches them, prefetched during travel
# so multi-turn journeys hide the latency entirely. Cached per world.
# ---------------------------------------------------------------------------

_site_tasks: dict = {}


def _site_mode() -> str:
    try:
        if _services is not None and _services.get("settings") is not None:
            mode = _services["settings"].get("world.site_expansion_mode")
            if mode:
                return str(mode)
    except Exception:
        pass
    return "prefetch"


def _maybe_expand_site(state: dict, node_id: str):
    """Fire-and-forget interior expansion for a major location (automatic
    modes only; the manual route calls the facade directly)."""
    if _site_mode() not in ("prefetch", "on_arrival") or not node_id:
        return
    if not _backfill_available(state):
        return
    wd = state.get("world_data")
    if not wd or node_id in (wd.get("site_maps") or {}):
        return
    node = next((n for n in _all_map_nodes(wd) if n.get("id") == node_id), None)
    if node is None or not world_builder.is_site_expandable(node):
        return
    existing = _site_tasks.get(node_id)
    if existing is not None and not existing.done():
        return
    _site_tasks[node_id] = _asyncio.create_task(
        _expand_site_task(state.get("world_id"), node_id))


async def _expand_site_task(world_id: str, node_id: str):
    try:
        site = await world_builder.expand_site(world_id, node_id)
        _sync_site(world_id, node_id, site)
        await _embed_site(world_id, site)
    except FileNotFoundError:
        _backfill["disabled"] = True
        _logger.warning("world '%s' missing; site expansion disabled", world_id)
    except Exception:
        _logger.exception("site expansion failed for node %s", node_id)
    finally:
        _site_tasks.pop(node_id, None)


def _sync_site(world_id: str, node_id: str, site: dict):
    """Merge a freshly expanded site into the live session and its save."""
    sm = _services.get("session_manager") if _services else None
    if sm is None or sm.state.get("world_id") != world_id:
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    wd.setdefault("site_maps", {})[node_id] = site
    _write_session_world_data(sm)


async def _embed_site(world_id: str, site: dict):
    """Add a freshly expanded site's entries to the save's RAG world index."""
    from wbworldgen.worldgen.enrichment import site_world_entries
    sm = _services.get("session_manager") if _services else None
    engine = _services.get("engine") if _services else None
    if sm is None or engine is None or sm.state.get("world_id") != world_id:
        return
    memory = getattr(engine, "memory", None)
    if memory is None or not memory.has_world_index():
        return
    entries = site_world_entries(site.get("parent_node_id", ""), site)
    if not entries:
        return
    try:
        await memory.embed_world_entries(entries, engine.llm)
    except Exception:
        _logger.exception("failed to embed site entries")


async def _ensure_current_node_detailed(state: dict):
    """Await-on-arrival: if the player stands on an undetailed node, wait
    (bounded) for its detail so the storyteller never narrates a fresh scene
    from thin air. Everything else stays non-blocking; on timeout the
    generation keeps running in the background and lands next turn."""
    world_data = state.get("world_data")
    node_id = state.get("player_location_node_id")
    if not world_data or not node_id or not _backfill_available(state):
        return
    if _get_travel(state):
        return  # en route — the journey narration doesn't need the destination yet
    node = next((n for n in _all_map_nodes(world_data) if n.get("id") == node_id), None)
    if node is None or not _node_needs_detail(node) or node_id in _backfill["failed"]:
        return
    fut = _backfill["futures"].get(node_id)
    if fut is None:
        fut = _asyncio.get_running_loop().create_future()
        _backfill["futures"][node_id] = fut
    _queue_backfill(state, [node_id], front=True)
    try:
        await _asyncio.wait_for(_asyncio.shield(fut), timeout=20)
    except _asyncio.TimeoutError:
        _logger.warning("timed out waiting for detail of node %s; continuing with sparse context", node_id)
    except Exception:
        pass


def _kick_background_detail(state: dict):
    """Fire-and-forget per-turn triggers: prefetch along an active travel
    route, then top up the idle trickle with the most important pending nodes."""
    world_data = state.get("world_data")
    if not world_data or not _backfill_available(state):
        return
    all_nodes = _all_map_nodes(world_data)
    by_id = {n.get("id"): n for n in all_nodes}

    # Travel prefetch: destination and remaining waypoints, highest priority —
    # multi-turn journeys hide the generation latency entirely.
    travel = _get_travel(state)
    if travel:
        route = travel.get("route", [])
        ahead = route[travel.get("leg_index", 0) + 1:]
        dest = travel.get("destination_node_id")
        wanted = ([dest] if dest else []) + list(ahead)
        needs = [nid for nid in wanted
                 if nid in by_id and _node_needs_detail(by_id[nid])]
        if needs:
            _queue_backfill(state, needs, front=True)
        if _site_mode() == "prefetch" and dest:
            # Start the destination's interior while the journey plays out.
            _maybe_expand_site(state, dest)

    # Idle trickle: keep quietly finishing the world, visited areas first.
    per_turn = _backfill_per_turn()
    if per_turn <= 0 or _backfill["queue"]:
        return
    pending = [n for n in all_nodes
               if _node_needs_detail(n) and n.get("id") not in _backfill["failed"]
               and n.get("id") not in _backfill["queued"]]
    if not pending:
        return
    revealed = set(state.get("revealed_node_ids", []))
    pending.sort(key=lambda n: (-(n.get("id") in revealed), -n.get("importance", 0)))
    _queue_backfill(state, [n["id"] for n in pending[:per_turn]])


async def on_gather_context(state: dict, sdk) -> dict:
    """Per-turn world context: the player's current location block."""
    world_data = state.get("world_data")
    if not world_data:
        return {}
    await _ensure_current_node_detailed(state)
    _kick_background_detail(state)
    if not _get_travel(state):
        # Arrived (or standing) somewhere expandable whose interior is still
        # missing (prefetch missed / on_arrival mode) — start it now.
        _maybe_expand_site(state, state.get("player_location_node_id"))
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
    return _build_location_mutation_schema(world_data, state)


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict:
    """Apply player movement.

    With travel enabled (world.travel_turns_per_edge > 0) a Reader-declared
    destination starts a journey along the edge graph instead of teleporting:
    the route is stored in module_data and progress advances every turn,
    revealing fog and updating the player node as each waypoint is reached.
    Instant mode (setting 0), layer changes, and off-graph destinations keep
    the classic teleport behavior.
    """
    world_data = state.get("world_data")
    if not world_data:
        return {}
    mutation = mutation or {}
    travel = _get_travel(state)
    current_node = state.get("player_location_node_id")
    speed = _travel_speed(world_data)

    new_node_id = _clean_option(mutation.get("player_location_node_id"))
    new_region = _clean_option(mutation.get("player_location_region"))
    new_layer_id = _clean_option(mutation.get("player_location_layer_id"))
    interrupted = bool(mutation.get("travel_interrupted"))

    # Intra-site movement (instant, inside the current node's interior).
    # Any real node move clears the position — the player walked out.
    site_position_update = _resolve_sub_location_move(mutation, state, world_data)

    revealed = list(set(state.get("revealed_node_ids", [])))
    revealed_dirty = False
    newly_revealed: list[str] = []

    def reveal_around(nid):
        nonlocal revealed_dirty
        adjacency = _build_graph_adjacency(world_data)
        for x in _reveal_bfs(nid, adjacency, radius=1):
            if x not in revealed:
                revealed.append(x)
                newly_revealed.append(x)
                revealed_dirty = True

    def queue_revealed_backfill():
        # Newly revealed places get detailed silently in the background so
        # they have names/descriptions by the time the story reaches them.
        if not newly_revealed:
            return
        by_id = {n.get("id"): n for n in _all_map_nodes(world_data)}
        needs = [nid for nid in newly_revealed
                 if nid in by_id and _node_needs_detail(by_id[nid])]
        if needs:
            _queue_backfill(state, needs, front=True)

    def teleport(node_id):
        reveal_around(node_id)
        queue_revealed_backfill()
        _maybe_expand_site(state, node_id)
        return {
            "player_location_node_id": node_id,
            "player_location_region": new_region or state.get("player_location_region"),
            "player_location_layer_id": new_layer_id or state.get("player_location_layer_id"),
            "revealed_node_ids": revealed,
            "module_data": {"wb_worldgen": {"travel": None, "site_position": None}},
        }

    # --- A Reader-declared destination -----------------------------------
    wants_move = new_node_id and new_node_id != current_node
    if wants_move:
        layer_changed = bool(new_layer_id) and new_layer_id != (state.get("player_location_layer_id") or new_layer_id)
        if speed is None or layer_changed:
            # Instant mode, or an inter-layer transition (portals, stairs,
            # cave mouths) — those are narrative jumps, not overland travel.
            return teleport(new_node_id)
        if not travel or travel.get("destination_node_id") != new_node_id:
            # (Re)route from the last node the player actually reached; any
            # partial progress on the current leg is abandoned.
            adjacency = _weighted_adjacency(world_data)
            route = _find_route(adjacency, current_node, new_node_id) if current_node else None
            if not route or len(route) < 2:
                # Unknown or unreachable destination — fall back to teleport
                # rather than trap the player.
                return teleport(new_node_id)
            travel = {
                "route": route,
                "leg_index": 0,
                "leg_progress": 0.0,
                "leg_distance": _edge_length(adjacency, route[0], route[1]),
                "destination_node_id": new_node_id,
                "destination_region": new_region,
            }
            interrupted = False  # setting out counts as traveling this turn
            site_position_update = {"site_position": None}  # walked out of the interior
            if _site_mode() == "prefetch":
                # Start the destination's interior now — the journey's turns
                # hide the generation latency.
                _maybe_expand_site(state, new_node_id)

    if travel and speed is None:
        # Travel was switched off mid-journey; the player simply stays at the
        # last reached node and the journey record is dropped.
        return {"module_data": {"wb_worldgen": {"travel": None, **site_position_update}}}

    if not travel:
        if site_position_update:
            return {"module_data": {"wb_worldgen": dict(site_position_update)}}
        return {}

    # --- Advance the journey ----------------------------------------------
    location_update = {}
    if not interrupted:
        adjacency = _weighted_adjacency(world_data)
        route = travel["route"]
        budget = speed
        while budget > 0:
            need = travel["leg_distance"] - travel["leg_progress"]
            if budget < need:
                travel["leg_progress"] += budget
                break
            budget -= need
            travel["leg_index"] += 1
            reached_id = travel["route"][travel["leg_index"]]
            reveal_around(reached_id)
            reached_node = next((n for n in _all_map_nodes(world_data) if n.get("id") == reached_id), {})
            location_update = {
                "player_location_node_id": reached_id,
                "player_location_region": reached_node.get("region") or state.get("player_location_region"),
                "player_location_layer_id": state.get("player_location_layer_id"),
            }
            if travel["leg_index"] >= len(route) - 1:
                # Arrived at the final destination.
                if travel.get("destination_region"):
                    location_update["player_location_region"] = travel["destination_region"]
                travel = None
                break
            travel["leg_progress"] = 0.0
            travel["leg_distance"] = _edge_length(adjacency, route[travel["leg_index"]], route[travel["leg_index"] + 1])

    result = {"module_data": {"wb_worldgen": {"travel": travel, **site_position_update}}}
    if location_update:
        # The player physically moved to another node — they are no longer
        # inside the previous location's interior.
        result["module_data"]["wb_worldgen"]["site_position"] = None
        result.update(location_update)
        result["revealed_node_ids"] = revealed
    elif revealed_dirty:
        result["revealed_node_ids"] = revealed
    queue_revealed_backfill()
    return result


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
