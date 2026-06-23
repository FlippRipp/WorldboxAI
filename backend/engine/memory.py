import os
import json
import lancedb
from lancedb.pydantic import LanceModel, Vector
from typing import List, Optional
import uuid


def create_memory_model(dim: int) -> type[LanceModel]:
    class MemoryEntry(LanceModel):
        id: str
        vector: Vector(dim)
        text: str
        summary: str = ""
        entities: str = "[]"
        topics: str = "[]"
        turn_range: str = ""
        turn_generated: int
        importance: int
        reason: str = ""
        permanent: bool = False
    return MemoryEntry


def create_world_entry_model(dim: int) -> type[LanceModel]:
    class WorldEntry(LanceModel):
        id: str
        vector: Vector(dim)
        text: str
        source_type: str
        source_id: str
        region: str = ""
    return WorldEntry


class MemoryManager:
    def __init__(self, db_path: str, embedding_dim: int):
        os.makedirs(db_path, exist_ok=True)
        self.db = lancedb.connect(db_path)
        self.schema = create_memory_model(embedding_dim)

        if "memories" not in self.db.table_names():
            self.table = self.db.create_table("memories", schema=self.schema)
        else:
            self.table = self.db.open_table("memories")
            actual_dim = self.table.schema.field("vector").type.list_size
            if actual_dim != embedding_dim:
                print(f"WARNING: Database vector dimension ({actual_dim}) does not match current LLM dimension ({embedding_dim}). This may cause errors.")
            self._ensure_columns()

        self._world_db = None
        self._world_table = None
        self._world_schema = None
        self._embedding_dim = embedding_dim

    def add_memory(self, vector: List[float], text: str, turn: int, importance: int,
                   summary: str = "", entities: list[str] = None, topics: list[str] = None,
                   turn_range: str = "", reason: str = "", permanent: bool = False) -> str:
        entities_json = json.dumps(entities or [], ensure_ascii=False)
        topics_json = json.dumps(topics or [], ensure_ascii=False)
        memory_id = str(uuid.uuid4())
        self.table.add([
            {
                "id": memory_id,
                "vector": vector,
                "text": text,
                "summary": summary or text,
                "entities": entities_json,
                "topics": topics_json,
                "turn_range": turn_range or "",
                "turn_generated": turn,
                "importance": importance,
                "reason": reason or "",
                "permanent": permanent,
            }
        ])
        return memory_id

    def search_memories(self, query_vector: List[float], current_turn: int, limit: int = 3):
        if self.table.count_rows() == 0:
            return []

        results = self.table.search(query_vector).where(f"turn_generated <= {current_turn}").limit(limit).to_list()
        return results

    def purge_decayed_memories(self, current_turn: int):
        if self.table.count_rows() == 0:
            return

        self.table.delete(f"permanent = false AND importance <= 3 AND turn_generated < {current_turn - 10}")
        self.table.delete(f"permanent = false AND importance > 3 AND importance <= 7 AND turn_generated < {current_turn - 30}")

    def rollback_memories(self, target_turn: int):
        if self.table.count_rows() == 0:
            return
        self.table.delete(f"turn_generated > {target_turn}")

    def list_all_memories(self, limit: int = 50) -> list[dict]:
        if self.table.count_rows() == 0:
            return []
        rows = self.table.search().limit(limit).to_list()
        return [self._format_memory_row(row) for row in rows]

    def delete_memory(self, memory_id: str) -> bool:
        if self.table.count_rows() == 0:
            return False
        before = self.table.count_rows()
        self.table.delete(f"id = '{memory_id}'")
        return self.table.count_rows() < before

    def get_memory_count(self) -> int:
        try:
            return self.table.count_rows()
        except Exception:
            return 0

    def get_memories_by_ids(self, memory_ids: list[str]) -> list[dict]:
        if self.table.count_rows() == 0 or not memory_ids:
            return []
        results = []
        for row in self.table.search().limit(200).to_list():
            if row.get("id") in memory_ids:
                results.append(self._format_memory_row(row))
            if len(results) >= len(memory_ids):
                break
        return results

    def _format_memory_row(self, row: dict) -> dict:
        entities_raw = row.get("entities", "[]")
        topics_raw = row.get("topics", "[]")
        try:
            entities = json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
        except (json.JSONDecodeError, TypeError):
            entities = []
        try:
            topics = json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
        except (json.JSONDecodeError, TypeError):
            topics = []
        return {
            "id": row.get("id", ""),
            "text": row.get("text", ""),
            "summary": row.get("summary", row.get("text", "")),
            "entities": entities if isinstance(entities, list) else [],
            "topics": topics if isinstance(topics, list) else [],
            "turn_range": row.get("turn_range", ""),
            "turn_generated": row.get("turn_generated", 0),
            "importance": row.get("importance", 5),
            "reason": row.get("reason", ""),
            "permanent": row.get("permanent", False),
        }

    def init_world_index(self, world_db_path: str):
        self._world_schema = create_world_entry_model(self._embedding_dim)
        os.makedirs(world_db_path, exist_ok=True)
        self._world_db = lancedb.connect(world_db_path)
        if "world_entries" not in self._world_db.table_names():
            self._world_table = self._world_db.create_table("world_entries", schema=self._world_schema)
        else:
            self._world_table = self._world_db.open_table("world_entries")

    def has_world_index(self) -> bool:
        return self._world_table is not None

    async def embed_world(self, world_data: dict, llm) -> int:
        if self._world_table is None:
            raise RuntimeError("World index not initialized. Call init_world_index() first.")
        # Clear existing entries so re-embedding never accumulates duplicates.
        if self._world_table.count_rows() > 0:
            self._world_db.drop_table("world_entries")
            self._world_table = self._world_db.create_table("world_entries", schema=self._world_schema)
        entries = self._build_world_entries(world_data)
        if not entries:
            return 0
        texts = [e["text"] for e in entries]
        vectors = []
        for text in texts:
            vec = await llm.get_embedding(text)
            vectors.append(vec)
        rows = []
        for i, entry in enumerate(entries):
            rows.append({
                "id": entry["id"],
                "vector": vectors[i],
                "text": entry["text"],
                "source_type": entry["source_type"],
                "source_id": entry["source_id"],
                "region": entry["region"],
            })
        self._world_table.add(rows)
        return len(rows)

    def _build_world_entries(self, world_data: dict) -> list[dict]:
        entries = []
        rules = world_data.get("rules", {})
        lore = world_data.get("lore", {})
        regions = world_data.get("regions", {}).get("regions", [])
        world_map = world_data.get("map", {})

        if lore.get("premise"):
            entries.append({
                "id": str(uuid.uuid4()),
                "text": f"World Premise: {lore['premise']}",
                "source_type": "lore",
                "source_id": "premise",
                "region": "",
            })
        if lore.get("central_conflict"):
            entries.append({
                "id": str(uuid.uuid4()),
                "text": f"Central Conflict: {lore['central_conflict']}",
                "source_type": "lore",
                "source_id": "central_conflict",
                "region": "",
            })
        if lore.get("creation_myth"):
            entries.append({
                "id": str(uuid.uuid4()),
                "text": f"Creation Myth: {lore['creation_myth']}",
                "source_type": "lore",
                "source_id": "creation_myth",
                "region": "",
            })
        for era in lore.get("historical_eras", []):
            entries.append({
                "id": str(uuid.uuid4()),
                "text": f"Historical Era - {era.get('name', '')}: {era.get('summary', '')}",
                "source_type": "era",
                "source_id": era.get("name", ""),
                "region": "",
            })
        for region in regions:
            region_name = region.get("name", "")
            landmarks = ", ".join(region.get("landmarks", []))
            factions = ", ".join(region.get("factions", []))
            entry_text = (
                f"Region: {region_name}. "
                f"Terrain: {region.get('terrain', '')}. "
                f"Climate: {region.get('climate', '')}"
            )
            if landmarks:
                entry_text += f". Landmarks: {landmarks}"
            if factions:
                entry_text += f". Factions: {factions}"
            entries.append({
                "id": str(uuid.uuid4()),
                "text": entry_text,
                "source_type": "region",
                "source_id": region_name,
                "region": region_name,
            })
            # Use named_locations (carries descriptions) for landmark entries.
            # Falls back to the bare name list for worlds compiled before this change.
            named_locations = region.get("named_locations", [])
            lm_named = {nl["name"].lower() for nl in named_locations if nl.get("category") == "landmark"}
            for nl in named_locations:
                if nl.get("category") == "landmark" and nl.get("name"):
                    desc = nl.get("description", "")
                    text = f"Landmark in {region_name}: {nl['name']}"
                    if desc:
                        text += f". {desc}"
                    entries.append({
                        "id": str(uuid.uuid4()),
                        "text": text,
                        "source_type": "landmark",
                        "source_id": nl["name"],
                        "region": region_name,
                    })
            # Fall back for any landmarks not present in named_locations.
            for landmark in region.get("landmarks", []):
                if landmark.lower() not in lm_named:
                    entries.append({
                        "id": str(uuid.uuid4()),
                        "text": f"Landmark in {region_name}: {landmark}",
                        "source_type": "landmark",
                        "source_id": landmark,
                        "region": region_name,
                    })
            # Use faction_details (carries type + description) when available.
            # Falls back to the bare name list for worlds compiled before this change.
            faction_details = region.get("faction_details", [])
            fd_names = {fd["name"].lower() for fd in faction_details if fd.get("name")}
            for fd in faction_details:
                fname = fd.get("name", "")
                if not fname:
                    continue
                parts = [f"Faction in {region_name}: {fname}"]
                if fd.get("type"):
                    parts.append(f"Type: {fd['type']}")
                if fd.get("description"):
                    parts.append(fd["description"])
                if fd.get("settlements"):
                    parts.append(f"Settlements: {', '.join(fd['settlements'])}")
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": ". ".join(parts),
                    "source_type": "faction",
                    "source_id": fname,
                    "region": region_name,
                })
            # Fall back for any factions not present in faction_details.
            for faction in region.get("factions", []):
                if faction.lower() not in fd_names:
                    entries.append({
                        "id": str(uuid.uuid4()),
                        "text": f"Faction in {region_name}: {faction}",
                        "source_type": "faction",
                        "source_id": faction,
                        "region": region_name,
                    })
        for map_layer in world_data.get("map_layers", []):
            layer_name = map_layer.get("name", "")
            layer_map = map_layer.get("map", {})
            for node in layer_map.get("nodes", []):
                if node.get("name") and node.get("description"):
                    entries.append({
                        "id": str(uuid.uuid4()),
                        "text": f"Location [{layer_name}]: {node['name']} ({node.get('type', 'location')}). {node['description']}",
                        "source_type": "node",
                        "source_id": node.get("id", ""),
                        "region": layer_name,
                    })
        for node in world_map.get("nodes", []):
            if node.get("name") and node.get("description"):
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": f"Location: {node['name']} ({node.get('type', 'location')}). {node['description']}",
                    "source_type": "node",
                    "source_id": node.get("id", ""),
                    "region": node.get("name", ""),
                })
        for layer_cfg in world_data.get("layers", []):
            lid = layer_cfg.get("layer_id", "")
            lname = layer_cfg.get("name", lid)
            ldesc = layer_cfg.get("description", "")
            if lid and lname:
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": f"Layer: {lname} ({layer_cfg.get('layer_type', 'surface')}). {ldesc}",
                    "source_type": "layer",
                    "source_id": lid,
                    "region": lid,
                })
        for conn in world_data.get("map_connections", []):
            entries.append({
                "id": str(uuid.uuid4()),
                "text": f"Layer Connection: {conn.get('connection_type', 'passage')} from {conn.get('from_layer_id', '?')} to {conn.get('to_layer_id', '?')}. {conn.get('description', '')}",
                "source_type": "connection",
                "source_id": conn.get("id", ""),
                "region": "",
            })
        return entries

    def search_world(self, query_vector: List[float], limit: int = 3) -> list[dict]:
        if self._world_table is None or self._world_table.count_rows() == 0:
            return []
        results = self._world_table.search(query_vector).limit(limit).to_list()
        formatted = []
        for row in results:
            formatted.append({
                "id": row.get("id", ""),
                "text": row.get("text", ""),
                "source_type": row.get("source_type", ""),
                "source_id": row.get("source_id", ""),
                "region": row.get("region", ""),
            })
        return formatted

    def get_node_by_id(self, node_id: str) -> Optional[dict]:
        if self._world_table is None or self._world_table.count_rows() == 0:
            return None
        results = self._world_table.search().where(f"source_type = 'node'").limit(500).to_list()
        for row in results:
            if row.get("source_id") == node_id:
                return {
                    "id": row.get("source_id", ""),
                    "name": row.get("text", "").replace("Location: ", "").split(" (")[0] if row.get("text") else "",
                    "description": row.get("text", ""),
                    "region": row.get("region", ""),
                }
        return None

    def get_region_info(self, region_name: str) -> Optional[dict]:
        if self._world_table is None or self._world_table.count_rows() == 0:
            return None
        results = self._world_table.search().where(f"source_type = 'region'").limit(50).to_list()
        for row in results:
            if row.get("source_id") == region_name or row.get("region") == region_name:
                return {
                    "name": row.get("source_id", ""),
                    "text": row.get("text", ""),
                }
        return None

    def get_world_entry_count(self) -> int:
        if self._world_table is None:
            return 0
        try:
            return self._world_table.count_rows()
        except Exception:
            return 0

    def _ensure_columns(self):
        """Add new columns to existing tables to support schema evolution."""
        existing_fields = {field.name for field in self.table.schema}
        defaults = {
            "summary": "",
            "entities": "[]",
            "topics": "[]",
            "turn_range": "",
            "reason": "",
            "permanent": False,
        }
        missing = {k: v for k, v in defaults.items() if k not in existing_fields}
        if missing:
            try:
                self.table.add_columns(missing)
                print(f"[Memory] Added columns: {list(missing.keys())}")
            except Exception as e:
                print(f"[Memory] Could not add columns (may be unsupported by this LanceDB version): {e}")
