"""Tests for the v2e research tool: the availability-gated ``web_search``
catalog entry over the engine's search slot. Covers the gate itself
(``ToolSpec.available`` + ``unavailable_tool_ids``), the loud invoke guard,
result/failure shaping, and every surface that must honor the gate — the
build system prompt, the chat catalog (``chat_tool_ids``), the note
verifier's mechanical carve, and the read_catalog tool. No tokens and no
network: the LLM side is a hand-built fake with the ``search_available`` /
``web_search`` contract.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests):
python -m pytest modules/wb_worldgen/test_agent_research.py
"""

import asyncio
import shutil
import tempfile
import types

import pytest

from wbworldgen.worldgen import WorldBuilder, register_default_steps
from wbworldgen.worldgen.agent import harness
from wbworldgen.worldgen.agent.registry import (
    ToolContext,
    ToolError,
    get_tool,
    invoke_tool,
    unavailable_tool_ids,
)
from wbworldgen.worldgen.agent.verifier import verifier_tool_ids


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="wb_research_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def builder(tmpdir):
    return register_default_steps(WorldBuilder(worlds_dir=tmpdir))


class FakeSearchLLM:
    """The engine contract the tool depends on: ``search_available()`` and
    ``web_search(**kwargs)``."""

    def __init__(self, available=True, result=None, error=None):
        self.mode = "live"
        self.available = available
        self.error = error
        self.result = result if result is not None else {
            "answer": "Arrakis is a desert planet; its deep desert is "
                      "ruled by the Fremen.",
            "sources": [{"title": "Dune Wiki", "url": "https://dune.fandom.com/a",
                         "excerpt": "Arrakis, also known as Dune..."}],
            "provider": "openrouter",
            "model": "openrouter/meta-llama/llama-4-maverick",
        }
        self.calls = []

    def search_available(self):
        return self.available

    async def web_search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return dict(self.result)


def _world(builder, world_id="research_world"):
    return builder.save_world(world_id, {
        "seed_prompt": "a world of dunes",
        "steps": {"world_rules": {"data": {"genre": "sci-fi",
                                           "custom_rules": ["Sand is law."]},
                  "approved": True}},
    })


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_web_search_is_a_read_only_gated_catalog_entry():
    spec = get_tool("web_search")
    assert spec.mutates is False
    assert spec.available is not None
    assert spec.params["query"]["required"] is True


def test_unavailable_without_a_search_capable_llm(builder):
    # The mock-path builder has no live LLM wired at all.
    assert "web_search" in unavailable_tool_ids(builder.services)

    builder.services.llm = FakeSearchLLM(available=False)
    assert "web_search" in unavailable_tool_ids(builder.services)

    builder.services.llm = FakeSearchLLM(available=True)
    assert "web_search" not in unavailable_tool_ids(builder.services)


def test_invoke_rejects_loudly_when_unavailable(builder):
    wid = _world(builder)
    ctx = ToolContext(builder=builder, world_id=wid)
    with pytest.raises(ToolError, match="not available"):
        run(invoke_tool(ctx, "web_search", {"query": "anything"}))


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def test_happy_path_result_shape_and_llm_args(builder):
    wid = _world(builder)
    fake = FakeSearchLLM()
    builder.services.llm = fake
    ctx = ToolContext(builder=builder, world_id=wid)

    result = run(invoke_tool(ctx, "web_search", {
        "query": "  Arrakis ecology  ", "max_results": 3,
        "include_domains": ["dune.fandom.com"]}))

    assert result["query"] == "Arrakis ecology"
    assert result["answer"].startswith("Arrakis is a desert planet")
    assert result["sources"][0]["url"] == "https://dune.fandom.com/a"
    assert "note" not in result

    call = fake.calls[0]
    assert call["query"] == "  Arrakis ecology  "
    assert call["max_results"] == 3
    assert call["include_domains"] == ["dune.fandom.com"]
    assert call["inspector_ctx"]["module_source"] == "wb_worldgen"


def test_llm_failures_become_tool_errors(builder):
    wid = _world(builder)
    builder.services.llm = FakeSearchLLM(error=RuntimeError("429 rate limited"))
    ctx = ToolContext(builder=builder, world_id=wid)
    with pytest.raises(ToolError, match="web_search failed: 429"):
        run(invoke_tool(ctx, "web_search", {"query": "anything"}))


def test_empty_results_carry_an_honest_note(builder):
    wid = _world(builder)
    builder.services.llm = FakeSearchLLM(result={"answer": "", "sources": []})
    ctx = ToolContext(builder=builder, world_id=wid)
    result = run(invoke_tool(ctx, "web_search", {"query": "gibberish"}))
    assert "nothing usable" in result["note"]


# ---------------------------------------------------------------------------
# The surfaces that honor the gate
# ---------------------------------------------------------------------------

def _fake_handle(builder, wid):
    return types.SimpleNamespace(builder=builder, world_id=wid,
                                 seed_prompt="a world of dunes",
                                 turns=1, tool_calls=0, todo=[], recent=[],
                                 log=[])


def test_build_system_prompt_gates_web_search(builder):
    wid = _world(builder)
    world_state = builder.load_world(wid)
    budgets = {"max_turns": 40, "max_tool_calls": 60, "fix_rounds": 3}
    handle = _fake_handle(builder, wid)

    prompt = harness._system_prompt(handle, world_state, budgets)
    assert "web_search" not in prompt

    builder.services.llm = FakeSearchLLM()
    prompt = harness._system_prompt(handle, world_state, budgets)
    assert "**web_search**" in prompt
    assert "web_search is available" in prompt  # the guidance bullet


def test_chat_catalog_gates_web_search(builder):
    wid = _world(builder)
    world_state = builder.load_world(wid)

    ids = harness.chat_tool_ids(builder)
    assert ids == harness.CHAT_TOOL_IDS
    prompt = harness._chat_system_prompt(world_state, ids)
    assert "web_search" not in prompt

    builder.services.llm = FakeSearchLLM()
    ids = harness.chat_tool_ids(builder)
    assert ids == harness.CHAT_TOOL_IDS + ("web_search",)
    prompt = harness._chat_system_prompt(world_state, ids)
    assert "web_search" in prompt
    assert "You can research" in prompt


def test_verifier_carve_respects_availability(builder):
    # The pure registry carve (no services): mechanically included.
    assert "web_search" in verifier_tool_ids()
    # Through the actual wiring: gated like every other surface.
    assert "web_search" not in verifier_tool_ids(builder.services)
    builder.services.llm = FakeSearchLLM()
    assert "web_search" in verifier_tool_ids(builder.services)


def test_read_catalog_filters_unavailable_tools(builder):
    wid = _world(builder)
    ctx = ToolContext(builder=builder, world_id=wid)

    markdown = run(invoke_tool(ctx, "read_catalog", {}))["markdown"]
    assert "web_search" not in markdown

    builder.services.llm = FakeSearchLLM()
    markdown = run(invoke_tool(ctx, "read_catalog", {}))["markdown"]
    assert "web_search" in markdown
