import re
from copy import deepcopy
from typing import Any


ALLOWED_BLOCK_TYPES = {"static_text", "engine_context", "module_prompt", "world_context", "character_context", "chat_history"}
ALLOWED_PLACEMENTS = {"system_relative", "chat_injection"}
ALLOWED_ROLES = {"system", "user", "assistant"}
ALLOWED_CATEGORIES = {"system_prompt", "post_history", "narrator", "world_context", "character", "utility", "other"}
ALLOWED_GENERATION_TYPES = {"storytelling", "world_building", "character_creation", "narration", "combat", "memory", "validation"}

# Injected as the final user turn when the player sends an empty message ("continue"):
# the normal context is compiled, minus any player input, plus this instruction.
DEFAULT_CONTINUE_PROMPT = (
    "Continue the story from where it left off. Advance the scene naturally, moving "
    "events, NPCs, and the world forward without waiting for the player to act."
)

# The editable story-style fields ({state key, prompt label}) and how they are
# rendered — shared by the per-turn depth-0 injection and the intro prompt.
STORY_STYLE_FIELDS = [
    ("themes", "Themes"),
    ("tags", "Tags"),
    ("pacing", "Pacing"),
]


def render_story_style(story_style: dict[str, Any] | None) -> str:
    """Render the story's themes/tags/pacing as a <story_style> directive.
    Empty fields are skipped; returns "" when nothing is set."""
    style = story_style or {}
    field_lines = []
    for key, label in STORY_STYLE_FIELDS:
        value = (style.get(key) or "").strip() if isinstance(style.get(key), str) else ""
        if value:
            field_lines.append(f"{label}: {value}")
    if not field_lines:
        return ""
    return "\n".join([
        "<story_style>",
        *field_lines,
        "Let these themes, tags, and pacing guide the style, mood, and rhythm "
        "of the narration. Express them through the story itself — never name "
        "or reference them directly.",
        "</story_style>",
    ])


def build_auto_player_action_prompt(state: dict[str, Any], nudge: str = "") -> str:
    """Prompt for the fast model that plays the player in storyteller auto
    mode: decide the character's next action from their personality and the
    recent story, phrased like a message the player would have typed."""
    characters = state.get("characters") or {}
    player = characters.get("default_player") or {}
    name = (player.get("name") or "").strip() or "the protagonist"

    lines = [
        f"You are playing {name}, the protagonist of an ongoing interactive story. "
        f"Decide what {name} does next, exactly as this character would.",
        "",
    ]

    char_fields = [
        ("Name", (player.get("name") or "").strip()),
        ("Race", (player.get("race") or "").strip()),
        ("Personality", (player.get("personality") or "").strip()),
        ("Appearance", (player.get("short_appearance") or "").strip()),
    ]
    char_lines = [f"{label}: {value}" for label, value in char_fields if value]
    if char_lines:
        lines.append("The character:")
        lines.extend(char_lines)
        lines.append("")

    recent = [entry[-1500:] for entry in (state.get("history") or [])[-2:]]
    if recent:
        lines.append("The story so far (most recent scenes; the \"you\" in the narration is this character):")
        lines.extend(recent)
        lines.append("")

    nudge = (nudge or "").strip()
    if nudge:
        lines.append(f"Direction from the player (follow its intent when deciding the action): {nudge}")
        lines.append("")

    lines.append(
        f"Answer with {name}'s next move as a short player message: 1-3 sentences, "
        f"first person, present tense (e.g. \"I draw my sword and step between them.\"), "
        f"stating what {name} does and says. Declare the attempt only — never its "
        f"outcome, and never other characters' reactions; the storyteller decides "
        f"those. Stay true to the personality and the current scene, and keep the "
        f"story moving. Respond with ONLY that message: no quotes around it, no "
        f"narration, no explanations."
    )
    return "\n".join(lines)

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
            "id": "player_character_context",
            "type": "character_context",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "display_name": "Player Character",
            "category": "character",
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
        {
            "id": "chat_history",
            "type": "chat_history",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "system_relative",
            "depth": None,
            "display_name": "Chat History",
            "category": "utility",
            "generation_types": None,
            "config": {
                # None = the full transcript; N = only the last N player turns
                # (each with the replies that follow it).
                "max_turns": None,
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
        has_chat_history = False
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

            if block_type == "chat_history":
                if has_chat_history:
                    raise PromptPipelineValidationError("Prompt pipeline may only contain one chat_history block.")
                has_chat_history = True
                if placement != "system_relative":
                    raise PromptPipelineValidationError(f"Chat history block {block_id} must use system_relative placement.")
                max_turns = block.get("config", {}).get("max_turns")
                if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 0):
                    raise PromptPipelineValidationError(
                        f"Chat history block {block_id} config.max_turns must be a non-negative integer or null."
                    )

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

        # A pipeline may anchor a module's block into the stack: an entry whose
        # id matches a module block's namespaced id (e.g.
        # "wb_plot_director:plot_thread") takes that block's freshly rendered
        # content at ITS position, with the pipeline entry's own enabled /
        # placement / role / depth in control. Anchors whose module block is
        # absent this turn (module inactive or removed) are skipped with a
        # trace reason instead of erroring. Module blocks nobody anchored keep
        # the historical behavior: appended after the pipeline.
        module_norm = self.normalize_pipeline(module_blocks) if module_blocks else []
        module_by_id = {b["id"]: b for b in module_norm}
        anchored = set()
        for block in blocks:
            rendered = module_by_id.get(block["id"])
            if rendered is not None:
                block["type"] = rendered["type"]
                block["config"] = rendered["config"]
                anchored.add(block["id"])
            elif (
                str(block.get("source", "")).startswith("module:")
                and block.get("type") == "module_prompt"
                and not str(block.get("config", {}).get("text") or "").strip()
            ):
                # An anchor placeholder whose module didn't contribute this
                # turn; a module-sourced block carrying its own text still
                # renders as-is.
                block["_module_unavailable"] = True
        if module_norm:
            blocks.extend(b for b in module_norm if b["id"] not in anchored)
            self._validate_unique_block_ids(blocks)
        if validation_veto:
            blocks.append(self._validation_veto_block(validation_veto))

        # Lorebook entries configured with an injection depth (ST '@ depth'):
        # gather_context routes their text here instead of the lore context
        # block, one pre-grouped message per depth.
        for index, injection in enumerate(state.get("lore_depth_injections") or []):
            depth = injection.get("depth")
            text = injection.get("text", "")
            if isinstance(depth, int) and depth >= 0 and text.strip():
                blocks.append(self._engine_directive_block(
                    f"engine_lore_depth_{index}", f"Lorebook @ depth {depth}",
                    text, depth=depth))

        # Story style (themes/tags/pacing) is injected at depth 0 — the last
        # directive before generation — so it steers every turn's output.
        story_style_block = self._story_style_block(state)
        if story_style_block:
            blocks.append(story_style_block)

        # (message, trace_entry) pairs in pipeline order; a None message marks
        # the slot where the chat history transcript is spliced in.
        system_entries = []
        chat_injections = []
        trace = []
        history_block = None
        has_history_block = False

        for block in blocks:
            is_history = block["type"] == "chat_history"
            if is_history:
                if has_history_block:
                    trace.append(self._trace(block, skipped=True, reason="duplicate chat_history block"))
                    continue
                has_history_block = True

            if generation_type:
                block_gen_types = block.get("generation_types")
                if block_gen_types and generation_type not in block_gen_types:
                    trace.append(self._trace(block, skipped=True, reason=f"filtered: {generation_type}"))
                    continue

            if not block.get("enabled", True):
                trace.append(self._trace(block, skipped=True, reason="disabled"))
                continue

            if block.get("_module_unavailable"):
                trace.append(self._trace(block, skipped=True, reason="module inactive or block not provided"))
                continue

            if is_history:
                history_block = block
                entry = self._trace(block)
                trace.append(entry)
                system_entries.append((None, entry))
                continue

            content = self._render_block(block, state)
            if not content.strip():
                trace.append(self._trace(block, skipped=True, reason="empty"))
                continue

            message = {"role": block["role_type"], "content": content}
            if block["placement"] == "system_relative":
                entry = self._trace(block)
                trace.append(entry)
                system_entries.append((message, entry))
            else:
                chat_injections.append((block, message))

        if history_block is not None:
            max_turns = history_block.get("config", {}).get("max_turns")
            history_messages = self._chat_history_messages(state, max_turns)
        elif not has_history_block:
            # Pipelines without an explicit chat_history block keep the
            # legacy behavior: the full transcript follows the system blocks.
            history_messages = self._chat_history_messages(state)
        else:
            # A chat_history block exists but is disabled or filtered out:
            # the transcript is deliberately omitted this generation.
            history_messages = []

        messages = []
        chat_start = None
        for message, entry in system_entries:
            if message is None:
                chat_start = len(messages)
                if history_messages:
                    entry["message_index"] = len(messages)
                    messages.extend(history_messages)
                else:
                    entry["skipped"] = True
                    entry["reason"] = "empty"
                continue
            entry["message_index"] = len(messages)
            messages.append(message)

        if not has_history_block:
            chat_start = len(messages)
            messages.extend(history_messages)
        if chat_start is None:
            chat_start = len(messages)

        messages.append(self._current_input_message(state))

        for block, message in chat_injections:
            insert_index = max(chat_start, len(messages) - block["depth"])
            messages.insert(insert_index, message)
            trace.append(self._trace(block, message_index=insert_index))

        return {
            "messages": messages,
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

        if block_type == "character_context":
            text = self._render_character_context(state, config)
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

    def _render_character_context(self, state: dict[str, Any], config: dict[str, Any]) -> str:
        """Render the player character's identity so the storyteller always knows
        who the player is. Reads the per-save character record; evolving fields
        (appearance/personality) reflect any changes recorded during play."""
        characters = state.get("characters") or {}
        player = characters.get("default_player") or {}
        if not isinstance(player, dict):
            return ""

        fields = [
            ("Name", (player.get("name") or "").strip()),
            ("Race", (player.get("race") or "").strip()),
            ("Gender", (player.get("gender") or "").strip()),
            ("Appearance", (player.get("full_appearance") or player.get("short_appearance") or "").strip()),
            ("Personality", (player.get("personality") or "").strip()),
        ]
        field_lines = [f"{label}: {value}" for label, value in fields if value]
        if not field_lines:
            return ""

        lines = [
            "<player_character>",
            "The player controls the following character. Refer to them consistently and never contradict these details:",
            *field_lines,
            "</player_character>",
        ]
        return "\n".join(lines)

    def _chat_history_messages(self, state: dict[str, Any], max_turns: int | None = None) -> list[dict[str, str]]:
        messages = []
        for message in state.get("chat_messages", []):
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            # Slash-command exchanges (/inventory, /plot, ...) are player-facing
            # chrome, not story; keep them out of the storyteller's prompt.
            meta = message.get("meta")
            if isinstance(meta, dict) and meta.get("command"):
                continue
            if role == "ai":
                role = "assistant"
            if role in {"user", "assistant", "system"}:
                messages.append({"role": role, "content": content})

        if max_turns is None:
            return messages
        if max_turns <= 0:
            return []
        # A turn starts at a player message and carries the replies that
        # follow it. A transcript with fewer turns than the cap (or no player
        # messages yet, e.g. only an opening narration) is kept whole.
        user_indices = [index for index, message in enumerate(messages) if message["role"] == "user"]
        if len(user_indices) > max_turns:
            messages = messages[user_indices[-max_turns]:]
        return messages

    def _current_input_message(self, state: dict[str, Any]) -> dict[str, str]:
        input_text = (state.get("input_text") or "").strip()
        if input_text:
            return {"role": "user", "content": input_text}
        # Empty input = a "continue" turn: no player message, just an
        # editable instruction to advance the story on its own.
        continue_prompt = (state.get("continue_prompt") or "").strip() or DEFAULT_CONTINUE_PROMPT
        return {"role": "user", "content": self.resolve_macros(continue_prompt, state)}

    def _engine_directive_block(self, block_id: str, display_name: str, text: str,
                                depth: int = 0) -> dict[str, Any]:
        """An engine-appended system directive injected into the chat at the
        given depth (0 = the very end, after the player's input)."""
        return {
            "id": block_id,
            "type": "static_text",
            "source": "engine",
            "enabled": True,
            "role_type": "system",
            "placement": "chat_injection",
            "depth": depth,
            "display_name": display_name,
            "category": "utility",
            "generation_types": None,
            "config": {
                "text": text,
            },
        }

    def _validation_veto_block(self, validation_veto: str) -> dict[str, Any]:
        return self._engine_directive_block("engine_validation_veto", "Validation Veto", validation_veto)

    def _story_style_block(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """The story's editable style direction (themes/tags/pacing) as a
        depth-0 chat injection. Returns None when every field is empty."""
        text = render_story_style(state.get("story_style"))
        if not text:
            return None
        return self._engine_directive_block("engine_story_style", "Story Style", text)

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
