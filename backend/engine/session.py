from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import json
import re
import shutil

from backend.engine.save_manager import SaveManager
from backend.engine.prompt_pipeline import PromptCompiler, STORY_STYLE_FIELDS
from backend.engine.settings_registry import SettingsRegistry


def sanitize_module_instructions(value: Any) -> dict[str, dict[str, str]]:
    """Coerce instruction overrides to {mod_id: {slot_id: text}} with stripped,
    non-empty strings only; anything malformed is dropped silently."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(value, dict):
        return out
    for mod_id, slots in value.items():
        if not isinstance(mod_id, str) or not isinstance(slots, dict):
            continue
        clean = {}
        for slot_id, text in slots.items():
            if isinstance(slot_id, str) and isinstance(text, str) and text.strip():
                clean[slot_id] = text.strip()
        if clean:
            out[mod_id] = clean
    return out


class GameSessionManager:
    """Owns the active local play session and bridges saves to engine state."""

    def __init__(self, data_dir: str, settings: SettingsRegistry = None):
        self.data_dir = Path(data_dir)
        self.save_manager = SaveManager(str(self.data_dir))
        self.prompt_compiler = PromptCompiler()
        self.settings = settings or SettingsRegistry()
        # No story is active until one is created or loaded. Saves exist only
        # when the player makes them — there is no implicit default slot.
        self.active_save_id: Optional[str] = None
        self.state = self._empty_state()
        # Restore the save that was active before the last shutdown, so a server
        # restart lands the session where the player left off. Any problem
        # (deleted save, corrupt marker) leaves the session without a story.
        restored = self._read_active_marker()
        if restored:
            try:
                self.load_save(restored)
            except Exception as exc:
                print(f"[Session] Could not restore last active save '{restored}': {exc}. Starting without an active story.")

    def _empty_state(self) -> dict[str, Any]:
        """Baseline state while no story is loaded (menu screens, fresh boot).
        Global resources (prompt pipeline, continue prompt) are still live so
        Prompt Studio and previews work outside a story."""
        return {
            "active_save_id": None,
            "active_display_name": None,
            "input_text": "",
            "module_data": {},
            "module_configs": {},
            "characters": {},
            "current_context": [],
            "history": [],
            "chat_messages": [],
            "prompt_pipeline": self.prompt_compiler.normalize_pipeline(
                self.save_manager.load_global_prompt_pipeline()
            ),
            "continue_prompt": self.save_manager.load_continue_prompt(),
            "last_prompt_trace": [],
            "turn": 0,
            "story_style": {},
        }

    def _require_active_save(self):
        if not self.active_save_id:
            raise ValueError("No story is loaded. Create or load a story first.")

    # ── Active-save persistence (survives restarts) ──────────────────────

    def _active_marker_path(self) -> Path:
        return self.data_dir / "saves" / "active_save.json"

    def _read_active_marker(self) -> Optional[str]:
        path = self._active_marker_path()
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                save_id = json.load(f).get("save_id", "")
            return save_id if isinstance(save_id, str) and save_id else None
        except (json.JSONDecodeError, OSError):
            return None

    def _persist_active_save_id(self, save_id: str):
        try:
            with open(self._active_marker_path(), "w", encoding="utf-8") as f:
                json.dump({"save_id": save_id}, f, indent=2)
        except OSError as exc:
            print(f"[Session] Could not persist active save marker: {exc}")

    def _validate_save_id(self, save_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", save_id):
            raise ValueError("Save id may only contain letters, numbers, underscores, and hyphens.")

    def derive_save_id(self, name: str) -> str:
        """Turn a human story name into a filesystem-safe save id: runs of
        whitespace become single underscores. Already-normalized ids pass
        through unchanged, so this is safe to apply at every entry point."""
        save_id = re.sub(r"\s+", "_", (name or "").strip())
        self._validate_save_id(save_id)
        return save_id

    def _display_name_for(self, save_id: str) -> str:
        """Fallback display name when metadata has none: the id with the
        underscores shown as spaces again."""
        return (save_id or "").replace("_", " ")

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
            # Display metadata comes straight from Core/metadata.json (or the
            # archive) — no full save load, so listing stays cheap.
            metadata = self.save_manager.read_core_json(save_id, "metadata.json", {}) or {}
            saves.append({
                "id": save_id,
                "active": save_id == self.active_save_id,
                "workspace_exists": workspace.exists(),
                "archive_exists": archive.exists(),
                "display_name": metadata.get("display_name") or self._display_name_for(save_id),
                "turn": metadata.get("turn", 0),
                "last_played": metadata.get("last_played"),
            })
        # Most recently played first; never-played saves keep alphabetical order
        # at the end (stable sort preserves the id ordering for equal keys).
        saves.sort(key=lambda s: s["last_played"] or "", reverse=True)
        return saves

    def create_save(self, save_id: str, world_id: str = None, player_location_node_id: str = None, player_location_region: str = None, player_location_layer_id: str = None, revealed_node_ids: list = None, character_module_data: dict = None, character_data: dict = None) -> dict[str, Any]:
        # The name the player typed may contain spaces; the id (and thus all
        # paths) uses underscores, while the display name keeps the spaces.
        save_id = self.derive_save_id(save_id)
        saves_dir = self.data_dir / "saves"
        save_workspace = saves_dir / save_id
        save_archive = saves_dir / f"{save_id}.wbx"
        if save_workspace.exists() or save_archive.exists():
            raise FileExistsError(f"Save '{save_id}' already exists.")

        metadata = {
            "turn": 0,
            "display_name": self._display_name_for(save_id),
        }
        if world_id:
            metadata["world_id"] = world_id
            metadata["player_location_node_id"] = player_location_node_id or ""
            metadata["player_location_region"] = player_location_region or ""
            metadata["player_location_layer_id"] = player_location_layer_id or ""
            metadata["revealed_node_ids"] = revealed_node_ids or []

        module_data = character_module_data if character_module_data else {"wb_core_rpg": {"hp": 85, "max_hp": 85}}

        # Carry the chosen character's identity into the save so the in-story
        # character view, prompt macros (${player_name}), and narration reflect
        # who the player actually created — not the generic default.
        player_record = {
            "id": "default_player",
            "name": "Adventurer",
            "module_data": module_data,
        }
        if character_data:
            for field in ("name", "gender", "race", "short_appearance", "full_appearance", "personality", "context"):
                value = character_data.get(field)
                if value:
                    player_record[field] = value

        self.save_manager.create_player_template("default_player", player_record)
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
        self._persist_active_save_id(save_id)
        return self.state

    def delete_save(self, save_id: str):
        self._validate_save_id(save_id)
        self.save_manager.delete_save(save_id)
        if save_id == self.active_save_id:
            # The active story is gone: drop back to the no-story baseline.
            self.active_save_id = None
            self.settings.bind_workspace("")
            self.state = self._empty_state()
        # The next boot must not try to restore a deleted save.
        if self._read_active_marker() == save_id:
            self._persist_active_save_id("")

    def load_active_state(self) -> dict[str, Any]:
        saved_state = self.save_manager.load_save(self.active_save_id)
        metadata = saved_state.get("core", {}).get("metadata", {})
        history = saved_state.get("core", {}).get("chat_history", [])
        chat_messages = saved_state.get("core", {}).get("chat_messages", [])
        # The prompt pipeline is global (a single source of truth shared by every
        # story), not a per-save snapshot. This ensures edits made in Prompt Studio
        # apply to all stories — existing and new — instead of only future saves.
        prompt_pipeline = self.save_manager.load_global_prompt_pipeline()

        state = {
            "active_save_id": self.active_save_id,
            "active_display_name": metadata.get("display_name") or self._display_name_for(self.active_save_id),
            "input_text": "",
            "module_data": saved_state.get("module_data", {}),
            "module_configs": saved_state.get("module_configs", {}),
            "characters": saved_state.get("characters", {}),
            "current_context": [],
            "history": history,
            "chat_messages": chat_messages,
            "prompt_pipeline": self.prompt_compiler.normalize_pipeline(prompt_pipeline),
            "continue_prompt": self.save_manager.load_continue_prompt(),
            "last_prompt_trace": [],
            "turn": metadata.get("turn", 0),
            "world_id": metadata.get("world_id"),
            "player_location_node_id": metadata.get("player_location_node_id"),
            "player_location_region": metadata.get("player_location_region"),
            "player_location_layer_id": metadata.get("player_location_layer_id"),
            "revealed_node_ids": metadata.get("revealed_node_ids", []),
            "sticky_world_entries": metadata.get("sticky_world_entries", {}),
            "story_style": metadata.get("story_style") or {},
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

        # Basic scenario story source (parallel to World/world_data.json).
        scenario_file = self.data_dir / "saves" / self.active_save_id / "Scenario" / "scenario.json"
        if scenario_file.exists():
            import json as _json
            with open(scenario_file, "r", encoding="utf-8") as f:
                state["scenario_data"] = _json.load(f)

        return state

    def set_input(self, input_text: str):
        self.state["input_text"] = input_text

    @staticmethod
    def build_message_meta(model: Optional[str] = None, usage: Optional[dict] = None) -> dict[str, Any]:
        """Display metadata stamped onto chat messages. Absent-tolerant by design:
        older saves have no ``meta`` key and providers may omit usage."""
        meta: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
        if model:
            meta["model"] = model
        if isinstance(usage, dict):
            tokens = {}
            if usage.get("prompt_tokens"):
                tokens["in"] = usage["prompt_tokens"]
            if usage.get("completion_tokens"):
                tokens["out"] = usage["completion_tokens"]
            if tokens:
                meta["tokens"] = tokens
        return meta

    def save_completed_turn(self, final_state: dict[str, Any],
                            user_text: Optional[str] = None) -> dict[str, Any]:
        """Persist a finished turn. Callers that know the input that started
        the turn pass it as `user_text`: the session state can be reloaded
        while a turn generates headless (client closed mid-turn, then reopened
        the story), which resets input_text — reading it back here would
        silently drop the player's message from the transcript."""
        self._require_active_save()
        if user_text is None:
            user_text = self.state.get("input_text", "")
        previous_history = self.state.get("history", [])
        final_history = final_state.get("history", previous_history)
        assistant_text = final_history[-1] if len(final_history) > len(previous_history) and final_history else None
        chat_messages = list(self.state.get("chat_messages", []))
        now = datetime.now(timezone.utc).isoformat()
        if user_text:
            chat_messages.append({"role": "user", "content": user_text, "meta": {"ts": now}})
        if assistant_text:
            ai_message = {"role": "ai", "content": assistant_text}
            reasoning = final_state.get("last_reasoning", "")
            if reasoning:
                ai_message["reasoning"] = reasoning
            ai_message["meta"] = self.build_message_meta(
                final_state.get("last_model"), final_state.get("last_usage")
            )
            chat_messages.append(ai_message)

        self.state = {
            **self.state,
            **final_state,
            "active_save_id": self.active_save_id,
            "input_text": "",
            "current_context": [],
            "lore_depth_injections": [],
            "chat_messages": chat_messages,
            "world_id": final_state.get("world_id", self.state.get("world_id")),
            "world_data": final_state.get("world_data", self.state.get("world_data")),
            "player_location_node_id": final_state.get("player_location_node_id", self.state.get("player_location_node_id")),
            "player_location_region": final_state.get("player_location_region", self.state.get("player_location_region")),
            "player_location_layer_id": final_state.get("player_location_layer_id", self.state.get("player_location_layer_id")),
            "revealed_node_ids": final_state.get("revealed_node_ids", self.state.get("revealed_node_ids", [])),
            "sticky_world_entries": final_state.get("sticky_world_entries", self.state.get("sticky_world_entries", {})),
        }
        turn = self.state.get("turn", 0)
        self.save_manager.save_turn(self.active_save_id, self.state, turn)
        return self.state

    def update_module_configs(self, module_configs: dict[str, Any]) -> dict[str, Any]:
        self._require_active_save()
        # Reserved keys (``__active_modules__`` and friends) live alongside the
        # per-module settings but are owned by the host, not the settings UI.
        # The in-game settings modal only sends per-module schema values, so a
        # naive replace would drop the reserved keys and silently re-enable
        # every module. Carry forward any reserved key the caller omitted.
        merged = dict(module_configs)
        existing = self.state.get("module_configs", {})
        for key, value in existing.items():
            if key.startswith("__") and key.endswith("__") and key not in merged:
                merged[key] = value
        self.state["module_configs"] = merged
        self.save_manager.save_module_configs(self.active_save_id, merged)
        return self.state

    def get_save_active_modules(self, save_id: str) -> Optional[list[str]]:
        """Return the reserved active-module set for a save, or None when the
        save predates the toggle (meaning: all modules are active)."""
        self._validate_save_id(save_id)
        if save_id == self.active_save_id:
            configs = self.state.get("module_configs", {})
        else:
            configs = self.save_manager.read_module_configs(save_id)
        value = configs.get("__active_modules__")
        return value if isinstance(value, list) else None

    def set_save_active_modules(self, save_id: str, active_modules: list[str]) -> list[str]:
        """Persist which modules are active for a save. Works on the loaded save
        (updates in-memory state too) or any other save on disk."""
        self._validate_save_id(save_id)
        active = list(active_modules)
        if save_id == self.active_save_id:
            configs = dict(self.state.get("module_configs", {}))
            configs["__active_modules__"] = active
            self.update_module_configs(configs)
        else:
            configs = dict(self.save_manager.read_module_configs(save_id))
            configs["__active_modules__"] = active
            self.save_manager.save_module_configs(save_id, configs)
        return active

    def get_save_module_instructions(self, save_id: str) -> dict[str, dict[str, str]]:
        """Per-module instruction-slot overrides for a save, shaped
        {mod_id: {slot_id: text}}. Stored under the reserved
        ``__module_instructions__`` key so the settings modal's schema-only
        rebuild can never drop them. Empty dict = all defaults."""
        self._validate_save_id(save_id)
        if save_id == self.active_save_id:
            configs = self.state.get("module_configs", {})
        else:
            configs = self.save_manager.read_module_configs(save_id)
        value = configs.get("__module_instructions__")
        return value if isinstance(value, dict) else {}

    def set_save_module_instructions(self, save_id: str, module_instructions: dict) -> dict[str, dict[str, str]]:
        """Persist instruction overrides for a save (loaded or on disk).
        Values are sanitized: non-string or blank slot texts are dropped, and
        modules with no remaining overrides are omitted entirely."""
        self._validate_save_id(save_id)
        sanitized = sanitize_module_instructions(module_instructions)
        if save_id == self.active_save_id:
            configs = dict(self.state.get("module_configs", {}))
            configs["__module_instructions__"] = sanitized
            self.update_module_configs(configs)
        else:
            configs = dict(self.save_manager.read_module_configs(save_id))
            configs["__module_instructions__"] = sanitized
            self.save_manager.save_module_configs(save_id, configs)
        return sanitized

    def get_story_style(self, save_id: str) -> dict[str, str]:
        """The save's editable story direction (themes/tags/pacing); every
        field defaults to "" for saves that never set one."""
        self._validate_save_id(save_id)
        if save_id == self.active_save_id:
            style = self.state.get("story_style") or {}
        else:
            if not (self.data_dir / "saves" / save_id).exists() \
                    and not (self.data_dir / "saves" / f"{save_id}.wbx").exists():
                raise FileNotFoundError(f"Save {save_id} not found.")
            metadata = self.save_manager.read_core_json(save_id, "metadata.json", {}) or {}
            style = metadata.get("story_style") or {}
        return {key: str(style.get(key) or "") for key, _ in STORY_STYLE_FIELDS}

    def set_story_style(self, save_id: str, style: dict[str, Any]) -> dict[str, str]:
        """Persist a save's story direction. Works on the loaded save (updates
        in-memory state too, so the next turn picks it up) or any save on disk."""
        self._validate_save_id(save_id)
        clean = {key: str((style or {}).get(key) or "").strip() for key, _ in STORY_STYLE_FIELDS}
        self.save_manager.update_metadata(save_id, {"story_style": clean})
        if save_id == self.active_save_id:
            self.state["story_style"] = clean
        return clean

    def update_prompt_pipeline(self, prompt_pipeline: list[dict[str, Any]]) -> dict[str, Any]:
        # The pipeline is global, so editing it from within a session writes the
        # shared global pipeline rather than a per-save snapshot.
        normalized = self.prompt_compiler.normalize_pipeline(prompt_pipeline)
        self.state["prompt_pipeline"] = normalized
        self.save_manager.save_global_prompt_pipeline(normalized)
        return self.state

    def update_continue_prompt(self, text: str) -> str:
        # Global setting (shared by every story). Persist and apply to the live
        # session so an empty send continues with the new text immediately.
        clean = text if isinstance(text, str) else ""
        self.save_manager.save_continue_prompt(clean)
        self.state["continue_prompt"] = self.save_manager.load_continue_prompt()
        return self.state["continue_prompt"]

    def undo_turn(self, target_turn: int) -> dict[str, Any]:
        self._require_active_save()
        if target_turn < 0:
            raise ValueError("Target turn must be zero or greater.")
        self.save_manager.undo_turn(self.active_save_id, target_turn)
        self.state = self.load_active_state()
        return self.state

    # ── Swipes / regenerate ──────────────────────────────────────────────

    def swipes_meta(self) -> Optional[dict[str, Any]]:
        """Swipe info for the last turn, for the UI's `i/n` counter (or None)."""
        if not self.active_save_id:
            return None
        m = self.save_manager.load_swipe_manifest(self.active_save_id)
        if not m:
            return None
        return {"turn": m.get("turn"), "active": m.get("active", 0), "count": m.get("count", 1)}

    def _last_turn_input(self) -> str:
        """The player input that produced the current last turn, for regenerate.
        A `continue` turn appends only an ai message (no user message), so the
        input is the message directly before the last ai only when it's a user
        message; otherwise it was a continue and the input is empty."""
        msgs = self.state.get("chat_messages", [])
        if len(msgs) >= 2 and msgs[-1].get("role") == "ai" and msgs[-2].get("role") == "user":
            return msgs[-2].get("content", "")
        return ""

    def begin_turn_swipes(self):
        """After a normal completed turn, start a fresh swipe set (v0 = this gen)."""
        if not self.active_save_id:
            return
        turn = self.state.get("turn", 0)
        if turn <= 0:
            return  # opening scene is not swipeable
        self.save_manager.reset_swipes(self.active_save_id, turn, self._last_turn_input())

    def prepare_regenerate(self) -> int:
        """Roll the workspace back to before the last turn and re-seat its user
        input so the pipeline can produce a fresh generation. Returns the turn
        number being regenerated (caller rolls back memory to turn-1)."""
        self._require_active_save()
        manifest = self.save_manager.load_swipe_manifest(self.active_save_id)
        if not manifest:
            raise ValueError("There is no turn available to regenerate.")
        turn = manifest["turn"]
        if turn <= 0:
            raise ValueError("The opening scene cannot be regenerated.")
        self.save_manager.restore_turn_snapshot(self.active_save_id, turn - 1)
        self.state = self.load_active_state()
        self.set_input(manifest.get("user_input", ""))
        return turn

    def add_regenerated_swipe(self) -> dict[str, Any]:
        self._require_active_save()
        return self.save_manager.add_swipe(self.active_save_id)

    def restore_active_swipe(self) -> Optional[dict[str, Any]]:
        """Undo a `prepare_regenerate` rollback by re-restoring the swipe variant
        that was active before the regenerate started, so a stopped or failed
        regenerate leaves the transcript exactly as it was. Returns the reloaded
        state, or None when there is no swipe set to restore."""
        if not self.active_save_id:
            return None
        manifest = self.save_manager.load_swipe_manifest(self.active_save_id)
        if not manifest:
            return None
        self.save_manager.set_active_swipe(self.active_save_id, manifest.get("active", 0))
        self.state = self.load_active_state()
        self.state["swipes"] = self.swipes_meta()
        return self.state

    def select_swipe(self, index: int) -> dict[str, Any]:
        self._require_active_save()
        self.save_manager.set_active_swipe(self.active_save_id, index)
        self.state = self.load_active_state()
        # Reloading active state drops the transient `swipes` key, so re-attach it
        # (as edit/delete do) — otherwise the client clears its swipe/regenerate
        # controls when selecting a previous generation.
        self.state["swipes"] = self.swipes_meta()
        return self.state

    # ── Edit / delete chat entries ───────────────────────────────────────

    def _history_index_for_message(self, msg_index: int) -> Optional[int]:
        """The `history` (raw story) index for the ai chat message at msg_index.
        The k-th ai message maps to history[k] (intro = history[0])."""
        msgs = self.state.get("chat_messages", [])
        if msg_index < 0 or msg_index >= len(msgs) or msgs[msg_index].get("role") != "ai":
            return None
        ai_count = sum(1 for m in msgs[: msg_index + 1] if m.get("role") == "ai")
        hist_idx = ai_count - 1
        history = self.state.get("history", [])
        return hist_idx if 0 <= hist_idx < len(history) else None

    def edit_message(self, index: int, content: str) -> dict[str, Any]:
        self._require_active_save()
        msgs = list(self.state.get("chat_messages", []))
        if index < 0 or index >= len(msgs):
            raise ValueError("Message index out of range.")
        msgs[index] = {**msgs[index], "content": content}
        self.state["chat_messages"] = msgs
        if msgs[index].get("role") == "ai":
            hist_idx = self._history_index_for_message(index)
            if hist_idx is not None:
                history = list(self.state.get("history", []))
                history[hist_idx] = content
                self.state["history"] = history
        # Editing invalidates the last turn's alternate generations; re-seat the
        # swipe set so the edited version becomes v0 and stays regeneratable.
        self.save_manager.save_turn(self.active_save_id, self.state, self.state.get("turn", 0))
        self.begin_turn_swipes()
        self.state["swipes"] = self.swipes_meta()
        return self.state

    def delete_message(self, index: int) -> dict[str, Any]:
        """Delete a chat entry. Deleting the last turn performs a true rollback
        (reverting state); deleting an older entry is a transcript edit only."""
        self._require_active_save()
        msgs = self.state.get("chat_messages", [])
        n = len(msgs)
        if index < 0 or index >= n:
            raise ValueError("Message index out of range.")
        turn = self.state.get("turn", 0)
        # Last turn = the trailing user+ai pair (indices n-2, n-1) for turn >= 1.
        if turn >= 1 and index >= n - 2:
            self.save_manager.undo_turn(self.active_save_id, turn - 1)
            self.state = self.load_active_state()
        else:
            if turn <= 0:
                raise ValueError("The opening scene cannot be deleted.")
            # Transcript-only delete of an older entry.
            hist_idx = self._history_index_for_message(index) if msgs[index].get("role") == "ai" else None
            new_msgs = list(msgs)
            new_msgs.pop(index)
            self.state["chat_messages"] = new_msgs
            if hist_idx is not None:
                history = list(self.state.get("history", []))
                history.pop(hist_idx)
                self.state["history"] = history
            self.save_manager.clear_swipes(self.active_save_id)
            self.save_manager.save_turn(self.active_save_id, self.state, turn)
        # Re-seat a swipe set for the now-current last turn so its regenerate /
        # swipe controls stay available (no-op for the opening scene).
        self.begin_turn_swipes()
        self.state["swipes"] = self.swipes_meta()
        return self.state

    # ── Chat management: rename / branch / export ────────────────────────

    def rename_save(self, save_id: str, display_name: str) -> dict[str, Any]:
        """Set a save's display name. The id (and thus paths) never changes, so
        renaming is safe on any save, loaded or not."""
        self._validate_save_id(save_id)
        clean = (display_name or "").strip()
        if not clean:
            raise ValueError("Display name cannot be empty.")
        metadata = self.save_manager.update_metadata(save_id, {"display_name": clean[:120]})
        return {"id": save_id, "display_name": metadata.get("display_name")}

    def _next_branch_id(self, source_save_id: str) -> str:
        saves_dir = self.data_dir / "saves"
        for n in range(1, 1000):
            candidate = f"{source_save_id}-b{n}"
            if not (saves_dir / candidate).exists() and not (saves_dir / f"{candidate}.wbx").exists():
                return candidate
        raise ValueError("Could not find a free branch id.")

    def branch_save(self, source_save_id: str, new_save_id: Optional[str] = None,
                    target_turn: Optional[int] = None,
                    display_name: Optional[str] = None) -> dict[str, Any]:
        """Fork a save into a new one, optionally rolled back to the end of
        `target_turn`. The source (including its vector memory) is copied
        wholesale; the copy's memories are rolled back alongside the snapshot.
        The active session is never touched. A blank `display_name` falls back
        to "<source> (branch @ turn N)"."""
        self._validate_save_id(source_save_id)
        source_path = self.save_manager._ensure_workspace(source_save_id)

        if new_save_id:
            new_save_id = self.derive_save_id(new_save_id)
        else:
            new_save_id = self._next_branch_id(source_save_id)
        saves_dir = self.data_dir / "saves"
        new_path = saves_dir / new_save_id
        if new_path.exists() or (saves_dir / f"{new_save_id}.wbx").exists():
            raise FileExistsError(f"Save '{new_save_id}' already exists.")

        source_meta = self.save_manager.read_core_json(source_save_id, "metadata.json", {}) or {}
        current_turn = source_meta.get("turn", 0)
        if target_turn is None:
            target_turn = current_turn
        if target_turn < 0 or target_turn > current_turn:
            raise ValueError(f"Cannot branch at turn {target_turn}: the story is at turn {current_turn}.")

        # Swipe variants belong to the source's last turn only — a fresh set is
        # seated when the branch is next played.
        shutil.copytree(source_path, new_path, ignore=shutil.ignore_patterns("Swipes"))

        try:
            if target_turn != current_turn:
                try:
                    self.save_manager.restore_turn_snapshot(new_save_id, target_turn)
                except FileNotFoundError:
                    raise ValueError(
                        f"Turn {target_turn} is too far back to branch from — only the last 10 turns keep snapshots."
                    )
                # Roll the copied memory index back to the branch point.
                branch_db = new_path / "vector_index"
                if (branch_db / "memories.db").exists():
                    from backend.engine.memory import MemoryManager
                    mm = MemoryManager(str(branch_db), 1)
                    try:
                        mm.rollback_memories(target_turn)
                    finally:
                        mm.close()

            clean_name = (display_name or "").strip()
            if clean_name:
                display_name = clean_name[:120]
            else:
                source_name = source_meta.get("display_name") or source_save_id
                display_name = f"{source_name} (branch @ turn {target_turn})"
            self.save_manager.update_metadata(new_save_id, {"display_name": display_name})
        except Exception:
            # Leave no half-built branch behind.
            shutil.rmtree(new_path, ignore_errors=True)
            wbx = saves_dir / f"{new_save_id}.wbx"
            if wbx.exists():
                wbx.unlink()
            raise

        return {"id": new_save_id, "display_name": display_name, "turn": target_turn}

    def export_transcript(self, save_id: str, fmt: str = "md") -> tuple[str, str, str]:
        """Render a save's transcript for download.
        Returns (content, media_type, filename)."""
        self._validate_save_id(save_id)
        messages = self.save_manager.read_core_json(save_id, "chat_messages.json", None)
        if messages is None:
            raise FileNotFoundError(f"Save {save_id} not found.")
        metadata = self.save_manager.read_core_json(save_id, "metadata.json", {}) or {}
        title = metadata.get("display_name") or save_id

        if fmt == "jsonl":
            import json as _json
            content = "\n".join(_json.dumps(m, ensure_ascii=False) for m in messages) + "\n"
            return content, "application/x-ndjson", f"{save_id}.jsonl"

        if fmt == "txt":
            parts = []
            for m in messages:
                role = m.get("role", "")
                text = m.get("content", "")
                if role == "user":
                    parts.append(f"You: {text}")
                elif role == "ai":
                    parts.append(text)
                else:
                    parts.append(f"[{role}] {text}")
            content = f"{title}\n{'=' * len(title)}\n\n" + "\n\n----\n\n".join(parts) + "\n"
            return content, "text/plain; charset=utf-8", f"{save_id}.txt"

        if fmt == "md":
            parts = [f"# {title}\n"]
            for m in messages:
                role = m.get("role", "")
                text = m.get("content", "")
                if role == "user":
                    parts.append(f"**You:** {text}")
                elif role == "ai":
                    parts.append(text)
                else:
                    parts.append(f"*[{role}] {text}*")
            content = "\n\n---\n\n".join(parts) + "\n"
            return content, "text/markdown; charset=utf-8", f"{save_id}.md"

        raise ValueError(f"Unsupported export format '{fmt}'. Use md, txt, or jsonl.")

    def get_memory_path(self) -> Optional[str]:
        if not self.active_save_id:
            return None
        return str(self.data_dir / "saves" / self.active_save_id / "vector_index")

    def get_status(self) -> dict[str, Any]:
        if not self.active_save_id:
            return {
                "active_save_id": None,
                "active_display_name": None,
                "state_backend": "save_backed_session",
                "turn": 0,
                "history_length": 0,
                "workspace_exists": False,
                "archive_exists": False,
                "memory_path": None,
                "swipes": None,
            }
        save_workspace = self.data_dir / "saves" / self.active_save_id
        save_archive = self.data_dir / "saves" / f"{self.active_save_id}.wbx"
        metadata = self.save_manager.read_core_json(self.active_save_id, "metadata.json", {}) or {}
        return {
            "active_save_id": self.active_save_id,
            "active_display_name": metadata.get("display_name") or self._display_name_for(self.active_save_id),
            "state_backend": "save_backed_session",
            "turn": self.state.get("turn", 0),
            "history_length": len(self.state.get("history", [])),
            "workspace_exists": save_workspace.exists(),
            "archive_exists": save_archive.exists(),
            "memory_path": self.get_memory_path(),
            "swipes": self.swipes_meta(),
        }

    def get_settings_descriptors(self, scope: str = "story") -> dict[str, list[dict]]:
        return self.settings.get_all_descriptors(scope=scope)

    async def update_settings(self, updates: dict[str, Any], scope: str = "story") -> dict[str, list[dict]]:
        await self.settings.update(updates, scope=scope)
        return self.settings.get_all_descriptors(scope=scope)
