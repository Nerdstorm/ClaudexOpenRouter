import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from test_catalog_recovery import EXPIRED, setup_client

from chatgpt_gateway.app import create_app
from chatgpt_gateway.errors import GatewayError
from chatgpt_gateway.protocol import chat_request

RENEWED = {
    "access_token": "replacement",
    "refresh_token": "rotated",
    "expires_in": 3600,
    "token_type": "Bearer",
}
REQUEST = {"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "Hello"}]}


async def invoke(plan):
    return [
        event
        async for event in plan.events(
            chat_request(REQUEST),
            request_id="test",
            endpoint="chat.completions",
            requested_model=REQUEST["model"],
            client_name="test",
        )
    ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_expired_rejection_refreshes_then_replays_identical_request(
    settings, store, backend, path, stream
):
    posts, refreshes = [], []

    def handler(request):
        if request.url.path == "/api/accounts/oauth/token":
            refreshes.append(request)
            return httpx.Response(200, json=RENEWED)
        if request.url.path == "/v1/responses":
            posts.append(request)
            if len(posts) == 1:
                return httpx.Response(401, json=EXPIRED)
        return backend.handler(request)

    with TestClient(
        create_app(settings, transport=httpx.MockTransport(handler)), base_url=settings.base_url
    ) as client:
        body = (
            REQUEST
            if path.endswith("completions")
            else {"model": REQUEST["model"], "input": "Hello"}
        )
        result = client.post(
            path,
            json={**body, "stream": stream},
            headers={"Authorization": "Bearer " + store.api_key},
        )
    assert result.status_code == 200
    assert "Hello" in result.text
    assert len(posts) == 2 and len(refreshes) == 1
    assert posts[0].content == posts[1].content
    assert [p.headers["authorization"] for p in posts] == [
        "Bearer test-access-secret",
        "Bearer replacement",
    ]
    assert store.active()["refresh_token"] == "rotated"
    assert [e["event"] for e in store.recent()] == ["started", "completed"]
    assert "replacement" not in json.dumps(store.recent())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "invalid_api_key"),
        (401, "invalid_token"),
        (403, "permission_denied"),
        (502, "token_expired"),
    ],
)
async def test_other_errors_never_refresh_or_replay(settings, store, backend, status, code):
    posts = []

    def handler(request):
        if request.method == "GET":
            return backend.handler(request)
        assert request.url.path == "/v1/responses"  # No OAuth exchange.
        posts.append(request)
        return httpx.Response(status, json={"error": {"code": code}})

    plan, _, http = setup_client(settings, store, handler)
    async with http:
        with pytest.raises(GatewayError) as error:
            await invoke(plan)
    assert (error.value.status, error.value.code) == (status, code)
    assert len(posts) == 1


async def test_second_expired_rejection_is_terminal(settings, store, backend):
    posts, refreshes = [], []

    def handler(request):
        if request.method == "GET":
            return backend.handler(request)
        if request.url.path == "/api/accounts/oauth/token":
            refreshes.append(request)
            return httpx.Response(200, json=RENEWED)
        posts.append(request)
        return httpx.Response(401, json=EXPIRED, headers={"x-request-id": "rejected-again"})

    plan, _, http = setup_client(settings, store, handler)
    async with http:
        with pytest.raises(GatewayError) as error:
            await invoke(plan)
    assert error.value.code == "token_expired"
    assert error.value.request_id == "rejected-again"
    assert len(posts) == 2 and len(refreshes) == 1
    assert [e["event"] for e in store.recent()] == ["started", "failed"]


@pytest.mark.parametrize("reason", ["too_early", "missing_refresh", "revoked", "unavailable"])
async def test_unavailable_renewal_stops_without_replaying(settings, store, backend, reason):
    saved = store.active()
    if reason == "too_early":
        saved["earliest_refresh_at"] = time.time() + 1800
    if reason == "missing_refresh":
        saved.pop("refresh_token")
    store.save(saved)
    posts, refreshes = [], []

    def handler(request):
        if request.method == "GET":
            return backend.handler(request)
        if request.url.path == "/api/accounts/oauth/token":
            refreshes.append(request)
            return httpx.Response(
                400 if reason == "revoked" else 503,
                json={
                    "error": "invalid_grant" if reason == "revoked" else "temporarily_unavailable"
                },
            )
        posts.append(request)
        return httpx.Response(401, json=EXPIRED)

    plan, _, http = setup_client(settings, store, handler)
    async with http:
        with pytest.raises(GatewayError):
            await invoke(plan)
    assert len(posts) == 1
    assert len(refreshes) == (1 if reason in ("revoked", "unavailable") else 0)
    assert ("refresh_token" in store.active()) == (reason not in ("missing_refresh", "revoked"))


async def test_token_error_inside_started_stream_is_not_replayed(settings, store, backend):
    backend.events = [
        {"type": "response.output_text.delta", "delta": "Partial answer"},
        {"type": "error", "code": "token_expired", "message": "Expired"},
    ]
    plan, _, http = setup_client(settings, store, backend.handler)
    async with http:
        with pytest.raises(GatewayError) as error:
            await invoke(plan)
    assert error.value.code == "token_expired"
    assert sum(r.method == "POST" for r in backend.requests) == 1


async def test_concurrent_rejections_share_one_rotation(settings, store, backend):
    posts, refreshes = [], []
    rejected = asyncio.Event()

    async def handler(request):
        if request.method == "GET":
            return backend.handler(request)
        if request.url.path == "/api/accounts/oauth/token":
            refreshes.append(request)
            return httpx.Response(200, json=RENEWED)
        posts.append(request)
        if request.headers["authorization"] == "Bearer test-access-secret":
            if len(posts) == 2:
                rejected.set()
            await asyncio.wait_for(rejected.wait(), 1)
            return httpx.Response(401, json=EXPIRED)
        return backend.handler(request)

    plan, _, http = setup_client(settings, store, handler)
    async with http:
        results = await asyncio.gather(invoke(plan), invoke(plan))
    assert all(r[-1]["type"] == "response.completed" for r in results)
    assert len(posts) == 4 and len(refreshes) == 1


def test_invalid_local_key_never_reaches_provider(client, backend):
    result = client.post(
        "/v1/chat/completions", json=REQUEST, headers={"Authorization": "Bearer invalid-local-key"}
    )
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "invalid_api_key"
    assert backend.requests == []
