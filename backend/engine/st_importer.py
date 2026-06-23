import re
import uuid
from typing import Any

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
COMMENT_PATTERN = re.compile(r"\{\{//.*?\}\}", re.DOTALL)
TRIM_PATTERN = re.compile(r"\{\{trim\}\}", re.IGNORECASE)

ST_MACROS = ["{{user}}", "{{char}}", "{{scenario}}", "{{personality}}", "{{group}}", "{0}"]

ST_MACRO_MAP = {
    "{{user}}": "${player_name}",
}


class SillyTavernImporter:

    def import_preset(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        prompts = raw_data.get("prompts", [])
        prompt_order = raw_data.get("prompt_order", [])

        enabled_map = {}
        for char_cfg in prompt_order:
            for item in char_cfg.get("order", []):
                enabled_map[item.get("identifier")] = item.get("enabled", True)

        blocks = []
        skipped = []
        separator_skip = 0

        for item in prompts:
            identifier = item.get("identifier", "")
            name = (item.get("name") or "").strip()
            marker = item.get("marker", False)

            if marker and identifier in SKIP_MARKERS:
                skipped.append({"id": identifier, "name": name, "reason": "marker placeholder"})
                continue

            if SECTION_PATTERN.match(name):
                skipped.append({"id": identifier, "name": name, "reason": "section separator"})
                separator_skip += 1
                continue

            content = (item.get("content") or "").strip()
            if content.startswith("{{//Keep toggled off}}") or content == "{{//Keep toggled off}}{{trim}}":
                skipped.append({"id": identifier, "name": name, "reason": "placeholder toggle"})
                continue

            cleaned_content = self._clean_text(content)
            if not cleaned_content.strip():
                skipped.append({"id": identifier, "name": name, "reason": "empty after cleaning"})
                continue

            block = {
                "id": self._make_block_id(identifier, name),
                "type": "static_text",
                "source": "user",
                "enabled": enabled_map.get(identifier, item.get("enabled", True)),
                "role_type": item.get("role", "system"),
                "placement": "system_relative" if item.get("injection_position", 0) == 0 else "chat_injection",
                "depth": item.get("injection_depth", 0) if item.get("injection_position", 0) != 0 else None,
                "display_name": name,
                "category": self._infer_category(identifier, name, item),
                "generation_types": None,
                "config": {"text": cleaned_content},
            }
            blocks.append(block)

        return {
            "blocks": blocks,
            "stats": {
                "total": len(prompts),
                "imported": len(blocks),
                "skipped": len(skipped) + separator_skip,
                "skipped_ids": skipped,
            },
        }

    def _clean_text(self, text: str) -> str:
        text = COMMENT_PATTERN.sub("", text)
        text = TRIM_PATTERN.sub("", text)
        for macro in ST_MACROS:
            text = text.replace(macro, ST_MACRO_MAP.get(macro, ""))
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

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
