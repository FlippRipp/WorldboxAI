from pathlib import Path
from typing import Any, Optional
import re

from backend.engine.save_manager import SaveManager
from backend.engine.prompt_pipeline import PromptCompiler
from backend.engine.settings_registry import SettingsRegistry


class GameSessionManager:
    """Owns the active local play session and bridges saves to engine state."""

    def __init__(self, data_dir: str, default_save_id: str = "autosave", settings: SettingsRegistry = None):
        self.data_dir = Path(data_dir)
        self.save_manager = SaveManager(str(self.data_dir))
        self.prompt_compiler = PromptCompiler()
        self.active_save_id = default_save_id
        self.settings = settings or SettingsRegistry()
        self._ensure_default_save()
        self.state = self.load_active_state()

    def _validate_save_id(self, save_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", save_id):
            raise ValueError("Save id may only contain letters, numbers, underscores, and hyphens.")

    def _ensure_default_save(self):
        save_workspace = self.data_dir / "saves" / self.active_save_id
        save_archive = self.data_dir / "saves" / f"{self.active_save_id}.wbx"
        if save_workspace.exists() or save_archive.exists():
            self.settings.bind_workspace(str(save_workspace))
            return

        default_player = {
            "id": "default_player",
            "name": "Adventurer",
            "module_data": {},
        }
        self.save_manager.create_player_template("default_player", default_player)
        self.save_manager.create_new_save(
            self.active_save_id,
            ["default_player"],
            {
                "module_data": {
                    "wb_core_rpg": {"hp": 85, "max_hp": 85},
                }
            },
        )
        self.settings.bind_workspace(str(save_workspace))

    def list_saves(self) -> list[dict[str, Any]]:
        saves_dir = self.data_dir / "saves"
        saves_dir.mkdir(parents=True, exist_ok=True)
        save_ids = set()

        for archive in saves_dir.glob("*.wbx"):
            save_ids.add(archive.stem)
        for workspace in saves_dir.iterdir():
            if workspace.is_dir():
                save_ids.add(workspace.name)

        saves = []
        for save_id in sorted(save_ids):
            workspace = saves_dir / save_id
            archive = saves_dir / f"{save_id}.wbx"
            saves.append({
                "id": save_id,
                "active": save_id == self.active_save_id,
                "workspace_exists": workspace.exists(),
                "archive_exists": archive.exists(),
            })
        return saves

    def create_save(self, save_id: str, world_id: str = None, player_location_node_id: str = None, player_location_region: str = None, player_location_layer_id: str = None, revealed_node_ids: list = None, character_module_data: dict = None) -> dict[str, Any]:
        self._validate_save_id(save_id)
        saves_dir = self.data_dir / "saves"
        save_workspace = saves_dir / save_id
        save_archive = saves_dir / f"{save_id}.wbx"
        if save_workspace.exists() or save_archive.exists():
            raise FileExistsError(f"Save '{save_id}' already exists.")

        metadata = {
            "turn": 0,
        }
        if world_id:
            metadata["world_id"] = world_id
            metadata["player_location_node_id"] = player_location_node_id or ""
            metadata["player_location_region"] = player_location_region or ""
            metadata["player_location_layer_id"] = player_location_layer_id or ""
            metadata["revealed_node_ids"] = revealed_node_ids or []

        module_data = character_module_data if character_module_data else {"wb_core_rpg": {"hp": 85, "max_hp": 85}}

        self.save_manager.create_player_template(
            "default_player",
            {
                "id": "default_player",
                "name": "Adventurer",
                "module_data": module_data,
            },
        )
        self.save_manager.create_new_save(
            save_id,
            ["default_player"],
            {
                "module_data": module_data,
                "metadata": metadata,
            },
        )
        return self.load_save(save_id)

    def load_save(self, save_id: str) -> dict[str, Any]:
        self._validate_save_id(save_id)
        save_workspace = self.data_dir / "saves" / save_id
        save_archive = self.data_dir / "saves" / f"{save_id}.wbx"
        if not save_workspace.exists() and not save_archive.exists():
            raise FileNotFoundError(f"Save {save_id} not found.")
        self.active_save_id = save_id
        self.settings.bind_workspace(str(save_workspace))
        self.state = self.load_active_state()
        return self.state

    def load_active_state(self) -> dict[str, Any]:
        saved_state = self.save_manager.load_save(self.active_save_id)
        metadata = saved_state.get("core", {}).get("metadata", {})
        history = saved_state.get("core", {}).get("chat_history", [])
        chat_messages = saved_state.get("core", {}).get("chat_messages", [])
        prompt_pipeline = saved_state.get("core", {}).get("prompt_pipeline")

        state = {
            "active_save_id": self.active_save_id,
            "input_text": "",
            "module_data": saved_state.get("module_data", {}),
            "module_configs": saved_state.get("module_configs", {}),
            "characters": saved_state.get("characters", {}),
            "current_context": [],
            "history": history,
            "chat_messages": chat_messages,
            "prompt_pipeline": self.prompt_compiler.normalize_pipeline(prompt_pipeline),
            "last_prompt_trace": [],
            "turn": metadata.get("turn", 0),
            "world_id": metadata.get("world_id"),
            "player_location_node_id": metadata.get("player_location_node_id"),
            "player_location_region": metadata.get("player_location_region"),
            "player_location_layer_id": metadata.get("player_location_layer_id"),
            "revealed_node_ids": metadata.get("revealed_node_ids", []),
        }

        world_data = saved_state.get("world_data")
        if world_data:
            state["world_data"] = world_data
        else:
            world_file = self.data_dir / "saves" / self.active_save_id / "World" / "world_data.json"
            if world_file.exists():
                import json as _json
                with open(world_file, "r", encoding="utf-8") as f:
                    state["world_data"] = _json.load(f)

        return state

    def set_input(self, input_text: str):
        self.state["input_text"] = input_text

    def save_completed_turn(self, final_state: dict[str, Any]) -> dict[str, Any]:
        user_text = self.state.get("input_text", "")
        previous_history = self.state.get("history", [])
        final_history = final_state.get("history", previous_history)
        assistant_text = final_history[-1] if len(final_history) > len(previous_history) and final_history else None
        chat_messages = list(self.state.get("chat_messages", []))
        if user_text:
            chat_messages.append({"role": "user", "content": user_text})
        if assistant_text:
            chat_messages.append({"role": "ai", "content": assistant_text})

        self.state = {
            **self.state,
            **final_state,
            "active_save_id": self.active_save_id,
            "input_text": "",
            "current_context": [],
            "chat_messages": chat_messages,
            "world_id": final_state.get("world_id", self.state.get("world_id")),
            "world_data": final_state.get("world_data", self.state.get("world_data")),
            "player_location_node_id": final_state.get("player_location_node_id", self.state.get("player_location_node_id")),
            "player_location_region": final_state.get("player_location_region", self.state.get("player_location_region")),
            "player_location_layer_id": final_state.get("player_location_layer_id", self.state.get("player_location_layer_id")),
            "revealed_node_ids": final_state.get("revealed_node_ids", self.state.get("revealed_node_ids", [])),
        }
        turn = self.state.get("turn", 0)
        self.save_manager.save_turn(self.active_save_id, self.state, turn)
        return self.state

    def update_module_configs(self, module_configs: dict[str, Any]) -> dict[str, Any]:
        self.state["module_configs"] = module_configs
        self.save_manager.save_module_configs(self.active_save_id, module_configs)
        return self.state

    def update_prompt_pipeline(self, prompt_pipeline: list[dict[str, Any]]) -> dict[str, Any]:
        normalized = self.prompt_compiler.normalize_pipeline(prompt_pipeline)
        self.state["prompt_pipeline"] = normalized
        self.save_manager.save_prompt_pipeline(self.active_save_id, normalized)
        return self.state

    def undo_turn(self, target_turn: int) -> dict[str, Any]:
        if target_turn < 0:
            raise ValueError("Target turn must be zero or greater.")
        self.save_manager.undo_turn(self.active_save_id, target_turn)
        self.state = self.load_active_state()
        return self.state

    def get_memory_path(self) -> str:
        return str(self.data_dir / "saves" / self.active_save_id / "vector_index")

    def get_status(self) -> dict[str, Any]:
        save_workspace = self.data_dir / "saves" / self.active_save_id
        save_archive = self.data_dir / "saves" / f"{self.active_save_id}.wbx"
        return {
            "active_save_id": self.active_save_id,
            "state_backend": "save_backed_session",
            "turn": self.state.get("turn", 0),
            "history_length": len(self.state.get("history", [])),
            "workspace_exists": save_workspace.exists(),
            "archive_exists": save_archive.exists(),
            "memory_path": self.get_memory_path(),
        }

    def get_settings_descriptors(self, scope: str = "story") -> dict[str, list[dict]]:
        return self.settings.get_all_descriptors(scope=scope)

    async def update_settings(self, updates: dict[str, Any], scope: str = "story") -> dict[str, list[dict]]:
        await self.settings.update(updates, scope=scope)
        return self.settings.get_all_descriptors(scope=scope)
