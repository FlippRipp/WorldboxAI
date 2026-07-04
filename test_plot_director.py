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


OUTLINE_REPLY = json.dumps({
    "premise": "A buried king stirs beneath the city.",
    "driving_tension": "Power calls to the unworthy.",
    "acts": [
        {"title": "Whispers", "goal": "Surface the threat", "beats": [
            {"description": "Rumors of tremors under the market"},
            {"description": "A collapsed cellar reveals old stonework"},
        ]},
        {"title": "Descent", "goal": "Confront the depths", "beats": [
            {"description": "A guide offers passage below"},
            {"description": "The buried throne room is found"},
        ]},
        {"title": "Crown", "goal": "Resolve the king's claim", "beats": [
            {"description": "The king demands fealty"},
        ]},
    ],
})


def _assessment_reply(**overrides):
    reply = {
        "beat_advanced": False,
        "beat_completed": False,
        "beat_summary": "",
        "drift_detected": False,
        "drift_note": "",
        "opportunity": False,
        "nudge": "A beggar mutters about the ground humming at night.",
        "momentum": "steady",
    }
    reply.update(overrides)
    return json.dumps(reply)


def _ready_data(backend, **overrides):
    data = backend._default_data()
    parsed = backend._normalize_outline(json.loads(OUTLINE_REPLY))
    data.update(parsed)
    data["status"] = "ready"
    data["outline_created_turn"] = 1
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


def test_gather_context_seeds_skeleton_once():
    backend = _load_backend()
    sdk = _make_sdk([], {})

    first = asyncio.run(backend.on_gather_context(_state(data=None), sdk))
    seeded = first["module_data"]["wb_plot_director"]
    assert seeded["status"] == "pending"
    assert seeded["position"] == {"act": 1, "beat_index": 0}

    second = asyncio.run(backend.on_gather_context(_state(data=seeded), sdk))
    assert second == {}


def test_first_librarian_generates_outline():
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk([OUTLINE_REPLY], captured)
    state = _state(
        turn=1,
        data=backend._default_data(),
        scenario_data={"scenario_description": "A merchant city above forgotten ruins."},
    )

    result = asyncio.run(backend.on_librarian(state, sdk))
    data = result["module_data"]["wb_plot_director"]

    assert "forgotten ruins" in captured["prompts"][0]
    assert captured["preferences"] == ["smartest"]
    assert data["status"] == "ready"
    beats = data["outline"]["acts"][0]["beats"]
    assert beats[0] == {"id": "a1b1", "description": "Rumors of tremors under the market", "status": "active"}
    assert beats[1]["status"] == "pending"
    assert data["position"] == {"act": 1, "beat_index": 0}


def test_outline_works_without_scenario_and_fails_dormant():
    backend = _load_backend()

    # Freeform story: no scenario_data, outline distilled from history alone.
    captured = {}
    sdk = _make_sdk([OUTLINE_REPLY], captured)
    result = asyncio.run(backend.on_librarian(
        _state(turn=1, data=backend._default_data(), history=["An unmarked road at dusk."]), sdk))
    assert result["module_data"]["wb_plot_director"]["status"] == "ready"
    assert "unmarked road" in captured["prompts"][0]

    # Malformed replies: retries, then goes dormant after the attempt cap.
    data = backend._default_data()
    for attempt in range(1, backend.OUTLINE_MAX_ATTEMPTS + 1):
        sdk = _make_sdk(["not json"], {})
        result = asyncio.run(backend.on_librarian(_state(turn=1, data=data), sdk))
        update = result["module_data"]["wb_plot_director"]
        data.update(update)
        assert update["outline_attempts"] == attempt
    assert data["status"] == "failed"
    assert asyncio.run(backend.on_librarian(_state(turn=2, data=data), sdk)) is None


def test_stall_counting_arms_nudge_at_threshold():
    backend = _load_backend()
    data = _ready_data(backend)

    for turn in (2, 3, 4):
        sdk = _make_sdk([_assessment_reply(momentum="stalled")], {})
        result = asyncio.run(backend.on_librarian(_state(turn=turn, data=data), sdk))
        data.update(result["module_data"]["wb_plot_director"])

    # Default threshold 3: third no-progress check fires and resets the counter.
    assert data["pending_nudge"] == "A beggar mutters about the ground humming at night."
    assert data["stall_count"] == 0
    assert data["last_nudge_turn"] == 4


def test_advancement_resets_stall_and_completion_moves_position():
    backend = _load_backend()
    data = _ready_data(backend, stall_count=2)

    sdk = _make_sdk([_assessment_reply(beat_advanced=True)], {})
    result = asyncio.run(backend.on_librarian(_state(turn=2, data=data), sdk))
    update = result["module_data"]["wb_plot_director"]
    assert update["stall_count"] == 0
    assert update["position"] == {"act": 1, "beat_index": 0}

    # Completing the last beat of act 1 crosses the act boundary.
    data = _ready_data(backend, position={"act": 1, "beat_index": 1})
    data["outline"]["acts"][0]["beats"][0]["status"] = "done"
    sdk = _make_sdk([_assessment_reply(beat_advanced=True, beat_completed=True,
                                       beat_summary="The cellar gave way.")], {})
    result = asyncio.run(backend.on_librarian(_state(turn=4, data=data), sdk))
    update = result["module_data"]["wb_plot_director"]
    assert update["position"] == {"act": 2, "beat_index": 0}
    assert update["beats_completed"][-1]["summary"] == "The cellar gave way."
    assert update["outline"]["acts"][0]["beats"][1]["status"] == "done"
    assert update["outline"]["acts"][1]["beats"][0]["status"] == "active"


def test_render_block_only_fires_when_armed_and_is_cleared_next_turn():
    backend = _load_backend()
    sdk = _make_sdk([], {})
    block = {"id": "plot_nudge"}

    quiet = asyncio.run(backend.on_render_prompt_block(
        block, _state(data=_ready_data(backend)), sdk))
    assert quiet["content"] == ""

    armed = _ready_data(backend, pending_nudge="A stranger asks about the tremors.", last_nudge_turn=3)
    rendered = asyncio.run(backend.on_render_prompt_block(block, _state(data=armed), sdk))
    assert "A stranger asks about the tremors." in rendered["content"]
    assert "Optional" in rendered["content"]

    # The next librarian pass consumes the nudge even when the assessment is
    # frequency-gated; _deep_merge cannot delete keys, so it must return "".
    result = asyncio.run(backend.on_librarian(
        _state(turn=4, data=armed, config={"assessment_frequency": 5}), sdk))
    assert result["module_data"]["wb_plot_director"]["pending_nudge"] == ""


def test_drift_needs_two_flags_and_replan_is_rate_bounded():
    backend = _load_backend()
    replan_reply = json.dumps({
        "premise": "A bakery becomes the heart of a haunted quarter.",
        "driving_tension": "Comfort against the uncanny.",
        "acts": [
            {"title": "Whispers", "goal": "Surface the threat", "beats": [
                {"description": "Rumors of tremors under the market", "status": "done"},
                {"description": "Strange customers at the new bakery"},
            ]},
            {"title": "Hearth", "goal": "Root the new life", "beats": [
                {"description": "The oven wakes something below"},
            ]},
            {"title": "Warmth", "goal": "Settle the quarter", "beats": [
                {"description": "The quarter chooses its guardian"},
            ]},
        ],
    })

    data = _ready_data(backend, beats_completed=[{"id": "a1b1", "turn": 3, "summary": "kept"}])

    # First drift flag: streak only, no re-plan LLM call.
    captured = {}
    sdk = _make_sdk([_assessment_reply(drift_detected=True, drift_note="Story turned cozy.")], captured)
    result = asyncio.run(backend.on_librarian(_state(turn=6, data=data), sdk))
    data.update(result["module_data"]["wb_plot_director"])
    assert data["drift_streak"] == 1
    assert len(captured["prompts"]) == 1

    # Second consecutive flag: re-plan call runs, outline follows the player.
    captured = {}
    sdk = _make_sdk([_assessment_reply(drift_detected=True, drift_note="Player opened a bakery."),
                     replan_reply], captured)
    result = asyncio.run(backend.on_librarian(_state(turn=7, data=data), sdk))
    data.update(result["module_data"]["wb_plot_director"])
    assert len(captured["prompts"]) == 2
    assert "bakery" in data["outline"]["premise"]
    assert data["beats_completed"] == [{"id": "a1b1", "turn": 3, "summary": "kept"}]
    assert data["outline"]["acts"][0]["beats"][0]["status"] == "done"
    assert data["drift_streak"] == 0
    assert data["last_replan_turn"] == 7
    assert data["replan_count"] == 1

    # Two more drift flags inside the rate window: no second re-plan call.
    for turn in (8, 9):
        captured = {}
        sdk = _make_sdk([_assessment_reply(drift_detected=True, drift_note="Still cozy.")], captured)
        result = asyncio.run(backend.on_librarian(_state(turn=turn, data=data), sdk))
        data.update(result["module_data"]["wb_plot_director"])
        assert len(captured["prompts"]) == 1
    assert data["replan_count"] == 1


def test_plot_command_is_spoiler_safe():
    backend = _load_backend()
    sdk = _make_sdk([], {})

    pending = asyncio.run(backend.on_command_plot(
        [], _state(data=backend._default_data()), sdk))
    assert pending["message"] == "[Plot] The story is still finding its shape."

    data = _ready_data(backend, position={"act": 2, "beat_index": 0}, momentum="steady",
                       replan_count=1, last_replan_turn=6)
    result = asyncio.run(backend.on_command_plot([], _state(turn=7, data=data), sdk))
    message = result["message"]
    assert "Act 2 of 3" in message
    assert "new direction" in message
    for act in data["outline"]["acts"]:
        assert act["goal"] not in message
        for beat in act["beats"]:
            assert beat["description"] not in message


def test_malformed_assessment_reply_is_a_noop():
    backend = _load_backend()
    data = _ready_data(backend, stall_count=1)
    sdk = _make_sdk(["[mock llm response for: ...]"], {})

    result = asyncio.run(backend.on_librarian(_state(turn=2, data=data), sdk))
    assert result is None
