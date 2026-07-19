"""Tests for C5 ideation notes: the notes module (cleaning, id assignment,
per-map subject binding, rendering), the ideation-turn protocol carrying the
third draft, the seed-seam and enrichment-context injection, the unbound-note
lint, and the brief handoff into an agent build.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_ideation_notes.py
"""

import shutil
import tempfile

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen import notes as notes_mod
from wbworldgen.worldgen.agent.lints import lint_world
from wbworldgen.worldgen.compiler import compile_world
from wbworldgen.worldgen.enrichment.context import build_enrichment_context
from wbworldgen.worldgen.prompts import (
    build_ideation_turn_messages,
    seed_with_scenario,
)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_notes_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _compiled(brief=None):
    """Two-map compiled world: a star map with a named node, and a parallel
    realm map."""
    wd = {
        "maps": {
            "root": {
                "map_id": "root", "label": "The Ember System",
                "level_type": "star_system",
                "nodes": [
                    {"id": "a1", "name": "Kharos", "type": "planet",
                     "importance": 9, "x": 0.0, "y": 0.0},
                    {"id": "a2", "name": "Port Vell", "type": "station",
                     "importance": 7, "x": 1.0, "y": 0.0},
                ],
                "edges": [{"from": "a1", "to": "a2"}],
            },
            "deeps": {
                "map_id": "deeps", "label": "The Drowned Deeps",
                "level_type": "realm",
                "nodes": [
                    {"id": "b1", "name": "Siren Gate", "type": "ruin",
                     "importance": 8, "x": 0.0, "y": 0.0},
                ],
                "edges": [],
            },
        },
        "connections": [],
    }
    if brief is not None:
        wd["brief"] = brief
    return wd


# --- cleaning + ids ----------------------------------------------------------

def test_clean_notes_shapes_and_junk():
    cleaned = notes_mod.clean_notes([
        {"text": "  Water is currency.  ", "subject": ""},
        {"text": "Three moons.", "subject": " Kharos "},
        "Bare string note.",
        {"text": "   "},          # empty text dropped
        {"subject": "Kharos"},    # no text dropped
        42, None, ["nested"],      # junk dropped
    ])
    assert cleaned == [
        {"text": "Water is currency.", "subject": ""},
        {"text": "Three moons.", "subject": "Kharos"},
        {"text": "Bare string note.", "subject": ""},
    ]


def test_clean_notes_carries_amendment_state():
    cleaned = notes_mod.clean_notes([
        {"text": "Amended text", "subject": "Kharos", "id": "n2",
         "status": "amended", "original_text": "Original text",
         "rationale": "why", "no_compromise": True,
         "unknown_key": "dropped"},
    ])
    assert cleaned == [{
        "text": "Amended text", "subject": "Kharos", "id": "n2",
        "status": "amended", "original_text": "Original text",
        "rationale": "why", "no_compromise": True,
    }]


def test_assign_ids_fills_gaps_and_keeps_existing():
    notes = notes_mod.assign_ids([
        {"text": "a", "subject": ""},
        {"text": "b", "subject": "", "id": "n1"},
        {"text": "c", "subject": ""},
    ])
    ids = [n["id"] for n in notes]
    assert ids[1] == "n1"
    assert len(set(ids)) == 3
    assert all(i.startswith("n") for i in ids)


# --- binding -----------------------------------------------------------------

def test_bind_exact_map_label_beats_everything():
    mid, cands = notes_mod.bind_subject("The Drowned Deeps", _compiled())
    assert (mid, cands) == ("deeps", [])


def test_bind_exact_map_id():
    mid, _ = notes_mod.bind_subject("deeps", _compiled())
    assert mid == "deeps"


def test_bind_exact_node_name_gives_its_map():
    mid, _ = notes_mod.bind_subject("Kharos", _compiled())
    assert mid == "root"


def test_bind_containment_on_map_label():
    # Subject containing the full map label binds; a fragment does not.
    mid, _ = notes_mod.bind_subject("the Drowned Deeps region", _compiled())
    assert mid == "deeps"
    mid, cands = notes_mod.bind_subject("the deeps realm", _compiled())
    assert (mid, cands) == (None, [])
    mid, _ = notes_mod.bind_subject("the Ember System's outer belt", _compiled())
    assert mid == "root"


def test_bind_containment_on_node_name():
    mid, _ = notes_mod.bind_subject("the sand planet Kharos", _compiled())
    assert mid == "root"


def test_bind_nothing_matches():
    mid, cands = notes_mod.bind_subject("The Glass Moon", _compiled())
    assert mid is None and cands == []


def test_bind_ambiguous_reports_candidates():
    wd = _compiled()
    wd["maps"]["deeps"]["nodes"].append(
        {"id": "b2", "name": "Kharos Shrine", "type": "shrine",
         "importance": 5, "x": 1.0, "y": 1.0})
    # "Kharos" matches node a1 exactly (root) — exact beats the deeps
    # containment match.
    mid, cands = notes_mod.bind_subject("Kharos", wd)
    assert mid == "root"
    # A containment-only subject matching nodes on both maps is ambiguous.
    wd["maps"]["root"]["nodes"][0]["name"] = "Kharos Prime"
    mid, cands = notes_mod.bind_subject("Kharos", wd)
    assert mid is None and set(cands) == {"root", "deeps"}


def test_bound_notes_and_notes_for_map():
    brief = {"prompt": "p", "rules": [], "notes": [
        {"id": "n1", "text": "Water is currency.", "subject": ""},
        {"id": "n2", "text": "Kharos has three moons.", "subject": "Kharos"},
        {"id": "n3", "text": "The Deeps are lightless.",
         "subject": "The Drowned Deeps"},
        {"id": "n4", "text": "Ghost city fact.", "subject": "The Glass Moon"},
    ]}
    wd = _compiled(brief)
    bound = notes_mod.bound_notes(wd, wd)
    scopes = {n["id"]: n["scope"] for n in bound}
    assert scopes == {"n1": "world", "n2": "map", "n3": "map", "n4": "unbound"}
    assert notes_mod.notes_for_map(wd, wd, "root") == ["Kharos has three moons."]
    assert notes_mod.notes_for_map(wd, wd, "deeps") == ["The Deeps are lightless."]
    assert notes_mod.world_note_texts(wd) == ["Water is currency."]


def test_notes_matching_name_prebinding():
    state = {"brief": {"notes": [
        {"id": "n1", "text": "Lightless.", "subject": "The Drowned Deeps"},
        {"id": "n2", "text": "Moons.", "subject": "Kharos"},
    ]}}
    assert notes_mod.notes_matching_name(state, "Drowned Deeps") == ["Lightless."]
    assert notes_mod.notes_matching_name(state, "Nowhere") == []


# --- rendering + injection ---------------------------------------------------

def test_seed_with_scenario_composes_notes_and_scenario():
    state = {
        "brief": {"notes": [
            {"id": "n1", "text": "Water is currency.", "subject": ""},
            {"id": "n2", "text": "Three moons.", "subject": "Kharos"},
        ]},
        "scenario": "A smuggler arrives.",
    }
    text = seed_with_scenario(state, "seed direction")
    assert text.startswith("seed direction")
    assert "Water is currency." in text
    # Subject notes appear only as an index entry, never in full.
    assert "Kharos (1 note)" in text
    assert "Three moons." not in text
    assert text.index("Water is currency.") < text.index("--- SCENARIO ---")
    # Without a brief the seed passes through untouched.
    assert seed_with_scenario({}, "plain") == "plain"


def test_agent_notes_block_annotates_bindings():
    brief = {"notes": [
        {"id": "n1", "text": "Water is currency.", "subject": ""},
        {"id": "n2", "text": "Three moons.", "subject": "Kharos"},
        {"id": "n3", "text": "Ghost fact.", "subject": "The Glass Moon"},
        {"id": "n4", "text": "Amended fact.", "subject": "The Drowned Deeps",
         "status": "amended", "original_text": "Original fact."},
    ]}
    wd = _compiled(brief)
    block = notes_mod.agent_notes_block(wd, wd)
    assert "[n1] Water is currency." in block
    assert "→ map 'root'" in block
    assert "UNBOUND, nothing matches yet" in block
    assert "amended by compromise, pending the user's review" in block
    assert notes_mod.agent_notes_block({}, {}) == ""


def test_enrichment_context_carries_map_notes():
    brief = {"notes": [
        {"id": "n1", "text": "Three moons hang over Kharos.", "subject": "Kharos"},
        {"id": "n2", "text": "The Deeps are lightless.",
         "subject": "The Drowned Deeps"},
    ]}
    wd = _compiled(brief)
    node = dict(wd["maps"]["root"]["nodes"][1], map_id="root")
    ctx = build_enrichment_context(node, [node], wd)
    assert ctx["notes"] == ["Three moons hang over Kharos."]
    deep_node = dict(wd["maps"]["deeps"]["nodes"][0], map_id="deeps")
    ctx = build_enrichment_context(deep_node, [deep_node], wd)
    assert ctx["notes"] == ["The Deeps are lightless."]


def test_compile_world_carries_brief():
    state = {"seed_prompt": "s", "steps": {},
             "brief": {"prompt": "s", "rules": [], "notes": [
                 {"id": "n1", "text": "t", "subject": ""}]}}
    compiled = compile_world(state)
    assert compiled["brief"] == state["brief"]
    assert "brief" not in compile_world({"seed_prompt": "s", "steps": {}})


# --- lint --------------------------------------------------------------------

def test_lint_flags_unbound_note_only_unscoped():
    brief = {"notes": [
        {"id": "n1", "text": "Bound fine.", "subject": "Kharos"},
        {"id": "n2", "text": "Ghost fact.", "subject": "The Glass Moon"},
    ]}
    wd = _compiled(brief)
    report = lint_world(wd)
    kinds = {(p["kind"], p.get("note_id")) for p in report["problems"]}
    assert ("note_unbound", "n2") in kinds
    assert ("note_unbound", "n1") not in kinds
    # Scoped lints skip note problems: they belong to no single map.
    scoped = lint_world(wd, map_id="root")
    assert not any(p["kind"] == "note_unbound" for p in scoped["problems"])
    # No brief -> no note problems.
    assert not any(p["kind"] == "note_unbound"
                   for p in lint_world(_compiled())["problems"])


# --- ideation protocol -------------------------------------------------------

def test_ideation_messages_carry_notes_draft_and_protocol():
    msgs = build_ideation_turn_messages(
        [{"role": "player", "text": "hi"}],
        prompt_draft="a world",
        rules_draft=["rule one"],
        notes_draft=[
            {"text": "Water is currency.", "subject": ""},
            {"text": "Three moons.", "subject": "Kharos"},
        ])
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert '"notes"' in system and '"subject"' in system
    assert "<current_notes>" in user
    assert "- Water is currency." in user
    assert "- [Kharos] Three moons." in user
    # Empty draft renders the placeholder.
    empty = build_ideation_turn_messages([{"role": "player", "text": "hi"}])
    assert "(none recorded yet)" in empty[1]["content"]


# --- the Go handoff ----------------------------------------------------------

def test_start_agent_build_records_notes_with_ids(tmpdir, monkeypatch):
    from wbworldgen.worldgen.agent import harness

    builder = register_default_steps(WorldBuilder(worlds_dir=tmpdir))

    async def _no_loop(handle):
        handle.status = "cancelled"

    monkeypatch.setattr(harness, "_run_build", _no_loop)
    import asyncio

    async def go():
        handle = harness.start_agent_build(
            builder, "seed", rules=["r1"],
            notes=[{"text": "Water is currency.", "subject": ""},
                   {"text": "Three moons.", "subject": "Kharos"}])
        await handle.task
        return handle

    handle = asyncio.run(go())
    state = builder.load_world(handle.world_id)
    brief = state["brief"]
    assert brief["rules"] == ["r1"]
    assert [n["text"] for n in brief["notes"]] == [
        "Water is currency.", "Three moons."]
    assert all(n.get("id") for n in brief["notes"])
    assert handle.brief == brief
