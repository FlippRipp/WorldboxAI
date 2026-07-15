import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_plot_director" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_plot_director_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_sdk(replies, captured: dict):
    """SDK stub whose llm.generate pops canned replies in order and records
    every prompt it was given."""
    queue = list(replies) if isinstance(replies, list) else [replies]

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        captured.setdefault("prompts", []).append(prompt)
        captured.setdefault("preferences", []).append(model_preference)
        return queue.pop(0) if queue else "{}"

    return SimpleNamespace(llm=SimpleNamespace(generate=generate, _current_module=""))


THREAD_REPLY = json.dumps({
    "title": "The Salt Baron's Ledger",
    "hook": "A dockworker presses a stolen ledger into your hands.",
    "challenge": "The Baron's enforcers want it back -- and know your face.",
    "stakes": "The harbor district's independence.",
    "appeal": "intrigue and back-alley deals",
})

SECOND_THREAD_REPLY = json.dumps({
    "title": "Tremors Below",
    "hook": "The ground hums at night beneath the market.",
    "challenge": "Something old is digging upward, faster each day.",
    "stakes": "The market quarter's foundations.",
    "appeal": "mystery",
})

CRITIC_ACCEPT = json.dumps({"verdict": "accept", "critique": ""})
CRITIC_REJECT = json.dumps({
    "verdict": "reject",
    "critique": "Not grounded in the established harbor cast.",
})
DEFER_REPLY = json.dumps({"defer": True})

DIRECTION_REPLY = json.dumps({
    "consequence": "The magistrate now owes you a favor, and the Baron wants revenge.",
    "premise": "A quiet war for the harbor's soul between the guilds and the Salt Baron.",
    "heading": "The Baron is preparing a reprisal against the docks.",
    "open_questions": ["Who tipped off the enforcers?"],
    "recurring_elements": ["The Salt Baron", "the harbor docks"],
    "attachments": [{"name": "The Salt Baron", "kind": "character", "note": "the player keeps circling him"}],
    "engagement": {"bites_on": ["intrigue with a personal stake"], "ignores": ["open brawls"]},
    "narrative": {"pacing": "slow-burn", "agency": "drives the story", "register": "tense and wry"},
    "notes": "Prefers to talk first and fight only when cornered.",
})

# Zero-cooldown config: restores the classic close-and-replace-in-one-pass
# behavior for tests whose subject is the replacement itself.
NO_COOLDOWN = {"thread_cooldown_turns": 0}


def _profile_reply(**overrides):
    profile = {
        "playstyle": {"combat": 1, "diplomacy": 4, "exploration": 2,
                      "mystery": 3, "social": 5, "intrigue": 6},
        "tone": "gritty",
        "themes": ["smuggling", "harbor politics"],
        "likes": ["back-alley deals", "haggling"],
        "dislikes": [],
    }
    profile.update(overrides)
    return profile


def _assessment_reply(**overrides):
    reply = {
        "thread_engaged": True,
        "thread_resolved": False,
        "resolution_note": "",
        "opportunity": False,
        "nudge": "A beggar mutters about enforcers asking around the docks.",
        "momentum": "steady",
        "profile": _profile_reply(),
    }
    reply.update(overrides)
    return json.dumps(reply)


def _observing_data(backend):
    """Default data with the narrative direction already seeded, so tests that
    are not about seeding skip the one-time seed call."""
    data = backend._default_data()
    data["direction"]["premise"] = "A test premise already in place."
    return data


def _active_data(backend, created_turn=1, **overrides):
    data = _observing_data(backend)
    data["status"] = "active"
    data["thread"] = backend._full_thread(
        id=f"t{created_turn}",
        title="The Salt Baron's Ledger",
        hook="A dockworker presses a stolen ledger into your hands.",
        challenge="The Baron's enforcers want it back -- and know your face.",
        stakes="The harbor district's independence.",
        appeal="intrigue and back-alley deals",
        status="active",
        created_turn=created_turn,
    )
    data["momentum"] = "steady"
    data.update(overrides)
    return data


def _state(turn=3, data=None, config=None, history=None, **extra):
    state = {
        "turn": turn,
        "history": history if history is not None else ["The market square bustles."],
        "chat_messages": [{"role": "user", "content": "I browse the stalls."}],
        "module_configs": {"wb_plot_director": config or {}},
        "module_data": {"wb_plot_director": data} if data is not None else {},
    }
    state.update(extra)
    return state


def _updates(result):
    return result["module_data"]["wb_plot_director"]


def test_gather_context_seeds_skeleton_once():
    backend = _load_backend()
    sdk = _make_sdk([], {})

    first = asyncio.run(backend.on_gather_context(_state(data=None), sdk))
    seeded = _updates(first)
    assert seeded["schema"] == backend.SCHEMA_VERSION
    assert seeded["status"] == "observing"
    assert seeded["thread"]["status"] == "none"
    assert seeded["profile"]["playstyle"] == {k: 0 for k in backend.PLAYSTYLE_KEYS}
    assert seeded["profile"]["likes"] == []
    assert seeded["profile"]["avoids"] == []
    assert seeded["profile"]["attachments"] == []
    assert seeded["profile"]["engagement"] == {"bites_on": [], "ignores": []}
    assert seeded["profile"]["narrative"] == {"pacing": "", "agency": "", "register": ""}
    assert seeded["direction"] == backend._default_direction()
    assert seeded["next_thread_turn"] == 0
    assert seeded["defer_streak"] == 0

    again = asyncio.run(backend.on_gather_context(_state(data=seeded), sdk))
    assert again == {}


def test_gather_context_migrates_legacy_save():
    backend = _load_backend()
    sdk = _make_sdk([], {})
    legacy = {
        "outline": {"premise": "A buried king stirs.", "acts": [{"beats": []}]},
        "status": "ready",
        "position": {"act": 2, "beat_index": 0},
        "beats_completed": [{"id": "a1b1"}],
        "assessment_log": [{"turn": 2}],
        "stall_count": 2,
        "drift_streak": 1,
        "pending_nudge": "old nudge",
    }

    result = asyncio.run(backend.on_gather_context(_state(data=legacy), sdk))
    update = _updates(result)
    assert update["schema"] == backend.SCHEMA_VERSION
    assert update["status"] == "observing"
    # Deep-merge never deletes keys: dead legacy dicts/lists must be blanked
    # with non-dict values so they get replaced, not merged around.
    for key in backend.LEGACY_KEYS:
        assert update[key] == ""
    assert update["pending_nudge"] == ""


def test_gather_context_soft_migrates_v2_save_preserving_profile_and_thread():
    backend = _load_backend()
    sdk = _make_sdk([], {})
    v2 = {
        "schema": 2,
        "status": "observing",
        "gen_attempts": 0,
        "profile": _profile_reply(),
        "thread": {"status": "none"},
        "thread_history": [{"title": "The Pilgrim Road", "outcome": "resolved",
                            "created_turn": 2, "closed_turn": 9, "note": "done"}],
        "momentum": "observing",
        "ignored_streak": 0,
        "pending_nudge": "",
        "last_nudge_turn": 0,
        "suspended": False,
        "suspended_turn": 0,
        "log": [],
    }

    result = asyncio.run(backend.on_gather_context(_state(data=v2), sdk))
    update = _updates(result)

    # Additive migration: only the version bump, the upgraded profile, and the
    # new v3 keys -- nothing learned is wiped, no legacy blanking.
    assert set(update) == {"schema", "profile", "direction", "last_closed_turn",
                           "next_thread_turn", "defer_streak", "direction_seed_attempts",
                           "last_pivot_turn"}
    assert update["schema"] == backend.SCHEMA_VERSION
    assert update["direction"] == backend._default_direction()
    # The v2 profile survives in full, upgraded to the v3 shape in place.
    profile = update["profile"]
    assert profile["tone"] == "gritty"
    assert profile["likes"] == [
        {"text": "back-alley deals", "weight": "medium", "evidence": ""},
        {"text": "haggling", "weight": "medium", "evidence": ""},
    ]
    assert profile["playstyle"]["intrigue"] == 6
    assert profile["playstyle"]["stealth"] == 0  # new axes default to 0
    assert profile["avoids"] == []
    assert profile["notes"] == ""

    # Second pass (post-merge): native v3, no further migration.
    merged = {**v2, **update}
    again = asyncio.run(backend.on_gather_context(_state(data=merged), sdk))
    assert again == {}


def test_gather_context_emits_context_line_for_active_thread():
    backend = _load_backend()
    sdk = _make_sdk([], {})
    data = _active_data(backend)

    result = asyncio.run(backend.on_gather_context(_state(data=data), sdk))
    context = result["context_string"]
    assert "The Salt Baron's Ledger" in context
    assert "stolen ledger" in context
    assert "optional" in context.lower()
    assert "never at the expense of what the player is doing" in context

    observing = asyncio.run(backend.on_gather_context(_state(data=backend._default_data()), sdk))
    assert "context_string" not in observing

    disabled = asyncio.run(backend.on_gather_context(
        _state(data=data, config={"plot_enabled": False}), sdk))
    assert "context_string" not in disabled


def test_context_line_softens_when_ignored_and_carries_nudge():
    backend = _load_backend()
    sdk = _make_sdk([], {})

    # Below the stall threshold: no softening, no nudge.
    plain = asyncio.run(backend.on_gather_context(
        _state(data=_active_data(backend, ignored_streak=1)), sdk))["context_string"]
    assert "faint background color" not in plain
    assert "one light way in" not in plain

    # At/past the stall threshold the line backs off.
    ignored = asyncio.run(backend.on_gather_context(
        _state(data=_active_data(backend, ignored_streak=3)), sdk))["context_string"]
    assert "faint background color" in ignored

    # An armed nudge rides inside the context line -- there is no separate
    # prompt-block injection anymore.
    assert not hasattr(backend, "on_render_prompt_block")
    nudged = asyncio.run(backend.on_gather_context(
        _state(data=_active_data(backend, pending_nudge="A rumor spreads.", last_nudge_turn=3)),
        sdk))["context_string"]
    assert "A rumor spreads." in nudged
    assert "one light way in" in nudged


def test_first_librarian_generates_thread_from_scenario():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured)
    state = _state(
        turn=1,
        data=_observing_data(backend),
        history=["You arrive at the forgotten ruins."],
        scenario_data={"scenario_description": "An expedition to the forgotten ruins."},
    )

    result = asyncio.run(backend.on_librarian(state, sdk))
    update = _updates(result)

    # Generation on the smartest model, then the fit-check critic on balanced.
    assert len(captured["prompts"]) == 2
    assert "forgotten ruins" in captured["prompts"][0]
    assert "GROUND IT" in captured["prompts"][0]
    assert captured["preferences"] == ["smartest", "balanced"]
    assert update["status"] == "active"
    assert update["momentum"] == "building"
    assert update["gen_attempts"] == 0
    assert update["defer_streak"] == 0
    thread = update["thread"]
    assert set(thread) == set(backend._empty_thread())
    assert thread["status"] == "active"
    assert thread["title"] == "The Salt Baron's Ledger"
    assert thread["created_turn"] == 1


def test_generation_prompt_includes_profile_difficulty_and_npc_threads():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured)
    data = _observing_data(backend)
    data["profile"] = _profile_reply()
    state = _state(turn=1, data=data, config={"difficulty": 5})
    state["module_data"]["wb_npc_system"] = {
        "story_threads": [{"text": "The dockmaster plots against the guild.", "npc_ids": ["n1"]}],
    }

    asyncio.run(backend.on_librarian(state, sdk))
    prompt = captured["prompts"][0]
    assert backend.DIFFICULTY_GUIDANCE[5] in prompt
    assert "back-alley deals" in prompt
    assert "dockmaster plots against the guild" in prompt

    # Soft-fail: no npc module data at all -> generation still runs, blocks omitted.
    captured2 = {}
    sdk2 = _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured2)
    result = asyncio.run(backend.on_librarian(_state(turn=1, data=_observing_data(backend)), sdk2))
    assert _updates(result)["status"] == "active"
    assert "OTHER ACTIVE STORYLINES" not in captured2["prompts"][0]
    assert "ESTABLISHED CHARACTERS" not in captured2["prompts"][0]


def test_generation_prompt_includes_character_roster():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured)
    state = _state(turn=1, data=_observing_data(backend))
    state["module_data"]["wb_npc_system"] = {
        "characters": {
            "npc_1": {"name": "Mira Voss", "archetype": "smuggler queen", "role": "antagonist",
                      "introduced": True, "status": "active",
                      "pitch": "Controls the night docks and everyone in debt to them."},
            "npc_2": {"name": "Brother Callum", "archetype": "wandering scholar", "role": "ally",
                      "introduced": False, "status": "unintroduced",
                      "pitch": "Knows what sleeps under the market."},
            "npc_3": {"name": "Old Tam", "archetype": "fisherman", "role": "neutral",
                      "introduced": True, "status": "deceased", "pitch": "Gone."},
        },
    }

    asyncio.run(backend.on_librarian(state, sdk))
    prompt = captured["prompts"][0]
    assert "ESTABLISHED CHARACTERS" in prompt
    assert "Mira Voss" in prompt and "smuggler queen" in prompt and "met" in prompt
    assert "Controls the night docks" in prompt
    assert "Brother Callum" in prompt and "not yet met" in prompt
    assert "Old Tam" not in prompt  # the dead stay out of new threads
    # The critic sees the same grounding material.
    assert "Mira Voss" in captured["prompts"][1]


def test_direction_and_consequences_feed_generation():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured)
    data = backend._default_data()
    data["profile"] = _profile_reply()  # non-empty: skip the bootstrap path
    data["direction"] = {
        "premise": "A quiet war for the harbor's soul.",
        "heading": "The Baron plans a reprisal.",
        "open_questions": ["Who tipped off the enforcers?"],
        "recurring_elements": ["The Salt Baron"],
        "updated_turn": 5,
    }
    data["thread_history"] = [{
        "title": "The Pilgrim Road", "outcome": "resolved", "created_turn": 2,
        "closed_turn": 9, "note": "", "consequence": "The pilgrims now owe the player passage.",
    }]

    asyncio.run(backend.on_librarian(_state(turn=10, data=data), sdk))
    prompt = captured["prompts"][0]
    assert "NARRATIVE DIRECTION" in prompt
    assert "A quiet war for the harbor's soul." in prompt
    assert "Who tipped off the enforcers?" in prompt
    assert "BUILD on" in prompt
    assert "The pilgrims now owe the player passage." in prompt
    assert "do not repeat" not in prompt  # the avoid-list framing is gone


def test_adoption_midstory_bootstraps_profile_before_first_thread():
    backend = _load_backend()
    captured = {}
    bootstrap_reply = json.dumps(_profile_reply())
    sdk = _make_sdk([bootstrap_reply, THREAD_REPLY, CRITIC_ACCEPT], captured)
    state = _state(
        turn=12,
        data=_observing_data(backend),
        history=["The intro.", "You duel the harbor watch.", "You bribe the customs clerk."],
    )
    state["chat_messages"] = [
        {"role": "user", "content": "I challenge the watch captain."},
        {"role": "ai", "content": "Steel rings."},
        {"role": "user", "content": "I slip the clerk a purse."},
    ]

    result = asyncio.run(backend.on_librarian(state, sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 3
    assert captured["preferences"] == ["smartest", "smartest", "balanced"]
    bootstrap_prompt = captured["prompts"][0]
    assert "already in progress" in bootstrap_prompt
    assert "bribe the customs clerk" in bootstrap_prompt
    assert "I slip the clerk a purse." in bootstrap_prompt
    # The bootstrap builds the full deep profile from the start.
    assert "attachments" in bootstrap_prompt
    assert "avoids" in bootstrap_prompt
    # The thread generation that follows sees the bootstrapped profile.
    assert "back-alley deals" in captured["prompts"][1]
    assert update["profile"]["likes"] == [
        {"text": "back-alley deals", "weight": "medium", "evidence": ""},
        {"text": "haggling", "weight": "medium", "evidence": ""},
    ]
    assert update["status"] == "active"
    assert update["thread"]["title"] == "The Salt Baron's Ledger"


def test_adoption_bootstrap_failure_still_generates_thread():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk(["not json", THREAD_REPLY, CRITIC_ACCEPT], captured)

    result = asyncio.run(backend.on_librarian(
        _state(turn=12, data=_observing_data(backend)), sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 3
    assert "profile" not in update  # starting blind, no profile written
    assert update["status"] == "active"
    assert update["thread"]["title"] == "The Salt Baron's Ledger"


def test_fresh_story_does_not_bootstrap():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured)

    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=_observing_data(backend)), sdk))

    assert len(captured["prompts"]) == 2  # thread generation + fit check only
    assert "already in progress" not in captured["prompts"][0]
    assert _updates(result)["status"] == "active"


def test_generation_failure_retries_then_dormant():
    backend = _load_backend()
    data = _observing_data(backend)

    for attempt in range(1, backend.GEN_MAX_ATTEMPTS + 1):
        captured = {}
        sdk = _make_sdk(["not json"], captured)
        result = asyncio.run(backend.on_librarian(_state(turn=1, data=data), sdk))
        data.update(_updates(result))
        assert data["gen_attempts"] == attempt
        # A malformed generation never reaches the critic.
        assert len(captured["prompts"]) == 1

    assert data["status"] == "failed"
    assert asyncio.run(backend.on_librarian(_state(turn=2, data=data), _make_sdk([], {}))) is None

    command = asyncio.run(backend.on_command_plot([], _state(data=data), _make_sdk([], {})))
    assert "inactive" in command["message"]
    gathered = asyncio.run(backend.on_gather_context(_state(data=data), _make_sdk([], {})))
    assert "context_string" not in gathered


def test_fit_check_reject_retries_with_critique():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_REJECT, SECOND_THREAD_REPLY, CRITIC_ACCEPT], captured)

    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=_observing_data(backend)), sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 4
    assert captured["preferences"] == ["smartest", "balanced", "smartest", "balanced"]
    # The retry generation carries the critic's critique.
    assert "Not grounded in the established harbor cast." in captured["prompts"][2]
    assert "rejected by a quality check" in captured["prompts"][2]
    assert update["thread"]["title"] == "Tremors Below"
    assert update["status"] == "active"
    assert update["gen_attempts"] == 0


def test_fit_check_double_reject_accepts_second_candidate():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_REJECT, SECOND_THREAD_REPLY, CRITIC_REJECT], captured)

    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=_observing_data(backend)), sdk))
    update = _updates(result)

    # The critic never starves the module: the second candidate ships anyway,
    # and gen_attempts stays clear of the dormancy counter.
    assert update["thread"]["title"] == "Tremors Below"
    assert update["status"] == "active"
    assert update["gen_attempts"] == 0


def test_fit_check_fails_open():
    backend = _load_backend()

    # Disabled: generation only, no critic call.
    captured = {}
    sdk = _make_sdk([THREAD_REPLY], captured)
    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=_observing_data(backend), config={"fit_check_enabled": False}), sdk))
    assert len(captured["prompts"]) == 1
    assert _updates(result)["thread"]["title"] == "The Salt Baron's Ledger"

    # Malformed critic reply accepts the candidate.
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, "not json"], captured)
    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=_observing_data(backend)), sdk))
    assert len(captured["prompts"]) == 2
    assert _updates(result)["thread"]["title"] == "The Salt Baron's Ledger"

    # A malformed retry generation falls back to the first (valid) candidate.
    captured = {}
    sdk = _make_sdk([THREAD_REPLY, CRITIC_REJECT, "not json"], captured)
    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=_observing_data(backend)), sdk))
    assert len(captured["prompts"]) == 3
    update = _updates(result)
    assert update["thread"]["title"] == "The Salt Baron's Ledger"
    assert update["gen_attempts"] == 0


def test_direction_seeds_from_story_start():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([DIRECTION_REPLY, THREAD_REPLY, CRITIC_ACCEPT], captured)
    data = backend._default_data()
    data["profile"] = _profile_reply()  # non-empty: skip the bootstrap path

    result = asyncio.run(backend.on_librarian(_state(turn=1, data=data), sdk))
    update = _updates(result)

    # Seed (balanced) runs before the first generation, so the overarching
    # plot exists from the story's first turns -- not only after a close.
    assert captured["preferences"] == ["balanced", "smartest", "balanced"]
    assert "INITIAL narrative direction" in captured["prompts"][0]
    assert update["direction"]["premise"].startswith("A quiet war")
    assert update["direction"]["updated_turn"] == 1
    # The freshly seeded direction already steers the first thread.
    assert "A quiet war for the harbor's soul" in captured["prompts"][1]
    # Deep profile fields arrive with the seed.
    assert update["profile"]["attachments"][0]["name"] == "The Salt Baron"
    assert update["thread"]["title"] == "The Salt Baron's Ledger"


def test_direction_seed_covers_migrated_active_thread_and_caps_retries():
    backend = _load_backend()

    # A save migrated mid-thread: active thread, empty direction. The seed
    # runs alongside the normal assessment.
    data = _active_data(backend)
    data["direction"] = backend._default_direction()
    captured = {}
    sdk = _make_sdk([DIRECTION_REPLY, _assessment_reply()], captured)
    result = asyncio.run(backend.on_librarian(_state(turn=3, data=data), sdk))
    update = _updates(result)
    assert len(captured["prompts"]) == 2
    assert update["direction"]["premise"].startswith("A quiet war")
    # The assessment builds on the seeded profile rather than clobbering it.
    assert update["profile"]["attachments"][0]["name"] == "The Salt Baron"
    assert update["profile"]["tone"] == "gritty"

    # Failed seeds are capped at two attempts, then the module stops paying
    # for them until a close writes the direction.
    data = _active_data(backend)
    data["direction"] = backend._default_direction()
    for expected_attempts in (1, 2):
        result = asyncio.run(backend.on_librarian(
            _state(turn=3, data=data), _make_sdk(["garbage", _assessment_reply()], {})))
        update = _updates(result)
        assert update["direction_seed_attempts"] == expected_attempts
        assert "direction" not in update
        data.update(update)
    captured = {}
    asyncio.run(backend.on_librarian(
        _state(turn=4, data=data), _make_sdk([_assessment_reply()], captured)))
    assert len(captured["prompts"]) == 1  # assessment only, no more seed tries


def test_generation_defer_postpones_without_failure():
    backend = _load_backend()
    data = _observing_data(backend)
    data["profile"] = _profile_reply()  # non-empty: skip the bootstrap path

    # First ask: the generator defers -- no thread, no failure bookkeeping.
    captured = {}
    result = asyncio.run(backend.on_librarian(
        _state(turn=5, data=data), _make_sdk([DEFER_REPLY], captured)))
    update = _updates(result)
    assert '"defer"' in captured["prompts"][0]
    assert "thread" not in update
    assert update["defer_streak"] == 1
    assert update["gen_attempts"] == 0
    assert update["next_thread_turn"] == 7
    data.update(update)

    # The deferred window is quiet: zero LLM calls.
    captured = {}
    assert asyncio.run(backend.on_librarian(
        _state(turn=6, data=data), _make_sdk([], captured))) is None
    assert captured == {}

    # Second defer allowed...
    result = asyncio.run(backend.on_librarian(
        _state(turn=7, data=data), _make_sdk([DEFER_REPLY], {})))
    update = _updates(result)
    assert update["defer_streak"] == 2
    data.update(update)

    # ...but after DEFER_MAX_STREAK the option disappears and generation runs.
    captured = {}
    result = asyncio.run(backend.on_librarian(
        _state(turn=9, data=data), _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], captured)))
    update = _updates(result)
    assert '"defer"' not in captured["prompts"][0]
    assert update["thread"]["title"] == "The Salt Baron's Ledger"
    assert update["defer_streak"] == 0


def test_assessment_updates_profile_and_resets_streak():
    backend = _load_backend()
    captured = {}
    wild_profile = _profile_reply(
        playstyle={"combat": 99, "diplomacy": 4, "exploration": 2,
                   "mystery": 3, "social": 5, "intrigue": 6},
        likes=[f"like {i}" for i in range(backend.PROFILE_LIST_CAP + 4)],
    )
    sdk = _make_sdk([_assessment_reply(profile=wild_profile)], captured)
    data = _active_data(backend, ignored_streak=2)

    result = asyncio.run(backend.on_librarian(_state(turn=3, data=data), sdk))
    update = _updates(result)

    prompt = captured["prompts"][0]
    assert "The Salt Baron's Ledger" in prompt
    assert "I browse the stalls." in prompt
    assert json.dumps(data["profile"], ensure_ascii=False) in prompt
    assert "evidence" in prompt  # observed entries must cite behavior
    assert captured["preferences"] == ["balanced"]

    assert update["ignored_streak"] == 0
    assert update["profile"]["playstyle"]["combat"] == 10
    assert len(update["profile"]["likes"]) == backend.PROFILE_LIST_CAP
    assert update["momentum"] == "steady"
    assert update["log"][-1]["turn"] == 3


def test_ai_writes_avoids_with_evidence_but_never_against_player_taste():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = backend._default_profile()
    data["profile"]["dislikes"] = [{"text": "body horror", "weight": "high"}]
    data["profile"]["likes"] = [{"text": "courtly politics", "weight": "high", "evidence": ""}]

    reply_profile = _profile_reply(
        likes=[{"text": "courtly politics", "weight": "high"}],
        avoids=[
            {"text": "open brawls", "weight": "medium", "evidence": "talked their way out three times"},
            {"text": "body horror", "weight": "high", "evidence": "in-story flinch"},   # matches a dislike
            {"text": "Courtly Politics", "weight": "low", "evidence": "guessed"},        # matches a like
        ],
    )
    result = asyncio.run(backend.on_librarian(
        _state(turn=3, data=data), _make_sdk([_assessment_reply(profile=reply_profile)], {})))
    profile = _updates(result)["profile"]

    # The genuine observation lands, evidence intact.
    assert profile["avoids"] == [
        {"text": "open brawls", "weight": "medium", "evidence": "talked their way out three times"},
    ]
    # Player-set taste wins on both sides.
    assert profile["dislikes"] == [{"text": "body horror", "weight": "high"}]
    assert any(e["text"] == "courtly politics" for e in profile["likes"])


def test_resolution_with_zero_cooldown_regenerates_with_full_replacement():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([
        _assessment_reply(thread_resolved=True, resolution_note="The ledger reached the magistrate."),
        DIRECTION_REPLY,
        SECOND_THREAD_REPLY,
        CRITIC_ACCEPT,
    ], captured)
    data = _active_data(backend, ignored_streak=1)

    result = asyncio.run(backend.on_librarian(
        _state(turn=5, data=data, config=NO_COOLDOWN), sdk))
    update = _updates(result)

    # Assessment, direction update, generation, fit check -- all in one pass.
    assert len(captured["prompts"]) == 4
    assert captured["preferences"] == ["balanced", "balanced", "smartest", "balanced"]
    closed = update["thread_history"][-1]
    assert closed["outcome"] == "resolved"
    assert closed["title"] == "The Salt Baron's Ledger"
    assert closed["closed_turn"] == 5
    assert closed["consequence"] == "The magistrate now owes you a favor, and the Baron wants revenge."

    # Full-key overwrite contract: the new thread replaces every field.
    thread = update["thread"]
    assert set(thread) == set(backend._empty_thread())
    assert thread["title"] == "Tremors Below"
    assert thread["stakes"] == "The market quarter's foundations."
    assert thread["closed_turn"] == 0
    assert thread["created_turn"] == 5
    assert update["momentum"] == "building"
    assert update["pending_nudge"] == ""
    # The evolved direction is stored, full shape.
    assert update["direction"]["premise"].startswith("A quiet war")
    assert update["direction"]["updated_turn"] == 5


def test_close_sets_cooldown_and_generation_waits():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([
        _assessment_reply(thread_resolved=True, resolution_note="The ledger reached the magistrate."),
        DIRECTION_REPLY,
    ], captured)
    data = _active_data(backend)

    result = asyncio.run(backend.on_librarian(_state(turn=5, data=data), sdk))
    update = _updates(result)

    # Default cooldown 3: no regeneration in this pass -- just the close.
    assert len(captured["prompts"]) == 2
    assert update["thread"]["status"] == "resolved"
    assert update["next_thread_turn"] == 8
    assert update["last_closed_turn"] == 5
    assert update["momentum"] == "resolving"
    assert update["ignored_streak"] == 0
    data.update(update)

    # The quiet period costs zero LLM calls.
    for turn in (6, 7):
        captured_gap = {}
        assert asyncio.run(backend.on_librarian(
            _state(turn=turn, data=data), _make_sdk([], captured_gap))) is None
        assert captured_gap == {}

    # At the gap's end the next thread is woven, building on the consequence.
    captured_end = {}
    result = asyncio.run(backend.on_librarian(
        _state(turn=8, data=data), _make_sdk([SECOND_THREAD_REPLY, CRITIC_ACCEPT], captured_end)))
    update = _updates(result)
    assert update["thread"]["title"] == "Tremors Below"
    assert "The magistrate now owes you a favor" in captured_end["prompts"][0]
    assert "NARRATIVE DIRECTION" in captured_end["prompts"][0]


def test_abandoned_cooldown_is_doubled():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([_assessment_reply(thread_engaged=False), DIRECTION_REPLY], captured)
    data = _active_data(backend, ignored_streak=3)

    result = asyncio.run(backend.on_librarian(_state(turn=6, data=data), sdk))
    update = _updates(result)

    assert update["thread_history"][-1]["outcome"] == "abandoned"
    assert len(captured["prompts"]) == 2  # assessment + direction, no regen yet
    assert update["next_thread_turn"] == 6 + 3 * backend.ABANDON_COOLDOWN_FACTOR
    # Abandonment must not write a dislike -- those are player-set only.
    assert update["profile"]["dislikes"] == []


def test_direction_update_failure_is_safe():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([
        _assessment_reply(thread_resolved=True, resolution_note="Settled quietly."),
        "not json",
    ], captured)
    data = _active_data(backend)

    result = asyncio.run(backend.on_librarian(_state(turn=5, data=data), sdk))
    update = _updates(result)

    # The close still completes; the consequence falls back to the resolution
    # note and the stored direction is left untouched.
    assert update["thread"]["status"] == "resolved"
    assert update["thread_history"][-1]["consequence"] == "Settled quietly."
    assert "direction" not in update
    assert update["next_thread_turn"] == 8


def test_close_time_deep_fields_merge_into_profile():
    backend = _load_backend()
    sdk = _make_sdk([
        _assessment_reply(thread_resolved=True, resolution_note="Done."),
        DIRECTION_REPLY,
    ], {})
    data = _active_data(backend)

    result = asyncio.run(backend.on_librarian(_state(turn=5, data=data), sdk))
    profile = _updates(result)["profile"]

    # Fast fields from the assessment survive...
    assert profile["tone"] == "gritty"
    assert any(e["text"] == "back-alley deals" for e in profile["likes"])
    # ...and the close-time call fills the slow-moving deep fields.
    assert profile["attachments"] == [
        {"name": "The Salt Baron", "kind": "character", "note": "the player keeps circling him"},
    ]
    assert profile["engagement"] == {
        "bites_on": ["intrigue with a personal stake"], "ignores": ["open brawls"],
    }
    assert profile["narrative"] == {
        "pacing": "slow-burn", "agency": "drives the story", "register": "tense and wry",
    }
    assert profile["notes"] == "Prefers to talk first and fight only when cornered."


def test_ignored_streak_nudges_then_abandons_without_touching_dislikes():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1)
    config = {"stall_threshold": 2, "thread_cooldown_turns": 0}  # abandon_after default 4
    armed_turn = None

    for turn in range(2, 6):
        replies = [_assessment_reply(thread_engaged=False)]
        if turn == 5:
            replies.extend([DIRECTION_REPLY, SECOND_THREAD_REPLY, CRITIC_ACCEPT])
        captured = {}
        sdk = _make_sdk(replies, captured)
        result = asyncio.run(backend.on_librarian(_state(turn=turn, data=data, config=config), sdk))
        update = _updates(result)

        if turn == 3:  # streak hits stall_threshold (2) exactly -> nudge armed
            assert update["pending_nudge"]
            armed_turn = turn
        if turn == 4:  # streak 3: past the threshold, no re-arm; streak keeps climbing
            assert update.get("pending_nudge", "") == ""  # consume-once clear only
            assert update["ignored_streak"] == 3
        if turn == 5:  # streak 4 = abandon_after -> abandoned + direction + regeneration
            assert len(captured["prompts"]) == 4
            assert update["thread_history"][-1]["outcome"] == "abandoned"
            # Abandonment must not write a dislike -- those are player-set only.
            assert update["profile"]["dislikes"] == []
            generation_prompt = captured["prompts"][2]
            assert "DIFFERENT kind" in generation_prompt
            assert '"The Salt Baron\'s Ledger" -- abandoned' in generation_prompt
            assert update["thread"]["title"] == "Tremors Below"

        data.update(update)

    assert armed_turn == 3


def test_expiry_by_turn_cap_skips_assessment():
    backend = _load_backend()

    # Default cooldown: the expiry pass spends only the direction call.
    captured = {}
    sdk = _make_sdk([DIRECTION_REPLY], captured)
    data = _active_data(backend, created_turn=1)
    result = asyncio.run(backend.on_librarian(_state(turn=13, data=data), sdk))
    update = _updates(result)
    assert len(captured["prompts"]) == 1
    assert update["thread_history"][-1]["outcome"] == "expired"
    assert update["thread"]["status"] == "expired"
    assert update["next_thread_turn"] == 16

    # Zero cooldown: close and replace in the same pass, still no assessment.
    captured = {}
    sdk = _make_sdk([DIRECTION_REPLY, SECOND_THREAD_REPLY, CRITIC_ACCEPT], captured)
    data = _active_data(backend, created_turn=1)
    result = asyncio.run(backend.on_librarian(
        _state(turn=13, data=data, config=NO_COOLDOWN), sdk))
    update = _updates(result)
    assert len(captured["prompts"]) == 3
    assert update["thread_history"][-1]["outcome"] == "expired"
    assert update["thread"]["title"] == "Tremors Below"


def test_nudge_consume_once():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1, pending_nudge="A rumor spreads.", last_nudge_turn=3)

    # Frequency-gated turn still consumes the already-rendered nudge; _deep_merge
    # cannot delete keys, so it must return an explicit "".
    result = asyncio.run(backend.on_librarian(
        _state(turn=4, data=data, config={"assessment_frequency": 5}), _make_sdk([], {})))
    assert _updates(result)["pending_nudge"] == ""


def test_opportunity_nudge_respects_toggle_and_cooldown():
    backend = _load_backend()

    def run(config, last_nudge_turn=0, turn=5):
        data = _active_data(backend, created_turn=1, last_nudge_turn=last_nudge_turn)
        sdk = _make_sdk([_assessment_reply(opportunity=True)], {})
        result = asyncio.run(backend.on_librarian(_state(turn=turn, data=data, config=config), sdk))
        return _updates(result).get("pending_nudge", "")

    assert run({}) != ""
    assert run({"opportunity_nudges": False}) == ""
    assert run({}, last_nudge_turn=3) == ""   # within the 3-turn cooldown
    assert run({}, last_nudge_turn=2) != ""   # cooldown elapsed


def test_malformed_assessment_reply_is_noop():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1, ignored_streak=1)

    result = asyncio.run(backend.on_librarian(
        _state(turn=4, data=data), _make_sdk(["[mock llm response for: ...]"], {})))
    assert result is None


def test_plot_regen_replaces_thread_and_writes_back():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([SECOND_THREAD_REPLY, CRITIC_ACCEPT], captured)
    data = _active_data(backend, created_turn=1)

    result = asyncio.run(backend.on_command_plot(["regen"], _state(turn=6, data=data), sdk))

    assert "Tremors Below" in result["message"]
    update = result["module_data"]["wb_plot_director"]
    assert update["thread_history"][-1]["outcome"] == "rerolled"
    assert update["thread_history"][-1]["title"] == "The Salt Baron's Ledger"
    assert update["thread_history"][-1]["consequence"] == "Set aside by the player."
    assert update["thread"]["title"] == "Tremors Below"
    assert update["thread"]["status"] == "active"
    # A reroll never waits out a quiet period.
    assert update["next_thread_turn"] == 0
    # The old thread appears in the generation prompt's consequences list.
    assert '"The Salt Baron\'s Ledger" -- rerolled' in captured["prompts"][0]
    # A player reroll records no dislike and skips the direction call.
    assert "profile" not in update
    assert "direction" not in update
    assert captured["preferences"] == ["smartest", "balanced"]


def test_plot_regen_failure_keeps_current_thread():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1)

    result = asyncio.run(backend.on_command_plot(
        ["regen"], _state(turn=6, data=data), _make_sdk(["not json"], {})))

    assert "nothing changed" in result["message"]
    assert "module_data" not in result

    # Regen also revives a dormant module on success.
    failed = backend._default_data()
    failed["status"] = "failed"
    failed["gen_attempts"] = 3
    result = asyncio.run(backend.on_command_plot(
        ["regen"], _state(turn=6, data=failed), _make_sdk([THREAD_REPLY, CRITIC_ACCEPT], {})))
    update = result["module_data"]["wb_plot_director"]
    assert update["status"] == "active"
    assert update["thread"]["status"] == "active"


def test_suspend_freezes_librarian_context_and_regen():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1, suspended=True, suspended_turn=2)

    # No context line, no librarian pass (no LLM calls), no reroll.
    gathered = asyncio.run(backend.on_gather_context(_state(data=data), _make_sdk([], {})))
    assert "context_string" not in gathered

    captured = {}
    assert asyncio.run(backend.on_librarian(_state(turn=13, data=data), _make_sdk([], captured))) is None
    assert captured == {}  # not even the turn-13 expiry fires while suspended

    regen = asyncio.run(backend.on_command_plot(["regen"], _state(turn=6, data=data), _make_sdk([], captured)))
    assert "suspended" in regen["message"]
    assert "module_data" not in regen
    assert captured == {}

    status = asyncio.run(backend.on_command_plot([], _state(data=data), _make_sdk([], {})))
    assert "Suspended since turn 2" in status["message"]


def test_suspend_and_resume_commands():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1, pending_nudge="A rumor spreads.")

    result = asyncio.run(backend.on_command_plot(["suspend"], _state(turn=4, data=data), _make_sdk([], {})))
    update = result["module_data"]["wb_plot_director"]
    assert update["suspended"] is True
    assert update["suspended_turn"] == 4
    assert update["pending_nudge"] == ""  # stale nudge never fires after resume
    data.update(update)

    again = asyncio.run(backend.on_command_plot(["suspend"], _state(turn=5, data=data), _make_sdk([], {})))
    assert "already suspended" in again["message"]
    assert "module_data" not in again

    # Resume 6 turns later: created_turn shifts by the paused duration, so the
    # thread has the same remaining lifespan it had when it was frozen.
    result = asyncio.run(backend.on_command_plot(["resume"], _state(turn=10, data=data), _make_sdk([], {})))
    update = result["module_data"]["wb_plot_director"]
    assert update["suspended"] is False
    assert update["thread"]["created_turn"] == 7  # 1 + (10 - 4)
    assert set(update["thread"]) == set(backend._empty_thread())
    assert update["thread"]["title"] == "The Salt Baron's Ledger"
    data.update(update)

    noop = asyncio.run(backend.on_command_plot(["resume"], _state(turn=11, data=data), _make_sdk([], {})))
    assert "not suspended" in noop["message"]
    assert "module_data" not in noop


def test_resume_between_threads_shifts_quiet_period():
    backend = _load_backend()

    # No pending quiet period: nothing but the flags change.
    data = backend._default_data()
    data["suspended"] = True
    data["suspended_turn"] = 3
    result = asyncio.run(backend.on_command_plot(["resume"], _state(turn=8, data=data), _make_sdk([], {})))
    assert result["module_data"]["wb_plot_director"] == {"suspended": False, "suspended_turn": 0}

    # A quiet period frozen mid-run resumes where it stopped.
    data = backend._default_data()
    data["suspended"] = True
    data["suspended_turn"] = 3
    data["next_thread_turn"] = 5
    result = asyncio.run(backend.on_command_plot(["resume"], _state(turn=8, data=data), _make_sdk([], {})))
    update = result["module_data"]["wb_plot_director"]
    assert update["next_thread_turn"] == 5 + (8 - 3)


def test_ai_cannot_write_dislikes_and_contradicting_likes_are_dropped():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = backend._default_profile()
    data["profile"]["dislikes"] = [{"text": "disobedience", "weight": "high"}]

    reply_profile = _profile_reply(
        likes=[{"text": "Disobedience", "weight": "low"}, {"text": "sea voyages", "weight": "medium"}],
        dislikes=[{"text": "ai-invented aversion", "weight": "high"}],
    )
    result = asyncio.run(backend.on_librarian(
        _state(turn=3, data=data), _make_sdk([_assessment_reply(profile=reply_profile)], {})))
    profile = _updates(result)["profile"]

    # The LLM's dislikes are ignored; the player's stand untouched.
    assert profile["dislikes"] == [{"text": "disobedience", "weight": "high"}]
    # A proposed like matching a player-set dislike is dropped (case-insensitive).
    texts = [e["text"] for e in profile["likes"]]
    assert "Disobedience" not in texts and "disobedience" not in texts
    assert "sea voyages" in texts


def test_profile_add_moves_entry_between_likes_and_dislikes():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = backend._default_profile()
    data["profile"]["dislikes"] = [{"text": "sea voyages", "weight": "medium"}]

    result = asyncio.run(backend.on_command_plot(
        ["profile", "likes", "add", "high", "sea", "voyages"], _state(data=data), _make_sdk([], {})))

    assert "Moved" in result["message"]
    profile = result["module_data"]["wb_plot_director"]["profile"]
    assert any(e["text"] == "sea voyages" and e["weight"] == "high" for e in profile["likes"])
    assert profile["dislikes"] == []


def test_profile_dislike_add_promotes_matching_avoid():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = backend._default_profile()
    data["profile"]["avoids"] = [
        {"text": "courtly politics", "weight": "medium", "evidence": "walked out twice"},
    ]

    result = asyncio.run(backend.on_command_plot(
        ["profile", "dislikes", "add", "courtly", "politics"], _state(data=data), _make_sdk([], {})))
    profile = result["module_data"]["wb_plot_director"]["profile"]

    assert any(e["text"] == "courtly politics" for e in profile["dislikes"])
    assert profile["avoids"] == []  # the observation graduated to a player veto


def test_profile_edit_deep_fields():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = backend._default_profile()
    data["profile"]["avoids"] = [
        {"text": "courtly politics", "weight": "medium", "evidence": "walked out twice"},
    ]

    def run(args, current=data):
        return asyncio.run(backend.on_command_plot(
            ["profile"] + args, _state(data=current), _make_sdk([], {})))

    # Avoids are AI-territory: the player can only veto them.
    result = run(["avoids", "remove", "courtly politics"])
    assert "Removed" in result["message"]
    assert result["module_data"]["wb_plot_director"]["profile"]["avoids"] == []
    assert "not in avoids" in run(["avoids", "remove", "nothing here"])["message"]
    assert "Usage" in run(["avoids", "add", "something"])["message"]

    # Narrative preferences and notes are direct setters.
    result = run(["pacing", "slow", "burn"])
    assert result["module_data"]["wb_plot_director"]["profile"]["narrative"]["pacing"] == "slow burn"
    result = run(["register", "dread", "and", "wonder"])
    assert result["module_data"]["wb_plot_director"]["profile"]["narrative"]["register"] == "dread and wonder"
    result = run(["notes", "Loves", "a", "slow", "reveal."])
    assert result["module_data"]["wb_plot_director"]["profile"]["notes"] == "Loves a slow reveal."
    result = run(["notes"])
    assert result["module_data"]["wb_plot_director"]["profile"]["notes"] == ""


def test_plot_profile_edit_lists_and_tone():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([], captured)
    data = _active_data(backend)
    data["profile"] = _profile_reply()

    def run(args, current=data):
        return asyncio.run(backend.on_command_plot(["profile"] + args, _state(data=current), sdk))

    # Add (multi-word text joined from args, default medium weight) and
    # remove (case-insensitive). String entries normalize to {text, weight}.
    result = run(["likes", "add", "sea", "voyages"])
    assert 'Added "sea voyages" to likes (medium)' in result["message"]
    profile = result["module_data"]["wb_plot_director"]["profile"]
    assert any(e["text"] == "sea voyages" and e["weight"] == "medium" for e in profile["likes"])

    result = run(["likes", "remove", "HAGGLING"])
    assert "Removed" in result["message"]
    assert all(e["text"] != "haggling" for e in result["module_data"]["wb_plot_director"]["profile"]["likes"])

    # Explicit weight on add; re-adding with a new weight updates in place.
    result = run(["dislikes", "add", "high", "dungeon", "crawls"])
    assert {"text": "dungeon crawls", "weight": "high"} in result["module_data"]["wb_plot_director"]["profile"]["dislikes"]

    weighted = _active_data(backend)
    weighted["profile"] = _profile_reply(likes=[{"text": "haggling", "weight": "medium"}])
    result = run(["likes", "add", "high", "haggling"], current=weighted)
    assert 'set to high' in result["message"]
    likes = result["module_data"]["wb_plot_director"]["profile"]["likes"]
    assert len(likes) == 1
    assert likes[0]["text"] == "haggling" and likes[0]["weight"] == "high"

    result = run(["themes", "remove", "smuggling"])
    assert result["module_data"]["wb_plot_director"]["profile"]["themes"] == ["harbor politics"]

    result = run(["tone", "cozy", "and", "warm"])
    assert result["module_data"]["wb_plot_director"]["profile"]["tone"] == "cozy and warm"

    # Failed edits write nothing back; bad grammar shows usage. No LLM calls ever.
    assert "module_data" not in run(["likes", "remove", "not there"])
    assert "already in" in run(["likes", "add", "back-alley", "deals"])["message"]
    assert "Usage" in run(["playstyle", "add", "x"])["message"]
    assert captured == {}

    # Adding past the cap evicts the lowest-weight entry, not the oldest.
    full = _active_data(backend)
    full["profile"] = _profile_reply(likes=(
        [{"text": "old but loved", "weight": "high"}]
        + [{"text": f"like {i}", "weight": "medium"} for i in range(backend.PROFILE_LIST_CAP - 2)]
        + [{"text": "meh", "weight": "low"}]
    ))
    likes = run(["likes", "add", "newest"], current=full)["module_data"]["wb_plot_director"]["profile"]["likes"]
    assert len(likes) == backend.PROFILE_LIST_CAP
    texts = [e["text"] for e in likes]
    assert "meh" not in texts          # low-weight entry evicted first
    assert "old but loved" in texts    # high-weight entry survives despite age
    assert "newest" in texts


def test_plot_command_shows_thread_openly():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = _profile_reply()
    data["direction"]["premise"] = "A quiet war for the harbor's soul."
    data["thread_history"] = [
        {"title": "The Pilgrim Road", "outcome": "abandoned", "created_turn": 2, "closed_turn": 9, "note": ""},
    ]

    result = asyncio.run(backend.on_command_plot([], _state(data=data), _make_sdk([], {})))
    message = result["message"]
    assert "The Salt Baron's Ledger" in message
    assert "stolen ledger" in message
    assert "steady" in message
    assert "intrigue" in message
    assert "The Pilgrim Road" in message
    assert result["signal"] == "end_turn"

    # The challenge and the story direction are spoiler-hidden by default and
    # revealed only on request.
    assert "enforcers" not in message
    assert "/plot challenge" in message
    assert "A quiet war for the harbor's soul." not in message
    assert "/plot direction" in message

    revealed = asyncio.run(backend.on_command_plot(["challenge"], _state(data=data), _make_sdk([], {})))
    assert "enforcers" in revealed["message"]
    assert "spoiler" in revealed["message"].lower()

    data["direction"]["heading"] = "The Baron plans a reprisal."
    data["direction"]["open_questions"] = ["Who tipped off the enforcers?"]
    arc = asyncio.run(backend.on_command_plot(["direction"], _state(data=data), _make_sdk([], {})))
    assert "A quiet war for the harbor's soul." in arc["message"]
    assert "The Baron plans a reprisal." in arc["message"]
    assert "Who tipped off the enforcers?" in arc["message"]

    no_thread = asyncio.run(backend.on_command_plot(
        ["challenge"], _state(data=backend._default_data()), _make_sdk([], {})))
    assert "No active thread challenge" in no_thread["message"]
    no_arc = asyncio.run(backend.on_command_plot(
        ["direction"], _state(data=backend._default_data()), _make_sdk([], {})))
    assert "No story direction yet" in no_arc["message"]

    observing = asyncio.run(backend.on_command_plot(
        [], _state(data=backend._default_data()), _make_sdk([], {})))
    assert "Watching how you play" in observing["message"]

    disabled = asyncio.run(backend.on_command_plot(
        [], _state(data=data, config={"plot_enabled": False}), _make_sdk([], {})))
    assert "inactive" in disabled["message"]


def test_plot_command_shows_breathing_state():
    backend = _load_backend()
    data = backend._default_data()
    data["status"] = "active"
    data["thread"] = backend._full_thread(status="resolved", closed_turn=5, created_turn=1)
    data["next_thread_turn"] = 8

    breathing = asyncio.run(backend.on_command_plot([], _state(turn=6, data=data), _make_sdk([], {})))
    assert "letting the story breathe" in breathing["message"]
    assert "turn 8" in breathing["message"]

    ready = asyncio.run(backend.on_command_plot([], _state(turn=9, data=data), _make_sdk([], {})))
    assert "being woven" in ready["message"]


def test_plot_reset_requires_confirm():
    backend = _load_backend()
    captured = {}
    result = asyncio.run(backend.on_command_plot(
        ["reset"], _state(data=_active_data(backend)), _make_sdk([], captured)))
    assert "reset confirm" in result["message"]
    assert "module_data" not in result
    assert captured == {}  # no LLM calls until confirmed


def test_plot_reset_rebuilds_profile_direction_and_thread():
    backend = _load_backend()
    data = _active_data(backend, suspended=True, suspended_turn=3)
    data["profile"] = backend._default_profile()
    data["profile"]["dislikes"] = [{"text": "body horror", "weight": "high"}]
    data["thread_history"] = [{"title": "Old Thread", "outcome": "resolved",
                               "created_turn": 1, "closed_turn": 2, "note": "", "consequence": "x"}]
    captured = {}
    sdk = _make_sdk([json.dumps(_profile_reply()), DIRECTION_REPLY,
                     THREAD_REPLY, CRITIC_ACCEPT], captured)

    result = asyncio.run(backend.on_command_plot(
        ["reset", "confirm"], _state(turn=6, data=data), sdk))

    # Bootstrap analysis, direction seed, generation, fit check -- in order.
    assert captured["preferences"] == ["smartest", "balanced", "smartest", "balanced"]
    assert "already in progress" in captured["prompts"][0]
    update = result["module_data"]["wb_plot_director"]
    # Every top-level key is replaced wholesale, so old data is truly gone.
    assert set(result["module_data_replace"]) == set(update)
    assert update["thread_history"] == []
    assert update["log"] == []
    assert update["profile"]["dislikes"] == []  # complete wipe, dislikes included
    assert any(e["text"] == "back-alley deals" for e in update["profile"]["likes"])
    assert update["profile"]["attachments"][0]["name"] == "The Salt Baron"
    assert update["direction"]["premise"].startswith("A quiet war")
    assert update["thread"]["title"] == "The Salt Baron's Ledger"
    assert update["thread"]["created_turn"] == 6
    assert update["suspended"] is False  # a reset unfreezes
    assert "Reset complete" in result["message"]
    assert "The Salt Baron's Ledger" in result["message"]


def test_plot_reset_fresh_story_skips_bootstrap_and_survives_failures():
    backend = _load_backend()

    # Turn 1: nothing to analyze -- seed + generation + critic only.
    captured = {}
    sdk = _make_sdk([DIRECTION_REPLY, THREAD_REPLY, CRITIC_ACCEPT], captured)
    result = asyncio.run(backend.on_command_plot(
        ["reset", "confirm"], _state(turn=1, data=backend._default_data()), sdk))
    assert len(captured["prompts"]) == 3
    assert all("already in progress" not in p for p in captured["prompts"])
    update = result["module_data"]["wb_plot_director"]
    assert update["thread"]["title"] == "The Salt Baron's Ledger"
    assert "story too young" in result["message"]

    # Every rebuild step failing still resets cleanly; the librarian machinery
    # retries direction seeding and generation on later turns.
    result = asyncio.run(backend.on_command_plot(
        ["reset", "confirm"], _state(turn=6, data=_active_data(backend)),
        _make_sdk(["garbage", "garbage", "garbage"], {})))
    update = result["module_data"]["wb_plot_director"]
    assert update["thread"]["status"] == "none"
    assert update["direction"]["premise"] == ""
    assert update["direction_seed_attempts"] == 0  # librarian will re-seed
    assert update["gen_attempts"] == 1
    assert "retries next turn" in result["message"]


def test_player_pivot_supersedes_thread_and_meets_player():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([
        _assessment_reply(thread_engaged=False, player_pivot=True,
                          pivot_intent="The player has set sail north to hunt the ice reavers."),
        DIRECTION_REPLY,
        SECOND_THREAD_REPLY,
        CRITIC_ACCEPT,
    ], captured)
    data = _active_data(backend, created_turn=1)

    result = asyncio.run(backend.on_librarian(_state(turn=5, data=data), sdk))
    update = _updates(result)

    # The assessment is asked to spot committed pivots...
    assert "player_pivot" in captured["prompts"][0]
    # ...and a detected one supersedes the thread and replaces it in the SAME
    # pass, despite the default quiet period -- the plot meets the player.
    assert update["thread_history"][-1]["outcome"] == "superseded"
    assert update["thread_history"][-1]["note"] == "The player has set sail north to hunt the ice reavers."
    assert update["thread"]["title"] == "Tremors Below"
    assert update["thread"]["status"] == "active"
    assert update["last_pivot_turn"] == 5
    assert update["next_thread_turn"] == 5
    generation_prompt = captured["prompts"][2]
    assert "CHANGED COURSE" in generation_prompt
    assert "set sail north" in generation_prompt
    assert '"defer"' not in generation_prompt  # the player is moving NOW


def test_pivot_needs_commitment_toggle_and_thrash_guard():
    backend = _load_backend()

    def run(reply_kwargs, config=None, last_pivot_turn=0):
        data = _active_data(backend, created_turn=1, last_pivot_turn=last_pivot_turn)
        captured = {}
        sdk = _make_sdk([_assessment_reply(**reply_kwargs)], captured)
        result = asyncio.run(backend.on_librarian(
            _state(turn=5, data=data, config=config), sdk))
        return _updates(result), captured

    pivot_kwargs = {"thread_engaged": False, "player_pivot": True,
                    "pivot_intent": "Sailing north."}

    # Still engaging with the thread: a claimed pivot changes nothing.
    update, captured = run({**pivot_kwargs, "thread_engaged": True})
    assert "thread_history" not in update
    assert len(captured["prompts"]) == 1

    # Toggle off: the pivot falls through to the normal ignore-streak path.
    update, captured = run(pivot_kwargs, config={"pivot_adapt": False})
    assert "thread_history" not in update
    assert update["ignored_streak"] == 1
    assert len(captured["prompts"]) == 1

    # Thrash guard: a pivot within PIVOT_COOLDOWN_TURNS of the last one waits.
    update, captured = run(pivot_kwargs, last_pivot_turn=3)
    assert "thread_history" not in update
    assert len(captured["prompts"]) == 1

    # No stated intent, no pivot -- there is nothing to meet the player with.
    update, captured = run({**pivot_kwargs, "pivot_intent": ""})
    assert "thread_history" not in update
    assert len(captured["prompts"]) == 1
