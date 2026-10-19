import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from conftest import completed

from chatgpt_gateway.admission import Admission
from chatgpt_gateway.app import create_app
from chatgpt_gateway.config import Settings
from chatgpt_gateway.errors import GatewayError


async def until(predicate):
    async with asyncio.timeout(2):
        # Observe exported counters without adding test-only callbacks to admission.
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def test_thirteenth_call_waits_beyond_old_deadline_and_cap_stays_twelve(settings):
    admission = Admission(settings)
    pool = asyncio.Semaphore(12)
    leases = [await admission.acquire(pool, timeout_seconds=10) for _ in range(12)]
    extra = asyncio.create_task(admission.acquire(pool, timeout_seconds=10))
    await until(lambda: admission.waiting == 1)
    await asyncio.sleep(1.05)
    assert not extra.done() and admission.active == 12
    leases.pop().release()
    lease = await asyncio.wait_for(extra, 1)
    assert lease.wait_ms >= 1000 and admission.active == 12
    for held in [*leases, lease]:
        held.release()
        held.release()
    assert pool._value == 12 and admission.waiting == admission.active == 0


async def test_queue_capacity_timeout_and_fifo(settings):
    admission = Admission(replace(settings, max_queued_requests=2))
    pool = asyncio.Semaphore(1)
    held = await admission.acquire(pool, timeout_seconds=10)
    first = asyncio.create_task(admission.acquire(pool, timeout_seconds=10))
    await until(lambda: admission.waiting == 1)
    second = asyncio.create_task(admission.acquire(pool, timeout_seconds=10))
    await until(lambda: admission.waiting == 2)
    with pytest.raises(GatewayError) as error:
        await admission.acquire(pool, timeout_seconds=10)
    assert error.value.status == 429 and error.value.code == "gateway_busy"
    assert error.value.retry_after == "1" and admission.waiting == 2
    held.release()
    first_lease = await asyncio.wait_for(first, 1)
    assert not second.done() and admission.waiting == 1
    first_lease.release()
    second_lease = await asyncio.wait_for(second, 1)
    second_lease.release()
    assert admission.active == admission.waiting == 0


@pytest.mark.parametrize("cancel_first", [True, False])
async def test_cancelled_waiter_cannot_leak_or_later_take_shared_capacity(settings, cancel_first):
    admission = Admission(settings)
    pool = asyncio.Semaphore(0)
    native = asyncio.Semaphore(1)
    task = asyncio.create_task(admission.acquire(native, pool, timeout_seconds=10))
    await until(lambda: native.locked() and admission.waiting == 1)
    if cancel_first:
        task.cancel()
        pool.release()
    else:
        pool.release()
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert native._value == pool._value == 1
    assert admission.waiting == admission.active == 0
    lease = await admission.acquire(native, pool, timeout_seconds=10)
    lease.release()


@pytest.mark.parametrize("request_timeout,queue_timeout", [(10, 0.02), (0.02, 10)])
async def test_partial_acquisition_released_on_either_deadline(
    settings, request_timeout, queue_timeout
):
    admission = Admission(replace(settings, admission_timeout_seconds=queue_timeout))
    native, shared = asyncio.Semaphore(1), asyncio.Semaphore(0)
    with pytest.raises(GatewayError) as error:
        await admission.acquire(native, shared, timeout_seconds=request_timeout)
    assert error.value.code == "gateway_busy" and error.value.status == 429
    assert native._value == 1 and shared._value == 0
    assert admission.waiting == admission.active == 0


async def test_waiting_for_image_pool_does_not_reserve_text_capacity(settings):
    admission = Admission(replace(settings, max_queued_requests=1))
    images, shared = asyncio.Semaphore(0), asyncio.Semaphore(1)
    task = asyncio.create_task(admission.acquire(images, shared, timeout_seconds=10))
    await until(lambda: admission.waiting == 1)
    text = await admission.acquire(shared, timeout_seconds=10)
    assert admission.active == 1 and admission.waiting == 1
    text.release()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shared._value == 1 and images._value == 0


async def test_zero_waiting_capacity_keeps_immediate_calls_working(settings):
    admission = Admission(replace(settings, max_queued_requests=0))
    pool = asyncio.Semaphore(1)
    lease = await admission.acquire(pool, timeout_seconds=10)
    with pytest.raises(GatewayError) as error:
        await admission.acquire(pool, timeout_seconds=10)
    assert error.value.code == "gateway_busy" and admission.waiting == 0
    lease.release()


@pytest.mark.parametrize("streaming", [False, True])
async def test_queued_http_disconnect_never_calls_upstream(settings, store, backend, streaming):
    app = create_app(
        replace(settings, max_concurrency=1), transport=httpx.MockTransport(backend.handler)
    )
    async with app.router.lifespan_context(app):
        pool = app.state.plan._slots
        await pool.acquire()
        messages = asyncio.Queue()
        await messages.put(
            {
                "type": "http.request",
                "body": json.dumps(
                    {
                        "model": "gpt-5.6-terra",
                        "input": "Private test",
                        "stream": streaming,
                    }
                ).encode(),
            }
        )
        sent = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/responses",
            "raw_path": b"/v1/responses",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"authorization", f"Bearer {store.api_key}".encode()),
                (b"content-type", b"application/json"),
            ],
            "server": ("127.0.0.1", 8317),
            "client": ("127.0.0.1", 1234),
        }

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(app(scope, messages.get, send))
        await until(lambda: app.state.admission.waiting == 1)
        await messages.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)
        assert not backend.requests
        assert app.state.admission.waiting == app.state.admission.active == 0
        pool.release()
        assert pool._value == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_queue_wait_counts_toward_total_timeout(settings, store, streaming):
    async def slow(request):
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"models": [{"slug": "gpt-5.6-terra"}]})
        await asyncio.sleep(0.06)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                f"data: {json.dumps({'type': 'response.completed', 'response': completed()})}\n\n"
            ),
        )

    app = create_app(
        replace(settings, max_concurrency=1, timeout_seconds=0.08),
        transport=httpx.MockTransport(slow),
    )
    async with app.router.lifespan_context(app):
        assert app.state.plan._admission is app.state.claude._admission
        assert app.state.plan._admission is app.state.claude_code._admission
        assert app.state.plan._admission is app.state.images._admission
        await app.state.plan._slots.acquire()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=settings.base_url,
            headers={"authorization": f"Bearer {store.api_key}"},
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/responses",
                    json={"model": "gpt-5.6-terra", "input": "test", "stream": streaming},
                )
            )
            await until(lambda: app.state.admission.waiting == 1)
            await asyncio.sleep(0.05)
            app.state.plan._slots.release()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 504
            assert response.json()["error"]["code"] == "upstream_timeout"
        assert app.state.admission.active == app.state.admission.waiting == 0
        assert app.state.plan._slots._value == 1


@pytest.mark.parametrize(
    "name,value",
    [
        ("CHATGPT_GATEWAY_MAX_QUEUED_REQUESTS", "-1"),
        ("CHATGPT_GATEWAY_ADMISSION_TIMEOUT_SECONDS", "0"),
        ("CHATGPT_GATEWAY_ADMISSION_TIMEOUT_SECONDS", "nan"),
        ("CHATGPT_GATEWAY_ADMISSION_TIMEOUT_SECONDS", "inf"),
        ("CHATGPT_GATEWAY_TIMEOUT_SECONDS", "nan"),
    ],
)
def test_queue_environment_limits_are_validated(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_queue_environment_is_loaded_and_defaults_keep_twelve(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_GATEWAY_ADMISSION_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("CHATGPT_GATEWAY_MAX_QUEUED_REQUESTS", "24")
    settings = Settings.from_env()
    assert settings.max_concurrency == 12
    assert settings.admission_timeout_seconds == 45 and settings.max_queued_requests == 24


@pytest.mark.parametrize("streaming", [False, True])
async def test_timeout_after_first_event_closes_transport_and_releases_slot(
    settings, store, streaming
):
    class Delayed(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created","response":{"id":"test"}}\n\n'
            await asyncio.sleep(10)
            raise AssertionError("The inference deadline must stop this stream")

    async def handler(request):
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"models": [{"slug": "gpt-5.6-terra"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed())

    app = create_app(
        replace(settings, timeout_seconds=0.04, max_concurrency=1),
        transport=httpx.MockTransport(handler),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=settings.base_url,
            headers={"authorization": f"Bearer {store.api_key}"},
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/v1/responses",
                    json={
                        "model": "gpt-5.6-terra",
                        "input": "test",
                        "stream": streaming,
                    },
                ),
                2,
            )
            assert response.status_code == (200 if streaming else 504)
            assert "upstream_timeout" in response.text and "response.completed" not in response.text
        assert app.state.admission.active == 0 and app.state.plan._slots._value == 1
