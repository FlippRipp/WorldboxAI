"""World-prompt message builders and scenario grounding.

Pure functions (no I/O, no facade state): they render the player's seed
prompt, scenario record and ideation conversation into the LLM message lists
the routes feed to the model. Kept unit-testable on purpose; the facade
re-exports the seed-prompt builder so its public surface is unchanged.
"""

def scenario_grounding_text(scenario: dict) -> str:
    """Render a linked scenario record (backend.engine.scenario) as the
    grounding text world generation is seeded with.

    The scenario's situation and opening scene are treated as established
    facts: the generated world must contain the places, people and stakes
    they reference, because the story will open there. Never truncated.
    """
    parts = []
    name = str(scenario.get("name") or "").strip()
    if name:
        parts.append(f"Scenario: {name}")
    desc = str(scenario.get("scenario_description") or "").strip()
    if desc:
        parts.append(f"Setting and situation:\n{desc}")
    opening = str(scenario.get("starting_prompt") or "").strip()
    if opening:
        parts.append(
            "The story will open with this exact scene — the world must contain "
            f"the places, people and situation it references:\n{opening}")
    for key, label in (("themes", "Themes"), ("tags", "Tags"), ("pacing", "Pacing")):
        val = str(scenario.get(key) or "").strip()
        if val:
            parts.append(f"{label}: {val}")
    return "\n\n".join(parts)


def scenario_start_brief(scenario: dict) -> str:
    """Render a scenario record as the start-location request used when a
    story combines a world with a scenario: the start location should be
    wherever the scenario's opening scene takes place.

    The player's pending modification request comes first and is marked
    highest-priority — it may move the opening somewhere the scenario text
    doesn't. Never truncated.
    """
    parts = []
    request = str(scenario.get("pending_modification_request") or "").strip()
    if request:
        parts.append(
            "The player's change request for this scenario — HIGHEST priority, "
            f"it overrides the scenario text below where they conflict:\n{request}")
    grounding = scenario_grounding_text(scenario)
    if grounding:
        parts.append(
            "The story starts with this scenario — choose the location where "
            f"its opening scene takes place (or the closest fit):\n{grounding}")
    return "\n\n".join(parts)


def build_world_prompt_messages(instruction: str, current_text: str = "",
                                scenario: dict | None = None) -> list[dict]:
    """LLM messages for writing a world SEED PROMPT from the player's notes.

    The player types free-form direction (the enrich field) and optionally has
    a draft prompt and/or a linked scenario; the model turns them into a
    concise seed prompt — the creative direction the generator expands into
    rules, lore and a map, NOT the world itself and NOT in-fiction narration.
    Pure (no I/O) so it is unit-testable; the route feeds the result to the
    LLM. Mirrors the scenario editor's prompt-rewrite framing.
    """
    system = (
        "You are a world-building assistant that writes the SEED PROMPT for an AI "
        "world generator. A seed prompt is a short, vivid paragraph of creative "
        "direction — premise, setting, tone, and any defining features — that the "
        "generator expands into a full world (rules, lore, regions, a map). It is "
        "NOT the world itself and NOT in-fiction narration: write it as direction "
        "for the generator, in plain descriptive prose, a few sentences long. "
        'Return only valid JSON: {"text": "..."}.'
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — honor its "
            "setting, situation, names and tone:\n"
            f"<scenario>\n{grounding}\n</scenario>")
    current_text = (current_text or "").strip()
    if current_text:
        parts.append(f"<current_world_prompt>\n{current_text}\n</current_world_prompt>")
    else:
        parts.append("<current_world_prompt>\n(empty — write a new seed prompt from scratch)\n</current_world_prompt>")
    instr = (instruction or "").strip()
    parts.append(
        "<direction>\n"
        + (instr or "Write a fitting world seed prompt from the scenario above.")
        + "\n</direction>")
    parts.append(
        "Write or revise the world seed prompt to follow the direction, building on "
        "the current prompt when present and grounding everything in the scenario "
        "when one is given. Return only the seed prompt text.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _conversation_transcript(history: list[dict] | None) -> str:
    """Render the ideation conversation for the LLM, oldest first. Never
    truncated."""
    lines = []
    for msg in history or []:
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        who = "You" if msg.get("role") == "assistant" else "Player"
        lines.append(f"{who}: {text}")
    return "\n\n".join(lines)


def build_ideation_turn_messages(history: list[dict],
                                 prompt_draft: str = "",
                                 rules_draft: list | None = None,
                                 scenario: dict | None = None,
                                 notes_draft: list | None = None) -> list[dict]:
    """LLM messages for one turn of the ideation conversation (C4): the
    chat-shaped front door of agent-mode world building.

    The model is a design partner converging with the player on what the
    world IS. Every turn it replies conversationally AND returns the three
    shared drafts in full — the seed prompt, the world rules (rules first:
    they double as the build's evaluation rubric, D3/D4), and the design
    notes (C5: established facts that are neither seed direction nor rules,
    each world-scoped or scoped to one named subject) — plus a ``ready``
    flag: its judgment that the idea feels settled and the build can start
    (the go *offer*; the player's go-ahead stays the approval moment, and
    the UI never gates on this flag). The drafts round-trip through the
    client every turn, so the player's hand edits are simply part of the
    current truth. Pure (no I/O) so it is unit-testable; the route feeds
    the result to the LLM.
    """
    from wbworldgen.worldgen.steps.world_rules import RULES_DOCTRINE

    system = (
        "You are the world-design partner in a game's world builder. The player "
        "and you are converging, in conversation, on what a world IS before an "
        "autonomous agent builds the whole thing unattended. Be a sharp, concrete "
        "collaborator: build on what the player gives, propose ideas they can "
        "react to, and ask at most a couple of pointed questions per turn — never "
        "a checklist interrogation. A player with a strong vision needs "
        "distillation, not invention; a player with a vague itch needs vivid "
        "options to pick between. Talk about the world only — protagonists and "
        "plot belong to the stories told in it later.\n\n"
        "You maintain three shared draft artifacts, returned in full every turn:\n"
        '- "prompt": the world seed prompt — a short, vivid paragraph of creative '
        "direction (premise, setting, tone, defining features) the generator "
        "expands into a full world. It is direction for a generator, never "
        "in-fiction narration.\n"
        '- "rules": the world rules — the handful of statements (aim for 3-7) '
        "that define how this world works. They are the spine of the design and "
        "double as the rubric the finished world is judged against, so converge "
        "them FIRST.\n"
        '- "notes": the design notebook — everything the conversation settles '
        "that is neither seed direction nor a rule: lore, cultures, biology, "
        "specific places and their quirks. Each note is "
        '{"text": "...", "subject": "..."}. Leave "subject" empty for a fact '
        "about the world as a whole; name ONE specific thing (\"the sand "
        "planet Kharos\") when the note belongs to it — scoped notes steer "
        "only their own place during the build, so one place's details never "
        "bleed into the others. The builder is verified against every note, "
        "so record what the player settles AS the conversation settles it — "
        "details the short prompt cannot hold survive the handoff only as "
        "notes. Never invent notes the player did not agree to; an empty "
        "list is fine early on.\n\n"
        f"What makes a good world rule:\n{RULES_DOCTRINE}\n\n"
        "The player sees the drafts beside the chat and can edit them by hand; "
        "the versions you receive are the current truth — never revert their "
        "edits, only evolve the drafts with the conversation.\n\n"
        "When the idea feels settled — the prompt captures it and the rules are "
        "concrete enough to judge a world by — set \"ready\" to true and offer "
        "in your reply to start the build. If the player asks to just build it, "
        "distill the best prompt, rules and notes you can from what you have "
        "and set \"ready\" to true immediately. Otherwise keep \"ready\" "
        "false.\n\n"
        "Return only valid JSON:\n"
        '{"reply": "...", "prompt": "...", "rules": ["...", "..."], '
        '"notes": [{"text": "...", "subject": ""}, ...], "ready": false}\n'
        '"reply" is your message to the player (plain conversational text). '
        '"prompt", "rules" and "notes" are the complete updated drafts — full '
        "replacements, not diffs."
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — treat "
            "everything in it as already decided, and design the wider world "
            "around it rather than re-asking what it settles:\n"
            f"<scenario>\n{grounding}\n</scenario>")
    prompt_draft = (prompt_draft or "").strip()
    parts.append(
        "<current_prompt>\n"
        + (prompt_draft or "(empty — no seed prompt yet)")
        + "\n</current_prompt>")
    rules = [str(r).strip() for r in (rules_draft or []) if str(r).strip()]
    parts.append(
        "<current_rules>\n"
        + ("\n".join(f"- {r}" for r in rules) if rules else "(none agreed yet)")
        + "\n</current_rules>")
    from wbworldgen.worldgen.notes import clean_notes
    notes = clean_notes(notes_draft)
    parts.append(
        "<current_notes>\n"
        + ("\n".join(
            f"- [{n['subject']}] {n['text']}" if n.get("subject") else f"- {n['text']}"
            for n in notes) if notes else "(none recorded yet)")
        + "\n</current_notes>")
    parts.append(f"<conversation>\n{_conversation_transcript(history)}\n</conversation>")
    parts.append(
        "Continue the conversation: answer the player's latest message and "
        "return the updated drafts. Return only the JSON.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def seed_with_scenario(world_state: dict, user_prompt: str) -> str:
    """The effective seed text for generation: the user's prompt, plus the
    ideation brief's notes (C5/N3 — the world-scoped facts in full and a
    one-line index of the subject-scoped ones, whose full texts inject only
    in their own scope), plus the optional scenario document supplied at
    world creation.

    The scenario is longer-form source material (a campaign setting, an
    adventure premise, pasted background text) the world must be grounded in;
    the seed prompt is the creative direction on top of it. Composed here —
    the single seam every step generation passes through — so the LLM, mock
    and custom-step paths all see all of it. Never truncated.
    """
    from wbworldgen.worldgen.notes import seed_notes_block

    parts = [user_prompt]
    notes_block = seed_notes_block(world_state or {})
    if notes_block:
        parts.append(
            "The world's creator agreed on these design notes during "
            "ideation — they are established facts, not suggestions:\n"
            + notes_block)
    scenario = str((world_state or {}).get("scenario") or "").strip()
    if scenario:
        parts.append(
            "The world's creator also provided a scenario — source material this world is set in. "
            "Ground the world in it: keep its facts, names, tone and situation consistent, and treat "
            "the seed prompt above as direction for what to build from it.\n"
            "--- SCENARIO ---\n"
            f"{scenario}\n"
            "--- END SCENARIO ---")
    return "\n\n".join(parts)
