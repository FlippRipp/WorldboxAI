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


def _active_data(backend, created_turn=1, **overrides):
    data = backend._default_data()
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


def test_gather_context_seeds_v2_skeleton_once():
    backend = _load_backend()
    sdk = _make_sdk([], {})

    first = asyncio.run(backend.on_gather_context(_state(data=None), sdk))
    seeded = _updates(first)
    assert seeded["schema"] == backend.SCHEMA_VERSION
    assert seeded["status"] == "observing"
    assert seeded["thread"]["status"] == "none"
    assert seeded["profile"]["playstyle"] == {k: 0 for k in backend.PLAYSTYLE_KEYS}
    assert seeded["profile"]["likes"] == []

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


def test_gather_context_emits_context_line_for_active_thread():
    backend = _load_backend()
    sdk = _make_sdk([], {})
    data = _active_data(backend)

    result = asyncio.run(backend.on_gather_context(_state(data=data), sdk))
    context = result["context_string"]
    assert "The Salt Baron's Ledger" in context
    assert "stolen ledger" in context
    assert "optional" in context.lower()

    observing = asyncio.run(backend.on_gather_context(_state(data=backend._default_data()), sdk))
    assert "context_string" not in observing

    disabled = asyncio.run(backend.on_gather_context(
        _state(data=data, config={"plot_enabled": False}), sdk))
    assert "context_string" not in disabled


def test_first_librarian_generates_thread_from_scenario():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY], captured)
    state = _state(
        turn=1,
        data=backend._default_data(),
        history=["You arrive at the forgotten ruins."],
        scenario_data={"scenario_description": "An expedition to the forgotten ruins."},
    )

    result = asyncio.run(backend.on_librarian(state, sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 1
    assert "forgotten ruins" in captured["prompts"][0]
    assert captured["preferences"] == ["smartest"]
    assert update["status"] == "active"
    assert update["momentum"] == "building"
    assert update["gen_attempts"] == 0
    thread = update["thread"]
    assert set(thread) == set(backend._empty_thread())
    assert thread["status"] == "active"
    assert thread["title"] == "The Salt Baron's Ledger"
    assert thread["created_turn"] == 1


def test_generation_prompt_includes_profile_difficulty_and_npc_threads():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY], captured)
    data = backend._default_data()
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

    # Soft-fail: no npc module data at all -> generation still runs, block omitted.
    captured2 = {}
    sdk2 = _make_sdk([THREAD_REPLY], captured2)
    result = asyncio.run(backend.on_librarian(_state(turn=1, data=backend._default_data()), sdk2))
    assert _updates(result)["status"] == "active"
    assert "OTHER ACTIVE STORYLINES" not in captured2["prompts"][0]


def test_adoption_midstory_bootstraps_profile_before_first_thread():
    backend = _load_backend()
    captured = {}
    bootstrap_reply = json.dumps(_profile_reply())
    sdk = _make_sdk([bootstrap_reply, THREAD_REPLY], captured)
    state = _state(
        turn=12,
        data=backend._default_data(),
        history=["The intro.", "You duel the harbor watch.", "You bribe the customs clerk."],
    )
    state["chat_messages"] = [
        {"role": "user", "content": "I challenge the watch captain."},
        {"role": "ai", "content": "Steel rings."},
        {"role": "user", "content": "I slip the clerk a purse."},
    ]

    result = asyncio.run(backend.on_librarian(state, sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 2
    assert captured["preferences"] == ["smartest", "smartest"]
    bootstrap_prompt = captured["prompts"][0]
    assert "already in progress" in bootstrap_prompt
    assert "bribe the customs clerk" in bootstrap_prompt
    assert "I slip the clerk a purse." in bootstrap_prompt
    # The thread generation that follows sees the bootstrapped profile.
    assert "back-alley deals" in captured["prompts"][1]
    assert update["profile"]["likes"] == ["back-alley deals", "haggling"]
    assert update["status"] == "active"
    assert update["thread"]["title"] == "The Salt Baron's Ledger"


def test_adoption_bootstrap_failure_still_generates_thread():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk(["not json", THREAD_REPLY], captured)

    result = asyncio.run(backend.on_librarian(
        _state(turn=12, data=backend._default_data()), sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 2
    assert "profile" not in update  # starting blind, no profile written
    assert update["status"] == "active"
    assert update["thread"]["title"] == "The Salt Baron's Ledger"


def test_fresh_story_does_not_bootstrap():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([THREAD_REPLY], captured)

    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=backend._default_data()), sdk))

    assert len(captured["prompts"]) == 1  # thread generation only
    assert "already in progress" not in captured["prompts"][0]
    assert _updates(result)["status"] == "active"


def test_generation_failure_retries_then_dormant():
    backend = _load_backend()
    data = backend._default_data()

    for attempt in range(1, backend.GEN_MAX_ATTEMPTS + 1):
        sdk = _make_sdk(["not json"], {})
        result = asyncio.run(backend.on_librarian(_state(turn=1, data=data), sdk))
        data.update(_updates(result))
        assert data["gen_attempts"] == attempt

    assert data["status"] == "failed"
    assert asyncio.run(backend.on_librarian(_state(turn=2, data=data), _make_sdk([], {}))) is None

    command = asyncio.run(backend.on_command_plot([], _state(data=data), _make_sdk([], {})))
    assert "inactive" in command["message"]
    gathered = asyncio.run(backend.on_gather_context(_state(data=data), _make_sdk([], {})))
    assert "context_string" not in gathered


def test_assessment_updates_profile_and_resets_streak():
    backend = _load_backend()
    captured = {}
    wild_profile = _profile_reply(
        playstyle={"combat": 99, "diplomacy": 4, "exploration": 2,
                   "mystery": 3, "social": 5, "intrigue": 6},
        likes=[f"like {i}" for i in range(12)],
    )
    sdk = _make_sdk([_assessment_reply(profile=wild_profile)], captured)
    data = _active_data(backend, ignored_streak=2)

    result = asyncio.run(backend.on_librarian(_state(turn=3, data=data), sdk))
    update = _updates(result)

    prompt = captured["prompts"][0]
    assert "The Salt Baron's Ledger" in prompt
    assert "I browse the stalls." in prompt
    assert json.dumps(data["profile"], ensure_ascii=False) in prompt
    assert captured["preferences"] == ["balanced"]

    assert update["ignored_streak"] == 0
    assert update["profile"]["playstyle"]["combat"] == 10
    assert len(update["profile"]["likes"]) == backend.PROFILE_LIST_CAP
    assert update["momentum"] == "steady"
    assert update["log"][-1]["turn"] == 3


def test_resolution_regenerates_immediately_with_full_replacement():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([
        _assessment_reply(thread_resolved=True, resolution_note="The ledger reached the magistrate."),
        SECOND_THREAD_REPLY,
    ], captured)
    data = _active_data(backend, ignored_streak=1)

    result = asyncio.run(backend.on_librarian(_state(turn=5, data=data), sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 2
    closed = update["thread_history"][-1]
    assert closed["outcome"] == "resolved"
    assert closed["title"] == "The Salt Baron's Ledger"
    assert closed["closed_turn"] == 5

    # Full-key overwrite contract: the new thread replaces every field.
    thread = update["thread"]
    assert set(thread) == set(backend._empty_thread())
    assert thread["title"] == "Tremors Below"
    assert thread["stakes"] == "The market quarter's foundations."
    assert thread["closed_turn"] == 0
    assert thread["created_turn"] == 5
    assert update["momentum"] == "building"
    assert update["pending_nudge"] == ""


def test_ignored_streak_nudges_then_abandons_and_records_dislike():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1)
    config = {}  # defaults: stall_threshold 2, abandon_after 4
    armed_turn = None

    for turn in range(2, 6):
        replies = [_assessment_reply(thread_engaged=False)]
        if turn == 5:
            replies.append(SECOND_THREAD_REPLY)
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
        if turn == 5:  # streak 4 = abandon_after -> abandoned + regeneration
            assert len(captured["prompts"]) == 2
            assert update["thread_history"][-1]["outcome"] == "abandoned"
            assert any(
                "Salt Baron" in dislike for dislike in update["profile"]["dislikes"]
            )
            assert "DIFFERENT kind" in captured["prompts"][1]
            assert '"The Salt Baron\'s Ledger" -- abandoned' in captured["prompts"][1]
            assert update["thread"]["title"] == "Tremors Below"

        data.update(update)

    assert armed_turn == 3


def test_expiry_by_turn_cap_skips_assessment():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([SECOND_THREAD_REPLY], captured)
    data = _active_data(backend, created_turn=1)

    result = asyncio.run(backend.on_librarian(_state(turn=13, data=data), sdk))
    update = _updates(result)

    assert len(captured["prompts"]) == 1  # generation only, no assessment spent
    assert update["thread_history"][-1]["outcome"] == "expired"
    assert update["thread"]["title"] == "Tremors Below"


def test_nudge_consume_and_render_block_contract():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1, pending_nudge="A rumor spreads.", last_nudge_turn=3)

    block = {"id": "plot_nudge"}
    rendered = asyncio.run(backend.on_render_prompt_block(block, _state(data=data), _make_sdk([], {})))
    assert "A rumor spreads." in rendered["content"]
    assert "Optional" in rendered["content"]

    # Frequency-gated turn still consumes the already-rendered nudge; _deep_merge
    # cannot delete keys, so it must return an explicit "".
    result = asyncio.run(backend.on_librarian(
        _state(turn=4, data=data, config={"assessment_frequency": 5}), _make_sdk([], {})))
    assert _updates(result)["pending_nudge"] == ""

    quiet = asyncio.run(backend.on_render_prompt_block(
        block, _state(data=_active_data(backend)), _make_sdk([], {})))
    assert quiet["content"] == ""


def test_opportunity_nudge_respects_toggle_and_cooldown():
    backend = _load_backend()

    def run(config, last_nudge_turn=0):
        data = _active_data(backend, created_turn=1, last_nudge_turn=last_nudge_turn)
        sdk = _make_sdk([_assessment_reply(opportunity=True)], {})
        result = asyncio.run(backend.on_librarian(_state(turn=4, data=data, config=config), sdk))
        return _updates(result).get("pending_nudge", "")

    assert run({}) != ""
    assert run({"opportunity_nudges": False}) == ""
    assert run({}, last_nudge_turn=3) == ""  # within the 2-turn cooldown


def test_malformed_assessment_reply_is_noop():
    backend = _load_backend()
    data = _active_data(backend, created_turn=1, ignored_streak=1)

    result = asyncio.run(backend.on_librarian(
        _state(turn=4, data=data), _make_sdk(["[mock llm response for: ...]"], {})))
    assert result is None


def test_plot_regen_replaces_thread_and_writes_back():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([SECOND_THREAD_REPLY], captured)
    data = _active_data(backend, created_turn=1)

    result = asyncio.run(backend.on_command_plot(["regen"], _state(turn=6, data=data), sdk))

    assert "Tremors Below" in result["message"]
    update = result["module_data"]["wb_plot_director"]
    assert update["thread_history"][-1]["outcome"] == "rerolled"
    assert update["thread_history"][-1]["title"] == "The Salt Baron's Ledger"
    assert update["thread"]["title"] == "Tremors Below"
    assert update["thread"]["status"] == "active"
    # The old thread appears in the generation prompt's do-not-repeat list.
    assert '"The Salt Baron\'s Ledger" -- rerolled' in captured["prompts"][0]
    # A player reroll records no dislike.
    assert "profile" not in update


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
        ["regen"], _state(turn=6, data=failed), _make_sdk([THREAD_REPLY], {})))
    update = result["module_data"]["wb_plot_director"]
    assert update["status"] == "active"
    assert update["thread"]["status"] == "active"


def test_plot_profile_edit_lists_and_tone():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([], captured)
    data = _active_data(backend)
    data["profile"] = _profile_reply()

    def run(args, current=data):
        return asyncio.run(backend.on_command_plot(["profile"] + args, _state(data=current), sdk))

    # Add (multi-word text joined from args) and remove (case-insensitive).
    result = run(["likes", "add", "sea", "voyages"])
    assert 'Added "sea voyages"' in result["message"]
    profile = result["module_data"]["wb_plot_director"]["profile"]
    assert "sea voyages" in profile["likes"]

    result = run(["likes", "remove", "HAGGLING"])
    assert "Removed" in result["message"]
    assert "haggling" not in result["module_data"]["wb_plot_director"]["profile"]["likes"]

    result = run(["dislikes", "add", "dungeon", "crawls"])
    assert "dungeon crawls" in result["module_data"]["wb_plot_director"]["profile"]["dislikes"]

    result = run(["themes", "remove", "smuggling"])
    assert result["module_data"]["wb_plot_director"]["profile"]["themes"] == ["harbor politics"]

    result = run(["tone", "cozy", "and", "warm"])
    assert result["module_data"]["wb_plot_director"]["profile"]["tone"] == "cozy and warm"

    # Failed edits write nothing back; bad grammar shows usage. No LLM calls ever.
    assert "module_data" not in run(["likes", "remove", "not there"])
    assert "already in" in run(["likes", "add", "back-alley", "deals"])["message"]
    assert "Usage" in run(["playstyle", "add", "x"])["message"]
    assert "captured" == "captured" and captured == {}

    # Adding past the cap drops the oldest entry.
    full = _active_data(backend)
    full["profile"] = _profile_reply(likes=[f"like {i}" for i in range(backend.PROFILE_LIST_CAP)])
    likes = run(["likes", "add", "newest"], current=full)["module_data"]["wb_plot_director"]["profile"]["likes"]
    assert len(likes) == backend.PROFILE_LIST_CAP
    assert likes[-1] == "newest"
    assert "like 0" not in likes


def test_plot_command_shows_thread_openly():
    backend = _load_backend()
    data = _active_data(backend)
    data["profile"] = _profile_reply()
    data["thread_history"] = [
        {"title": "The Pilgrim Road", "outcome": "abandoned", "created_turn": 2, "closed_turn": 9, "note": ""},
    ]

    result = asyncio.run(backend.on_command_plot([], _state(data=data), _make_sdk([], {})))
    message = result["message"]
    assert "The Salt Baron's Ledger" in message
    assert "stolen ledger" in message
    assert "enforcers" in message
    assert "steady" in message
    assert "intrigue" in message
    assert "The Pilgrim Road" in message
    assert result["signal"] == "end_turn"

    observing = asyncio.run(backend.on_command_plot(
        [], _state(data=backend._default_data()), _make_sdk([], {})))
    assert "Watching how you play" in observing["message"]

    disabled = asyncio.run(backend.on_command_plot(
        [], _state(data=data, config={"plot_enabled": False}), _make_sdk([], {})))
    assert "inactive" in disabled["message"]
