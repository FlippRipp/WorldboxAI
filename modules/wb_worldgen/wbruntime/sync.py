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
        for field in ("name", "label_description", "description", "type", "importance"):
            value = enriched.get(field)
            if value and value != target.get(field):
                target[field] = value
                changed = True
    if changed:
        write_session_world_data(sm)


def node_world_entry(wd: dict, node: dict) -> dict | None:
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


def sync_site(host, world_id: str, node_id: str, site: dict):
    """Merge a freshly expanded site into the live session and its save."""
    sm = host._services.get("session_manager") if host._services else None
    if sm is None or sm.state.get("world_id") != world_id:
        return
    wd = sm.state.get("world_data")
    if not wd:
        return
    wd.setdefault("site_maps", {})[node_id] = site
    write_session_world_data(sm)


async def embed_site(host, world_id: str, site: dict):
    """Add a freshly expanded site's entries to the save's RAG world index."""
    from wbworldgen.worldgen.enrichment import site_world_entries
    sm = host._services.get("session_manager") if host._services else None
    engine = host._services.get("engine") if host._services else None
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
