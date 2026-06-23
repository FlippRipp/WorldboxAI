"""Modular settings registry with per-save and global persistence and change callbacks."""
import json
import logging
import os
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"slider", "toggle", "select", "text", "secret"}

Callback = Callable[[Any, Any], Awaitable[None]]


class SettingsRegistry:
    def __init__(self, save_workspace: str = ""):
        self._definitions: dict[str, dict] = {}
        self._values: dict[str, Any] = {}
        self._callbacks: dict[str, list[Callback]] = {}
        self._workspace = save_workspace
        self._global_path = ""

    def bind_workspace(self, save_workspace: str):
        self._workspace = save_workspace
        self._load()

    def bind_global(self, global_path: str):
        self._global_path = global_path
        self._load_global()

    def register(
        self,
        key: str,
        type: str,
        default: Any,
        label: str = "",
        category: str = "General",
        description: str = "",
        is_global: bool = False,
        **constraints,
    ) -> "SettingsRegistry":
        if type not in ALLOWED_TYPES:
            raise ValueError(f"Unsupported setting type: {type}. Allowed: {ALLOWED_TYPES}")

        self._definitions[key] = {
            "key": key,
            "type": type,
            "label": label or key,
            "category": category,
            "description": description,
            "default": default,
            "is_global": is_global,
            **constraints,
        }
        self._values.setdefault(key, default)
        return self

    def on_change(self, key: str, callback: Callback) -> "SettingsRegistry":
        if key not in self._definitions:
            raise KeyError(f"Cannot register callback for unknown setting: {key}")
        self._callbacks.setdefault(key, []).append(callback)
        return self

    def get(self, key: str) -> Any:
        return self._values.get(key, self._definitions.get(key, {}).get("default"))

    async def set(self, key: str, value: Any) -> None:
        if key not in self._definitions:
            raise KeyError(f"Unknown setting: {key}")

        defn = self._definitions[key]
        value = self._coerce(value, defn)
        old = self._values.get(key, defn["default"])

        if value == old:
            return

        self._values[key] = value
        self._save_for_key(key)

        for cb in self._callbacks.get(key, []):
            try:
                await cb(value, old)
            except Exception as e:
                logger.error(f"Callback for {key} raised: {e}")

    async def update(self, updates: dict[str, Any], scope: str = None) -> None:
        for key, value in updates.items():
            if key not in self._definitions:
                continue
            if scope and self._definitions[key].get("is_global") != (scope == "global"):
                continue
            await self.set(key, value)

    def get_all_descriptors(self, scope: str = "all") -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for key, defn in self._definitions.items():
            if scope == "global" and not defn.get("is_global"):
                continue
            if scope == "story" and defn.get("is_global"):
                continue
            descriptor = {**defn, "value": self.get(key)}
            if defn["type"] == "secret" and descriptor["value"]:
                descriptor["value"] = "••••••••"
            category = defn.get("category", "General")
            grouped.setdefault(category, []).append(descriptor)
        return grouped

    def get_values(self) -> dict[str, Any]:
        return dict(self._values)

    def _coerce(self, value: Any, defn: dict) -> Any:
        type_ = defn["type"]
        if type_ == "slider":
            v = int(value)
            return max(defn.get("min", v), min(defn.get("max", v), v))
        if type_ == "toggle":
            return bool(value)
        if type_ == "select":
            options = [o["value"] if isinstance(o, dict) else o for o in defn.get("options", [])]
            if options and value not in options:
                raise ValueError(f"Invalid select value: {value}. Options: {options}")
            return value
        if type_ in ("text", "secret"):
            return str(value)
        return value

    def _is_global(self, key: str) -> bool:
        return self._definitions.get(key, {}).get("is_global", False)

    def _save_path(self, key: str = None) -> str:
        if key and self._is_global(key):
            return self._global_path
        return os.path.join(self._workspace, "Core", "engine_settings.json")

    def _save_for_key(self, key: str):
        if self._is_global(key):
            self._save_global()
        else:
            self._save()

    def _save(self):
        if not self._workspace:
            return
        path = os.path.join(self._workspace, "Core", "engine_settings.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        story_values = {k: v for k, v in self._values.items() if not self._is_global(k)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(story_values, f, indent=2)

    def _save_global(self):
        if not self._global_path:
            return
        os.makedirs(os.path.dirname(self._global_path), exist_ok=True)
        global_values = {k: v for k, v in self._values.items() if self._is_global(k)}
        with open(self._global_path, "w", encoding="utf-8") as f:
            json.dump(global_values, f, indent=2)

    def _load(self):
        path = os.path.join(self._workspace, "Core", "engine_settings.json")
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key, value in saved.items():
                if key in self._definitions and not self._is_global(key):
                    defn = self._definitions[key]
                    self._values[key] = self._coerce(value, defn)
        except Exception as e:
            logger.warning(f"Failed to load engine settings: {e}")

    def _load_global(self):
        if not self._global_path or not os.path.isfile(self._global_path):
            return
        try:
            with open(self._global_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key, value in saved.items():
                if key in self._definitions and self._is_global(key):
                    defn = self._definitions[key]
                    self._values[key] = self._coerce(value, defn)
        except Exception as e:
            logger.warning(f"Failed to load global settings: {e}")
