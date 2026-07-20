"""World-prompt message builders and scenario grounding.

Pure functions (no I/O, no facade state): they render the player's seed
prompt and scenario record into the LLM message lists the routes feed to
the model. Kept unit-testable on purpose; the facade re-exports the
seed-prompt builder so its public surface is unchanged. (The ideation
conversation's builder lived here from C4 to C7b; the design conversation
is now the agent session's chat phase — ``agent/harness.py``.)
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
