"""Tests for the codex step (the world's lorebook): registration and
compiled contribution, the codex module's reads and subject binding, the
node-context and expansion injection seams, the evaluator excerpt, and the
codex lints (declared-but-empty domain, undeclared domain, unbound
subject).

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_codex.py
"""

import pytest

from wbworldgen.worldgen import codex as codex_mod
from wbworldgen.worldgen.agent.evaluator import _content_excerpts
from wbworldgen.worldgen.agent.lints import lint_world
from wbworldgen.worldgen.compiler import compile_world
from wbworldgen.worldgen.enrichment.context import build_enrichment_context
from wbworldgen.worldgen.fixtures.mock_data import mock_codex


def _codex(domains=None, entries=None):
    return {"domains": domains or [], "entries": entries or []}


def _compiled(codex=None):
    """Two-map compiled world: a star map with named nodes, and a parallel
    realm map (the ideation-notes fixture shape)."""
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
        "root_map_id": "root",
    }
    if codex is not None:
        wd["codex"] = codex
    return wd


# --- step + compile ----------------------------------------------------------

def test_codex_step_is_registered_after_lore():
    from wbworldgen.worldgen import WorldBuilder, register_default_steps
    import shutil, tempfile
    d = tempfile.mkdtemp(prefix="wb_codex_")
    try:
        wb = register_default_steps(WorldBuilder(worlds_dir=d))
        order = wb._ordered_ids
        assert order.index("codex") == order.index("lore") + 1
        assert order.index("codex") < order.index("hierarchy_design")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_compile_copies_codex_through():
    from wbworldgen.worldgen.steps.codex import CodexStep
    state = {"steps": {"codex": {"data": mock_codex("p")}}}
    compiled = compile_world(state, steps={"codex": CodexStep()})
    assert compiled["codex"]["domains"] == mock_codex("p")["domains"]
    assert len(compiled["codex"]["entries"]) == 2


def test_compile_without_codex_has_no_key():
    from wbworldgen.worldgen.steps.codex import CodexStep
    compiled = compile_world({"steps": {}}, steps={"codex": CodexStep()})
    assert "codex" not in compiled


def test_codex_data_reads_both_shapes():
    data = mock_codex("p")
    assert codex_mod.codex_data({"codex": data}) is data
    assert codex_mod.codex_data({"steps": {"codex": {"data": data}}}) is data
    assert codex_mod.codex_data({}) == {}


# --- reads + binding ---------------------------------------------------------

def test_entries_cleaning_drops_junk():
    compiled = _compiled(_codex(entries=[
        {"domain": "magic", "name": "Soulforging", "summary": "s", "details": "d"},
        {"domain": "magic", "name": "", "summary": "no name"},       # dropped
        {"domain": "magic", "name": "Empty body"},                    # dropped
        "not a dict", 42,                                             # dropped
        {"name": "Domainless", "summary": "kept"},
    ]))
    cleaned = codex_mod.entries(compiled)
    assert [e["name"] for e in cleaned] == ["Soulforging", "Domainless"]


def test_binding_world_map_and_unbound():
    compiled = _compiled(_codex(entries=[
        {"domain": "magic", "name": "Soulforging", "summary": "s"},
        {"domain": "biology", "name": "Deep Song", "summary": "s",
         "subject": "The Drowned Deeps"},
        {"domain": "biology", "name": "Sand Wyrms", "summary": "s",
         "subject": "Kharos"},
        {"domain": "biology", "name": "Ghost Kelp", "summary": "s",
         "subject": "The Sunless Sea"},
    ]))
    bound = {e["name"]: e for e in codex_mod.bound_entries(compiled)}
    assert bound["Soulforging"]["scope"] == "world"
    assert (bound["Deep Song"]["scope"], bound["Deep Song"]["map_id"]) == ("map", "deeps")
    # A named node's subject binds to its map.
    assert (bound["Sand Wyrms"]["scope"], bound["Sand Wyrms"]["map_id"]) == ("map", "root")
    assert bound["Ghost Kelp"]["scope"] == "unbound"


def test_node_context_block_scoping():
    compiled = _compiled(_codex(entries=[
        {"domain": "magic", "name": "Soulforging", "summary": "the short",
         "details": "the long"},
        {"domain": "biology", "name": "Deep Song", "summary": "s",
         "details": "full deeps lore", "subject": "The Drowned Deeps"},
    ]))
    root_block = codex_mod.node_context_block(compiled, "root")
    # World entries ride by summary; the deeps-bound entry stays out.
    assert root_block == [{"domain": "magic", "name": "Soulforging",
                           "summary": "the short"}]
    deeps_block = codex_mod.node_context_block(compiled, "deeps")
    assert {e["name"] for e in deeps_block} == {"Soulforging", "Deep Song"}
    local = next(e for e in deeps_block if e["name"] == "Deep Song")
    assert local["details"] == "full deeps lore"


def test_enrichment_context_carries_codex():
    compiled = _compiled(_codex(entries=[
        {"domain": "magic", "name": "Soulforging", "summary": "s"}]))
    node = compiled["maps"]["root"]["nodes"][0]
    ctx = build_enrichment_context(node, [], compiled)
    assert ctx["codex"] == [{"domain": "magic", "name": "Soulforging",
                             "summary": "s"}]
    # No codex, no key.
    node2 = _compiled()["maps"]["root"]["nodes"][0]
    assert "codex" not in build_enrichment_context(node2, [], _compiled())


def test_entries_matching_name_prebinding():
    compiled = _compiled(_codex(entries=[
        {"domain": "biology", "name": "Sand Wyrms", "summary": "burrowers",
         "subject": "Kharos"},
        {"domain": "magic", "name": "Soulforging", "summary": "world-wide"},
    ]))
    matched = codex_mod.entries_matching_name(compiled, "Kharos")
    assert matched == ["Sand Wyrms (biology): burrowers"]
    assert codex_mod.entries_matching_name(compiled, "Port Vell") == []
    assert codex_mod.entries_matching_name(compiled, "") == []


# --- evaluator excerpt -------------------------------------------------------

def test_evaluator_excerpt_leads_with_codex():
    compiled = _compiled(_codex(
        domains=[{"name": "magic", "reason": "r"}],
        entries=[{"domain": "magic", "name": "Soulforging",
                  "summary": "s", "details": "d"}]))
    excerpt = _content_excerpts(compiled)
    assert excerpt.splitlines()[0].startswith("Codex")
    assert "Soulforging: s d" in excerpt
    # Without a codex the excerpt keeps its old head.
    assert _content_excerpts(_compiled()).startswith("Map ")


# --- lints -------------------------------------------------------------------

def _kinds(compiled):
    return [p["kind"] for p in lint_world(compiled)["problems"]]


def test_lint_clean_codex():
    compiled = _compiled(_codex(
        domains=[{"name": "magic", "reason": "r"}],
        entries=[{"domain": "magic", "name": "Soulforging", "summary": "s"}]))
    assert not [k for k in _kinds(compiled) if k.startswith("codex")]
    assert not [k for k in _kinds(_compiled()) if k.startswith("codex")]


def test_lint_declared_domain_with_no_entries():
    compiled = _compiled(_codex(domains=[{"name": "magic", "reason": "r"}]))
    problems = codex_mod.lint_codex(compiled)
    assert [p["kind"] for p in problems] == ["codex_domain_empty"]
    assert problems[0]["domain"] == "magic"
    assert "codex_domain_empty" in _kinds(compiled)


def test_lint_undeclared_domain():
    compiled = _compiled(_codex(
        domains=[{"name": "magic", "reason": "r"}],
        entries=[{"domain": "magic", "name": "Soulforging", "summary": "s"},
                 {"domain": "cuisine", "name": "Spore Bread", "summary": "s"}]))
    problems = codex_mod.lint_codex(compiled)
    assert [p["kind"] for p in problems] == ["codex_unknown_domain"]
    assert problems[0]["entry"] == "Spore Bread"


def test_lint_domain_matching_is_join_key_tolerant():
    compiled = _compiled(_codex(
        domains=[{"name": "The Magic", "reason": "r"}],
        entries=[{"domain": "magic", "name": "Soulforging", "summary": "s"}]))
    assert codex_mod.lint_codex(compiled) == []


def test_lint_unbound_and_ambiguous_subjects():
    compiled = _compiled(_codex(entries=[
        {"domain": "", "name": "Ghost Kelp", "summary": "s",
         "subject": "The Sunless Sea"}]))
    problems = codex_mod.lint_codex(compiled)
    assert [p["kind"] for p in problems] == ["codex_unbound"]
    assert "nothing in the world matches" in problems[0]["message"]

    # Ambiguity: a subject matching two maps in the same tier.
    ambiguous = _compiled(_codex(entries=[
        {"domain": "", "name": "Dual", "summary": "s", "subject": "The"}]))
    ambiguous["maps"]["root"]["label"] = "The Ember"
    ambiguous["maps"]["deeps"]["label"] = "The Deeps"
    problems = codex_mod.lint_codex(ambiguous)
    # "The" is under the containment length guard, so it binds nothing.
    assert [p["kind"] for p in problems] == ["codex_unbound"]


def test_lint_scoped_run_skips_codex():
    compiled = _compiled(_codex(domains=[{"name": "magic", "reason": "r"}]))
    scoped = lint_world(compiled, map_id="root")
    assert not [p for p in scoped["problems"]
                if p["kind"].startswith("codex")]


def test_mock_codex_lints_clean():
    compiled = _compiled(mock_codex("prompt"))
    assert not [k for k in _kinds(compiled) if k.startswith("codex")]
