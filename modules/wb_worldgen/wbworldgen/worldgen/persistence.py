"""Disk persistence for worlds + the node-enrichment write cache.

Disk layout is identical to the legacy implementation
(``data/worlds/<id>/step_*.json`` + ``metadata.json``) so existing worlds load
unchanged. Operates on plain ``world_state`` dicts.
"""

import json
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: Per-world checkpoint store (v2c): byte-exact world snapshots the agent
#: harness takes before mutating tool actions. Lives inside the world
#: directory but is never itself part of a snapshot.
CHECKPOINTS_DIRNAME = "_checkpoints"

#: Top-level entries excluded from snapshot AND left untouched by restore:
#: the checkpoint store itself and the agent build's observability artifact
#: — the action log is history, not world content, and must never rewind.
CHECKPOINT_EXCLUDE = (CHECKPOINTS_DIRNAME, "agent_build.json")


def safe_world_id(world_id: str) -> str:
    """Normalize a world id into a filesystem-safe slug, matching the rule used
    when persisting worlds. Falls back to a random hex id when empty."""
    safe = (world_id or "").lower().replace(" ", "_")
    safe = "".join(c for c in safe if c.isalnum() or c in "_-")
    return safe or uuid.uuid4().hex[:8]


def resolve_world_id(world_state: dict) -> str:
    """Derive the stable world id for an in-flight generation session.

    Prefers an already-assigned draft id; otherwise derives one from the lore
    world name (mirroring ``_save_world_state``) so terrain artifacts written
    mid-generation land in the same directory the world is later saved to.
    """
    draft = world_state.get("_draft_id")
    if draft:
        return safe_world_id(draft)
    lore = world_state.get("steps", {}).get("lore", {}).get("data", {})
    if isinstance(lore, dict) and lore.get("world_name"):
        return safe_world_id(lore["world_name"])
    return uuid.uuid4().hex[:8]


class WorldPersistence:
    def __init__(self, worlds_dir: str = "data/worlds", prompt_library_path: str = "data/prompt_library.json"):
        self._dir = Path(worlds_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._enrichment_cache: dict[str, dict] = {}
        self._enrichment_cache_max: int = 4
        #: node_id -> map_id over a world's child-map bundles, so play-time
        #: enrichment of procedurally generated child maps persists into the
        #: right maps/*.json file. Invalidated when a bundle is (re)saved.
        self._child_node_index: dict[str, dict] = {}
        self._prompt_library_path = Path(prompt_library_path)
        self._enrichment_prompts: dict[str, str] = {}
        self.load_enrichment_prompts()

    # --- world CRUD ---------------------------------------------------------

    def world_dir(self, world_id: str) -> Path:
        """The world's on-disk directory. Public for module-internal
        consumers that persist per-world artifacts beside the step files
        (the agent harness writes ``agent_build.json`` here)."""
        return self._dir / safe_world_id(world_id)

    def list_worlds(self) -> list[dict]:
        worlds = []
        for world_dir in self._dir.iterdir():
            if not world_dir.is_dir():
                continue
            meta_path = world_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            step_count = len(list(world_dir.glob("step_*.json")))
            in_progress = meta.get("in_progress", False)
            worlds.append({
                "id": world_dir.name,
                "name": meta.get("name", world_dir.name),
                "seed_prompt": meta.get("seed_prompt", "")[:200],
                "created_at": meta.get("created_at", ""),
                "step_count": step_count,
                "in_progress": in_progress,
                "current_step": meta.get("current_step") if in_progress else None,
                # Scenario linked at creation; story creation pairs them back up.
                "scenario_id": meta.get("scenario_id"),
            })
        worlds.sort(key=lambda w: w.get("created_at", ""), reverse=True)
        return worlds

    def save_world(self, world_id: str, world_state: dict) -> str:
        return self._save_world_state(world_id, world_state, in_progress=False)

    def save_draft(self, world_id: str, world_state: dict) -> str:
        return self._save_world_state(world_id, world_state, in_progress=True)

    def _save_world_state(self, world_id: str, world_state: dict, in_progress: bool = False) -> str:
        if not world_id or not world_id.strip():
            lore = world_state.get("steps", {}).get("lore", {}).get("data", {})
            if isinstance(lore, dict) and lore.get("world_name") and in_progress:
                world_id = lore["world_name"]
            else:
                world_id = uuid.uuid4().hex[:8]

        safe_id = safe_world_id(world_id)

        world_dir = self._dir / safe_id
        world_dir.mkdir(parents=True, exist_ok=True)

        steps = world_state.get("steps", {})
        for step_id, step_data in steps.items():
            with open(world_dir / f"step_{step_id}.json", "w", encoding="utf-8") as f:
                json.dump(step_data, f, indent=2, default=str)
        if "map_generation" in steps:
            self.invalidate_enrichment_cache(safe_id)

        lore_data = steps.get("lore", {}).get("data", {})
        world_name = lore_data.get("world_name", safe_id) if isinstance(lore_data, dict) else safe_id

        metadata = {
            "name": world_name,
            "seed_prompt": world_state.get("seed_prompt", ""),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        if world_state.get("scenario"):
            metadata["scenario"] = world_state["scenario"]
        if world_state.get("scenario_id"):
            metadata["scenario_id"] = world_state["scenario_id"]
        if isinstance(world_state.get("brief"), dict):
            # The ideation brief (C4): prompt + co-authored world rules — the
            # agent's standing instructions, part of the world's record.
            metadata["brief"] = world_state["brief"]
        if world_state.get("world_connections"):
            # Surgery-authored root/parallel-map connections (v2a): native
            # ConnectionRecords the compiler folds in post-migrate.
            metadata["world_connections"] = world_state["world_connections"]
        if world_state.get("template_id"):
            metadata["template_id"] = world_state["template_id"]
            # Snapshot the vocabulary at creation time so a later template
            # edit never silently changes an existing world.
            if isinstance(world_state.get("template_vocab"), dict):
                metadata["template_vocab"] = world_state["template_vocab"]
        if world_state.get("skip_review"):
            # One-shot generations must resume as one-shot: /api/world/continue
            # keys off this after a backend restart mid-run.
            metadata["skip_review"] = True
        if in_progress:
            metadata["in_progress"] = True
            metadata["current_step"] = world_state.get("current_step")
            if world_state.get("complete"):
                # Generation finished but the player hasn't hit "Save World"
                # yet — restore straight to the finished review, not mid-run.
                metadata["draft_complete"] = True

        with open(world_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, default=str)

        return safe_id

    def load_world(self, world_id: str) -> dict:
        world_dir = self._dir / world_id
        if not world_dir.is_dir():
            raise FileNotFoundError(f"World '{world_id}' not found.")

        meta_path = world_dir / "metadata.json"
        metadata = {}
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)

        steps = {}
        for step_file in sorted(world_dir.glob("step_*.json")):
            step_id = step_file.stem.replace("step_", "")
            with open(step_file, "r", encoding="utf-8") as f:
                steps[step_id] = json.load(f)

        world_state = {
            "seed_prompt": metadata.get("seed_prompt", ""),
            "steps": steps,
            "complete": (not metadata.get("in_progress", False)
                         or bool(metadata.get("draft_complete"))),
            "current_step": metadata.get("current_step") if metadata.get("in_progress") else None,
        }
        if metadata.get("skip_review"):
            world_state["skip_review"] = True
        if metadata.get("scenario"):
            world_state["scenario"] = metadata["scenario"]
        if metadata.get("scenario_id"):
            world_state["scenario_id"] = metadata["scenario_id"]
        if isinstance(metadata.get("brief"), dict):
            world_state["brief"] = metadata["brief"]
        if metadata.get("world_connections"):
            world_state["world_connections"] = metadata["world_connections"]
        if metadata.get("template_id"):
            world_state["template_id"] = metadata["template_id"]
            if isinstance(metadata.get("template_vocab"), dict):
                world_state["template_vocab"] = metadata["template_vocab"]
        sites = self.load_sites(world_id)
        if sites:
            world_state["sites"] = sites
        child_maps = self.load_child_maps(world_id)
        if child_maps:
            world_state["child_maps"] = child_maps
        return world_state

    def save_step(self, world_id: str, step_id: str, step_data: dict):
        world_dir = self._dir / world_id
        if not world_dir.is_dir():
            raise FileNotFoundError(f"World '{world_id}' not found.")
        with open(world_dir / f"step_{step_id}.json", "w", encoding="utf-8") as f:
            json.dump(step_data, f, indent=2, default=str)
        if step_id == "map_generation":
            self.invalidate_enrichment_cache(world_id)

    def delete_world(self, world_id: str):
        world_dir = self._dir / world_id
        if not world_dir.is_dir():
            raise FileNotFoundError(f"World '{world_id}' not found.")
        shutil.rmtree(world_dir)
        self.invalidate_enrichment_cache(world_id)

    # --- site bundles (lazy interior detail) --------------------------------

    def child_map_path(self, world_id: str, map_id: str) -> Path:
        return self._dir / safe_world_id(world_id) / "maps" / f"{safe_world_id(map_id)}.json"

    def save_child_map(self, world_id: str, bundle: dict):
        """Cache of an expanded child map ({"map", "connections"}). Content is
        written once; enrichment later fills node names/descriptions in place
        (``save_node_enrichment``)."""
        map_id = (bundle.get("map") or {}).get("map_id", "")
        path = self.child_map_path(world_id, map_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)
        self._child_node_index.pop(safe_world_id(world_id), None)

    def load_child_map(self, world_id: str, map_id: str) -> dict | None:
        path = self.child_map_path(world_id, map_id)
        if not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                bundle = json.load(f)
            return bundle if isinstance(bundle, dict) and bundle.get("map") else None
        except Exception:
            return None

    def load_child_maps(self, world_id: str) -> list[dict]:
        """All persisted child-map bundles for a world."""
        maps_dir = self._dir / safe_world_id(world_id) / "maps"
        if not maps_dir.is_dir():
            return []
        bundles = []
        for path in sorted(maps_dir.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    bundle = json.load(f)
                if isinstance(bundle, dict) and bundle.get("map"):
                    bundles.append(bundle)
            except Exception:
                continue
        return bundles

    def site_path(self, world_id: str, node_id: str) -> Path:
        return self._dir / safe_world_id(world_id) / "sites" / f"{safe_world_id(node_id)}.json"

    def save_site(self, world_id: str, node_id: str, site: dict):
        """Write-once cache of a node's interior detail — future saves of the
        same world inherit it, so a site is generated at most once per world."""
        path = self.site_path(world_id, node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(site, f, indent=2, default=str)

    def load_site(self, world_id: str, node_id: str) -> dict | None:
        path = self.site_path(world_id, node_id)
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def load_sites(self, world_id: str) -> dict:
        """All persisted sites for a world, keyed by parent node id."""
        sites_dir = self._dir / world_id / "sites"
        if not sites_dir.is_dir():
            return {}
        sites = {}
        for path in sorted(sites_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    site = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            parent_id = site.get("parent_node_id") or path.stem
            sites[parent_id] = site
        return sites

    def terrain_dir(self, world_id: str, layer_id: str = "") -> Path:
        """Directory holding a world's terrain rasters/images. When ``layer_id``
        is given, the per-layer subdirectory is returned. Created on access."""
        d = self._dir / safe_world_id(world_id) / "terrain"
        if layer_id:
            d = d / safe_world_id(layer_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- enrichment write cache --------------------------------------------

    def save_node_enrichment(self, world_id: str, node_id: str, field: str, value: str):
        step_data = self._step_data_cached(world_id)
        if step_data is not None:
            node_index = step_data.get("_node_index")
            if node_index is None:
                node_index = self.build_enrichment_node_index(step_data.get("data", {}))
                step_data["_node_index"] = node_index

            entry = node_index.get(node_id)
            if entry:
                entry[field] = value
                return

        # Not a root/parallel-map node: the node may live in a lazily
        # expanded child map (procedural children are born unnamed and get
        # enriched during play) — update its bundle in place.
        self._save_child_node_enrichment(world_id, node_id, field, value)

    def _save_child_node_enrichment(self, world_id: str, node_id: str, field: str, value: str):
        safe_id = safe_world_id(world_id)
        index = self._child_node_index.get(safe_id)
        if index is None:
            index = {}
            for bundle in self.load_child_maps(world_id):
                mid = bundle["map"].get("map_id", "")
                for n in bundle["map"].get("nodes", []):
                    if n.get("id"):
                        index[n["id"]] = mid
            self._child_node_index[safe_id] = index
        map_id = index.get(node_id)
        if not map_id:
            return
        bundle = self.load_child_map(world_id, map_id)
        if bundle is None:
            return
        for n in bundle["map"].get("nodes", []):
            if n.get("id") == node_id:
                n[field] = value
                break
        else:
            return
        path = self.child_map_path(world_id, map_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)

    def append_map_node(self, world_id: str, map_id: str, node: dict, edges: list) -> bool:
        """Append a play-time-founded node (and its link edges) to the map that
        owns ``map_id``: a lazily-expanded child map's bundle when one exists,
        else the matching root/parallel map inside the ``map_generation`` step
        data. The node also joins the region of its edge partner. Writes
        through to disk; returns False when no such map is stored."""
        bundle = self.load_child_map(world_id, map_id)
        if bundle is not None:
            record = bundle["map"]
            nodes = record.setdefault("nodes", [])
            if not any(n.get("id") == node.get("id") for n in nodes):
                nodes.append(node)
                record.setdefault("edges", []).extend(edges)
                self._append_to_partner_region(record.get("regions"), node, edges)
                self.save_child_map(world_id, bundle)
            return True
        return self._append_root_map_node(world_id, map_id, node, edges)

    def _append_root_map_node(self, world_id: str, map_id: str, node: dict, edges: list) -> bool:
        # Go through the enrichment write cache so a pending cached state and
        # this append never clobber each other.
        step_data = self._step_data_cached(world_id)
        if step_data is None:
            return False
        target = self._step_map_for_id(step_data.get("data", {}), map_id)
        if target is None:
            return False
        nodes = target.setdefault("nodes", [])
        if not any(n.get("id") == node.get("id") for n in nodes):
            nodes.append(node)
            target.setdefault("edges", []).extend(edges)
            self._append_to_partner_region(target.get("regions"), node, edges)
            index = step_data.get("_node_index")
            if index is not None:
                index[node["id"]] = node
        self.write_enrichment_to_disk(world_id)
        return True

    @staticmethod
    def _step_map_for_id(map_data: dict, map_id: str) -> dict | None:
        """The step-data map dict a compiled map_id refers to, mirroring the
        migrate rule: layer 0 is 'root', later layers keep their layer_id."""
        if not isinstance(map_data, dict):
            return None
        if "layers" in map_data:
            for i, layer in enumerate(map_data.get("layers") or []):
                lid = layer.get("layer_id") or ("root" if i == 0 else f"layer_{i}")
                mid = "root" if i == 0 else lid
                if map_id in (mid, lid):
                    return layer.setdefault("map", {})
            return None
        if "nodes" in map_data and map_id in ("root", ""):
            return map_data
        return None

    @staticmethod
    def _append_to_partner_region(regions, node: dict, edges: list):
        """Add the new node to the region membership of its edge partner, so
        region lookups see it where its anchor lives."""
        if not isinstance(regions, list):
            return
        partners = {e.get("from") for e in edges} | {e.get("to") for e in edges}
        partners.discard(node.get("id"))
        for region in regions:
            ids = region.get("node_ids")
            if isinstance(ids, list) and partners & set(ids):
                if node.get("id") not in ids:
                    ids.append(node["id"])
                return

    # --- structural surgery write paths (v2a) ------------------------------
    #
    # The removal/rewiring mirror of ``append_map_node``: same dual dispatch
    # (child-map bundle vs the map_generation step data through the
    # enrichment write cache), same write-through discipline. Validation is
    # the caller's job (worldgen/surgery.py) — these do the mechanical
    # mutation only.

    def _step_data_cached(self, world_id: str) -> dict | None:
        """The map_generation step data through the enrichment write cache
        (loading and caching on miss), or None when the world has none."""
        step_path = self._dir / world_id / "step_map_generation.json"
        if not step_path.exists():
            return None
        step_data = self._enrichment_cache.get(world_id)
        if step_data is None:
            if len(self._enrichment_cache) >= self._enrichment_cache_max:
                oldest = next(iter(self._enrichment_cache))
                self.write_enrichment_to_disk(oldest, evict=True)
            with open(step_path, "r", encoding="utf-8") as f:
                step_data = json.load(f)
            self._enrichment_cache[world_id] = step_data
        return step_data

    def _step_maps(self, step_data: dict):
        """Yield (map_id, map dict) for every map in the step data, using the
        same id rule as ``_step_map_for_id``."""
        map_data = step_data.get("data", {})
        if not isinstance(map_data, dict):
            return
        if "layers" in map_data:
            for i, layer in enumerate(map_data.get("layers") or []):
                lid = layer.get("layer_id") or ("root" if i == 0 else f"layer_{i}")
                yield ("root" if i == 0 else lid), layer.setdefault("map", {})
        elif "nodes" in map_data:
            yield "root", map_data

    @staticmethod
    def _remove_node_from_record(record: dict, node_id: str) -> dict | None:
        """Drop one node from a map record, cascading its edges and region
        membership (a region centered on it gets its first remaining member,
        or an empty center). Returns {"node", "edges_removed"} or None."""
        nodes = record.get("nodes") or []
        node = next((n for n in nodes if n.get("id") == node_id), None)
        if node is None:
            return None
        nodes.remove(node)
        edges = record.get("edges") or []
        dropped = [e for e in edges if node_id in (e.get("from"), e.get("to"))]
        for e in dropped:
            edges.remove(e)
        for region in record.get("regions") or []:
            ids = region.get("node_ids")
            if isinstance(ids, list) and node_id in ids:
                ids.remove(node_id)
            if region.get("center_node_id") == node_id:
                region["center_node_id"] = (ids[0] if isinstance(ids, list) and ids else "")
        return {"node": node, "edges_removed": len(dropped)}

    def remove_map_node(self, world_id: str, node_id: str) -> dict | None:
        """Remove a node (and its edges/region membership) from whichever map
        owns it — child bundle or step data. Returns {"map_id", "node",
        "edges_removed"} or None when no stored map carries the node."""
        step_data = self._step_data_cached(world_id)
        if step_data is not None:
            for map_id, record in self._step_maps(step_data):
                removed = self._remove_node_from_record(record, node_id)
                if removed is not None:
                    index = step_data.get("_node_index")
                    if index is not None:
                        index.pop(node_id, None)
                    self.write_enrichment_to_disk(world_id)
                    return {"map_id": map_id, **removed}
        safe_id = safe_world_id(world_id)
        for bundle in self.load_child_maps(world_id):
            record = bundle.get("map") or {}
            removed = self._remove_node_from_record(record, node_id)
            if removed is not None:
                self.save_child_map(world_id, bundle)
                self._child_node_index.pop(safe_id, None)
                return {"map_id": record.get("map_id", ""), **removed}
        return None

    def _mutate_map_record(self, world_id: str, map_id: str, mutate):
        """Apply ``mutate(record)`` to the persisted map record owning
        ``map_id`` and write it back through that home's path. Returns
        mutate's result; None when no such map is stored. A falsy result
        means nothing changed and skips the write."""
        bundle = self.load_child_map(world_id, map_id)
        if bundle is not None:
            result = mutate(bundle["map"])
            if result:
                self.save_child_map(world_id, bundle)
            return result
        step_data = self._step_data_cached(world_id)
        if step_data is None:
            return None
        target = self._step_map_for_id(step_data.get("data", {}), map_id)
        if target is None:
            return None
        result = mutate(target)
        if result:
            self.write_enrichment_to_disk(world_id)
        return result

    def add_map_edge(self, world_id: str, map_id: str, from_id: str, to_id: str) -> dict | None:
        """Append an intra-map edge with the map's distance convention.
        Idempotent (an existing edge in either direction is returned as-is);
        None when no such map is stored."""
        def _add(record: dict):
            for e in record.get("edges") or []:
                if {e.get("from"), e.get("to")} == {from_id, to_id}:
                    return e
            by_id = {n.get("id"): n for n in record.get("nodes") or []}
            a, b = by_id.get(from_id), by_id.get(to_id)
            dist = 1.0
            if a is not None and b is not None:
                dist = ((a.get("x", 0.0) - b.get("x", 0.0)) ** 2
                        + (a.get("y", 0.0) - b.get("y", 0.0)) ** 2) ** 0.5
            edge = {"from": from_id, "to": to_id,
                    "distance": round(max(dist, 1.0), 2)}
            record.setdefault("edges", []).append(edge)
            return edge

        return self._mutate_map_record(world_id, map_id, _add)

    def remove_map_edge(self, world_id: str, map_id: str, from_id: str, to_id: str):
        """Drop every edge joining the pair (either direction). Returns the
        number removed (0 = no such edge); None when no such map is stored."""
        def _remove(record: dict):
            edges = record.get("edges") or []
            dropped = [e for e in edges
                       if {e.get("from"), e.get("to")} == {from_id, to_id}]
            for e in dropped:
                edges.remove(e)
            return len(dropped)

        result = self._mutate_map_record(world_id, map_id, _remove)
        return result

    def _read_metadata(self, world_id: str) -> dict:
        meta_path = self._dir / safe_world_id(world_id) / "metadata.json"
        if not meta_path.exists():
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_metadata(self, world_id: str, metadata: dict):
        meta_path = self._dir / safe_world_id(world_id) / "metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, default=str)

    def add_world_connection(self, world_id: str, connection: dict,
                             owner_map_id: str = None) -> str | None:
        """Persist a native-v2 ConnectionRecord. ``owner_map_id`` names the
        child-map bundle that owns it; None stores it under the world's
        ``world_connections`` metadata key (root/parallel-map connections —
        the compiler folds them in post-migrate). Returns the home
        ("child:<map_id>" | "world") or None when the owner bundle is
        missing."""
        if owner_map_id:
            bundle = self.load_child_map(world_id, owner_map_id)
            if bundle is None:
                return None
            bundle.setdefault("connections", []).append(connection)
            self.save_child_map(world_id, bundle)
            return f"child:{owner_map_id}"
        metadata = self._read_metadata(world_id)
        metadata.setdefault("world_connections", []).append(connection)
        self._write_metadata(world_id, metadata)
        return "world"

    def remove_world_connection(self, world_id: str, connection_id: str) -> str | None:
        """Remove one persisted connection by id, wherever it lives: the
        ``world_connections`` metadata key, a child-map bundle, or the legacy
        layer-connection list inside the map_generation step data (fresh
        worlds' plane crossings — their nodes' ``interlayer_connection_id``
        stamps are cleared too). Returns the home it was removed from, or
        None when no persisted record carries the id (e.g. a migrated
        connection with a synthesized id)."""
        metadata = self._read_metadata(world_id)
        world_conns = metadata.get("world_connections") or []
        kept = [c for c in world_conns if c.get("id") != connection_id]
        if len(kept) != len(world_conns):
            metadata["world_connections"] = kept
            self._write_metadata(world_id, metadata)
            return "world"

        for bundle in self.load_child_maps(world_id):
            conns = bundle.get("connections") or []
            kept = [c for c in conns if c.get("id") != connection_id]
            if len(kept) != len(conns):
                bundle["connections"] = kept
                self.save_child_map(world_id, bundle)
                return f"child:{bundle.get('map', {}).get('map_id', '')}"

        step_data = self._step_data_cached(world_id)
        if step_data is not None:
            map_data = step_data.get("data", {})
            conns = map_data.get("connections") if isinstance(map_data, dict) else None
            if isinstance(conns, list):
                kept = [c for c in conns if c.get("id") != connection_id]
                if len(kept) != len(conns):
                    map_data["connections"] = kept
                    for _mid, record in self._step_maps(step_data):
                        for n in record.get("nodes") or []:
                            if n.get("interlayer_connection_id") == connection_id:
                                n["interlayer_connection_id"] = ""
                    self.write_enrichment_to_disk(world_id)
                    return "step"
        return None

    # --- world checkpoints (v2c: the agent's revert safety net) -------------
    #
    # A checkpoint is a byte-exact copy of the world's persisted content
    # (metadata, step files, child-map bundles, sites, terrain rasters).
    # The harness snapshots before every mutating agent action; the revert
    # tool restores. Restore rewinds world CONTENT only — the current brief
    # (the user's contract: rules and notes, amendments, verifier context
    # and veto locks included) is carried forward, because the agreement is
    # not the agent's work product to roll back.

    def checkpoints_dir(self, world_id: str) -> Path:
        return self.world_dir(world_id) / CHECKPOINTS_DIRNAME

    def snapshot_world(self, world_id: str, tag: str) -> str:
        """Copy the world's persisted content into checkpoint ``tag``
        (replacing a same-tag leftover). Flushes the enrichment write cache
        first so pending node writes are part of the snapshot. Returns the
        normalized tag; unknown worlds raise FileNotFoundError."""
        world_dir = self.world_dir(world_id)
        if not world_dir.is_dir():
            raise FileNotFoundError(f"World '{world_id}' not found.")
        safe_tag = safe_world_id(str(tag))
        self.write_enrichment_to_disk(world_id)
        dest = self.checkpoints_dir(world_id) / safe_tag
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(world_dir, dest,
                        ignore=shutil.ignore_patterns(*CHECKPOINT_EXCLUDE))
        return safe_tag

    def list_checkpoints(self, world_id: str) -> list:
        """Existing checkpoint tags, numerically ordered for the digit tags
        the harness uses (action-log indices)."""
        d = self.checkpoints_dir(world_id)
        if not d.is_dir():
            return []
        return sorted((p.name for p in d.iterdir() if p.is_dir()),
                      key=lambda t: (len(t), t))

    def restore_world(self, world_id: str, tag: str):
        """Replace the world's persisted content with checkpoint ``tag``'s.

        The world rewinds; the agreement does not: the current metadata
        ``brief`` survives the restore. Cache coherence: the write-cache
        entry is invalidated BEFORE files are replaced — a later flush must
        never resurrect post-checkpoint state over the restored files (the
        exact hazard ``invalidate_enrichment_cache`` exists for) — and the
        child-node index is dropped. Compiled-cache invalidation is the
        caller's job (that cache is facade-owned)."""
        world_dir = self.world_dir(world_id)
        snap = self.checkpoints_dir(world_id) / safe_world_id(str(tag))
        if not snap.is_dir():
            raise FileNotFoundError(
                f"World '{world_id}' has no checkpoint '{tag}'.")
        current_brief = self._read_metadata(world_id).get("brief")

        self.invalidate_enrichment_cache(world_id)
        self._child_node_index.pop(safe_world_id(world_id), None)

        for entry in world_dir.iterdir():
            if entry.name in CHECKPOINT_EXCLUDE:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for entry in snap.iterdir():
            target = world_dir / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)

        if isinstance(current_brief, dict):
            metadata = self._read_metadata(world_id)
            metadata["brief"] = current_brief
            self._write_metadata(world_id, metadata)

    def clear_checkpoints(self, world_id: str):
        """Drop the world's whole checkpoint store. The revert window is
        build-scoped: a new build clears leftovers up front (stale tags
        would collide with its fresh action indices) and every terminal
        build state clears them behind itself."""
        shutil.rmtree(self.checkpoints_dir(world_id), ignore_errors=True)

    # --- enrichment write cache (flush/invalidate) --------------------------

    def flush_enrichment_cache(self, world_id: str = None):
        if world_id:
            self.write_enrichment_to_disk(world_id)
        else:
            for wid in list(self._enrichment_cache.keys()):
                self.write_enrichment_to_disk(wid)

    def write_enrichment_to_disk(self, world_id: str, evict: bool = False):
        """Write-through: persist the cached enrichment state but keep the entry
        cached so the next save doesn't re-read the whole map step from disk.
        ``evict=True`` drops the entry afterwards (LRU making room)."""
        step_data = self._enrichment_cache.get(world_id)
        if step_data is None:
            return
        step_path = self._dir / world_id / "step_map_generation.json"
        to_dump = {k: v for k, v in step_data.items() if k != "_node_index"}
        with open(step_path, "w", encoding="utf-8") as f:
            json.dump(to_dump, f, indent=2, default=str)
        if evict:
            self._enrichment_cache.pop(world_id, None)

    def invalidate_enrichment_cache(self, world_id: str):
        """Drop the cached copy after an external write to the world's step
        files so a later flush can't resurrect stale map data over it."""
        self._enrichment_cache.pop(world_id, None)
        self._enrichment_cache.pop(safe_world_id(world_id), None)

    @staticmethod
    def build_enrichment_node_index(map_data: dict) -> dict:
        index = {}
        if "layers" in map_data:
            for layer in map_data["layers"]:
                for node in layer.get("map", {}).get("nodes", []):
                    index[node["id"]] = node
        elif "nodes" in map_data:
            for node in map_data["nodes"]:
                index[node["id"]] = node
        return index

    @staticmethod
    def sync_enrichment_to_map_state(map_data: dict, node_map: dict):
        """Copy enrichment fields (name, label_description, description) from
        enriched nodes into the in-memory map state. Mutates map_data in place."""
        if not isinstance(map_data, dict):
            return
        fields = ("name", "label_description", "description")
        if "layers" in map_data:
            for layer in map_data.get("layers", []):
                for node in layer.get("map", {}).get("nodes", []):
                    enriched = node_map.get(node.get("id"))
                    if enriched:
                        for field in fields:
                            if enriched.get(field):
                                node[field] = enriched[field]
        elif "nodes" in map_data:
            for node in map_data.get("nodes", []):
                enriched = node_map.get(node.get("id"))
                if enriched:
                    for field in fields:
                        if enriched.get(field):
                            node[field] = enriched[field]

    # --- prompt templates ---------------------------------------------------

    def load_enrichment_prompts(self):
        try:
            if not self._prompt_library_path.exists():
                return
            with open(self._prompt_library_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            for entry in entries:
                eid = entry.get("id", "")
                content = entry.get("content", "")
                if eid and content:
                    self._enrichment_prompts[eid] = content
        except Exception:
            pass

    def get_prompt(self, prompt_id: str, fallback: str, **kwargs) -> str:
        template = self._enrichment_prompts.get(prompt_id)
        if template:
            try:
                return template.format(**kwargs)
            except Exception:
                pass
        return fallback
