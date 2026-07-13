import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.lorebook import (
    STORY_LOREBOOK_ID,
    LorebookStore,
    make_story_entry,
    parse_sillytavern_lorebook,
    patch_story_entry,
    story_entries_book,
)
from backend.engine.scenario import ScenarioStore
from backend.engine.session import GameSessionManager


# ── fixtures: the two SillyTavern shapes ─────────────────────────────────────

V2_LOREBOOK = {
    "entries": {
        "0": {
            "uid": 0,
            "key": ["Eldoria", "the capital"],
            "keysecondary": ["throne"],
            "comment": "Capital City",
            "content": "Eldoria is the shining capital of the realm.",
            "constant": False,
            "disable": False,
            "sticky": 3,
            "order": 100,
        },
        "1": {
            "uid": 1,
            "key": ["magic"],
            "comment": "Magic System",
            "content": "Magic is drawn from ley lines and fades at sea.",
            "constant": True,
            "disable": False,
            "position": 4,
            "depth": 2,
            "order": 50,
        },
        "2": {
            "uid": 2,
            "key": ["forgotten"],
            "comment": "Disabled entry",
            "content": "This entry was disabled in ST.",
            "constant": False,
            "disable": True,
            "order": 200,
        },
        "3": {
            "uid": 3,
            "key": ["empty"],
            "comment": "No content",
            "content": "",
        },
    }
}

CHARACTER_BOOK = {
    "name": "Vale Companion Book",
    "entries": [
        {
            "keys": ["Vale", "ranger"],
            "secondary_keys": ["bow"],
            "name": "Vale the Ranger",
            "content": "Vale is a ranger who guards the northern woods.",
            "enabled": True,
            "constant": False,
            "extensions": {"sticky": 2, "position": 4, "depth": 1},
            "insertion_order": 10,
        },
        {
            "keys": ["oath"],
            "comment": "The Oath",
            "content": "Rangers swear an oath never to abandon a traveler.",
            "enabled": False,
            "constant": True,
            "insertion_order": 5,
        },
    ],
}


# ── parser ───────────────────────────────────────────────────────────────────

def test_parse_v2_lorebook():
    parsed = parse_sillytavern_lorebook(V2_LOREBOOK, fallback_name="My Book")
    assert parsed["name"] == "My Book"
    assert parsed["stats"] == {"total": 4, "imported": 3, "skipped": 1}

    entries = {e["uid"]: e for e in parsed["entries"]}
    assert set(entries) == {"0", "1", "2"}

    capital = entries["0"]
    assert capital["title"] == "Capital City"
    assert capital["keys"] == ["Eldoria", "the capital"]
    assert capital["secondary_keys"] == ["throne"]
    assert capital["enabled"] is True
    assert capital["constant"] is False
    assert capital["sticky_turns"] == 3  # ST 'sticky' preserved
    assert capital["raw"]["comment"] == "Capital City"

    assert entries["1"]["constant"] is True
    assert entries["1"]["sticky_turns"] is None  # no sticky → inherit book default
    assert entries["1"]["injection_depth"] == 2  # ST position 4 ('@ depth')
    assert capital["injection_depth"] is None  # no position → normal placement
    assert entries["2"]["enabled"] is False  # disable: true → preserved but flagged

    # Sorted by ST order (magic=50 before capital=100 before disabled=200).
    assert [e["uid"] for e in parsed["entries"]] == ["1", "0", "2"]


def test_parse_character_book_list_shape():
    parsed = parse_sillytavern_lorebook(CHARACTER_BOOK)
    assert parsed["name"] == "Vale Companion Book"
    assert parsed["stats"]["imported"] == 2

    entries = {e["title"]: e for e in parsed["entries"]}
    vale = entries["Vale the Ranger"]
    assert vale["keys"] == ["Vale", "ranger"]
    assert vale["secondary_keys"] == ["bow"]
    assert vale["sticky_turns"] == 2  # character books carry sticky under extensions
    assert vale["injection_depth"] == 1  # extensions position 4 / depth 1
    oath = entries["The Oath"]
    assert oath["enabled"] is False
    assert oath["constant"] is True
    assert oath["sticky_turns"] is None
    assert oath["injection_depth"] is None
    # insertion_order respected: oath (5) before vale (10)
    assert parsed["entries"][0]["title"] == "The Oath"


def test_parse_character_card_wrapper():
    card = {"name": "Vale", "data": {"character_book": CHARACTER_BOOK}}
    parsed = parse_sillytavern_lorebook(card)
    assert parsed["stats"]["imported"] == 2


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_sillytavern_lorebook({"not": "a lorebook"})
    with pytest.raises(ValueError):
        parse_sillytavern_lorebook({"entries": {"0": {"content": ""}}})


# ── store ────────────────────────────────────────────────────────────────────

def test_store_import_list_load_delete(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    result = store.import_lorebook(V2_LOREBOOK, name="Realm Lore")
    book_id = result["lorebook"]["id"]
    assert book_id == "realm_lore"
    assert result["stats"]["imported"] == 3

    listed = store.list_lorebooks()
    assert len(listed) == 1
    assert listed[0]["entry_count"] == 3
    assert listed[0]["enabled_count"] == 2
    assert listed[0]["constant_count"] == 1

    record = store.load_lorebook(book_id)
    assert record["name"] == "Realm Lore"

    store.set_links("scenario", "ambush", [book_id])
    store.delete_lorebook(book_id)
    assert store.list_lorebooks() == []
    assert store.get_links("scenario", "ambush") == []  # delete prunes links


def test_store_toggle_bumps_updated_at_and_fingerprint(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    book_id = store.import_lorebook(V2_LOREBOOK, name="Realm Lore")["lorebook"]["id"]

    before = store.load_lorebook(book_id)["updated_at"]
    fp_before = store.embed_fingerprint([book_id])

    record = store.set_entry_enabled(book_id, "0", False)
    assert record["updated_at"] >= before
    entry = next(e for e in record["entries"] if e["uid"] == "0")
    assert entry["enabled"] is False
    assert store.embed_fingerprint([book_id]) != fp_before or record["updated_at"] != before

    fingerprint = store.embed_fingerprint([book_id])
    assert fingerprint == store.embed_fingerprint([book_id])  # stable
    assert store.embed_fingerprint([]) != fingerprint


def test_store_update_entry_patches_fields(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    book_id = store.import_lorebook(V2_LOREBOOK, name="Realm Lore")["lorebook"]["id"]
    before = store.load_lorebook(book_id)["updated_at"]

    record = store.update_entry(book_id, "0", {
        "title": "Grand Capital",
        "keys": ["Eldoria", "capital city"],
        "content": "Eldoria gleams atop the white cliffs.",
        "constant": True,
    })
    entry = next(e for e in record["entries"] if e["uid"] == "0")
    assert entry["title"] == "Grand Capital"
    assert entry["keys"] == ["Eldoria", "capital city"]
    assert entry["content"] == "Eldoria gleams atop the white cliffs."
    assert entry["constant"] is True
    assert entry["enabled"] is True  # untouched fields preserved
    assert record["updated_at"] >= before

    # Comma-separated key strings are accepted like hand-edited files.
    record = store.update_entry(book_id, "0", {"keys": "one, two"})
    entry = next(e for e in record["entries"] if e["uid"] == "0")
    assert entry["keys"] == ["one", "two"]

    with pytest.raises(FileNotFoundError):
        store.update_entry(book_id, "nope", {"title": "x"})
    with pytest.raises(ValueError):
        store.update_entry(book_id, "0", {"content": "   "})


def test_book_sticky_setting_and_entry_override(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    book_id = store.import_lorebook(V2_LOREBOOK, name="Realm Lore")["lorebook"]["id"]
    record = store.load_lorebook(book_id)
    assert record["sticky_turns"] == 0  # book default: sticky off
    assert store.list_lorebooks()[0]["sticky_turns"] == 0

    before = record["updated_at"]
    fp_before = store.embed_fingerprint([book_id])
    record = store.update_lorebook(book_id, {"sticky_turns": 2})
    assert record["sticky_turns"] == 2
    # The bump invalidates linked saves' embed fingerprints so the new value
    # reaches their world indexes on the next sync.
    assert store.embed_fingerprint([book_id]) != fp_before or record["updated_at"] != before

    # Per-entry override: set and clear (None = inherit the book value again).
    record = store.update_entry(book_id, "1", {"sticky_turns": 5})
    assert next(e for e in record["entries"] if e["uid"] == "1")["sticky_turns"] == 5
    record = store.update_entry(book_id, "1", {"sticky_turns": None})
    assert next(e for e in record["entries"] if e["uid"] == "1")["sticky_turns"] is None

    # Negative values clamp to zero.
    assert store.update_lorebook(book_id, {"sticky_turns": -3})["sticky_turns"] == 0
    record = store.update_entry(book_id, "1", {"sticky_turns": -1})
    assert next(e for e in record["entries"] if e["uid"] == "1")["sticky_turns"] == 0

    with pytest.raises(FileNotFoundError):
        store.update_lorebook("missing_book", {"sticky_turns": 1})


def test_entry_injection_depth_update(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    book_id = store.import_lorebook(V2_LOREBOOK, name="Realm Lore")["lorebook"]["id"]

    record = store.update_entry(book_id, "0", {"injection_depth": 3})
    assert next(e for e in record["entries"] if e["uid"] == "0")["injection_depth"] == 3
    record = store.update_entry(book_id, "0", {"injection_depth": -2})  # clamps
    assert next(e for e in record["entries"] if e["uid"] == "0")["injection_depth"] == 0
    record = store.update_entry(book_id, "0", {"injection_depth": None})  # clears
    assert next(e for e in record["entries"] if e["uid"] == "0")["injection_depth"] is None


def test_store_links_roundtrip(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    a = store.import_lorebook(V2_LOREBOOK, name="Book A")["lorebook"]["id"]
    b = store.import_lorebook(CHARACTER_BOOK, name="Book B")["lorebook"]["id"]

    store.set_links("scenario", "ambush", [a, b, "missing_book"])
    assert store.get_links("scenario", "ambush") == [a, b]  # missing ids dropped
    assert store.get_reverse_links(a) == ["scenario:ambush"]

    store.set_links("scenario", "ambush", [])
    assert store.get_links("scenario", "ambush") == []

    assert store.resolve_save_lorebooks([a, "missing_book"])[0]["id"] == a
    assert len(store.resolve_save_lorebooks([a, "missing_book"])) == 1


# ── free-standing story entries ──────────────────────────────────────────────

def test_make_and_patch_story_entry():
    entry = make_story_entry({
        "title": "The Pact",
        "keys": "pact, oath",  # comma strings accepted like hand-edited files
        "content": "The rivers obey whoever holds the pact stone.",
        "constant": True,
    })
    assert entry["uid"]
    assert entry["keys"] == ["pact", "oath"]
    assert entry["secondary_keys"] == []
    assert entry["enabled"] is True
    assert entry["constant"] is True

    patched = patch_story_entry(entry, {"enabled": False, "keys": ["pact"]})
    assert patched["uid"] == entry["uid"]  # uid survives edits
    assert patched["enabled"] is False
    assert patched["keys"] == ["pact"]
    assert patched["content"] == entry["content"]  # untouched fields preserved

    with pytest.raises(ValueError):
        make_story_entry({"content": "   "})
    with pytest.raises(ValueError):
        patch_story_entry(entry, {"content": ""})

    book = story_entries_book([entry])
    assert book["id"] == STORY_LOREBOOK_ID
    assert book["entries"] == [entry]


def test_fingerprint_includes_story_entries(tmp_path):
    store = LorebookStore(str(tmp_path / "data"))
    book_id = store.import_lorebook(V2_LOREBOOK, name="Realm Lore")["lorebook"]["id"]
    entry = make_story_entry({"content": "Lantern light keeps the mists at bay."},
                             uid="e1")

    # Back-compat: no story entries → the historical fingerprint, so existing
    # saves don't re-embed on their next load.
    base = store.embed_fingerprint([book_id])
    assert base == store.embed_fingerprint([book_id], [])

    with_entry = store.embed_fingerprint([book_id], [entry])
    assert with_entry != base
    assert with_entry == store.embed_fingerprint([book_id], [dict(entry)])  # stable
    # Any edit — including an enabled toggle — changes the fingerprint.
    assert store.embed_fingerprint(
        [book_id], [patch_story_entry(entry, {"enabled": False})]) != with_entry
    # Story entries alone (no imported books) also fingerprint.
    assert store.embed_fingerprint([], [entry]) != store.embed_fingerprint([])


# ── API + save inheritance ───────────────────────────────────────────────────

def make_client(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"))
    session_manager.create_save("autosave")
    monkeypatch.setattr(server, "session_manager", session_manager)
    server.engine.set_memory_path(session_manager.get_memory_path())
    server.engine.llm.mode = "mock"
    lorebook_store = LorebookStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "lorebook_store", lorebook_store)
    scenario_store = ScenarioStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "scenario_store", scenario_store)
    return TestClient(server.app), session_manager, lorebook_store, scenario_store


def _world_db_lorebook_rows(tmp_path, save_id):
    db = tmp_path / "data" / "saves" / save_id / "world_index" / "world.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT source_id, constant, text FROM world_entries WHERE source_type = 'lorebook' ORDER BY source_id"
        ).fetchall()
    finally:
        conn.close()


def test_lorebook_api_import_and_crud(tmp_path, monkeypatch):
    client, _, _, _ = make_client(tmp_path, monkeypatch)

    resp = client.post("/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"]["imported"] == 3
    book_id = body["lorebook"]["id"]

    assert client.post("/api/lorebooks/import", json={"data": {"nope": 1}}).status_code == 400

    listed = client.get("/api/lorebooks").json()["lorebooks"]
    assert [b["id"] for b in listed] == [book_id]

    detail = client.get(f"/api/lorebooks/{book_id}").json()
    assert detail["lorebook"]["name"] == "Realm Lore"
    assert detail["links"] == []

    toggled = client.put(f"/api/lorebooks/{book_id}/entries/0", json={"enabled": False})
    assert toggled.status_code == 200
    entry = next(e for e in toggled.json()["lorebook"]["entries"] if e["uid"] == "0")
    assert entry["enabled"] is False

    assert client.put(f"/api/lorebooks/{book_id}/entries/nope", json={"enabled": True}).status_code == 404

    links = client.put(f"/api/lorebooks/links/scenario/ambush", json={"lorebook_ids": [book_id]})
    assert links.json()["lorebook_ids"] == [book_id]
    assert client.get(f"/api/lorebooks/links/scenario/ambush").json()["lorebook_ids"] == [book_id]
    assert client.get(f"/api/lorebooks/links/bogus/ambush").status_code == 400

    assert client.delete(f"/api/lorebooks/{book_id}").status_code == 200
    assert client.get("/api/lorebooks").json()["lorebooks"] == []


def test_save_inherits_scenario_lorebooks_and_embeds(tmp_path, monkeypatch):
    client, session_manager, lorebook_store, scenario_store = make_client(tmp_path, monkeypatch)

    scenario = scenario_store.save_scenario({
        "name": "Ambush",
        "scenario_description": "Bandits stalk the mountain road.",
        "starting_prompt": "The wagon wheel snaps at dusk.",
    })
    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    client.put(f"/api/lorebooks/links/scenario/{scenario['id']}", json={"lorebook_ids": [book_id]})

    resp = client.post("/api/saves", json={"save_id": "lore_save", "scenario_id": scenario["id"]})
    assert resp.status_code == 200

    meta = session_manager.save_manager.read_core_json("lore_save", "metadata.json", {})
    assert meta["lorebook_ids"] == [book_id]
    fingerprint = meta["lorebook_embed_fingerprint"]
    assert fingerprint == lorebook_store.embed_fingerprint([book_id])

    rows = _world_db_lorebook_rows(tmp_path, "lore_save")
    # 2 enabled entries (uid 0 + constant uid 1); disabled uid 2 skipped.
    assert len(rows) == 2
    by_source = {r[0]: r for r in rows}
    assert by_source[f"{book_id}:1"][1] == 1  # constant flag stored
    assert "keywords: Eldoria, the capital, throne" in by_source[f"{book_id}:0"][2]

    # Reload with unchanged links: fingerprint short-circuits, rows unchanged.
    assert client.post("/api/saves/lore_save/load").status_code == 200
    assert _world_db_lorebook_rows(tmp_path, "lore_save") == rows

    # Toggle an entry in the library → reload re-embeds (row set changes).
    client.put(f"/api/lorebooks/{book_id}/entries/0", json={"enabled": False})
    assert client.post("/api/saves/lore_save/load").status_code == 200
    rows_after = _world_db_lorebook_rows(tmp_path, "lore_save")
    assert [r[0] for r in rows_after] == [f"{book_id}:1"]


def test_lorebook_entry_edit_resyncs_active_save(tmp_path, monkeypatch):
    client, _, _, _ = make_client(tmp_path, monkeypatch)

    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})

    # Content edit through the entry PUT re-embeds the active save's rows.
    resp = client.put(f"/api/lorebooks/{book_id}/entries/0",
                      json={"content": "Eldoria fell to the bandit kings."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced"] is True
    entry = next(e for e in body["lorebook"]["entries"] if e["uid"] == "0")
    assert entry["content"] == "Eldoria fell to the bandit kings."

    rows = _world_db_lorebook_rows(tmp_path, "autosave")
    by_source = {r[0]: r for r in rows}
    assert "Eldoria fell to the bandit kings." in by_source[f"{book_id}:0"][2]

    # Empty content is rejected; enabled-only payloads still work (back-compat).
    assert client.put(f"/api/lorebooks/{book_id}/entries/0",
                      json={"content": "  "}).status_code == 400
    toggled = client.put(f"/api/lorebooks/{book_id}/entries/0", json={"enabled": False})
    assert toggled.status_code == 200
    assert [r[0] for r in _world_db_lorebook_rows(tmp_path, "autosave")] == [f"{book_id}:1"]

    # Books not attached to the active save don't trigger a sync.
    other_id = client.post(
        "/api/lorebooks/import", json={"data": CHARACTER_BOOK, "name": "Vale Book"}
    ).json()["lorebook"]["id"]
    resp = client.put(f"/api/lorebooks/{other_id}/entries/0", json={"enabled": False})
    assert resp.json()["synced"] is False


def test_attach_detach_lorebooks_on_save(tmp_path, monkeypatch):
    client, session_manager, _, _ = make_client(tmp_path, monkeypatch)

    book_id = client.post(
        "/api/lorebooks/import", json={"data": CHARACTER_BOOK, "name": "Vale Book"}
    ).json()["lorebook"]["id"]

    assert client.get("/api/saves/autosave/lorebooks").json()["lorebook_ids"] == []
    assert client.get("/api/saves/nope/lorebooks").status_code == 404

    resp = client.put("/api/saves/autosave/lorebooks",
                      json={"lorebook_ids": [book_id, "missing_book"]})
    assert resp.json()["lorebook_ids"] == [book_id]

    rows = _world_db_lorebook_rows(tmp_path, "autosave")
    assert [r[0] for r in rows] == [f"{book_id}:0"]  # only the enabled entry

    # Detach: rows are removed on the follow-up sync.
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": []})
    assert _world_db_lorebook_rows(tmp_path, "autosave") == []


def test_story_entries_api_add_toggle_edit_remove(tmp_path, monkeypatch):
    client, session_manager, _, _ = make_client(tmp_path, monkeypatch)

    # A story entry needs no imported lorebook at all.
    resp = client.post("/api/saves/autosave/lorebooks/entries", json={
        "title": "The Pact",
        "keys": ["pact", "oath"],
        "content": "The rivers obey whoever holds the pact stone.",
        "constant": True,
    })
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    uid = entry["uid"]
    assert entry["enabled"] is True

    rows = _world_db_lorebook_rows(tmp_path, "autosave")
    assert [r[0] for r in rows] == [f"{STORY_LOREBOOK_ID}:{uid}"]
    assert rows[0][1] == 1  # constant flag carried into the world index
    assert "keywords: pact, oath" in rows[0][2]

    # Persisted on the save and surfaced by the GET.
    meta = session_manager.save_manager.read_core_json("autosave", "metadata.json", {})
    assert meta["story_lorebook_entries"] == [entry]
    got = client.get("/api/saves/autosave/lorebooks").json()
    assert got["story_entries"] == [entry]

    # Toggle off: the state persists and the row is un-embedded.
    resp = client.put(f"/api/saves/autosave/lorebooks/entries/{uid}",
                      json={"enabled": False})
    assert resp.json()["entry"]["enabled"] is False
    assert _world_db_lorebook_rows(tmp_path, "autosave") == []
    meta = session_manager.save_manager.read_core_json("autosave", "metadata.json", {})
    assert meta["story_lorebook_entries"][0]["enabled"] is False

    # Toggle back on, then edit: re-embedded with the new text.
    client.put(f"/api/saves/autosave/lorebooks/entries/{uid}", json={"enabled": True})
    resp = client.put(f"/api/saves/autosave/lorebooks/entries/{uid}",
                      json={"content": "The pact stone shattered at dawn."})
    assert resp.status_code == 200
    rows = _world_db_lorebook_rows(tmp_path, "autosave")
    assert "The pact stone shattered at dawn." in rows[0][2]

    # Story entries coexist with attached lorebooks.
    book_id = client.post(
        "/api/lorebooks/import", json={"data": CHARACTER_BOOK, "name": "Vale Book"}
    ).json()["lorebook"]["id"]
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})
    assert {r[0] for r in _world_db_lorebook_rows(tmp_path, "autosave")} == {
        f"{STORY_LOREBOOK_ID}:{uid}", f"{book_id}:0"}

    # Remove: the entry and its row are gone; the attached book is untouched.
    resp = client.delete(f"/api/saves/autosave/lorebooks/entries/{uid}")
    assert resp.json() == {"story_entries": [], "deleted": True}
    assert [r[0] for r in _world_db_lorebook_rows(tmp_path, "autosave")] == [f"{book_id}:0"]

    # Validation: empty content, unknown uid, unknown save.
    assert client.post("/api/saves/autosave/lorebooks/entries",
                       json={"content": "  "}).status_code == 400
    assert client.put(f"/api/saves/autosave/lorebooks/entries/{uid}",
                      json={"enabled": True}).status_code == 404
    assert client.delete(f"/api/saves/autosave/lorebooks/entries/{uid}").status_code == 404
    assert client.post("/api/saves/nope/lorebooks/entries",
                       json={"content": "x"}).status_code == 404


def test_story_entries_persist_across_reload(tmp_path, monkeypatch):
    client, session_manager, lorebook_store, _ = make_client(tmp_path, monkeypatch)

    uid = client.post("/api/saves/autosave/lorebooks/entries", json={
        "content": "Lantern light keeps the mists at bay.",
    }).json()["entry"]["uid"]
    rows = _world_db_lorebook_rows(tmp_path, "autosave")
    assert [r[0] for r in rows] == [f"{STORY_LOREBOOK_ID}:{uid}"]

    meta = session_manager.save_manager.read_core_json("autosave", "metadata.json", {})
    assert meta["lorebook_embed_fingerprint"] == lorebook_store.embed_fingerprint(
        [], meta["story_lorebook_entries"])

    # Reload with unchanged entries: the fingerprint short-circuits the sync.
    assert client.post("/api/saves/autosave/load").status_code == 200
    assert _world_db_lorebook_rows(tmp_path, "autosave") == rows


# ── sticky turns (ST 'sticky': stay active N turns after triggered) ─────────

def _sticky_db_rows(tmp_path, save_id):
    db = tmp_path / "data" / "saves" / save_id / "world_index" / "world.db"
    conn = sqlite3.connect(str(db))
    try:
        return dict(conn.execute(
            "SELECT source_id, COALESCE(sticky_turns, 0) FROM world_entries WHERE source_type = 'lorebook'"
        ).fetchall())
    finally:
        conn.close()


def test_sticky_api_book_setting_entry_override_and_embed(tmp_path, monkeypatch):
    client, _, _, _ = make_client(tmp_path, monkeypatch)
    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})

    # Book-level setting: the PUT re-embeds the active save with the new default.
    resp = client.put(f"/api/lorebooks/{book_id}", json={"sticky_turns": 2})
    assert resp.status_code == 200
    assert resp.json()["lorebook"]["sticky_turns"] == 2
    assert resp.json()["synced"] is True

    # uid 0 keeps its imported ST sticky (3); uid 1 inherits the book default.
    assert _sticky_db_rows(tmp_path, "autosave") == {
        f"{book_id}:0": 3, f"{book_id}:1": 2}

    # Per-entry override via the entry PUT; an explicit null clears it back to
    # the book default.
    client.put(f"/api/lorebooks/{book_id}/entries/0", json={"sticky_turns": 7})
    assert _sticky_db_rows(tmp_path, "autosave")[f"{book_id}:0"] == 7
    client.put(f"/api/lorebooks/{book_id}/entries/0", json={"sticky_turns": None})
    assert _sticky_db_rows(tmp_path, "autosave")[f"{book_id}:0"] == 2

    assert client.put("/api/lorebooks/missing_book",
                      json={"sticky_turns": 1}).status_code == 404

    # Story entries carry their own sticky value (no book to inherit from).
    entry = client.post("/api/saves/autosave/lorebooks/entries", json={
        "content": "The pact stone hums.", "sticky_turns": 4}).json()["entry"]
    assert entry["sticky_turns"] == 4
    assert _sticky_db_rows(tmp_path, "autosave")[f"{STORY_LOREBOOK_ID}:{entry['uid']}"] == 4


def test_sticky_entries_stay_in_context_for_configured_turns(tmp_path, monkeypatch):
    client, session_manager, _, _ = make_client(tmp_path, monkeypatch)
    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    # uid 0 was imported with ST sticky 3 (see the fixture); it is the only
    # non-constant embedded entry, so retrieval always surfaces it.
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})

    def gather(turn, input_text, sticky_state):
        state = dict(session_manager.state)
        state.update({"input_text": input_text, "turn": turn,
                      "sticky_world_entries": sticky_state})
        return asyncio.run(server.engine.gather_context_node(state))

    # Turn 1: retrieval triggers the entry and opens its 3-turn window.
    result = gather(1, "Tell me about the capital", {})
    assert result["sticky_world_entries"] == {f"{book_id}:0": 4}  # 1 + 3
    world_blocks = [b for b in result["current_context"] if "<world_knowledge>" in b]
    assert len(world_blocks) == 1 and "Eldoria" in world_blocks[0]

    # Through turn 4 the entry is forced into context without retrieval —
    # even on an empty (continue) input that skips RAG entirely.
    result = gather(4, "", {f"{book_id}:0": 4})
    world_blocks = [b for b in result["current_context"] if "<world_knowledge>" in b]
    assert len(world_blocks) == 1 and "Eldoria" in world_blocks[0]
    assert len(result["last_retrieved_world_ids"]) == 1  # surfaced as active in the UI
    assert result["sticky_world_entries"] == {f"{book_id}:0": 4}

    # Turn 5: the window is over — nothing forced, record dropped.
    result = gather(5, "", {f"{book_id}:0": 4})
    assert not [b for b in result["current_context"] if "<world_knowledge>" in b]
    assert result["sticky_world_entries"] == {}


def test_sticky_state_persists_in_save_metadata_and_api(tmp_path, monkeypatch):
    client, session_manager, _, _ = make_client(tmp_path, monkeypatch)
    sm = session_manager
    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})

    sm.state["sticky_world_entries"] = {f"{book_id}:0": 4}
    sm.save_manager.save_turn("autosave", sm.state, 1)
    meta = sm.save_manager.read_core_json("autosave", "metadata.json", {})
    assert meta["sticky_world_entries"] == {f"{book_id}:0": 4}

    # Reload restores the sticky window into session state.
    assert client.post("/api/saves/autosave/load").status_code == 200
    assert sm.state["sticky_world_entries"] == {f"{book_id}:0": 4}

    # The world-entries endpoint exposes it (plus the turn) for the Active tab.
    resp = client.get("/api/session/world-entries").json()
    assert resp["sticky_source_ids"] == {f"{book_id}:0": 4}
    assert resp["turn"] == 1


# ── injection depth (ST '@ depth': inject into the chat, not the lore block) ─

def _depth_db_rows(tmp_path, save_id):
    db = tmp_path / "data" / "saves" / save_id / "world_index" / "world.db"
    conn = sqlite3.connect(str(db))
    try:
        return dict(conn.execute(
            "SELECT source_id, injection_depth FROM world_entries WHERE source_type = 'lorebook'"
        ).fetchall())
    finally:
        conn.close()


def test_injection_depth_routes_active_entries_into_chat(tmp_path, monkeypatch):
    client, session_manager, _, _ = make_client(tmp_path, monkeypatch)
    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})

    # Embed carries the imported ST depth (uid 1: position 4 / depth 2).
    assert _depth_db_rows(tmp_path, "autosave") == {
        f"{book_id}:0": None, f"{book_id}:1": 2}

    state = dict(session_manager.state)
    state.update({
        "input_text": "Tell me about the capital",
        "turn": 1,
        "chat_messages": [
            {"role": "user", "content": "one"},
            {"role": "ai", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "ai", "content": "four"},
        ],
    })
    result = asyncio.run(server.engine.gather_context_node(state))

    # The constant '@ depth' entry leaves the <lorebook> block for a chat
    # injection; the normal entry still lands in <world_knowledge>.
    assert not any("<lorebook>" in b for b in result["current_context"])
    assert any("<world_knowledge>" in b and "Eldoria" in b
               for b in result["current_context"])
    injections = result["lore_depth_injections"]
    assert len(injections) == 1
    assert injections[0]["depth"] == 2
    assert "ley lines" in injections[0]["text"]

    # The compiler inserts it 2 messages from the chat bottom, above the
    # player's input.
    compiled = server.engine.prompt_compiler.compile({**state, **result})
    messages = compiled["messages"]
    idx = next(i for i, m in enumerate(messages)
               if m["role"] == "system" and "ley lines" in m["content"])
    assert idx == len(messages) - 3
    assert messages[-1]["content"] == "Tell me about the capital"

    # Per-entry PUT sets and (with an explicit null) clears the depth.
    client.put(f"/api/lorebooks/{book_id}/entries/0", json={"injection_depth": 1})
    assert _depth_db_rows(tmp_path, "autosave")[f"{book_id}:0"] == 1
    client.put(f"/api/lorebooks/{book_id}/entries/0", json={"injection_depth": None})
    assert _depth_db_rows(tmp_path, "autosave")[f"{book_id}:0"] is None

    # Story entries support it too (depth 0 = the very bottom of the chat).
    entry = client.post("/api/saves/autosave/lorebooks/entries", json={
        "content": "The pact stone hums.", "injection_depth": 0}).json()["entry"]
    assert entry["injection_depth"] == 0
    assert _depth_db_rows(tmp_path, "autosave")[f"{STORY_LOREBOOK_ID}:{entry['uid']}"] == 0
    # And an explicit null on the story PUT reverts to normal placement.
    updated = client.put(f"/api/saves/autosave/lorebooks/entries/{entry['uid']}",
                         json={"injection_depth": None}).json()["entry"]
    assert updated["injection_depth"] is None
    assert _depth_db_rows(tmp_path, "autosave")[f"{STORY_LOREBOOK_ID}:{entry['uid']}"] is None


def test_undo_preserves_lorebook_links_and_keeps_toggles_effective(tmp_path, monkeypatch):
    # Regression: undo_turn used to overwrite metadata with just {"turn": n},
    # dropping lorebook_ids/fingerprint. The embedded rows survived in the world
    # index but toggles no longer re-synced, so disabled entries kept surfacing
    # in RAG. Undo must preserve the metadata the way restore_turn_snapshot does.
    client, session_manager, _, _ = make_client(tmp_path, monkeypatch)
    sm = session_manager

    book_id = client.post(
        "/api/lorebooks/import", json={"data": V2_LOREBOOK, "name": "Realm Lore"}
    ).json()["lorebook"]["id"]
    client.put("/api/saves/autosave/lorebooks", json={"lorebook_ids": [book_id]})
    assert [r[0] for r in _world_db_lorebook_rows(tmp_path, "autosave")] == [
        f"{book_id}:0", f"{book_id}:1"]

    # Play two turns so a snapshot exists to undo to.
    sm.state["turn"] = 1
    sm.save_manager.save_turn("autosave", sm.state, 1)
    sm.state["turn"] = 2
    sm.save_manager.save_turn("autosave", sm.state, 2)

    sm.save_manager.undo_turn("autosave", 1)

    meta = sm.save_manager.read_core_json("autosave", "metadata.json", {})
    assert meta["lorebook_ids"] == [book_id]
    assert "lorebook_embed_fingerprint" in meta

    # Toggling an entry off still re-embeds the active save (was a no-op before).
    resp = client.put(f"/api/lorebooks/{book_id}/entries/0", json={"enabled": False})
    assert resp.json()["synced"] is True
    assert [r[0] for r in _world_db_lorebook_rows(tmp_path, "autosave")] == [f"{book_id}:1"]
