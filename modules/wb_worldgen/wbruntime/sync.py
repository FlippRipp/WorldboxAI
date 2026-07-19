"""Three-way sync for play-time generated world content: merge into the live
session's world_data, rewrite the save's World/world_data.json, and add the
new entries to the save's RAG world index. One owner for all of it so backfill
and site expansion stay consistent."""

import json
import logging

from .worldspace import all_map_nodes

_logger = logging.getLogger(__name__)


def write_session_world_data(sm):
    """Rewrite the active save's World/world_data.json from the live session's
    world_data so a reload sees play-time generated content."""
    wd = sm.state.get("world_data")
    save_id = sm.state.get("active_save_id")
    if not wd or not save_id:
        return
    try:
        world_dir = sm.data_dir / "saves" / save_id / "World"
        if world_dir.is_dir():
            with open(world_dir / "world_data.json", "w", encoding="utf-8") as f:
                json.dump(wd, f, indent=2)
    except Exception:
        _logger.exception("failed to persist world_data for save %s", save_id)


NODE_SYNC_FIELDS = ("name", "label_description", "description",
                    "additional_details", "type", "importance")


def merge_node_fields(target: dict, source: dict) -> bool:
    """Copy world-level node fields onto a session node (the heal primitive
    for session copies that diverged from the world bundle). Returns True
    when anything changed."""
    changed = False
    for field in NODE_SYNC_FIELDS:
        value = source.get(field)
        if value and value != target.get(field):
            target[field] = value
            changed = True
    return changed


def sync_enriched_nodes(host, world_id: str, node_ids: list):
    """Merge freshly generated node fields into the live session's world_data
    and rewrite the save's World/world_data.json so a reload sees them."""
    sm = host._services.get("session_manager") if host._services else None
    if sm is None or sm.state.get("world_id") != world_id:
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    by_id = {n.get("id"): n for n in all_map_nodes(wd)}
    changed = False
    for nid in node_ids:
        target = by_id.get(nid)
        if target is None:
            continue
        enriched = host.world_builder.get_map_node(world_id, nid)
        if not enriched:
            continue
        if merge_node_fields(target, enriched):
            changed = True
    if changed:
        write_session_world_data(sm)


def node_world_entry(wd: dict, node: dict) -> dict | None:
    """RAG world-index entry for a backfilled node, matching the format
    memory._build_world_entries uses for map nodes (format lockstep: root-map
    nodes keep the flat legacy text, other maps are labeled with the map)."""
    if not node.get("name") or not node.get("description"):
        return None
    nid = node.get("id", "")
    from wbworldgen.worldgen import mapspace as _ms
    map_id = _ms.map_of_node(wd, nid)
    root_id = wd.get("root_map_id", "root")
    details = f" Storyteller notes: {node['additional_details']}" \
        if node.get("additional_details") else ""
    if map_id is not None and map_id != root_id:
        m = _ms.get_map(wd, map_id) or {}
        label = m.get("label", map_id)
        return {
            "text": f"Location [{label}]: {node['name']} ({node.get('type', 'location')}). {node['description']}{details}",
            "source_type": "node", "source_id": nid, "region": label,
        }
    return {
        "text": f"Location: {node['name']} ({node.get('type', 'location')}). {node['description']}{details}",
        "source_type": "node", "source_id": nid, "region": node.get("region") or node.get("name", ""),
    }


async def embed_backfilled_nodes(host, world_id: str, node_ids: list):
    """Add freshly detailed nodes to the save's RAG world index."""
    sm = host._services.get("session_manager") if host._services else None
    engine = host._services.get("engine") if host._services else None
    if sm is None or engine is None or sm.state.get("world_id") != world_id:
        return
    memory = getattr(engine, "memory", None)
    if memory is None or not memory.has_world_index():
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    by_id = {n.get("id"): n for n in all_map_nodes(wd)}
    entries = []
    for nid in node_ids:
        node = by_id.get(nid)
        if node is None:
            continue
        entry = node_world_entry(wd, node)
        if entry:
            entries.append(entry)
    if not entries:
        return
    try:
        await memory.embed_world_entries(entries, engine.llm)
    except Exception:
        _logger.exception("failed to embed backfilled world entries")


def sync_child_map(host, world_id: str, bundle: dict):
    """Merge a freshly expanded child map into the live session and its save."""
    sm = host._services.get("session_manager") if host._services else None
    if sm is None or sm.state.get("world_id") != world_id:
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    record = bundle.get("map") or {}
    if not record.get("map_id"):
        return
    wd.setdefault("maps", {})[record["map_id"]] = record
    existing_ids = {c.get("id") for c in wd.setdefault("connections", [])}
    wd["connections"].extend(
        c for c in bundle.get("connections", []) if c.get("id") not in existing_ids)
    write_session_world_data(sm)


async def embed_child_map(host, world_id: str, bundle: dict):
    """Add a freshly expanded child map's entries to the save's RAG index."""
    from wbworldgen.worldgen.expansion.maps_expand import map_world_entries
    sm = host._services.get("session_manager") if host._services else None
    engine = host._services.get("engine") if host._services else None
    if sm is None or engine is None or sm.state.get("world_id") != world_id:
        return
    memory = getattr(engine, "memory", None)
    if memory is None or not memory.has_world_index():
        return
    wd = sm.state.get("world_data") or {}
    entries = map_world_entries(bundle.get("map") or {}, bundle.get("connections"),
                                maps_by_id=wd.get("maps") or {})
    if not entries:
        return
    try:
        await memory.embed_world_entries(entries, engine.llm)
    except Exception:
        _logger.exception("failed to embed child map entries")
