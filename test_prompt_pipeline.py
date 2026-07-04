import asyncio
import os
import tempfile

from backend.engine.graph import EngineGraph
from backend.engine.prompt_pipeline import PromptCompiler, PromptPipelineValidationError
from backend.engine.registry import ModuleRegistry


def test_default_pipeline_compiles_messages():
    compiler = PromptCompiler()
    compiled = compiler.compile({
        "input_text": "I open the iron door.",
        "current_context": ["<room>There is a locked chest.</room>"],
        "chat_messages": [],
    })

    messages = compiled["messages"]
    assert messages[0]["role"] == "system"
    assert "creative storyteller" in messages[0]["content"]
    assert messages[1]["role"] == "system"
    assert "Current Game State:" in messages[1]["content"]
    assert "locked chest" in messages[1]["content"]
    assert messages[-1] == {"role": "user", "content": "I open the iron door."}
    assert [entry["id"] for entry in compiled["trace"]] == [
        "core_narrator_rules",
        "world_rules_context",
        "player_character_context",
        "engine_context",
        "storyteller_task",
    ]
    print("Default prompt pipeline compile test passed.")


def test_chat_injection_depth_and_veto_order():
    compiler = PromptCompiler()
    pipeline = [
        {
            "id": "rules",
            "type": "static_text",
            "source": "user",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "config": {"text": "Narrate tersely."},
        },
        {
            "id": "combat_state",
            "type": "static_text",
            "source": "module:wb_core_rpg",
            "enabled": True,
            "role_type": "system",
            "placement": "chat_injection",
            "depth": 1,
            "config": {"text": "<rpg_state>{\"hp\": 15}</rpg_state>"},
        },
    ]
    state = {
        "input_text": "I order a drink.",
        "chat_messages": [
            {"role": "user", "content": "I walk into the tavern."},
            {"role": "ai", "content": "The barkeep glares at you."},
        ],
    }

    compiled = compiler.compile(state, pipeline, validation_veto="Rule: correct the impossible purchase.")
    contents = [message["content"] for message in compiled["messages"]]

    assert contents == [
        "Narrate tersely.",
        "I walk into the tavern.",
        "The barkeep glares at you.",
        "<rpg_state>{\"hp\": 15}</rpg_state>",
        "I order a drink.",
        "Rule: correct the impossible purchase.",
    ]
    assert compiled["messages"][-1]["role"] == "system"
    print("Chat injection depth and veto order test passed.")


def test_invalid_pipeline_rejected():
    compiler = PromptCompiler()
    try:
        compiler.normalize_pipeline([
            {
                "id": "bad",
                "type": "unknown",
                "role_type": "system",
                "placement": "system_relative",
                "config": {},
            }
        ])
    except PromptPipelineValidationError:
        print("Invalid prompt pipeline rejection test passed.")
        return

    raise AssertionError("Invalid prompt pipeline was not rejected.")


async def _graph_records_prompt_trace():
    previous_mode = os.getenv("LLM_MODE")
    os.environ["LLM_MODE"] = "mock"

    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        registry = ModuleRegistry(os.path.join(base_dir, "modules"))
        registry.load_all_modules()

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = EngineGraph(registry)
            engine.set_memory_path(os.path.join(temp_dir, "vector_index"))
            try:
                await _assert_prompt_trace(engine)
            finally:
                # Windows: the open SQLite handle would make the temp dir
                # cleanup fail with PermissionError.
                engine.close_memory()
    finally:
        if previous_mode is None:
            os.environ.pop("LLM_MODE", None)
        else:
            os.environ["LLM_MODE"] = previous_mode


async def _assert_prompt_trace(engine):
    result = await engine.app.ainvoke({
        "active_save_id": "prompt_test",
        "input_text": "I test the compiler.",
        "module_data": {"wb_core_rpg": {"hp": 85}},
        "module_configs": {"wb_core_rpg": {"progression_system": "xp"}},
        "characters": {},
        "current_context": [],
        "history": [],
        "chat_messages": [],
        "turn": 0,
    })

    assert result["history"][-1].startswith("Mock outcome: I test the compiler.")

    trace = result["last_prompt_trace"]
    trace_ids = [entry["id"] for entry in trace]
    # Core engine blocks and the RPG module blocks must appear in this
    # order; other loaded modules may contribute additional entries.
    expected_order = [
        "core_narrator_rules",
        "world_rules_context",
        "player_character_context",
        "engine_context",
        "storyteller_task",
        "wb_core_rpg:character_sheet",
        "wb_core_rpg:action_feasibility",
    ]
    positions = [trace_ids.index(block_id) for block_id in expected_order]
    assert positions == sorted(positions), f"Trace order mismatch: {trace_ids}"

    by_id = {entry["id"]: entry for entry in trace}
    assert not by_id["wb_core_rpg:character_sheet"]["skipped"]
    # In mock mode the LLM returns non-JSON, so the action assessment
    # stays empty and the feasibility block renders no content.
    assert by_id["wb_core_rpg:action_feasibility"]["skipped"]
    print("Graph prompt trace test passed.")


def test_graph_records_prompt_trace():
    asyncio.run(_graph_records_prompt_trace())


async def _graph_prompt_preview_includes_messages_and_module_blocks():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    registry = ModuleRegistry(os.path.join(base_dir, "modules"))
    registry.load_all_modules()
    engine = EngineGraph(registry)

    preview = await engine.compile_prompt_preview(
        {
            "active_save_id": "preview_test",
            "input_text": "I preview the prompt.",
            "module_data": {"wb_core_rpg": {"hp": 42, "action_assessment": {
                "feasibility": 7,
                "skill_used": "sword_stance",
                "difficulty": "moderate",
                "failure_reason": "",
            }}},
            "module_configs": {"wb_core_rpg": {"progression_system": "xp"}},
            "characters": {},
            "current_context": ["<scene>A stone hallway.</scene>"],
            "history": [],
            "chat_messages": [],
            "turn": 0,
        },
        [
            {
                "id": "preview_rules",
                "type": "static_text",
                "source": "user",
                "enabled": True,
                "role_type": "system",
                "placement": "system_relative",
                "depth": None,
                "config": {"text": "Preview this prompt exactly."},
            }
        ],
    )

    contents = [message["content"] for message in preview["messages"]]
    trace_ids = [entry["id"] for entry in preview["trace"]]

    assert contents[0] == "Preview this prompt exactly."
    assert "I preview the prompt." in contents
    # The seeded action assessment must render through the feasibility block.
    assert any("Ruling:" in content for content in contents)
    assert "preview_rules" in trace_ids
    assert "wb_core_rpg:character_sheet" in trace_ids
    assert "wb_core_rpg:action_feasibility" in trace_ids
    print("Graph prompt preview test passed.")


async def run_all_tests():
    test_default_pipeline_compiles_messages()
    test_chat_injection_depth_and_veto_order()
    test_invalid_pipeline_rejected()
    await _graph_records_prompt_trace()
    await _graph_prompt_preview_includes_messages_and_module_blocks()


def test_graph_prompt_preview_includes_messages_and_module_blocks():
    asyncio.run(_graph_prompt_preview_includes_messages_and_module_blocks())


if __name__ == "__main__":
    asyncio.run(run_all_tests())
