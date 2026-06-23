import json
import os
import uuid
from typing import Any, Optional

from backend.engine.prompt_pipeline import PromptPipelineValidationError, ALLOWED_BLOCK_TYPES, ALLOWED_ROLES, ALLOWED_PLACEMENTS


CATEGORIES = ["system_prompt", "post_history", "narrator", "world_context", "character", "utility", "other"]


class PromptLibrary:
    def __init__(self, library_path: str):
        self._path = library_path
        self._ensure_storage()

    def _ensure_storage(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        if not os.path.exists(self._path):
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write(self, templates: list[dict[str, Any]]):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(templates, f, indent=2, ensure_ascii=False)

    def list_templates(self, category: Optional[str] = None) -> list[dict[str, Any]]:
        templates = self._read()
        if category:
            templates = [t for t in templates if t.get("category") == category]
        return templates

    def get_template(self, template_id: str) -> Optional[dict[str, Any]]:
        for t in self._read():
            if t.get("id") == template_id:
                return t
        return None

    def create_template(self, name: str, config: dict[str, Any], category: str = "other") -> dict[str, Any]:
        templates = self._read()

        block_type = config.get("type", "static_text")
        if block_type not in ALLOWED_BLOCK_TYPES:
            raise PromptPipelineValidationError(f"Unsupported block type: {block_type}")

        role_type = config.get("role_type", "system")
        if role_type not in ALLOWED_ROLES:
            raise PromptPipelineValidationError(f"Unsupported role_type: {role_type}")

        placement = config.get("placement", "system_relative")
        if placement not in ALLOWED_PLACEMENTS:
            raise PromptPipelineValidationError(f"Unsupported placement: {placement}")

        if category not in CATEGORIES:
            category = "other"

        template_id = config.get("id") or f"template_{uuid.uuid4().hex[:8]}"

        existing_ids = {t.get("id") for t in templates}
        if template_id in existing_ids:
            template_id = f"{template_id}_{uuid.uuid4().hex[:4]}"

        template = {
            "id": template_id,
            "name": name or template_id,
            "category": category,
            "type": block_type,
            "role_type": role_type,
            "placement": placement,
            "config": config.get("config", {"text": ""}),
            "depth": config.get("depth") if placement == "chat_injection" else None,
        }

        templates.append(template)
        self._write(templates)
        return template

    def update_template(self, template_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        templates = self._read()
        for i, t in enumerate(templates):
            if t.get("id") == template_id:
                updated = {**t, **patch}
                if updated.get("category") not in CATEGORIES:
                    updated["category"] = "other"
                if updated.get("type") not in ALLOWED_BLOCK_TYPES:
                    raise PromptPipelineValidationError(f"Unsupported block type: {updated['type']}")
                if updated.get("role_type") not in ALLOWED_ROLES:
                    raise PromptPipelineValidationError(f"Unsupported role_type: {updated['role_type']}")
                if updated.get("placement") not in ALLOWED_PLACEMENTS:
                    raise PromptPipelineValidationError(f"Unsupported placement: {updated['placement']}")
                templates[i] = updated
                self._write(templates)
                return updated
        raise PromptPipelineValidationError(f"Template not found: {template_id}")

    def delete_template(self, template_id: str):
        templates = self._read()
        original_len = len(templates)
        templates = [t for t in templates if t.get("id") != template_id]
        if len(templates) == original_len:
            raise PromptPipelineValidationError(f"Template not found: {template_id}")
        self._write(templates)

    def template_to_block(self, template_id: str, block_id: Optional[str] = None) -> dict[str, Any]:
        template = self.get_template(template_id)
        if not template:
            raise PromptPipelineValidationError(f"Template not found: {template_id}")
        block = {
            "id": block_id or template["id"],
            "type": template["type"],
            "source": "user",
            "enabled": True,
            "role_type": template["role_type"],
            "placement": template["placement"],
            "depth": template.get("depth"),
            "config": template.get("config", {"text": ""}),
            "display_name": template.get("name", ""),
            "category": template.get("category", "other"),
        }
        return block


def _get_base_data_dir() -> str:
    base = os.path.join(os.path.dirname(__file__), "..", "..")
    return os.path.abspath(os.path.join(base, "data"))


def get_default_library_path() -> str:
    return os.path.join(_get_base_data_dir(), "prompt_library.json")
