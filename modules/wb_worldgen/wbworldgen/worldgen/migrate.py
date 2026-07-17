"""world_format 2 migration.

``migrate_world_data`` converts any compiled world dict — flat ``map``,
layered ``map_layers`` + ``map_connections``, or already-v2 — into the
hierarchical shape: ``maps`` ({map_id: MapRecord}) + ``connections`` (flat
ConnectionRecord list) + ``hierarchy`` + ``root_map_id``. Idempotent; node
and edge lists are carried over by reference (no copies) and node ids are
preserved verbatim so saves, fog (revealed_node_ids) and RAG source_ids
survive unchanged.

Legacy layers become: the lowest-index layer -> the root map; every other
layer -> a parallel sibling map (parent_map_id = root, anchor_node_id =
None). Inter-layer map_connections become ordinary connections with
``origin: "migrated"`` and instant travel. Each migrated map keeps its old
layer id as ``legacy_layer_id`` (terrain image URLs key on it).

``site_maps`` (one-level interior bundles) are left untouched here — they
are converted to real interior child maps by the site migration in the
expansion engine (phase 2 of the hierarchy work).
"""

from .mapspace import ROOT_MAP_ID

WORLD_FORMAT = 2

DEFAULT_LEVELS = [
    {"level_type": "world", "label": "World", "generator_id": "world_map",
     "guidance": "The top-level overworld map."},
    {"level_type": "interior", "label": "Interior", "generator_id": "interior",
     "nestable": True,
     "guidance": "Rooms, halls and courts of one building, complex or vessel."},
]


def _default_hierarchy() -> dict:
    return {"levels": [dict(l) for l in DEFAULT_LEVELS], "notes": ""}


def _map_record_from(map_dict: dict, *, map_id: str, label: str, level_type: str,
                     description: str = "", parent_map_id=None, anchor_node_id=None,
                     legacy_layer_id: str = "", rules=None) -> dict:
    record = {
        "map_id": map_id,
        "label": label,
        "level_type": level_type,
        "description": description or "",
        "parent_map_id": parent_map_id,
        "anchor_node_id": anchor_node_id,
        "generator_id": "world_map",
        "nodes": map_dict.get("nodes", []),
        "edges": map_dict.get("edges", []),
        "config": map_dict.get("config", {}),
        "schema": 2,
    }
    # Optional geometry extras — carried by reference when present.
    for key in ("regions", "roads"):
        if map_dict.get(key):
            record[key] = map_dict[key]
    if legacy_layer_id:
        record["legacy_layer_id"] = legacy_layer_id
    if rules:
        record["rules"] = rules
    return record


def _migrate_layers(wd: dict):
    """map_layers + map_connections -> maps + connections."""
    map_layers = wd.get("map_layers", [])
    layer_rules = {lr.get("layer_id"): lr.get("rules", [])
                   for lr in wd.get("layer_rules", []) if isinstance(lr, dict)}

    ordered = sorted(
        (l for l in map_layers if isinstance(l, dict)),
        key=lambda l: l.get("index", 0),
    )
    maps: dict[str, dict] = {}
    id_alias: dict[str, str] = {}
    for i, layer in enumerate(ordered):
        lid = layer.get("layer_id") or (ROOT_MAP_ID if i == 0 else f"layer_{i}")
        map_id = ROOT_MAP_ID if i == 0 else lid
        id_alias[lid] = map_id
        maps[map_id] = _map_record_from(
            layer.get("map", {}),
            map_id=map_id,
            label=layer.get("name", lid),
            level_type=layer.get("layer_type", "world") or "world",
            description=layer.get("description", ""),
            parent_map_id=None if i == 0 else ROOT_MAP_ID,
            anchor_node_id=None,
            legacy_layer_id=lid,
            rules=layer_rules.get(lid),
        )

    connections = []
    for lc in wd.get("map_connections", []):
        if not isinstance(lc, dict):
            continue
        from_map = id_alias.get(lc.get("from_layer_id"), lc.get("from_layer_id"))
        to_map = id_alias.get(lc.get("to_layer_id"), lc.get("to_layer_id"))
        if from_map not in maps or to_map not in maps:
            continue
        connections.append({
            "id": lc.get("id") or f"c_{len(connections):04d}",
            "from": {"map_id": from_map, "node_id": lc.get("from_node_id")},
            "to": {"map_id": to_map, "node_id": lc.get("to_node_id")},
            "kind": lc.get("connection_type", "passage") or "passage",
            "name": lc.get("name", ""),
            "description": lc.get("description", ""),
            "travel": {"mode": "instant"},
            "bidirectional": bool(lc.get("bidirectional", True)),
            "requirements": "",
            "hidden": False,
            "origin": "migrated",
        })
    return maps, connections


def migrate_world_data(wd: dict) -> dict:
    """Migrate a world dict to world_format 2, in place. Idempotent."""
    if not isinstance(wd, dict):
        return wd
    if wd.get("world_format", 0) >= WORLD_FORMAT and isinstance(wd.get("maps"), dict):
        return wd

    if wd.get("map_layers"):
        maps, connections = _migrate_layers(wd)
    elif isinstance(wd.get("map"), dict) and wd["map"].get("nodes") is not None:
        maps = {ROOT_MAP_ID: _map_record_from(
            wd["map"],
            map_id=ROOT_MAP_ID,
            label=(wd.get("lore") or {}).get("world_name") or "World",
            level_type="world",
            legacy_layer_id=wd["map"].get("layer_id") or "main",
        )}
        connections = []
    elif isinstance(wd.get("maps"), dict):
        # Native maps without the format stamp (partial construction).
        maps, connections = wd["maps"], wd.get("connections") or []
    else:
        # No map content at all (e.g. world without a generated map yet):
        # stamp the format but add nothing.
        wd["world_format"] = WORLD_FORMAT
        wd.setdefault("hierarchy", _default_hierarchy())
        return wd

    wd["maps"] = maps
    wd["connections"] = wd.get("connections") or connections
    wd["root_map_id"] = ROOT_MAP_ID
    wd["world_format"] = WORLD_FORMAT
    wd.setdefault("hierarchy", _default_hierarchy())

    # The legacy views are replaced, not kept: layers are gone as a concept.
    for legacy_key in ("map", "map_layers", "map_connections", "layers", "layer_rules"):
        wd.pop(legacy_key, None)
    return wd


def migrate_session_state(state: dict) -> bool:
    """Migrate play-session keys after world_data is v2. Returns True if
    anything changed.

    ``player_location_layer_id`` -> ``player_location_map_id`` (old layer ids
    equal migrated map ids, except the primary layer which became the root).
    The player's map is re-derived from their node when possible — the node
    is the source of truth.
    """
    wd = state.get("world_data")
    if not isinstance(wd, dict) or not isinstance(wd.get("maps"), dict):
        return False
    changed = False
    if "player_location_map_id" not in state:
        from .mapspace import map_of_node
        map_id = None
        node_id = state.get("player_location_node_id")
        if node_id:
            map_id = map_of_node(wd, node_id)
        if map_id is None:
            old_layer = state.get("player_location_layer_id")
            if old_layer and old_layer in wd["maps"]:
                map_id = old_layer
            else:
                map_id = wd.get("root_map_id", ROOT_MAP_ID)
        state["player_location_map_id"] = map_id
        changed = True
    if "player_location_layer_id" in state:
        state.pop("player_location_layer_id", None)
        changed = True
    return changed
