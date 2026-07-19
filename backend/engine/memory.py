import asyncio
import os
import json
import sqlite3
import uuid
from typing import List, Optional

try:
    import sqlite_vec
except ImportError:
    # No wheel on this platform (Termux/Android) — use the bundled build.
    from backend.engine import sqlite_vec_fallback as sqlite_vec


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
    "constant": "INTEGER DEFAULT 0",
    # ST 'sticky': after this entry is triggered by retrieval it stays in
    # context for N more turns (0 = off). Only set on lorebook rows.
    "sticky_turns": "INTEGER DEFAULT 0",
    # ST '@ depth' placement: when set, an active entry is injected into the
    # chat N messages from the bottom instead of the normal lore context
    # block. NULL = normal placement. Only set on lorebook rows.
    "injection_depth": "INTEGER",
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


# Bulk-embedding throughput: texts per provider call, and how many of those
# calls may be in flight at once (same spirit as worldgen's enrichment
# semaphore — keeps a big world/lorebook from stampeding the provider).
_EMBED_BATCH_SIZE = 64
_EMBED_CONCURRENCY = 4


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

    def search_memories(self, query_vector: List[float], current_turn: int, limit: int = 3,
                        with_scores: bool = False):
        rows = self.conn.execute(
            """SELECT *, vec_distance_l2(embedding, ?) AS dist
               FROM memories
               WHERE turn_generated <= ?
               ORDER BY dist
               LIMIT ?""",
            (_serialize(query_vector), current_turn, limit),
        ).fetchall()
        results = []
        for row in rows:
            formatted = self._raw_memory_row(row)
            if with_scores:
                formatted["dist"] = row["dist"]
            results.append(formatted)
        return results

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

    def update_memory(self, memory_id: str, fields: dict,
                      vector: Optional[List[float]] = None) -> Optional[dict]:
        """Patch editable columns on a memory; callers pass a fresh embedding
        vector whenever they changed the text. Returns the updated formatted
        row, or None when the id doesn't exist."""
        sets, params = [], []
        if "text" in fields:
            sets.append("text = ?")
            params.append(fields["text"])
        if "summary" in fields:
            sets.append("summary = ?")
            params.append(fields["summary"])
        if "importance" in fields:
            sets.append("importance = ?")
            params.append(int(fields["importance"]))
        if "permanent" in fields:
            sets.append("permanent = ?")
            params.append(1 if fields["permanent"] else 0)
        if "entities" in fields:
            sets.append("entities = ?")
            params.append(json.dumps(fields["entities"] or [], ensure_ascii=False))
        if "topics" in fields:
            sets.append("topics = ?")
            params.append(json.dumps(fields["topics"] or [], ensure_ascii=False))
        if vector is not None:
            sets.append("embedding = ?")
            params.append(_serialize(vector))
        if not sets:
            rows = self.get_memories_by_ids([memory_id])
            return rows[0] if rows else None
        cursor = self.conn.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id = ?",
            (*params, memory_id),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            return None
        rows = self.get_memories_by_ids([memory_id])
        return rows[0] if rows else None

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
        # Lorebook rows are managed by embed_lorebooks and must survive a
        # world re-compile.
        self._world_conn.execute("DELETE FROM world_entries WHERE source_type != 'lorebook'")
        entries = self._build_world_entries(world_data)
        if not entries:
            self._world_conn.commit()
            return 0
        vectors = await self._embed_texts([e["text"] for e in entries], llm)
        for entry, vec in zip(entries, vectors):
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

    async def embed_world_entries(self, entries: list[dict], llm) -> int:
        """Insert additional world entries without touching existing rows.

        Used for content generated after the initial ``embed_world`` pass
        (play-time node backfill, on-demand site expansion). Each entry is a
        {"text", "source_type", "source_id", "region"} dict (id optional).
        Existing rows with the same (source_type, source_id) are replaced so
        re-generating a node never accumulates duplicates.
        """
        if self._world_conn is None:
            raise RuntimeError("World index not initialized. Call init_world_index() first.")
        entries = [e for e in entries if e.get("text")]
        if not entries:
            return 0
        for entry in entries:
            self._world_conn.execute(
                "DELETE FROM world_entries WHERE source_type = ? AND source_id = ?",
                (entry.get("source_type", ""), entry.get("source_id", "")),
            )
        vectors = await self._embed_texts([e["text"] for e in entries], llm)
        for entry, vec in zip(entries, vectors):
            self._world_conn.execute(
                """INSERT INTO world_entries (id, embedding, text, source_type, source_id, region)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    entry.get("id") or str(uuid.uuid4()), _serialize(vec), entry["text"],
                    entry.get("source_type", ""), entry.get("source_id", ""),
                    entry.get("region", ""),
                ),
            )
        self._world_conn.commit()
        return len(entries)

    async def _embed_texts(self, texts: list[str], llm) -> list[List[float]]:
        """Embed texts in _EMBED_BATCH_SIZE provider batches with up to
        _EMBED_CONCURRENCY batches in flight; result order matches input."""
        semaphore = asyncio.Semaphore(_EMBED_CONCURRENCY)

        if hasattr(llm, "get_embeddings"):
            async def embed_chunk(chunk):
                async with semaphore:
                    return await llm.get_embeddings(chunk)

            chunks = [texts[i:i + _EMBED_BATCH_SIZE]
                      for i in range(0, len(texts), _EMBED_BATCH_SIZE)]
            results = await asyncio.gather(*(embed_chunk(c) for c in chunks))
            return [vec for chunk_vectors in results for vec in chunk_vectors]

        async def embed_one(text):
            async with semaphore:
                return await llm.get_embedding(text)

        return list(await asyncio.gather(*(embed_one(t) for t in texts)))

    async def embed_lorebooks(self, lorebooks: list[dict], llm) -> int:
        """Replace all lorebook rows in the world index with the enabled entries
        of the given lorebook records (idempotent — safe to call on every sync)."""
        if self._world_conn is None:
            raise RuntimeError("World index not initialized. Call init_world_index() first.")
        self._world_conn.execute("DELETE FROM world_entries WHERE source_type = 'lorebook'")
        pending = []  # (text, source_id, constant, sticky_turns, injection_depth)
        for book in lorebooks:
            book_id = book.get("id", "")
            book_sticky = int(book.get("sticky_turns") or 0)
            for entry in book.get("entries", []):
                if not entry.get("enabled", True):
                    continue
                text = self._lorebook_entry_text(entry)
                if not text:
                    continue
                # Per-entry sticky override wins; None inherits the book value.
                override = entry.get("sticky_turns")
                sticky = book_sticky if override is None else int(override)
                depth = entry.get("injection_depth")
                pending.append((text, f"{book_id}:{entry.get('uid', '')}",
                                1 if entry.get("constant") else 0, max(0, sticky),
                                None if depth is None else max(0, int(depth))))
        vectors = await self._embed_texts([t for t, _, _, _, _ in pending], llm)
        for (text, source_id, constant, sticky, depth), vec in zip(pending, vectors):
            self._world_conn.execute(
                """INSERT INTO world_entries (id, embedding, text, source_type, source_id, region, constant, sticky_turns, injection_depth)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), _serialize(vec), text, "lorebook", source_id, "", constant, sticky, depth),
            )
        self._world_conn.commit()
        return len(pending)

    @staticmethod
    def _lorebook_entry_text(entry: dict) -> str:
        """Fold title and trigger keywords into the stored text so ST keywords
        still steer semantic similarity and the entry reads well in prompts."""
        content = (entry.get("content") or "").strip()
        if not content:
            return ""
        title = (entry.get("title") or "").strip()
        keys = [k for k in (entry.get("keys") or []) + (entry.get("secondary_keys") or []) if k]
        prefix = "Lore"
        if title:
            prefix += f" — {title}"
        if keys:
            prefix += f" (keywords: {', '.join(keys)})"
        return f"{prefix}: {content}"

    def get_constant_lorebook_entries(self) -> list[dict]:
        """Always-injected lorebook entries (ST 'constant' flag); no vector math."""
        if self._world_conn is None:
            return []
        rows = self._world_conn.execute(
            """SELECT * FROM world_entries
               WHERE source_type = 'lorebook' AND COALESCE(constant, 0) = 1
               ORDER BY source_id""",
        ).fetchall()
        return [
            {
                "id": row["id"] or "",
                "text": row["text"] or "",
                "source_id": row["source_id"] or "",
                "injection_depth": row["injection_depth"],
            }
            for row in rows
        ]

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
        maps = world_data.get("maps")
        maps = maps if isinstance(maps, dict) else None
        if maps is not None:
            # world_format 2: hierarchical maps. Root-map nodes keep the exact
            # legacy flat-map format so incremental entries (backfill) written
            # against migrated flat worlds match the compiled index verbatim.
            root_map_id = world_data.get("root_map_id", "root")
            for map_id, map_rec in maps.items():
                label = map_rec.get("label", "")
                description = map_rec.get("description", "")
                if label or description:
                    entries.append({
                        "id": str(uuid.uuid4()),
                        "text": f"Map: {label} ({map_rec.get('level_type', 'world')}). {description}",
                        "source_type": "map",
                        "source_id": map_id,
                        "region": label,
                    })
                for node in map_rec.get("nodes", []):
                    if not (node.get("name") and node.get("description")):
                        continue
                    if map_id == root_map_id:
                        text = f"Location: {node['name']} ({node.get('type', 'location')}). {node['description']}"
                        region = node.get("region") or node.get("name", "")
                    else:
                        text = f"Location [{label}]: {node['name']} ({node.get('type', 'location')}). {node['description']}"
                        region = label
                    entries.append({
                        "id": str(uuid.uuid4()),
                        "text": text,
                        "source_type": "node",
                        "source_id": node.get("id", ""),
                        "region": region,
                    })
        else:
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
        # Lazily-expanded site interiors (districts/venues inside major
        # locations). Format must stay in lockstep with
        # wbworldgen.worldgen.expansion.sites.site_world_entries, which emits
        # the same entries incrementally when a site is expanded mid-play.
        for parent_id, site in (world_data.get("site_maps") or {}).items():
            parent_name = site.get("name", "")
            if site.get("layout_summary"):
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": f"Layout of {parent_name}: {site['layout_summary']}",
                    "source_type": "site",
                    "source_id": parent_id,
                    "region": parent_name,
                })
            for sub in site.get("sub_locations", []):
                if not sub.get("name"):
                    continue
                text = f"Place in {parent_name}: {sub['name']} ({sub.get('type', 'place')})"
                if sub.get("description"):
                    text += f". {sub['description']}"
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": text,
                    "source_type": "site_node",
                    "source_id": sub.get("id", ""),
                    "region": parent_name,
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
        if maps is not None:
            # world_format 2: connections are a flat top-level list keyed by map.
            for conn in world_data.get("connections", []):
                if conn.get("hidden"):
                    continue
                from_end = conn.get("from") or {}
                to_end = conn.get("to") or {}
                from_label = (maps.get(from_end.get("map_id")) or {}).get("label") or from_end.get("map_id") or "?"
                to_label = (maps.get(to_end.get("map_id")) or {}).get("label") or to_end.get("map_id") or "?"
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": f"Connection: {conn.get('kind', 'passage')} '{conn.get('name', '')}' linking {from_label} and {to_label}. {conn.get('description', '')}",
                    "source_type": "connection",
                    "source_id": conn.get("id", ""),
                    "region": from_label,
                })
        else:
            for conn in world_data.get("map_connections", []):
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": f"Layer Connection: {conn.get('connection_type', 'passage')} from {conn.get('from_layer_id', '?')} to {conn.get('to_layer_id', '?')}. {conn.get('description', '')}",
                    "source_type": "connection",
                    "source_id": conn.get("id", ""),
                    "region": "",
                })
        return entries

    def search_world(self, query_vector: List[float], limit: int = 3,
                     with_scores: bool = False) -> list[dict]:
        if self._world_conn is None:
            return []
        # Constant lorebook entries are excluded: they are always injected
        # separately, so retrieving them here would duplicate context.
        rows = self._world_conn.execute(
            """SELECT *, vec_distance_l2(embedding, ?) AS dist
               FROM world_entries
               WHERE NOT (source_type = 'lorebook' AND COALESCE(constant, 0) = 1)
               ORDER BY dist
               LIMIT ?""",
            (_serialize(query_vector), limit),
        ).fetchall()
        results = []
        for row in rows:
            entry = {
                "id": row["id"] or "",
                "text": row["text"] or "",
                "source_type": row["source_type"] or "",
                "source_id": row["source_id"] or "",
                "region": row["region"] or "",
                "sticky_turns": int(row["sticky_turns"] or 0),
                "injection_depth": row["injection_depth"],
            }
            if with_scores:
                entry["dist"] = row["dist"]
            results.append(entry)
        return results

    def get_world_entries_by_source_ids(self, source_ids: list[str]) -> list[dict]:
        """Rows for the given stable source ids, in the given order — used to
        force sticky entries into context without a vector search. Constant
        rows are excluded (they're always injected separately)."""
        if self._world_conn is None or not source_ids:
            return []
        placeholders = ", ".join("?" for _ in source_ids)
        rows = self._world_conn.execute(
            f"""SELECT id, text, source_type, source_id, region,
                       COALESCE(sticky_turns, 0) AS sticky_turns, injection_depth
                FROM world_entries
                WHERE source_id IN ({placeholders}) AND COALESCE(constant, 0) = 0""",
            list(source_ids),
        ).fetchall()
        by_source = {row["source_id"]: row for row in rows}
        return [
            {
                "id": row["id"] or "",
                "text": row["text"] or "",
                "source_type": row["source_type"] or "",
                "source_id": row["source_id"] or "",
                "region": row["region"] or "",
                "sticky_turns": int(row["sticky_turns"] or 0),
                "injection_depth": row["injection_depth"],
            }
            for sid in source_ids
            if (row := by_source.get(sid)) is not None
        ]

    @staticmethod
    def _format_world_row(row) -> dict:
        return {
            "id": row["id"] or "",
            "text": row["text"] or "",
            "source_type": row["source_type"] or "",
            "source_id": row["source_id"] or "",
            "region": row["region"] or "",
            "constant": bool(row["constant"]),
            "sticky_turns": int(row["sticky_turns"] or 0),
            "injection_depth": row["injection_depth"],
        }

    def list_world_entries(self, limit: int = 1000) -> list[dict]:
        """All world-index rows (including constant lorebook entries) without
        the embedding blob, for browsing/editing in the UI."""
        if self._world_conn is None:
            return []
        rows = self._world_conn.execute(
            """SELECT id, text, source_type, source_id, region,
                      COALESCE(constant, 0) AS constant,
                      COALESCE(sticky_turns, 0) AS sticky_turns,
                      injection_depth
               FROM world_entries
               ORDER BY source_type, source_id
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._format_world_row(row) for row in rows]

    def get_world_entry(self, entry_id: str) -> Optional[dict]:
        if self._world_conn is None:
            return None
        row = self._world_conn.execute(
            """SELECT id, text, source_type, source_id, region,
                      COALESCE(constant, 0) AS constant,
                      COALESCE(sticky_turns, 0) AS sticky_turns,
                      injection_depth
               FROM world_entries WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        return self._format_world_row(row) if row is not None else None

    def update_world_entry(self, entry_id: str, text: str,
                           vector: List[float]) -> Optional[dict]:
        """Replace a world entry's text and embedding in place. Safe for
        world-derived rows (embedded once at story creation); lorebook rows
        are re-synced from their JSON source, so callers must not edit those
        here. Returns the updated row, or None when the id doesn't exist."""
        if self._world_conn is None:
            return None
        cursor = self._world_conn.execute(
            "UPDATE world_entries SET text = ?, embedding = ? WHERE id = ?",
            (text, _serialize(vector), entry_id),
        )
        self._world_conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_world_entry(entry_id)

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
