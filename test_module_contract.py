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


def create_test_module(root: str, folder_name: str, manifest: dict):
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
        f.write("backend_marker = True\n")


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


async def run_all_tests():
    test_dependency_order_and_invalid_manifests()
    await _module_owned_mutation_dispatch()


def test_module_owned_mutation_dispatch():
    asyncio.run(_module_owned_mutation_dispatch())


if __name__ == "__main__":
    asyncio.run(run_all_tests())
