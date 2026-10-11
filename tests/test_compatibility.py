import json
from dataclasses import replace

import httpx
import pytest
from conftest import completed
from fastapi.testclient import TestClient
from openai import OpenAI
from pydantic import BaseModel

from chatgpt_gateway.app import create_app
from chatgpt_gateway.config import Settings

PINS = {
    "only": ["openai"],
    "allow_fallbacks": False,
    "require_parameters": True,
    "data_collection": "deny",
}
REQUEST = {"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "Hi"}]}
SEARCH = {"type": "openrouter:web_search", "parameters": {"engine": "native"}}


def test_policy_override_is_disabled_by_default(client, backend):
    response = client.post("/api/v1/chat/completions", json={**REQUEST, "provider": PINS})
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "provider.data_collection"
    assert not backend.requests
    assert (
        client.get("/gateway/capabilities").json()["openrouter_data_policy_override_enabled"]
        is False
    )


def test_explicit_override_is_visible_and_other_pins_still_hold(settings, store, backend):
    app = create_app(
        replace(settings, accept_chatgpt_data_controls=True),
        transport=httpx.MockTransport(backend.handler),
    )
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        response = client.post("/api/v1/chat/completions", json={**REQUEST, "provider": PINS})
        assert response.status_code == 200
        assert response.headers["x-gateway-policy-override"] == "provider.data_collection"
        assert response.headers["x-gateway-data-controls"] == "chatgpt_account"
        assert response.json()["provider"] == "chatgpt_plan"
        assert all(item["data_policy_override"] is True for item in store.recent())
        for pin in (
            {"only": ["anthropic"]},
            {"allow_fallbacks": True},
            {"data_collection": "typo"},
        ):
            assert (
                client.post(
                    "/api/v1/chat/completions", json={**REQUEST, "provider": {**PINS, **pin}}
                ).status_code
                == 400
            )


@pytest.mark.parametrize("value", ["true", "false", "yes", "bad"])
def test_policy_setting_requires_explicit_zero_or_one(monkeypatch, value):
    monkeypatch.setenv("CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS", value)
    with pytest.raises(ValueError, match="must be 0 or 1"):
        Settings.from_env()


def test_search_translation_preserves_real_tool_evidence_and_sdk_schema(client, backend, store):
    class Answer(BaseModel):
        answer: str

    result = completed('{"answer":"yes"}')
    result["output"].insert(0, {"type": "web_search_call", "id": "search1", "status": "completed"})
    result["output"][1]["content"][0]["annotations"] = [
        {
            "type": "url_citation",
            "url": "https://example.test",
            "title": "Source",
            "start_index": 0,
            "end_index": 3,
        }
    ]
    backend.events = [{"type": "response.completed", "response": result}]
    sdk = OpenAI(
        base_url=f"{settings_url(client)}/api/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=client,
    )
    raw = sdk.chat.completions.with_raw_response.parse(
        model="gpt-5.6-terra",
        messages=REQUEST["messages"],
        response_format=Answer,
        extra_body={"tools": [SEARCH]},
    )
    assert raw.parse().choices[0].message.parsed.answer == "yes"
    payload = json.loads(raw.content)
    assert payload["usage"]["server_tool_use_details"]["web_search_requests"] == 1
    assert (
        payload["choices"][0]["message"]["annotations"][0]["url_citation"]["url"]
        == "https://example.test"
    )
    post = next(r for r in backend.requests if r.method == "POST")
    assert json.loads(post.content)["tools"] == [{"type": "web_search"}]


def settings_url(client):
    return str(client.base_url).rstrip("/")


@pytest.mark.parametrize(
    "tools",
    [
        [{**SEARCH, "parameters": {"engine": "exa"}}],
        [{**SEARCH, "parameters": {"engine": "native", "max_results": 1}}],
        [SEARCH, SEARCH],
        [{"type": "function"}],
        "bad",
        "",
        {},
        False,
    ],
)
def test_unsupported_tool_semantics_are_not_dropped(client, backend, tools):
    response = client.post("/api/v1/chat/completions", json={**REQUEST, "tools": tools})
    assert response.status_code == 400 and not backend.requests


def test_search_request_does_not_fabricate_execution(client, backend):
    response = client.post("/api/v1/chat/completions", json={**REQUEST, "tools": [SEARCH]})
    assert response.status_code == 200
    assert "server_tool_use_details" not in response.json()["usage"]


def test_search_evidence_survives_missing_usage_without_inventing_token_counts(client, backend):
    result = completed(usage=None)
    result["output"].insert(0, {"type": "web_search_call", "id": "search1", "status": "completed"})
    backend.events = [{"type": "response.completed", "response": result}]
    response = client.post("/api/v1/chat/completions", json={**REQUEST, "tools": [SEARCH]})
    assert response.json()["usage"] == {"server_tool_use_details": {"web_search_requests": 1}}
