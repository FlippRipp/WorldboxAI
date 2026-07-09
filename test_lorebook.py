import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.lorebook import LorebookStore, parse_sillytavern_lorebook
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
            "order": 100,
        },
        "1": {
            "uid": 1,
            "key": ["magic"],
            "comment": "Magic System",
            "content": "Magic is drawn from ley lines and fades at sea.",
            "constant": True,
            "disable": False,
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
    assert capital["raw"]["comment"] == "Capital City"

    assert entries["1"]["constant"] is True
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
    oath = entries["The Oath"]
    assert oath["enabled"] is False
    assert oath["constant"] is True
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
