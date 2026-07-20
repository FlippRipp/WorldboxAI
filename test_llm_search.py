"""The engine's search slot: provider ``supports_search`` declarations, the
per-provider ``search_enabled`` toggle, ``LLMService.search_available`` and
the OpenRouter web-plugin ``web_search`` call (worldgen v2e).

``acompletion`` is patched at the module attribute llm.py imported, so no
network and no tokens are ever involved.
"""
import asyncio
import types

import pytest

import backend.engine.llm as llm_mod
from backend.engine.llm import LLMProviderError, LLMService
from backend.engine.provider_manager import ProviderManager
from backend.engine.providers import PROVIDERS


# ------------------------------------------------------------- declarations

def test_every_provider_declares_search_support():
    for pid, pdef in PROVIDERS.items():
        assert isinstance(pdef.get("supports_search"), bool), (
            f"provider '{pid}' must declare supports_search explicitly")
    assert PROVIDERS["openrouter"]["supports_search"] is True


def test_search_toggle_field_only_on_supporting_providers():
    for pid, pdef in PROVIDERS.items():
        has_field = "search_enabled" in pdef["fields"]
        assert has_field == pdef["supports_search"], (
            f"provider '{pid}': the search_enabled field must exist exactly "
            "when the provider supports search (save_config whitelists by "
            "fields, so a non-supporting provider must not carry the key)")
    fdef = PROVIDERS["openrouter"]["fields"]["search_enabled"]
    assert fdef["type"] == "toggle"
    assert fdef["default"] is True


def test_provider_payload_carries_supports_search(tmp_path):
    manager = ProviderManager(providers_dir=str(tmp_path))
    payload = {p["id"]: p for p in manager.get_all()}
    for pid, pdef in PROVIDERS.items():
        assert payload[pid]["supports_search"] == pdef["supports_search"]


# --------------------------------------------------------- search_available

def _openrouter_config(**overrides):
    config = {
        "storyteller_model": "openrouter/anthropic/claude-sonnet-4-20250514",
        "reader_model": "openrouter/meta-llama/llama-4-maverick",
        "module_fast_model": "openrouter/meta-llama/llama-4-maverick",
    }
    config.update(overrides)
    return config


def test_search_available_truth_table():
    service = LLMService(mode="live")
    assert not service.search_available()  # nothing configured yet

    service.reconfigure("openrouter", _openrouter_config())
    assert service.search_available()  # toggle defaults on

    service.reconfigure("openrouter", _openrouter_config(search_enabled=False))
    assert not service.search_available()

    service.reconfigure("openrouter", _openrouter_config(search_enabled="false"))
    assert not service.search_available()

    service.reconfigure("openrouter", _openrouter_config(search_enabled=True))
    assert service.search_available()

    service.reconfigure("gemini", {"reader_model": "gemini/gemini-2.5-flash"})
    assert not service.search_available()  # provider has no search slot


def test_search_unavailable_in_mock_mode():
    service = LLMService(mode="mock")
    service.reconfigure("openrouter", _openrouter_config())
    assert not service.search_available()


def test_web_search_unavailable_reasons():
    service = LLMService(mode="live")
    service.reconfigure("gemini", {"reader_model": "gemini/gemini-2.5-flash"})
    with pytest.raises(LLMProviderError, match="no web search integration"):
        asyncio.run(service.web_search("anything"))

    service.reconfigure("openrouter", _openrouter_config(search_enabled=False))
    with pytest.raises(LLMProviderError, match="toggled off"):
        asyncio.run(service.web_search("anything"))

    mock_service = LLMService(mode="mock")
    mock_service.reconfigure("openrouter", _openrouter_config())
    with pytest.raises(LLMProviderError, match="mock"):
        asyncio.run(mock_service.web_search("anything"))


# ------------------------------------------------------------- the web call

def _fake_response(content="Grounded answer.", annotations=None):
    message = types.SimpleNamespace(content=content,
                                    annotations=annotations or [])
    usage = types.SimpleNamespace(
        to_dict=lambda: {"prompt_tokens": 11, "completion_tokens": 7,
                         "total_tokens": 18})
    choice = types.SimpleNamespace(message=message, finish_reason="stop")
    return types.SimpleNamespace(choices=[choice], usage=usage)


def _capture_acompletion(monkeypatch, response):
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(llm_mod, "acompletion", fake_acompletion)
    return calls


def test_web_search_openrouter_call_and_result(monkeypatch):
    annotations = [
        {"type": "url_citation",
         "url_citation": {"url": "https://a.example/one",
                          "title": "Article One", "content": "excerpt one"}},
        # Typed-object shape (litellm sometimes parses annotations).
        types.SimpleNamespace(url_citation=types.SimpleNamespace(
            url="https://b.example/two", title="", content="excerpt two")),
        # No URL -> skipped, never fails the search.
        {"type": "url_citation", "url_citation": {"title": "no url"}},
    ]
    calls = _capture_acompletion(monkeypatch, _fake_response(annotations=annotations))

    service = LLMService(mode="live")
    service.reconfigure(
        "openrouter", _openrouter_config(openrouter_fast_provider="deepseek"))
    result = asyncio.run(service.web_search(
        "  Night City districts  ", max_results=50,
        include_domains=["cyberpunk.fandom.com", " ", ""]))

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["model"] == "openrouter/meta-llama/llama-4-maverick"
    assert kwargs["temperature"] == 0.3
    plugin = kwargs["extra_body"]["plugins"][0]
    assert plugin["id"] == "web"
    assert plugin["engine"] == "exa"
    assert plugin["max_results"] == 10  # clamped to the included tier
    assert plugin["include_domains"] == ["cyberpunk.fandom.com"]
    # The per-slot OpenRouter route pin rides the same extra_body.
    assert kwargs["extra_body"]["provider"] == {"order": ["deepseek"]}
    assert kwargs["messages"][1] == {"role": "user",
                                     "content": "Night City districts"}

    assert result["answer"] == "Grounded answer."
    assert result["provider"] == "openrouter"
    assert result["model"] == "openrouter/meta-llama/llama-4-maverick"
    assert result["sources"] == [
        {"title": "Article One", "url": "https://a.example/one",
         "excerpt": "excerpt one"},
        {"title": "https://b.example/two", "url": "https://b.example/two",
         "excerpt": "excerpt two"},
    ]


def test_web_search_defaults_and_no_domains(monkeypatch):
    calls = _capture_acompletion(monkeypatch, _fake_response())
    service = LLMService(mode="live")
    service.reconfigure("openrouter", _openrouter_config())
    result = asyncio.run(service.web_search("dune fremen culture"))

    plugin = calls[0]["extra_body"]["plugins"][0]
    assert plugin["max_results"] == 5
    assert "include_domains" not in plugin
    assert "provider" not in calls[0]["extra_body"]  # no route pin configured
    assert result["sources"] == []


def test_web_search_rejects_empty_query(monkeypatch):
    calls = _capture_acompletion(monkeypatch, _fake_response())
    service = LLMService(mode="live")
    service.reconfigure("openrouter", _openrouter_config())
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(service.web_search("   "))
    assert not calls


def test_web_search_propagates_provider_errors(monkeypatch):
    async def failing_acompletion(**kwargs):
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(llm_mod, "acompletion", failing_acompletion)
    service = LLMService(mode="live")
    service.reconfigure("openrouter", _openrouter_config())
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(service.web_search("anything"))
