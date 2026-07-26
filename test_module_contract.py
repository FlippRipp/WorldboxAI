import asyncio
import json
import os
import tempfile

from backend.engine.graph import EngineGraph
from backend.engine.registry import ModuleRegistry


class FakeLLM:
    async def extract_mutations(self, story_text: str, schema: dict, inspector_ctx=None) -> dict:
        assert "wb_core_rpg" in schema
        assert "hp_change" in schema["wb_core_rpg"]
        return {"wb_core_rpg": {"hp_change": -7}}


def create_test_module(root: str, folder_name: str, manifest: dict,
                       backend_source: str = "backend_marker = True\n"):
    module_path = os.path.join(root, folder_name)
    os.makedirs(module_path)

    manifest.setdefault("consumes", {
        "state": ["input_text", "turn"],
        "module_data": ["*"],
        "module_configs": [],
        "world_data": False,
    })
    manifest.setdefault("produces", {
        "module_data": True,
        "context_string": True,
        "messages": False,
    })

    with open(os.path.join(module_path, "manifest.json"), "w") as f:
        json.dump(manifest, f)

    with open(os.path.join(module_path, "backend.py"), "w") as f:
        f.write(backend_source)


def test_dependency_order_and_invalid_manifests():
    with tempfile.TemporaryDirectory() as temp_dir:
        create_test_module(temp_dir, "child", {
            "id": "wb_alpha_child",
            "name": "Child",
            "version": "1.0.0",
            "dependencies": ["wb_zeta_base"],
        })
        create_test_module(temp_dir, "base", {
            "id": "wb_zeta_base",
            "name": "Base",
            "version": "1.0.0",
        })
        create_test_module(temp_dir, "bad_slot", {
            "id": "wb_bad_slot",
            "name": "Bad Slot",
            "version": "1.0.0",
            "ui_slots": ["slot_missing"],
        })
        create_test_module(temp_dir, "bad_prompt", {
            "id": "wb_bad_prompt",
            "name": "Bad Prompt",
            "version": "1.0.0",
            "prompt_blocks": [
                {
                    "id": "bad",
                    "type": "unknown",
                    "role_type": "system",
                    "placement": "system_relative",
                    "config": {},
                }
            ],
        })
        create_test_module(temp_dir, "orphan", {
            "id": "wb_orphan",
            "name": "Orphan",
            "version": "1.0.0",
            "dependencies": ["wb_missing_dependency"],
        })
        create_test_module(temp_dir, "cycle_a", {
            "id": "wb_cycle_a",
            "name": "Cycle A",
            "version": "1.0.0",
            "dependencies": ["wb_cycle_b"],
        })
        create_test_module(temp_dir, "cycle_b", {
            "id": "wb_cycle_b",
            "name": "Cycle B",
            "version": "1.0.0",
            "dependencies": ["wb_cycle_a"],
        })

        registry = ModuleRegistry(temp_dir)
        registry.load_all_modules()

        loaded_ids = list(registry.get_modules().keys())
        assert loaded_ids == ["wb_zeta_base", "wb_alpha_child"]
        assert "wb_bad_slot" not in loaded_ids
        assert "wb_bad_prompt" not in loaded_ids
        assert "wb_orphan" not in loaded_ids
        assert "wb_cycle_a" not in loaded_ids
        assert "wb_cycle_b" not in loaded_ids
        print("Dependency order and invalid manifest tests passed.")


def test_all_shipped_modules_load():
    # A manifest validation failure silently drops a module from the registry
    # (wb_character_tracker once vanished because it consumed a state key the
    # registry whitelist didn't know yet). Every module shipped in modules/
    # must actually load.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    modules_dir = os.path.join(base_dir, "modules")
    registry = ModuleRegistry(modules_dir)
    registry.load_all_modules()

    expected = sorted(
        entry for entry in os.listdir(modules_dir)
        if os.path.isfile(os.path.join(modules_dir, entry, "manifest.json"))
    )
    assert sorted(registry.get_modules().keys()) == expected
    print("All shipped modules load test passed.")


async def _module_owned_mutation_dispatch():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    registry = ModuleRegistry(os.path.join(base_dir, "modules"))
    registry.load_all_modules()

    combat_manifest = registry.get_modules()["wb_core_rpg"]["manifest"]
    assert "mutation_schema" in combat_manifest

    engine = EngineGraph(registry)
    engine.llm = FakeLLM()

    state = {
        "active_save_id": "test",
        "input_text": "",
        "module_data": {"wb_core_rpg": {"hp": 85, "max_hp": 85}},
        "module_configs": {"wb_core_rpg": {"progression_system": "xp"}},
        "characters": {},
        "current_context": [],
        "history": ["The player is wounded."],
        "chat_messages": [],
        "turn": 0,
    }

    result = await engine.reader_node(state)
    assert result["module_data"]["wb_core_rpg"]["hp"] == 78
    assert result["turn"] == 1
    print("Module-owned mutation dispatch test passed.")


async def _dedicated_reader_gets_own_extraction_call():
    # A module flagged dedicated_reader in its manifest is pulled out of the
    # shared reader extraction into its own call, which carries the player's
    # declared action and the module's on_reader_context block as context.
    with tempfile.TemporaryDirectory() as temp_dir:
        create_test_module(temp_dir, "shared", {
            "id": "wb_shared",
            "name": "Shared",
            "version": "1.0.0",
            "mutation_schema": {"hp_change": {"type": "integer"}},
        })
        create_test_module(temp_dir, "dedicated", {
            "id": "wb_ded",
            "name": "Dedicated",
            "version": "1.0.0",
            "dedicated_reader": True,
            "mutation_schema": {"moved": {"type": "boolean"}},
        }, backend_source=(
            "async def on_reader_context(state, sdk):\n"
            "    return 'WORLD CONTEXT BLOCK'\n"
        ))

        registry = ModuleRegistry(temp_dir)
        registry.load_all_modules()
        assert sorted(registry.get_modules().keys()) == ["wb_ded", "wb_shared"]

        engine = EngineGraph(registry)
        calls = []

        class RecordingLLM:
            async def extract_mutations(self, story_text, schema, inspector_ctx=None, context=""):
                calls.append({"schema": schema, "context": context})
                if "wb_ded" in schema:
                    return {"wb_ded": {"moved": True}}
                return {"wb_shared": {"hp_change": -3}}

        engine.llm = RecordingLLM()

        state = {
            "active_save_id": "test",
            "input_text": "I walk to the gate",
            "module_data": {},
            "module_configs": {},
            "characters": {},
            "current_context": [],
            "history": ["You stride toward the gate."],
            "chat_messages": [],
            "turn": 0,
        }
        result = await engine.reader_node(state)

        assert len(calls) == 2
        shared_call = next(c for c in calls if "wb_shared" in c["schema"])
        dedicated_call = next(c for c in calls if "wb_ded" in c["schema"])
        assert "wb_ded" not in shared_call["schema"]
        assert shared_call["context"] == ""
        assert list(dedicated_call["schema"].keys()) == ["wb_ded"]
        assert "I walk to the gate" in dedicated_call["context"]
        assert "WORLD CONTEXT BLOCK" in dedicated_call["context"]
        assert result["turn"] == 1
        print("Dedicated reader partition test passed.")


async def _librarian_skill_removal_survives_merge():
    # The hook runner deep-merges returned module_data, which is additive and
    # can't delete a dict entry. A skill removed by wb_core_rpg's on_librarian
    # (external curse stripping a power) used to be resurrected from the old
    # state by that merge; the module_data_replace opt-in must make it stick.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    registry = ModuleRegistry(os.path.join(base_dir, "modules"))
    registry.load_all_modules()

    engine = EngineGraph(registry)

    async def fake_generate(prompt, model_preference="balanced", **kwargs):
        return json.dumps({"added": [], "removed": ["emberkiss"], "altered": []})

    engine.sdk.llm.generate = fake_generate

    state = {
        "active_save_id": "test",
        "input_text": "",
        "module_data": {"wb_core_rpg": {
            "hp": 85, "max_hp": 85,
            "skills": {"emberkiss": {"rating": 4, "description": "Fire by touch.",
                                     "trigger_words": [], "type": "active"}},
            "practice_counters": {"emberkiss": 7},
        }},
        "module_configs": {"__active_modules__": ["wb_core_rpg"]},
        "characters": {},
        "current_context": [],
        "history": ["The god withdraws his gift; the warmth leaves your hands."],
        "chat_messages": [],
        "turn": 3,
    }

    accumulated = await engine._run_modules_in_levels("on_librarian", state)

    rpg = accumulated["module_data"]["wb_core_rpg"]
    assert "emberkiss" not in rpg["skills"]
    assert "emberkiss" not in rpg["practice_counters"]
    print("Librarian skill removal survives merge test passed.")


# ---------------------------------------------------------------------------
# Turn lifecycle + stream token hooks (on_turn_start / on_stream_token /
# on_turn_stopped)
# ---------------------------------------------------------------------------

STREAM_TOKENS = ["Once ", "upon ", "a ", "time."]

RECORDER_BACKEND = (
    "events = []\n"
    "\n"
    "async def on_turn_start(state, sdk):\n"
    "    events.append(('start', state.get('turn'), sdk is not None))\n"
    "\n"
    "def on_stream_token(token, state, sdk):\n"
    "    events.append(('token', token, id(state)))\n"
    "\n"
    "async def on_turn_stopped(state, sdk, reason):\n"
    "    events.append(('stopped', reason))\n"
)


class StreamingFakeLLM:
    """Streams a fixed token list through the storyteller's callback."""

    def __init__(self, tokens=None):
        self.tokens = tokens if tokens is not None else list(STREAM_TOKENS)

    async def generate_story_from_messages(self, messages, streaming_callback=None,
                                           inspector_ctx=None, reasoning_callback=None):
        if streaming_callback:
            for token in self.tokens:
                await streaming_callback(token)
        return {"content": "".join(self.tokens), "reasoning": "", "model": "mock", "usage": {}}


def _make_stream_engine(temp_dir, extra_modules=(), active=None, llm=None):
    create_test_module(temp_dir, "recorder", {
        "id": "wb_recorder", "name": "Recorder", "version": "1.0.0",
    }, backend_source=RECORDER_BACKEND)
    for folder, manifest, source in extra_modules:
        create_test_module(temp_dir, folder, manifest, backend_source=source)

    registry = ModuleRegistry(temp_dir)
    registry.load_all_modules()
    engine = EngineGraph(registry)
    engine.llm = llm or StreamingFakeLLM()

    state = {
        "active_save_id": "test",
        "input_text": "I open the door",
        "module_data": {},
        "module_configs": {},
        "characters": {},
        "current_context": [],
        "history": [],
        "chat_messages": [],
        "turn": 4,
    }
    if active is not None:
        state["module_configs"]["__active_modules__"] = list(active)
    return engine, registry, state


async def _run_completed_turn(engine, state):
    # The server-side turn shape: on_turn_start at the entry point, the real
    # storyteller node (which owns the streaming-callback wrap), then
    # on_turn_stopped("completed").
    await engine.dispatch_turn_start(state)
    await engine.storyteller_node(state)
    await engine.dispatch_turn_stopped(state, "completed")


async def _turn_lifecycle_and_stream_order():
    # Hook order per turn: on_turn_start -> each streamed token in order ->
    # on_turn_stopped("completed"), with correct args throughout — and the UI
    # still receives every token as before.
    with tempfile.TemporaryDirectory() as temp_dir:
        engine, registry, state = _make_stream_engine(temp_dir)

        ui_tokens = []

        async def on_token(token):
            ui_tokens.append(token)

        engine.sdk.ui.on_token = on_token

        await _run_completed_turn(engine, state)

        events = registry.get_modules()["wb_recorder"]["backend"].events
        assert events[0] == ("start", 4, True)
        token_events = [e for e in events if e[0] == "token"]
        assert [e[1] for e in token_events] == STREAM_TOKENS
        # The per-module state view is built once at turn start and reused for
        # every token — same object identity across the whole stream.
        assert len({e[2] for e in token_events}) == 1
        assert events[-1] == ("stopped", "completed")
        assert len(events) == len(STREAM_TOKENS) + 2
        assert ui_tokens == STREAM_TOKENS
        print("Turn lifecycle and stream order test passed.")


async def _cancelled_turn_fires_stopped_once():
    # A turn cancelled mid-stream fires on_turn_stopped("cancelled") exactly
    # once, even though the server also runs an error-path safety net.
    class HangingLLM:
        async def generate_story_from_messages(self, messages, streaming_callback=None,
                                               inspector_ctx=None, reasoning_callback=None):
            for token in STREAM_TOKENS[:2]:
                if streaming_callback:
                    await streaming_callback(token)
            await asyncio.Event().wait()  # blocks until the task is cancelled

    with tempfile.TemporaryDirectory() as temp_dir:
        engine, registry, state = _make_stream_engine(temp_dir, llm=HangingLLM())
        events = registry.get_modules()["wb_recorder"]["backend"].events

        async def fake_run_action():
            # Mirrors server.run_action: "cancelled" from the cancel handler,
            # plus the finally safety net that must stay a no-op here.
            try:
                await engine.dispatch_turn_start(state)
                await engine.storyteller_node(state)
                await engine.dispatch_turn_stopped(state, "completed")
            except asyncio.CancelledError:
                await engine.dispatch_turn_stopped(state, "cancelled")
                raise
            finally:
                await engine.dispatch_turn_stopped(state, "error")

        task = asyncio.ensure_future(fake_run_action())
        for _ in range(200):
            if len([e for e in events if e[0] == "token"]) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        stopped = [e for e in events if e[0] == "stopped"]
        assert stopped == [("stopped", "cancelled")]
        assert events[0][0] == "start"
        # Ordering guarantee: no token after on_turn_stopped.
        assert events[-1] == ("stopped", "cancelled")
        print("Cancelled turn fires stopped once test passed.")


async def _raising_subscriber_is_isolated():
    # A subscriber that raises on every token must not break streaming, the
    # other subscribers, or turn completion — and is logged once per turn.
    raiser = (
        "def on_stream_token(token, state, sdk):\n"
        "    raise RuntimeError('boom')\n"
        "\n"
        "stopped_calls = []\n"
        "\n"
        "def on_turn_stopped(state, sdk, reason):\n"
        "    stopped_calls.append(reason)\n"
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        engine, registry, state = _make_stream_engine(temp_dir, extra_modules=[
            ("raiser", {"id": "wb_raiser", "name": "Raiser", "version": "1.0.0"}, raiser),
        ])
        await _run_completed_turn(engine, state)

        events = registry.get_modules()["wb_recorder"]["backend"].events
        assert [e[1] for e in events if e[0] == "token"] == STREAM_TOKENS
        assert events[-1] == ("stopped", "completed")
        # The raiser's (sync) on_turn_stopped still ran despite its token hook
        # failing all turn.
        assert registry.get_modules()["wb_raiser"]["backend"].stopped_calls == ["completed"]
        print("Raising subscriber isolation test passed.")


async def _inactive_module_gets_no_dispatches():
    with tempfile.TemporaryDirectory() as temp_dir:
        engine, registry, state = _make_stream_engine(temp_dir, extra_modules=[
            ("bystander", {"id": "wb_bystander", "name": "Bystander", "version": "1.0.0"},
             RECORDER_BACKEND),
        ], active=["wb_recorder"])
        await _run_completed_turn(engine, state)

        assert registry.get_modules()["wb_bystander"]["backend"].events == []
        recorder_events = registry.get_modules()["wb_recorder"]["backend"].events
        assert recorder_events[0][0] == "start"
        assert recorder_events[-1] == ("stopped", "completed")
        print("Inactive module isolation test passed.")


async def _async_stream_token_hook_is_skipped():
    # The token path is hot: an async def on_stream_token is rejected at turn
    # start (logged warning), never awaited, and never dispatched.
    async_hook = (
        "async def on_stream_token(token, state, sdk):\n"
        "    raise AssertionError('must never be awaited')\n"
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        engine, registry, state = _make_stream_engine(temp_dir, extra_modules=[
            ("asynctok", {"id": "wb_asynctok", "name": "AsyncTok", "version": "1.0.0"},
             async_hook),
        ])
        await _run_completed_turn(engine, state)

        # Rejected at subscriber collection, so it never entered the fan-out.
        assert all(mod_id != "wb_asynctok" for mod_id, _, _ in engine._stream_subscribers)
        events = registry.get_modules()["wb_recorder"]["backend"].events
        assert [e[1] for e in events if e[0] == "token"] == STREAM_TOKENS
        print("Async stream token hook skip test passed.")


def test_turn_lifecycle_and_stream_order():
    asyncio.run(_turn_lifecycle_and_stream_order())


def test_cancelled_turn_fires_stopped_once():
    asyncio.run(_cancelled_turn_fires_stopped_once())


def test_raising_subscriber_is_isolated(capsys):
    asyncio.run(_raising_subscriber_is_isolated())
    out = capsys.readouterr().out
    # Logged once per turn per module, not once per token.
    assert out.count("Error in wb_raiser.on_stream_token") == 1


def test_inactive_module_gets_no_dispatches():
    asyncio.run(_inactive_module_gets_no_dispatches())


def test_async_stream_token_hook_is_skipped(capsys):
    asyncio.run(_async_stream_token_hook_is_skipped())
    out = capsys.readouterr().out
    assert "wb_asynctok.on_stream_token must be a plain sync function" in out


def test_module_api_features():
    # Modules probe this with getattr(engine, "MODULE_API_FEATURES", set()),
    # so it must be a class attribute containing one name per capability.
    assert "stream_tokens" in EngineGraph.MODULE_API_FEATURES
    assert "turn_lifecycle" in EngineGraph.MODULE_API_FEATURES


async def run_all_tests():
    test_dependency_order_and_invalid_manifests()
    await _module_owned_mutation_dispatch()
    await _dedicated_reader_gets_own_extraction_call()
    await _librarian_skill_removal_survives_merge()
    await _turn_lifecycle_and_stream_order()
    await _cancelled_turn_fires_stopped_once()
    await _raising_subscriber_is_isolated()
    await _inactive_module_gets_no_dispatches()
    await _async_stream_token_hook_is_skipped()


def test_build_module_state_injects_module_instructions():
    """A module's instruction overrides (reserved __module_instructions__ key)
    are injected as state["module_instructions"] for that module only —
    without any consumes declaration."""
    full_state = {
        "active_save_id": "test",
        "turn": 3,
        "module_data": {"wb_core_rpg": {"hp": 85}},
        "module_configs": {
            "wb_core_rpg": {"xp_per_action": 10},
            "__module_instructions__": {"wb_core_rpg": {"action_assessment": "Gravity is optional."}},
        },
    }
    build = EngineGraph._build_module_state

    rpg_view = build(None, full_state, "wb_core_rpg", {})
    assert rpg_view["module_instructions"] == {"action_assessment": "Gravity is optional."}
    # Injected as a copy: mutating the view can't corrupt the real state.
    rpg_view["module_instructions"]["action_assessment"] = "tampered"
    assert full_state["module_configs"]["__module_instructions__"]["wb_core_rpg"]["action_assessment"] == "Gravity is optional."

    # A module with no overrides gets no key at all.
    other_view = build(None, full_state, "wb_time_tracker", {})
    assert "module_instructions" not in other_view


def test_module_owned_mutation_dispatch():
    asyncio.run(_module_owned_mutation_dispatch())


def test_dedicated_reader_gets_own_extraction_call():
    asyncio.run(_dedicated_reader_gets_own_extraction_call())


def test_librarian_skill_removal_survives_merge():
    asyncio.run(_librarian_skill_removal_survives_merge())


if __name__ == "__main__":
    asyncio.run(run_all_tests())
