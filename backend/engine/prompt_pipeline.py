import re
from copy import deepcopy
from typing import Any


ALLOWED_BLOCK_TYPES = {"static_text", "engine_context", "module_prompt", "world_context"}
ALLOWED_PLACEMENTS = {"system_relative", "chat_injection"}
ALLOWED_ROLES = {"system", "user", "assistant"}
ALLOWED_CATEGORIES = {"system_prompt", "post_history", "narrator", "world_context", "character", "utility", "other"}
ALLOWED_GENERATION_TYPES = {"storytelling", "world_building", "character_creation", "narration", "combat", "memory", "validation"}

AVAILABLE_MACROS = [
    {"key": "${player_name}", "description": "Name of the player character"},
    {"key": "${world_name}", "description": "Name of the current world"},
    {"key": "${world_genre}", "description": "Genre of the current world"},
    {"key": "${world_tone}", "description": "Narrative tone of the world"},
    {"key": "${world_magic_level}", "description": "Magic level setting"},
    {"key": "${world_tech_era}", "description": "Technology era setting"},
    {"key": "${world_lethality}", "description": "Lethality score (0-10)"},
    {"key": "${world_premise}", "description": "World premise/lore"},
    {"key": "${world_conflict}", "description": "Central conflict"},
    {"key": "${player_location}", "description": "Current player location node ID"},
    {"key": "${player_region}", "description": "Current player region name"},
    {"key": "${player_layer}", "description": "Current player layer (overworld, underground, etc.)"},
    {"key": "${player_layer_desc}", "description": "Current layer description (terrain, climate, etc.)"},
    {"key": "${turn_number}", "description": "Current turn number"},
]


def default_prompt_pipeline() -> list[dict[str, Any]]:
    return [
        {
            "id": "core_narrator_rules",
            "type": "static_text",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "display_name": "Core Narrator Rules",
            "category": "system_prompt",
            "generation_types": None,
            "config": {
                "text": "You are a creative storyteller in a text-based RPG. Never mention stat names, numeric values, game mechanics, or meta-game terms in your narration. Describe character capabilities through narrative prose only.",
            },
        },
        {
            "id": "world_rules_context",
            "type": "world_context",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "display_name": "World Rules & Lore",
            "category": "world_context",
            "generation_types": None,
            "config": {},
        },
        {
            "id": "engine_context",
            "type": "engine_context",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "display_name": "Engine Context",
            "category": "utility",
            "generation_types": None,
            "config": {
                "empty_text": "No additional engine context.",
            },
        },
        {
            "id": "storyteller_task",
            "type": "static_text",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "display_name": "Storyteller Task",
            "category": "post_history",
            "generation_types": None,
            "config": {
                "text": "Describe the outcome of the player's action and the current environment. Write 2-4 concise paragraphs. Do not use bullet points or lists.",
            },
        },
    ]


class PromptPipelineValidationError(ValueError):
    pass


class PromptCompiler:

    def resolve_macros(self, text: str, state: dict[str, Any]) -> str:
        if not text or "${" not in text:
            return text

        world_data = state.get("world_data") or {}
        rules = world_data.get("rules", {})
        lore = world_data.get("lore", {})

        characters = state.get("characters") or {}
        player_char = characters.get("default_player", {})
        substitutions = {
            "${player_name}": player_char.get("name", "Adventurer"),
            "${world_name}": lore.get("world_name", "Unknown World"),
            "${world_genre}": rules.get("genre", "Fantasy"),
            "${world_tone}": rules.get("tone", "Neutral"),
            "${world_magic_level}": str(rules.get("magic_level", "Medium")),
            "${world_tech_era}": str(rules.get("tech_era", "Medieval")),
            "${world_lethality}": str(rules.get("lethality", "5")),
            "${world_premise}": lore.get("premise", ""),
            "${world_conflict}": lore.get("central_conflict", ""),
            "${player_location}": state.get("player_location_node_id", "Unknown"),
            "${player_region}": state.get("player_location_region", "Unknown"),
            "${player_layer}": self._get_layer_name(state),
            "${player_layer_desc}": self._get_layer_desc(state),
            "${turn_number}": str(state.get("turn", 0)),
            "$t(": "<invalid_macro>$t(",
        }

        def replacer(match):
            key = match.group(0)
            return substitutions.get(key, key)

        return re.sub(r"\$\{[a-z_]+\}", replacer, text)

    def _get_layer_name(self, state: dict[str, Any]) -> str:
        layer_id = state.get("player_location_layer_id", "")
        if not layer_id:
            return "Unknown"
        world_data = state.get("world_data") or {}
        layers = world_data.get("layers", [])
        for layer in layers:
            if layer.get("layer_id") == layer_id:
                return layer.get("name", layer_id)
        return layer_id.replace("_", " ").title()

    def _get_layer_desc(self, state: dict[str, Any]) -> str:
        layer_id = state.get("player_location_layer_id", "")
        if not layer_id:
            return ""
        world_data = state.get("world_data") or {}
        layers = world_data.get("layers", [])
        for layer in layers:
            if layer.get("layer_id") == layer_id:
                return layer.get("description", "")
        return ""

    def normalize_pipeline(self, pipeline: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        normalized = deepcopy(pipeline if pipeline is not None else default_prompt_pipeline())
        if not isinstance(normalized, list):
            raise PromptPipelineValidationError("Prompt pipeline must be a list of blocks.")

        seen_ids = set()
        for index, block in enumerate(normalized):
            if not isinstance(block, dict):
                raise PromptPipelineValidationError(f"Prompt block at index {index} must be an object.")

            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id:
                raise PromptPipelineValidationError(f"Prompt block at index {index} must have a non-empty string id.")
            if block_id in seen_ids:
                raise PromptPipelineValidationError(f"Duplicate prompt block id: {block_id}")
            seen_ids.add(block_id)

            block_type = block.get("type")
            if block_type not in ALLOWED_BLOCK_TYPES:
                raise PromptPipelineValidationError(f"Prompt block {block_id} has unsupported type: {block_type}")

            placement = block.get("placement")
            if placement not in ALLOWED_PLACEMENTS:
                raise PromptPipelineValidationError(f"Prompt block {block_id} has unsupported placement: {placement}")

            role_type = block.get("role_type")
            if role_type not in ALLOWED_ROLES:
                raise PromptPipelineValidationError(f"Prompt block {block_id} has unsupported role_type: {role_type}")

            block["enabled"] = bool(block.get("enabled", True))
            block.setdefault("source", "user")
            block.setdefault("config", {})

            if placement == "chat_injection":
                depth = block.get("depth", 0)
                if not isinstance(depth, int) or depth < 0:
                    raise PromptPipelineValidationError(f"Prompt block {block_id} depth must be a non-negative integer.")
                block["depth"] = depth
            else:
                block["depth"] = None

            if block_type == "static_text" and not isinstance(block.get("config", {}).get("text"), str):
                raise PromptPipelineValidationError(f"Static text block {block_id} must define config.text.")

            display_name = block.get("display_name")
            if display_name is not None and not isinstance(display_name, str):
                block["display_name"] = str(display_name)
            block.setdefault("display_name", "")

            category = block.get("category")
            if category is not None and category not in ALLOWED_CATEGORIES:
                category = "other"
            block.setdefault("category", None)

            generation_types = block.get("generation_types")
            if generation_types is not None:
                if not isinstance(generation_types, list):
                    generation_types = None
                else:
                    generation_types = [gt for gt in generation_types if gt in ALLOWED_GENERATION_TYPES]
                    if not generation_types:
                        generation_types = None
            block["generation_types"] = generation_types

        return normalized

    def compile(
        self,
        state: dict[str, Any],
        pipeline: list[dict[str, Any]] | None = None,
        module_blocks: list[dict[str, Any]] | None = None,
        validation_veto: str | None = None,
        generation_type: str | None = None,
    ) -> dict[str, Any]:
        blocks = self.normalize_pipeline(pipeline if pipeline is not None else state.get("prompt_pipeline"))
        if module_blocks:
            blocks.extend(self.normalize_pipeline(module_blocks))
            self._validate_unique_block_ids(blocks)
        if validation_veto:
            blocks.append(self._validation_veto_block(validation_veto))

        system_relative_messages = []
        chat_injections = []
        trace = []

        for block in blocks:
            if generation_type:
                block_gen_types = block.get("generation_types")
                if block_gen_types and generation_type not in block_gen_types:
                    trace.append(self._trace(block, skipped=True, reason=f"filtered: {generation_type}"))
                    continue

            if not block.get("enabled", True):
                trace.append(self._trace(block, skipped=True, reason="disabled"))
                continue

            content = self._render_block(block, state)
            if not content.strip():
                trace.append(self._trace(block, skipped=True, reason="empty"))
                continue

            message = {"role": block["role_type"], "content": content}
            if block["placement"] == "system_relative":
                system_relative_messages.append(message)
                trace.append(self._trace(block, message_index=len(system_relative_messages) - 1))
            else:
                chat_injections.append((block, message))

        chat_messages = self._chat_messages(state)
        for block, message in chat_injections:
            insert_index = max(0, len(chat_messages) - block["depth"])
            chat_messages.insert(insert_index, message)
            trace.append(self._trace(block, message_index=len(system_relative_messages) + insert_index))

        return {
            "messages": system_relative_messages + chat_messages,
            "trace": trace,
        }

    def _validate_unique_block_ids(self, blocks: list[dict[str, Any]]):
        seen_ids = set()
        for block in blocks:
            block_id = block.get("id")
            if block_id in seen_ids:
                raise PromptPipelineValidationError(f"Duplicate prompt block id: {block_id}")
            seen_ids.add(block_id)

    def _render_block(self, block: dict[str, Any], state: dict[str, Any]) -> str:
        block_type = block["type"]
        config = block.get("config", {})

        if block_type == "static_text":
            text = config.get("text", "")
            return self.resolve_macros(text, state)

        if block_type == "engine_context":
            context_blocks = state.get("current_context", [])
            context_text = "\n".join(context_blocks).strip()
            if not context_text:
                context_text = config.get("empty_text", "No additional engine context.")
            return self.resolve_macros(f"Current Game State:\n{context_text}", state)

        if block_type == "world_context":
            text = self._render_world_context(state, config)
            return self.resolve_macros(text, state)

        if block_type == "module_prompt":
            text = config.get("text", "")
            return self.resolve_macros(text, state)

        return ""

    def _render_world_context(self, state: dict[str, Any], config: dict[str, Any]) -> str:
        world_data = state.get("world_data")
        if not world_data:
            return ""
        rules = world_data.get("rules", {})
        lore = world_data.get("lore", {})
        parts = []
        if rules:
            parts.append("<world_rules>")
            parts.append(f"Genre: {rules.get('genre', 'N/A')}")
            parts.append(f"Tone: {rules.get('tone', 'N/A')}")
            parts.append(f"Magic Level: {rules.get('magic_level', 'N/A')}")
            parts.append(f"Technology Era: {rules.get('tech_era', 'N/A')}")
            parts.append(f"Lethality: {rules.get('lethality', 'N/A')}/10")
            custom_rules = rules.get("custom_rules", [])
            if custom_rules:
                parts.append("Custom Rules:")
                for rule in custom_rules:
                    parts.append(f"  - {rule}")
            parts.append("</world_rules>")
        if lore:
            parts.append("<world_premise>")
            world_name = lore.get("world_name", "")
            if world_name:
                parts.append(f"World: {world_name}")
            premise = lore.get("premise", "")
            if premise:
                parts.append(premise)
            central_conflict = lore.get("central_conflict", "")
            if central_conflict:
                parts.append(f"Central Conflict: {central_conflict}")
            creation_myth = lore.get("creation_myth", "")
            if creation_myth:
                parts.append(f"Creation Myth: {creation_myth}")
            eras = lore.get("historical_eras", [])
            if eras:
                parts.append("Historical Eras:")
                for era in eras:
                    parts.append(f"  - {era.get('name', '')}: {era.get('summary', '')}")
            parts.append("</world_premise>")
        return "\n".join(parts)

    def _chat_messages(self, state: dict[str, Any]) -> list[dict[str, str]]:
        messages = []
        for message in state.get("chat_messages", []):
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "ai":
                role = "assistant"
            if role in {"user", "assistant", "system"}:
                messages.append({"role": role, "content": content})

        input_text = state.get("input_text") or "I look around."
        messages.append({"role": "user", "content": input_text})
        return messages

    def _validation_veto_block(self, validation_veto: str) -> dict[str, Any]:
        return {
            "id": "engine_validation_veto",
            "type": "static_text",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "chat_injection",
            "depth": 0,
            "display_name": "Validation Veto",
            "category": "utility",
            "generation_types": None,
            "config": {
                "text": validation_veto,
            },
        }

    def _trace(
        self,
        block: dict[str, Any],
        message_index: int | None = None,
        skipped: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": block.get("id"),
            "type": block.get("type"),
            "source": block.get("source"),
            "role_type": block.get("role_type"),
            "placement": block.get("placement"),
            "depth": block.get("depth"),
            "skipped": skipped,
        }
        if message_index is not None:
            entry["message_index"] = message_index
        if reason:
            entry["reason"] = reason
        return entry
