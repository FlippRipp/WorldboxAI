"""Tests for narrative perspective & tense options and pronoun disambiguation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import backend.api.server as server
from backend.engine.character_builder import CharacterBuilder
from backend.engine.director_shared import setting_material
from backend.engine.graph import EngineGraph
from backend.engine.llm import LLMService
from backend.engine.narrative_voice import (
    input_pronoun_grounding,
    narration_voice_directive,
    resolve_narrative_voice,
)
from backend.engine.prompt_pipeline import (
    DEFAULT_CONTINUE_PROMPT,
    PromptCompiler,
    build_auto_player_action_prompt,
    render_story_style,
)
from backend.engine.registry import ModuleRegistry
from backend.engine.scenario import ScenarioStore
from backend.engine.session import GameSessionManager
from backend.engine.settings_registry import SettingsRegistry


def _make_settings(perspective="second_person", tense="present"):
    reg = SettingsRegistry()
    reg.register("narrative.perspective", "select", perspective, is_global=True,
                 options=["second_person", "first_person", "third_person"])
    reg.register("narrative.tense", "select", tense, is_global=True,
                 options=["present", "past"])
    return reg


def test_voice_resolution_hierarchy():
    settings = _make_settings(perspective="third_person", tense="past")

    # 1. Global setting applies when story_style and override are empty.
    v = resolve_narrative_voice({"story_style": {}}, settings=settings)
    assert v["perspective"] == "third_person"
    assert v["tense"] == "past"

    # 2. Per-save story_style overrides global setting.
    state = {
        "characters": {"default_player": {"name": "Kaelen"}},
        "story_style": {"perspective": "first_person", "tense": ""},
    }
    v2 = resolve_narrative_voice(state, settings=settings)
    assert v2 == {"perspective": "first_person", "tense": "past", "player_name": "Kaelen"}

    # 3. Explicit style_override outranks state and global setting.
    v3 = resolve_narrative_voice(
        state,
        settings=settings,
        style_override={"perspective": "second_person", "tense": "present"},
    )
    assert v3 == {"perspective": "second_person", "tense": "present", "player_name": "Kaelen"}


def test_narration_and_pronoun_grounding_cover_all_perspectives_and_tenses():
    for pov, expected_pov in (
        ("second_person", "second person"),
        ("first_person", "first person"),
        ("third_person", "third person limited"),
    ):
        for tense, expected_tense in (("present", "present tense"), ("past", "past tense")):
            directive = narration_voice_directive(pov, tense, "Lyra")
            assert expected_pov in directive
            assert expected_tense in directive
            assert "Lyra" in directive
            assert "you remember your memories" in directive
            assert "never to you, the AI" in directive

            grounding = input_pronoun_grounding(pov, tense, "Lyra")
            assert expected_tense in grounding
            assert "you remember your memories" in grounding
            assert "NEVER to you (the AI)" in grounding


def test_prompt_compiler_macros_and_story_style_voice():
    settings = _make_settings(perspective="first_person", tense="past")
    compiler = PromptCompiler(settings=settings)
    state = {
        "input_text": "",
        "characters": {"default_player": {"name": "Vael"}},
        "story_style": {},
        "chat_messages": [{"role": "ai", "content": "I woke in the cold cell."}],
    }
    compiled = compiler.compile(state)
    # Continue prompt expands ${narrative_perspective} and ${narrative_tense}.
    cont = compiler.resolve_macros(DEFAULT_CONTINUE_PROMPT, state)
    assert "first person, past tense" in cont
    assert "Vael" in cont

    # Depth-0 story_style block carries the resolved perspective, tense, and pronoun rule.
    last_msg = compiled["messages"][-1]
    assert last_msg["role"] == "system"
    assert "<story_style>" in last_msg["content"]
    assert "first person, past tense" in last_msg["content"]
    assert "you remember your memories" in last_msg["content"]

    # Explicit story_style override wins over global settings.
    override_text = render_story_style(
        {"perspective": "third_person", "tense": "present"},
        settings=settings,
        player_name="Vael",
    )
    assert "third person limited, present tense" in override_text


def test_modify_scenario_respects_perspective_tense_and_second_person_pronouns():
    engine = object.__new__(EngineGraph)
    engine.settings = _make_settings(perspective="second_person", tense="past")
    engine.sdk = SimpleNamespace(ui=SimpleNamespace(
        emit_status=AsyncMock(), emit_reasoning_token=AsyncMock(),
    ))
    engine.llm = SimpleNamespace(
        simple_completion=AsyncMock(return_value='{"scenario_description": "The protagonist retains all past memories."}'),
        generate_story_from_messages=AsyncMock(return_value={
            "content": "You opened your eyes, every memory intact.", "reasoning": "",
        }),
    )

    state = {
        "characters": {"default_player": {"name": "Alden"}},
        "story_style": {"perspective": "second_person", "tense": "past"},
        "scenario_data": {
            "scenario_description": "An amnesiac awakens in a ruined shrine.",
            "starting_prompt": "You wake on cold stone with no memory of your name.",
            "pending_modification_request": "you remember your memories",
            "revision_mode": "flexible",
        },
    }

    result = asyncio.run(engine.generate_intro(state))
    assert result["content"] == "You opened your eyes, every memory intact."

    desc_msgs = engine.llm.simple_completion.call_args.args[0]
    assert "Alden (the player character)" in desc_msgs[0]["content"]
    assert "you remember your memories" in desc_msgs[0]["content"]
    assert "NEVER to you (the AI)" in desc_msgs[0]["content"]
    assert "<requested_changes>\nyou remember your memories\n</requested_changes>" in desc_msgs[1]["content"]

    open_msgs = engine.llm.generate_story_from_messages.call_args.args[0]
    assert "second person, past tense" in open_msgs[0]["content"]
    assert "Alden (the player character)" in open_msgs[0]["content"]
    assert "you remember your memories" in open_msgs[0]["content"]


def test_character_creator_grounds_second_person_inputs_to_character():
    captured = []

    class FakeLLM:
        reader_model = "mock-reader"

        async def simple_completion(self, messages, **kwargs):
            captured.append(messages)
            if len(messages) == 2:
                return '{"stats": {"power": 10, "agility": 11, "vitality": 10, "intelligence": 16, "spirit": 15, "charm": 10}, "skills": {"past_life_recall": {"type": "passive", "rating": 6, "description": "Retains memories of a prior life.", "trigger_words": []}}, "backstory": "Alden carries intact memories across lifetimes."}'
            return "Alden has sharp silver eyes and a faint crescent scar."

    cb = CharacterBuilder(characters_dir="data/characters")
    cb.set_llm_service(FakeLLM())
    cb.set_settings(_make_settings(perspective="second_person", tense="present"))

    app_res = asyncio.run(cb.generate_full_appearance(
        "you have silver eyes and a crescent scar on your cheek",
        name="Alden",
    ))
    assert "Alden" in app_res["full_appearance"]
    app_prompt = captured[-1][0]["content"]
    assert "the character being created" in app_prompt
    assert "NEVER to you (the AI)" in app_prompt
    assert "third-person physical description" in app_prompt

    stats_res = asyncio.run(cb.generate_stats(
        "you remember your memories from your previous life",
        name="Alden",
    ))
    assert "past_life_recall" in stats_res["skills"]
    stats_user_prompt = captured[-1][1]["content"]
    assert "you remember your memories" in stats_user_prompt
    assert "Alden (the character being created)" in stats_user_prompt
    assert "writing the JSON output fields themselves in third person" in stats_user_prompt


def test_scenario_rewrite_and_create_save_api_voice_overrides(tmp_path, monkeypatch):
    session_manager = GameSessionManager(str(tmp_path / "data"), settings=server.backend_settings)
    monkeypatch.setattr(server, "session_manager", session_manager)
    store = ScenarioStore(str(tmp_path / "data"))
    monkeypatch.setattr(server, "scenario_store", store)
    server.engine.llm.mode = "mock"
    client = TestClient(server.app)

    seen = {}

    async def fake_completion(messages, **kwargs):
        seen["messages"] = messages
        return '{"text": "I stepped out into the rain."}'

    monkeypatch.setattr(server.engine.llm, "simple_completion", fake_completion)

    res = client.post("/api/scenarios/rewrite-prompt", json={
        "request": "you remember your memories",
        "current_text": "You step out into the rain.",
        "field": "starting_prompt",
        "perspective": "first_person",
        "tense": "past",
    })
    assert res.status_code == 200
    sys_content = seen["messages"][0]["content"]
    assert "first person, past tense" in sys_content
    assert "never to you, the AI" in sys_content

    # Create a scenario with third_person + past, then override tense at save creation.
    sc_res = client.post("/api/scenarios", json={
        "name": "Chronicle",
        "scenario_description": "An ancient kingdom.",
        "perspective": "third_person",
        "tense": "past",
    })
    assert sc_res.status_code == 200
    sc_id = sc_res.json()["scenario"]["id"]

    save_res = client.post("/api/saves", json={
        "save_id": "chronicle_run",
        "scenario_id": sc_id,
        "tense": "present",
    })
    assert save_res.status_code == 200
    assert session_manager.state["story_style"]["perspective"] == "third_person"
    assert session_manager.state["story_style"]["tense"] == "present"


def test_auto_player_reader_librarian_and_director_shared_voice():
    state = {
        "characters": {"default_player": {"name": "Sera"}},
        "story_style": {"perspective": "first_person", "tense": "past"},
        "history": ["I drew my cloak tight against the ash."],
    }
    auto_prompt = build_auto_player_action_prompt(state, nudge="you remember your memories")
    assert 'the "I" in the narration is this character' in auto_prompt
    assert "first person, past tense" in auto_prompt
    assert "Sera (the player character)" in auto_prompt

    # Director shared material carries <narrative_voice> for Plot Director & NPC System.
    mat = "\n".join(setting_material(state))
    assert "<narrative_voice>" in mat
    assert "first person, past tense" in mat
    assert "Sera (the player character)" in mat

    # Module state built by EngineGraph automatically carries resolved narrative_voice.
    reg = ModuleRegistry("modules")
    reg.load_all_modules()
    engine = EngineGraph(reg)
    mod_state = engine._build_module_state(state, "wb_core_rpg", {"state": []})
    assert mod_state["narrative_voice"] == {
        "perspective": "first_person",
        "tense": "past",
        "player_name": "Sera",
    }

    # LLMService embeds narrative_voice_note in reader and librarian prompts.
    svc = LLMService(mode="live")
    captured = []

    async def fake_simple(messages, **kwargs):
        captured.append(messages)
        return "{}"

    svc.simple_completion = fake_simple
    svc.set_narrative_voice_note("Pronoun grounding: Sera is the player character.")
    asyncio.run(svc.extract_mutations("I opened the chest.", {"wb_core_rpg": {"hp_change": "int"}}))
    assert "Pronoun grounding: Sera is the player character." in captured[-1][1]["content"]
