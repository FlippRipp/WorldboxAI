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


def _state(effects=None, clock=None, input_text=""):
    module_data = {"wb_core_rpg": {"status_effects": effects or []}}
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
        "duration_turns": None,
        "expires_at_minutes": None,
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

    # No duration resolved: nothing to tick, the effect just persists.
    assert asyncio.run(backend.on_mutate_state({}, _state(effects), _make_sdk())) == {}


def test_indefinite_effect_persists_untouched():
    backend = _load_backend()
    result = asyncio.run(backend.on_mutate_state({}, _state([_broken_leg()]), _make_sdk()))
    assert result == {}


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

    assert "[bad] Broken leg from the fall." in captured["prompt"]
    assert "[good] Blessed by the hearth-goddess." in captured["prompt"]
    assert "Status effects: weigh them like circumstances" in captured["prompt"]


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
