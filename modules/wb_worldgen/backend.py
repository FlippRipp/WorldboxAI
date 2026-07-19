"""wb_worldgen module backend (thin adapter).

Owns the WorldBuilder instance and the world/terrain API routes. Wires itself
into the engine through ``set_services`` (called by the core server after the
engine is constructed):

  * injects llm + settings into its WorldBuilder and registers the pipeline steps
  * registers a character-generation context provider (world flavour)
  * registers a "world" story-source so create_save can produce ``world_data``
  * hands the engine/session services to the route module via ``configure``

The world code itself lives under ``wbworldgen`` (relocated from
``backend.engine``); the play-time turn logic (travel, context, mutation
schema, background backfill, site expansion, save/RAG sync) lives under
``wbruntime``. This file only holds the module-scoped state (world_builder,
services, backfill queues) and thin delegating wrappers, so tests can load it
under private names and monkeypatch ``world_builder``/``_services`` per
instance — wbruntime reads them back through the ``_HOST`` view at call time.
We put the module directory on sys.path so both packages import as
self-contained, independent of the core shim.
"""

import os
import sys

_MOD_DIR = os.path.abspath(os.path.dirname(__file__))
if _MOD_DIR not in sys.path:
    sys.path.insert(0, _MOD_DIR)

from wbworldgen.worldgen import WorldBuilder, register_default_steps  # noqa: E402

import routes as _routes  # noqa: E402  (module-local sibling, importable via _MOD_DIR on sys.path)
import terrain_routes as _terrain_routes  # noqa: E402  (experimental terrain lab API)

from wbruntime import backfill as _rt_backfill  # noqa: E402
from wbruntime import context as _rt_context  # noqa: E402
from wbruntime import expansion as _rt_expansion  # noqa: E402
from wbruntime import intent as _rt_intent  # noqa: E402
from wbruntime import known_locations as _rt_known  # noqa: E402
from wbruntime import routing as _rt_routing  # noqa: E402
from wbruntime import schema as _rt_schema  # noqa: E402
from wbruntime import sync as _rt_sync  # noqa: E402
from wbruntime import travel as _rt_travel  # noqa: E402
from wbruntime import worldspace as _rt_worldspace  # noqa: E402


# Single WorldBuilder instance owned by this module; built in set_services once
# the engine's llm/settings are available.
world_builder: WorldBuilder | None = None
_services: dict | None = None

# Background-work state (queues, in-flight tasks); owned here so each loaded
# backend instance is isolated, operated on by wbruntime through _HOST.
_backfill = {
    "task": None,       # the single running worker task, or None
    "queue": [],        # ordered node ids waiting for detail
    "queued": set(),    # membership mirror of queue + in-flight chunk
    "failed": set(),    # ids that exhausted retries this session
    "futures": {},      # node_id -> Future resolved when its detail is synced
    "disabled": False,  # set when the world template dir is gone
}
_site_tasks: dict = {}


class _HostView:
    """Live view of this module's globals for wbruntime functions.

    Attribute reads resolve against globals() at call time, so tests that
    assign e.g. ``backend._services = {...}`` are always seen."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


_HOST = _HostView()


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


async def create_world_story_source(*, save_id, source_id, start_preference, session_manager, engine, start_location_node_id=None, scenario=None, character_module_data=None, character_data=None) -> dict:
    """Story-source provider for create_save: turn a world_id into a playable save.

    Uses the start location the player already picked on the start screen
    (``start_location_node_id``) when given; otherwise picks one via LLM —
    from the typed preference, or, when the story combines the world with a
    scenario, from the scenario itself (its opening scene decides where the
    story starts; the player's modification request has highest priority) —
    or random. Seeds the fog-of-war reveal, persists ``World/world_data.json``,
    embeds the world into the save's RAG index, and writes the world keys into
    session state. Returns the chosen start_location (for the API response).
    """
    import json as _json
    import random as _random

    from wbworldgen.worldgen.facade import scenario_start_brief

    world_id = source_id
    world_state = world_builder.load_world(world_id)
    compiled = world_builder.compile_world(world_state)

    start_location = None
    if start_location_node_id:
        # An explicit pick may point anywhere in the map hierarchy (the
        # start-screen LLM pick can descend into interiors), so resolve it
        # against every map, not just the type-filtered candidate list.
        start_location = world_builder.find_start_location(world_id, start_location_node_id)
    if start_location is None:
        pick_request = (start_preference or "").strip() or (scenario_start_brief(scenario) if scenario else "")
        if pick_request:
            start_location = await world_builder.llm_pick_start_location(world_id, pick_request, engine.llm)
            if start_location and (start_location.get("generated")
                                   or start_location.get("world_modified")):
                # The pick authored a brand-new start location or expanded an
                # interior — recompile so the save's world_data carries it.
                world_state = world_builder.load_world(world_id)
                compiled = world_builder.compile_world(world_state)
    if start_location is None:
        locations = world_builder.get_start_locations(world_id)
        start_location = _random.choice(locations) if locations else None

    player_location_node_id = None
    player_location_region = None
    player_location_map_id = None
    revealed_node_ids: list[str] = []
    if start_location:
        player_location_node_id = start_location.get("node_id")
        player_location_region = start_location.get("region")
        player_location_map_id = start_location.get("map_id") or compiled.get("root_map_id", "root")
        # Only the start node itself is fully known; its neighbors show up on
        # the map as the faded name-only fringe, and the known-locations pass
        # fully reveals whatever the character genuinely knows about. A start
        # reached by descending into interiors also knows its container chain
        # (the building it is in, the city that is in...).
        revealed_node_ids = [player_location_node_id]
        for ancestor_id in start_location.get("ancestor_node_ids") or []:
            if ancestor_id and ancestor_id not in revealed_node_ids:
                revealed_node_ids.append(ancestor_id)

    state = session_manager.create_save(
        save_id,
        world_id=world_id,
        player_location_node_id=player_location_node_id,
        player_location_region=player_location_region,
        player_location_map_id=player_location_map_id,
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
    session_manager.state["player_location_map_id"] = player_location_map_id
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
                "world.travel_minutes_per_edge", "slider", 60,
                label="Travel Pace (Minutes per Map Leg)",
                category="World Building",
                description="How many in-world minutes it takes to cross one average map route between locations, used when no better estimate exists (the travel-intent call usually supplies one). Journeys to distant places take proportionally longer. 0 = instant travel (the player jumps straight to the destination).",
                is_global=True,
                min=0, max=480,
            )
            settings.register(
                "world.destination_resolution", "select", "semantic",
                label="Travel Destination Resolution",
                category="World Building",
                description="How the travel-intent call matches a spoken destination ('the school') to a map location. 'semantic' embeds the destination and searches the world index, then confirms with a small LLM call; 'roster' shows the LLM every known named location and lets it pick directly.",
                is_global=True,
                options=["semantic", "roster"],
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
# Turn-time contributions: location context + movement. Implemented in
# wbruntime; these wrappers keep the module-hook names the engine discovers
# and the underscore names tests exercise, threading _HOST for state access.
#   * on_gather_context   -> per-turn <current_location> context_string,
#                             plus the pre-storyteller travel-intent pass
#                             (may start a time-based journey)
#   * on_intro_context     -> richer world block for the opening scene
#   * on_intro_complete    -> one-shot reveal of locations the character knows
#   * on_command_recall    -> /recall: same reveal pass, on demand mid-story
#   * on_mutation_schema   -> dynamic movement schema offered to the Reader
#   * on_reader_context    -> guidance + location block for the module's
#                             dedicated reader call (manifest dedicated_reader)
#   * on_mutate_state      -> apply a move + fog-of-war reveal
# ---------------------------------------------------------------------------

# Pure helpers (no module state) — direct aliases.
_all_map_nodes = _rt_worldspace.all_map_nodes
_all_map_edges = _rt_worldspace.all_map_edges
_build_graph_adjacency = _rt_worldspace.build_graph_adjacency
_initial_adjacency = _rt_worldspace.build_graph_adjacency
_reveal_bfs = _rt_worldspace.reveal_bfs
_clean_option = _rt_worldspace.clean_option
_get_travel = _rt_worldspace.get_travel
_get_site_position = _rt_worldspace.get_site_position
_node_needs_detail = _rt_worldspace.node_needs_detail
_weighted_adjacency = _rt_travel.weighted_adjacency
_find_route = _rt_travel.find_route
_edge_length = _rt_travel.edge_length
_journey_progress = _rt_travel.journey_progress
_plan_itinerary = _rt_routing.plan_itinerary
_advance_position = _rt_routing.advance_position
_resolve_sub_location_move = _rt_travel.resolve_sub_location_move
_build_location_mutation_schema = _rt_schema.build_location_mutation_schema
_write_session_world_data = _rt_sync.write_session_world_data


# Host-bound wrappers (read world_builder/_services/_backfill/_site_tasks live).

def _travel_minutes_per_edge() -> int:
    return _rt_travel.travel_minutes_per_edge(_HOST)


def _plan_journey(state: dict, world_data: dict, destination_node_id: str,
                  eta_minutes: int = None, transport: str = ""):
    return _rt_travel.plan_journey(_HOST, state, world_data, destination_node_id,
                                   eta_minutes=eta_minutes, transport=transport)


def _backfill_reset():
    _rt_backfill.backfill_reset(_HOST)


def _backfill_available(state: dict) -> bool:
    return _rt_backfill.backfill_available(_HOST, state)


def _backfill_per_turn() -> int:
    return _rt_backfill.backfill_per_turn(_HOST)


def _queue_backfill(state: dict, node_ids: list, front: bool = False):
    _rt_backfill.queue_backfill(_HOST, state, node_ids, front=front)


async def _backfill_worker(world_id: str):
    await _rt_backfill.backfill_worker(_HOST, world_id)


def _sync_enriched_nodes(world_id: str, node_ids: list):
    _rt_sync.sync_enriched_nodes(_HOST, world_id, node_ids)


def _node_world_entry(wd: dict, node: dict) -> dict | None:
    return _rt_sync.node_world_entry(wd, node)


async def _embed_backfilled_nodes(world_id: str, node_ids: list):
    await _rt_sync.embed_backfilled_nodes(_HOST, world_id, node_ids)


def _site_mode() -> str:
    return _rt_expansion.site_mode(_HOST)


def _maybe_expand_node(state: dict, node_id: str, on_request: bool = False):
    _rt_expansion.maybe_expand_node(_HOST, state, node_id, on_request=on_request)


async def _expand_node_task(world_id: str, map_id: str, node_id: str):
    await _rt_expansion.expand_node_task(_HOST, world_id, map_id, node_id)


async def _ensure_child_map(state: dict, node_id: str):
    return await _rt_expansion.ensure_child_map(_HOST, state, node_id)


def _sync_child_map(world_id: str, bundle: dict):
    _rt_sync.sync_child_map(_HOST, world_id, bundle)


async def _embed_child_map(world_id: str, bundle: dict):
    await _rt_sync.embed_child_map(_HOST, world_id, bundle)


async def _ensure_current_node_detailed(state: dict):
    await _rt_backfill.ensure_current_node_detailed(_HOST, state)


def _kick_background_detail(state: dict):
    _rt_backfill.kick_background_detail(_HOST, state)


def _build_travel_context(travel: dict, state: dict, world_data: dict) -> str:
    return _rt_context.build_travel_context(_HOST, travel, state, world_data)


def _build_location_context(state: dict, world_data: dict) -> str:
    return _rt_context.build_location_context(_HOST, state, world_data)


async def on_gather_context(state: dict, sdk) -> dict:
    return await _rt_context.on_gather_context(_HOST, state, sdk)


async def on_intro_context(state: dict, sdk) -> dict:
    return await _rt_context.on_intro_context(_HOST, state, sdk)


async def on_intro_complete(state: dict, sdk) -> dict:
    return await _rt_known.reveal_known_locations(_HOST, state, sdk)


async def on_command_recall(args, state: dict, sdk) -> dict:
    """``/recall``: run the known-locations pass in an already-started story."""
    if not state.get("world_data"):
        return {"error": True, "message": "This story has no world map."}
    result = await _rt_known.reveal_known_locations(_HOST, state, sdk)
    new_ids = result.get("newly_known_node_ids") or []
    if not new_ids:
        return {"message": "No new places came to mind — everything your "
                           "character knows is already on the map."}
    by_id = _rt_worldspace.node_index(state["world_data"])
    names = [by_id[nid].get("name") for nid in new_ids if nid in by_id]
    names = [n for n in names if n]
    return {
        "message": f"Your character recalls {len(new_ids)} known place(s): "
                   + ", ".join(names),
        "revealed_node_ids": result["revealed_node_ids"],
    }


async def on_command_teleport(args, state: dict, sdk) -> dict:
    """``/teleport <node id or name>``: put the player at any map node
    instantly, clearing any active journey and interior position. A testing
    tool, not gameplay: no story turn, no fog fringe, no site expansion —
    only the target node itself is revealed."""
    world_data = state.get("world_data")
    if not world_data:
        return {"error": True, "message": "This story has no world map."}
    by_id = _rt_worldspace.node_index(world_data)
    current = state.get("player_location_node_id")
    query = " ".join(args).strip()
    if not query:
        here = (by_id.get(current) or {}).get("name") or "an unknown place"
        return {"message": f"You are at {here} ({current}). "
                           "Usage: /teleport <node id or name>"}

    if query in by_id:
        target = by_id[query]
    else:
        q = query.lower()
        named = [n for n in by_id.values() if q == (n.get("name") or "").lower()]
        matches = named or [n for n in by_id.values()
                            if q in (n.get("name") or "").lower()]
        if not matches:
            return {"error": True, "message": f"No map node matches '{query}'."}
        if len(matches) > 1:
            options = ", ".join(
                f"{n.get('name')} ({n.get('id')})" for n in matches[:5])
            return {"error": True, "message": f"'{query}' is ambiguous: {options}"}
        target = matches[0]

    node_id = target.get("id")
    name = target.get("name") or node_id
    if node_id == current:
        return {"message": f"Already at {name}."}
    map_id = _rt_worldspace.map_of_node(world_data, node_id) \
        or _rt_worldspace.player_map_id(state)
    revealed = list(state.get("revealed_node_ids") or [])
    if node_id not in revealed:
        revealed.append(node_id)
    here = (by_id.get(current) or {}).get("name") or current or "nowhere"
    return {
        "message": f"Teleported from {here} to {name} ({node_id}).",
        "player_location_node_id": node_id,
        "player_location_map_id": map_id,
        "player_location_region": target.get("region")
            or state.get("player_location_region"),
        "revealed_node_ids": revealed,
        "module_data": {"wb_worldgen": {"travel": None, "site_position": None}},
        "module_data_replace": ["travel", "site_position"],
    }


async def on_mutation_schema(state: dict, sdk) -> dict:
    return await _rt_schema.on_mutation_schema(_HOST, state, sdk)


async def on_reader_context(state: dict, sdk) -> str:
    return await _rt_context.on_reader_context(_HOST, state, sdk)


async def _evaluate_travel_intent(state: dict, sdk=None) -> dict:
    return await _rt_intent.evaluate_travel_intent(_HOST, state, sdk)


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict:
    return await _rt_travel.on_mutate_state(_HOST, mutation, state, sdk)


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
