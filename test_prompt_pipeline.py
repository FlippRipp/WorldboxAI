import asyncio
import os
import tempfile

from backend.engine.graph import EngineGraph
from backend.engine.prompt_pipeline import PromptCompiler, PromptPipelineValidationError, build_auto_player_action_prompt, render_story_style
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
        "chat_history",
    ]
    print("Default prompt pipeline compile test passed.")


def _history_pipeline(max_turns=None, enabled=True, extra_blocks=None, history_position=-1):
    blocks = [
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
            "id": "chat_history",
            "type": "chat_history",
            "source": "engine",
            "enabled": enabled,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "config": {"max_turns": max_turns},
        },
    ]
    if extra_blocks:
        blocks.extend(extra_blocks)
    if history_position != -1:
        block = blocks.pop(1)
        blocks.insert(history_position, block)
    return blocks


def _three_turn_state():
    return {
        "input_text": "I order a drink.",
        "chat_messages": [
            {"role": "user", "content": "I enter the city."},
            {"role": "ai", "content": "The gates creak open."},
            {"role": "user", "content": "I find the market."},
            {"role": "ai", "content": "Stalls crowd the square."},
            {"role": "user", "content": "I walk into the tavern."},
            {"role": "ai", "content": "The barkeep glares at you."},
        ],
    }


def test_chat_history_block_limits_turns():
    compiler = PromptCompiler()
    compiled = compiler.compile(_three_turn_state(), _history_pipeline(max_turns=2))
    contents = [message["content"] for message in compiled["messages"]]

    assert contents == [
        "Narrate tersely.",
        "I find the market.",
        "Stalls crowd the square.",
        "I walk into the tavern.",
        "The barkeep glares at you.",
        "I order a drink.",
    ]

    # A cap larger than the transcript keeps everything.
    compiled = compiler.compile(_three_turn_state(), _history_pipeline(max_turns=10))
    assert len(compiled["messages"]) == 8

    # Zero turns drops the transcript but keeps the player input.
    compiled = compiler.compile(_three_turn_state(), _history_pipeline(max_turns=0))
    contents = [message["content"] for message in compiled["messages"]]
    assert contents == ["Narrate tersely.", "I order a drink."]
    print("Chat history turn limit test passed.")


def test_disabled_chat_history_block_omits_transcript():
    compiler = PromptCompiler()
    compiled = compiler.compile(_three_turn_state(), _history_pipeline(enabled=False))
    contents = [message["content"] for message in compiled["messages"]]

    assert contents == ["Narrate tersely.", "I order a drink."]
    by_id = {entry["id"]: entry for entry in compiled["trace"]}
    assert by_id["chat_history"]["skipped"]
    assert by_id["chat_history"]["reason"] == "disabled"
    print("Disabled chat history block test passed.")


def test_pipeline_without_chat_history_block_keeps_legacy_order():
    # Saved pipelines predating the chat_history block still get the full
    # transcript appended after the system blocks.
    compiler = PromptCompiler()
    pipeline = [block for block in _history_pipeline() if block["type"] != "chat_history"]
    compiled = compiler.compile(_three_turn_state(), pipeline)
    contents = [message["content"] for message in compiled["messages"]]

    assert contents[0] == "Narrate tersely."
    assert contents[1] == "I enter the city."
    assert contents[-1] == "I order a drink."
    assert len(contents) == 8
    print("Legacy pipeline without chat history block test passed.")


def test_chat_history_block_position_moves_transcript():
    compiler = PromptCompiler()
    post_history_block = {
        "id": "post_history_note",
        "type": "static_text",
        "source": "user",
        "enabled": True,
        "role_type": "system",
        "placement": "system_relative",
        "depth": None,
        "config": {"text": "Answer in strict prose."},
    }
    pipeline = _history_pipeline(max_turns=1, extra_blocks=[post_history_block], history_position=1)
    compiled = compiler.compile(_three_turn_state(), pipeline)
    contents = [message["content"] for message in compiled["messages"]]

    assert contents == [
        "Narrate tersely.",
        "I walk into the tavern.",
        "The barkeep glares at you.",
        "Answer in strict prose.",
        "I order a drink.",
    ]
    print("Chat history block position test passed.")


def test_chat_history_block_validation():
    compiler = PromptCompiler()

    for bad_config in [{"max_turns": -1}, {"max_turns": "five"}, {"max_turns": True}]:
        try:
            compiler.normalize_pipeline(_history_pipeline()[:1] + [
                {
                    "id": "chat_history",
                    "type": "chat_history",
                    "role_type": "system",
                    "placement": "system_relative",
                    "config": bad_config,
                }
            ])
        except PromptPipelineValidationError:
            continue
        raise AssertionError(f"Invalid chat history config was not rejected: {bad_config}")

    try:
        compiler.normalize_pipeline([
            {
                "id": "history_a",
                "type": "chat_history",
                "role_type": "system",
                "placement": "system_relative",
                "config": {},
            },
            {
                "id": "history_b",
                "type": "chat_history",
                "role_type": "system",
                "placement": "system_relative",
                "config": {},
            },
        ])
    except PromptPipelineValidationError:
        print("Chat history block validation test passed.")
        return
    raise AssertionError("Duplicate chat history blocks were not rejected.")


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


def _injection_block(block_id, text, depth, order=None):
    block = {
        "id": block_id,
        "type": "static_text",
        "source": "user",
        "enabled": True,
        "role_type": "system",
        "placement": "chat_injection",
        "depth": depth,
        "config": {"text": text},
    }
    if order is not None:
        block["order"] = order
    return block


def test_same_depth_injections_follow_insertion_order():
    # Two blocks at the same depth: the configured order decides which comes
    # first (lower = earlier), overriding their pipeline positions.
    compiler = PromptCompiler()
    pipeline = [
        _injection_block("late", "Comes second.", depth=1, order=200),
        _injection_block("early", "Comes first.", depth=1, order=50),
    ]
    state = {
        "input_text": "I order a drink.",
        "chat_messages": [
            {"role": "user", "content": "I walk into the tavern."},
            {"role": "ai", "content": "The barkeep glares at you."},
        ],
    }

    compiled = compiler.compile(state, pipeline)
    contents = [message["content"] for message in compiled["messages"]]
    assert contents == [
        "I walk into the tavern.",
        "The barkeep glares at you.",
        "Comes first.",
        "Comes second.",
        "I order a drink.",
    ]

    # The trace records each injection's order and final position.
    by_id = {entry["id"]: entry for entry in compiled["trace"]}
    assert by_id["early"]["order"] == 50
    assert by_id["late"]["order"] == 200
    assert by_id["early"]["message_index"] == 2
    assert by_id["late"]["message_index"] == 3

    # Without explicit orders both default to 100 and pipeline order wins.
    compiled = compiler.compile(state, [
        _injection_block("first", "Pipeline first.", depth=1),
        _injection_block("second", "Pipeline second.", depth=1),
    ])
    contents = [message["content"] for message in compiled["messages"]]
    assert contents.index("Pipeline first.") + 1 == contents.index("Pipeline second.")
    print("Same-depth insertion order test passed.")


def test_injection_depth_counts_chat_messages_not_other_injections():
    # Depth is measured against the actual chat (history + current input);
    # injections never shift each other's target slots.
    compiler = PromptCompiler()
    pipeline = [
        _injection_block("bottom", "At the very bottom.", depth=0),
        _injection_block("above_input", "Above the player's input.", depth=1),
    ]
    state = {
        "input_text": "I order a drink.",
        "chat_messages": [
            {"role": "user", "content": "I walk into the tavern."},
            {"role": "ai", "content": "The barkeep glares at you."},
        ],
    }

    compiled = compiler.compile(state, pipeline)
    contents = [message["content"] for message in compiled["messages"]]
    assert contents == [
        "I walk into the tavern.",
        "The barkeep glares at you.",
        "Above the player's input.",
        "I order a drink.",
        "At the very bottom.",
    ]
    print("Injection depth vs other injections test passed.")


def test_invalid_injection_order_rejected():
    compiler = PromptCompiler()
    try:
        compiler.normalize_pipeline([
            _injection_block("bad", "Text.", depth=1, order="high"),
        ])
    except PromptPipelineValidationError:
        pass
    else:
        raise AssertionError("Non-integer injection order was not rejected.")

    # system_relative blocks carry no order; chat injections default to 100.
    normalized = compiler.normalize_pipeline([
        {
            "id": "rules", "type": "static_text", "source": "user", "enabled": True,
            "role_type": "system", "placement": "system_relative", "depth": None,
            "config": {"text": "Narrate tersely."},
        },
        _injection_block("inj", "Text.", depth=1),
    ])
    assert normalized[0]["order"] is None
    assert normalized[1]["order"] == 100
    print("Injection order validation test passed.")


def test_st_import_carries_injection_order():
    from backend.engine.st_importer import SillyTavernImporter

    result = SillyTavernImporter().import_preset({
        "prompts": [
            {"identifier": "inj", "name": "Injected", "content": "Stay terse.",
             "injection_position": 1, "injection_depth": 2, "injection_order": 42},
            {"identifier": "sys", "name": "System", "content": "Main prompt."},
        ],
        "prompt_order": [],
    })
    blocks = {b["id"]: b for b in result["blocks"]}
    assert blocks["inj"]["placement"] == "chat_injection"
    assert blocks["inj"]["depth"] == 2
    assert blocks["inj"]["order"] == 42
    assert blocks["sys"]["placement"] == "system_relative"
    assert blocks["sys"]["order"] is None
    print("ST import injection order test passed.")


def test_command_messages_stay_out_of_the_prompt():
    # Slash-command exchanges live in the transcript for the player but must
    # never reach the storyteller LLM.
    compiler = PromptCompiler()
    state = {
        "input_text": "I open the iron door.",
        "chat_messages": [
            {"role": "user", "content": "I walk into the tavern.", "meta": {"ts": "t"}},
            {"role": "user", "content": "/plot", "meta": {"ts": "t", "command": True}},
            {"role": "system", "content": "[Plot] Act 1 of 3.", "meta": {"ts": "t", "command": True}},
        ],
    }

    compiled = compiler.compile(state)
    contents = [message["content"] for message in compiled["messages"]]

    assert "I walk into the tavern." in contents
    assert "/plot" not in contents
    assert "[Plot] Act 1 of 3." not in contents
    print("Command message exclusion test passed.")


def test_story_style_injected_at_depth_zero():
    # The save's editable themes/tags/pacing must land as the final system
    # directive of every turn, after the player's input.
    compiler = PromptCompiler()
    state = {
        "input_text": "I order a drink.",
        "chat_messages": [
            {"role": "user", "content": "I walk into the tavern."},
            {"role": "ai", "content": "The barkeep glares at you."},
        ],
        "story_style": {"themes": "redemption, found family", "tags": "dark fantasy", "pacing": "slow burn"},
    }

    compiled = compiler.compile(state)
    messages = compiled["messages"]

    assert messages[-1]["role"] == "system"
    assert "<story_style>" in messages[-1]["content"]
    assert "Themes: redemption, found family" in messages[-1]["content"]
    assert "Tags: dark fantasy" in messages[-1]["content"]
    assert "Pacing: slow burn" in messages[-1]["content"]
    assert messages[-2] == {"role": "user", "content": "I order a drink."}
    assert any(entry["id"] == "engine_story_style" for entry in compiled["trace"])
    print("Story style depth-0 injection test passed.")


def test_empty_story_style_injects_nothing():
    compiler = PromptCompiler()
    state = {
        "input_text": "I order a drink.",
        "chat_messages": [],
        "story_style": {"themes": "  ", "tags": "", "pacing": ""},
    }

    compiled = compiler.compile(state)

    assert all("<story_style>" not in m["content"] for m in compiled["messages"])
    assert compiled["messages"][-1] == {"role": "user", "content": "I order a drink."}

    # Partially filled: only the set fields are rendered.
    text = render_story_style({"themes": "", "tags": "", "pacing": "breakneck"})
    assert "Pacing: breakneck" in text
    assert "Themes:" not in text and "Tags:" not in text
    print("Empty story style no-op test passed.")


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




def test_auto_player_action_prompt():
    state = {
        "characters": {"default_player": {
            "name": "Nyx",
            "race": "Half-elf",
            "personality": "Reckless, loyal, allergic to authority.",
        }},
        "history": [
            "You crest the ridge at dusk.",
            "The bandit camp sprawls below, fires guttering in the wind.",
        ],
    }

    prompt = build_auto_player_action_prompt(state, nudge="have her sneak toward the camp")
    assert "Nyx" in prompt
    assert "Reckless, loyal, allergic to authority." in prompt
    assert "The bandit camp sprawls below" in prompt
    assert "have her sneak toward the camp" in prompt
    assert "first person" in prompt
    # The generator declares attempts; outcomes belong to the storyteller.
    assert "never its" in prompt and "outcome" in prompt

    # Without a nudge the direction line is absent entirely.
    no_nudge = build_auto_player_action_prompt(state)
    assert "Direction from the player" not in no_nudge

    # Degrades gracefully with no character or story yet.
    bare = build_auto_player_action_prompt({})
    assert "the protagonist" in bare
    print("Auto player action prompt test passed.")


def test_auto_player_action_generation_mock():
    async def run():
        previous_mode = os.environ.get("LLM_MODE")
        os.environ["LLM_MODE"] = "mock"
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            registry = ModuleRegistry(os.path.join(base_dir, "modules"))
            registry.load_all_modules()
            engine = EngineGraph(registry)
            action = await engine.generate_auto_player_action(
                {"characters": {"default_player": {"name": "Nyx"}}, "history": []},
                nudge="scout ahead",
            )
            # Mock bridge answers with a canned string; the engine passes it
            # through cleaned of stray whitespace.
            assert action.startswith("[mock llm response")
        finally:
            if previous_mode is None:
                os.environ.pop("LLM_MODE", None)
            else:
                os.environ["LLM_MODE"] = previous_mode

    asyncio.run(run())
    print("Auto player action mock generation test passed.")


async def run_all_tests():
    test_default_pipeline_compiles_messages()
    test_chat_injection_depth_and_veto_order()
    test_same_depth_injections_follow_insertion_order()
    test_injection_depth_counts_chat_messages_not_other_injections()
    test_invalid_injection_order_rejected()
    test_st_import_carries_injection_order()
    test_chat_history_block_limits_turns()
    test_disabled_chat_history_block_omits_transcript()
    test_pipeline_without_chat_history_block_keeps_legacy_order()
    test_chat_history_block_position_moves_transcript()
    test_chat_history_block_validation()
    test_auto_player_action_prompt()
    test_auto_player_action_generation_mock()
    test_invalid_pipeline_rejected()
    await _graph_records_prompt_trace()
    await _graph_prompt_preview_includes_messages_and_module_blocks()


def test_graph_prompt_preview_includes_messages_and_module_blocks():
    asyncio.run(_graph_prompt_preview_includes_messages_and_module_blocks())


if __name__ == "__main__":
    asyncio.run(run_all_tests())


def test_module_block_anchoring_in_pipeline():
    compiler = PromptCompiler()

    def _anchor():
        return {
            "id": "wb_x:threads", "type": "module_prompt", "source": "module:wb_x",
            "enabled": True, "role_type": "system", "placement": "system_relative",
            "depth": None, "config": {},
        }

    rules = {
        "id": "rules", "type": "static_text", "source": "user", "enabled": True,
        "role_type": "system", "placement": "system_relative", "depth": None,
        "config": {"text": "Narrate tersely."},
    }
    module_blocks = [{
        "id": "wb_x:threads", "type": "module_prompt", "source": "module:wb_x",
        "enabled": True, "role_type": "system", "placement": "system_relative",
        "depth": None, "config": {"text": "Thread guidance."},
    }]
    state = {"input_text": "hi", "chat_messages": []}

    # Anchored: the module's rendered content lands at the anchor's position
    # (ahead of "rules"), exactly once -- not appended after the pipeline.
    compiled = compiler.compile(state, [_anchor(), rules], module_blocks=module_blocks)
    contents = [m["content"] for m in compiled["messages"]]
    assert contents[0] == "Thread guidance."
    assert contents[1] == "Narrate tersely."
    assert contents.count("Thread guidance.") == 1

    # Module inactive (no module blocks this compile): the anchor is skipped
    # with a clear trace reason instead of erroring or rendering stale text.
    compiled = compiler.compile(state, [_anchor(), rules])
    assert all("Thread guidance." not in m["content"] for m in compiled["messages"])
    entry = next(e for e in compiled["trace"] if e["id"] == "wb_x:threads")
    assert entry["skipped"] is True
    assert "module" in entry["reason"]

    # A disabled anchor suppresses the module block entirely (it was consumed
    # by the anchor, so it must not fall back to the appended position).
    disabled = _anchor()
    disabled["enabled"] = False
    compiled = compiler.compile(state, [disabled, rules], module_blocks=module_blocks)
    assert all("Thread guidance." not in m["content"] for m in compiled["messages"])

    # Unanchored module blocks keep the historical append behavior.
    compiled = compiler.compile(state, [rules], module_blocks=module_blocks)
    contents = [m["content"] for m in compiled["messages"]]
    assert "Thread guidance." in contents

    # A module-sourced block carrying its own static text still renders as-is
    # (it is not mistaken for an empty anchor).
    legacy = {
        "id": "combat_note", "type": "module_prompt", "source": "module:wb_y",
        "enabled": True, "role_type": "system", "placement": "system_relative",
        "depth": None, "config": {"text": "Baked-in module text."},
    }
    compiled = compiler.compile(state, [legacy, rules])
    assert any("Baked-in module text." in m["content"] for m in compiled["messages"])
    print("Module block anchoring test passed.")
