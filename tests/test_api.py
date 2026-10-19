import json

import httpx
import pytest
from conftest import completed
from openai import OpenAI
from pydantic import BaseModel

REQUEST = {"model": "openai/gpt-5.6-terra", "messages": [{"role": "user", "content": "Hi"}]}
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}


def test_plain_completion_preserves_model_usage_and_plan_route(client, backend, store):
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 200
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "Hello"
    assert data["usage"]["total_tokens"] == 23
    post = next(r for r in backend.requests if r.method == "POST")
    assert post.headers["authorization"] == "Bearer test-access-secret"
    payload = json.loads(post.content)
    assert payload["store"] is False and payload["stream"] is True
    assert payload["model"] == "gpt-5.6-terra"
    journal = store.recent()
    assert [item["event"] for item in journal] == ["started", "completed"]
    encoded = json.dumps(journal)
    assert "test-access-secret" not in encoded and '"Hi"' not in encoded
    assert "Hello" not in encoded


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/api/v1/chat/completions"])
def test_base_url_variants(client, path):
    assert client.post(path, json=REQUEST).status_code == 200


@pytest.mark.parametrize("path", ["/v1/models", "/api/v1/models"])
def test_catalog_is_account_specific_without_fake_entitlement(client, path):
    response = client.get(path).json()
    assert [m["id"] for m in response["data"]] == ["openai/gpt-5.6-terra", "openai/gpt-5.6-sol"]
    assert all(m["access_verified"] is False for m in response["data"])


@pytest.mark.parametrize(
    "path",
    [
        "/v1/models",
        "/claude-code/v1/models",
        "/gateway/status",
        "/gateway/accounts",
        "/gateway/requests",
        "/gateway/capabilities",
    ],
)
def test_every_private_read_requires_local_auth(client, path):
    client.headers.pop("authorization")
    assert client.get(path).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/gateway/login",
        "/gateway/logout",
        "/gateway/accounts/select",
        "/v1/chat/completions",
        "/claude-code/v1/chat/completions",
        "/v1/responses",
        "/v1/images/generations",
        "/v1/images/edits",
        "/api/v1/images/generations",
        "/api/v1/images/edits",
        "/api/v1/images",
    ],
)
def test_every_mutation_requires_local_auth(client, path, backend):
    client.headers["authorization"] = "Bearer not-the-key"
    assert client.post(path, json=REQUEST).status_code == 401
    assert not backend.requests


def test_bad_host_and_foreign_browser_origins_are_refused(client, backend):
    assert client.get("/healthz", headers={"host": "attacker.test"}).status_code == 400
    assert (
        client.post(
            "/v1/chat/completions", json=REQUEST, headers={"origin": "https://attacker.test"}
        ).status_code
        == 403
    )
    assert not backend.requests


def test_docker_host_alias_is_local_but_does_not_bypass_auth_or_origin_checks(client):
    assert client.get("/healthz", headers={"host": "host.docker.internal:8317"}).status_code == 200
    assert (
        client.get(
            "/gateway/capabilities",
            headers={"host": "host.docker.internal:8317", "authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/gateway/capabilities", headers={"host": "host.docker.internal:8317"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/gateway/capabilities",
            headers={"host": "host.docker.internal:8317", "origin": "https://attacker.test"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "change",
    [
        {"temperature": 0.3},
        {"max_tokens": 100},
        {"max_completion_tokens": 100},
        {"top_p": 0.9},
        {"seed": 1},
        {"n": 2},
        {"model": "anthropic/claude"},
        {"provider": {"only": ["anthropic"]}},
        {"provider": {"allow_fallbacks": True}},
        {"provider": {"data_collection": "deny"}},
        {"provider": {"allow_fallbacks": "false"}},
        {"provider": {"require_parameters": "true"}},
        {"parallel_tool_calls": True},
        {"tools": [{"type": "openrouter:web_search"}]},
        {"reasoning": {"max_tokens": 1000}},
        {"reasoning_effort": "invalid"},
    ],
)
def test_unsupported_options_never_reach_inference(client, backend, change):
    response = client.post("/v1/chat/completions", json={**REQUEST, **change})
    assert response.status_code == 400
    assert not backend.requests


def test_unknown_account_model_is_not_substituted(client, backend):
    response = client.post("/v1/chat/completions", json={**REQUEST, "model": "gpt-unknown"})
    assert response.status_code == 404
    assert all(r.method != "POST" for r in backend.requests)


def test_system_context_image_and_reasoning_are_translated_without_adding_context(client, backend):
    messages = [
        {"role": "system", "content": "Only answer this task."},
        {"role": "assistant", "content": "Earlier answer"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AA==", "detail": "high"},
                },
            ],
        },
    ]
    response = client.post(
        "/v1/chat/completions", json={**REQUEST, "messages": messages, "reasoning_effort": "high"}
    )
    assert response.status_code == 200
    payload = json.loads(backend.requests[-1].content)
    assert len(payload["input"]) == 3
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][1]["content"][0]["type"] == "input_text"
    assert payload["input"][2]["content"][1]["type"] == "input_image"
    assert payload["reasoning"] == {"effort": "high"}


def test_native_responses_returns_completed_object(client):
    response = client.post("/v1/responses", json={"model": "gpt-5.6-terra", "input": "Hi"})
    assert response.status_code == 200 and response.json()["id"] == "resp_test"


@pytest.mark.parametrize(
    "change",
    [
        {"store": True},
        {"previous_response_id": "resp_old"},
        {"metadata": {}},
        {"tools": [{"type": "image_generation"}]},
    ],
)
def test_native_unsupported_options_fail_explicitly(client, backend, change):
    response = client.post(
        "/v1/responses", json={"model": "gpt-5.6-terra", "input": "Hi", **change}
    )
    assert response.status_code == 400 and not backend.requests


def test_completed_json_is_schema_checked(client, backend):
    backend.events = [{"type": "response.completed", "response": completed('{"answer":"yes"}')}]
    response = client.post("/v1/chat/completions", json={**REQUEST, "response_format": SCHEMA})
    assert response.status_code == 200
    assert json.loads(backend.requests[-1].content)["text"]["format"]["strict"] is True
    backend.events = [{"type": "response.completed", "response": completed('{"answer":5}')}]
    response = client.post("/v1/chat/completions", json={**REQUEST, "response_format": SCHEMA})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "schema_validation_failed"


def test_sdk_parse_uses_gateway_protocol(client, backend, store):
    class Answer(BaseModel):
        answer: str

    backend.events = [{"type": "response.completed", "response": completed('{"answer":"yes"}')}]
    sdk = OpenAI(
        base_url="http://127.0.0.1:8317/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=client,
    )
    result = sdk.chat.completions.parse(
        model="openai/gpt-5.6-terra", messages=REQUEST["messages"], response_format=Answer
    )
    assert result.choices[0].message.parsed.answer == "yes"


def test_sdk_streaming_includes_usage(client, store):
    sdk = OpenAI(
        base_url="http://127.0.0.1:8317/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=client,
    )
    chunks = list(
        sdk.chat.completions.create(
            model="openai/gpt-5.6-terra",
            messages=REQUEST["messages"],
            stream=True,
            stream_options={"include_usage": True},
        )
    )
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello"
    assert chunks[-1].usage.total_tokens == 23
    assert chunks[-2].choices[0].finish_reason == "stop"


def test_http_quota_error_preserves_status_body_and_request_id(client, backend):
    backend.status = 429
    backend.error_body = {
        "error": {
            "message": "Limit reached",
            "code": "subscription_sharing_usage_limit_exceeded",
            "param": None,
        }
    }
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 429
    assert response.json() == backend.error_body
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-upstream-request-id"] == "upstream-id"
    assert sum(r.method == "POST" for r in backend.requests) == 1


def test_streamed_quota_failure_is_never_reported_as_success(client, backend, store):
    backend.events = [
        {"type": "response.created", "response": {}},
        {"type": "response.output_text.delta", "delta": "partial"},
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "message": "Limit",
                    "code": "subscription_sharing_usage_limit_exceeded",
                }
            },
        },
    ]
    response = client.post("/v1/chat/completions", json={**REQUEST, "stream": True})
    assert '"content": "partial"' in response.text
    assert "subscription_sharing_usage_limit_exceeded" in response.text
    assert '"finish_reason": "stop"' not in response.text
    assert store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize(
    "code,status",
    [
        ("subscription_sharing_user_not_eligible", 403),
        ("subscription_sharing_usage_limit_exceeded", 429),
        ("subscription_sharing_usage_unavailable", 503),
        ("subscription_sharing_unsupported_capability", 400),
        ("subscription_sharing_route_not_supported", 403),
        ("subscription_sharing_invalid_user", 401),
        ("subscription_sharing_user_unavailable", 503),
        ("chatpass_v2_scope_not_authorized", 403),
        ("chatpass_v2_invalid_authorization_context", 403),
    ],
)
def test_plan_failure_codes_preserve_documented_status_and_parameter(client, backend, code, status):
    backend.events = [
        {"type": "response.failed", "response": {"error": {"code": code, "param": "tools"}}}
    ]
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["param"] == "tools"
    assert response.headers["x-upstream-request-id"] == "upstream-id"


@pytest.mark.parametrize("stream", [False, True])
def test_missing_usage_stays_unknown_and_served_snapshot_is_reported(client, backend, stream):
    backend.events = [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {
            "type": "response.completed",
            "response": completed(model="gpt-5.6-terra-2026-10-01", usage=None),
        },
    ]
    response = client.post(
        "/v1/chat/completions",
        json={**REQUEST, "stream": stream, "stream_options": {"include_usage": True}},
    )
    assert response.status_code == 200
    if stream:
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        assert chunks[-1]["usage"] is None and chunks[-1]["choices"] == []
        assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[-2]["model"] == "openai/gpt-5.6-terra-2026-10-01"
    else:
        assert response.json()["usage"] is None
        assert response.json()["model"] == "openai/gpt-5.6-terra-2026-10-01"


@pytest.mark.parametrize("event", [None, {"type": "response.incomplete", "response": {}}])
def test_truncated_or_incomplete_responses_fail(client, backend, store, event):
    backend.events = [{"type": "response.output_text.delta", "delta": "partial"}]
    if event:
        backend.events.append(event)
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 502
    assert store.recent()[-1]["event"] == "failed"


def test_different_returned_model_is_rejected(client, backend):
    backend.events = [{"type": "response.completed", "response": completed(model="gpt-5.6-sol")}]
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 502 and response.json()["error"]["code"] == "model_mismatch"


def test_refusal_is_preserved_for_sdk_parse(client, backend):
    backend.events = [
        {
            "type": "response.completed",
            "response": completed(
                output=[
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot answer"}],
                    }
                ]
            ),
        }
    ]
    response = client.post("/v1/chat/completions", json={**REQUEST, "response_format": SCHEMA})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["refusal"] == "Cannot answer"


def test_missing_plan_grant_is_not_bypassed(client, store, backend):
    store.save({**store.active(), "scopes": ["openid"]}, active=True)
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 403 and not backend.requests


def test_clear_tokens_blocks_inference_but_retains_registration(client, store, backend):
    identity = store.active()["client_id"]
    store.clear_tokens(store.active()["id"])
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 401 and not backend.requests
    assert store.active()["client_id"] == identity


def test_body_size_is_enforced_before_inference(client, backend):
    response = client.post("/v1/chat/completions", content=b"x" * (8 * 1024 * 1024 + 1))
    assert response.status_code == 413 and not backend.requests


def test_non_json_object_is_a_client_error(client, backend):
    response = client.post("/v1/chat/completions", json=[1, 2])
    assert response.status_code == 400 and not backend.requests


def test_transport_error_is_a_failed_journal_event(client, backend, store):
    old = backend.handler

    def disconnected(request):
        if request.method == "POST":
            raise httpx.ReadError("connection lost")
        return old(request)

    client.app.state.plan.client._transport = httpx.MockTransport(disconnected)
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 502 and store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize("content_type", [None, "Text/Event-Stream; charset=utf-8"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_valid_sse_with_missing_or_case_insensitive_media_type(
    client, backend, store, content_type, stream, native
):
    backend.content_type = content_type
    path = "/v1/responses" if native else "/v1/chat/completions"
    body = (
        {"model": "gpt-5.6-terra", "input": "Hi", "stream": stream}
        if native
        else {**REQUEST, "stream": stream}
    )
    response = client.post(path, json=body)
    assert response.status_code == 200
    if stream:
        marker = '"type": "response.completed"' if native else '"finish_reason": "stop"'
        assert marker in response.text
    elif native:
        assert response.json()["status"] == "completed"
    else:
        assert response.json()["choices"][0]["message"]["content"] == "Hello"
    assert store.recent()[-1]["event"] == "completed"


@pytest.mark.parametrize(
    "content_type", ["application/json", "text/plain", "text/event-stream-bad"]
)
def test_explicit_non_sse_media_type_remains_a_failure(client, backend, store, content_type):
    backend.content_type = content_type
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_upstream_stream"
    assert store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize(
    "events,status,code",
    [
        ([], 502, "incomplete_stream"),
        ([{"type": "response.output_text.delta", "delta": "partial"}], 502, "incomplete_stream"),
        (
            [
                {
                    "type": "response.failed",
                    "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
                }
            ],
            429,
            "subscription_sharing_usage_limit_exceeded",
        ),
    ],
)
def test_missing_media_type_does_not_turn_unfinished_or_failed_output_into_success(
    client, backend, store, events, status, code
):
    backend.content_type = None
    backend.events = events
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize("native", [False, True])
def test_sdk_schema_parse_recovers_finished_items_from_empty_terminal_output(
    client, backend, store, native
):
    class Answer(BaseModel):
        answer: str

    item = completed('{"answer":"yes"}')["output"][0]
    backend.content_type = None
    backend.events = [
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": completed(output=[])},
    ]
    sdk = OpenAI(
        base_url="http://127.0.0.1:8317/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=client,
    )
    if native:
        result = sdk.responses.parse(model="gpt-5.6-terra", input="Hi", text_format=Answer)
        assert result.output_parsed.answer == "yes"
    else:
        result = sdk.chat.completions.parse(
            model="gpt-5.6-terra",
            messages=REQUEST["messages"],
            response_format=Answer,
        )
        assert result.choices[0].message.parsed.answer == "yes"
    assert store.recent()[-1]["event"] == "completed"


@pytest.mark.parametrize("terminal", [False, True])
def test_finished_item_still_requires_valid_terminal_status(client, backend, store, terminal):
    backend.events = [
        {"type": "response.output_item.done", "output_index": 0, "item": completed()["output"][0]},
    ]
    if terminal:
        backend.events.append(
            {"type": "response.completed", "response": completed(status="incomplete", output=[])}
        )
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 502
    assert store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize("done", [False, True])
def test_recovery_does_not_accept_partial_items_or_invalid_final_json(client, backend, done):
    item = completed('{"answer":5}' if done else '{"answer":"yes"}')["output"][0]
    backend.events = [
        {
            "type": "response.output_item.done" if done else "response.output_item.added",
            "output_index": 0,
            "item": item,
        },
        {"type": "response.completed", "response": completed(output=[])},
    ]
    response = client.post("/v1/chat/completions", json={**REQUEST, "response_format": SCHEMA})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "schema_validation_failed"


@pytest.mark.parametrize("index", [-1, True, 1])
def test_invalid_or_missing_output_indices_cannot_be_recovered(client, backend, index):
    backend.events = [
        {
            "type": "response.output_item.done",
            "output_index": index,
            "item": completed()["output"][0],
        },
        {"type": "response.completed", "response": completed(output=[])},
    ]
    response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_upstream_stream"
