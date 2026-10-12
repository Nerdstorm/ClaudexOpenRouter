import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from pydantic import BaseModel, Field

from chatgpt_gateway.anthropic_protocol import CLAUDE_MODELS, claude_request, claude_usage
from chatgpt_gateway.anthropic_upstream import ClaudeClient
from chatgpt_gateway.app import create_app
from chatgpt_gateway.config import Settings
from chatgpt_gateway.errors import GatewayError

MODEL = "anthropic/claude-sonnet-5.5"
REQUEST = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Stable instructions, not recipe-specific."},
        {"role": "user", "content": "A new question."},
    ],
}
MARKER = {"type": "ephemeral", "ttl": "1h"}


def message_events(text="Hello", model="claude-sonnet-5-5", stop="end_turn", usage=None):
    initial_usage = (
        {
            "input_tokens": 10,
            "output_tokens": 1,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0},
        }
        if usage is None
        else usage
    )
    return [
        {
            "type": "message_start",
            "message": {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": initial_usage,
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


@dataclass
class ClaudeBackend:
    events: list | None = None
    status: int = 200
    text: str = "Hello"
    error_type: str = "rate_limit_error"
    error_message: str = "Limited"
    available: list = field(default_factory=lambda: [v[0] for v in CLAUDE_MODELS.values()])
    requests: list = field(default_factory=list)

    def handler(self, request):
        assert request.url.host == "api.anthropic.com"
        assert request.headers["x-api-key"] == "test-anthropic-secret"
        assert "authorization" not in request.headers
        self.requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": native,
                            "type": "model",
                            "display_name": native,
                            "created_at": "2026-10-01T00:00:00Z",
                        }
                        for native in self.available
                    ],
                    "has_more": False,
                    "first_id": "first",
                    "last_id": "last",
                },
            )
        assert request.url.path == "/v1/messages"
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={
                    "type": "error",
                    "error": {"type": self.error_type, "message": self.error_message},
                },
                headers={"request-id": "claude-id", "retry-after": "7"},
            )
        model = json.loads(request.content)["model"]
        events = self.events if self.events is not None else message_events(self.text, model=model)
        return httpx.Response(
            200,
            content="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events),
            headers={"content-type": "text/event-stream", "request-id": "claude-id"},
        )


@pytest.fixture
def claude_backend():
    return ClaudeBackend()


@pytest.fixture
def claude_client(settings, store, backend, claude_backend, request):
    configured = replace(
        settings,
        anthropic_api_key="test-anthropic-secret",
        anthropic_structured_output=getattr(request, "param", "native"),
    )

    def handle(request):
        if request.url.host == "api.anthropic.com":
            return claude_backend.handler(request)
        return backend.handler(request)

    app = create_app(configured, transport=httpx.MockTransport(handle))
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        yield client


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/api/v1/chat/completions"])
@pytest.mark.parametrize("model", list(CLAUDE_MODELS))
def test_claude_models_use_anthropic_without_openai_credentials(
    claude_client, claude_backend, backend, store, path, model
):
    response = claude_client.post(path, json={**REQUEST, "model": model})
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "anthropic"
    assert response.headers["x-gateway-billing-source"] == "anthropic_api"
    data = response.json()
    assert data["provider"] == "anthropic" and data["model"] == model
    assert data["choices"][0]["message"]["content"] == "Hello"
    assert data["usage"]["prompt_tokens"] == 3010
    assert data["usage"]["total_tokens"] == 3013
    assert data["usage"]["prompt_tokens_details"]["cached_tokens"] == 2000
    assert data["usage"]["prompt_tokens_details"]["cache_write_tokens"] == 1000
    assert not backend.requests
    body = json.loads(claude_backend.requests[-1].content)
    assert body["model"] == CLAUDE_MODELS[model][0]
    assert body["system"][0]["text"] == REQUEST["messages"][0]["content"]
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    events = store.recent()
    assert [e["event"] for e in events] == ["started", "completed"]
    assert events[0]["cache_policy"]["block_ttls"] == ["5m"]
    assert events[1]["first_token_ms"] is not None
    assert events[1]["upstream_request_id"] == "claude-id"
    encoded = json.dumps(events)
    for private in ["test-anthropic-secret", "Stable instructions", "A new question", "Hello"]:
        assert private not in encoded


def test_combined_catalog_is_real_and_keeps_openai_route(claude_client, claude_backend, backend):
    models = claude_client.get("/api/v1/models").json()["data"]
    assert {r["id"] for r in models} == {
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-sol",
        *CLAUDE_MODELS,
    }
    assert all(not r["access_verified"] for r in models)
    claude_backend.available = []
    assert all(r["owned_by"] == "openai" for r in claude_client.get("/v1/models").json()["data"])
    response = claude_client.post(
        "/v1/chat/completions", json={**REQUEST, "model": "openai/gpt-5.6-terra"}
    )
    assert response.status_code == 200 and response.json()["provider"] == "chatgpt_plan"
    assert any(r.method == "POST" for r in backend.requests)


def test_claude_can_run_without_a_chatgpt_login(settings, claude_backend):
    configured = replace(settings, anthropic_api_key="test-anthropic-secret")
    app = create_app(configured, transport=httpx.MockTransport(claude_backend.handler))
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["authorization"] = f"Bearer {app.state.store.api_key}"
        assert {r["id"] for r in client.get("/v1/models").json()["data"]} == set(CLAUDE_MODELS)
        assert client.post("/v1/chat/completions", json=REQUEST).status_code == 200


def test_missing_key_has_no_fallback(client, backend):
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"
    assert not backend.requests


def test_sdk_does_not_inherit_a_different_host_or_subscription_token(
    settings, store, claude_backend, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unexpected.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-token")
    configured = replace(settings, anthropic_api_key="test-anthropic-secret")
    app = create_app(configured, transport=httpx.MockTransport(claude_backend.handler))
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        assert client.post("/v1/chat/completions", json=REQUEST).status_code == 200


def test_portable_cache_markers_and_effort_are_preserved(settings):
    body = deepcopy(REQUEST)
    body["cache_control"] = {"type": "ephemeral"}
    body["messages"][0]["content"] = [{"type": "text", "text": "Fixed", "cache_control": MARKER}]
    body["messages"][1]["content"] = [{"type": "text", "text": "Changing"}]
    body["max_tokens"] = 100
    body["reasoning"] = {"effort": "low"}
    prepared = claude_request(body, settings)
    assert prepared.body["system"][0]["cache_control"] == MARKER
    assert prepared.body["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in prepared.body["messages"][0]["content"][0]
    assert prepared.body["output_config"]["effort"] == "low"
    assert prepared.body["thinking"] == {"type": "adaptive"}
    assert prepared.body["max_tokens"] == 100
    assert body["messages"][0]["content"][0]["cache_control"] == MARKER


def test_cache_defaults_can_be_disabled_and_do_not_write_dynamic_content(settings):
    prepared = claude_request({**REQUEST, "cache_control": None}, settings)
    assert "cache_control" not in prepared.body["system"][0]
    assert "cache_control" not in prepared.body
    prepared = claude_request(REQUEST, replace(settings, anthropic_system_cache_ttl="off"))
    assert "cache_control" not in prepared.body["system"][0]
    prepared = claude_request({**REQUEST, "messages": REQUEST["messages"][1:]}, settings)
    assert "cache_control" not in prepared.body["messages"][0]["content"][0]
    prepared = claude_request(
        {**REQUEST, "max_tokens": None, "max_completion_tokens": 75}, settings
    )
    assert prepared.body["max_tokens"] == 75


@pytest.mark.parametrize(
    "change",
    [
        {"model": "anthropic/claude-sonnet-latest"},
        {"cache_control": False},
        {"cache_control": {"type": "ephemeral", "ttl": []}},
        {"cache_control": {"type": "ephemeral", "ttl": "2h"}},
        {"cache_control": {"type": "permanent"}},
        {"max_tokens": True},
        {"max_tokens": 0},
        {"max_tokens": 128001},
        {"max_tokens": 10, "max_completion_tokens": 20},
        {"reasoning_effort": []},
        {"reasoning_effort": ""},
        {"reasoning_effort": "none"},
        {"reasoning": {"summary": "auto"}},
        {"reasoning": False},
        {"tools": [{"type": "openrouter:web_search"}]},
        {"tools": {}},
        {"tool_choice": "auto"},
        {"parallel_tool_calls": False},
        {"temperature": 0.1},
        {"provider": {"only": ["openai"]}},
        {"provider": {"allow_fallbacks": True}},
        {"provider": {"data_collection": "deny"}},
        {"stream": "true"},
        {"stream_options": {"include_usage": "yes"}},
        {"stream_options": False},
        {"response_format": {"type": "json_object"}},
        {"messages": [{"role": [], "content": "Hi"}]},
        {"messages": [{"role": "user", "content": []}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
                    ],
                }
            ]
        },
        {"messages": [{"role": "user", "content": "Hi"}, {"role": "system", "content": "Later"}]},
    ],
)
def test_unsupported_semantics_are_rejected_before_network(
    claude_client, claude_backend, backend, change
):
    response = claude_client.post("/v1/chat/completions", json={**REQUEST, **change})
    assert response.status_code == 400
    assert not claude_backend.requests and not backend.requests


def test_claude_responses_route_is_explicitly_unsupported(claude_client, claude_backend):
    response = claude_client.post("/v1/responses", json={"model": MODEL, "input": "Hi"})
    assert response.status_code == 400 and not claude_backend.requests


def test_schema_transformation_preserves_local_constraints_and_sdk_parse(
    claude_client, claude_backend, store
):
    class Answer(BaseModel):
        amount: int = Field(ge=1)

    sdk = OpenAI(
        base_url=str(claude_client.base_url).rstrip("/") + "/api/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=claude_client,
    )
    claude_backend.text = '{"amount":2}'
    result = sdk.chat.completions.parse(
        model=MODEL, messages=REQUEST["messages"], response_format=Answer
    )
    assert result.choices[0].message.parsed.amount == 2
    post = json.loads(claude_backend.requests[-1].content)
    assert "minimum" not in post["output_config"]["format"]["schema"]["properties"]["amount"]
    assert (
        "minimum"
        in post["output_config"]["format"]["schema"]["properties"]["amount"]["description"]
    )
    claude_backend.text = '{"amount":0}'
    with pytest.raises(Exception) as failure:
        sdk.chat.completions.parse(
            model=MODEL, messages=REQUEST["messages"], response_format=Answer
        )
    assert "schema_validation_failed" in str(failure.value)


@pytest.mark.parametrize("claude_client", ["validated_json"], indirect=True)
def test_explicit_validated_json_keeps_full_schema_order_and_records_mode(
    claude_client, claude_backend, store, backend
):
    class Part(BaseModel):
        label: str
        amount: int = Field(ge=1)

    class Answer(BaseModel):
        plan: Part
        items: list[Part]
        state: Literal["ok"]
        optional: Part | None

    claude_backend.text = json.dumps(
        {"plan": {"label": "a", "amount": 2}, "items": [], "state": "ok", "optional": None}
    )
    sdk = OpenAI(
        base_url=str(claude_client.base_url).rstrip("/") + "/api/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=claude_client,
    )
    raw = sdk.chat.completions.with_raw_response.parse(
        model=MODEL,
        messages=REQUEST["messages"],
        response_format=Answer,
        reasoning_effort="high",
    )
    assert raw.parse().choices[0].message.parsed.plan.amount == 2
    assert raw.headers["x-gateway-structured-output-mode"] == "validated_json"
    assert raw.headers["x-gateway-structured-output-version"] == "validated-json.v2"
    post = json.loads(claude_backend.requests[-1].content)
    assert "format" not in post["output_config"]
    assert post["output_config"]["effort"] == "high"
    assert post["messages"][-1]["content"][0]["text"] == REQUEST["messages"][-1]["content"]
    assert len(post["messages"][-1]["content"]) == 1
    instruction = post["system"][-1]["text"]
    schema = json.loads(instruction.split("\n", 1)[1])
    assert list(schema["properties"]) == ["plan", "items", "state", "optional"]
    assert schema["$defs"]["Part"]["properties"]["amount"]["minimum"] == 1
    assert schema["properties"]["state"]["const"] == "ok"
    assert set(schema["required"]) == set(schema["properties"])
    assert post["system"][-2]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert "cache_control" not in post["system"][-1]
    assert not backend.requests
    events = store.recent()
    assert all(e["structured_output_mode"] == "validated_json" for e in events)
    assert all(e["structured_output_version"] == "validated-json.v2" for e in events)
    assert events[-1]["event"] == "completed"
    assert instruction not in json.dumps(events) and "minimum" not in json.dumps(events)
    cap = claude_client.get("/gateway/capabilities").json()["providers"]["anthropic"]
    assert cap["structured_output_mode"] == "validated_json"
    invalid = json.loads(claude_backend.text)
    invalid["state"] = "wrong"
    claude_backend.text = json.dumps(invalid)
    with pytest.raises(Exception, match="schema_validation_failed"):
        sdk.chat.completions.parse(
            model=MODEL, messages=REQUEST["messages"], response_format=Answer
        )
    assert store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize("claude_client", ["validated_json"], indirect=True)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "{}",
        '{"amount":0}',
        '{"amount":"2"}',
        '{"amount":2,"extra":true}',
        '{"amount":NaN}',
        '{"amount":Infinity}',
    ],
)
def test_validated_json_rejects_invalid_completed_answers_without_retry(
    claude_client, claude_backend, store, backend, stream, text
):
    claude_backend.text = text
    response = claude_client.post(
        "/api/v1/chat/completions",
        json={
            **REQUEST,
            "stream": stream,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "Answer",
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "number", "minimum": 1}},
                        "required": ["amount"],
                        "additionalProperties": False,
                    },
                },
            },
        },
    )
    if stream:
        assert response.status_code == 200
        assert "schema_validation_failed" in response.text
        assert '"finish_reason": "stop"' not in response.text
    else:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "schema_validation_failed"
        assert response.json()["error"]["validation_errors"]
        assert response.json()["usage"]["prompt_tokens"] == 3010
        assert response.headers["x-gateway-structured-output-mode"] == "validated_json"
        assert response.headers["x-gateway-structured-output-version"] == "validated-json.v2"
        assert response.headers["x-upstream-request-id"] == "claude-id"
    assert store.recent()[-1]["event"] == "failed"
    assert len([r for r in claude_backend.requests if r.method == "POST"]) == 1
    assert not backend.requests


def test_validated_json_preserves_explicit_cache_boundaries_and_caller_objects(settings):
    body = deepcopy(REQUEST)
    body["messages"][-1]["content"] = [
        {"type": "text", "text": "Prefix", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    body["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "Answer", "schema": {"type": "integer", "minimum": 1}},
    }
    before = deepcopy(body)
    prepared = claude_request(body, replace(settings, anthropic_structured_output="validated_json"))
    assert body == before
    assert prepared.body["messages"][-1]["content"][0] == body["messages"][-1]["content"][0]
    assert len(prepared.body["messages"][-1]["content"]) == 1
    assert "cache_control" not in prepared.body["system"][-1]
    assert "cache_control" not in prepared.body["system"][0]
    assert prepared.schema == body["response_format"]["json_schema"] | {"type": "json_schema"}


@pytest.mark.parametrize("claude_client", ["validated_json"], indirect=True)
def test_validation_diagnostics_never_echo_the_answer_or_schema(
    claude_client, claude_backend, store
):
    claude_backend.text = '{"amount":"private-answer","extra":"another-private-value"}'
    response = claude_client.post(
        "/api/v1/chat/completions",
        json={
            **REQUEST,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "Answer",
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "integer"}},
                        "required": ["amount"],
                        "additionalProperties": False,
                    },
                },
            },
        },
    )
    assert response.status_code == 422
    issues = response.json()["error"]["validation_errors"]
    assert issues[0] == {"path": ["amount"], "validator": "type", "expected_type": "integer"}
    assert issues[1]["validator"] == "additionalProperties"
    for private in ["private-answer", "another-private-value", "A new question", '"schema":']:
        assert private not in response.text
        assert private not in json.dumps(store.recent())
    assert "validation_errors" not in json.dumps(store.recent())


def test_schema_compilation_error_is_safe_actionable_and_keeps_request_id(
    claude_client, claude_backend, store
):
    claude_backend.status = 400
    claude_backend.error_type = "invalid_request_error"
    claude_backend.error_message = "The compiled grammar is too large. private-schema-text"
    response = claude_client.post("/api/v1/chat/completions", json=REQUEST)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "schema_compilation_limit"
    assert "validated_json" in response.json()["error"]["message"]
    assert "private-schema-text" not in response.text
    assert response.headers["x-upstream-request-id"] == "claude-id"
    assert store.recent()[-1]["upstream_request_id"] == "claude-id"
    assert len([r for r in claude_backend.requests if r.method == "POST"]) == 1


def test_structured_output_mode_is_explicit_and_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT", raising=False)
    assert Settings.from_env().anthropic_structured_output == "native"
    monkeypatch.setenv("CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT", "validated_json")
    assert Settings.from_env().anthropic_structured_output == "validated_json"
    monkeypatch.setenv("CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT", "automatic")
    with pytest.raises(ValueError, match="native or validated_json"):
        Settings.from_env()


def test_stream_usage_and_refusal_remain_distinct(claude_client, claude_backend, store):
    response = claude_client.post(
        "/v1/chat/completions",
        json={**REQUEST, "stream": True, "stream_options": {"include_usage": True}},
    )
    chunks = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    assert any(c.get("usage", {}).get("prompt_tokens") == 3010 for c in chunks)
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert all(c["model"] == MODEL for c in chunks)
    claude_backend.events = message_events("Cannot comply", stop="refusal")
    response = claude_client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["refusal"] == "Cannot comply"
    assert store.recent()[-1]["event"] == "refused"


@pytest.mark.parametrize(
    "fault",
    ["truncated", "max_tokens", "wrong_model", "unclosed", "error", "missing_delta", "fallback"],
)
def test_bad_streams_cannot_be_successes(claude_client, claude_backend, backend, store, fault):
    events = message_events()
    if fault == "truncated":
        events.pop()
    elif fault == "max_tokens":
        events[-2]["delta"]["stop_reason"] = "max_tokens"
    elif fault == "wrong_model":
        events[0]["message"]["model"] = "claude-opus-5-5"
    elif fault == "unclosed":
        events.pop(3)
    elif fault == "error":
        events = events[:3] + [
            {"type": "error", "error": {"type": "overloaded_error", "message": "Busy"}}
        ]
    elif fault == "missing_delta":
        events.pop(-2)
    elif fault == "fallback":
        events[1]["content_block"]["type"] = "fallback"
    claude_backend.events = events
    response = claude_client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code >= 400
    assert store.recent()[-1]["event"] == "failed"
    assert len([r for r in claude_backend.requests if r.method == "POST"]) == 1
    assert not backend.requests
    # A failed attempt releases the shared slot; the next attempt can complete.
    claude_backend.events = None
    assert claude_client.post("/v1/chat/completions", json=REQUEST).status_code == 200


def test_midstream_error_has_no_success_finish(claude_client, claude_backend):
    claude_backend.events = message_events()[:3] + [
        {"type": "error", "error": {"type": "overloaded_error", "message": "Busy"}}
    ]
    response = claude_client.post("/v1/chat/completions", json={**REQUEST, "stream": True})
    assert response.status_code == 200
    assert '"code": "overloaded_error"' in response.text
    assert '"finish_reason": "stop"' not in response.text


def test_http_failure_preserves_retry_hint_without_retry(claude_client, claude_backend):
    claude_backend.status = 429
    response = claude_client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 429 and response.headers["retry-after"] == "7"
    assert response.headers["x-upstream-request-id"] == "claude-id"
    assert len([r for r in claude_backend.requests if r.method == "POST"]) == 1


def test_missing_usage_is_not_fabricated():
    assert claude_usage(None) is None
    value = claude_usage({"input_tokens": 10, "output_tokens": 3})
    assert value["input_tokens"] is None and value["total_tokens"] is None
    assert value["input_tokens_details"]["cached_tokens"] is None


def test_secure_key_file_and_settings_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CHATGPT_GATEWAY_ANTHROPIC_API_KEY_FILE", raising=False)
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(tmp_path))
    path = tmp_path / "anthropic-api-key"
    path.write_text("private-key\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        Settings.from_env()
    path.chmod(0o600)
    configured = Settings.from_env()
    assert configured.anthropic_api_key == "private-key"
    assert "private-key" not in repr(configured)
    monkeypatch.setenv("CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL", "bad")
    with pytest.raises(ValueError, match="must be off"):
        Settings.from_env()


def test_both_providers_share_one_admission_limit(claude_client, claude_backend, backend):
    async def occupy():
        slots = claude_client.app.state.plan._slots
        for _ in range(12):
            await slots.acquire()

    asyncio.run(occupy())
    try:
        for model in [MODEL, "openai/gpt-5.6-sol"]:
            response = claude_client.post("/v1/chat/completions", json={**REQUEST, "model": model})
            assert (
                response.status_code == 429 and response.json()["error"]["code"] == "gateway_busy"
            )
        assert not claude_backend.requests and not backend.requests
    finally:
        for _ in range(12):
            claude_client.app.state.plan._slots.release()
    assert claude_client.post("/v1/chat/completions", json=REQUEST).status_code == 200


def test_cache_slots_and_ttl_order_are_validated(settings):
    body = deepcopy(REQUEST)
    body["messages"][0]["content"] = [
        {"type": "text", "text": "Fixed", "cache_control": MARKER} for _ in range(4)
    ]
    assert len(claude_request(body, settings).body["system"]) == 4
    body["cache_control"] = MARKER
    with pytest.raises(GatewayError, match="four cache breakpoints"):
        claude_request(body, settings)
    body.pop("cache_control")
    body["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    with pytest.raises(GatewayError, match="One-hour"):
        claude_request(body, settings)


def test_anthropic_policy_exception_is_separate_from_chatgpt(settings):
    body = {**REQUEST, "provider": {"only": ["anthropic"], "data_collection": "deny"}}
    with pytest.raises(GatewayError, match="cannot attest"):
        claude_request(body, replace(settings, accept_chatgpt_data_controls=True))
    prepared = claude_request(body, replace(settings, accept_anthropic_data_controls=True))
    assert prepared.data_policy_override


def test_not_listed_claude_model_is_not_sent_or_substituted(claude_client, claude_backend, backend):
    claude_backend.available = []
    response = claude_client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 404
    assert all(r.method == "GET" for r in claude_backend.requests)
    assert not backend.requests


@pytest.mark.asyncio
async def test_closed_claude_stream_releases_capacity_and_records_interruption(
    settings, store, claude_backend
):
    slots = asyncio.Semaphore(1)
    configured = replace(settings, anthropic_api_key="test-anthropic-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(claude_backend.handler)) as client:
        upstream = ClaudeClient(configured, store, client, slots=slots)
        events = upstream.events(
            claude_request(REQUEST, configured),
            request_id="closed",
            endpoint="chat.completions",
            requested_model=MODEL,
            client_name="test",
        )
        assert (await anext(events))["type"] == "response.created"
        await events.aclose()
        await asyncio.wait_for(slots.acquire(), timeout=0.1)
        slots.release()
    assert store.recent()[-1]["event"] == "interrupted"


@pytest.mark.asyncio
async def test_claude_timeout_has_no_retry_and_releases_capacity(settings, store, claude_backend):
    async def handler(request):
        if request.url.path == "/v1/messages":
            await asyncio.sleep(0.1)
        return claude_backend.handler(request)

    configured = replace(settings, anthropic_api_key="test-anthropic-secret", timeout_seconds=0.02)
    slots = asyncio.Semaphore(1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        upstream = ClaudeClient(configured, store, client, slots=slots)
        with pytest.raises(GatewayError) as error:
            async for _ in upstream.events(
                claude_request(REQUEST, configured),
                request_id="timeout",
                endpoint="chat.completions",
                requested_model=MODEL,
                client_name="test",
            ):
                pass
        assert error.value.status == 504
        await asyncio.wait_for(slots.acquire(), timeout=0.1)
        slots.release()
    assert store.recent()[-1]["code"] == "upstream_timeout"
