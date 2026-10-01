"""Finite RPG/NPC judgments; existing module functions own every state mutation."""
import asyncio
import json
import re

from backend.engine.jev import ask, choice, noul, Decision, Uncertain

RPG = "wb_core_rpg"
NPC = "wb_npc_system"


def _options(records, describe):
    mapping = {f"option_{i}": record for i, record in enumerate(records)}
    options = {key: describe(record) for key, record in mapping.items()}
    options["none"] = "None of these applies."
    return mapping, options


def _facts(value, path="context"):
    """Stable source excerpts, never generated evidence or shortened strings."""
    facts = {}
    if isinstance(value, dict):
        for key, item in value.items():
            facts.update(_facts(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            facts.update(_facts(item, f"{path}[{i}]"))
    elif isinstance(value, str):
        for i, sentence in enumerate(re.split(r"(?<=[.!?])\s+|\n+", value)):
            if sentence.strip():
                facts[f"{path}:{i}"] = sentence.strip()
    elif value is not None:
        facts[path] = str(value)
    return facts


async def grounded_reason(sdk, state, facts, categories, module, step):
    mapping, options = _options(list(facts.items()), lambda fact: {"source": fact[0], "text": fact[1]})
    options.pop("none")
    options["unsupported"] = "No supplied fact directly supports a concrete reason."
    d = await ask(sdk, state, {
        "category": choice("Choose the reason for the selected decision, or unsupported if it cannot be represented.",
                           {**categories, "unsupported": "Cannot represent this reason."}),
        "evidence": choice("Which supplied fact directly supports the reason for the selected decision? Do not infer an unstated fact.", options),
    }, module, step)
    try:
        category, evidence = d.pick("category"), d.pick("evidence")
        if "unsupported" in (category, evidence):
            raise Uncertain("reason needs a generated explanation")
        text = f"{categories[category]} — established context: {mapping[evidence][1]}"
        await d.finish("accepted")
        return text, d
    except asyncio.CancelledError:
        await d.finish("cancelled")
        raise
    except Uncertain as exc:
        d.reason = str(exc)
        return None, d


async def assess_action(sdk, prompt, character, practice, fail_max, evidence):
    skills = character.get("skills", {})
    active = [n for n, s in skills.items() if s.get("type", "active") == "active"]
    curses = [n for n, s in skills.items() if s.get("type") == "curse"]
    passives = [n for n, s in skills.items() if s.get("type") == "passive"]
    active_map, active_options = _options(active, lambda n: {"name": n, **skills[n]})
    curse_map, curse_options = _options(curses, lambda n: {"name": n, **skills[n]})
    # Include an unsupported answer even for a character with no such skills.
    active_options["unsupported"] = "The applicable skill cannot be represented by this list."
    curse_options["unsupported"] = "The curse ruling cannot be represented by this list."
    questions = {
        "substantive": noul("Is the player action substantive, with a contested outcome or mechanical consequence? Contested social attempts count; ordinary dialogue and routine actions do not."),
        "feasibility": choice("Choose the exact integer feasibility under the supplied scale, world facts, difficulty and custom judging instructions.",
                              {str(n): f"Feasibility {n}/10 under the supplied task's rubric." for n in range(1, 11)}),
        "difficulty": choice("Choose the difficulty of this action under the supplied instructions.",
                             {s: s for s in ("trivial", "easy", "moderate", "hard", "extreme", "impossible")}),
        "skill": choice("Which single active skill is used in the assessed action?", active_options),
        "curse": choice("Which single curse is triggered? Choose unsupported if several triggered curses cannot be represented by one.", curse_options),
    }
    if practice and active:
        questions["practice"] = choice("Which single active skill is most relevant for practice progression on the player's action?", active_options)
    for i, name in enumerate(passives):
        questions[f"passive_{i}"] = noul(f"Does the passive skill {name!r} apply to this action? Evaluate its recorded description and conditions.")
    state = {"task": prompt, "character": character}
    d = await ask(sdk, state, questions, RPG, "rpg:action")
    try:
        substantive = d.yes("substantive")
        practice_name = None
        if "practice" in questions:
            selected = d.pick("practice")
            if selected == "unsupported":
                raise Uncertain("unsupported practice decision")
            practice_name = active_map.get(selected)
        if not substantive:
            await d.finish("skipped")
            return ({}, practice_name), d
        feasibility, difficulty = int(d.pick("feasibility")), d.pick("difficulty")
        selected_skill, selected_curse = d.pick("skill"), d.pick("curse")
        if "unsupported" in (selected_skill, selected_curse):
            raise Uncertain("unsupported skill or curse")
        if difficulty == "impossible" and feasibility > 2:
            raise Uncertain("inconsistent impossible ruling")
        applied = [name for i, name in enumerate(passives) if d.yes(f"passive_{i}")]
        assessment = {"feasibility": feasibility, "difficulty": difficulty,
                      "skill_used": active_map.get(selected_skill, ""),
                      "curse_triggered": curse_map.get(selected_curse, ""),
                      "passive_effects": "; ".join(f"{n}: {skills[n].get('description', n)}" for n in applied),
                      "failure_reason": ""}
        if feasibility <= fail_max:
            reason, reason_call = await grounded_reason(sdk,
                {**state, "selected_assessment": assessment}, _facts(evidence),
                {"world_rule": "Conflicts with an established world rule",
                 "story_fact": "Conflicts with established circumstances",
                 "capability": "Exceeds the character's relevant capabilities",
                 "condition": "Prevented by a current condition"}, RPG, "rpg:failure_reason")
            if reason is None:
                await d.finish("fallback", reason_call.reason)
                return None, reason_call
            assessment["failure_reason"] = reason
        await d.finish("accepted")
        return (assessment, practice_name), d
    except asyncio.CancelledError:
        await d.finish("cancelled")
        raise
    except Uncertain as exc:
        d.reason = str(exc)
        return None, d


async def judge_xp(sdk, prompt):
    reasons = {"success": "Meaningful success", "failure": "An instructive failure",
               "ingenuity": "Risk or ingenuity", "extraordinary": "An extraordinary feat",
               "routine": "Routine, repeated, or unengaged action", "unsupported": "Needs a specific explanation outside these categories"}
    d = await ask(sdk, {"task": prompt}, {
        "amount": choice("Choose the exact whole-number XP award under the supplied default rules. Use outside if the appropriate amount is not in the list.",
                         {**{str(n): f"Award {n} XP" for n in range(51)}, "outside": "Award outside 0–50 XP"}),
        "reason": choice("Why was XP earned or withheld in the resolved scene?", reasons),
    }, RPG, "rpg:xp")
    try:
        amount, reason = d.pick("amount"), d.pick("reason")
        if amount == "outside" or reason == "unsupported":
            raise Uncertain("XP requires existing judge")
        amount = int(amount)
        if (amount == 0) != (reason == "routine"):
            raise Uncertain("inconsistent XP amount and reason")
        await d.finish("accepted")
        return {"xp_awarded": amount, "reason": reasons[reason]}, d
    except Uncertain as exc:
        d.reason = str(exc)
        return None, d


async def screen(sdk, state, questions, module, step):
    d = await ask(sdk, state, {key: noul(q) for key, q in questions.items()}, module, step)
    skip = bool(d.answers) and all(d.no_work(key) for key in questions)
    if skip:
        await d.finish("skipped")
    else:
        try:
            if any(d.yes(key) for key in questions):
                await d.finish("accepted")
            else:
                d.reason = "not certain enough to skip generation"
        except Uncertain as exc:
            d.reason = str(exc)
    return skip, d


def npc_context(state, candidates):
    return {"recent_story": list(state.get("history", []))[-5:],
            "player_action": state.get("last_input_text") or state.get("input_text", ""),
            "characters": candidates, "turn": state.get("turn", 0),
            "location": {k: v for k, v in state.items() if k.startswith("player_location")},
            "story_threads": state.get("module_data", {}).get(NPC, {}).get("story_threads", []),
            "plot_direction": state.get("module_data", {}).get("wb_plot_director", {}),
            "instructions": state.get("module_instructions", {})}


async def npc_ids(sdk, state, candidates, kind):
    if not candidates:
        return set(), Decision()
    instruction = ("Is this character physically present with the player NOW? A mention, memory or discussion alone is not presence."
                   if kind == "presence" else
                   "Does this off-screen character have a concrete story reason RIGHT NOW to travel toward the player, such as a goal, grudge, debt, errand or plot tie? Most characters should not.")
    questions = {f"npc_{i}": noul({"question": instruction, "character_id": npc["id"]})
                 for i, npc in enumerate(candidates)}
    d = await ask(sdk, npc_context(state, candidates), questions, NPC, f"npc:{kind}")
    try:
        selected = {npc["id"] for i, npc in enumerate(candidates) if d.yes(f"npc_{i}")}
        await d.finish("accepted")
        return selected, d
    except Uncertain as exc:
        d.reason = str(exc)
        return None, d


async def introduce(sdk, state, candidates, prompt):
    mapping, options = _options(candidates, lambda npc: npc)
    options["unsupported"] = "The decision needs reasoning not represented by the eligible candidates."
    context = {**npc_context(state, candidates), "task": prompt}
    d = await ask(sdk, context, {"npc": choice(
        "Choose the single eligible character the scene naturally calls for now, or none. Respect the supplied introduction rules.", options)}, NPC, "npc:introduction")
    try:
        selected = d.pick("npc")
        if selected == "none":
            await d.finish("accepted")
            return {"introduce": False}, d
        if selected == "unsupported":
            raise Uncertain("unsupported introduction")
        npc = mapping[selected]
        reason, reason_call = await grounded_reason(sdk, {**context, "selected_character": npc},
            _facts({"character": npc, "recent_story": context["recent_story"]}),
            {"need": "The scene calls for this character's role",
             "location": "The character belongs at this location",
             "thread": "The character serves an active story thread"}, NPC, "npc:introduction_reason")
        if reason is None:
            await d.finish("fallback", reason_call.reason)
            return None, reason_call
        await d.finish("accepted")
        return {"introduce": True, "npc_id": npc["id"], "reason": reason}, d
    except asyncio.CancelledError:
        await d.finish("cancelled")
        raise
    except Uncertain as exc:
        d.reason = str(exc)
        return None, d
