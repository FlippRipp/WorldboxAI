"""Tests for the NPC System character-browser commands (/npc update, /npc edit)."""
import asyncio
import importlib.util
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_npc_system" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_npc_system_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_sdk(reply: str, captured: dict):
    async def generate(prompt, model_preference="balanced", max_tokens=None):
        captured["prompt"] = prompt
        return reply

    async def remember(entity_id, text, turn, importance=5, permanent=False, tags=None):
        captured.setdefault("memories", []).append({
            "id": entity_id, "text": text, "turn": turn,
            "permanent": permanent, "tags": list(tags or []),
        })

    async def forget(entity_id, tags=None):
        captured.setdefault("forgotten", []).append({"id": entity_id, "tags": list(tags or [])})
        return 1

    return SimpleNamespace(
        llm=SimpleNamespace(generate=generate, _current_module=""),
        memory=SimpleNamespace(remember=remember, forget=forget),
    )


def _npc(introduced=True):
    return {
        "id": "npc_aaaa1111",
        "name": "Serah",
        "race": "elf",
        "gender": "female",
        "appearance": "Silver-haired, sharp-eyed.",
        "archetype": "wandering scholar",
        "pitch": "A scholar chasing forbidden texts.",
        "personality": ["curious", "guarded", "dry-witted"],
        "role": "informant",
        "encounter_type": "location_bound",
        "introduced": introduced,
        "met_turn": 4 if introduced else None,
        "status": "active" if introduced else "unintroduced",
        "notes": "Met at the archive.",
        "created_turn": 2,
        "source": "generated",
        "relationships": [],
        "traveling_with_player": False,
    }


def _state(history=None, npc=None):
    npc = npc or _npc()
    return {
        "turn": 9,
        "history": history if history is not None else ["Serah takes an arrow to the shoulder defending the archive."],
        "module_configs": {},
        "module_data": {"wb_npc_system": {"characters": {npc["id"]: npc}}},
    }


def _edit_cmd(npc_id, payload: dict) -> list[str]:
    return ["edit", npc_id, urllib.parse.quote(json.dumps(payload))]


# --------------------------------------------------------------------------
# /npc edit
# --------------------------------------------------------------------------

def test_edit_applies_whitelisted_fields_and_logs_change():
    backend = _load_backend()
    state = _state()
    args = _edit_cmd("npc_aaaa1111", {
        "name": "Serah Veil",
        "notes": "Met at the archive. Now owes the player a favor.",
        "introduced": False,          # not editable — must be dropped
        "met_turn": 999,              # not editable — must be dropped
        "role": "ally",
    })

    result = asyncio.run(backend.on_command_npc(args, state, _make_sdk("{}", {})))

    npc = result["module_data"]["wb_npc_system"]["characters"]["npc_aaaa1111"]
    assert npc["name"] == "Serah Veil"
    assert npc["role"] == "ally"
    assert npc["notes"].endswith("owes the player a favor.")
    assert npc["introduced"] is True
    assert npc["met_turn"] == 4
    log = npc["change_log"]
    assert log[-1]["source"] == "manual"
    assert set(log[-1]["fields"]) == {"name", "notes", "role"}
    assert "Serah Veil" in result["message"]


def test_edit_survives_url_encoding_of_spaces_and_quotes():
    backend = _load_backend()
    state = _state()
    text = 'She said "call me Veil" — twice.'
    # The dispatcher splits on whitespace; the encoded payload must be one token.
    encoded = urllib.parse.quote(json.dumps({"pitch": text}))
    assert " " not in encoded

    result = asyncio.run(backend.on_command_npc(["edit", "npc_aaaa1111", encoded], state, _make_sdk("{}", {})))

    npc = result["module_data"]["wb_npc_system"]["characters"]["npc_aaaa1111"]
    assert npc["pitch"] == text


def test_edit_coerces_personality_string_to_list():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(
        _edit_cmd("npc_aaaa1111", {"personality": "bold, weary,  loyal "}),
        _state(), _make_sdk("{}", {}),
    ))

    npc = result["module_data"]["wb_npc_system"]["characters"]["npc_aaaa1111"]
    assert npc["personality"] == ["bold", "weary", "loyal"]


def test_edit_rejects_invalid_role_and_status():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(
        _edit_cmd("npc_aaaa1111", {"role": "demigod", "status": "ascended"}),
        _state(), _make_sdk("{}", {}),
    ))

    # Both values invalid → nothing to change, no writeback.
    assert "module_data" not in result
    assert "Nothing to change" in result["message"]


def test_edit_unknown_id_and_bad_payload_fail_gracefully():
    backend = _load_backend()
    sdk = _make_sdk("{}", {})

    unknown = asyncio.run(backend.on_command_npc(_edit_cmd("npc_nope", {"name": "X"}), _state(), sdk))
    assert "module_data" not in unknown
    assert "Unknown character" in unknown["message"]

    garbage = asyncio.run(backend.on_command_npc(["edit", "npc_aaaa1111", "not%7Bjson"], _state(), sdk))
    assert "module_data" not in garbage
    assert "Could not parse" in garbage["message"]


def test_edit_replaces_rag_profile_when_profile_fields_change():
    backend = _load_backend()
    captured = {}
    npc = _npc()
    npc["profile_embedded"] = True
    state = _state(npc=npc)

    result = asyncio.run(backend.on_command_npc(
        _edit_cmd("npc_aaaa1111", {"name": "Serah Veil", "appearance": "Scarred and grey-cloaked."}),
        state, _make_sdk("{}", captured),
    ))

    # Old profile removed from RAG, then the updated one embedded.
    assert captured["forgotten"] == [{"id": "npc_aaaa1111", "tags": ["profile"]}]
    profiles = [m for m in captured["memories"] if "profile" in m["tags"]]
    assert len(profiles) == 1
    assert profiles[0]["permanent"] is True
    assert "Serah Veil" in profiles[0]["text"]
    assert "grey-cloaked" in profiles[0]["text"]
    npc = result["module_data"]["wb_npc_system"]["characters"]["npc_aaaa1111"]
    assert npc["profile_embedded"] is True


def test_edit_of_non_profile_fields_leaves_rag_alone():
    backend = _load_backend()
    captured = {}
    npc = _npc()
    npc["profile_embedded"] = True

    asyncio.run(backend.on_command_npc(
        _edit_cmd("npc_aaaa1111", {"notes": "Owes the player a favor.", "status": "departed"}),
        _state(npc=npc), _make_sdk("{}", captured),
    ))

    assert "forgotten" not in captured
    assert not any("profile" in m["tags"] for m in captured.get("memories", []))


def test_edit_before_profile_embedded_does_not_touch_rag():
    # An unintroduced NPC has no profile in RAG yet; editing it must not embed
    # one early -- the (now current) profile is embedded at introduction.
    backend = _load_backend()
    captured = {}
    npc = _npc(introduced=False)

    asyncio.run(backend.on_command_npc(
        _edit_cmd("npc_aaaa1111", {"name": "Serah Veil"}),
        _state(npc=npc), _make_sdk("{}", captured),
    ))

    assert "forgotten" not in captured
    assert "memories" not in captured


def test_edit_respects_embed_profiles_toggle_off():
    backend = _load_backend()
    captured = {}
    npc = _npc()
    npc["profile_embedded"] = True
    state = _state(npc=npc)
    state["module_configs"] = {"wb_npc_system": {"embed_profiles": False}}

    asyncio.run(backend.on_command_npc(
        _edit_cmd("npc_aaaa1111", {"name": "Serah Veil"}),
        state, _make_sdk("{}", captured),
    ))

    assert "forgotten" not in captured
    assert "memories" not in captured


def test_usage_message_for_missing_args():
    backend = _load_backend()
    sdk = _make_sdk("{}", {})

    for args in ([], ["update"], ["edit", "npc_aaaa1111"], ["frobnicate"]):
        result = asyncio.run(backend.on_command_npc(args, _state(), sdk))
        assert "Usage" in result["message"]


# --------------------------------------------------------------------------
# /npc update
# --------------------------------------------------------------------------

def test_update_merges_changed_fields_and_remembers():
    backend = _load_backend()
    captured = {}
    reply = json.dumps({
        "appearance": "Silver-haired, sharp-eyed, her shoulder bandaged from an arrow wound.",
        "status": "active",  # unchanged → must be dropped
        "notes": "Met at the archive. Took an arrow defending it.",
        "change_note": "Serah was wounded defending the archive.",
    })
    sdk = _make_sdk(reply, captured)
    state = _state()

    result = asyncio.run(backend.on_command_npc(["update", "npc_aaaa1111"], state, sdk))

    # The record and the recent story both reach the prompt.
    assert "wandering scholar" in captured["prompt"]
    assert "arrow to the shoulder" in captured["prompt"]

    npc = result["module_data"]["wb_npc_system"]["characters"]["npc_aaaa1111"]
    assert "bandaged" in npc["appearance"]
    assert set(npc["change_log"][-1]["fields"]) == {"appearance", "notes"}
    assert npc["change_log"][-1]["source"] == "story"
    # The change note is embedded into RAG under the NPC's id.
    assert captured["memories"][0]["id"] == "npc_aaaa1111"
    assert "wounded" in captured["memories"][0]["text"]
    assert "Serah" in result["message"]


def test_update_replaces_rag_profile_when_profile_fields_change():
    backend = _load_backend()
    captured = {}
    npc = _npc()
    npc["profile_embedded"] = True
    reply = json.dumps({
        "appearance": "Silver-haired, her shoulder bandaged from an arrow wound.",
        "change_note": "Serah was wounded defending the archive.",
    })

    asyncio.run(backend.on_command_npc(
        ["update", "npc_aaaa1111"], _state(npc=npc), _make_sdk(reply, captured),
    ))

    assert captured["forgotten"] == [{"id": "npc_aaaa1111", "tags": ["profile"]}]
    profiles = [m for m in captured["memories"] if "profile" in m["tags"]]
    assert len(profiles) == 1
    assert "bandaged" in profiles[0]["text"]


def test_update_can_mark_a_character_deceased():
    backend = _load_backend()
    reply = json.dumps({"status": "deceased", "change_note": "Serah died in the fire."})
    result = asyncio.run(backend.on_command_npc(
        ["update", "npc_aaaa1111"], _state(["The archive burns; Serah does not escape."]), _make_sdk(reply, {}),
    ))

    npc = result["module_data"]["wb_npc_system"]["characters"]["npc_aaaa1111"]
    assert npc["status"] == "deceased"


def test_update_refuses_unintroduced_npcs():
    backend = _load_backend()
    npc = _npc(introduced=False)
    result = asyncio.run(backend.on_command_npc(
        ["update", npc["id"]], _state(npc=npc), _make_sdk("{}", {}),
    ))

    assert "module_data" not in result
    assert "not appeared" in result["message"]


def test_update_with_unusable_or_empty_llm_reply_is_a_noop():
    backend = _load_backend()

    garbage = asyncio.run(backend.on_command_npc(
        ["update", "npc_aaaa1111"], _state(), _make_sdk("I cannot help with that.", {}),
    ))
    assert "module_data" not in garbage
    assert "try again" in garbage["message"].lower()

    no_changes = asyncio.run(backend.on_command_npc(
        ["update", "npc_aaaa1111"], _state(), _make_sdk("{}", {}),
    ))
    assert "module_data" not in no_changes
    assert "No lasting changes" in no_changes["message"]


# --------------------------------------------------------------------------
# /npc add
# --------------------------------------------------------------------------

def _add_cmd(payload: dict) -> list[str]:
    return ["add", urllib.parse.quote(json.dumps(payload))]


def test_add_creates_introduced_character_and_embeds_profile():
    backend = _load_backend()
    captured = {}
    state = _state()  # already holds npc_aaaa1111

    result = asyncio.run(backend.on_command_npc(_add_cmd({
        "name": "Bram Holt",
        "race": "human",
        "gender": "male",
        "archetype": "dockside fixer",
        "role": "informant",
        "appearance": "Weathered, missing two fingers.",
        "pitch": "Knows who moves what through the harbor.",
        "personality": "shrewd, wry, cautious",
        "notes": "Owes nobody, trusts nobody.",
    }), state, _make_sdk("{}", captured)))

    # Delete-safe write-back so the new bank is authoritative.
    assert result["module_data_replace"] == ["characters"]
    chars = result["module_data"]["wb_npc_system"]["characters"]
    # The existing character survives and the new one is added alongside it.
    assert len(chars) == 2
    assert "npc_aaaa1111" in chars
    added = next(n for n in chars.values() if n["name"] == "Bram Holt")
    assert added["id"].startswith("npc_")
    assert added["introduced"] is True
    assert added["status"] == "active"
    assert added["source"] == "manual"
    assert added["met_turn"] == state["turn"]
    assert added["role"] == "informant"
    assert added["personality"] == ["shrewd", "wry", "cautious"]
    assert added["notes"] == "Owes nobody, trusts nobody."
    assert added["change_log"][-1]["source"] == "manual"
    # An introduced manual character gets its profile embedded into RAG.
    profiles = [m for m in captured["memories"] if "profile" in m["tags"]]
    assert len(profiles) == 1
    assert profiles[0]["id"] == added["id"]
    assert "Bram Holt" in profiles[0]["text"]
    assert "Added Bram Holt" in result["message"]


def test_add_unintroduced_character_stays_in_the_wings_without_rag():
    backend = _load_backend()
    captured = {}

    result = asyncio.run(backend.on_command_npc(_add_cmd({
        "name": "The Whisper", "role": "wildcard", "introduced": False,
    }), _state(), _make_sdk("{}", captured)))

    chars = result["module_data"]["wb_npc_system"]["characters"]
    added = next(n for n in chars.values() if n["name"] == "The Whisper")
    assert added["introduced"] is False
    assert added["status"] == "unintroduced"
    assert added["met_turn"] is None
    # No profile embedded until the character is actually introduced.
    assert "memories" not in captured


def test_add_requires_a_name():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(
        _add_cmd({"role": "ally", "archetype": "nameless"}), _state(), _make_sdk("{}", {}),
    ))
    assert "module_data" not in result
    assert "needs a name" in result["message"]


def test_add_with_unparseable_payload_fails_gracefully():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(
        ["add", "not%7Bjson"], _state(), _make_sdk("{}", {}),
    ))
    assert "module_data" not in result
    assert "Could not parse" in result["message"]


def test_add_respects_embed_profiles_toggle_off():
    backend = _load_backend()
    captured = {}
    state = _state()
    state["module_configs"] = {"wb_npc_system": {"embed_profiles": False}}

    asyncio.run(backend.on_command_npc(
        _add_cmd({"name": "Bram Holt"}), state, _make_sdk("{}", captured),
    ))
    assert "memories" not in captured


# --------------------------------------------------------------------------
# /npc generate
# --------------------------------------------------------------------------

_GEN_REPLY = json.dumps({"npc": {
    "name": "Mira Voss",
    "race": "human",
    "gender": "female",
    "appearance": "Cloaked, ink-stained fingers.",
    "archetype": "rogue cartographer",
    "pitch": "Maps the routes smugglers wish stayed secret.",
    "personality": ["restless", "clever", "secretive"],
    "role": "wildcard",
    "encounter_type": "encounter",
}})


def test_generate_creates_hidden_character():
    backend = _load_backend()
    captured = {}
    state = _state()  # already holds npc_aaaa1111

    result = asyncio.run(backend.on_command_npc(
        ["generate"], state, _make_sdk(_GEN_REPLY, captured),
    ))

    assert result["module_data_replace"] == ["characters"]
    chars = result["module_data"]["wb_npc_system"]["characters"]
    # The existing character survives and the generated one is added alongside it.
    assert len(chars) == 2
    assert "npc_aaaa1111" in chars
    gen = next(n for n in chars.values() if n["name"] == "Mira Voss")
    assert gen["id"].startswith("npc_")
    # Always hidden: kept in the unintroduced pool, not yet met.
    assert gen["introduced"] is False
    assert gen["status"] == "unintroduced"
    assert gen["met_turn"] is None
    assert gen["source"] == "generated"
    assert gen["role"] == "wildcard"
    assert gen["personality"] == ["restless", "clever", "secretive"]
    assert gen["change_log"][-1]["source"] == "generated"
    # Nothing embedded into RAG while the character is unmet.
    assert "memories" not in captured
    assert "Mira Voss" in result["message"]


def test_generate_accepts_gen_and_random_aliases():
    backend = _load_backend()
    for alias in ("gen", "random"):
        result = asyncio.run(backend.on_command_npc(
            [alias], _state(), _make_sdk(_GEN_REPLY, {}),
        ))
        chars = result["module_data"]["wb_npc_system"]["characters"]
        assert any(n["name"] == "Mira Voss" for n in chars.values())


def test_generate_forces_hidden_even_if_llm_marks_location_bound():
    backend = _load_backend()
    reply = json.dumps({"npc": {"name": "Ser Hidden", "encounter_type": "location_bound"}})

    result = asyncio.run(backend.on_command_npc(["generate"], _state(), _make_sdk(reply, {})))

    gen = next(n for n in result["module_data"]["wb_npc_system"]["characters"].values()
               if n["name"] == "Ser Hidden")
    assert gen["introduced"] is False
    assert gen["status"] == "unintroduced"


def test_generate_handles_unparseable_llm_output():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(["generate"], _state(), _make_sdk("not json", {})))
    assert "module_data" not in result
    assert "Could not generate" in result["message"]


def test_generate_handles_empty_payload():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(
        ["generate"], _state(), _make_sdk(json.dumps({"npc": {}}), {}),
    ))
    assert "module_data" not in result
    assert "nothing usable" in result["message"]


def test_generate_with_request_threads_brief_into_prompt():
    backend = _load_backend()
    captured = {}
    request = "a grumpy dwarven blacksmith with a smuggling secret"
    args = ["generate", urllib.parse.quote(request)]

    result = asyncio.run(backend.on_command_npc(args, _state(), _make_sdk(_GEN_REPLY, captured)))

    prompt = captured["prompt"]
    assert "PLAYER REQUEST" in prompt
    assert request in prompt
    # The request is recorded on the new character's change log for provenance.
    gen = next(n for n in result["module_data"]["wb_npc_system"]["characters"].values()
               if n["name"] == "Mira Voss")
    assert request in gen["change_log"][-1]["note"]


def test_generate_without_request_omits_request_block():
    backend = _load_backend()
    captured = {}
    asyncio.run(backend.on_command_npc(["generate"], _state(), _make_sdk(_GEN_REPLY, captured)))
    assert "PLAYER REQUEST" not in captured["prompt"]


def test_generate_request_is_length_capped():
    backend = _load_backend()
    captured = {}
    long_request = "x" * 5000
    args = ["generate", urllib.parse.quote(long_request)]

    asyncio.run(backend.on_command_npc(args, _state(), _make_sdk(_GEN_REPLY, captured)))

    # Only the capped prefix reaches the prompt, never the full 5000 chars.
    assert "x" * backend.MAX_GEN_REQUEST_CHARS in captured["prompt"]
    assert "x" * (backend.MAX_GEN_REQUEST_CHARS + 1) not in captured["prompt"]


def _plot_data():
    return {
        "profile": {
            "playstyle": {"combat": 2, "intrigue": 8},
            "tone": "grim and superstitious",
            "themes": ["forbidden knowledge", "coastal decay"],
            "likes": [{"text": "morally grey allies", "weight": "high"}],
            "dislikes": [{"text": "comic relief", "weight": "medium"}],
        }
    }


def test_generate_hooks_into_plot_director_profile():
    backend = _load_backend()
    captured = {}
    state = _state()
    state["module_data"]["wb_plot_director"] = _plot_data()

    asyncio.run(backend.on_command_npc(["generate"], state, _make_sdk(_GEN_REPLY, captured)))

    prompt = captured["prompt"]
    assert "STORY DIRECTION" in prompt
    assert "grim and superstitious" in prompt
    assert "forbidden knowledge" in prompt
    assert "morally grey allies (high)" in prompt
    assert "comic relief (medium)" in prompt
    # The instruction that ties the character to the profile is present.
    assert "resonate with the STORY DIRECTION" in prompt


def test_generate_omits_plot_block_when_module_absent():
    backend = _load_backend()
    captured = {}
    asyncio.run(backend.on_command_npc(["generate"], _state(), _make_sdk(_GEN_REPLY, captured)))
    assert "STORY DIRECTION" not in captured["prompt"]


def test_generate_omits_plot_block_when_profile_empty():
    backend = _load_backend()
    captured = {}
    state = _state()
    # Module present but nothing learned yet -> no block, no dangling instruction.
    state["module_data"]["wb_plot_director"] = {"profile": {
        "playstyle": {}, "tone": "", "themes": [], "likes": [], "dislikes": [],
    }}
    asyncio.run(backend.on_command_npc(["generate"], state, _make_sdk(_GEN_REPLY, captured)))
    assert "STORY DIRECTION" not in captured["prompt"]
    assert "resonate with the STORY DIRECTION" not in captured["prompt"]


# --------------------------------------------------------------------------
# /npc delete
# --------------------------------------------------------------------------

def test_delete_removes_character_and_purges_memories():
    backend = _load_backend()
    captured = {}
    state = _state()

    result = asyncio.run(backend.on_command_npc(
        ["delete", "npc_aaaa1111"], state, _make_sdk("{}", captured),
    ))

    chars = result["module_data"]["wb_npc_system"]["characters"]
    assert "npc_aaaa1111" not in chars
    # Authoritative replace so the removal actually propagates past deep-merge.
    assert result["module_data_replace"] == ["characters"]
    # All of the character's memories are purged (no tag filter).
    assert captured["forgotten"] == [{"id": "npc_aaaa1111", "tags": []}]
    assert "Deleted Serah" in result["message"]


def test_delete_strips_dangling_relationships_from_survivors():
    backend = _load_backend()
    doomed = _npc()  # npc_aaaa1111
    survivor = _npc()
    survivor["id"] = "npc_bbbb2222"
    survivor["name"] = "Corin"
    survivor["relationships"] = [
        {"npc_id": "npc_aaaa1111", "type": "rival", "description": "old grudge"},
        {"npc_id": "npc_cccc3333", "type": "ally", "description": "kept"},
    ]
    state = {
        "turn": 9,
        "history": [],
        "module_configs": {},
        "module_data": {"wb_npc_system": {"characters": {
            doomed["id"]: doomed, survivor["id"]: survivor,
        }}},
    }

    result = asyncio.run(backend.on_command_npc(
        ["delete", "npc_aaaa1111"], state, _make_sdk("{}", {}),
    ))

    chars = result["module_data"]["wb_npc_system"]["characters"]
    assert "npc_aaaa1111" not in chars
    rels = chars["npc_bbbb2222"]["relationships"]
    assert [r["npc_id"] for r in rels] == ["npc_cccc3333"]


def test_delete_unknown_id_is_a_noop():
    backend = _load_backend()
    result = asyncio.run(backend.on_command_npc(
        ["delete", "npc_nope"], _state(), _make_sdk("{}", {}),
    ))
    assert "module_data" not in result
    assert "Unknown character" in result["message"]
