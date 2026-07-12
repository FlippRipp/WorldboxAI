"""SillyTavern lorebook (World Info) library.

Lorebooks are imported once into ``data/lorebooks/{id}.json`` and linked to
scenarios and worlds via ``data/lorebooks/links.json``. A new save inherits the
links of its story source; the linked entries are embedded into the save's
world index (``world_entries`` with ``source_type='lorebook'``) so the existing
RAG retrieval surfaces them. ST keyword triggers are not replayed — keywords are
folded into the embedded text so they still steer semantic similarity, and
``constant`` entries are always injected.

Two import shapes are supported:

- V2 World Info export: ``{"entries": {"0": {...}, ...}}`` with ``key``,
  ``keysecondary``, ``comment``, ``content``, ``constant``, ``disable``.
- Character-book format: ``{"entries": [{...}, ...]}`` with ``keys``,
  ``secondary_keys``, ``content``, ``enabled``, ``constant``,
  ``insertion_order`` (also accepted embedded in a character card under
  ``data.character_book`` / ``character_book``).
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Reserved pseudo-book id for a save's free-standing story entries. Imported
# books can never claim it: _slugify strips leading/trailing underscores.
STORY_LOREBOOK_ID = "__story__"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "lorebook"


def _as_key_list(value) -> list[str]:
    """ST stores keys as a list, but hand-edited files sometimes use a
    comma-separated string."""
    if isinstance(value, list):
        return [str(k).strip() for k in value if str(k).strip()]
    if isinstance(value, str):
        return [k.strip() for k in value.split(",") if k.strip()]
    return []


def _normalize_entry(raw_entry: dict, uid, *, v2: bool) -> dict | None:
    content = str(raw_entry.get("content") or "").strip()
    if not content:
        return None
    if v2:
        title = str(raw_entry.get("comment") or "").strip()
        keys = _as_key_list(raw_entry.get("key"))
        secondary = _as_key_list(raw_entry.get("keysecondary"))
        enabled = not bool(raw_entry.get("disable", False))
        order = raw_entry.get("order", 100)
    else:
        title = str(raw_entry.get("name") or raw_entry.get("comment") or "").strip()
        keys = _as_key_list(raw_entry.get("keys"))
        secondary = _as_key_list(raw_entry.get("secondary_keys"))
        enabled = bool(raw_entry.get("enabled", True))
        order = raw_entry.get("insertion_order", raw_entry.get("order", 100))
    return {
        "uid": str(raw_entry.get("uid", raw_entry.get("id", uid))),
        "title": title,
        "keys": keys,
        "secondary_keys": secondary,
        "content": content,
        "constant": bool(raw_entry.get("constant", False)),
        "enabled": enabled,
        "order": int(order) if isinstance(order, (int, float)) else 100,
        "raw": raw_entry,
    }


def parse_sillytavern_lorebook(raw: dict, fallback_name: str = "") -> dict:
    """Normalize a SillyTavern lorebook into ``{name, description, entries, stats}``.

    Raises ``ValueError`` when the payload has no recognizable entries.
    """
    if not isinstance(raw, dict):
        raise ValueError("Lorebook must be a JSON object.")

    # A full character card carries its book under (data.)character_book.
    book = raw
    for candidate in (raw.get("data", {}).get("character_book") if isinstance(raw.get("data"), dict) else None,
                      raw.get("character_book")):
        if isinstance(candidate, dict) and "entries" in candidate:
            book = candidate
            break

    entries_raw = book.get("entries")
    if isinstance(entries_raw, dict):
        items = [(uid, e) for uid, e in entries_raw.items() if isinstance(e, dict)]
        v2 = True
    elif isinstance(entries_raw, list):
        items = [(i, e) for i, e in enumerate(entries_raw) if isinstance(e, dict)]
        v2 = False
    else:
        raise ValueError("Not a SillyTavern lorebook: missing 'entries'.")
    if not items:
        raise ValueError("Lorebook contains no entries.")

    entries, skipped = [], 0
    seen_uids = set()
    for uid, raw_entry in items:
        entry = _normalize_entry(raw_entry, uid, v2=v2)
        if entry is None:
            skipped += 1
            continue
        # Duplicate uids would collide as world_entries source_ids.
        while entry["uid"] in seen_uids:
            entry["uid"] += "_dup"
        seen_uids.add(entry["uid"])
        entries.append(entry)
    if not entries:
        raise ValueError("Lorebook contains no entries with content.")
    entries.sort(key=lambda e: e["order"])

    name = str(book.get("name") or raw.get("name") or fallback_name or "").strip()
    return {
        "name": name or "Imported Lorebook",
        "description": str(book.get("description") or "").strip(),
        "entries": entries,
        "stats": {"total": len(items), "imported": len(entries), "skipped": skipped},
    }


# ── free-standing story entries ──────────────────────────────────────────────
#
# A save can carry lorebook entries of its own, not belonging to any imported
# book. They live in the save's metadata (``story_lorebook_entries``) and are
# normalized to the imported-entry shape so they ride the same embed path
# (keywords, constant injection, enabled flag, RAG retrieval).

_STORY_ENTRY_FIELDS = ("title", "keys", "secondary_keys", "content", "constant", "enabled")


def make_story_entry(data: dict, uid: str | None = None) -> dict:
    """Normalize a free-standing story entry; raises ValueError on empty content."""
    content = str(data.get("content") or "").strip()
    if not content:
        raise ValueError("Lorebook entry content cannot be empty.")
    return {
        "uid": str(uid) if uid else uuid.uuid4().hex[:8],
        "title": str(data.get("title") or "").strip(),
        "keys": _as_key_list(data.get("keys")),
        "secondary_keys": _as_key_list(data.get("secondary_keys")),
        "content": content,
        "constant": bool(data.get("constant", False)),
        "enabled": bool(data.get("enabled", True)),
    }


def patch_story_entry(entry: dict, patch: dict) -> dict:
    """Apply a partial edit to a story entry, keeping its uid and re-normalizing."""
    merged = dict(entry)
    for field in _STORY_ENTRY_FIELDS:
        if field in patch:
            merged[field] = patch[field]
    return make_story_entry(merged, uid=entry.get("uid"))


def story_entries_book(entries: list[dict]) -> dict:
    """Wrap a save's story entries as a pseudo-lorebook for the embed path,
    giving them world-index source ids of ``__story__:{uid}``."""
    return {"id": STORY_LOREBOOK_ID, "name": "Story Entries", "entries": list(entries or [])}


class LorebookStore:
    def __init__(self, data_dir):
        self.lorebooks_dir = Path(data_dir) / "lorebooks"

    def _path(self, lorebook_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_]+", lorebook_id or ""):
            raise ValueError(f"Invalid lorebook id: {lorebook_id!r}")
        return self.lorebooks_dir / f"{lorebook_id}.json"

    # ── CRUD ────────────────────────────────────────────────────────────────

    def import_lorebook(self, raw: dict, name: str = None) -> dict:
        parsed = parse_sillytavern_lorebook(raw, fallback_name=name or "")
        if name and name.strip():
            parsed["name"] = name.strip()
        stats = parsed.pop("stats")
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": self._unique_id(_slugify(parsed["name"])),
            "created_at": now,
            "updated_at": now,
            **parsed,
        }
        self.lorebooks_dir.mkdir(parents=True, exist_ok=True)
        with open(self._path(record["id"]), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        return {"lorebook": record, "stats": stats}

    def list_lorebooks(self) -> list[dict]:
        if not self.lorebooks_dir.exists():
            return []
        out = []
        for path in sorted(self.lorebooks_dir.glob("*.json")):
            if path.name == "links.json":
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries = data.get("entries", [])
                out.append({
                    "id": data.get("id", path.stem),
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "entry_count": len(entries),
                    "enabled_count": sum(1 for e in entries if e.get("enabled")),
                    "constant_count": sum(1 for e in entries if e.get("constant")),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping unreadable lorebook %s: %s", path.name, exc)
        return out

    def exists(self, lorebook_id: str) -> bool:
        try:
            return self._path(lorebook_id).exists()
        except ValueError:
            return False

    def load_lorebook(self, lorebook_id: str) -> dict:
        path = self._path(lorebook_id)
        if not path.exists():
            raise FileNotFoundError(f"Lorebook '{lorebook_id}' not found.")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete_lorebook(self, lorebook_id: str) -> None:
        path = self._path(lorebook_id)
        if path.exists():
            path.unlink()
        links = self._read_links()
        pruned = {k: [i for i in ids if i != lorebook_id] for k, ids in links.items()}
        pruned = {k: ids for k, ids in pruned.items() if ids}
        if pruned != links:
            self._write_links(pruned)

    def update_entry(self, lorebook_id: str, uid: str, patch: dict) -> dict:
        """Patch an entry's editable fields and bump updated_at, which
        invalidates every linked save's embed fingerprint so the entry is
        re-embedded on the next sync."""
        record = self.load_lorebook(lorebook_id)
        for entry in record.get("entries", []):
            if entry.get("uid") == uid:
                break
        else:
            raise FileNotFoundError(f"Entry '{uid}' not found in lorebook '{lorebook_id}'.")
        if "content" in patch:
            content = str(patch["content"] or "").strip()
            if not content:
                raise ValueError("Lorebook entry content cannot be empty.")
            entry["content"] = content
        if "title" in patch:
            entry["title"] = str(patch["title"] or "").strip()
        if "keys" in patch:
            entry["keys"] = _as_key_list(patch["keys"])
        if "secondary_keys" in patch:
            entry["secondary_keys"] = _as_key_list(patch["secondary_keys"])
        if "enabled" in patch:
            entry["enabled"] = bool(patch["enabled"])
        if "constant" in patch:
            entry["constant"] = bool(patch["constant"])
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(self._path(lorebook_id), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        return record

    def set_entry_enabled(self, lorebook_id: str, uid: str, enabled: bool) -> dict:
        return self.update_entry(lorebook_id, uid, {"enabled": enabled})

    # ── links (scenario/world → lorebook ids) ───────────────────────────────

    def _links_path(self) -> Path:
        return self.lorebooks_dir / "links.json"

    def _read_links(self) -> dict:
        path = self._links_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_links(self, links: dict) -> None:
        self.lorebooks_dir.mkdir(parents=True, exist_ok=True)
        with open(self._links_path(), "w", encoding="utf-8") as f:
            json.dump(links, f, indent=2, ensure_ascii=False)

    def get_links(self, kind: str, target_id: str) -> list[str]:
        return list(self._read_links().get(f"{kind}:{target_id}", []))

    def set_links(self, kind: str, target_id: str, lorebook_ids: list[str]) -> list[str]:
        ids = []
        for lid in lorebook_ids or []:
            if lid not in ids and self._path(lid).exists():
                ids.append(lid)
        links = self._read_links()
        key = f"{kind}:{target_id}"
        if ids:
            links[key] = ids
        else:
            links.pop(key, None)
        self._write_links(links)
        return ids

    def remove_target(self, kind: str, target_id: str) -> None:
        links = self._read_links()
        if links.pop(f"{kind}:{target_id}", None) is not None:
            self._write_links(links)

    def get_reverse_links(self, lorebook_id: str) -> list[str]:
        """All 'kind:target' keys that link to this lorebook."""
        return [k for k, ids in self._read_links().items() if lorebook_id in ids]

    # ── save-side helpers ────────────────────────────────────────────────────

    def resolve_save_lorebooks(self, lorebook_ids: list[str]) -> list[dict]:
        """Load linked lorebooks, silently skipping deleted ones."""
        out = []
        for lid in lorebook_ids or []:
            try:
                out.append(self.load_lorebook(lid))
            except (FileNotFoundError, ValueError):
                logger.warning("Save references missing lorebook '%s'; skipping.", lid)
        return out

    def embed_fingerprint(self, lorebook_ids: list[str],
                          story_entries: list[dict] | None = None) -> str:
        """Changes whenever the linked set changes, any linked book is edited
        (updated_at bumps), or the save's free-standing story entries change —
        the trigger for re-embedding a save's lore rows."""
        parts = []
        for lid in sorted(set(lorebook_ids or [])):
            try:
                record = self.load_lorebook(lid)
                parts.append([lid, record.get("updated_at", "")])
            except (FileNotFoundError, ValueError):
                continue
        # Story entries carry no updated_at, so their full payload is hashed —
        # any edit or toggle re-embeds. Without them the payload keeps its
        # historical shape so existing saves don't re-embed on next load.
        payload_obj = [parts, story_entries] if story_entries else parts
        payload = json.dumps(payload_obj, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _unique_id(self, base: str) -> str:
        candidate = base
        i = 2
        while (self.lorebooks_dir / f"{candidate}.json").exists() or candidate == "links":
            candidate = f"{base}_{i}"
            i += 1
        return candidate
