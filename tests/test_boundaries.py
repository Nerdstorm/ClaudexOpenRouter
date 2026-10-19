import asyncio
from dataclasses import replace

import anyio
import httpx
import pytest
from conftest import completed
from fastapi.testclient import TestClient
from filelock import FileLock
from openai import OpenAI
from pydantic import BaseModel

from chatgpt_gateway.app import create_app
from chatgpt_gateway.auth import Auth
from chatgpt_gateway.errors import GatewayError
from chatgpt_gateway.protocol import Prepared
from chatgpt_gateway.upstream import PlanClient


def test_native_sdk_schema_parse(client, backend, store):
    class Answer(BaseModel):
        answer: str

    backend.events = [{"type": "response.completed", "response": completed('{"answer":"yes"}')}]
    sdk = OpenAI(
        base_url="http://127.0.0.1:8317/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=client,
    )
    result = sdk.responses.parse(model="gpt-5.6-terra", input="Hi", text_format=Answer)
    assert result.output_parsed.answer == "yes"


@pytest.mark.parametrize(
    "body",
    [
        {"text": {"format": "bad"}},
        {"tools": [{"type": "namespace", "name": "x", "tools": ["bad"]}]},
        {"tools": [{"type": "namespace", "tools": []}]},
    ],
)
def test_malformed_native_fields_are_client_errors(client, backend, body):
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-terra",
            "input": "Hi",
            **body,
        },
    )
    assert response.status_code == 400 and not backend.requests


def test_remote_schema_references_never_trigger_network(client, backend):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.6-terra",
            "messages": [{"role": "user", "content": "Hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "bad",
                    "schema": {"$ref": "http://127.0.0.1:1/secret"},
                },
            },
        },
    )
    assert response.status_code == 400 and not backend.requests


def test_callback_replay_is_refused_by_http_route(client):
    response = client.get("/auth/callback?state=unknown&code=secret-code")
    assert response.status_code == 400 and "secret-code" not in response.text
    assert client.get("/static/callback.css").status_code == 200


def test_timed_out_inference_releases_slot_and_records_failure(settings, store):
    async def scenario():
        async def handler(request):
            if request.url.path.endswith("models"):
                return httpx.Response(200, json={"models": [{"slug": "gpt-5.6-terra"}]})
            await asyncio.sleep(1)
            raise AssertionError("Timeout should cancel this request")

        configured = replace(settings, timeout_seconds=0.02)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = Auth(store, client, settings.callback_uri)
            plan = PlanClient(configured, store, auth, client)
            for request_id in ("one", "two"):
                with pytest.raises(GatewayError) as error:
                    async for _ in plan.events(
                        Prepared(
                            {"model": "gpt-5.6-terra", "input": [], "store": False, "stream": True},
                            False,
                        ),
                        request_id=request_id,
                        endpoint="responses",
                        requested_model="gpt-5.6-terra",
                        client_name="test",
                    ):
                        pass
                assert error.value.code == "upstream_timeout"
        assert [e["event"] for e in store.recent()] == ["started", "failed", "started", "failed"]

    anyio.run(scenario)


def test_closing_stream_releases_slot_and_marks_interruption(settings, store, backend):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend.handler)) as client:
            auth = Auth(store, client, settings.callback_uri)
            plan = PlanClient(settings, store, auth, client)
            prepared = Prepared({"model": "gpt-5.6-terra"}, True)
            iterator = plan.events(
                prepared,
                request_id="one",
                endpoint="responses",
                requested_model="gpt-5.6-terra",
                client_name="test",
            )
            assert (await anext(iterator))["type"] == "response.created"
            await iterator.aclose()
            assert store.recent()[-1]["event"] == "interrupted"
            iterator = plan.events(
                prepared,
                request_id="two",
                endpoint="responses",
                requested_model="gpt-5.6-terra",
                client_name="test",
            )
            results = [event async for event in iterator]
            assert results[-1]["type"] == "response.completed"

    anyio.run(scenario)


def test_duplicate_gateway_cannot_race_saved_refresh_token(settings, backend):
    lock = FileLock(settings.data_dir / "gateway.lock")
    lock.acquire()
    try:
        app = create_app(settings, transport=httpx.MockTransport(backend.handler))
        with pytest.raises(RuntimeError, match="already uses"):
            with TestClient(app):
                pass
    finally:
        lock.release()


def test_served_model_snapshot_is_reported(client, backend):
    backend.events = [
        {
            "type": "response.completed",
            "response": completed(
                model="gpt-5.6-terra-2026-10-01",
            ),
        }
    ]
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-5.6-terra",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "openai/gpt-5.6-terra-2026-10-01"


def test_status_does_not_expose_saved_credentials(client, store):
    text = client.get("/gateway/status").text + client.get("/gateway/accounts").text
    assert "test-access-secret" not in text and "test-refresh-secret" not in text
    assert "test-id-secret" not in text and store.api_key not in text


def test_capabilities_describe_image_review_without_promising_generation(client, backend):
    response = client.get("/gateway/capabilities")
    assert response.status_code == 200
    assert response.json()["image_input"] == "subject_to_model_access"
    assert response.json()["image_generation"] is False
    assert response.json()["billing_fallback"] is False
    assert not backend.requests


@pytest.mark.parametrize("path", ["/v1/images/generations", "/v1/images/edits", "/api/v1/images"])
def test_api_image_model_ids_are_not_substituted_by_the_codex_route(client, backend, path):
    response = client.post(path, json={"model": "gpt-image-2", "prompt": "A bowl of rice"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == "model"
    assert not backend.requests
