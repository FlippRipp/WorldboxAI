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
    assert npc["personality"] == ["grim", "loyal", "quiet"]


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
