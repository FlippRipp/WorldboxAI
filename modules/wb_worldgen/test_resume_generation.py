"""Interrupted-generation resilience: one-shot runs auto-save drafts per step,
/api/world/continue picks an interrupted run back up without redoing finished
steps, drafts round-trip skip_review / completion through disk metadata, and
saving a world cleans up its differently-named draft. This is what makes the
world generator survive Android killing the app (or the whole Termux backend)
while minimized.
"""

import asyncio

import pytest
from fastapi import HTTPException

import routes as world_routes
from wbworldgen.worldgen.persistence import WorldPersistence


class FakeBuilder:
    def __init__(self, ordered_ids=("world_rules", "lore", "map_generation")):
        self._ordered_ids = list(ordered_ids)
        self._steps = {sid: object() for sid in self._ordered_ids}
        self.generated = []
        self.draft_saves = []

    async def generate_step(self, step_id, state, prompt, user_note="", config=None):
        self.generated.append(step_id)
        await asyncio.sleep(0)
        return {"step": step_id}

    def save_draft(self, world_id, state):
        self.draft_saves.append(
            {"world_id": world_id, "steps": sorted(state.get("steps", {})),
             "complete": bool(state.get("complete"))})
        return world_id or "draft1"


@pytest.fixture
def fake_builder():
    fake = FakeBuilder()
    old = world_routes.world_builder
    world_routes.world_builder = fake
    yield fake
    world_routes.world_builder = old
    for sid in list(world_routes.world_gen_sessions):
        if sid.startswith("resume_test"):
            world_routes.world_gen_sessions.pop(sid, None)
            world_routes.world_draft_ids.pop(sid, None)


def test_one_shot_auto_saves_draft_after_every_step(fake_builder):
    resp = asyncio.run(world_routes.generate_world(
        world_routes.WorldGenerateRequest(seed_prompt="p", skip_review=True),
        session_id="resume_test_saves"))

    assert resp["complete"] is True
    # One eager save before the run, one per generated step, plus the final
    # complete-marking save.
    assert [s["steps"] for s in fake_builder.draft_saves] == [
        [],
        ["world_rules"],
        ["lore", "world_rules"],
        ["lore", "map_generation", "world_rules"],
        ["lore", "map_generation", "world_rules"],
    ]
    assert fake_builder.draft_saves[-1]["complete"] is True
    assert all(not s["complete"] for s in fake_builder.draft_saves[:-1])


def test_one_shot_society_factions_sees_natural_landmarks_data():
    # society_factions' region field must reference the areas natural_landmarks
    # authors, so one-shot mode must run them strictly in order — a former
    # optimization gathered them concurrently and every faction region name
    # missed the join.
    class OrderCheckingBuilder(FakeBuilder):
        def __init__(self):
            super().__init__(ordered_ids=("natural_landmarks", "society_factions"))
            self.landmarks_data_at_factions_call = None

        async def generate_step(self, step_id, state, prompt, user_note="", config=None):
            if step_id == "society_factions":
                self.landmarks_data_at_factions_call = bool(
                    state.get("steps", {}).get("natural_landmarks", {}).get("data"))
            return await super().generate_step(step_id, state, prompt, user_note, config)

    fake = OrderCheckingBuilder()
    old = world_routes.world_builder
    world_routes.world_builder = fake
    try:
        asyncio.run(world_routes.generate_world(
            world_routes.WorldGenerateRequest(seed_prompt="p", skip_review=True),
            session_id="resume_test_order"))
    finally:
        world_routes.world_builder = old
        world_routes.world_gen_sessions.pop("resume_test_order", None)
        world_routes.world_draft_ids.pop("resume_test_order", None)

    assert fake.generated == ["natural_landmarks", "society_factions"]
    assert fake.landmarks_data_at_factions_call is True


def test_continue_resumes_without_redoing_finished_steps(fake_builder):
    world_routes.world_gen_sessions["resume_test_cont"] = {
        "seed_prompt": "p",
        "skip_review": True,
        "steps": {"world_rules": {"data": {"step": "world_rules"}, "approved": True}},
    }

    resp = asyncio.run(world_routes.continue_world_generation(session_id="resume_test_cont"))

    assert fake_builder.generated == ["lore", "map_generation"]
    assert resp["complete"] is True
    assert resp["state"]["complete"] is True
    assert "_generating" not in resp["state"]
    assert set(resp["state"]["steps"]) == {"world_rules", "lore", "map_generation"}


def test_continue_is_a_noop_when_running_complete_or_review_mode(fake_builder):
    # Already running: don't start a second loop over the same session.
    world_routes.world_gen_sessions["resume_test_noop"] = {
        "seed_prompt": "p", "skip_review": True, "_generating": "all",
        "steps": {"world_rules": {"data": {"x": 1}, "approved": True}},
    }
    resp = asyncio.run(world_routes.continue_world_generation(session_id="resume_test_noop"))
    assert fake_builder.generated == []
    assert resp["state"]["_generating"] == "all"

    # Already complete: nothing left to do.
    world_routes.world_gen_sessions["resume_test_noop"] = {
        "seed_prompt": "p", "skip_review": True, "complete": True,
        "steps": {"world_rules": {"data": {"x": 1}, "approved": True}},
    }
    asyncio.run(world_routes.continue_world_generation(session_id="resume_test_noop"))
    assert fake_builder.generated == []

    # Review-mode sessions continue through approve-step, not this route.
    world_routes.world_gen_sessions["resume_test_noop"] = {
        "seed_prompt": "p",
        "steps": {"world_rules": {"data": {"x": 1}, "approved": False}},
    }
    asyncio.run(world_routes.continue_world_generation(session_id="resume_test_noop"))
    assert fake_builder.generated == []


def test_continue_404s_without_a_session(fake_builder):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(world_routes.continue_world_generation(session_id="resume_test_missing"))
    assert exc.value.status_code == 404


def test_review_mode_drafts_eagerly_and_after_first_step(fake_builder):
    asyncio.run(world_routes.generate_world(
        world_routes.WorldGenerateRequest(seed_prompt="p"),
        session_id="resume_test_first"))
    assert [s["steps"] for s in fake_builder.draft_saves] == [[], ["world_rules"]]


def test_continue_regenerates_interrupted_review_step(fake_builder):
    # Killed while generating "lore" (world_rules already approved): nothing
    # awaits review, so continue regenerates the first data-less step.
    world_routes.world_gen_sessions["resume_test_review"] = {
        "seed_prompt": "p",
        "current_step": "world_rules",
        "steps": {"world_rules": {"data": {"x": 1}, "approved": True}},
    }
    resp = asyncio.run(world_routes.continue_world_generation(session_id="resume_test_review"))
    assert fake_builder.generated == ["lore"]
    assert resp["current_step"] == "lore"
    assert resp["state"]["steps"]["lore"] == {"data": {"step": "lore"}, "approved": False}
    assert "complete" not in resp["state"] or not resp["state"]["complete"]


def test_continue_restarts_review_session_with_zero_steps(fake_builder):
    # Killed during the very first step: only the eagerly-drafted prompt
    # survived. Continue regenerates the first step from it.
    world_routes.world_gen_sessions["resume_test_zero"] = {"seed_prompt": "p", "steps": {}}
    resp = asyncio.run(world_routes.continue_world_generation(session_id="resume_test_zero"))
    assert fake_builder.generated == ["world_rules"]
    assert resp["current_step"] == "world_rules"


def test_discard_deletes_only_step_less_drafts(fake_builder):
    class DiscardBuilder(FakeBuilder):
        def __init__(self, steps):
            super().__init__()
            self.stored_steps = steps
            self.deleted = []

        def load_world(self, world_id):
            return {"steps": self.stored_steps}

        def delete_world(self, world_id):
            self.deleted.append(world_id)

    # Empty eager draft: deleted on discard.
    empty = DiscardBuilder({})
    world_routes.world_builder = empty
    world_routes.world_gen_sessions["resume_test_disc"] = {"seed_prompt": "p", "steps": {}}
    world_routes.world_draft_ids["resume_test_disc"] = "empty_draft"
    asyncio.run(world_routes.discard_world(session_id="resume_test_disc"))
    assert empty.deleted == ["empty_draft"]
    assert "resume_test_disc" not in world_routes.world_gen_sessions

    # Draft with generated steps: kept resumable.
    kept = DiscardBuilder({"world_rules": {"data": {"x": 1}}})
    world_routes.world_builder = kept
    world_routes.world_gen_sessions["resume_test_disc"] = {
        "seed_prompt": "p", "steps": {"world_rules": {"data": {"x": 1}, "approved": True}}}
    world_routes.world_draft_ids["resume_test_disc"] = "real_draft"
    asyncio.run(world_routes.discard_world(session_id="resume_test_disc"))
    assert kept.deleted == []


def test_draft_round_trips_skip_review_and_completion(tmp_path):
    persistence = WorldPersistence(worlds_dir=str(tmp_path / "worlds"),
                                   prompt_library_path=str(tmp_path / "prompts.json"))
    state = {
        "seed_prompt": "a fungal empire",
        "skip_review": True,
        "steps": {"world_rules": {"data": {"x": 1}, "approved": True}},
    }

    # Interrupted mid-run: resumes incomplete, still one-shot.
    wid = persistence.save_draft("", state)
    loaded = persistence.load_world(wid)
    assert loaded["skip_review"] is True
    assert loaded["complete"] is False

    # Finished but unsaved: resumes straight to the finished review.
    state["complete"] = True
    persistence.save_draft(wid, state)
    loaded = persistence.load_world(wid)
    assert loaded["skip_review"] is True
    assert loaded["complete"] is True

    # A final save clears the draft markers entirely.
    final = persistence.save_world(wid, {k: v for k, v in state.items() if k != "skip_review"})
    loaded = persistence.load_world(final)
    assert loaded["complete"] is True
    assert "skip_review" not in loaded


def test_save_world_cleans_up_orphaned_draft(fake_builder):
    class SavingBuilder(FakeBuilder):
        def __init__(self):
            super().__init__()
            self.deleted = []

        def save_world(self, world_id, state):
            return "final_world"

        def delete_world(self, world_id):
            self.deleted.append(world_id)

    saving = SavingBuilder()
    world_routes.world_builder = saving
    world_routes.world_gen_sessions["resume_test_save"] = {
        "seed_prompt": "p", "complete": True,
        "steps": {"world_rules": {"data": {"x": 1}, "approved": True}},
    }
    world_routes.world_draft_ids["resume_test_save"] = "draft_abc"

    resp = asyncio.run(world_routes.save_world(
        world_routes.SaveWorldRequest(world_id="Final World"),
        session_id="resume_test_save"))

    assert resp["world_id"] == "final_world"
    assert saving.deleted == ["draft_abc"]
    assert "resume_test_save" not in world_routes.world_gen_sessions
    assert "resume_test_save" not in world_routes.world_draft_ids

    # Draft id matching the final world must NOT be deleted.
    world_routes.world_gen_sessions["resume_test_save"] = {
        "steps": {"world_rules": {"data": {"x": 1}, "approved": True}}}
    world_routes.world_draft_ids["resume_test_save"] = "final_world"
    saving.deleted.clear()
    asyncio.run(world_routes.save_world(
        world_routes.SaveWorldRequest(world_id="final_world"),
        session_id="resume_test_save"))
    assert saving.deleted == []
