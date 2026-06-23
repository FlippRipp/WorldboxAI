import json
import zipfile
import os
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from backend.engine.prompt_pipeline import default_prompt_pipeline

class SaveManager:
    """
    Manages loading templates (.wbp) and active instances (.wbx) 
    including snapshots for Undo features.
    (.wbs scenario templates are planned but not yet implemented.)
    """
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.templates_dir = self.data_dir / "templates"
        self.saves_dir = self.data_dir / "saves"
        
        self.templates_players_dir = self.templates_dir / "players"
        self.templates_players_dir.mkdir(parents=True, exist_ok=True)
        self.saves_dir.mkdir(parents=True, exist_ok=True)

    # --- Templates ---

    def create_player_template(self, template_id: str, data: dict):
        p = self.templates_players_dir / f"{template_id}.wbp"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_player_template(self, template_id: str) -> dict:
        p = self.templates_players_dir / f"{template_id}.wbp"
        if not p.exists():
            raise FileNotFoundError(f"Player template {template_id} not found.")
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    # --- Save Instance (.wbx) ---

    def create_new_save(self, save_id: str, player_template_ids: list[str], initial_state: dict):
        """
        Initialize a new save directory structure and wrap it into a .wbx (zip archive).
        Since we operate in-memory/on-disk while playing, the save structure acts as a workspace.
        """
        save_path = self.saves_dir / save_id
        if save_path.exists():
            shutil.rmtree(save_path)
        
        save_path.mkdir(parents=True)
        (save_path / "Core").mkdir()
        (save_path / "Characters").mkdir()
        (save_path / "Module_States").mkdir()
        (save_path / "Snapshots").mkdir()
        
        # Write initial Core
        metadata = {
            "turn": 0,
            "playtime": 0,
        }
        extra_metadata = initial_state.get("metadata", {})
        if extra_metadata:
            metadata.update(extra_metadata)
        with open(save_path / "Core" / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
            
        with open(save_path / "Core" / "chat_history.json", "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

        with open(save_path / "Core" / "chat_messages.json", "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

        with open(save_path / "Core" / "module_configs.json", "w", encoding="utf-8") as f:
            json.dump(initial_state.get("module_configs", {}), f, indent=2)

        with open(save_path / "Core" / "prompt_pipeline.json", "w", encoding="utf-8") as f:
            json.dump(initial_state.get("prompt_pipeline", self.load_global_prompt_pipeline()), f, indent=2)

        # Copy player templates
        for p_id in player_template_ids:
            p_data = self.load_player_template(p_id)
            with open(save_path / "Characters" / f"{p_id}.json", "w", encoding="utf-8") as f:
                json.dump(p_data, f, indent=2)
                
        # Module states
        if "module_data" in initial_state:
            for mod_name, mod_data in initial_state["module_data"].items():
                with open(save_path / "Module_States" / f"{mod_name}.json", "w", encoding="utf-8") as f:
                    json.dump(mod_data, f, indent=2)
                    
        self._pack_save(save_id)
        
    def _pack_save(self, save_id: str):
        """Pack the workspace into a .wbx archive"""
        save_path = self.saves_dir / save_id
        wbx_path = self.saves_dir / f"{save_id}.wbx"
        
        with zipfile.ZipFile(wbx_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(save_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, save_path)
                    zipf.write(file_path, arcname)

    def load_save(self, save_id: str) -> dict:
        """Unpack a .wbx if necessary and load into active state"""
        save_path = self.saves_dir / save_id
        wbx_path = self.saves_dir / f"{save_id}.wbx"
        
        if not save_path.exists() and wbx_path.exists():
            # Unzip it
            with zipfile.ZipFile(wbx_path, 'r') as zipf:
                zipf.extractall(save_path)
        elif not save_path.exists():
            raise FileNotFoundError(f"Save {save_id} not found.")
            
        # Reconstruct state
        state = {"module_data": {}, "module_configs": {}, "characters": {}, "core": {}}
        
        with open(save_path / "Core" / "metadata.json", "r", encoding="utf-8") as f:
            state["core"]["metadata"] = json.load(f)
            
        with open(save_path / "Core" / "chat_history.json", "r", encoding="utf-8") as f:
            state["core"]["chat_history"] = json.load(f)

        chat_messages_path = save_path / "Core" / "chat_messages.json"
        if chat_messages_path.exists():
            with open(chat_messages_path, "r", encoding="utf-8") as f:
                state["core"]["chat_messages"] = json.load(f)
        else:
            state["core"]["chat_messages"] = []

        module_configs_path = save_path / "Core" / "module_configs.json"
        if module_configs_path.exists():
            with open(module_configs_path, "r", encoding="utf-8") as f:
                state["module_configs"] = json.load(f)

        prompt_pipeline_path = save_path / "Core" / "prompt_pipeline.json"
        if prompt_pipeline_path.exists():
            with open(prompt_pipeline_path, "r", encoding="utf-8") as f:
                state["core"]["prompt_pipeline"] = json.load(f)
        else:
            state["core"]["prompt_pipeline"] = self.load_global_prompt_pipeline()
            
        for char_file in (save_path / "Characters").glob("*.json"):
            with open(char_file, "r", encoding="utf-8") as f:
                state["characters"][char_file.stem] = json.load(f)
                
        for mod_file in (save_path / "Module_States").glob("*.json"):
            with open(mod_file, "r", encoding="utf-8") as f:
                state["module_data"][mod_file.stem] = json.load(f)
                
        return state

    def save_turn(self, save_id: str, state: dict, turn_number: int):
        """Save the current state to disk and create a snapshot."""
        save_path = self.saves_dir / save_id
        if not save_path.exists():
            raise Exception("Save workspace not found. Must be loaded first.")
            
        # Update metadata and chat history
        metadata = {}
        metadata_path = save_path / "Core" / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path, "r", encoding="utf-8") as f:
                try:
                    metadata = json.load(f)
                except (json.JSONDecodeError, OSError):
                    metadata = {}
        metadata["turn"] = turn_number
        for key in ("world_id", "player_location_node_id", "player_location_region", "player_location_layer_id", "revealed_node_ids"):
            if key in state and state[key] not in (None, ""):
                metadata[key] = state[key]
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
            
        if "history" in state:
            with open(save_path / "Core" / "chat_history.json", "w", encoding="utf-8") as f:
                json.dump(state["history"], f, indent=2)

        if "chat_messages" in state:
            with open(save_path / "Core" / "chat_messages.json", "w", encoding="utf-8") as f:
                json.dump(state["chat_messages"], f, indent=2)

        if "module_configs" in state:
            with open(save_path / "Core" / "module_configs.json", "w", encoding="utf-8") as f:
                json.dump(state["module_configs"], f, indent=2)

        if "prompt_pipeline" in state:
            with open(save_path / "Core" / "prompt_pipeline.json", "w", encoding="utf-8") as f:
                json.dump(state["prompt_pipeline"], f, indent=2)
                
        # Update character states if they exist in state
        if "characters" in state:
            for char_id, char_data in state["characters"].items():
                with open(save_path / "Characters" / f"{char_id}.json", "w", encoding="utf-8") as f:
                    json.dump(char_data, f, indent=2)
                    
        # Update Module States
        if "module_data" in state:
            # Clear old to handle deleted states cleanly
            for old_mod in (save_path / "Module_States").glob("*.json"):
                old_mod.unlink()
                
            for mod_name, mod_data in state["module_data"].items():
                with open(save_path / "Module_States" / f"{mod_name}.json", "w", encoding="utf-8") as f:
                    json.dump(mod_data, f, indent=2)
                    
        # Create Snapshot
        self._create_snapshot(save_id, turn_number)
        
        # Pack everything into .wbx
        self._pack_save(save_id)

    def save_module_configs(self, save_id: str, module_configs: dict):
        """Persist module settings without creating a gameplay snapshot."""
        save_path = self.saves_dir / save_id
        if not save_path.exists():
            raise Exception("Save workspace not found. Must be loaded first.")

        with open(save_path / "Core" / "module_configs.json", "w", encoding="utf-8") as f:
            json.dump(module_configs, f, indent=2)

        self._pack_save(save_id)

    def save_prompt_pipeline(self, save_id: str, prompt_pipeline: list[dict[str, Any]]):
        """Persist prompt block settings without creating a gameplay snapshot."""
        save_path = self.saves_dir / save_id
        if not save_path.exists():
            raise Exception("Save workspace not found. Must be loaded first.")

        with open(save_path / "Core" / "prompt_pipeline.json", "w", encoding="utf-8") as f:
            json.dump(prompt_pipeline, f, indent=2)

        self._pack_save(save_id)

    # --- Global prompt pipeline ---

    def _global_pipeline_path(self) -> Path:
        return self.data_dir / "global_prompt_pipeline.json"

    def load_global_prompt_pipeline(self) -> list[dict[str, Any]]:
        path = self._global_pipeline_path()
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return default_prompt_pipeline()

    def save_global_prompt_pipeline(self, prompt_pipeline: list[dict[str, Any]]):
        path = self._global_pipeline_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prompt_pipeline, f, indent=2)

    def _create_snapshot(self, save_id: str, turn_number: int):
        """Creates a snapshot zip of Characters and Module_States"""
        save_path = self.saves_dir / save_id
        snap_path = save_path / "Snapshots" / f"turn_{turn_number}.zip"
        
        with zipfile.ZipFile(snap_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            chars_dir = save_path / "Characters"
            mods_dir = save_path / "Module_States"
            core_dir = save_path / "Core"
            for file in ["chat_history.json", "chat_messages.json", "prompt_pipeline.json"]:
                fpath = core_dir / file
                if fpath.exists():
                    zipf.write(fpath, os.path.join("Core", file))
            
            for root, dirs, files in os.walk(chars_dir):
                for file in files:
                    fpath = os.path.join(root, file)
                    zipf.write(fpath, os.path.join("Characters", file))
                    
            for root, dirs, files in os.walk(mods_dir):
                for file in files:
                    fpath = os.path.join(root, file)
                    zipf.write(fpath, os.path.join("Module_States", file))
                    
        # Cleanup old snapshots (keep last 10)
        snaps = sorted(list((save_path / "Snapshots").glob("turn_*.zip")), 
                      key=lambda x: int(x.stem.split("_")[1]))
        while len(snaps) > 10:
            snaps[0].unlink()
            snaps.pop(0)

    def delete_save(self, save_id: str):
        """Remove both the workspace directory and the .wbx archive."""
        save_path = self.saves_dir / save_id
        wbx_path = self.saves_dir / f"{save_id}.wbx"
        if save_path.exists():
            shutil.rmtree(save_path)
        if wbx_path.exists():
            wbx_path.unlink()

    def undo_turn(self, save_id: str, target_turn: int) -> dict:
        """Restores state from a snapshot and removes newer snapshots."""
        save_path = self.saves_dir / save_id
        snap_path = save_path / "Snapshots" / f"turn_{target_turn}.zip"
        
        if not snap_path.exists():
            raise FileNotFoundError(f"Snapshot for turn {target_turn} not found.")
            
        # Clean current Characters and Module_States
        for f in (save_path / "Characters").glob("*.json"): f.unlink()
        for f in (save_path / "Module_States").glob("*.json"): f.unlink()
        
        # Extract snapshot
        with zipfile.ZipFile(snap_path, 'r') as zipf:
            zipf.extractall(save_path)
            
        # Cleanup newer snapshots
        for snap in (save_path / "Snapshots").glob("turn_*.zip"):
            snap_turn = int(snap.stem.split("_")[1])
            if snap_turn > target_turn:
                snap.unlink()
                
        # Update metadata to reflect target turn
        with open(save_path / "Core" / "metadata.json", "w", encoding="utf-8") as f:
            json.dump({"turn": target_turn}, f, indent=2)
            
        self._pack_save(save_id)
        return self.load_save(save_id)
