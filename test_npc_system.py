import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_npc_system" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_npc_system_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_sdk(generate_reply="{}"):
    calls = {"remember": [], "forget": [], "prompts": [], "generate_count": 0}

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls["prompts"].append(prompt)
        calls["generate_count"] += 1
        return generate_reply(prompt) if callable(generate_reply) else generate_reply

    async def remember(npc_id, text, turn, importance=5, permanent=False, tags=None):
        calls["remember"].append({
            "npc_id": npc_id, "text": text, "turn": turn,
            "importance": importance, "permanent": permanent, "tags": list(tags or []),
        })
        return f"mem_{npc_id}"

    async def recall(npc_id, limit=3):
        return []

    async def forget(npc_id, tags=None):
        calls["forget"].append({"npc_id": npc_id, "tags": list(tags or [])})
        return 1

    sdk = SimpleNamespace(
        llm=SimpleNamespace(generate=generate),
        memory=SimpleNamespace(remember=remember, recall=recall, forget=forget),
    )
    return sdk, calls


def _state(bank=None, mutation_config=None, history=None):
    return {
        "turn": 4,
        "history": history if history is not None else ["The tavern door creaks open."],
        "player_location_node_id": "node_market",
        "player_location_region": "Harborside",
        "player_location_layer_id": "surface",
        "characters": {"default_player": {"name": "Aria"}},
        "module_configs": {"wb_npc_system": mutation_config or {}},
        "module_data": {"wb_npc_system": {"characters": bank or {}}},
    }


def _bank_from_result(result):
    return result["module_data"]["wb_npc_system"]["characters"]


# ── Part A: profile embedding on introduction ────────────────────────────────

def test_introduction_embeds_permanent_profile():
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_1": {
        "id": "npc_1", "name": "Borin", "race": "dwarf", "gender": "male",
        "appearance": "Broad, soot-streaked.", "archetype": "smith",
        "personality": ["gruff", "loyal", "proud"], "role": "ally",
        "pitch": "A smith who owes the player a debt.",
        "encounter_type": "encounter", "introduced": False, "status": "unintroduced",
    }}
    mutation = {"npc_introductions": [{"npc_id": "npc_1", "first_impression": "Met Borin at his forge."}]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    npc = _bank_from_result(result)["npc_1"]
    assert npc["introduced"] is True
    assert npc["profile_embedded"] is True

    profiles = [c for c in calls["remember"] if c["tags"] == ["profile"]]
    assert len(profiles) == 1
    assert profiles[0]["permanent"] is True
    assert profiles[0]["importance"] == 8
    assert "Borin" in profiles[0]["text"] and "smith" in profiles[0]["text"]
    # The one-line interaction memory is still stored (non-permanent).
    assert any(c["tags"] == [] and c["permanent"] is False for c in calls["remember"])


def test_embed_profile_is_idempotent():
    backend = _load_backend()
    sdk, calls = _make_sdk()
    npc = {"id": "npc_x", "name": "Sela", "role": "informant", "personality": []}

    asyncio.run(backend._embed_profile(npc, turn=2, sdk=sdk))
    asyncio.run(backend._embed_profile(npc, turn=3, sdk=sdk))

    assert len([c for c in calls["remember"] if c["tags"] == ["profile"]]) == 1


def test_embed_profile_force_replaces_old_profile():
    backend = _load_backend()
    sdk, calls = _make_sdk()
    npc = {"id": "npc_x", "name": "Sela", "role": "informant", "personality": []}

    asyncio.run(backend._embed_profile(npc, turn=2, sdk=sdk))
    npc["name"] = "Sela the Grey"
    asyncio.run(backend._embed_profile(npc, turn=5, sdk=sdk, force=True))

    # The stale profile is forgotten before the updated one is embedded.
    assert calls["forget"] == [{"npc_id": "npc_x", "tags": ["profile"]}]
    profiles = [c for c in calls["remember"] if c["tags"] == ["profile"]]
    assert len(profiles) == 2
    assert "Sela the Grey" in profiles[-1]["text"]
    assert npc["profile_embedded"] is True


def test_embed_profiles_toggle_off_skips_embedding():
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_1": {"id": "npc_1", "name": "Borin", "role": "ally",
                      "personality": [], "introduced": False, "status": "unintroduced"}}
    mutation = {"npc_introductions": [{"npc_id": "npc_1", "first_impression": "Hi."}]}
    state = _state(bank, mutation_config={"embed_profiles": False})

    asyncio.run(backend.on_mutate_state(mutation, state, sdk))

    assert not any(c["tags"] == ["profile"] for c in calls["remember"])


# ── Part B: capturing story-introduced characters ────────────────────────────

def test_capture_creates_introduced_bank_npc_and_embeds():
    backend = _load_backend()
    reply = json.dumps({"npcs": [{
        "name": "Seraphine", "race": "human", "gender": "female",
        "appearance": "A masked woman in grey.", "archetype": "oracle",
        "pitch": "A blind oracle who speaks in riddles.",
        "personality": ["cryptic", "calm", "watchful"], "role": "informant",
    }]})
    sdk, calls = _make_sdk(reply)
    mutation = {"story_characters": [
        {"name": "Seraphine", "descriptor": "a masked oracle at the shrine", "evidence": "She warned the player."}
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state({}), sdk))

    bank = _bank_from_result(result)
    seraphine = next(n for n in bank.values() if n["name"] == "Seraphine")
    assert seraphine["introduced"] is True
    assert seraphine["source"] == "story"
    assert seraphine["encounter_type"] == "location_bound"
    assert seraphine["location_region"] == "Harborside"
    assert any(c["tags"] == ["profile"] and c["permanent"] for c in calls["remember"])


def test_capture_skips_already_known_names():
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps({"npcs": []}))
    bank = {"npc_9": {"id": "npc_9", "name": "Seraphine", "role": "informant",
                      "personality": [], "introduced": True, "status": "active"}}
    mutation = {"story_characters": [{"name": "Seraphine", "descriptor": "the oracle", "evidence": "x"}]}

    added = asyncio.run(backend._capture_story_characters(mutation, _state(bank), bank, sdk))

    # Name already in the bank → filtered before any LLM call.
    assert added is False
    assert calls["generate_count"] == 0


def test_capture_excludes_the_player():
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps({"npcs": []}))
    mutation = {"story_characters": [{"name": "Aria", "descriptor": "the player", "evidence": "x"}]}

    added = asyncio.run(backend._capture_story_characters(mutation, _state({}), {}, sdk))

    assert added is False
    assert calls["generate_count"] == 0


# ── Part B: dynamic mutation schema ──────────────────────────────────────────

def test_mutation_schema_lists_known_names_to_exclude():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_1": {"id": "npc_1", "name": "Borin"}}

    schema = asyncio.run(backend.on_mutation_schema(_state(bank), sdk))

    assert "story_characters" in schema
    desc = schema["story_characters"].lower()
    assert "borin" in desc
    assert "aria" in desc  # player excluded too


def test_mutation_schema_disabled_returns_none():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    state = _state({}, mutation_config={"capture_story_characters": False})

    assert asyncio.run(backend.on_mutation_schema(state, sdk)) is None


# ── Part C: present characters always in context ─────────────────────────────

def _present_npc(npc_id, name, **overrides):
    npc = {
        "id": npc_id, "name": name, "race": "human", "gender": "female",
        "appearance": f"{name} has a scarred cheek.", "archetype": "guide",
        "personality": ["stern", "loyal", "quiet"], "role": "ally",
        "pitch": f"{name} knows every alley in Harborside.",
        "encounter_type": "location_bound", "introduced": True, "status": "active",
        "location_node_id": "node_market", "location_region": "Harborside",
        "location_layer_id": "surface", "traveling_with_player": False, "notes": "",
    }
    npc.update(overrides)
    return npc


def test_present_characters_injected_into_context():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {
        "npc_here": _present_npc("npc_here", "Mara", notes="Owes the player a favor."),
        "npc_party": _present_npc("npc_party", "Tobin", location_node_id="node_far",
                                  location_region="Frostpeak", traveling_with_player=True),
        "npc_far": _present_npc("npc_far", "Vex", location_node_id="node_keep",
                                location_region="Frostpeak"),
        "npc_dead": _present_npc("npc_dead", "Old Han", status="deceased"),
        "npc_pending": _present_npc("npc_pending", "Ilya", introduced=False,
                                    status="unintroduced"),
    }

    result = asyncio.run(backend.on_gather_context(_state(bank), sdk))

    ctx = result["context_string"]
    # At the player's location, with full established record and notes.
    assert "Mara" in ctx and "scarred cheek" in ctx and "stern" in ctx
    assert "Owes the player a favor." in ctx
    # Party members are present wherever the player is.
    assert "Tobin" in ctx and "traveling with the player" in ctx
    # Elsewhere / dead / unintroduced characters stay out.
    assert "Vex" not in ctx and "Old Han" not in ctx and "Ilya" not in ctx


def _locationless_state(bank, history, **config):
    state = _state(bank, mutation_config=config or None, history=history)
    state["player_location_node_id"] = ""
    state["player_location_region"] = ""
    state["player_location_layer_id"] = ""
    return state


def _unlocated_npc(npc_id, name, **overrides):
    return _present_npc(npc_id, name, location_node_id=None,
                        location_region=None, location_layer_id=None, **overrides)


def test_present_characters_include_story_mentions_without_location():
    # Saves without location tracking can't match on location; with the LLM
    # check disabled, a character named in the recent story is still on stage
    # and must reach the context.
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_x": _unlocated_npc("npc_x", "Mara")}
    state = _locationless_state(bank, ["You enter the market.", "Mara waves you over."],
                                scene_presence_use_llm=False)

    result = asyncio.run(backend.on_gather_context(state, sdk))

    assert "Mara" in result["context_string"]
    assert calls["generate_count"] == 0


def test_present_characters_mention_matches_whole_words_only():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_x": _unlocated_npc("npc_x", "Han")}
    state = _locationless_state(bank, ["You reach out a hand to the merchant."],
                                scene_presence_use_llm=False)

    assert asyncio.run(backend.on_gather_context(state, sdk)) is None


def test_llm_scene_presence_includes_unnamed_character():
    # The scene refers to "the guide" without naming her; the LLM presence
    # check still puts her in context.
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps(["npc_x"]))
    bank = {"npc_x": _unlocated_npc("npc_x", "Mara")}
    state = _locationless_state(bank, ["The guide leads you deeper into the tunnels."])

    result = asyncio.run(backend.on_gather_context(state, sdk))

    assert "Mara" in result["context_string"]
    assert calls["generate_count"] == 1
    assert "PHYSICALLY PRESENT" in calls["prompts"][0]


def test_llm_scene_presence_excludes_merely_mentioned_character():
    # Mara is talked about but not there; the LLM verdict overrides the
    # name-mention heuristic.
    backend = _load_backend()
    sdk, _ = _make_sdk(json.dumps([]))
    bank = {"npc_x": _unlocated_npc("npc_x", "Mara")}
    state = _locationless_state(bank, ["The merchant tells you Mara left town yesterday."])

    assert asyncio.run(backend.on_gather_context(state, sdk)) is None


def test_llm_scene_presence_falls_back_to_name_matching_on_bad_reply():
    backend = _load_backend()
    sdk, _ = _make_sdk("I cannot answer that.")
    bank = {"npc_x": _unlocated_npc("npc_x", "Mara")}
    state = _locationless_state(bank, ["Mara waves you over to her stall."])

    result = asyncio.run(backend.on_gather_context(state, sdk))

    assert "Mara" in result["context_string"]


def test_llm_scene_presence_not_used_when_location_is_tracked():
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps(["npc_far"]))
    bank = {"npc_far": _present_npc("npc_far", "Vex", location_node_id="node_keep",
                                    location_region="Frostpeak")}

    # Located elsewhere, not named in the story: absent, and no LLM call spent.
    assert asyncio.run(backend.on_gather_context(_state(bank), sdk)) is None
    assert calls["generate_count"] == 0


def test_present_characters_excluded_across_layers():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_below": _present_npc("npc_below", "Drez", location_layer_id="underdark",
                                      location_region="Harborside")}

    result = asyncio.run(backend.on_gather_context(_state(bank), sdk))

    assert result is None


def test_present_character_context_toggle_off():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_here": _present_npc("npc_here", "Mara")}
    state = _state(bank, mutation_config={"present_character_context": False})

    assert asyncio.run(backend.on_gather_context(state, sdk)) is None


def test_context_and_introduction_merge_in_one_result():
    backend = _load_backend()
    reply = json.dumps({"introduce": True, "npc_id": "npc_pending", "reason": "The player asks around."})
    sdk, _ = _make_sdk(reply)
    bank = {
        "npc_here": _present_npc("npc_here", "Mara"),
        "npc_pending": _present_npc("npc_pending", "Ilya", introduced=False,
                                    status="unintroduced", encounter_type="encounter"),
    }

    result = asyncio.run(backend.on_gather_context(_state(bank), sdk))

    assert "Mara" in result["context_string"]
    assert result["module_data"]["wb_npc_system"]["pending_introduction"] == "npc_pending"
