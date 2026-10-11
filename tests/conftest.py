import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from chatgpt_gateway.app import create_app
from chatgpt_gateway.config import AUTH_ISSUER, Settings
from chatgpt_gateway.storage import Store


def account(**changes) -> dict:
    return {
        "issuer": AUTH_ISSUER,
        "subject": "test-subject",
        "client_id": "oaiapp_test",
        "email": "tester@example.test",
        "access_token": "test-access-secret",
        "refresh_token": "test-refresh-secret",
        "id_token": "test-id-secret",
        "scopes": ["chatgpt.tokens.use.direct", "resource.invoke", "offline_access"],
        "expires_at": time.time() + 3600,
        **changes,
    }


def completed(text: str = "Hello", **changes) -> dict:
    return {
        "id": "resp_test",
        "model": "gpt-5.6-terra",
        "created_at": 123,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": text, "annotations": []},
                ],
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
        **changes,
    }


@dataclass
class Backend:
    events: list[dict] | None = None
    content_type: str | None = "text/event-stream"
    status: int = 200
    error_body: dict[str, Any] = field(default_factory=lambda: {"detail": "Unavailable"})
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "api.openai.com":
            raise AssertionError("No unexpected network/auth requests in inference tests")
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-5.6-terra", "display_name": "Terra", "visibility": "list"},
                        {"slug": "gpt-5.6-sol", "display_name": "Sol", "visibility": "list"},
                        {"slug": "gpt-hidden", "visibility": "hide"},
                        {"slug": "gpt-image-2", "visibility": "list"},
                    ]
                },
            )
        assert request.url.path == "/v1/responses"
        if self.status != 200:
            return httpx.Response(
                self.status,
                json=self.error_body,
                headers={
                    "x-request-id": "upstream-id",
                    "retry-after": "30",
                },
            )
        events = (
            self.events
            if self.events is not None
            else [
                {"type": "response.created", "response": {"id": "resp_test"}},
                {"type": "response.output_text.delta", "delta": "Hello"},
                {"type": "response.completed", "response": completed()},
            ]
        )
        data = ": keepalive\n\n" + "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        )
        headers = {"x-request-id": "upstream-id"}
        if self.content_type is not None:
            headers["content-type"] = self.content_type
        return httpx.Response(200, content=data.encode(), headers=headers)


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "state")


@pytest.fixture
def store(settings):
    value = Store(settings.data_dir)
    value.save(account(), active=True)
    return value


@pytest.fixture
def client(settings, store, backend):
    app = create_app(settings, transport=httpx.MockTransport(backend.handler))
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        yield client
