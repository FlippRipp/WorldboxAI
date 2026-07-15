import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_sdk(reply: str = "{}", captured: dict | None = None):
    async def generate(prompt, model_preference="balanced", max_tokens=None):
        if captured is not None:
            captured["prompt"] = prompt
        return reply

    return SimpleNamespace(llm=SimpleNamespace(generate=generate, _current_module=""))


def _state(effects=None, clock=None, input_text="", skills=None):
    module_data = {"wb_core_rpg": {"status_effects": effects or [], "skills": skills or {}}}
    if clock is not None:
        module_data["wb_time_tracker"] = {"clock": {"total_minutes_elapsed": clock}}
    return {
        "turn": 3,
        "history": ["The fight ends."],
        "input_text": input_text,
        "module_configs": {},
        "module_data": module_data,
    }


def _effects_of(result):
    return result["module_data"]["wb_core_rpg"]["status_effects"]


def _broken_leg(**overrides):
    effect = {
        "name": "broken leg",
        "description": "Broken leg from the fall.",
        "kind": "bad",
        "severity": 3,
        "duration_turns": None,
        "expires_at_minutes": None,
        "turns_active": 0,
    }
    effect.update(overrides)
    return effect


def test_gained_effect_is_recorded_and_not_ticked_on_the_gain_turn():
    backend = _load_backend()
    mutation = {"status_effects_gained": [
        {"name": "Blessed", "description": "Blessed by the hearth-goddess.", "kind": "good", "duration_turns": 1},
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(), _make_sdk()))

    effects = _effects_of(result)
    assert len(effects) == 1
    assert effects[0]["name"] == "blessed"
    assert effects[0]["kind"] == "good"
    assert effects[0]["description"] == "Blessed by the hearth-goddess."
    # A 1-turn effect must survive its gain turn so it sways exactly one action.
    assert effects[0]["duration_turns"] == 1


def test_invalid_kind_defaults_to_bad():
    backend = _load_backend()
    mutation = {"status_effects_gained": [{"name": "Dazed", "description": "Dazed.", "kind": "debuff"}]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(), _make_sdk()))

    assert _effects_of(result)[0]["kind"] == "bad"


def test_turn_durations_tick_and_the_effect_wears_off():
    backend = _load_backend()

    result = asyncio.run(backend.on_mutate_state({}, _state([_broken_leg(duration_turns=2)]), _make_sdk()))
    effects = _effects_of(result)
    assert effects[0]["duration_turns"] == 1

    result = asyncio.run(backend.on_mutate_state({}, _state(effects), _make_sdk()))
    assert _effects_of(result) == []


def test_minutes_duration_resolves_against_the_time_tracker_clock():
    backend = _load_backend()
    mutation = {"status_effects_gained": [
        {"name": "Poisoned", "description": "Poisoned by the viper's bite.", "kind": "bad", "duration_minutes": 120},
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(clock=1000), _make_sdk()))
    effects = _effects_of(result)
    assert effects[0]["expires_at_minutes"] == 1120
    assert effects[0]["duration_turns"] is None

    # Clock has not reached the expiry yet: the effect persists.
    result = asyncio.run(backend.on_mutate_state({}, _state(effects, clock=1119), _make_sdk()))
    assert result == {} or len(_effects_of(result)) == 1

    # Clock passed the expiry: the effect is gone.
    result = asyncio.run(backend.on_mutate_state({}, _state(effects, clock=1120), _make_sdk()))
    assert _effects_of(result) == []


def test_minutes_duration_without_a_clock_lasts_until_story_removal():
    backend = _load_backend()
    mutation = {"status_effects_gained": [
        {"name": "Cursed mark", "description": "Marked by the witch.", "kind": "bad", "duration_minutes": 60},
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(), _make_sdk()))
    effects = _effects_of(result)
    assert effects[0]["expires_at_minutes"] is None

    # No duration resolved: the effect persists, only its age advances.
    result = asyncio.run(backend.on_mutate_state({}, _state(effects), _make_sdk()))
    assert _effects_of(result)[0]["name"] == "cursed mark"


def test_indefinite_effect_persists_and_ages():
    backend = _load_backend()
    result = asyncio.run(backend.on_mutate_state({}, _state([_broken_leg()]), _make_sdk()))
    effects = _effects_of(result)
    assert effects[0]["name"] == "broken leg"
    assert effects[0]["turns_active"] == 1


def test_story_removal_is_case_insensitive():
    backend = _load_backend()
    mutation = {"status_effects_removed": ["Broken Leg"]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state([_broken_leg()]), _make_sdk()))

    assert _effects_of(result) == []


def test_regaining_an_effect_refreshes_it_without_duplicating():
    backend = _load_backend()
    mutation = {"status_effects_gained": [
        {"name": "Broken leg", "description": "Re-broken in the scuffle.", "kind": "bad", "duration_turns": 5},
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state([_broken_leg(duration_turns=1)]), _make_sdk()))

    effects = _effects_of(result)
    assert len(effects) == 1
    assert effects[0]["duration_turns"] == 5
    assert effects[0]["description"] == "Re-broken in the scuffle."


def test_feasibility_assessment_sees_status_effects():
    backend = _load_backend()
    captured = {}
    reply = json.dumps({"feasibility": 5, "skill_used": "", "difficulty": "moderate",
                        "curse_triggered": "", "passive_effects": "", "failure_reason": ""})
    sdk = _make_sdk(reply, captured)
    state = _state([_broken_leg(), _broken_leg(name="blessed", description="Blessed by the hearth-goddess.", kind="good")],
                   input_text="I sprint across the courtyard")

    asyncio.run(backend.on_gather_context(state, sdk))

    assert "[bad, severity 3/10] Broken leg from the fall." in captured["prompt"]
    assert "[good, severity 3/10] Blessed by the hearth-goddess." in captured["prompt"]
    assert "Status effects: weigh them like circumstances" in captured["prompt"]


def test_difficulty_tier_reaches_the_prompt_as_a_label_not_a_scale():
    # The judge is told only the named difficulty tier for the configured
    # strictness value - never the raw 1-10 slider or its band ranges.
    backend = _load_backend()
    captured = {}
    reply = json.dumps({"feasibility": 5, "skill_used": "", "difficulty": "moderate",
                        "curse_triggered": "", "passive_effects": "", "failure_reason": ""})
    sdk = _make_sdk(reply, captured)
    state = _state(input_text="I climb the crumbling wall")
    state["module_configs"] = {"wb_core_rpg": {"action_rating_strictness": 10}}

    asyncio.run(backend.on_gather_context(state, sdk))

    assert 'Difficulty is set to "Brutal"' in captured["prompt"]
    assert "success is almost impossible" in captured["prompt"]
    # Brutal has no plain-failure band: every failing score worsens the situation.
    assert "1-5 = the attempt fails and the situation worsens; 6-8 = partial success at a cost; 9-10 = success" in captured["prompt"]
    assert "= the attempt fails;" not in captured["prompt"]
    # Brutal is exempt from the merely-unlikely guardrail the lower tiers carry.
    assert "Never rate a merely unlikely attempt" not in captured["prompt"]
    assert "Strictness" not in captured["prompt"]

    state["module_configs"] = {"wb_core_rpg": {"action_rating_strictness": 1}}
    asyncio.run(backend.on_gather_context(state, sdk))
    assert 'Difficulty is set to "Power Fantasy"' in captured["prompt"]
    # Power Fantasy never worsens a failure: no fails-and-worsens band.
    assert "1 = the attempt fails; 2-4 = partial success at a cost; 5-10 = success" in captured["prompt"]
    assert "the situation worsens" not in captured["prompt"]


def test_brutal_widens_the_failure_band_for_ruling_and_xp():
    backend = _load_backend()
    brutal = {"action_rating_strictness": 10}
    balanced = {"action_rating_strictness": 5}

    def char_at(feasibility):
        return backend.Character.from_dict({"action_assessment": {
            "feasibility": feasibility, "difficulty": "moderate",
            "failure_reason": "the sheer cliff face offers no holds",
        }})

    # Feasibility 5 fails on Brutal - and every Brutal failure worsens the
    # situation - but the same score is a costly partial on Balanced.
    for feasibility in (2, 5):
        ruling = backend._build_action_feasibility_prompt(char_at(feasibility), "I scale the cliff", brutal)
        assert "the attempt fails" in ruling
        assert "the situation worsens" in ruling
    assert "partial success" in backend._build_action_feasibility_prompt(char_at(5), "I scale the cliff", balanced)

    # On Balanced only a 1 worsens; a 2 is a plain failure.
    balanced_two = backend._build_action_feasibility_prompt(char_at(2), "I scale the cliff", balanced)
    assert "the attempt fails" in balanced_two
    assert "the situation worsens" not in balanced_two
    assert "the situation worsens" in backend._build_action_feasibility_prompt(char_at(1), "I scale the cliff", balanced)

    # Success-conditioned XP follows the same band: no reward for a Brutal failure.
    assert backend._xp_from_assessment(char_at(5), brutal) == 0
    assert backend._xp_from_assessment(char_at(5), balanced) > 0

    # On Brutal, outright success starts at 9.
    char8 = backend.Character.from_dict({"action_assessment": {"feasibility": 8, "difficulty": "hard"}})
    char9 = backend.Character.from_dict({"action_assessment": {"feasibility": 9, "difficulty": "hard"}})
    assert "the attempt succeeds" not in backend._build_action_feasibility_prompt(char8, "I strike", brutal)
    assert "the attempt succeeds" in backend._build_action_feasibility_prompt(char9, "I strike", brutal)


def test_character_sheet_lists_afflictions_and_boons():
    backend = _load_backend()
    char = backend.Character.from_dict({
        "hp": 50, "max_hp": 85,
        "status_effects": [
            _broken_leg(),
            _broken_leg(name="blessed", description="Blessed by the hearth-goddess.", kind="good"),
        ],
    })

    sheet = backend._render_character_sheet(char, {})

    assert "Current afflictions: Broken leg from the fall." in sheet
    assert "Current boons: Blessed by the hearth-goddess." in sheet


def test_stats_command_shows_effects_with_duration():
    backend = _load_backend()
    state = _state([
        _broken_leg(duration_turns=2),
        _broken_leg(name="poisoned", expires_at_minutes=1120),
        _broken_leg(name="cursed mark"),
    ], clock=1000)

    result = asyncio.run(backend.on_command_stats([], state, _make_sdk()))

    assert "broken leg [bad, 2 turns left]" in result["message"]
    assert "poisoned [bad, ~2h left]" in result["message"]
    assert "cursed mark [bad, ongoing]" in result["message"]


def test_effects_survive_the_character_round_trip():
    backend = _load_backend()
    effects = [_broken_leg(duration_turns=3), _broken_leg(name="poisoned", expires_at_minutes=500)]

    char = backend.Character.from_dict({"status_effects": effects})
    round_tripped = backend.Character.from_dict(char.to_dict())

    assert round_tripped.status_effects == effects


def test_cap_of_three_and_stronger_overrides_weakest():
    backend = _load_backend()
    full = [
        _broken_leg(name="scraped knee", severity=2),
        _broken_leg(name="poisoned", severity=5),
        _broken_leg(name="dazed", severity=4),
    ]

    # A stronger effect lands by overriding the weakest active one.
    mutation = {"status_effects_gained": [
        {"name": "Deadly venom", "description": "Deadly venom in the veins.", "kind": "bad", "severity": 8},
    ]}
    result = asyncio.run(backend.on_mutate_state(mutation, _state(full), _make_sdk()))
    names = {e["name"] for e in _effects_of(result)}
    assert names == {"poisoned", "dazed", "deadly venom"}

    # A weaker effect is rejected at the cap.
    mutation = {"status_effects_gained": [
        {"name": "Mild headache", "description": "A mild headache.", "kind": "bad", "severity": 1},
    ]}
    result = asyncio.run(backend.on_mutate_state(mutation, _state([_broken_leg(name=n, severity=s) for n, s in
                                                                   [("a", 4), ("b", 5), ("c", 6)]]), _make_sdk()))
    assert "mild headache" not in {e["name"] for e in _effects_of(result)}


def test_refreshing_an_active_effect_at_the_cap_keeps_all_three():
    backend = _load_backend()
    full = [
        _broken_leg(name="poisoned", severity=5, turns_active=4),
        _broken_leg(name="dazed", severity=4),
        _broken_leg(name="cursed mark", severity=6),
    ]
    mutation = {"status_effects_gained": [
        {"name": "Poisoned", "description": "The venom takes fresh hold.", "kind": "bad", "severity": 7},
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(full), _make_sdk()))

    effects = {e["name"]: e for e in _effects_of(result)}
    assert set(effects) == {"poisoned", "dazed", "cursed mark"}
    assert effects["poisoned"]["severity"] == 7
    # A refresh keeps the effect's lingering age.
    assert effects["poisoned"]["turns_active"] == 4


def test_effect_gain_skipped_when_a_same_named_skill_exists():
    backend = _load_backend()
    skills = {"shadow brand": {"rating": 6, "description": "A brand of living shadow.",
                               "trigger_words": [], "type": "curse"}}
    mutation = {"status_effects_gained": [
        {"name": "Shadow Brand", "description": "Branded by shadow.", "kind": "bad", "severity": 6},
    ]}

    result = asyncio.run(backend.on_mutate_state(mutation, _state(skills=skills), _make_sdk()))

    assert all(e["name"] != "shadow brand" for e in _effects_of(result)) if result else True


def test_new_skill_supersedes_a_same_named_status_effect():
    backend = _load_backend()
    mutation = {"skill_changes": {"Iron Grip": {
        "rating": 4, "description": "Crushing grip hardened by the trial.",
        "trigger_words": [], "type": "passive",
    }}}

    result = asyncio.run(backend.on_mutate_state(
        mutation, _state([_broken_leg(name="iron grip", kind="good", severity=4)]), _make_sdk()))

    data = result["module_data"]["wb_core_rpg"]
    assert "iron grip" in data["skills"]
    assert all(e["name"] != "iron grip" for e in data["status_effects"])


def test_librarian_grant_supersedes_effect_and_prompt_lists_effects():
    backend = _load_backend()
    captured = {}
    reply = json.dumps({"added": [{
        "name": "Emberkiss", "rating": 4, "description": "A boon from the hearth-goddess.",
        "trigger_words": [], "type": "active",
    }], "removed": [], "altered": []})
    sdk = _make_sdk(reply, captured)
    state = _state([_broken_leg(name="emberkiss", kind="good", severity=5)])
    state["history"] = ["The goddess makes her blessing permanent."]

    result = asyncio.run(backend.on_librarian(state, sdk))

    assert "emberkiss (good, severity 5): Broken leg from the fall." in captured["prompt"]
    assert "Do NOT report a temporary condition" in captured["prompt"]
    data = result["module_data"]["wb_core_rpg"]
    assert "emberkiss" in data["skills"]
    assert all(e["name"] != "emberkiss" for e in data["status_effects"])


def test_lingering_strong_bad_effect_hardens_into_a_curse():
    backend = _load_backend()
    effect = _broken_leg(name="creeping corruption", severity=8, turns_active=9)

    result = asyncio.run(backend.on_mutate_state({}, _state([effect]), _make_sdk()))

    data = result["module_data"]["wb_core_rpg"]
    assert all(e["name"] != "creeping corruption" for e in data["status_effects"])
    curse = data["skills"]["creeping corruption"]
    assert curse["type"] == "curse"
    assert curse["rating"] == 8


def test_no_curse_promotion_for_good_weak_or_expiring_effects():
    backend = _load_backend()
    effects = [
        _broken_leg(name="divine favor", kind="good", severity=9, turns_active=20),
        _broken_leg(name="dull ache", severity=5, turns_active=20),
        _broken_leg(name="fading venom", severity=9, turns_active=20, duration_turns=5),
    ]

    result = asyncio.run(backend.on_mutate_state({}, _state(effects), _make_sdk()))

    data = result["module_data"]["wb_core_rpg"]
    assert {e["name"] for e in data["status_effects"]} == {"divine favor", "dull ache", "fading venom"}
    assert data["skills"] == {}


def test_mutation_schema_feeds_the_reader_both_lists():
    backend = _load_backend()
    skills = {"emberkiss": {"rating": 4, "description": "", "trigger_words": [], "type": "active"}}
    state = _state([_broken_leg(severity=5)], skills=skills)

    schema = asyncio.run(backend.on_mutation_schema(state, _make_sdk()))

    assert "emberkiss (active)" in schema["status_effects_gained"]
    assert "broken leg (bad, severity 5)" in schema["status_effects_gained"]
    # The dynamic entry re-includes the manifest's base description.
    assert "temporary conditions the PLAYER gained" in schema["status_effects_gained"]
    assert "broken leg (bad, severity 5)" in schema["skill_changes"]
    assert "never duplicate an active status effect's name" in schema["skill_changes"]

    # Nothing to report when the character is blank.
    assert asyncio.run(backend.on_mutation_schema(_state(), _make_sdk())) == {}


def test_legacy_saves_over_the_cap_are_trimmed_to_the_strongest():
    backend = _load_backend()
    effects = [
        _broken_leg(name="a", severity=2),
        _broken_leg(name="b", severity=8),
        _broken_leg(name="c", severity=3),
        _broken_leg(name="d", severity=8),
        _broken_leg(name="e", severity=5),
    ]

    # Strongest kept, ties favoring the earlier (older) entry, order preserved.
    char = backend.Character.from_dict({"status_effects": effects})
    assert [e["name"] for e in char.status_effects] == ["b", "d", "e"]

    # The trim persists through the per-turn round trip.
    result = asyncio.run(backend.on_gather_context(_state(effects), _make_sdk()))
    assert [e["name"] for e in _effects_of(result)] == ["b", "d", "e"]


# ---------------------------------------------------------------------------
# Cheat-gated manual removal endpoint (DELETE /status-effects/{name})
# ---------------------------------------------------------------------------

def _make_client(mod, effects, cheats_enabled=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    saved = []

    def save_turn(save_id, st, turn):
        saved.append({"save_id": save_id, "turn": turn})

    session_manager = SimpleNamespace(
        active_save_id="save1",
        state=_state(effects=effects),
        save_manager=SimpleNamespace(save_turn=save_turn),
    )
    mod.set_services({
        "session_manager": session_manager,
        "settings": {"cheats.enabled": cheats_enabled},
    })
    app = FastAPI()
    app.include_router(mod.get_router(), prefix="/api/modules/wb_core_rpg")
    return TestClient(app), session_manager, saved


EFFECTS_BASE = "/api/modules/wb_core_rpg/status-effects"


def test_cheat_delete_removes_effect_and_persists():
    backend = _load_backend()
    effects = [_broken_leg(), _broken_leg(name="blessed", kind="good")]
    client, sm, saved = _make_client(backend, effects)

    # Case-insensitive match, like the widget's skill endpoints.
    res = client.delete(f"{EFFECTS_BASE}/Broken%20Leg")
    assert res.status_code == 200
    remaining = res.json()["status_effects"]
    assert [e["name"] for e in remaining] == ["blessed"]
    assert sm.state["module_data"]["wb_core_rpg"]["status_effects"] == remaining
    assert saved == [{"save_id": "save1", "turn": 3}]


def test_delete_requires_cheat_mode():
    backend = _load_backend()
    client, sm, saved = _make_client(backend, [_broken_leg()], cheats_enabled=False)

    res = client.delete(f"{EFFECTS_BASE}/broken%20leg")
    assert res.status_code == 403
    # State untouched, nothing persisted.
    assert [e["name"] for e in sm.state["module_data"]["wb_core_rpg"]["status_effects"]] == ["broken leg"]
    assert saved == []


def test_delete_unknown_effect_is_404():
    backend = _load_backend()
    client, _, saved = _make_client(backend, [_broken_leg()])

    res = client.delete(f"{EFFECTS_BASE}/frostbite")
    assert res.status_code == 404
    assert saved == []
