"""World-prompt message builders and scenario grounding.

Pure functions (no I/O, no facade state): they render the player's seed
prompt, scenario record and interview history into the LLM message lists the
routes feed to the model. Kept unit-testable on purpose; the facade re-exports
them so its public surface is unchanged.
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


def _interview_history_text(history: list[dict] | None) -> str:
    """Render prior interview rounds (question/answer pairs) as plain text for
    the LLM. Skipped questions are shown as such — the player saw them and
    chose not to answer. Never truncated."""
    lines = []
    for pair in history or []:
        question = str(pair.get("question") or "").strip()
        if not question:
            continue
        answer = str(pair.get("answer") or "").strip()
        lines.append(f"Q: {question}\nA: {answer or '(skipped — the player left this open)'}")
    return "\n\n".join(lines)


def build_world_questions_messages(current_text: str = "",
                                   history: list[dict] | None = None,
                                   scenario: dict | None = None) -> list[dict]:
    """LLM messages for the world-prompt interview: ask the player a short
    round of clarifying questions about details the seed prompt leaves open.

    Works from an empty prompt too — the first round then asks foundational
    questions (genre, tone, scale, central conflict). Prior rounds are passed
    in `history` so the model never repeats itself, and a linked scenario is
    grounding so it never asks what the scenario already answers. Pure (no
    I/O) so it is unit-testable; the route feeds the result to the LLM.
    """
    system = (
        "You are a world-building assistant interviewing the player about the world "
        "they want an AI world generator to create. Read their seed prompt draft and "
        "ask 3-5 short, concrete questions about important details it leaves open — "
        "the things that would most change the generated world (tone, scale, conflict, "
        "magic or technology, factions, geography, cultures, history, what makes it "
        "distinct). Ask ONLY about the world itself — the setting the generator will "
        "build. Never ask about protagonists, individual characters, their goals or "
        "relationships, or how the story's plot unfolds: those belong to the scenario "
        "and the story, not to world generation. Each question must be answerable in "
        "a sentence or two. Never ask anything the prompt, the scenario, or a "
        "previous answer already settles, and never repeat a question from a previous "
        "round — a skipped question means the player wants to leave it open, so move "
        "on to something else. "
        'Return only valid JSON: {"questions": ["...", "..."]}.'
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — treat "
            "everything in it as already decided, not something to ask about. Its "
            "characters and events are story material, not open questions: ask "
            "about the wider world the scenario takes place in, never about the "
            "scenario's people or plot:\n"
            f"<scenario>\n{grounding}\n</scenario>")
    current_text = (current_text or "").strip()
    if current_text:
        parts.append(f"<current_world_prompt>\n{current_text}\n</current_world_prompt>")
    else:
        parts.append(
            "<current_world_prompt>\n(empty — the player hasn't written anything yet; "
            "ask foundational questions that help them shape the world from scratch)\n"
            "</current_world_prompt>")
    history_text = _interview_history_text(history)
    if history_text:
        parts.append(
            "Questions already asked in previous rounds — do not repeat or rephrase "
            f"any of these:\n<previous_rounds>\n{history_text}\n</previous_rounds>")
    parts.append("Ask the next round of questions. Return only the JSON.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_world_prompt_fold_messages(current_text: str,
                                     answers: list[dict],
                                     scenario: dict | None = None) -> list[dict]:
    """LLM messages for folding a round of interview answers into the seed
    prompt.

    Every answer must land in the prompt — added where it brings something
    new, rewriting whatever it changes — while parts the answers don't touch
    keep the player's wording. With an empty current prompt the answers become
    the first draft. Pure (no I/O) so it is unit-testable.
    """
    system = (
        "You are a world-building assistant maintaining the SEED PROMPT for an AI "
        "world generator — a short, vivid paragraph of creative direction the "
        "generator expands into a full world. The player has answered interview "
        "questions about their world; fold their answers into the prompt. Every "
        "answer must end up reflected in the prompt: add what it introduces, and "
        "rewrite whatever parts of the prompt it changes or contradicts — "
        "preserving the current text is never a reason to leave an answer out. "
        "Where the answers don't touch the prompt, keep the player's wording and "
        "details as they are, and do not pad or embellish beyond what the answers "
        "say. If the current prompt is empty, write a first draft from the answers "
        "alone. "
        'Return only valid JSON: {"text": "..."}.'
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — keep the "
            f"prompt consistent with it:\n<scenario>\n{grounding}\n</scenario>")
    current_text = (current_text or "").strip()
    if current_text:
        parts.append(f"<current_world_prompt>\n{current_text}\n</current_world_prompt>")
    else:
        parts.append("<current_world_prompt>\n(empty — write the first draft from the answers)\n</current_world_prompt>")
    answers_text = _interview_history_text(answers)
    parts.append(f"The player's answers this round:\n<answers>\n{answers_text}\n</answers>")
    parts.append(
        "Update the seed prompt so every answer is fully incorporated — add and "
        "change whatever the answers require, and keep the rest as the player "
        "wrote it. Return only the seed prompt text.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def seed_with_scenario(world_state: dict, user_prompt: str) -> str:
    """The effective seed text for generation: the user's prompt, plus the
    optional scenario document supplied at world creation.

    The scenario is longer-form source material (a campaign setting, an
    adventure premise, pasted background text) the world must be grounded in;
    the seed prompt is the creative direction on top of it. Composed here —
    the single seam every step generation passes through — so the LLM, mock
    and custom-step paths all see both. Never truncated.
    """
    scenario = str((world_state or {}).get("scenario") or "").strip()
    if not scenario:
        return user_prompt
    return (
        f"{user_prompt}\n\n"
        "The world's creator also provided a scenario — source material this world is set in. "
        "Ground the world in it: keep its facts, names, tone and situation consistent, and treat "
        "the seed prompt above as direction for what to build from it.\n"
        "--- SCENARIO ---\n"
        f"{scenario}\n"
        "--- END SCENARIO ---"
    )
