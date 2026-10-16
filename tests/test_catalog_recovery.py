import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from chatgpt_gateway.auth import Auth
from chatgpt_gateway.errors import GatewayError
from chatgpt_gateway.upstream import PlanClient

CATALOG = {"models": [{"slug": "gpt-5.6-terra", "visibility": "list"}]}
EXPIRED = {
    "error": {"message": "Provided authentication token is expired.", "code": "token_expired"}
}


def setup_client(settings, store, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = Auth(store, http, "http://127.0.0.1:8317/auth/callback")
    return PlanClient(settings, store, auth, http), auth, http


async def test_catalog_rejection_renews_eligible_token_once(settings, store):
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return (
            httpx.Response(200, json=CATALOG)
            if len(seen) == 2
            else httpx.Response(401, json=EXPIRED)
        )

    plan, auth, http = setup_client(settings, store, handler)
    auth.exchange = AsyncMock(
        return_value={"access_token": "replacement", "refresh_token": "rotated", "expires_in": 3600}
    )
    current = store.active()
    async with http:
        assert (await plan.catalog(current))[0]["canonical_slug"] == "gpt-5.6-terra"
    assert len(seen) == 2
    assert current["access_token"] == "replacement"
    assert store.active()["refresh_token"] == "rotated"
    auth.exchange.assert_awaited_once()


async def test_rejected_token_refresh_is_serialized(settings, store):
    plan, auth, http = setup_client(settings, store, lambda _: httpx.Response(200, json=CATALOG))
    auth.exchange = AsyncMock(
        return_value={"access_token": "replacement", "refresh_token": "rotated", "expires_in": 3600}
    )
    rejected = store.active()
    async with http:
        results = await asyncio.gather(*(auth.credentials(rejected=rejected) for _ in range(4)))
    assert {result["access_token"] for result in results} == {"replacement"}
    auth.exchange.assert_awaited_once()


@pytest.mark.parametrize("status,code", [(401, "token_expired"), (502, "overloaded")])
async def test_recent_catalog_survives_listing_outage_without_refreshing_too_early(
    settings, store, status, code
):
    store.save({**store.active(), "earliest_refresh_at": time.time() + 1800})
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json=CATALOG)
            if len(calls) == 1
            else httpx.Response(status, json={"error": {"code": code}})
        )

    plan, auth, http = setup_client(settings, store, handler)
    auth.exchange = AsyncMock()
    current = store.active()
    async with http:
        expected = await plan.catalog(current)
        plan._catalog[current["id"]] = (time.monotonic() - 70, expected)
        results = await asyncio.gather(*(plan.catalog(dict(current)) for _ in range(5)))
        assert all(result == expected for result in results)
        assert len(calls) == 2
        auth.exchange.assert_not_awaited()
        # Explicit discovery must report the actual failure, not a stale successful list.
        with pytest.raises(GatewayError) as error:
            await plan.catalog(current, fresh=True)
        assert error.value.status == status
        plan._catalog[current["id"]] = (time.monotonic() - 301, expected)
        with pytest.raises(GatewayError):
            await plan.catalog(current)


@pytest.mark.parametrize("status,code", [(403, "permission_denied"), (401, "invalid_token")])
async def test_definite_auth_errors_do_not_use_cached_catalog(settings, store, status, code):
    plan, auth, http = setup_client(
        settings, store, lambda _: httpx.Response(status, json={"error": {"code": code}})
    )
    auth.exchange = AsyncMock()
    current = store.active()
    plan._catalog[current["id"]] = (time.monotonic() - 70, [{"canonical_slug": "gpt-5.6-terra"}])
    async with http:
        with pytest.raises(GatewayError) as error:
            await plan.catalog(current)
    assert error.value.status == status
    auth.exchange.assert_not_awaited()


async def test_no_cached_catalog_is_not_invented(settings, store):
    store.save({**store.active(), "earliest_refresh_at": time.time() + 1800})
    plan, _, http = setup_client(settings, store, lambda _: httpx.Response(401, json=EXPIRED))
    async with http:
        with pytest.raises(GatewayError) as error:
            await plan.catalog(store.active())
    assert error.value.code == "token_expired"


@pytest.mark.parametrize("inference_status", [200, 401])
async def test_cached_listing_still_checks_inference_authorization(
    settings, store, backend, inference_status
):
    from chatgpt_gateway.protocol import chat_request

    store.save({**store.active(), "earliest_refresh_at": time.time() + 1800})
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(401, json=EXPIRED)
        backend.status = inference_status
        backend.error_body = EXPIRED
        return backend.handler(request)

    plan, _, http = setup_client(settings, store, handler)
    plan._catalog[store.active()["id"]] = (
        time.monotonic() - 70,
        [{"canonical_slug": "gpt-5.6-terra"}],
    )
    prepared = chat_request(
        {"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "Hi"}]}
    )

    async def invoke():
        return [
            event
            async for event in plan.events(
                prepared,
                request_id="test",
                endpoint="chat.completions",
                requested_model="gpt-5.6-terra",
                client_name="test",
            )
        ]

    async with http:
        if inference_status == 401:
            with pytest.raises(GatewayError) as error:
                await invoke()
            assert error.value.code == "token_expired"
        else:
            assert (await invoke())[-1]["type"] == "response.completed"
    assert sum(r.method == "POST" for r in calls) == 1


async def test_catalog_preflight_failure_is_journalled_without_an_execution(settings, store):
    from chatgpt_gateway.protocol import chat_request

    store.save({**store.active(), "earliest_refresh_at": time.time() + 1800})
    plan, _, http = setup_client(settings, store, lambda _: httpx.Response(401, json=EXPIRED))
    prepared = chat_request(
        {"model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "private prompt"}]}
    )
    async with http:
        with pytest.raises(GatewayError):
            _ = [
                event
                async for event in plan.events(
                    prepared,
                    request_id="test",
                    endpoint="chat.completions",
                    requested_model="gpt-5.6-terra",
                    client_name="test",
                )
            ]
    events = store.recent()
    assert [e["event"] for e in events] == ["preflight_failed"]
    assert events[0]["code"] == "token_expired"
    assert "private prompt" not in str(events)
    assert "test-access-secret" not in str(events)


async def test_cached_catalog_does_not_cross_accounts(settings, store):
    from conftest import account

    plan, _, http = setup_client(
        settings, store, lambda _: httpx.Response(502, json={"error": {"code": "overloaded"}})
    )
    previous = store.active()
    plan._catalog[previous["id"]] = (time.monotonic() - 70, [{"canonical_slug": "gpt-5.6-terra"}])
    current = store.save(account(subject="other-subject"), active=True)
    async with http:
        with pytest.raises(GatewayError):
            await plan.catalog(current)


async def test_rejection_does_not_switch_account(settings, store):
    from conftest import account

    plan, auth, http = setup_client(settings, store, lambda _: httpx.Response(200, json=CATALOG))
    previous = store.active()
    store.save(account(subject="other-subject"), active=True)
    auth.exchange = AsyncMock()
    async with http:
        with pytest.raises(GatewayError) as error:
            await auth.credentials(rejected=previous)
    assert error.value.code == "account_changed"
    auth.exchange.assert_not_awaited()
