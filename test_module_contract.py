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


async def run_all_tests():
    test_dependency_order_and_invalid_manifests()
    await _module_owned_mutation_dispatch()
    await _dedicated_reader_gets_own_extraction_call()
    await _librarian_skill_removal_survives_merge()


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
