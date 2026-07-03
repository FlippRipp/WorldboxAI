import os
import json
import sqlite3
import uuid
from typing import List, Optional

import sqlite_vec


_MEMORY_COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "embedding": "BLOB",
    "text": "TEXT",
    "summary": "TEXT DEFAULT ''",
    "entities": "TEXT DEFAULT '[]'",
    "topics": "TEXT DEFAULT '[]'",
    "turn_range": "TEXT DEFAULT ''",
    "turn_generated": "INTEGER",
    "importance": "INTEGER",
    "reason": "TEXT DEFAULT ''",
    "permanent": "INTEGER DEFAULT 0",
}

_WORLD_COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "embedding": "BLOB",
    "text": "TEXT",
    "source_type": "TEXT",
    "source_id": "TEXT",
    "region": "TEXT DEFAULT ''",
}


def _connect(path: str) -> sqlite3.Connection:
    """Open a sqlite connection with the sqlite-vec extension loaded.
    check_same_thread=False: the engine is shared across event-loop threads in
    tests (TestClient spins a fresh portal thread per client); access is always
    sequential, never concurrent, so cross-thread use is safe."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _serialize(vector: List[float]) -> bytes:
    return sqlite_vec.serialize_float32(list(vector))


class MemoryManager:
    def __init__(self, db_path: str, embedding_dim: int):
        os.makedirs(db_path, exist_ok=True)
        self._embedding_dim = embedding_dim
        self._db_file = os.path.join(db_path, "memories.db")
        self.conn = _connect(self._db_file)
        self._create_table(self.conn, "memories", _MEMORY_COLUMNS)
        self._ensure_columns(self.conn, "memories", _MEMORY_COLUMNS)

        self._world_conn = None

    def close(self):
        """Release the SQLite handles. On Windows an open handle blocks
        deleting the db files (e.g. a test's TemporaryDirectory cleanup)."""
        self.conn.close()
        if self._world_conn is not None:
            self._world_conn.close()
            self._world_conn = None

    # ── schema helpers ───────────────────────────────────────────────────────

    def _create_table(self, conn: sqlite3.Connection, name: str, columns: dict):
        cols_sql = ", ".join(f"{col} {decl}" for col, decl in columns.items())
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols_sql})")
        conn.commit()

    def _ensure_columns(self, conn: sqlite3.Connection, name: str, columns: dict):
        """Add any missing columns to support schema evolution on older DB files."""
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}
        added = []
        for col, decl in columns.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE {name} ADD COLUMN {col} {decl}")
                    added.append(col)
                except sqlite3.OperationalError as e:
                    print(f"[Memory] Could not add column {col} to {name}: {e}")
        if added:
            conn.commit()
            print(f"[Memory] Added columns to {name}: {added}")

    # ── memory CRUD ──────────────────────────────────────────────────────────

    def add_memory(self, vector: List[float], text: str, turn: int, importance: int,
                   summary: str = "", entities: list[str] = None, topics: list[str] = None,
                   turn_range: str = "", reason: str = "", permanent: bool = False) -> str:
        entities_json = json.dumps(entities or [], ensure_ascii=False)
        topics_json = json.dumps(topics or [], ensure_ascii=False)
        memory_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO memories
               (id, embedding, text, summary, entities, topics, turn_range,
                turn_generated, importance, reason, permanent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory_id, _serialize(vector), text, summary or text,
                entities_json, topics_json, turn_range or "",
                turn, importance, reason or "", 1 if permanent else 0,
            ),
        )
        self.conn.commit()
        return memory_id

    def search_memories(self, query_vector: List[float], current_turn: int, limit: int = 3):
        rows = self.conn.execute(
            """SELECT *, vec_distance_l2(embedding, ?) AS dist
               FROM memories
               WHERE turn_generated <= ?
               ORDER BY dist
               LIMIT ?""",
            (_serialize(query_vector), current_turn, limit),
        ).fetchall()
        return [self._raw_memory_row(row) for row in rows]

    def purge_decayed_memories(self, current_turn: int):
        self.conn.execute(
            "DELETE FROM memories WHERE permanent = 0 AND importance <= 3 AND turn_generated < ?",
            (current_turn - 10,),
        )
        self.conn.execute(
            "DELETE FROM memories WHERE permanent = 0 AND importance > 3 AND importance <= 7 AND turn_generated < ?",
            (current_turn - 30,),
        )
        self.conn.commit()

    def rollback_memories(self, target_turn: int):
        self.conn.execute("DELETE FROM memories WHERE turn_generated > ?", (target_turn,))
        self.conn.commit()

    def list_all_memories(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM memories LIMIT ?", (limit,)).fetchall()
        return [self._format_memory_row(row) for row in rows]

    def delete_memory(self, memory_id: str) -> bool:
        cursor = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_memory_count(self) -> int:
        try:
            return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        except Exception:
            return 0

    def get_vector_dimension(self) -> Optional[int]:
        return self._embedding_dim

    def get_memories_by_ids(self, memory_ids: list[str]) -> list[dict]:
        if not memory_ids:
            return []
        placeholders = ", ".join("?" for _ in memory_ids)
        rows = self.conn.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})", tuple(memory_ids)
        ).fetchall()
        return [self._format_memory_row(row) for row in rows]

    def get_memories_by_entity(self, entity: str, limit: int = 3) -> list[dict]:
        matches = []
        for row in self.conn.execute("SELECT * FROM memories").fetchall():
            formatted = self._format_memory_row(row)
            if entity in formatted["entities"]:
                matches.append(formatted)
        matches.sort(key=lambda m: m["turn_generated"], reverse=True)
        return matches[:limit]

    def _raw_memory_row(self, row) -> dict:
        """Stored columns as-is (entities/topics stay JSON strings), mirroring the
        previous backend's raw search rows. Used by search_memories; callers that
        need parsed list fields use _format_memory_row instead."""
        row = dict(row)
        return {
            "id": row.get("id", ""),
            "text": row.get("text", ""),
            "summary": row.get("summary", row.get("text", "")),
            "entities": row.get("entities", "[]"),
            "topics": row.get("topics", "[]"),
            "turn_range": row.get("turn_range", ""),
            "turn_generated": row.get("turn_generated", 0),
            "importance": row.get("importance", 5),
            "reason": row.get("reason", ""),
            "permanent": bool(row.get("permanent", False)),
        }

    def _format_memory_row(self, row) -> dict:
        row = dict(row)
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
            "permanent": bool(row.get("permanent", False)),
        }

    # ── world index ──────────────────────────────────────────────────────────

    def init_world_index(self, world_db_path: str):
        os.makedirs(world_db_path, exist_ok=True)
        self._world_conn = _connect(os.path.join(world_db_path, "world.db"))
        self._create_table(self._world_conn, "world_entries", _WORLD_COLUMNS)
        self._ensure_columns(self._world_conn, "world_entries", _WORLD_COLUMNS)

    def has_world_index(self) -> bool:
        return self._world_conn is not None

    async def embed_world(self, world_data: dict, llm) -> int:
        if self._world_conn is None:
            raise RuntimeError("World index not initialized. Call init_world_index() first.")
        # Clear existing entries so re-embedding never accumulates duplicates.
        self._world_conn.execute("DELETE FROM world_entries")
        entries = self._build_world_entries(world_data)
        if not entries:
            self._world_conn.commit()
            return 0
        for entry in entries:
            vec = await llm.get_embedding(entry["text"])
            self._world_conn.execute(
                """INSERT INTO world_entries (id, embedding, text, source_type, source_id, region)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    entry["id"], _serialize(vec), entry["text"],
                    entry["source_type"], entry["source_id"], entry["region"],
                ),
            )
        self._world_conn.commit()
        return len(entries)

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
        if self._world_conn is None:
            return []
        rows = self._world_conn.execute(
            """SELECT *, vec_distance_l2(embedding, ?) AS dist
               FROM world_entries
               ORDER BY dist
               LIMIT ?""",
            (_serialize(query_vector), limit),
        ).fetchall()
        return [
            {
                "id": row["id"] or "",
                "text": row["text"] or "",
                "source_type": row["source_type"] or "",
                "source_id": row["source_id"] or "",
                "region": row["region"] or "",
            }
            for row in rows
        ]

    def get_node_by_id(self, node_id: str) -> Optional[dict]:
        if self._world_conn is None:
            return None
        row = self._world_conn.execute(
            "SELECT * FROM world_entries WHERE source_type = 'node' AND source_id = ? LIMIT 1",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        text = row["text"] or ""
        return {
            "id": row["source_id"] or "",
            "name": text.replace("Location: ", "").split(" (")[0] if text else "",
            "description": text,
            "region": row["region"] or "",
        }

    def get_region_info(self, region_name: str) -> Optional[dict]:
        if self._world_conn is None:
            return None
        row = self._world_conn.execute(
            """SELECT * FROM world_entries
               WHERE source_type = 'region' AND (source_id = ? OR region = ?)
               LIMIT 1""",
            (region_name, region_name),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row["source_id"] or "",
            "text": row["text"] or "",
        }

    def get_world_entry_count(self) -> int:
        if self._world_conn is None:
            return 0
        try:
            return self._world_conn.execute("SELECT COUNT(*) FROM world_entries").fetchone()[0]
        except Exception:
            return 0
