"""Tests for the skill evolution flow (options + evolve endpoints, queueing)."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE = "/api/modules/wb_core_rpg"


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend_evolution", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_llm(replies, calls):
    """LLM stub: pops canned replies in order and records each prompt."""

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append({"prompt": prompt, "model_preference": model_preference})
        if not replies:
            return ""
        return replies.pop(0)

    return SimpleNamespace(generate=generate, _current_module="")


def _rpg(**overrides):
    base = {
        "stats": {s: 10 for s in ["power", "agility", "vitality", "intelligence", "spirit", "charm"]},
        "level": 5,
        "xp": 700,
        "hp": 80,
        "max_hp": 80,
        "backstory": "A wandering duelist.",
        "skills": {
            "swordplay": {"rating": 10, "description": "Master duelist.", "trigger_words": ["slash"], "type": "active"},
        },
        "practice_counters": {"swordplay": 12},
        "pending_evolutions": [{"skill": "swordplay", "options": None, "status": "pending"}],
        "unspent_attribute_points": 0,
        "unspent_skill_points": 0,
    }
    base.update(overrides)
    return base


def _make_client(mod, rpg, llm_replies=None, config=None):
    calls = []
    llm = _fake_llm(list(llm_replies or []), calls)
    session_manager = SimpleNamespace(
        active_save_id="save1",
        state={
            "turn": 9,
            "world_data": {"rules": {"genre": "Dark Fantasy", "tone": "Grim"}, "lore": {"premise": "A dying empire."}},
            "module_configs": {"wb_core_rpg": config or {}},
            "module_data": {"wb_core_rpg": rpg},
        },
        save_manager=SimpleNamespace(save_turn=lambda *a: None),
    )
    engine = SimpleNamespace(sdk=SimpleNamespace(llm=llm))
    mod.set_services({"session_manager": session_manager, "engine": engine})

    app = FastAPI()
    app.include_router(mod.get_router(), prefix=BASE)
    return TestClient(app), session_manager, calls


OPTIONS_REPLY = json.dumps({
    "options": [
        {"theme": "Perfected Blade", "summary": "the same swordplay, honed far past mortal limits"},
        {"theme": "Brutal", "summary": "raw overwhelming force"},
        {"theme": "Efficiency", "summary": "no wasted motion"},
        {"theme": "Stealthy", "summary": "strikes from silence"},
    ]
})

EVOLVE_REPLY = json.dumps({
    "name": "Brutal Bladework",
    "description": "Every cut lands with crushing force; armor and guard mean little.",
    "trigger_words": ["cleave", "crush", "overpower"],
})


# ---------------------------------------------------------------------------
# evolution-options
# ---------------------------------------------------------------------------

def test_options_returns_four_themes_and_caches():
    mod = _load_backend()
    client, sm, calls = _make_client(mod, _rpg(), llm_replies=[OPTIONS_REPLY])

    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 200
    body = res.json()
    assert body["skill"] == "swordplay"
    assert body["tier"] == 1
    assert [o["theme"] for o in body["options"]] == ["Perfected Blade", "Brutal", "Efficiency", "Stealthy"]
    # The first option is the pure amplification path; the rest diverge.
    assert [o["kind"] for o in body["options"]] == ["pure", "divergent", "divergent", "divergent"]
    assert len(calls) == 1
    # Full context reaches the prompt: world, character, complete skill record.
    prompt = calls[0]["prompt"]
    assert "Dark Fantasy" in prompt
    assert "Master duelist." in prompt
    assert "A wandering duelist." in prompt
    assert "exactly 4 evolution paths" in prompt
    assert "pure path" in prompt

    # Second call returns the cached options with zero extra LLM calls.
    res2 = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res2.status_code == 200
    assert res2.json()["options"] == body["options"]
    assert len(calls) == 1


def test_options_concurrent_requests_share_one_generation():
    """Two overlapping requests (StrictMode double-mount) must produce one
    LLM call and identical option sets, not two racing generations."""
    mod = _load_backend()
    calls = []

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append(prompt)
        await asyncio.sleep(0.01)  # suspend so both requests overlap
        return OPTIONS_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _rpg())
    mod.set_services({
        "session_manager": sm,
        "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm)),
    })
    router = mod.get_router()
    endpoint = next(
        r.endpoint for r in router.routes if r.path == "/skills/{skill_name}/evolution-options"
    )

    async def run():
        return await asyncio.gather(endpoint("swordplay"), endpoint("swordplay"))

    first, second = asyncio.run(run())
    assert first["options"] == second["options"]
    assert len(calls) == 1


def test_options_rejects_non_maxed_skill():
    mod = _load_backend()
    rpg = _rpg()
    rpg["skills"]["swordplay"]["rating"] = 9
    rpg["pending_evolutions"] = []
    client, _, calls = _make_client(mod, rpg, llm_replies=[OPTIONS_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 409
    assert calls == []


def test_options_rejects_curse():
    mod = _load_backend()
    rpg = _rpg()
    rpg["skills"]["swordplay"]["type"] = "curse"
    rpg["pending_evolutions"] = []
    client, _, calls = _make_client(mod, rpg, llm_replies=[OPTIONS_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 409
    assert calls == []


def test_options_llm_garbage_returns_502():
    mod = _load_backend()
    client, sm, _ = _make_client(mod, _rpg(), llm_replies=["not json at all"])
    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 502
    # Entry stays pending with no cached options, so a retry re-calls the LLM.
    entry = sm.state["module_data"]["wb_core_rpg"]["pending_evolutions"][0]
    assert entry["options"] is None


def test_options_theme_clamped_to_three_words():
    mod = _load_backend()
    reply = json.dumps({"options": [
        {"theme": "Way Too Many Words Here Truly", "summary": "s"},
        {"theme": "B", "summary": "s"},
        {"theme": "C", "summary": "s"},
        {"theme": "D", "summary": "s"},
    ]})
    client, _, _ = _make_client(mod, _rpg(), llm_replies=[reply])
    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 200
    assert res.json()["options"][0]["theme"] == "Way Too Many"


def test_options_wrong_count_returns_502():
    mod = _load_backend()
    reply = json.dumps({"options": [
        {"theme": "A", "summary": "s"},
        {"theme": "B", "summary": "s"},
        {"theme": "C", "summary": "s"},
    ]})
    client, sm, _ = _make_client(mod, _rpg(), llm_replies=[reply])
    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 502
    entry = sm.state["module_data"]["wb_core_rpg"]["pending_evolutions"][0]
    assert entry["options"] is None


# ---------------------------------------------------------------------------
# evolve
# ---------------------------------------------------------------------------

def _cached_rpg():
    rpg = _rpg()
    rpg["pending_evolutions"] = [{
        "skill": "swordplay",
        "options": [
            {"theme": "Perfected Blade", "summary": "sharper still", "kind": "pure"},
            {"theme": "Brutal", "summary": "raw force", "kind": "divergent"},
            {"theme": "Efficiency", "summary": "clean", "kind": "divergent"},
            {"theme": "Stealthy", "summary": "silent", "kind": "divergent"},
        ],
        "status": "pending",
    }]
    return rpg


def test_evolve_applies_tiered_form():
    mod = _load_backend()
    client, sm, calls = _make_client(mod, _cached_rpg(), llm_replies=[EVOLVE_REPLY], config={"evolution_ai_model": "smartest"})

    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 200
    body = res.json()
    assert body["evolved"] == {
        "old_name": "swordplay",
        "new_name": "brutal bladework",
        "tier": 2,
        "theme": "Brutal",
        "description": "Every cut lands with crushing force; armor and guard mean little.",
    }
    rpg = body["rpg"]
    assert "swordplay" not in rpg["skills"]
    skill = rpg["skills"]["brutal bladework"]
    assert skill["tier"] == 2
    assert skill["rating"] == 5  # reset to regrow
    assert skill["type"] == "active"
    assert skill["lineage"] == ["swordplay"]
    assert skill["evolution_theme"] == "Brutal"
    # Practice counters follow the rename; the queue entry is consumed.
    assert rpg["practice_counters"] == {"brutal bladework": 12}
    assert rpg["pending_evolutions"] == []
    assert calls[0]["model_preference"] == "smartest"
    assert '"Brutal" path' in calls[0]["prompt"]
    # Divergent path: the theme steers a new direction, not a pure upgrade.
    assert 'embody the "Brutal" theme' in calls[0]["prompt"]
    assert "PURE path" not in calls[0]["prompt"]
    # Power-up applies to benefits only; drawbacks must not scale with it.
    assert "must NOT grow stronger" in calls[0]["prompt"]
    # The description must stand alone, never referencing the prior form.
    assert "FREE-STANDING" in calls[0]["prompt"]


def test_evolve_pure_path_keeps_skill_identity_in_prompt():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _cached_rpg(), llm_replies=[EVOLVE_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Perfected Blade"})
    assert res.status_code == 200
    prompt = calls[0]["prompt"]
    assert "PURE path" in prompt
    assert "do NOT take it" in prompt
    assert "embody the" not in prompt


def test_evolve_legacy_cached_options_without_kind_stay_divergent():
    # Saves from before the 4-option flow cached options without a kind.
    mod = _load_backend()
    rpg = _cached_rpg()
    rpg["pending_evolutions"][0]["options"] = [
        {"theme": "Brutal", "summary": "raw force"},
        {"theme": "Efficiency", "summary": "clean"},
        {"theme": "Stealthy", "summary": "silent"},
    ]
    client, _, calls = _make_client(mod, rpg, llm_replies=[EVOLVE_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 200
    assert 'embody the "Brutal" theme' in calls[0]["prompt"]
    assert "PURE path" not in calls[0]["prompt"]


def test_evolve_rejects_theme_not_offered():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _cached_rpg(), llm_replies=[EVOLVE_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Cuddly"})
    assert res.status_code == 400
    assert calls == []


def test_evolve_accepts_any_theme_when_no_cached_options():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), llm_replies=[EVOLVE_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Relentless"})
    assert res.status_code == 200
    assert res.json()["evolved"]["theme"] == "Relentless"


def test_evolve_name_collision_disambiguates():
    mod = _load_backend()
    rpg = _cached_rpg()
    rpg["skills"]["brutal bladework"] = {"rating": 4, "description": "", "trigger_words": [], "type": "active"}
    client, _, _ = _make_client(mod, rpg, llm_replies=[EVOLVE_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 200
    assert res.json()["evolved"]["new_name"] == "brutal bladework 2"
    # The pre-existing skill is untouched.
    assert res.json()["rpg"]["skills"]["brutal bladework"]["rating"] == 4


def test_evolve_llm_failure_leaves_state_untouched():
    mod = _load_backend()
    client, sm, _ = _make_client(mod, _cached_rpg(), llm_replies=[""])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 502
    rpg = sm.state["module_data"]["wb_core_rpg"]
    assert rpg["skills"]["swordplay"]["rating"] == 10
    assert rpg["pending_evolutions"][0]["skill"] == "swordplay"


def test_evolved_skill_can_evolve_again():
    mod = _load_backend()
    rpg = _cached_rpg()
    rpg["skills"]["swordplay"].update({"tier": 2, "lineage": ["blades"], "evolution_theme": "Efficiency"})
    reply = json.dumps({"name": "Deathless Edge", "description": "d", "trigger_words": ["edge"]})
    client, _, calls = _make_client(mod, rpg, llm_replies=[reply])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 200
    skill = res.json()["rpg"]["skills"]["deathless edge"]
    assert skill["tier"] == 3
    assert skill["lineage"] == ["blades", "swordplay"]
    assert "Tier 2 to Tier 3" in calls[0]["prompt"]


def test_evolve_survives_waiter_cancellation():
    """Closing the app mid-evolution cancels only the waiting request: the
    shielded task finishes, mutates the skill, and persists."""
    mod = _load_backend()
    calls = []

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append(prompt)
        await asyncio.sleep(0.05)
        return EVOLVE_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _cached_rpg())
    mod.set_services({"session_manager": sm, "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm))})
    router = mod.get_router()
    endpoint = next(r.endpoint for r in router.routes if r.path == "/skills/{skill_name}/evolve")

    async def run():
        waiter = asyncio.create_task(endpoint("swordplay", SimpleNamespace(theme="Brutal")))
        await asyncio.sleep(0.01)
        waiter.cancel()  # the app closed mid-evolution
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.15)  # the evolution finishes in the background

    asyncio.run(run())
    rpg = sm.state["module_data"]["wb_core_rpg"]
    assert "swordplay" not in rpg["skills"]
    assert "brutal bladework" in rpg["skills"]
    # The reveal survives for the returning player via recent_evolutions.
    assert rpg["recent_evolutions"][0]["new_name"] == "brutal bladework"
    assert len(calls) == 1


def test_evolve_mutates_reloaded_state_not_the_orphan():
    """Reopening the app mid-evolution reloads the save from disk, replacing
    the state dicts the shielded evolve task captured at request time. The
    finished evolution must land in the LIVE state - mutating the orphan made
    the evolved skill silently vanish from the character sheet."""
    import copy
    mod = _load_backend()
    calls = []

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append(prompt)
        await asyncio.sleep(0.05)
        return EVOLVE_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _cached_rpg())
    mod.set_services({"session_manager": sm, "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm))})
    router = mod.get_router()
    endpoint = next(r.endpoint for r in router.routes if r.path == "/skills/{skill_name}/evolve")

    async def run():
        waiter = asyncio.create_task(endpoint("swordplay", SimpleNamespace(theme="Brutal")))
        await asyncio.sleep(0.01)
        # The app reopened: same content reloaded from disk, new dict objects.
        sm.state = copy.deepcopy(sm.state)
        return await waiter

    result = asyncio.run(run())
    live = sm.state["module_data"]["wb_core_rpg"]
    assert "brutal bladework" in live["skills"]
    assert "swordplay" not in live["skills"]
    assert live["recent_evolutions"][0]["new_name"] == "brutal bladework"
    assert result["rpg"] is live


def test_evolve_aborts_when_a_different_save_loads():
    mod = _load_backend()

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        await asyncio.sleep(0.05)
        return EVOLVE_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _cached_rpg())
    other_rpg = {"skills": {"weaving": {"rating": 3, "description": "", "trigger_words": [], "type": "active"}}}
    mod.set_services({"session_manager": sm, "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm))})
    router = mod.get_router()
    endpoint = next(r.endpoint for r in router.routes if r.path == "/skills/{skill_name}/evolve")

    async def run():
        waiter = asyncio.create_task(endpoint("swordplay", SimpleNamespace(theme="Brutal")))
        await asyncio.sleep(0.01)
        # The player switched to a different story mid-evolution.
        sm.active_save_id = "save2"
        sm.state["module_data"]["wb_core_rpg"] = other_rpg
        try:
            await waiter
            return None
        except Exception as e:
            return e

    err = asyncio.run(run())
    assert getattr(err, "status_code", None) == 409
    # The other story's character was never touched.
    assert set(other_rpg["skills"]) == {"weaving"}


def test_options_cache_lands_in_reloaded_state():
    import copy
    mod = _load_backend()
    calls = []

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append(prompt)
        await asyncio.sleep(0.05)
        return OPTIONS_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _rpg())
    mod.set_services({"session_manager": sm, "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm))})
    router = mod.get_router()
    endpoint = next(r.endpoint for r in router.routes if r.path == "/skills/{skill_name}/evolution-options")

    async def run():
        waiter = asyncio.create_task(endpoint("swordplay"))
        await asyncio.sleep(0.01)
        sm.state = copy.deepcopy(sm.state)
        return await waiter

    result = asyncio.run(run())
    assert len(result["options"]) == 4
    live_entry = sm.state["module_data"]["wb_core_rpg"]["pending_evolutions"][0]
    assert live_entry["options"] == result["options"]


def test_evolution_prompts_carry_story_style_and_recent_scenes():
    mod = _load_backend()
    client, sm, calls = _make_client(mod, _rpg(), llm_replies=[OPTIONS_REPLY, EVOLVE_REPLY])
    sm.state["story_style"] = {"themes": "redemption, found family", "tags": "noir, heist", "pacing": "slow burn"}
    sm.state["history"] = ["Scene one.", "Scene two.", "Scene three.", "Scene four."]

    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 200
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 200

    for call in calls:
        prompt = call["prompt"]
        assert "Story themes: redemption, found family" in prompt
        assert "Story tags: noir, heist" in prompt
        # The last three scenes, in full.
        assert "Scene two." in prompt and "Scene three." in prompt and "Scene four." in prompt
        assert "Scene one." not in prompt
        # Pacing is narration rhythm, not ability design.
        assert "slow burn" not in prompt


def test_evolution_prompts_skip_empty_story_context():
    mod = _load_backend()
    client, sm, calls = _make_client(mod, _rpg(), llm_replies=[OPTIONS_REPLY])
    sm.state["story_style"] = {"themes": "  ", "tags": "", "pacing": ""}
    res = client.post(f"{BASE}/skills/swordplay/evolution-options")
    assert res.status_code == 200
    prompt = calls[0]["prompt"]
    assert "Story themes" not in prompt
    assert "Story tags" not in prompt
    assert "Recent story" not in prompt


# ---------------------------------------------------------------------------
# storyteller announcement after an evolution
# ---------------------------------------------------------------------------

def test_evolve_queues_storyteller_announcement():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _cached_rpg(), llm_replies=[EVOLVE_REPLY])
    res = client.post(f"{BASE}/skills/swordplay/evolve", json={"theme": "Brutal"})
    assert res.status_code == 200
    assert res.json()["rpg"]["recent_evolutions"] == [{
        "old_name": "swordplay",
        "new_name": "brutal bladework",
        "tier": 2,
        "theme": "Brutal",
        "description": "Every cut lands with crushing force; armor and guard mean little.",
        "announced": False,
    }]


def test_evolution_announcement_feeds_exactly_one_generation():
    backend = _load_backend()
    rpg = _rpg()
    rpg["skills"] = {"brutal bladework": {
        "rating": 5, "description": "Crushing cuts.", "trigger_words": [], "type": "active", "tier": 2,
    }}
    rpg["pending_evolutions"] = []
    rpg["recent_evolutions"] = [{
        "old_name": "swordplay", "new_name": "brutal bladework", "tier": 2,
        "theme": "Brutal", "description": "Crushing cuts.", "announced": False,
    }]
    state = {"module_data": {"wb_core_rpg": rpg}, "module_configs": {"wb_core_rpg": {}}}

    # Turn 1: gather marks the note as this generation's; the sheet carries it.
    updates = asyncio.run(backend.on_gather_context(state, None))
    state["module_data"]["wb_core_rpg"] = updates["module_data"]["wb_core_rpg"]
    block = asyncio.run(backend.on_render_prompt_block({"id": "character_sheet"}, state, None))
    assert "SKILL EVOLUTION" in block["content"]
    assert '"swordplay" has just evolved into "brutal bladework"' in block["content"]
    assert "Crushing cuts." in block["content"]

    # Turn 2: the note was announced, so it is dropped and the sheet is clean.
    updates = asyncio.run(backend.on_gather_context(state, None))
    state["module_data"]["wb_core_rpg"] = updates["module_data"]["wb_core_rpg"]
    assert state["module_data"]["wb_core_rpg"]["recent_evolutions"] == []
    block = asyncio.run(backend.on_render_prompt_block({"id": "character_sheet"}, state, None))
    assert "SKILL EVOLUTION" not in block["content"]


def test_evolution_announcement_included_when_unconscious():
    backend = _load_backend()
    char = backend.Character()
    char.hp = 0
    char.recent_evolutions = [{
        "old_name": "swordplay", "new_name": "brutal bladework", "tier": 2,
        "theme": "Brutal", "description": "Crushing cuts.", "announced": True,
    }]
    sheet = backend._render_character_sheet(char, {})
    assert "unconscious" in sheet
    assert "SKILL EVOLUTION" in sheet


# ---------------------------------------------------------------------------
# defer + queueing paths
# ---------------------------------------------------------------------------

def test_defer_marks_entry_deferred():
    mod = _load_backend()
    client, sm, _ = _make_client(mod, _rpg())
    res = client.delete(f"{BASE}/skills/swordplay/evolution")
    assert res.status_code == 200
    assert res.json()["pending_evolutions"][0]["status"] == "deferred"


def test_deferred_entry_not_requeued_as_pending():
    backend = _load_backend()
    char = {
        "stats": {s: 10 for s in ["power", "agility", "vitality", "intelligence", "spirit", "charm"]},
        "level": 1, "xp": 0, "hp": 85, "max_hp": 85,
        "skills": {"swordplay": {"rating": 10, "description": "", "trigger_words": [], "type": "active"}},
        "pending_evolutions": [{"skill": "swordplay", "options": None, "status": "deferred"}],
        "action_assessment": {"feasibility": 8, "difficulty": "moderate"},
    }
    state = {"module_configs": {"wb_core_rpg": {}}, "module_data": {"wb_core_rpg": char}}
    result = asyncio.run(backend.on_mutate_state({}, state, None))
    rpg = result["module_data"]["wb_core_rpg"]
    assert rpg["pending_evolutions"] == [{"skill": "swordplay", "options": None, "status": "deferred"}]


def test_librarian_grant_to_max_queues_evolution():
    backend = _load_backend()
    reply = json.dumps({
        "added": [],
        "removed": [],
        "altered": [{"name": "swordplay", "new_rating": 10}],
    })
    calls = []
    sdk = SimpleNamespace(llm=_fake_llm([reply], calls))
    state = {
        "turn": 3,
        "history": ["The war-god's blessing settles into your arms."],
        "module_configs": {},
        "module_data": {"wb_core_rpg": {
            "skills": {"swordplay": {"rating": 7, "description": "", "trigger_words": [], "type": "active"}},
        }},
    }
    result = asyncio.run(backend.on_librarian(state, sdk))
    rpg = result["module_data"]["wb_core_rpg"]
    assert rpg["skills"]["swordplay"]["rating"] == 10
    assert rpg["pending_evolutions"] == [{"skill": "swordplay", "options": None, "status": "pending"}]


def test_manual_edit_to_max_queues_and_delete_prunes():
    mod = _load_backend()
    rpg = _rpg()
    rpg["skills"]["swordplay"]["rating"] = 9
    rpg["pending_evolutions"] = []
    client, _, _ = _make_client(mod, rpg)

    res = client.put(f"{BASE}/skills/swordplay", json={"rating": 10})
    assert res.status_code == 200
    assert rpg["pending_evolutions"] == [{"skill": "swordplay", "options": None, "status": "pending"}]

    res = client.delete(f"{BASE}/skills/swordplay")
    assert res.status_code == 200
    assert rpg["pending_evolutions"] == []
