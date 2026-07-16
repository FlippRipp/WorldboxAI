"""Tests for the RPG module's customizable instruction slots: the directive
split in each prompt builder, the override lookup, and the slot contract."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend_directives", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SLOT_IDS = [
    "action_assessment",
    "xp_judgment",
    "skill_categories",
    "skill_options",
    "skill_refine",
    "evolution_options",
    "evolve",
]


def _rpg():
    return {
        "level": 5,
        "stats": {s: 10 for s in ["power", "agility", "vitality", "intelligence", "spirit", "charm"]},
        "backstory": "A wandering duelist.",
        "skills": {
            "swordplay": {"rating": 10, "description": "Master duelist.", "trigger_words": ["slash"], "type": "active"},
        },
    }


def _state():
    return {
        "world_data": {"rules": {"genre": "Dark Fantasy"}},
        "story_style": {"themes": "redemption", "tags": "gritty"},
        "history": ["Scene one.", "Scene two."],
        "module_data": {},
    }


CUSTOM = "Only propose things related to competitive cooking."


# ---------------------------------------------------------------------------
# slot contract
# ---------------------------------------------------------------------------

def test_instruction_slots_shape():
    mod = _load_backend()
    slots = mod.get_instruction_slots()
    assert [s["id"] for s in slots] == SLOT_IDS
    for slot in slots:
        assert slot["label"].strip()
        assert slot["description"].strip()
        assert slot["default"].strip()


def test_instruction_slots_returns_copies():
    mod = _load_backend()
    slots = mod.get_instruction_slots()
    slots[0]["default"] = "tampered"
    assert mod.get_instruction_slots()[0]["default"] != "tampered"


def test_directive_falls_back_to_default():
    mod = _load_backend()
    default = mod._SLOT_DEFAULTS["skill_categories"]
    assert mod._directive("skill_categories", None) == default
    assert mod._directive("skill_categories", {}) == default
    assert mod._directive("skill_categories", {"skill_categories": ""}) == default
    assert mod._directive("skill_categories", {"skill_categories": "   "}) == default
    assert mod._directive("skill_categories", {"other_slot": CUSTOM}) == default
    assert mod._directive("skill_categories", {"skill_categories": CUSTOM}) == CUSTOM


# ---------------------------------------------------------------------------
# each builder: default directive present by default, replaced by an override,
# fixed scaffolding (counts + JSON contract) intact either way
# ---------------------------------------------------------------------------

def _check(default_prompt, custom_prompt, default_directive, scaffolding):
    assert default_directive in default_prompt
    assert CUSTOM not in default_prompt
    assert CUSTOM in custom_prompt
    assert default_directive not in custom_prompt
    for marker in scaffolding:
        assert marker in default_prompt
        assert marker in custom_prompt


def test_skill_categories_prompt_directive():
    mod = _load_backend()
    _check(
        mod._skill_categories_prompt(_rpg(), _state()),
        mod._skill_categories_prompt(_rpg(), _state(), instructions={"skill_categories": CUSTOM}),
        mod.DIRECTIVE_SKILL_CATEGORIES,
        ["Propose exactly 10 skill categories", '{"categories":', "Output ONLY valid JSON"],
    )


def test_skill_options_prompt_directive():
    mod = _load_backend()
    _check(
        mod._skill_options_prompt(_rpg(), "Flame Arts", ["swordplay"], _state()),
        mod._skill_options_prompt(_rpg(), "Flame Arts", ["swordplay"], _state(),
                                  instructions={"skill_options": CUSTOM}),
        mod.DIRECTIVE_SKILL_OPTIONS,
        ["exactly 5 NEW skills", '{"skills":', "swordplay", "decided later by fate"],
    )


def test_skill_refine_prompt_directive():
    mod = _load_backend()
    draft = {"name": "Ember Feint", "type": "active", "description": "Draft.", "trigger_words": ["feint"], "strength": 7}
    _check(
        mod._skill_refine_prompt(_rpg(), draft, "Flame Arts", _state()),
        mod._skill_refine_prompt(_rpg(), draft, "Flame Arts", _state(),
                                 instructions={"skill_refine": CUSTOM}),
        mod.DIRECTIVE_SKILL_REFINE,
        ["Refine this skill. Requirements:", "rarity: Rare (7/10", '{"name":', "Trigger words: 2-5 short words"],
    )


def test_evolution_options_prompt_directive():
    mod = _load_backend()
    data = _rpg()["skills"]["swordplay"]
    _check(
        mod._evolution_options_prompt(_rpg(), "swordplay", data, _state()),
        mod._evolution_options_prompt(_rpg(), "swordplay", data, _state(),
                                      instructions={"evolution_options": CUSTOM}),
        mod.DIRECTIVE_EVOLUTION_OPTIONS,
        ["Propose exactly 4 evolution paths.", "JSON response (pure path first):", '{"options":'],
    )


def test_evolve_prompt_directive():
    mod = _load_backend()
    data = _rpg()["skills"]["swordplay"]
    _check(
        mod._evolve_prompt(_rpg(), "swordplay", data, "Brutal", _state()),
        mod._evolve_prompt(_rpg(), "swordplay", data, "Brutal", _state(),
                           instructions={"evolve": CUSTOM}),
        mod.DIRECTIVE_EVOLVE,
        ['embody the "Brutal" theme', "new evocative name of 2-4 words", '{"name":'],
    )


def test_xp_judge_prompt_directive():
    mod = _load_backend()
    assessment = {"feasibility": 7, "difficulty": "hard"}
    _check(
        mod._xp_judge_prompt(_rpg(), "I leap the chasm", assessment, _state()),
        mod._xp_judge_prompt(_rpg(), "I leap the chasm", assessment, _state(),
                             instructions={"xp_judgment": CUSTOM}),
        mod.DIRECTIVE_XP_JUDGMENT,
        ["You are the XP judge", "I leap the chasm", "difficulty hard", '{"xp_deserved":'],
    )


def test_evolve_prompt_directive_pure_path():
    mod = _load_backend()
    data = _rpg()["skills"]["swordplay"]
    prompt = mod._evolve_prompt(_rpg(), "swordplay", data, "Perfected", _state(), pure=True,
                                instructions={"evolve": CUSTOM})
    # The pure-path theme requirement is scaffolding and survives an override.
    assert "This is the PURE path" in prompt
    assert CUSTOM in prompt


# ---------------------------------------------------------------------------
# action assessment: directive reaches the prompt, from the hook's state key
# ---------------------------------------------------------------------------

def _capture_llm(captured):
    async def generate(prompt, model_preference="balanced", max_tokens=None):
        captured.append(prompt)
        return '{"skip": true}'
    return SimpleNamespace(llm=SimpleNamespace(generate=generate))


def test_assess_action_prompt_directive():
    mod = _load_backend()
    char = mod.Character.from_dict(_rpg())
    XP_CUSTOM = "Only social maneuvering earns success; combat is always hopeless."

    for instructions, expect_custom in ((None, False), ({"action_assessment": XP_CUSTOM}, True)):
        captured = []
        asyncio.run(mod._assess_action(
            "I leap the chasm", char, {}, _capture_llm(captured), instructions=instructions,
        ))
        prompt = captured[0]
        assert ("Judging guidelines:" in prompt)
        assert ('"feasibility": int 1-10' in prompt)  # JSON contract is fixed
        assert ("Difficulty is set to" in prompt)      # dynamic difficulty bullet is fixed
        if expect_custom:
            assert XP_CUSTOM in prompt
            assert mod.DIRECTIVE_ACTION_ASSESSMENT not in prompt
        else:
            assert mod.DIRECTIVE_ACTION_ASSESSMENT in prompt


def test_on_gather_context_threads_module_instructions():
    """The engine injects overrides as state["module_instructions"]; the
    gather-context hook must pass them into the assessment prompt."""
    mod = _load_backend()
    XP_CUSTOM = "Judge every action as if gravity were optional."
    captured = []
    state = {
        "input_text": "I leap the chasm",
        "module_data": {"wb_core_rpg": _rpg()},
        "module_configs": {"wb_core_rpg": {}},
        "module_instructions": {"action_assessment": XP_CUSTOM},
        "history": [],
    }
    asyncio.run(mod.on_gather_context(state, _capture_llm(captured)))
    assert XP_CUSTOM in captured[0]
    assert mod.DIRECTIVE_ACTION_ASSESSMENT not in captured[0]
