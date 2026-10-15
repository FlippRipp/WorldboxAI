import random
import re
import uuid
from typing import Any

from backend.engine.st_macros import STMacroContext, clean_st_import_text

SKIP_MARKERS = {
    "worldInfoBefore", "worldInfoAfter", "personaDescription",
    "charDescription", "charPersonality", "scenario",
    "dialogueExamples", "chatHistory",
}

CATEGORY_KEYWORDS = {
    "system_prompt": ["main"],
    "post_history": ["jailbreak", "post.history", "post_history"],
    "narrator": ["prose", "style", "pov", "combat", "spectacle", "story mode", "cinematic"],
    "character": ["npc", "voice", "dialogue", "nsfw", "adult", "freaky", "realism mode", "emotion", "thought", "personality"],
    "utility": ["banned", "anti", "formatting", "length", "time", "place", "colored", "graphics", "onomatopoeia", "internal thought"],
    "world_context": [],
}

SECTION_PATTERN = re.compile(r"^=+.*=+$")
ST_GENERATION_TRIGGERS = {"normal", "continue", "swipe", "regenerate", "quiet", "impersonate"}
UNAVAILABLE_GENERATION_TRIGGERS = {"swipe", "quiet", "impersonate"}
LOCAL_VARIABLE_PATTERN = re.compile(r"\{\{\s*(?:setvar|getvar|addvar|incvar|decvar)(?=\s|:|\}\})", re.IGNORECASE)


class SillyTavernImporter:

    def import_preset(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        prompts, order_configs = self._validate_preset(raw_data)
        warnings = self._preset_warnings(raw_data)
        selected_order = self._select_order(order_configs, warnings)
        enabled_map = {}
        ordered_ids = []
        if selected_order is not None:
            for entry in selected_order:
                identifier = entry["identifier"]
                if identifier in enabled_map:
                    warnings.append(self._warning(
                        "duplicate_order_entry", f"Duplicate order entry {identifier!r} was ignored."))
                    continue
                ordered_ids.append(identifier)
                # Missing flags must not activate a dormant template.
                enabled_map[identifier] = entry.get("enabled", False)
        positions = {identifier: index for index, identifier in enumerate(ordered_ids)}
        ordered_prompts = sorted(
            enumerate(prompts),
            key=lambda pair: (positions.get(pair[1].get("identifier", ""), len(positions)), pair[0]),
        )
        blocks = []
        skipped = []
        block_warnings = []
        unordered_blocks = []
        uses_variables = False

        for _, item in ordered_prompts:
            identifier = item.get("identifier", "")
            name = (item.get("name") or "").strip()
            marker = item.get("marker", False)

            if marker and identifier in SKIP_MARKERS:
                skipped.append({"id": identifier, "name": name, "reason": "marker placeholder"})
                continue

            if SECTION_PATTERN.match(name):
                skipped.append({"id": identifier, "name": name, "reason": "section separator"})
                continue

            content = (item.get("content") or "").strip()
            cleaned_content, unsupported_macros = clean_st_import_text(content)
            cleaned_content = self._normalize_text(cleaned_content)
            if not cleaned_content.strip():
                skipped.append({"id": identifier, "name": name, "reason": "empty after cleaning"})
                if unsupported_macros:
                    warnings.append(self._warning(
                        "unsupported_macros_removed",
                        "Removed unsupported SillyTavern macros: " + ", ".join(unsupported_macros) + ".",
                        block_name=name or identifier,
                    ))
                continue

            is_injection = item.get("injection_position", 0) != 0
            block = {
                "id": self._make_block_id(identifier, name),
                "type": "static_text",
                "source": "user",
                "enabled": enabled_map.get(identifier, False) if selected_order is not None else item.get("enabled", True),
                "role_type": item.get("role", "system"),
                "placement": "chat_injection" if is_injection else "system_relative",
                "depth": item.get("injection_depth", 0) if is_injection else None,
                "order": self._parse_order(item.get("injection_order")) if is_injection else None,
                "display_name": name,
                "category": self._infer_category(identifier, name, item),
                "generation_types": None,
                "config": {"text": cleaned_content, "st_macros": True},
            }
            diagnostics = []
            triggers = item.get("injection_trigger") or []
            if triggers:
                normalized = list(dict.fromkeys(trigger.strip().lower() for trigger in triggers))
                recognized = [trigger for trigger in normalized if trigger in ST_GENERATION_TRIGGERS]
                unknown = [trigger for trigger in normalized if trigger not in ST_GENERATION_TRIGGERS]
                block["generation_types"] = recognized or None
                if unknown:
                    diagnostics.append(self._warning(
                        "unsupported_generation_triggers",
                        "Unsupported generation triggers were excluded: " + ", ".join(unknown) + "."
                        + (" The block was disabled because no supported trigger remains." if not recognized else ""),
                    ))
                if not recognized:
                    block["enabled"] = False
                    block["config"]["st_unsupported_triggers"] = unknown
                elif set(recognized) <= UNAVAILABLE_GENERATION_TRIGGERS:
                    diagnostics.append(self._warning(
                        "generation_action_unavailable",
                        "This block is restricted to " + ", ".join(recognized)
                        + "; Worldbox has no matching generation action yet, so it will not run during normal storytelling.",
                    ))
            if unsupported_macros:
                diagnostics.append(self._warning(
                    "unsupported_macros_removed",
                    "Removed unsupported SillyTavern macros: " + ", ".join(unsupported_macros) + ".",
                ))
            if re.search(r"IMG_PROMPT\s*:", cleaned_content, re.IGNORECASE):
                diagnostics.append(self._warning(
                    "image_prompt_unsupported",
                    "IMG_PROMPT tags do not trigger Worldbox image generation; disable or adapt this block.",
                ))
            if re.search(r"\bstyle\s*=", cleaned_content, re.IGNORECASE):
                diagnostics.append(self._warning(
                    "inline_styles_unsupported",
                    "Arbitrary inline HTML styles are removed by the story renderer; this block's custom layout will not be reproduced.",
                ))
            uses_variables = uses_variables or bool(LOCAL_VARIABLE_PATTERN.search(cleaned_content))
            blocks.append(block)
            block_warnings.append((block, diagnostics))
            if selected_order is not None and identifier not in enabled_map:
                unordered_blocks.append(block)

        # Different ST identifiers can produce the same display-name slug or
        # sanitized id. Preserve every block and reserve natural ids before
        # allocating suffixes so a collision cannot rename another block.
        reserved_ids = {block["id"] for block in blocks}
        used_ids = set()
        next_suffix = {}
        for block in blocks:
            base_id = block["id"]
            if base_id in used_ids:
                suffix = next_suffix.get(base_id, 2)
                candidate = f"{base_id}_{suffix}"
                while candidate in reserved_ids or candidate in used_ids:
                    suffix += 1
                    candidate = f"{base_id}_{suffix}"
                block["id"] = candidate
                next_suffix[base_id] = suffix + 1
            used_ids.add(block["id"])

        for block, diagnostics in block_warnings:
            for diagnostic in diagnostics:
                diagnostic.update(block_id=block["id"], block_name=block["display_name"] or block["id"])
                warnings.append(diagnostic)
        warnings.extend(self._validate_macro_dependencies(blocks))
        if uses_variables:
            warnings.append(self._warning(
                "variables_not_persistent",
                "SillyTavern variables are evaluated anew for each generation and are not saved as persistent Worldbox game state. Dice stay consistent for previews, retries and regeneration of the same story turn; the next turn gets new rolls.",
            ))
        if unordered_blocks:
            warnings.append(self._warning(
                "unordered_prompts_disabled",
                "Prompts absent from the selected SillyTavern order were retained disabled: "
                + ", ".join(block["display_name"] or block["id"] for block in unordered_blocks) + ".",
            ))
        if any(entry["reason"] == "marker placeholder" for entry in skipped):
            warnings.append(self._warning(
                "character_assets_not_imported",
                "Character cards, personas, scenarios, dialogue examples, lorebooks and chat history placeholders do not import their SillyTavern data or recreate their positions. Keep Worldbox context/history blocks, position them as needed, and import lorebooks separately.",
            ))

        return {
            "blocks": blocks,
            "warnings": warnings,
            "stats": {
                "total": len(prompts),
                "imported": len(blocks),
                "skipped": len(skipped),
                "skipped_ids": skipped,
            },
        }

    def _validate_macro_dependencies(self, blocks: list[dict]) -> list[dict]:
        """Inspect active dependencies without baking evaluated values into text.

        Each action has an independent throwaway context, just as each real
        prompt compilation does. A private deterministic random source keeps
        this diagnostic pass from consuming story or process-global dice.
        """
        findings: dict[tuple[str, str], list[str]] = {}
        for action in ("normal", "continue", "regenerate"):
            context = STMacroContext(rng=random.Random(0))
            for block in blocks:
                if not block["enabled"]:
                    continue
                triggers = block["generation_types"]
                if triggers and action not in triggers:
                    continue
                previous_warnings = len(context.warnings)
                context.render(block["config"]["text"])
                for issue in context.warnings[previous_warnings:]:
                    actions = findings.setdefault((block["id"], issue), [])
                    if action not in actions:
                        actions.append(action)

        names = {block["id"]: block["display_name"] or block["id"] for block in blocks}
        warnings = []
        for (block_id, issue), actions in findings.items():
            explanation = ""
            if "is unset in this prompt build" in issue:
                explanation = (
                    " This may be an optional value, a dependency in a disabled block,"
                    " or unsupported persistent SillyTavern data; it does not disable the block."
                )
            warnings.append(self._warning(
                "macro_validation",
                issue + " Checked for: " + ", ".join(actions) + "." + explanation,
                block_id=block_id,
                block_name=names[block_id],
            ))
        return warnings

    def _validate_preset(self, raw_data: Any) -> tuple[list, list]:
        if not isinstance(raw_data, dict):
            raise ValueError("SillyTavern preset must be a JSON object.")
        prompts = raw_data.get("prompts")
        if not isinstance(prompts, list):
            raise ValueError("SillyTavern preset must contain a prompts list.")
        for index, item in enumerate(prompts):
            if not isinstance(item, dict):
                raise ValueError(f"Prompt at index {index} must be an object.")
            for field in ("identifier", "name", "content"):
                if field in item and item[field] is not None and not isinstance(item[field], str):
                    raise ValueError(f"Prompt at index {index}: {field} must be a string.")
            if item.get("identifier") is None and "identifier" in item:
                raise ValueError(f"Prompt at index {index}: identifier must be a string.")
            for field in ("enabled", "marker", "system_prompt"):
                if field in item and not isinstance(item[field], bool):
                    raise ValueError(f"Prompt at index {index}: {field} must be true or false.")
            role = item.get("role", "system")
            if not isinstance(role, str) or role not in {"system", "user", "assistant"}:
                raise ValueError(f"Prompt at index {index}: unsupported role.")
            position = item.get("injection_position", 0)
            if isinstance(position, bool) or not isinstance(position, int) or position not in {0, 1}:
                raise ValueError(f"Prompt at index {index}: injection_position must be 0 or 1.")
            if position == 1:
                depth = item.get("injection_depth", 0)
                if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
                    raise ValueError(f"Prompt at index {index}: injection_depth must be a non-negative integer.")
            triggers = item.get("injection_trigger")
            if triggers is not None and (not isinstance(triggers, list) or any(not isinstance(t, str) for t in triggers)):
                raise ValueError(f"Prompt at index {index}: injection_trigger must be a list of strings.")
        order_configs = raw_data.get("prompt_order", [])
        if not isinstance(order_configs, list):
            raise ValueError("prompt_order must be a list.")
        for index, config in enumerate(order_configs):
            if not isinstance(config, dict):
                raise ValueError(f"prompt_order configuration at index {index} must be an object.")
            order = config.get("order")
            if order is None:
                continue
            if not isinstance(order, list):
                raise ValueError(f"prompt_order configuration at index {index}: order must be a list.")
            for entry in order:
                if not isinstance(entry, dict) or not isinstance(entry.get("identifier"), str):
                    raise ValueError("Each prompt_order entry must be an object with a string identifier.")
                if "enabled" in entry and not isinstance(entry["enabled"], bool):
                    raise ValueError("Each prompt_order enabled flag must be true or false.")
        extensions = raw_data.get("extensions")
        if extensions is not None and not isinstance(extensions, dict):
            raise ValueError("Preset extensions must be an object.")
        return prompts, order_configs

    def _select_order(self, configs: list[dict], warnings: list[dict]) -> list[dict] | None:
        valid = [config for config in configs if isinstance(config.get("order"), list)]
        if configs and not valid:
            raise ValueError("prompt_order contains no valid order list.")
        if not valid:
            return None
        selected = next((config for config in reversed(valid) if str(config.get("character_id")) == "100001"), valid[-1])
        if len(configs) > 1:
            warnings.append(self._warning(
                "multiple_order_configurations",
                "Multiple SillyTavern order configurations were found; used "
                + ("character_id 100001." if str(selected.get("character_id")) == "100001" else "the last valid configuration."),
            ))
        return selected["order"]

    def _preset_warnings(self, raw_data: dict) -> list[dict]:
        warnings = []
        ignored_settings = sorted(set(raw_data) - {"prompts", "prompt_order", "extensions"})
        if ignored_settings:
            warnings.append(self._warning(
                "preset_settings_ignored",
                "Worldbox keeps its own model, sampling, reasoning, tools and continuation settings. The preset's settings for these features were not applied.",
                details="Ignored preset settings: " + ", ".join(ignored_settings) + ".",
            ))
        extensions = raw_data.get("extensions") or {}
        if extensions:
            warnings.append(self._warning(
                "extensions_ignored",
                "SillyTavern extensions and regex scripts are not imported or executed. Native dialogue colors and collapsible details work, but extension-specific replacements, image actions and context cleanup do not.",
            ))
        return warnings

    @staticmethod
    def _warning(code: str, message: str, **context: str) -> dict[str, str]:
        return {"code": code, "message": message, **context}

    def _parse_order(self, raw: Any) -> int:
        """ST injection_order: same-depth tiebreak, default 100."""
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 100
        try:
            return int(raw)
        except (OverflowError, ValueError):
            return 100

    def _clean_text(self, text: str) -> str:
        cleaned, _ = clean_st_import_text(text)
        return self._normalize_text(cleaned)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", text.replace("{0}", "")).strip()

    def _make_block_id(self, identifier: str, name: str) -> str:
        if identifier and identifier not in SKIP_MARKERS and not len(identifier) > 30:
            raw = re.sub(r"[^a-z0-9_]", "_", identifier.lower())
            if raw and raw[0].isdigit():
                raw = "st_" + raw
            if raw:
                return raw
        slug = re.sub(r"[^a-z0-9_]", "_", name.lower()) if name else ""
        if slug:
            slug = re.sub(r"_+", "_", slug).strip("_")
        if not slug:
            slug = uuid.uuid4().hex[:6]
        return f"st_{slug}"

    def _infer_category(self, identifier: str, name: str, item: dict) -> str | None:
        search = f"{identifier} {name}".lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in search:
                    return category
        if item.get("system_prompt") and not item.get("marker") and identifier != "nsfw":
            return "system_prompt"
        return "other"
