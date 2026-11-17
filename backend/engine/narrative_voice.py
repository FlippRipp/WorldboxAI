"""Shared narrative perspective & tense resolution and prompt directives.

Perspective options:
- ``second_person``: Direct address ("you" / "your")
- ``first_person``: Subjective protagonist voice ("I" / "my")
- ``third_person``: Third-person limited (character name + "he/she/they")

Tense options:
- ``present``: Present tense ("walk" / "walks")
- ``past``: Past tense ("walked")

Resolution order (most specific wins):
1. Explicit ``style_override`` (e.g. scenario editor context or create-save payload)
2. Per-save ``state["story_style"]`` (``perspective`` / ``tense`` when non-empty)
3. Pre-resolved ``state["narrative_voice"]`` (injected into filtered module state)
4. Global ``SettingsRegistry`` (``narrative.perspective`` / ``narrative.tense``)
5. Engine defaults (``second_person``, ``present``)
"""

from typing import Any

PERSPECTIVE_OPTIONS = ("second_person", "first_person", "third_person")
TENSE_OPTIONS = ("present", "past")

DEFAULT_PERSPECTIVE = "second_person"
DEFAULT_TENSE = "present"

PERSPECTIVE_LABELS = {
    "second_person": 'Second person ("you")',
    "first_person": 'First person ("I")',
    "third_person": 'Third person ("he/she/they")',
}

TENSE_LABELS = {
    "present": "Present tense",
    "past": "Past tense",
}

PERSPECTIVE_SELECT_OPTIONS = [
    {"value": key, "label": PERSPECTIVE_LABELS[key]} for key in PERSPECTIVE_OPTIONS
]

TENSE_SELECT_OPTIONS = [
    {"value": key, "label": TENSE_LABELS[key]} for key in TENSE_OPTIONS
]

_PERSPECTIVE_ALIASES = {
    "second_person": "second_person",
    "second": "second_person",
    "2nd": "second_person",
    "2nd_person": "second_person",
    "first_person": "first_person",
    "first": "first_person",
    "1st": "first_person",
    "1st_person": "first_person",
    "third_person": "third_person",
    "third": "third_person",
    "3rd": "third_person",
    "3rd_person": "third_person",
}

_TENSE_ALIASES = {
    "present": "present",
    "present_tense": "present",
    "past": "past",
    "past_tense": "past",
}


def normalize_perspective(value: Any, *, allow_empty: bool = False) -> str:
    if isinstance(value, str):
        cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not cleaned and allow_empty:
            return ""
        if cleaned in _PERSPECTIVE_ALIASES:
            return _PERSPECTIVE_ALIASES[cleaned]
    return "" if allow_empty else DEFAULT_PERSPECTIVE


def normalize_tense(value: Any, *, allow_empty: bool = False) -> str:
    if isinstance(value, str):
        cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not cleaned and allow_empty:
            return ""
        if cleaned in _TENSE_ALIASES:
            return _TENSE_ALIASES[cleaned]
    return "" if allow_empty else DEFAULT_TENSE


def extract_player_name(state: dict[str, Any] | None, fallback: str = "") -> str:
    if not isinstance(state, dict):
        return fallback
    characters = state.get("characters")
    if isinstance(characters, dict):
        player = characters.get("default_player")
        if isinstance(player, dict):
            name = (player.get("name") or "").strip()
            if name:
                return name
    nv = state.get("narrative_voice")
    if isinstance(nv, dict):
        name = (nv.get("player_name") or "").strip()
        if name:
            return name
    return fallback


def resolve_narrative_voice(
    state: dict[str, Any] | None = None,
    settings: Any = None,
    style_override: dict[str, Any] | None = None,
    player_name: str = "",
) -> dict[str, str]:
    """Resolve effective ``{perspective, tense, player_name}``."""
    perspective = ""
    tense = ""

    for candidate in (
        style_override,
        state.get("story_style") if isinstance(state, dict) else None,
        state.get("narrative_voice") if isinstance(state, dict) else None,
    ):
        if isinstance(candidate, dict):
            if not perspective:
                perspective = normalize_perspective(candidate.get("perspective"), allow_empty=True)
            if not tense:
                tense = normalize_tense(candidate.get("tense"), allow_empty=True)

    if settings is not None and hasattr(settings, "get"):
        if not perspective:
            try:
                perspective = normalize_perspective(settings.get("narrative.perspective"), allow_empty=True)
            except Exception:
                pass
        if not tense:
            try:
                tense = normalize_tense(settings.get("narrative.tense"), allow_empty=True)
            except Exception:
                pass

    resolved_name = (player_name or "").strip() or extract_player_name(state)
    return {
        "perspective": perspective or DEFAULT_PERSPECTIVE,
        "tense": tense or DEFAULT_TENSE,
        "player_name": resolved_name,
    }


def perspective_short_label(perspective: str) -> str:
    p = normalize_perspective(perspective)
    if p == "first_person":
        return "first person"
    if p == "third_person":
        return "third person"
    return "second person"


def tense_short_label(tense: str) -> str:
    t = normalize_tense(tense)
    return "past tense" if t == "past" else "present tense"


def _protagonist_ref(player_name: str = "", subject_label: str = "the player character") -> str:
    clean = (player_name or "").strip()
    if clean and clean.lower() not in {"the protagonist", "the player character"}:
        return f"{clean} ({subject_label})"
    return subject_label


def narration_voice_lines(perspective: str, tense: str, player_name: str = "") -> list[str]:
    """Instruction lines for prompts that author in-story narrative prose."""
    p = normalize_perspective(perspective)
    t = normalize_tense(tense)
    who = _protagonist_ref(player_name)
    name_or_char = (player_name or "").strip() or "the protagonist"

    if p == "first_person":
        if t == "past":
            pov_rule = (
                f'Perspective & Tense: Write strictly in first person, past tense — narrate from '
                f'{who}\'s own viewpoint using "I"/"me"/"my" and past-tense verbs '
                f'(e.g. "I stepped into the hall and steadied my breath.").'
            )
        else:
            pov_rule = (
                f'Perspective & Tense: Write strictly in first person, present tense — narrate from '
                f'{who}\'s own viewpoint using "I"/"me"/"my" and present-tense verbs '
                f'(e.g. "I step into the hall and steady my breath.").'
            )
    elif p == "third_person":
        if t == "past":
            pov_rule = (
                f"Perspective & Tense: Write strictly in third person limited, past tense — refer to "
                f'{who} by name ("{name_or_char}") and third-person pronouns (he/she/they), never '
                f'"you" or "I" outside spoken dialogue, using past-tense verbs '
                f'(e.g. "{name_or_char} stepped into the hall and steadied their breath.").'
            )
        else:
            pov_rule = (
                f"Perspective & Tense: Write strictly in third person limited, present tense — refer to "
                f'{who} by name ("{name_or_char}") and third-person pronouns (he/she/they), never '
                f'"you" or "I" outside spoken dialogue, using present-tense verbs '
                f'(e.g. "{name_or_char} steps into the hall and steadies their breath.").'
            )
    else:
        if t == "past":
            pov_rule = (
                f'Perspective & Tense: Write strictly in second person, past tense — address '
                f'{who} directly as "you"/"your" and use past-tense verbs '
                f'(e.g. "You stepped into the hall and steadied your breath.").'
            )
        else:
            pov_rule = (
                f'Perspective & Tense: Write strictly in second person, present tense — address '
                f'{who} directly as "you"/"your" and use present-tense verbs '
                f'(e.g. "You step into the hall and steady your breath.").'
            )

    pronoun_rule = (
        f'Player input pronouns: Whether the player writes in first person ("I"/"my"), '
        f'second person ("you"/"your" — e.g. "you remember your memories"), or third person, '
        f"those pronouns in player-submitted actions, directions, or requests refer to {who} — "
        f"never to you, the AI."
    )
    return [pov_rule, pronoun_rule]


def narration_voice_directive(perspective: str, tense: str, player_name: str = "") -> str:
    """Block of narration perspective, tense, and player-pronoun rules."""
    return "\n".join(narration_voice_lines(perspective, tense, player_name))


def input_pronoun_grounding(
    perspective: str = DEFAULT_PERSPECTIVE,
    tense: str = DEFAULT_TENSE,
    player_name: str = "",
    *,
    subject_label: str = "the player character",
    include_story_voice: bool = True,
) -> str:
    """Compact grounding note for analytical, builder, and editorial prompts.

    Ensures the LLM knows both how the story's narration refers to the protagonist
    AND that ``"you"``/``"your"`` or ``"I"``/``"my"`` in player-written inputs,
    concepts, descriptions, or modification requests (e.g. ``"you remember your
    memories"``) refer to the protagonist/character, never to the AI.
    """
    p = normalize_perspective(perspective)
    t = normalize_tense(tense)
    who = _protagonist_ref(player_name, subject_label=subject_label)
    parts = []
    if include_story_voice:
        if p == "first_person":
            narration_ref = f'"I"/"my" in narration is {who}'
        elif p == "third_person":
            narration_ref = f"narration refers to {who} in third person"
        else:
            narration_ref = f'"you"/"your" in narration is {who}'
        parts.append(
            f"Narrative voice context: The story uses {perspective_short_label(p)}, "
            f"{tense_short_label(t)} ({narration_ref})."
        )
    parts.append(
        f'Pronoun grounding: In any player-written text (actions, concepts, descriptions, '
        f'directions, or modification requests — e.g. "you remember your memories"), '
        f'"you"/"your" and "I"/"my" refer to {who}, NEVER to you (the AI).'
    )
    return " ".join(parts)


def state_pronoun_grounding(
    state: dict[str, Any] | None,
    settings: Any = None,
    *,
    subject_label: str = "the player character",
) -> str:
    """Convenience wrapper resolving voice + player name directly from state."""
    voice = resolve_narrative_voice(state, settings=settings)
    return input_pronoun_grounding(
        voice["perspective"],
        voice["tense"],
        voice["player_name"],
        subject_label=subject_label,
    )
