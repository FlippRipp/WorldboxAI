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
        self._prompt_library_path = Path(prompt_library_path)
        self._enrichment_prompts: dict[str, str] = {}
        self.load_enrichment_prompts()

    # --- world CRUD ---------------------------------------------------------

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

        lore_data = steps.get("lore", {}).get("data", {})
        world_name = lore_data.get("world_name", safe_id) if isinstance(lore_data, dict) else safe_id

        metadata = {
            "name": world_name,
            "seed_prompt": world_state.get("seed_prompt", ""),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        if in_progress:
            metadata["in_progress"] = True
            metadata["current_step"] = world_state.get("current_step")

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

        return {
            "seed_prompt": metadata.get("seed_prompt", ""),
            "steps": steps,
            "complete": not metadata.get("in_progress", False),
            "current_step": metadata.get("current_step") if metadata.get("in_progress") else None,
        }

    def save_step(self, world_id: str, step_id: str, step_data: dict):
        world_dir = self._dir / world_id
        if not world_dir.is_dir():
            raise FileNotFoundError(f"World '{world_id}' not found.")
        with open(world_dir / f"step_{step_id}.json", "w", encoding="utf-8") as f:
            json.dump(step_data, f, indent=2, default=str)

    def delete_world(self, world_id: str):
        world_dir = self._dir / world_id
        if not world_dir.is_dir():
            raise FileNotFoundError(f"World '{world_id}' not found.")
        shutil.rmtree(world_dir)

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
        step_path = self._dir / world_id / "step_map_generation.json"
        if not step_path.exists():
            return

        step_data = self._enrichment_cache.get(world_id)
        if step_data is None:
            if len(self._enrichment_cache) >= self._enrichment_cache_max:
                oldest = next(iter(self._enrichment_cache))
                self.write_enrichment_to_disk(oldest)
            with open(step_path, "r", encoding="utf-8") as f:
                step_data = json.load(f)
            self._enrichment_cache[world_id] = step_data

        node_index = step_data.get("_node_index")
        if node_index is None:
            node_index = self.build_enrichment_node_index(step_data.get("data", {}))
            step_data["_node_index"] = node_index

        entry = node_index.get(node_id)
        if entry:
            entry[field] = value

    def flush_enrichment_cache(self, world_id: str = None):
        if world_id:
            self.write_enrichment_to_disk(world_id)
        else:
            for wid in list(self._enrichment_cache.keys()):
                self.write_enrichment_to_disk(wid)

    def write_enrichment_to_disk(self, world_id: str):
        step_data = self._enrichment_cache.pop(world_id, None)
        if step_data is None:
            return
        step_path = self._dir / world_id / "step_map_generation.json"
        step_data.pop("_node_index", None)
        with open(step_path, "w", encoding="utf-8") as f:
            json.dump(step_data, f, indent=2, default=str)

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
