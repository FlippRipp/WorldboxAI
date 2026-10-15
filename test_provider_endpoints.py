import asyncio

import httpx
import pytest

from backend.engine.provider_manager import ProviderManager


def mock_openrouter(monkeypatch, handler):
    requests = []

    def respond(request):
        requests.append(request)
        return handler(request)

    async_client = httpx.AsyncClient

    def make_client(**kwargs):
        return async_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return requests


def test_endpoints_include_ai_studio_and_preserve_variants(tmp_path, monkeypatch):
    endpoints = [
        {"provider_name": "Google AI Studio", "tag": "google-ai-studio/flex"},
        {"provider_name": "Google", "tag": "google-vertex/global/priority"},
        {"provider_name": "Google AI Studio", "tag": "google-ai-studio"},
        {"provider_name": "Google", "tag": "google-vertex/global"},
        {"provider_name": "Google AI Studio", "tag": "google-ai-studio"},
    ]
    requests = mock_openrouter(monkeypatch, lambda _: httpx.Response(200, json={"data": {"endpoints": endpoints}}))
    manager = ProviderManager(str(tmp_path))

    result = asyncio.run(manager.fetch_openrouter_endpoints("google/gemini-2.5-pro"))

    assert result == {"providers": [
        {"id": "google-vertex/global", "label": "Google (global)"},
        {"id": "google-vertex/global/priority", "label": "Google (global/priority)"},
        {"id": "google-ai-studio", "label": "Google AI Studio"},
        {"id": "google-ai-studio/flex", "label": "Google AI Studio (flex)"},
    ], "error": None}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/models/google/gemini-2.5-pro/endpoints"
    assert "authorization" not in requests[0].headers


def test_model_id_normalization_and_safe_path_encoding(tmp_path, monkeypatch):
    requests = mock_openrouter(monkeypatch, lambda _: httpx.Response(200, json={"data": {"endpoints": []}}))
    manager = ProviderManager(str(tmp_path))

    result = asyncio.run(manager.fetch_openrouter_endpoints("  openrouter/author/model:free?query#fragment  "))

    assert result == {"providers": [], "error": None}
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/models/author/model%3Afree%3Fquery%23fragment/endpoints"


@pytest.mark.parametrize("model", ["openrouter/auto", "openrouter/openrouter/auto"])
def test_openrouter_authored_model_keeps_its_author(tmp_path, monkeypatch, model):
    requests = mock_openrouter(monkeypatch, lambda _: httpx.Response(200, json={"data": {"endpoints": []}}))

    result = asyncio.run(ProviderManager(str(tmp_path)).fetch_openrouter_endpoints(model))

    assert result == {"providers": [], "error": None}
    assert requests[0].url.path == "/api/v1/models/openrouter/auto/endpoints"


@pytest.mark.parametrize("model", ["", "google", "google/", "/gemini", "google/gemini/extra", "../model", "google/..", None])
def test_invalid_model_ids_do_not_make_requests(tmp_path, monkeypatch, model):
    requests = mock_openrouter(monkeypatch, lambda _: pytest.fail("Invalid model must not be requested"))

    result = asyncio.run(ProviderManager(str(tmp_path)).fetch_openrouter_endpoints(model))

    assert result["providers"] == []
    assert "author/model" in result["error"]
    assert requests == []


@pytest.mark.parametrize("payload", [
    None, [], {}, {"data": []}, {"data": {}}, {"data": {"endpoints": {}}},
    {"data": {"endpoints": [None]}},
    {"data": {"endpoints": [{"provider_name": "Google AI Studio"}]}},
    {"data": {"endpoints": [{"provider_name": "Google AI Studio", "tag": ""}]}},
    {"data": {"endpoints": [{"provider_name": 123, "tag": "google-ai-studio"}]}},
])
def test_malformed_responses_show_an_error(tmp_path, monkeypatch, payload):
    mock_openrouter(monkeypatch, lambda _: httpx.Response(200, json=payload))

    result = asyncio.run(ProviderManager(str(tmp_path)).fetch_openrouter_endpoints("google/gemini"))

    assert result["providers"] == []
    assert result["error"]


def test_invalid_json_shows_an_error(tmp_path, monkeypatch):
    mock_openrouter(monkeypatch, lambda _: httpx.Response(200, text="not JSON"))

    result = asyncio.run(ProviderManager(str(tmp_path)).fetch_openrouter_endpoints("google/gemini"))

    assert result["providers"] == []
    assert result["error"]


@pytest.mark.parametrize("status", [404, 429, 503])
def test_http_failures_are_not_empty_successes(tmp_path, monkeypatch, status):
    mock_openrouter(monkeypatch, lambda _: httpx.Response(status, json={"data": {"endpoints": []}}))

    result = asyncio.run(ProviderManager(str(tmp_path)).fetch_openrouter_endpoints("google/gemini"))

    assert result["providers"] == []
    assert str(status) in result["error"]


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_request_failures_have_a_visible_error(tmp_path, monkeypatch, error_type):
    def fail(request):
        raise error_type("", request=request)

    mock_openrouter(monkeypatch, fail)
    result = asyncio.run(ProviderManager(str(tmp_path)).fetch_openrouter_endpoints("google/gemini"))

    assert result["providers"] == []
    assert error_type.__name__ in result["error"]


def test_model_catalogue_does_not_mislabel_authors_as_providers(tmp_path, monkeypatch):
    mock_openrouter(monkeypatch, lambda _: httpx.Response(200, json={"data": [
        {"id": "google/gemini-2.5-pro", "architecture": {"output_modalities": ["text"]}},
    ]}))
    manager = ProviderManager(str(tmp_path))
    monkeypatch.setattr(manager, "get_config", lambda _: {"api_key": "unused"})

    result = asyncio.run(manager.fetch_models("openrouter"))

    assert result == {"models": [{"id": "google/gemini-2.5-pro", "is_embedding": False, "provider": None}], "error": None}


def test_api_returns_endpoints_for_the_requested_model(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import backend.api.server as server

    requests = mock_openrouter(monkeypatch, lambda _: httpx.Response(200, json={"data": {"endpoints": [
        {"provider_name": "Google AI Studio", "tag": "google-ai-studio"},
    ]}}))
    monkeypatch.setattr(server, "provider_manager", ProviderManager(str(tmp_path)))

    response = TestClient(server.app).get("/api/providers/openrouter/endpoints", params={"model": "openrouter/google/gemini-2.5-pro"})

    assert response.status_code == 200
    assert response.json() == {"providers": [{"id": "google-ai-studio", "label": "Google AI Studio"}], "error": None}
    assert requests[0].url.path == "/api/v1/models/google/gemini-2.5-pro/endpoints"
