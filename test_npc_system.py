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


# ── Duplicate prevention: introductions must land on the existing record ─────

def _unmet_npc(npc_id, name, **overrides):
    npc = {
        "id": npc_id, "name": name, "race": "human", "gender": "male",
        "appearance": "Weathered.", "archetype": "smith",
        "personality": ["gruff", "loyal", "proud"], "role": "ally",
        "pitch": f"{name} owes the player a debt.",
        "encounter_type": "encounter", "introduced": False,
        "status": "unintroduced", "notes": "",
    }
    npc.update(overrides)
    return npc


def test_intro_resolves_by_name_when_reader_id_is_wrong():
    # Reader models regularly echo the character's name (or invent an id)
    # instead of the bank id; the introduction must still land on the record
    # instead of being silently dropped.
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_1": _unmet_npc("npc_1", "Borin Ironvein")}
    mutation = {"npc_introductions": [
        {"npc_id": "npc_borin", "name": "Borin", "first_impression": "Met Borin at his forge."}
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    npc = _bank_from_result(result)["npc_1"]
    assert npc["introduced"] is True
    assert any("Met Borin at his forge." in c["text"] for c in calls["remember"])


def test_intro_skips_already_introduced_character():
    # Re-reporting a met character must not reset their record or add another
    # "met them" memory.
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_1": _unmet_npc("npc_1", "Borin", introduced=True, status="active")}
    mutation = {"npc_introductions": [{"npc_id": "npc_1", "first_impression": "Met again."}]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    assert result is None
    assert calls["remember"] == []


def _pending_state(bank, pending_id, history):
    state = _state(bank, history=history)
    state["module_data"]["wb_npc_system"]["pending_introduction"] = pending_id
    state["module_data"]["wb_npc_system"]["introduction_reason"] = "The scene calls for it."
    return state


def test_pending_introduction_lands_when_narration_names_character():
    # The storyteller was explicitly told to introduce this character. If the
    # reader fails to report it but the narration names them (even partially),
    # the introduction still registers -- otherwise the record stays unmet
    # while the character walks the story, the exact split that later gets
    # them captured as a duplicate.
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_1": _unmet_npc("npc_1", "Borin Ironvein")}
    state = _pending_state(bank, "npc_1", ["Borin looks up from the anvil and nods."])

    result = asyncio.run(backend.on_mutate_state({}, state, sdk))

    npc = _bank_from_result(result)["npc_1"]
    assert npc["introduced"] is True
    assert result["module_data"]["wb_npc_system"]["pending_introduction"] is None


def test_pending_introduction_cleared_even_when_not_woven_in():
    # A stale pending id would re-render the "introduce this character" block
    # every turn, making the storyteller re-introduce someone repeatedly.
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_1": _unmet_npc("npc_1", "Borin Ironvein")}
    state = _pending_state(bank, "npc_1", ["The rain hammers the empty street."])

    result = asyncio.run(backend.on_mutate_state({}, state, sdk))

    assert result["module_data"]["wb_npc_system"]["pending_introduction"] is None
    assert _bank_from_result(result)["npc_1"]["introduced"] is False


def test_capture_name_variant_introduces_existing_record():
    # "Kara" in the reader's report is the bank's "Kara Vane" -- the existing
    # record is introduced instead of a duplicate being created, without
    # spending an LLM call.
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_1": _unmet_npc("npc_1", "Kara Vane")}
    mutation = {"story_characters": [
        {"name": "Kara", "descriptor": "a smuggler at the docks", "evidence": "She waved."}
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    new_bank = _bank_from_result(result)
    assert list(new_bank) == ["npc_1"]
    assert new_bank["npc_1"]["introduced"] is True
    assert calls["generate_count"] == 0
    assert any("smuggler at the docks" in c["text"] for c in calls["remember"])


def test_capture_llm_resolves_revealed_name_to_epithet_record():
    # The story reveals that "The Hooded Stranger" is called Veyra. String
    # matching cannot connect the two, so the capture prompt carries the
    # existing roster and the profiler answers with existing_npc_id; the
    # record is renamed and introduced instead of duplicated.
    backend = _load_backend()
    reply = json.dumps({"npcs": [{"existing_npc_id": "npc_1", "name": "Veyra"}]})
    sdk, calls = _make_sdk(reply)
    bank = {"npc_1": _unmet_npc("npc_1", "The Hooded Stranger")}
    mutation = {"story_characters": [
        {"name": "Veyra", "descriptor": "the stranger, finally named", "evidence": "'I am Veyra.'"}
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    new_bank = _bank_from_result(result)
    assert list(new_bank) == ["npc_1"]
    npc = new_bank["npc_1"]
    assert npc["name"] == "Veyra"
    assert npc["introduced"] is True
    assert "Formerly known as The Hooded Stranger." in npc["notes"]
    # The profiler saw the existing roster to match identities against.
    assert "npc_1" in calls["prompts"][0] and "The Hooded Stranger" in calls["prompts"][0]


def test_capture_llm_duplicate_of_introduced_character_creates_nothing():
    # The profiler flags the reported "new" character as an already-met
    # record: nothing is created, renamed, or re-remembered. (An epithet
    # reported for a character with a real name never overwrites it.)
    backend = _load_backend()
    reply = json.dumps({"npcs": [{"existing_npc_id": "npc_1", "name": "The Grey Guide"}]})
    sdk, calls = _make_sdk(reply)
    bank = {"npc_1": _unmet_npc("npc_1", "Mara", introduced=True, status="active")}
    mutation = {"story_characters": [
        {"name": "The Grey Guide", "descriptor": "a grey-cloaked guide", "evidence": "x"}
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    assert result is None or list(_bank_from_result(result)) == ["npc_1"]
    assert calls["remember"] == []
    assert bank["npc_1"]["name"] == "Mara"


def test_same_character_name_matching():
    backend = _load_backend()
    assert backend._same_character_name("Kara", "Kara Vane")
    assert backend._same_character_name("Borin Ironvein", "borin")
    assert not backend._same_character_name("The Hooded Stranger", "The Grey Warden")
    assert not backend._same_character_name("Veyra", "The Hooded Stranger")
    assert not backend._same_character_name("", "Kara")


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


def test_mutation_schema_asks_for_unnamed_epithets():
    backend = _load_backend()
    sdk, _ = _make_sdk()

    schema = asyncio.run(backend.on_mutation_schema(_state({}), sdk))

    desc = schema["story_characters"].lower()
    assert "epithet" in desc
    # Trivial extras stay excluded even with unnamed capture on.
    assert "incidental extras" in desc


def test_mutation_schema_disabled_omits_story_characters():
    # The capture toggle only controls story-character capture; the schema
    # still tells the reader which unmet characters it may introduce.
    backend = _load_backend()
    sdk, _ = _make_sdk()
    state = _state({}, mutation_config={"capture_story_characters": False})

    schema = asyncio.run(backend.on_mutation_schema(state, sdk))
    assert "story_characters" not in schema
    assert "npc_introductions" in schema


def test_mutation_schema_lists_unmet_character_ids():
    # The reader only ever sees the story text plus this schema, so the bank
    # ids of unmet characters must be spelled out -- otherwise it cannot
    # report an introduction by a valid npc_id.
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {
        "npc_1": {"id": "npc_1", "name": "Borin Ironvein",
                  "introduced": False, "status": "unintroduced"},
        "npc_2": {"id": "npc_2", "name": "Mara", "introduced": True, "status": "active"},
    }

    schema = asyncio.run(backend.on_mutation_schema(_state(bank), sdk))

    desc = schema["npc_introductions"]
    assert "npc_1" in desc and "Borin Ironvein" in desc
    # Already-met characters are not introduction candidates.
    assert "npc_2" not in desc

    # With nobody left to meet, the reader is told to report nothing.
    schema = asyncio.run(backend.on_mutation_schema(_state({}), sdk))
    assert "empty array" in schema["npc_introductions"]


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

    # The same roster is published for other modules to consume.
    presence = result["module_data"]["wb_npc_system"]["scene_presence"]
    assert presence["turn"] == 4
    assert sorted(presence["npc_ids"]) == ["npc_here", "npc_party"]


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

    result = asyncio.run(backend.on_gather_context(state, sdk))
    assert "context_string" not in result
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == []


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

    result = asyncio.run(backend.on_gather_context(state, sdk))
    assert "context_string" not in result
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == []


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
    result = asyncio.run(backend.on_gather_context(_state(bank), sdk))
    assert "context_string" not in result
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == []
    assert calls["generate_count"] == 0


def test_present_characters_excluded_across_layers():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_below": _present_npc("npc_below", "Drez", location_layer_id="underdark",
                                      location_region="Harborside")}

    result = asyncio.run(backend.on_gather_context(_state(bank), sdk))

    assert "context_string" not in result
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == []


def test_present_character_context_toggle_off():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_here": _present_npc("npc_here", "Mara")}
    state = _state(bank, mutation_config={"present_character_context": False})

    # The toggle suppresses the storyteller block, but the scene roster is
    # still published for other modules.
    result = asyncio.run(backend.on_gather_context(state, sdk))
    assert "context_string" not in result
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == ["npc_here"]


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
    # The about-to-be-introduced character counts as present in the roster.
    presence = result["module_data"]["wb_npc_system"]["scene_presence"]
    assert sorted(presence["npc_ids"]) == ["npc_here", "npc_pending"]


# ── Part D: manual activation from the browser ───────────────────────────────

def _edit_payload(fields):
    import urllib.parse
    return urllib.parse.quote(json.dumps(fields))


def test_manual_status_activation_introduces_character():
    # The UI status dropdown is player authority: switching an unmet character
    # to active must bring them fully into play, not just relabel them.
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {"npc_1": _present_npc("npc_1", "Sela", introduced=False,
                                  status="unintroduced", encounter_type="encounter",
                                  location_node_id=None, location_region=None,
                                  location_layer_id=None)}
    state = _state(bank)

    result = asyncio.run(backend._apply_manual_edit(
        "npc_1", _edit_payload({"status": "active"}), state, sdk))
    npc = _bank_from_result(result)["npc_1"]

    assert npc["introduced"] is True
    assert npc["status"] == "active"
    assert npc["met_turn"] == 4
    assert npc["presence_pinned_turn"] == 4
    # Bound to the player's location, like a story introduction.
    assert npc["encounter_type"] == "location_bound"
    assert npc["location_node_id"] == "node_market"
    # Profile embedded into RAG on activation.
    assert npc["profile_embedded"] is True
    assert any(c["tags"] == ["profile"] for c in calls["remember"])

    # And the reverse: back to unintroduced returns them to the hidden pool.
    result = asyncio.run(backend._apply_manual_edit(
        "npc_1", _edit_payload({"status": "unintroduced"}), state, sdk))
    npc = _bank_from_result(result)["npc_1"]
    assert npc["introduced"] is False
    assert npc["met_turn"] is None
    assert "presence_pinned_turn" not in npc


def test_presence_pin_keeps_manual_character_on_stage():
    # A freshly pinned character has never been mentioned in the story, so
    # both name matching and the LLM tracker would drop them; the pin wins
    # while fresh, then expires so normal tracking takes over.
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps([]))  # LLM: nobody is present
    bank = {"npc_1": _unlocated_npc("npc_1", "Sela", presence_pinned_turn=4)}
    state = _locationless_state(bank, ["You walk the empty pier."])

    result = asyncio.run(backend.on_gather_context(state, sdk))
    assert "Sela" in result["context_string"]
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == ["npc_1"]
    # Pinned characters skip the LLM presence check entirely.
    assert calls["generate_count"] == 0

    # A stale pin no longer forces presence.
    bank = {"npc_1": _unlocated_npc("npc_1", "Sela", presence_pinned_turn=1)}
    state = _locationless_state(bank, ["You walk the empty pier."])
    result = asyncio.run(backend.on_gather_context(state, sdk))
    assert "context_string" not in result


def test_manual_add_pins_presence():
    backend = _load_backend()
    sdk, _ = _make_sdk()
    state = _state({})

    result = asyncio.run(backend._apply_manual_add(
        _edit_payload({"name": "Korrin", "appearance": "a wiry sailor"}), state, sdk))
    npc = next(iter(_bank_from_result(result).values()))
    assert npc["introduced"] is True
    assert npc["presence_pinned_turn"] == 4

    # Characters added into the hidden pool are not pinned.
    result = asyncio.run(backend._apply_manual_add(
        _edit_payload({"name": "Hidden", "appearance": "cloaked", "introduced": False}),
        state, sdk))
    npc = next(n for n in _bank_from_result(result).values() if n["name"] == "Hidden")
    assert "presence_pinned_turn" not in npc


def test_stale_active_record_reaches_context():
    # A record whose status was flipped to active before the introduction
    # sync existed still reaches the storyteller context: _get_bank heals the
    # flags, and the presence pass keys on status alone anyway.
    backend = _load_backend()
    sdk, _ = _make_sdk()
    bank = {"npc_1": _present_npc("npc_1", "Sela", introduced=False, status="active")}
    result = asyncio.run(backend.on_gather_context(_state(bank), sdk))
    assert "Sela" in result["context_string"]
    assert result["module_data"]["wb_npc_system"]["scene_presence"]["npc_ids"] == ["npc_1"]


def test_get_bank_reconciles_stale_active_records():
    # Any code path touching the bank heals records whose status was flipped
    # to active before the introduction sync existed.
    backend = _load_backend()
    state = _state({"npc_1": _present_npc("npc_1", "Sela", introduced=False,
                                          status="active")})
    npc = backend._get_bank(state)["npc_1"]
    assert npc["introduced"] is True
    assert npc["met_turn"] == 4

    # Genuinely unmet characters stay hidden.
    state = _state({"npc_2": _present_npc("npc_2", "Vex", introduced=False,
                                          status="unintroduced")})
    assert backend._get_bank(state)["npc_2"]["introduced"] is False


# ── Appearance prompts must pin hair and eye color ───────────────────────────

def test_appearance_prompts_require_hair_and_eye_color():
    # Every LLM prompt that writes or rewrites an NPC appearance demands hair
    # color, eye color, and visual age, so image generation never has to
    # invent them per image.
    backend = _load_backend()

    def _prompt_with(calls, marker):
        return next(p for p in calls["prompts"] if marker in p)

    # Story-character capture.
    sdk, calls = _make_sdk(json.dumps({"npcs": []}))
    mutation = {"story_characters": [
        {"name": "Vex", "descriptor": "a hooded stranger", "evidence": ""}]}
    asyncio.run(backend._capture_story_characters(mutation, _state({}), {}, sdk))
    prompt = _prompt_with(calls, "Build a full character record")
    assert "hair color and eye color" in prompt
    assert "visual age" in prompt

    # The librarian's generator agent.
    sdk, calls = _make_sdk("{}")
    state = _state(mutation_config={"generator_frequency": 1})
    asyncio.run(backend.on_librarian(state, sdk))
    prompt = _prompt_with(calls, "new NPC concepts for the game")
    assert "hair color and eye color" in prompt
    assert "visual age" in prompt

    # /npc generate (random character).
    sdk, calls = _make_sdk("{}")
    asyncio.run(backend._generate_random_character(_state({}), sdk))
    prompt = _prompt_with(calls, "Create 1 new NPC concept")
    assert "hair color and eye color" in prompt
    assert "visual age" in prompt

    # /npc update record rewrite.
    sdk, calls = _make_sdk("{}")
    bank = {"npc_1": {"id": "npc_1", "name": "Borin", "personality": [],
                      "role": "ally", "introduced": True, "status": "active"}}
    asyncio.run(backend._update_npc_from_story("npc_1", _state(bank), sdk))
    prompt = _prompt_with(calls, "bring one character's record up to date")
    assert "keep hair color, eye color, and visual age stated" in prompt

    # The per-turn change-tracking pass.
    sdk, calls = _make_sdk(json.dumps({"updates": []}))
    state = _state({"npc_1": _present_npc("npc_1", "Mara")},
                   history=["Mara sharpens her blade."])
    asyncio.run(backend._track_character_changes(state, backend._get_bank(state), sdk))
    prompt = _prompt_with(calls, "bring their records up to date")
    assert "keep hair color, eye color, and visual age stated" in prompt


# ── Per-turn automatic change tracking ───────────────────────────────────────

def _tracking_state(bank, latest, mutation_config=None):
    # turn 4 with the default generator_frequency of 5: on_librarian runs
    # ONLY the change-tracking pass on this turn.
    return _state(bank, mutation_config=mutation_config,
                  history=["The tavern hums with low voices.", latest])


def test_tracking_pass_updates_changed_fields():
    backend = _load_backend()
    reply = json.dumps({"updates": [{
        "npc_id": "npc_1",
        "appearance": "Mara has a fresh scar across her brow, black hair, grey eyes.",
        "status": "unintroduced",
        "change_note": "Mara was scarred defending the gate.",
    }]})
    sdk, calls = _make_sdk(reply)
    bank = {"npc_1": _present_npc("npc_1", "Mara", profile_embedded=True)}
    state = _tracking_state(bank, "The blade catches Mara across the brow.")

    result = asyncio.run(backend.on_librarian(state, sdk))

    npc = _bank_from_result(result)["npc_1"]
    assert "fresh scar" in npc["appearance"]
    # A story-driven pass may retire a character but never un-introduce one.
    assert npc["status"] == "active"
    log = npc["change_log"][-1]
    assert log["source"] == "auto"
    assert log["fields"] == ["appearance"]
    # The change note is remembered for RAG, and the stale profile embedding
    # is replaced (appearance feeds the profile text).
    assert any(c["text"] == "Mara was scarred defending the gate." for c in calls["remember"])
    assert calls["forget"] == [{"npc_id": "npc_1", "tags": ["profile"]}]


def test_tracking_pass_rename_sweeps_record_and_keeps_old_name():
    backend = _load_backend()
    reply = json.dumps({"updates": [{
        "npc_id": "npc_1", "name": "Veyra",
        "change_note": "The stranger gives her name: Veyra.",
    }]})
    sdk, _ = _make_sdk(reply)
    bank = {"npc_1": _present_npc("npc_1", "The Hooded Stranger")}
    state = _tracking_state(bank, "The hooded stranger lowers her cowl. 'I am Veyra.'")

    result = asyncio.run(backend.on_librarian(state, sdk))

    npc = _bank_from_result(result)["npc_1"]
    assert npc["name"] == "Veyra"
    # The old name is swept out of the other text fields...
    assert npc["appearance"] == "Veyra has a scarred cheek."
    assert npc["pitch"] == "Veyra knows every alley in Harborside."
    # ...but stays retrievable in notes.
    assert "Formerly known as The Hooded Stranger." in npc["notes"]


def test_tracking_pass_rename_never_takes_another_characters_name():
    backend = _load_backend()
    reply = json.dumps({"updates": [{
        "npc_id": "npc_1", "name": "Tobin",
        "personality": ["grim", "loyal", "quiet"],
        "change_note": "Mara hardens after the ambush.",
    }]})
    sdk, _ = _make_sdk(reply)
    bank = {
        "npc_1": _present_npc("npc_1", "Mara"),
        "npc_2": _present_npc("npc_2", "Tobin"),
    }
    state = _tracking_state(bank, "Mara stares into the fire, changed.")

    result = asyncio.run(backend.on_librarian(state, sdk))

    npc = _bank_from_result(result)["npc_1"]
    # The colliding rename is dropped; the rest of the update still lands.
    assert npc["name"] == "Mara"
    assert npc["personality"] == "grim, loyal, quiet"


def test_tracking_pass_resolves_name_echo_to_record():
    # Tracker models sometimes echo the character's name instead of the id.
    backend = _load_backend()
    reply = json.dumps({"updates": [{
        "npc_id": "Mara", "notes": "Owes the player a favor.",
    }]})
    sdk, _ = _make_sdk(reply)
    bank = {"npc_1": _present_npc("npc_1", "Mara")}
    state = _tracking_state(bank, "Mara nods: 'I won't forget this.'")

    result = asyncio.run(backend.on_librarian(state, sdk))

    assert _bank_from_result(result)["npc_1"]["notes"] == "Owes the player a favor."


def test_tracking_pass_skips_when_no_character_is_in_scene():
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps({"updates": []}))
    bank = {"npc_1": _present_npc("npc_1", "Mara")}
    state = _tracking_state(bank, "Wind howls over the empty pass.")

    result = asyncio.run(backend.on_librarian(state, sdk))

    assert result is None
    assert calls["generate_count"] == 0


def test_tracking_pass_toggle_off_makes_no_llm_call():
    backend = _load_backend()
    sdk, calls = _make_sdk(json.dumps({"updates": []}))
    bank = {"npc_1": _present_npc("npc_1", "Mara")}
    state = _tracking_state(bank, "The blade catches Mara across the brow.",
                            mutation_config={"track_character_changes": False})

    result = asyncio.run(backend.on_librarian(state, sdk))

    assert result is None
    assert calls["generate_count"] == 0


def test_tracking_candidates_cover_roster_party_and_named():
    backend = _load_backend()
    bank = {
        "npc_a": _present_npc("npc_a", "Mara"),
        "npc_b": _present_npc("npc_b", "Tobin", traveling_with_player=True),
        "npc_c": _present_npc("npc_c", "Serel"),
        "npc_d": _present_npc("npc_d", "Wren"),
    }
    state = _tracking_state(bank, "Serel pours another round.")
    # npc_a comes from the published scene roster (stamped last turn -- the
    # reader increments the turn before the librarian runs).
    state["module_data"]["wb_npc_system"]["scene_presence"] = {
        "turn": state["turn"] - 1, "npc_ids": ["npc_a"],
    }

    ids = {n["id"] for n in backend._tracking_candidates(state, backend._get_bank(state))}

    assert ids == {"npc_a", "npc_b", "npc_c"}


def test_capture_llm_renames_real_named_record():
    # The story starts calling a known character by a genuinely new name
    # (not an epithet reveal). The profiler resolves the identity; the record
    # follows the story's name instead of spawning a duplicate.
    backend = _load_backend()
    reply = json.dumps({"npcs": [{"existing_npc_id": "npc_1", "name": "Lady Vane"}]})
    sdk, _ = _make_sdk(reply)
    bank = {"npc_1": _unmet_npc("npc_1", "Kara", introduced=True, status="active")}
    mutation = {"story_characters": [
        {"name": "Lady Vane", "descriptor": "the merchant queen of Harborside", "evidence": ""}
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(bank), sdk))

    npc = _bank_from_result(result)["npc_1"]
    assert npc["name"] == "Lady Vane"
    assert npc["pitch"] == "Lady Vane owes the player a debt."
    assert "Formerly known as Kara." in npc["notes"]


def test_update_command_rename_sweeps_record():
    backend = _load_backend()
    reply = json.dumps({"name": "Veyra", "change_note": "Now goes by Veyra."})
    sdk, _ = _make_sdk(reply)
    bank = {"npc_1": _present_npc("npc_1", "Mara")}

    asyncio.run(backend._update_npc_from_story("npc_1", _state(bank), sdk))

    npc = bank["npc_1"]
    assert npc["name"] == "Veyra"
    assert npc["pitch"] == "Veyra knows every alley in Harborside."
    assert "Formerly known as Mara." in npc["notes"]


# ── Demand-driven character generation ───────────────────────────────────────

def _npc_concept(name, need=None):
    concept = {
        "name": name, "race": "human", "gender": "female",
        "appearance": "Silver hair, gray eyes.",
        "archetype": "informant", "pitch": f"{name} hears everything.",
        "personality": ["wry", "careful", "curious"],
        "role": "informant", "encounter_type": "encounter",
    }
    if need is not None:
        concept["need"] = need
    return concept


def test_generator_is_demand_driven_and_sees_story_direction():
    backend = _load_backend()

    def reply(prompt):
        if "casting director" in prompt:
            return json.dumps({"npcs": []})
        return "{}"

    sdk, calls = _make_sdk(reply)
    state = _state(mutation_config={"generator_frequency": 1})
    state["module_data"]["wb_plot_director"] = {
        "profile": {"tone": "gritty", "themes": [], "likes": [], "dislikes": [],
                    "avoids": [{"text": "open brawls", "weight": "medium", "evidence": ""}]},
        "direction": {"premise": "A quiet war for the harbor's soul.",
                      "heading": "Reprisal is coming.",
                      "open_questions": ["Who tipped off the enforcers?"],
                      "recurring_elements": [], "updated_turn": 3},
    }

    result = asyncio.run(backend.on_librarian(state, sdk))

    prompt = next(p for p in calls["prompts"] if "casting director" in p)
    # Zero characters is framed as the normal outcome, and every character
    # must cite the story signal that demands it.
    assert '{"npcs": []}' in prompt
    assert '"need"' in prompt
    assert "usually zero or one" in prompt
    # The Plot Director's narrative direction and observed avoids reach the
    # generator, so "the direction of the story" is what gates creation.
    assert "A quiet war for the harbor's soul." in prompt
    assert "Who tipped off the enforcers?" in prompt
    assert "open brawls" in prompt
    # An empty reply creates nobody.
    assert not result or not _bank_from_result(result)


def test_generator_drops_characters_without_a_need():
    backend = _load_backend()

    def reply(prompt):
        if "casting director" in prompt:
            return json.dumps({"npcs": [
                _npc_concept("Sela", need="The open question about the enforcers needs an informant."),
                _npc_concept("Filler Fred"),  # no need stated -> dropped
            ]})
        return "{}"

    sdk, _ = _make_sdk(reply)
    result = asyncio.run(backend.on_librarian(
        _state(mutation_config={"generator_frequency": 1}), sdk))

    bank = _bank_from_result(result)
    names = [n["name"] for n in bank.values()]
    assert names == ["Sela"]
    record = next(iter(bank.values()))
    assert record["creation_need"] == "The open question about the enforcers needs an informant."


def test_generator_toggle_restores_pool_filling():
    backend = _load_backend()

    def reply(prompt):
        if "character designer" in prompt:
            return json.dumps({"npcs": [_npc_concept("Ambient Anna")]})
        return "{}"

    sdk, calls = _make_sdk(reply)
    result = asyncio.run(backend.on_librarian(
        _state(mutation_config={"generator_frequency": 1,
                                "demand_driven_generation": False}), sdk))

    prompt = next(p for p in calls["prompts"] if "character designer" in p)
    assert "fill gaps NOT covered" in prompt
    assert "casting director" not in prompt
    # Legacy mode keeps characters that state no need.
    assert [n["name"] for n in _bank_from_result(result).values()] == ["Ambient Anna"]


# ── world_format 2: map ids and cross-map travel ─────────────────────────────

def _v2_world_data():
    return {
        "world_format": 2,
        "root_map_id": "root",
        "maps": {
            "root": {
                "map_id": "root", "label": "Aldera", "level_type": "world",
                "parent_map_id": None, "anchor_node_id": None,
                "legacy_layer_id": "surface",
                "nodes": [
                    {"id": "node_market", "name": "Market", "x": 0, "y": 0},
                    {"id": "n_gate", "name": "Gate", "x": 5, "y": 0},
                ],
                "edges": [{"from": "node_market", "to": "n_gate"}],
            },
            "underdark": {
                "map_id": "underdark", "label": "The Underdark",
                "level_type": "underground", "parent_map_id": "root",
                "anchor_node_id": None, "legacy_layer_id": "underdark",
                "nodes": [
                    {"id": "u_hall", "name": "Hall", "x": 0, "y": 0},
                    {"id": "u_stair", "name": "Stair", "x": 1, "y": 0},
                ],
                "edges": [{"from": "u_hall", "to": "u_stair"}],
            },
        },
        "connections": [
            {"id": "c1", "from": {"map_id": "root", "node_id": "n_gate"},
             "to": {"map_id": "underdark", "node_id": "u_stair"}, "kind": "stair",
             "name": "The Deep Stair", "description": "A spiral stair.",
             "travel": {"mode": "journey", "turns": 3}, "bidirectional": True,
             "requirements": "a lantern", "hidden": False, "origin": "generated"},
        ],
    }


def test_get_bank_migrates_location_layer_id_to_map_id():
    backend = _load_backend()

    # v2 world: kept ids stay, vanished layer ids resolve via legacy_layer_id,
    # unknown ids fall back to the root map, absent stays absent-but-tolerated.
    state = _state({
        "npc_a": _present_npc("npc_a", "Ana", location_layer_id="underdark"),
        "npc_b": _present_npc("npc_b", "Bram"),  # fixture layer "surface" -> root
        "npc_c": _present_npc("npc_c", "Cato", location_layer_id="ghost_layer"),
        "npc_d": _present_npc("npc_d", "Dara", location_layer_id=None),
    })
    state["world_data"] = _v2_world_data()
    bank = backend._get_bank(state)

    assert bank["npc_a"]["location_map_id"] == "underdark"
    assert bank["npc_b"]["location_map_id"] == "root"
    assert bank["npc_c"]["location_map_id"] == "root"
    assert bank["npc_d"]["location_map_id"] is None
    assert all("location_layer_id" not in n for n in bank.values())

    # Un-migrated world: old layer ids are still the live ids, kept verbatim.
    state = _state({"npc_x": _present_npc("npc_x", "Xan", location_layer_id="underdark")})
    npc = backend._get_bank(state)["npc_x"]
    assert npc["location_map_id"] == "underdark"
    assert "location_layer_id" not in npc


def test_v2_npc_crosses_connection_to_players_map():
    # A very-far NPC on a parallel map walks to the near end of a v2 connection
    # and teleports through to the player's map, ignoring requirements and
    # travel turns, exactly like the legacy layer crossing.
    backend = _load_backend()
    sdk, calls = _make_sdk()
    bank = {
        "npc_deep": _present_npc(
            "npc_deep", "Vhal", role="antagonist",
            location_layer_id="underdark", location_node_id="u_stair",
            location_region=None, last_interaction_turn=0,
            last_travel_check_minutes=0),
        "npc_walker": _present_npc(
            "npc_walker", "Skrit", role="antagonist",
            location_layer_id="underdark", location_node_id="u_hall",
            location_region=None, last_interaction_turn=0,
            last_travel_check_minutes=0),
    }
    state = _state(bank)
    state["turn"] = 20
    state["world_data"] = _v2_world_data()
    state["player_location_map_id"] = "root"
    state["player_location_node_id"] = "node_market"
    state["module_data"]["wb_time_tracker"] = {"clock": {"total_minutes_elapsed": 2000}}

    changed = asyncio.run(
        backend._independent_travel_pass(state, backend._get_bank(state), sdk))

    assert changed is True
    # Already at the connection's near node: crosses to the far side. The
    # connection points root -> underdark, so the bidirectional reverse is used.
    assert bank["npc_deep"]["location_map_id"] == "root"
    assert bank["npc_deep"]["location_node_id"] == "n_gate"
    assert bank["npc_deep"]["location_region"] is None
    # One hop away from the exit: steps toward it along the map's own edges.
    assert bank["npc_walker"]["location_map_id"] == "underdark"
    assert bank["npc_walker"]["location_node_id"] == "u_stair"
    # Heuristic motivation (default): no LLM call spent.
    assert calls["generate_count"] == 0


def test_introduction_pass_shows_creation_need():
    backend = _load_backend()
    bank = {"npc_1": {
        "id": "npc_1", "name": "Sela", "race": "human", "gender": "female",
        "archetype": "informant", "pitch": "Sela hears everything.",
        "personality": ["wry"], "role": "informant", "encounter_type": "encounter",
        "introduced": False, "status": "unintroduced",
        "creation_need": "The enforcer question needs an informant.",
    }}
    sdk, calls = _make_sdk(json.dumps({"introduce": False, "npc_id": None, "reason": "not yet"}))

    asyncio.run(backend._introduction_pass(_state(bank), sdk))

    prompt = next(p for p in calls["prompts"] if "narrative director" in p)
    assert "Created for: The enforcer question needs an informant." in prompt


def test_transformation_dedup_and_untruncated_capture_context():
    """Known characters who physically transform or have their mind rewritten
    are flagged against duplication in both reader schema and profiler context,
    and history[-3:] is not truncated at 2500 characters."""
    backend = _load_backend()
    bank = {
        "npc_goddess": {
            "id": "npc_goddess",
            "name": "The Star-Eyed Goddess",
            "race": "Deity",
            "gender": "female",
            "appearance": "Luminous white hair with fluffy white cat ears and a white tail where her golden wings vanished.",
            "archetype": "Jaded Bureaucrat",
            "pitch": "Ancient reincarnation deity.",
            "personality": ["ditzy", "submissive", "lustful"],
            "role": "ally",
            "encounter_type": "location_bound",
            "introduced": True,
            "status": "active",
            "notes": "Mind rewritten to act as a devoted cat-girl calling the player Master.",
        }
    }
    state = _state(bank)
    early_marker = "EARLY_TRANSFORMATION_MARKER: Her golden wings dissolved into white cat ears."
    state["history"] = [
        early_marker + (" " + "detail" * 600),
        "Scene two continuation.",
        "Scene three continuation.",
    ]

    sdk, calls = _make_sdk(json.dumps({
        "npcs": [{"existing_npc_id": "npc_goddess", "name": "The White Catgirl"}]
    }))
    schema = asyncio.run(backend.on_mutation_schema(state, sdk))
    assert "the star-eyed goddess" in schema["story_characters"]
    assert "transformed" in schema["story_characters"]
    assert "shapeshifted" in schema["story_characters"]

    asyncio.run(backend._capture_story_characters(
        {"story_characters": [{"name": "The White Catgirl", "descriptor": "Cat-eared girl clinging to player", "evidence": "nya"}]},
        state,
        bank,
        sdk,
    ))
    assert len(bank) == 1
    capture_prompt = calls["prompts"][-1]
    assert early_marker in capture_prompt
    assert "Appearance: Luminous white hair with fluffy white cat ears" in capture_prompt
    assert "Notes: Mind rewritten to act as a devoted cat-girl" in capture_prompt
    assert "physically transformed, shapeshifted" in capture_prompt


# ── Part K: Character depth, Plot Director borrowing, fit-check, & async runner ─

def test_legacy_list_personality_healed_and_rename_sweeps_personality():
    backend = _load_backend()
    state = _state({
        "npc_1": {
            "id": "npc_1",
            "name": "The Hooded Stranger",
            "appearance": "The Hooded Stranger wears salt-stained wool.",
            "pitch": "The Hooded Stranger watches the pier.",
            "personality": ["guarded", "The Hooded Stranger speaks in clipped whispers"],
            "role": "informant",
            "introduced": True,
            "status": "active",
            "notes": "",
        }
    })
    bank = backend._get_bank(state)
    assert bank["npc_1"]["personality"] == "guarded, The Hooded Stranger speaks in clipped whispers"

    reply = json.dumps({
        "updates": [{"npc_id": "npc_1", "name": "Veyra", "change_note": "Reveals her name."}]
    })
    sdk, _ = _make_sdk(reply)
    state["history"] = ["The hooded stranger lowers her hood. 'Call me Veyra.'"]
    result = asyncio.run(backend.on_librarian(state, sdk))
    npc = _bank_from_result(result)["npc_1"]
    assert npc["name"] == "Veyra"
    assert "Veyra speaks in clipped whispers" in npc["personality"]
    assert "The Hooded Stranger" not in npc["personality"]


def test_instruction_slots_contract_and_custom_overrides():
    backend = _load_backend()
    slots = backend.get_instruction_slots()
    slot_ids = [s["id"] for s in slots]
    assert slot_ids == [
        "character_generation",
        "character_fit_check",
        "character_capture",
        "character_introduction",
        "character_tracking",
    ]
    for s in slots:
        assert s["label"] and s["description"] and s["default"].strip()
    # Returned list/dicts are defensive copies.
    slots[0]["default"] = "mutated"
    assert backend.get_instruction_slots()[0]["default"] != "mutated"

    custom = {
        "character_generation": "CUSTOM_GEN_DIRECTIVE",
        "character_fit_check": "CUSTOM_FIT_DIRECTIVE",
        "character_capture": "CUSTOM_CAPTURE_DIRECTIVE",
        "character_introduction": "CUSTOM_INTRO_DIRECTIVE",
        "character_tracking": "CUSTOM_TRACKING_DIRECTIVE",
    }
    state = _state(
        {
            "npc_unmet": {
                "id": "npc_unmet", "name": "Kael", "archetype": "scout",
                "role": "ally", "encounter_type": "encounter",
                "introduced": False, "status": "unintroduced", "personality": "wry",
            },
            "npc_met": {
                "id": "npc_met", "name": "Mara", "archetype": "guard",
                "role": "ally", "encounter_type": "location_bound",
                "location_node_id": "node_market", "location_region": "Harborside",
                "introduced": True, "status": "active", "personality": "steady",
            },
        },
        mutation_config={"generator_frequency": 1, "demand_driven_generation": False},
        history=["Mara steps into the market square."],
    )
    state["turn"] = 2
    state["module_instructions"] = custom

    # Generation + Fit-Check + Tracking prompts pick up overrides while keeping JSON schema & booru hair/eye rules.
    def reply_router(prompt):
        if "continuity and casting editor" in prompt:
            return json.dumps({"verdict": "accept", "rejected_names": [], "critique": ""})
        if "LASTING way" in prompt:
            return json.dumps({"updates": []})
        if "active story threads" in prompt:
            return json.dumps([])
        return json.dumps({"npcs": [{
            "name": "Orin", "race": "human", "gender": "male",
            "appearance": "Dark hair, grey eyes, early thirties.",
            "archetype": "broker", "pitch": "Sells dock manifests.",
            "personality": "Polite smile over cold calculation; wants harbor leverage; taps his signet ring.",
            "role": "informant", "encounter_type": "encounter",
        }]})

    sdk, calls = _make_sdk(reply_router)
    asyncio.run(backend.on_librarian(state, sdk))
    joined_prompts = "\n".join(calls["prompts"])
    assert "CUSTOM_GEN_DIRECTIVE" in joined_prompts
    assert "hair color and eye color" in joined_prompts
    assert "CUSTOM_FIT_DIRECTIVE" in joined_prompts
    assert "CUSTOM_TRACKING_DIRECTIVE" in joined_prompts

    # Introduction override
    sdk_intro, intro_calls = _make_sdk(json.dumps({"introduce": False, "npc_id": None, "reason": "wait"}))
    asyncio.run(backend._introduction_pass(state, sdk_intro))
    assert any("CUSTOM_INTRO_DIRECTIVE" in p for p in intro_calls["prompts"])

    # Capture override
    sdk_cap, cap_calls = _make_sdk(json.dumps({"npcs": [{
        "name": "Tessa", "race": "human", "gender": "female",
        "appearance": "Auburn hair, green eyes, mid-twenties.",
        "archetype": "ferryman", "pitch": "Controls the night skiff.",
        "personality": "gruff humor hiding debt panic", "role": "neutral",
    }]}))
    asyncio.run(backend._capture_story_characters(
        {"story_characters": [{"name": "Tessa", "descriptor": "ferryman", "evidence": "rows"}]},
        state, backend._get_bank(state), sdk_cap,
    ))
    assert any("CUSTOM_CAPTURE_DIRECTIVE" in p and "hair color and eye color" in p for p in cap_calls["prompts"])


def test_character_craft_rubric_and_casting_notes_iterations():
    backend = _load_backend()
    system_prompts = []
    prompts = []

    class RecordingLLM:
        async def generate(self, prompt, model_preference="balanced", system_prompt=None, **kw):
            prompts.append(prompt)
            system_prompts.append(system_prompt)
            if system_prompt == backend.CASTING_NOTES_SYSTEM_PROMPT:
                if "Orin" in prompt:
                    return json.dumps({"casting_notes": "Post-gen note: Orin covers harbor leverage; hold off on smugglers."})
                return json.dumps({"casting_notes": "Pre-gen note: Aria needs a charming customs foil with a nervous tell."})
            if "continuity and casting editor" in prompt:
                return json.dumps({"verdict": "accept", "rejected_names": [], "critique": ""})
            if "active story threads" in prompt:
                return json.dumps([])
            return json.dumps({"npcs": [{
                "need": "Harbor smuggling arc needs an insider foil.",
                "name": "Orin",
                "race": "human",
                "gender": "male",
                "appearance": "Black hair, amber eyes, late thirties.",
                "archetype": "customs broker",
                "pitch": "Controls the manifest stamps and needs Aria's silence.",
                "personality": "Projects unflappable bureaucratic warmth, yet panics whenever the auditing magistrate is named. Wants off-ledger gold to buy out his indenture, speaking in clipped shipping idioms while thumbing an ink-stained brass seal.",
                "role": "informant",
                "encounter_type": "encounter",
            }]})

    sdk, _ = _make_sdk()
    sdk.llm = RecordingLLM()

    state = _state(
        mutation_config={
            "generator_frequency": 3,
            "casting_iterations": 3,
            "demand_driven_generation": True,
        },
        history=["Aria inspects the sealed harbor warehouse."],
    )
    state["turn"] = 3

    result = asyncio.run(backend.on_librarian(state, sdk))
    mod_out = result["module_data"]["wb_npc_system"]
    assert "Post-gen note: Orin covers harbor leverage" in mod_out["casting_notes"]

    # casting_iterations=3 -> 2 pre-gen casting-note passes + 1 post-gen pass with CASTING_NOTES_SYSTEM_PROMPT
    note_calls = [sp for sp in system_prompts if sp == backend.CASTING_NOTES_SYSTEM_PROMPT]
    assert len(note_calls) == 3

    gen_prompt = next(p for p in prompts if "Create at most" in p and "Pre-gen note" in p)
    assert "Mask vs. Undercurrent" in gen_prompt
    assert "Active Want" in gen_prompt
    assert "Voice & Somatic Tell" in gen_prompt
    assert "NEVER invert the player's role" in gen_prompt

    orin = next(n for n in mod_out["characters"].values() if n["name"] == "Orin")
    assert isinstance(orin["personality"], str)
    assert "thumbing an ink-stained brass seal" in orin["personality"]


def test_borrows_plot_director_profile_roles_threads_gm_notes_and_protagonist():
    backend = _load_backend()
    state = _state(
        mutation_config={
            "generator_frequency": 3,
            "casting_iterations": 1,
            "demand_driven_generation": False,
            "fit_check_enabled": False,
        },
        history=["Aria bargains with the guild factors at dusk."],
    )
    state["turn"] = 3
    state["characters"] = {
        "default_player": {
            "name": "Aria",
            "race": "Half-Elf",
            "context": "Disgraced Admiralty Cipher-Clerk",
        }
    }
    state["module_data"]["wb_core_rpg"] = {
        "level": 4,
        "stats": {"Dexterity": 16, "Cunning": 15},
        "skills": {"Lockpicking": {"type": "active", "rating": 8, "description": "Opens port vaults"}},
    }
    state["module_data"]["wb_time_tracker"] = {
        "clock": {"day": 3, "hour": 19, "minute": 45, "time_of_day": "Evening", "total_minutes_elapsed": 4065}
    }
    state["module_data"]["wb_plot_director"] = {
        "status": "active",
        "profile": {
            "tone": "tense maritime intrigue",
            "themes": ["leverage", "double-crosses"],
            "likes": [{"text": "turning arrogant officials into informants", "role": "actor", "weight": "high"}],
            "avoids": [{"text": "being blackmailed or stripped of agency", "role": "target", "weight": "high"}],
            "dislikes": ["slapstick comedy"],
            "attachments": [{"name": "Harborside", "note": "protecting the dockworkers"}],
        },
        "direction": {
            "premise": "Expose the admiralty contraband ring.",
            "endgame": "Reclaim Aria's cleared name or seize the port.",
            "heading": "Infiltrating the customs house.",
            "horizon": ["Who forged the admiral's cipher?"],
            "planted_seeds": [{"seed": "A missing ledger with silver clasps", "payoff": "proves the forgery"}],
        },
        "thread": {
            "status": "active",
            "title": "Break into the customs vault",
            "hook": "The night clerk owes gambling debts",
        },
        "gm_notes": "Player loves verbal cat-and-mouse where Aria holds the cards.",
    }

    sdk, calls = _make_sdk(json.dumps({"npcs": [{
        "name": "Cassian", "race": "human", "gender": "male",
        "appearance": "Silver-streaked brown hair, hazel eyes, forties.",
        "archetype": "indebted night clerk",
        "pitch": "Keeps the vault keys and fears his creditors.",
        "personality": "Fussy, pedantic clerk mask cracking into sweaty desperation; wants his chit burned; adjusts spectacles constantly.",
        "role": "informant", "encounter_type": "location_bound",
        "location_region": "Harborside",
    }]}))

    result = asyncio.run(backend.on_librarian(state, sdk))
    mod_out = result["module_data"]["wb_npc_system"]

    # Plot Director threads were borrowed WITHOUT spending an _update_story_threads LLM call!
    assert not any("Extract 3-5 active story threads" in p for p in calls["prompts"])
    assert any("Break into the customs vault" in t["text"] for t in mod_out["story_threads"])

    gen_prompt = calls["prompts"][-1]
    assert "Disgraced Admiralty Cipher-Clerk" in gen_prompt
    assert "Lockpicking" in gen_prompt
    assert "Day 3" in gen_prompt
    assert "turning arrogant officials into informants (high, role: actor)" in gen_prompt
    assert "being blackmailed or stripped of agency (high, role: target)" in gen_prompt
    assert "Reclaim Aria's cleared name" in gen_prompt
    assert "A missing ledger with silver clasps" in gen_prompt
    assert "Player loves verbal cat-and-mouse" in gen_prompt


def test_introduction_pass_stands_down_when_plot_director_is_holding():
    backend = _load_backend()
    bank = {"npc_1": {
        "id": "npc_1", "name": "Sela", "race": "human", "gender": "female",
        "archetype": "informant", "pitch": "Sela hears everything.",
        "personality": "wry and watchful", "role": "informant", "encounter_type": "encounter",
        "introduced": False, "status": "unintroduced",
    }}
    sdk, calls = _make_sdk(json.dumps({"introduce": True, "npc_id": "npc_1", "reason": "enter"}))

    # 1. Withhold mode during intimate/private beat -> 0 LLM calls, no introduction
    state_withhold = _state(bank)
    state_withhold["module_configs"]["wb_plot_director"] = {"plot_enabled": True}
    state_withhold["module_data"]["wb_plot_director"] = {
        "status": "active",
        "thread": {"status": "active", "title": "Port intrigue"},
        "last_assessment": {"turn": state_withhold["turn"], "scene_mode": "withhold"},
    }
    assert asyncio.run(backend._introduction_pass(state_withhold, sdk)) is None
    assert calls["generate_count"] == 0

    # 2. Deferred climax streak -> 0 LLM calls, no introduction
    state_defer = _state(bank)
    state_defer["module_configs"]["wb_plot_director"] = {"plot_enabled": True}
    state_defer["module_data"]["wb_plot_director"] = {"status": "idle", "defer_streak": 2}
    assert asyncio.run(backend._introduction_pass(state_defer, sdk)) is None
    assert calls["generate_count"] == 0

    # 3. Post-thread quiet period -> 0 LLM calls, no introduction
    state_quiet = _state(bank)
    state_quiet["turn"] = 6
    state_quiet["module_configs"]["wb_plot_director"] = {"plot_enabled": True}
    state_quiet["module_data"]["wb_plot_director"] = {
        "status": "idle",
        "thread": None,
        "last_closed_turn": 5,
        "next_thread_turn": 8,
    }
    assert asyncio.run(backend._introduction_pass(state_quiet, sdk)) is None
    assert calls["generate_count"] == 0


def test_librarian_respects_plot_observation_and_rapid_dialogue_hold():
    backend = _load_backend()
    gen_payload = json.dumps({"npcs": [{
        "name": "Voss", "race": "human", "gender": "male",
        "appearance": "Grey hair, blue eyes, fifties.",
        "archetype": "captain", "pitch": "Commands a brig.",
        "personality": "Crisp demeanor over homesick exhaustion.",
        "role": "ally", "encounter_type": "encounter",
    }]})

    # A. Plot Director still in initial observation window -> no NPC generation
    sdk_obs, obs_calls = _make_sdk(lambda p: "[]" if "story threads" in p else gen_payload)
    state_obs = _state(mutation_config={"generator_frequency": 3, "demand_driven_generation": False})
    state_obs["turn"] = 3
    state_obs["module_configs"]["wb_plot_director"] = {"plot_enabled": True, "observation_turns": 5}
    state_obs["module_data"]["wb_plot_director"] = {"status": "observing", "profile": {}}
    res_obs = asyncio.run(backend.on_librarian(state_obs, sdk_obs))
    assert not res_obs or not res_obs.get("module_data", {}).get("wb_npc_system", {}).get("characters")
    assert not any("Create" in p and "NPC concepts" in p for p in obs_calls["prompts"])

    # B. Rapid-fire dialogue (<30 in-world minutes since last_gen_minutes) holds generation...
    sdk_hold, hold_calls = _make_sdk(lambda p: "[]" if "story threads" in p else gen_payload)
    state_hold = _state(mutation_config={
        "generator_frequency": 3, "demand_driven_generation": False, "fit_check_enabled": False,
    })
    state_hold["turn"] = 6
    state_hold["module_data"]["wb_npc_system"]["last_gen_turn"] = 3
    state_hold["module_data"]["wb_npc_system"]["last_gen_minutes"] = 600
    state_hold["module_data"]["wb_time_tracker"] = {"clock": {"total_minutes_elapsed": 612}}
    res_hold = asyncio.run(backend.on_librarian(state_hold, sdk_hold))
    assert not res_hold or not res_hold.get("module_data", {}).get("wb_npc_system", {}).get("characters")
    assert not any("NPC concepts" in p for p in hold_calls["prompts"])

    # ...until the 2x turn cap (turn - last_gen_turn >= 2 * frequency) overrides the hold.
    state_hold["turn"] = 9
    res_cap = asyncio.run(backend.on_librarian(state_hold, sdk_hold))
    chars = _bank_from_result(res_cap)
    assert any(n["name"] == "Voss" for n in chars.values())


def test_fit_check_rejects_role_inversion_retries_and_filters_rejected_names():
    backend = _load_backend()
    gen_attempt = {"n": 0}

    def router(prompt):
        if "active story threads" in prompt:
            return "[]"
        if "continuity and casting editor" in prompt:
            if "Inquisitor Malakor" in prompt and "Lady Sera" not in prompt:
                return json.dumps({
                    "verdict": "reject",
                    "rejected_names": ["Inquisitor Malakor"],
                    "critique": "Inverts player's actor role by scripting Aria as helpless captive target.",
                })
            if "BadClone" in prompt:
                return json.dumps({
                    "verdict": "reject",
                    "rejected_names": ["BadClone"],
                    "critique": "BadClone duplicates existing cast; Lady Sera is great.",
                })
            return json.dumps({"verdict": "accept", "rejected_names": [], "critique": ""})
        gen_attempt["n"] += 1
        if gen_attempt["n"] == 1:
            return json.dumps({"npcs": [{
                "name": "Inquisitor Malakor", "race": "human", "gender": "male",
                "appearance": "Black hair, pale eyes, forties.",
                "archetype": "captor", "pitch": "Collars the player in irons.",
                "personality": "Cruel zealot.", "role": "antagonist", "encounter_type": "encounter",
            }]})
        # Second attempt includes critique and returns 2 candidates, 1 good + 1 rejected selectively
        assert "PREVIOUS DRAFT REJECTED" in prompt
        assert "Inverts player's actor role" in prompt
        return json.dumps({"npcs": [
            {
                "name": "Lady Sera", "race": "elf", "gender": "female",
                "appearance": "Blonde hair, green eyes, thirties.",
                "archetype": "haughty envoy", "pitch": "Rival diplomat ripe to be outmaneuvered.",
                "personality": "Icy courtly poise hiding terror of scandal; wants treaty credit; snaps her fan shut.",
                "role": "rival", "encounter_type": "encounter",
            },
            {
                "name": "BadClone", "race": "human", "gender": "male",
                "appearance": "Brown hair, brown eyes, twenties.",
                "archetype": "extra", "pitch": "Redundant.",
                "personality": "Flat.", "role": "neutral", "encounter_type": "encounter",
            },
        ]})

    sdk, _ = _make_sdk(router)
    state = _state(mutation_config={
        "generator_frequency": 3,
        "demand_driven_generation": False,
        "fit_check_enabled": True,
    })
    state["turn"] = 3
    result = asyncio.run(backend.on_librarian(state, sdk))
    names = {n["name"] for n in _bank_from_result(result).values()}
    assert names == {"Lady Sera"}
    assert gen_attempt["n"] == 2


def test_model_preferences_route_casting_and_tracking_calls():
    backend = _load_backend()
    prefs = []

    class PrefLLM:
        async def generate(self, prompt, model_preference="balanced", **kw):
            prefs.append((model_preference, prompt))
            if "LASTING way" in prompt:
                return json.dumps({"updates": []})
            if "continuity and casting editor" in prompt:
                return json.dumps({"verdict": "accept", "rejected_names": [], "critique": ""})
            if "active story threads" in prompt:
                return "[]"
            return json.dumps({"npcs": [{
                "name": "Nera", "race": "human", "gender": "female",
                "appearance": "Red hair, grey eyes, twenties.",
                "archetype": "courier", "pitch": "Runs rooftop packets.",
                "personality": "Brisk bravado over fear of heights; wants guild tenure; rolls knuckles.",
                "role": "ally", "encounter_type": "encounter",
            }]})

    sdk, _ = _make_sdk()
    sdk.llm = PrefLLM()
    bank = {"npc_1": _present_npc("npc_1", "Mara")}
    state = _tracking_state(bank, "Mara checks the watchtower.", mutation_config={
        "generator_frequency": 5,
        "demand_driven_generation": False,
        "casting_ai_model": "smartest",
        "tracking_ai_model": "fastest",
    })
    state["turn"] = 5
    asyncio.run(backend.on_librarian(state, sdk))

    gen_prefs = [m for m, p in prefs if "NPC concepts" in p]
    fit_prefs = [m for m, p in prefs if "continuity and casting editor" in p]
    track_prefs = [m for m, p in prefs if "LASTING way" in p]
    assert gen_prefs == ["smartest"]
    assert fit_prefs == ["fastest"]
    assert track_prefs == ["fastest"]


def test_untruncated_notes_story_threads_and_manual_update_context():
    backend = _load_backend()
    long_note_prefix = "EARLY_NOTE_FACT: Mara secretly carries the signet of House Vane. "
    long_notes = long_note_prefix + ("x" * 900) + " LATE_NOTE_FACT"
    npc = _present_npc("npc_1", "Mara", notes=long_notes)
    state = _state({"npc_1": npc})

    # 1. _present_characters_context does not slice notes[-400:]
    ctx = backend._present_characters_context(state, [npc])
    assert "EARLY_NOTE_FACT" in ctx and "LATE_NOTE_FACT" in ctx

    # 2. _update_story_threads does not slice recent[:2000]
    early_thread_marker = "EARLY_THREAD_MARKER: The Obsidian Fleet blockaded Saltreach."
    state["history"] = [
        early_thread_marker + (" " + "word" * 700),
        "LATE_THREAD_MARKER: Aria arrived at the docks.",
    ]
    sdk_threads, thread_calls = _make_sdk("[]")
    asyncio.run(backend._update_story_threads(state, sdk_threads))
    assert "EARLY_THREAD_MARKER" in thread_calls["prompts"][-1]
    assert "LATE_THREAD_MARKER" in thread_calls["prompts"][-1]

    # 3. /npc update does not slice story[-10000:]
    early_story_marker = "CHAPTER_ONE_MARKER: Mara swore the blood-oath at Dawnspire."
    state["history"] = [
        early_story_marker + (" " + "scene" * 2600),
        "CHAPTER_TEN_MARKER: Mara stands at the harbor gate.",
    ]
    sdk_upd, upd_calls = _make_sdk("{}")
    asyncio.run(backend.on_command_npc(["update", "npc_1"], state, sdk_upd))
    assert "CHAPTER_ONE_MARKER" in upd_calls["prompts"][-1]
    assert "CHAPTER_TEN_MARKER" in upd_calls["prompts"][-1]


def test_background_runner_defers_live_turns_patches_snapshots_and_cancels_on_command(tmp_path):
    import zipfile

    backend = _load_backend()
    save_id = "save_bg_npc"
    save_dir = tmp_path / save_id
    snap_dir = save_dir / "Snapshots"
    swipe_dir = save_dir / "Swipes"
    snap_dir.mkdir(parents=True)
    swipe_dir.mkdir(parents=True)

    initial_state = _state(
        mutation_config={
            "generator_frequency": 3,
            "demand_driven_generation": False,
            "fit_check_enabled": False,
            "casting_iterations": 1,
        },
        history=["Aria steps onto the rainy pier."],
    )
    initial_state["turn"] = 3
    initial_state["active_save_id"] = save_id

    for zpath in (snap_dir / "turn_3.zip", swipe_dir / "v0.zip"):
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("Module_States/wb_npc_system.json", json.dumps({"characters": {}}))
    (swipe_dir / "manifest.json").write_text(
        json.dumps({"turn": 3, "active": 0}), encoding="utf-8"
    )

    persisted = {"state": json.loads(json.dumps(initial_state)), "saved_turns": []}

    class FakeSaveManager:
        saves_dir = tmp_path

        def save_turn(self, sid, st, turn_num):
            persisted["saved_turns"].append((sid, turn_num))
            with zipfile.ZipFile(snap_dir / f"turn_{turn_num}.zip", "w") as zf:
                zf.writestr(
                    "Module_States/wb_npc_system.json",
                    json.dumps(st["module_data"]["wb_npc_system"]),
                )

    class FakeSessionManager:
        active_save_id = save_id
        save_manager = FakeSaveManager()

        @property
        def state(self):
            return persisted["state"]

    class FakeEngine:
        _turn_hooks_armed = True

        @staticmethod
        def _deep_merge(base, updates):
            out = dict(base or {})
            for k, v in (updates or {}).items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = FakeEngine._deep_merge(out[k], v)
                else:
                    out[k] = v
            return out

    backend.set_services({
        "session_manager": FakeSessionManager(),
        "engine": FakeEngine(),
    })

    gen_reply = json.dumps({"npcs": [{
        "name": "Dorian", "race": "human", "gender": "male",
        "appearance": "Dark curls, grey eyes, thirties.",
        "archetype": "harbor pilot", "pitch": "Knows the reef channels.",
        "personality": "Easygoing whistle hiding survivor's guilt; wants his ship salvaged.",
        "role": "ally", "encounter_type": "encounter",
    }]})
    sdk, calls = _make_sdk(lambda p: "[]" if "story threads" in p else gen_reply)

    try:
        async def run_flow():
            # During live turn: on_librarian defers LLM work with 0 blocking LLM calls
            imm = await backend.on_librarian(initial_state, sdk)
            assert imm is None
            assert calls["generate_count"] == 0

            # Turn finishes -> spawns background task and persists into state + snapshot + swipe zip
            await backend.on_turn_stopped(initial_state, sdk, "completed")
            await asyncio.gather(*list(backend._bg_tasks), return_exceptions=True)

        asyncio.run(run_flow())

        live_chars = persisted["state"]["module_data"]["wb_npc_system"]["characters"]
        assert any(n["name"] == "Dorian" for n in live_chars.values())
        assert persisted["saved_turns"] == [(save_id, 3)]

        for zpath in (snap_dir / "turn_3.zip", swipe_dir / "v0.zip"):
            with zipfile.ZipFile(zpath, "r") as zf:
                snap_mod = json.loads(zf.read("Module_States/wb_npc_system.json").decode("utf-8"))
            snap_chars = snap_mod["characters"]
            assert any(n["name"] == "Dorian" for n in snap_chars.values())

        # Manual /npc command cancels any in-flight background task cleanly
        async def run_cancel():
            gate = asyncio.Event()

            async def slow_gen(prompt, **kw):
                await gate.wait()
                return "{}"

            slow_sdk, _ = _make_sdk(slow_gen)
            persisted["state"]["turn"] = 6
            state_t6 = json.loads(json.dumps(persisted["state"]))
            await backend.on_librarian(state_t6, slow_sdk)
            await backend.on_turn_stopped(state_t6, slow_sdk, "completed")
            assert len(backend._bg_tasks) == 1
            epoch_before = backend._bg_epoch
            await backend.on_command_npc(["delete", "npc_nonexistent"], state_t6, sdk)
            assert backend._bg_epoch > epoch_before
            gate.set()
            await asyncio.sleep(0.02)

        asyncio.run(run_cancel())
    finally:
        backend._cancel_bg_task()
        backend.set_services({})


