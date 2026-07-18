"""Notable Features (natural_landmarks) step tests: the ``environment`` tag
only exists for terrain-aware placement, so the step's per-world view drops it
(field + tag guidance) on worlds whose creation generates no terrain.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_natural_landmarks.py
"""

import asyncio
import json
import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.steps.natural_landmarks import NaturalLandmarksStep


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_nl_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    wb = WorldBuilder(worlds_dir=tmpdir)
    register_default_steps(wb)
    return wb


class RecordingLLM:
    def __init__(self, payload=None):
        self.mode = "live"
        self.reader_model = "reader-slot"
        self.module_fast_model = "fast-slot"
        self.payload = payload or {}
        self.calls = []

    async def simple_completion(self, messages=None, **kwargs):
        self.calls.append(messages)
        return json.dumps(self.payload)


def _state_with_form(data):
    return {"seed_prompt": "seed", "steps": {"world_form": {"data": data, "approved": True}}}


# ---------------------------------------------------------------------------
# view_for (pure)
# ---------------------------------------------------------------------------

def test_view_keeps_environment_on_terrain_worlds():
    step = NaturalLandmarksStep()
    for state in (_state_with_form({"map_style": "terrain"}),
                  {},  # no world_form data (old worlds) -> terrain by default
                  {"seed_prompt": "s", "steps": {}}):
        view = step.view_for(state)
        assert view is step
        assert "environment" in view.schema["landmarks"]["item_schema"]
        assert "grassy_plain" in view.guidance


def test_view_drops_environment_on_abstract_and_city_worlds():
    step = NaturalLandmarksStep()
    for style in ("abstract", "city"):
        view = step.view_for(_state_with_form({"map_style": style}))
        assert view is not step
        assert "environment" not in view.schema["landmarks"]["item_schema"]
        assert "grassy_plain" not in view.guidance
        assert "scope" in view.schema["landmarks"]["item_schema"]
        # Scope guidance survives; the class-level (frontend) schema is untouched.
        assert "parallel map's name" in view.guidance
        assert "environment" in step.schema["landmarks"]["item_schema"]
        assert step.schema["landmarks"]["item_schema"]["environment"]["conditional"] is True


# ---------------------------------------------------------------------------
# facade: generation + per-item reroll use the per-world view
# ---------------------------------------------------------------------------

def test_city_world_prompt_has_no_environment_tags(builder):
    builder.set_llm_service(RecordingLLM({"landmarks": []}))
    state = _state_with_form({"map_style": "city"})
    asyncio.run(builder.generate_step("natural_landmarks", state, "seed"))
    user = builder._llm_service.calls[0][1]["content"]
    assert "grassy_plain" not in user
    assert '"environment"' not in user

    builder._llm_service.calls.clear()
    state = _state_with_form({"map_style": "terrain"})
    asyncio.run(builder.generate_step("natural_landmarks", state, "seed"))
    user = builder._llm_service.calls[0][1]["content"]
    assert "grassy_plain" in user
    assert '"environment"' in user


def test_city_world_item_reroll_has_no_environment_tags(builder):
    builder.set_llm_service(RecordingLLM({"name": "The Night Market"}))
    items = [{"scope": "", "name": "Old Quarter", "type": "district", "description": "d"}]
    state = _state_with_form({"map_style": "city"})
    asyncio.run(builder.regenerate_list_item(
        "natural_landmarks", "landmarks", items, 0, state, "seed"))
    user = builder._llm_service.calls[0][1]["content"]
    assert "grassy_plain" not in user
    assert '"environment"' not in user
