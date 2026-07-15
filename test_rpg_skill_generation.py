"""Tests for the new-skill wizard (categories/options/refine endpoints)."""
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
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend_generation", path)
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
            "swordplay": {"rating": 5, "description": "Trained duelist.", "trigger_words": ["slash"], "type": "active"},
        },
        "practice_counters": {"swordplay": 12},
        "pending_evolutions": [],
        "unspent_attribute_points": 0,
        "unspent_skill_points": 5,
    }
    base.update(overrides)
    return base


def _make_client(mod, rpg, llm_replies=None, config=None, cheats=False):
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
    settings = SimpleNamespace(get=lambda key: cheats if key == "cheats.enabled" else None)
    mod.set_services({"session_manager": session_manager, "engine": engine, "settings": settings})

    app = FastAPI()
    app.include_router(mod.get_router(), prefix=BASE)
    return TestClient(app), session_manager, calls


def _fix_roll(mod, value):
    """Make the pick-time strength roll deterministic."""
    mod._roll_strength = lambda: value


CATEGORIES_REPLY = json.dumps({
    "categories": [
        {"name": f"Category {i}", "summary": f"summary {i}"} for i in range(1, 11)
    ]
})

_P0_NAMES = ["Ember Feint", "Ash Veil", "Cinder Step", "Soot Sense", "Flame Ward"]
_P1_NAMES = ["Char Brand", "Smoke Form", "Pyre Call", "Kindled Eye", "Blaze Sprint"]


def _options_reply(names, skill_type="active"):
    return json.dumps({
        "skills": [
            {
                "name": n,
                "type": skill_type,
                "description": f"{n} does something concrete.",
                "trigger_words": ["burn", "flare"],
            }
            for n in names
        ]
    })


REFINE_REPLY = json.dumps({
    "name": "Ember Feint",
    "type": "active",
    "description": "A flicker of stolen flame misdirects one foe for a heartbeat.",
    "trigger_words": ["feint", "flicker"],
})


# ---------------------------------------------------------------------------
# wizard/categories
# ---------------------------------------------------------------------------

def test_categories_returns_ten_and_caches():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[CATEGORIES_REPLY])

    res = client.post(f"{BASE}/skills/wizard/categories")
    assert res.status_code == 200
    cats = res.json()["categories"]
    assert len(cats) == 10
    assert cats[0] == {"name": "Category 1", "summary": "summary 1"}
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "Dark Fantasy" in prompt
    assert "A wandering duelist." in prompt
    assert "exactly 10 skill categories" in prompt

    # Second call returns the cached list with zero extra LLM calls.
    res2 = client.post(f"{BASE}/skills/wizard/categories")
    assert res2.status_code == 200
    assert res2.json()["categories"] == cats
    assert len(calls) == 1


def test_categories_concurrent_requests_share_one_generation():
    """Two overlapping requests (StrictMode double-mount) must produce one
    LLM call and identical category sets, not two racing generations."""
    mod = _load_backend()
    calls = []

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append(prompt)
        await asyncio.sleep(0.01)  # suspend so both requests overlap
        return CATEGORIES_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _rpg())
    mod.set_services({
        "session_manager": sm,
        "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm)),
    })
    router = mod.get_router()
    endpoint = next(
        r.endpoint for r in router.routes if r.path == "/skills/wizard/categories"
    )

    async def run():
        return await asyncio.gather(endpoint(), endpoint())

    first, second = asyncio.run(run())
    assert first["categories"] == second["categories"]
    assert len(calls) == 1


def test_categories_generation_survives_waiter_cancellation():
    """Closing the app mid-generation cancels only the waiting request: the
    shielded task finishes and caches, so coming back finds the result."""
    mod = _load_backend()
    calls = []

    async def generate(prompt, model_preference="balanced", max_tokens=None):
        calls.append(prompt)
        await asyncio.sleep(0.05)
        return CATEGORIES_REPLY

    llm = SimpleNamespace(generate=generate, _current_module="")
    _, sm, _ = _make_client(mod, _rpg())
    mod.set_services({"session_manager": sm, "engine": SimpleNamespace(sdk=SimpleNamespace(llm=llm))})
    router = mod.get_router()
    endpoint = next(r.endpoint for r in router.routes if r.path == "/skills/wizard/categories")

    async def run():
        waiter = asyncio.create_task(endpoint())
        await asyncio.sleep(0.01)
        waiter.cancel()  # the app closed
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.15)  # generation finishes in the background
        return await endpoint()  # the app came back

    result = asyncio.run(run())
    assert len(result["categories"]) == 10
    assert len(calls) == 1  # cached by the surviving task, not regenerated


def test_categories_wrong_count_502_and_retry_recalls_llm():
    mod = _load_backend()
    short = json.dumps({"categories": [{"name": "Only One", "summary": "s"}]})
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[short, CATEGORIES_REPLY])

    res = client.post(f"{BASE}/skills/wizard/categories")
    assert res.status_code == 502

    # The failure is not cached: the retry pays for a fresh LLM call.
    res2 = client.post(f"{BASE}/skills/wizard/categories")
    assert res2.status_code == 200
    assert len(res2.json()["categories"]) == 10
    assert len(calls) == 2


def test_categories_llm_garbage_502():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), llm_replies=["not json at all"])
    res = client.post(f"{BASE}/skills/wizard/categories")
    assert res.status_code == 502


def test_categories_regenerate_replaces_cache():
    mod = _load_backend()
    fresh = json.dumps({
        "categories": [{"name": f"Fresh {i}", "summary": "s"} for i in range(10)]
    })
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[CATEGORIES_REPLY, fresh])

    res = client.post(f"{BASE}/skills/wizard/categories")
    assert res.json()["categories"][0]["name"] == "Category 1"

    res2 = client.post(f"{BASE}/skills/wizard/categories", json={"regenerate": True})
    assert res2.status_code == 200
    assert res2.json()["categories"][0]["name"] == "Fresh 0"
    assert len(calls) == 2

    # The regenerated list is cached: a plain call returns it with no LLM call.
    res3 = client.post(f"{BASE}/skills/wizard/categories")
    assert res3.json()["categories"] == res2.json()["categories"]
    assert len(calls) == 2


def test_categories_duplicate_names_rejected():
    mod = _load_backend()
    dupes = json.dumps({
        "categories": [{"name": "Same Name", "summary": "s"} for _ in range(10)]
    })
    client, _, _ = _make_client(mod, _rpg(), llm_replies=[dupes])
    res = client.post(f"{BASE}/skills/wizard/categories")
    assert res.status_code == 502


# ---------------------------------------------------------------------------
# wizard/options
# ---------------------------------------------------------------------------

def test_options_page0_returns_five_base_level_skills():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES)])

    res = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res.status_code == 200
    body = res.json()
    assert body["menu"] == "Flame Arts"
    assert body["page"] == 0
    assert [s["name"] for s in body["skills"]] == _P0_NAMES
    # Browsing is rarity-free: strength is only rolled at pick time (refine).
    assert all("strength" not in s for s in body["skills"])
    prompt = calls[0]["prompt"]
    assert '"Flame Arts" category' in prompt
    assert "swordplay" in prompt  # existing skills excluded
    assert "exactly 5 NEW skills" in prompt
    # Page skills are uniform base-level with one-sentence descriptions.
    assert "ONE tight sentence" in prompt
    assert "has power" not in prompt  # no per-slot power lines anymore


def test_options_page1_excludes_page0_names():
    mod = _load_backend()
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES), _options_reply(_P1_NAMES)]
    )
    res0 = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res0.status_code == 200
    res1 = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 1})
    assert res1.status_code == 200
    prompt = calls[1]["prompt"]
    for name in _P0_NAMES:
        assert name in prompt  # page-0 skills are in the do-not-repeat list
    names0 = {s["name"] for s in res0.json()["skills"]}
    names1 = {s["name"] for s in res1.json()["skills"]}
    assert not names0 & names1


def test_options_cached_page_needs_no_llm():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES)])
    res = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res.status_code == 200
    res2 = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res2.status_code == 200
    assert res2.json()["skills"] == res.json()["skills"]
    assert len(calls) == 1


def test_options_page_out_of_order_409():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES)])
    res = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 2})
    assert res.status_code == 409
    assert calls == []


def test_options_empty_menu_400():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES)])
    res = client.post(f"{BASE}/skills/wizard/options", json={"menu": "   ", "page": 0})
    assert res.status_code == 400
    assert calls == []


def test_options_excluded_name_in_reply_502():
    mod = _load_backend()
    # The model re-proposes the character's existing skill; the page must not
    # be cached so a retry re-calls the LLM.
    names = ["Swordplay"] + _P1_NAMES[:4]
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[_options_reply(names), _options_reply(_P0_NAMES)]
    )
    res = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res.status_code == 502
    res2 = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res2.status_code == 200
    assert len(calls) == 2


def test_options_curse_type_coerced_to_active():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES, skill_type="curse")])
    res = client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0})
    assert res.status_code == 200
    assert all(s["type"] == "active" for s in res.json()["skills"])


def test_options_search_mode_prompt_and_separate_cache():
    mod = _load_backend()
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[_options_reply(_P0_NAMES), _options_reply(_P1_NAMES)]
    )
    res = client.post(
        f"{BASE}/skills/wizard/options", json={"menu": "fire breathing", "search": True, "page": 0}
    )
    assert res.status_code == 200
    prompt = calls[0]["prompt"]
    assert 'searched for "fire breathing"' in prompt
    assert "close interpretations" in prompt

    # The same string as a category is a separate menu with its own pages.
    res2 = client.post(
        f"{BASE}/skills/wizard/options", json={"menu": "fire breathing", "search": False, "page": 0}
    )
    assert res2.status_code == 200
    assert len(calls) == 2
    assert res.json()["skills"] != res2.json()["skills"]


def test_roll_strength_always_five_to_ten():
    mod = _load_backend()
    rolls = {mod._roll_strength() for _ in range(200)}
    assert rolls <= set(range(5, 11))
    # Uniform over six faces: 200 rolls hit every tier in practice.
    assert rolls == set(range(5, 11))


# ---------------------------------------------------------------------------
# wizard/refine
# ---------------------------------------------------------------------------

def test_refine_rolls_strength_and_carries_draft_and_context_in_prompt():
    mod = _load_backend()
    _fix_roll(mod, 7)
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY])
    res = client.post(f"{BASE}/skills/wizard/refine", json={
        "name": "Ember Feint",
        "type": "active",
        "description": "A draft description.",
        "trigger_words": ["feint"],
        "menu": "Flame Arts",
    })
    assert res.status_code == 200
    skill = res.json()["skill"]
    assert skill["name"] == "Ember Feint"
    assert skill["strength"] == 7
    assert skill["description"] == "A flicker of stolen flame misdirects one foe for a heartbeat."
    prompt = calls[0]["prompt"]
    assert "Ember Feint" in prompt
    assert "A draft description." in prompt
    assert "FREE-STANDING" in prompt
    assert "rarity: Rare (7/10" in prompt
    # Higher rarity = stronger benefits, weaker drawbacks - stated explicitly.
    assert "STRONGER" in prompt and "WEAKER" in prompt
    assert "Flame Arts" in prompt


def test_refine_ignores_client_supplied_strength():
    mod = _load_backend()
    _fix_roll(mod, 9)
    client, _, _ = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY])
    # A client trying to smuggle in its own roll gets the server's roll anyway.
    res = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint", "strength": 3})
    assert res.status_code == 200
    assert res.json()["skill"]["strength"] == 9


def test_refine_mythic_roll_adds_peak_guidance():
    mod = _load_backend()
    _fix_roll(mod, 10)
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY])
    res = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"})
    assert res.status_code == 200
    assert res.json()["skill"]["strength"] == 10
    prompt = calls[0]["prompt"]
    assert "rarity: Mythic (10/10" in prompt
    assert "absolute peak" in prompt


def test_refine_roll_is_locked_per_skill():
    """Re-picking the same skill can't re-roll: the cached refined result
    (same strength, same text) comes back with no second LLM call."""
    mod = _load_backend()
    rolls = iter([6, 10])
    mod._roll_strength = lambda: next(rolls)
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY, REFINE_REPLY])

    first = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"})
    assert first.status_code == 200
    assert first.json()["skill"]["strength"] == 6

    again = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"})
    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(calls) == 1  # no second generation, no second roll

    # A different skill still gets its own fresh roll.
    other = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ash Veil"})
    assert other.status_code == 200
    assert other.json()["skill"]["strength"] == 10
    assert len(calls) == 2


def test_refine_roll_lock_clears_after_learning():
    mod = _load_backend()
    rolls = iter([6, 9])
    mod._roll_strength = lambda: next(rolls)
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY, REFINE_REPLY])

    assert client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"}).status_code == 200
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {"name": "Ember Feint", "description": "d", "rating": 6}
    })
    assert res.status_code == 200

    # Learning a skill clears the wizard cache, so a fresh wizard session
    # rolls anew (the learned name would be rejected as a duplicate anyway).
    res2 = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Cinder Step"})
    assert res2.status_code == 200
    assert res2.json()["skill"]["strength"] == 9
    assert len(calls) == 2


def test_refine_falls_back_to_draft_fields():
    mod = _load_backend()
    _fix_roll(mod, 6)
    reply = json.dumps({"name": "Ember Feint"})
    client, _, _ = _make_client(mod, _rpg(), llm_replies=[reply])
    res = client.post(f"{BASE}/skills/wizard/refine", json={
        "name": "Ember Feint",
        "type": "passive",
        "description": "Draft only.",
        "trigger_words": ["feint", "flicker"],
    })
    assert res.status_code == 200
    skill = res.json()["skill"]
    assert skill["description"] == "Draft only."
    assert skill["trigger_words"] == ["feint", "flicker"]
    assert skill["strength"] == 6


def test_refine_llm_garbage_502():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), llm_replies=["nope"])
    res = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"})
    assert res.status_code == 502


# ---------------------------------------------------------------------------
# cheats: forced rarity, gated by the global cheats.enabled engine setting
# ---------------------------------------------------------------------------

def _boom():
    raise AssertionError("_roll_strength must not be called for a forced pick")


def test_forced_strength_honored_when_cheats_on():
    mod = _load_backend()
    mod._roll_strength = _boom  # a forced pick must not consume a roll
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY], cheats=True)
    res = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint", "forced_strength": 10})
    assert res.status_code == 200
    assert res.json()["skill"]["strength"] == 10
    assert "rarity: Mythic (10/10" in calls[0]["prompt"]


def test_forced_strength_out_of_range_400():
    mod = _load_backend()
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY], cheats=True)
    for bad in (4, 11, 0):
        res = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint", "forced_strength": bad})
        assert res.status_code == 400
    assert calls == []


def test_forced_repick_overwrites_locked_roll():
    mod = _load_backend()
    _fix_roll(mod, 5)
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[REFINE_REPLY, REFINE_REPLY, REFINE_REPLY], cheats=True
    )
    # Honest roll first: locked at 5.
    first = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"})
    assert first.json()["skill"]["strength"] == 5
    # Forcing bypasses and overwrites the lock.
    forced = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint", "forced_strength": 8})
    assert forced.json()["skill"]["strength"] == 8
    assert len(calls) == 2
    # The overwritten result is what a later plain re-pick returns.
    again = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"})
    assert again.json()["skill"]["strength"] == 8
    assert len(calls) == 2


def test_forced_strength_ignored_when_cheats_off():
    mod = _load_backend()
    _fix_roll(mod, 6)
    client, _, calls = _make_client(mod, _rpg(), llm_replies=[REFINE_REPLY], cheats=False)
    res = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint", "forced_strength": 10})
    assert res.status_code == 200
    assert res.json()["skill"]["strength"] == 6  # fate rolled, cheat ignored
    # And the roll stays locked: a second forced attempt returns the cache.
    res2 = client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint", "forced_strength": 10})
    assert res2.json()["skill"]["strength"] == 6
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# cross-cutting: story context, config, cache invalidation
# ---------------------------------------------------------------------------

def test_wizard_prompts_carry_story_style_and_recent_scenes():
    mod = _load_backend()
    client, sm, calls = _make_client(
        mod, _rpg(), llm_replies=[CATEGORIES_REPLY, _options_reply(_P0_NAMES), REFINE_REPLY]
    )
    sm.state["story_style"] = {"themes": "redemption, found family", "tags": "noir, heist", "pacing": "slow burn"}
    sm.state["history"] = ["Scene one.", "Scene two.", "Scene three.", "Scene four."]

    assert client.post(f"{BASE}/skills/wizard/categories").status_code == 200
    assert client.post(f"{BASE}/skills/wizard/options", json={"menu": "Flame Arts", "page": 0}).status_code == 200
    assert client.post(f"{BASE}/skills/wizard/refine", json={"name": "Ember Feint"}).status_code == 200

    assert len(calls) == 3
    for call in calls:
        prompt = call["prompt"]
        assert "Story themes: redemption, found family" in prompt
        assert "Story tags: noir, heist" in prompt
        # The last three scenes, in full.
        assert "Scene two." in prompt and "Scene three." in prompt and "Scene four." in prompt
        assert "Scene one." not in prompt
        # Pacing is narration rhythm, not ability design.
        assert "slow burn" not in prompt


def test_wizard_prompts_skip_empty_story_context():
    mod = _load_backend()
    client, sm, calls = _make_client(mod, _rpg(), llm_replies=[CATEGORIES_REPLY])
    sm.state["story_style"] = {"themes": "  ", "tags": "", "pacing": ""}
    res = client.post(f"{BASE}/skills/wizard/categories")
    assert res.status_code == 200
    prompt = calls[0]["prompt"]
    assert "Story themes" not in prompt
    assert "Story tags" not in prompt
    assert "Recent story" not in prompt


def test_wizard_uses_new_skill_ai_model_config():
    mod = _load_backend()
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[CATEGORIES_REPLY], config={"new_skill_ai_model": "balanced"}
    )
    assert client.post(f"{BASE}/skills/wizard/categories").status_code == 200
    assert calls[0]["model_preference"] == "balanced"


def test_add_skill_clears_wizard_cache():
    mod = _load_backend()
    # Cheats on: the manual add-skill endpoint used below is cheat-gated.
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[CATEGORIES_REPLY, CATEGORIES_REPLY], cheats=True
    )
    assert client.post(f"{BASE}/skills/wizard/categories").status_code == 200
    res = client.post(f"{BASE}/skills", json={"name": "herbalism", "description": "d"})
    assert res.status_code == 200
    # Cache was invalidated: a fresh categories call pays for a new generation.
    assert client.post(f"{BASE}/skills/wizard/categories").status_code == 200
    assert len(calls) == 2


def test_levelup_spend_clears_wizard_cache():
    mod = _load_backend()
    client, _, calls = _make_client(
        mod, _rpg(), llm_replies=[CATEGORIES_REPLY, CATEGORIES_REPLY]
    )
    assert client.post(f"{BASE}/skills/wizard/categories").status_code == 200
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {"name": "Ember Feint", "description": "d", "type": "active"}
    })
    assert res.status_code == 200
    assert client.post(f"{BASE}/skills/wizard/categories").status_code == 200
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# save path: rolled strength becomes the starting rating
# ---------------------------------------------------------------------------

def test_levelup_new_skill_rating_from_strength():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), config={"new_skill_cost": 3})
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {
            "name": "Ember Feint",
            "description": "d",
            "trigger_words": ["feint", "flicker"],
            "type": "active",
            "rating": 8,
        }
    })
    assert res.status_code == 200
    rpg = res.json()
    skill = rpg["skills"]["ember feint"]
    assert skill["rating"] == 8
    assert skill["trigger_words"] == ["feint", "flicker"]
    # Cost stays new_skill_cost regardless of the rolled strength.
    assert rpg["unspent_skill_points"] == 2


def test_levelup_new_skill_rating_out_of_range_400():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg())
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {"name": "Ember Feint", "rating": 11}
    })
    assert res.status_code == 400


def test_levelup_new_skill_without_rating_keeps_cost_default():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), config={"new_skill_cost": 3})
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {"name": "Ember Feint", "description": "d"}
    })
    assert res.status_code == 200
    assert res.json()["skills"]["ember feint"]["rating"] == 3


def test_levelup_strength_ten_skill_is_immediately_evolvable():
    mod = _load_backend()
    client, _, _ = _make_client(mod, _rpg(), config={"new_skill_cost": 3})
    res = client.post(f"{BASE}/levelup/spend", json={
        "new_skill": {"name": "Ember Feint", "description": "d", "rating": 10}
    })
    assert res.status_code == 200
    pending = res.json()["pending_evolutions"]
    assert any(e["skill"] == "ember feint" for e in pending)
