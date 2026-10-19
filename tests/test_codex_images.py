import asyncio
import base64
import io
import json
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from PIL import Image
from starlette.requests import Request

from chatgpt_gateway.app import create_app
from chatgpt_gateway.codex_images import CodexImageClient, worker_environment
from chatgpt_gateway.config import Settings
from chatgpt_gateway.errors import GatewayError
from chatgpt_gateway.image_protocol import IMAGE_MODEL, ImageRequest

REQUEST = {"model": IMAGE_MODEL, "prompt": "One ceramic mug, no text."}


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def codex(settings, tmp_path, monkeypatch, png):
    home = tmp_path / "native-codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    binary = tmp_path / "fake-codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        + '''import base64, json, os, sys, time, uuid
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
mode = (home / "mode").read_text() if (home / "mode").exists() else "success"
if sys.argv[1:] == ["login", "status"]:
    print("Logged in using API key" if mode == "api_key" else "Logged in using ChatGPT")
    sys.exit(0)
assert sys.argv[1] == "exec"
assert sys.argv[-2:] == ["--", "-"]
assert not any(k.startswith(("OPENAI_", "ANTHROPIC_", "CHATGPT_GATEWAY_")) for k in os.environ)
workdir = Path(sys.argv[sys.argv.index("--cd") + 1])
prompt = sys.stdin.read()
assert prompt
if "--image" in sys.argv:
    images = sys.argv[sys.argv.index("--image") + 1:sys.argv.index("--")]
    assert images and all(Path(p).is_file() for p in images)
with (home / "calls.jsonl").open("a") as f:
    record={"pid":os.getpid(),"workdir":str(workdir),"references":"--image" in sys.argv}
    f.write(json.dumps(record) + "\\n")
def emit(v): print(json.dumps(v), flush=True)
thread = str(uuid.uuid4())
emit({"type":"thread.started","thread_id":thread})
if mode == "hang": time.sleep(60)
if mode in {"error", "quota"}:
    message="usage limit exceeded" if mode=="quota" else "private failure details"
    emit({"type":"turn.failed","error":{"message":message}})
    sys.exit(1)
directory = home / "generated_images" / thread
directory.mkdir(parents=True)
path = directory / "output.png"
path.write_bytes(base64.b64decode("'''
        + base64.b64encode(png).decode()
        + '''"))
if mode == "invalid_image": path.write_bytes(b"not an image")
if mode == "outside":
    path = home / "other-image.png"
    path.write_bytes(base64.b64decode("'''
        + base64.b64encode(png).decode()
        + """"))
if mode == "symlink":
    target=home / "secret-image.png"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
if mode == "no_image": path.unlink()
manifest={"image_path": str(path)}
if mode == "empty": manifest["image_path"]=""
emit({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(manifest)}})
if mode != "incomplete":
    emit({"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":3}})
"""
    )
    binary.chmod(0o700)
    return replace(settings, codex_images_enabled=True, codex_binary=str(binary))


@pytest.fixture
def image_client(codex, store, backend):
    app = create_app(codex, transport=httpx.MockTransport(backend.handler))
    with TestClient(app, base_url=codex.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        yield client


def mode(codex, name):
    Path(os.environ["CODEX_HOME"], "mode").write_text(name)


def test_catalog_and_capabilities_distinguish_default_engine_and_shared_limits(image_client):
    rows = image_client.get("/v1/models").json()["data"]
    assert [row["id"] for row in rows][-1] == IMAGE_MODEL
    image = rows[-1]
    assert image["architecture"]["output_modalities"] == ["image"]
    assert image["image_model"] == "codex_default"
    assert not image["selectable_image_model"] and not image["image_model_verified"]
    assert not image["access_verified"]
    caps = image_client.get("/gateway/capabilities").json()
    assert caps["image_generation"] is True
    assert caps["concurrency"] == {
        "shared_max": 12,
        "image_max": 1,
        "images_use_shared_pool": True,
        "claude_code_max": 1,
        "claude_code_uses_shared_pool": True,
        "admission_timeout_seconds": 120,
        "queue": True,
        "queue_max": 48,
        "queue_persistent": False,
        "waiting": 0,
        "active": 0,
    }
    assert caps["providers"]["codex"]["quality"] == ["auto"]


def test_codex_only_catalog_does_not_require_gateway_oauth(codex, backend):
    app = create_app(codex, transport=httpx.MockTransport(backend.handler))
    with TestClient(app, base_url=codex.base_url) as client:
        client.headers["authorization"] = f"Bearer {app.state.store.api_key}"
        assert [r["id"] for r in client.get("/v1/models").json()["data"]] == [IMAGE_MODEL]
    assert not backend.requests


def test_signed_out_gateway_registration_does_not_hide_native_images(image_client, store, backend):
    store.clear_tokens(store.active()["id"])
    rows = image_client.get("/v1/models").json()["data"]
    assert [row["id"] for row in rows] == [IMAGE_MODEL]
    assert not backend.requests


def test_disabled_and_api_key_only_cli_are_not_advertised(client, codex, backend):
    assert all(row["id"] != IMAGE_MODEL for row in client.get("/v1/models").json()["data"])
    mode(codex, "api_key")
    app = create_app(
        replace(codex, data_dir=codex.data_dir / "api-key-only"),
        transport=httpx.MockTransport(backend.handler),
    )
    with TestClient(app, base_url=codex.base_url) as configured:
        configured.headers["authorization"] = f"Bearer {app.state.store.api_key}"
        assert configured.get("/v1/models").json()["data"] == []
        assert configured.get("/gateway/capabilities").json()["image_generation"] is False
        response = configured.post("/v1/images/generations", json=REQUEST)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "codex_chatgpt_login_required"


@pytest.mark.parametrize(
    "path", ["/v1/images/generations", "/api/v1/images/generations", "/api/v1/images"]
)
def test_generation_returns_valid_png_and_metadata_without_text_upstream(
    image_client, store, backend, path
):
    response = image_client.post(path, json=REQUEST)
    assert response.status_code == 200, response.text
    body = response.json()
    with Image.open(io.BytesIO(base64.b64decode(body["data"][0]["b64_json"]))) as image:
        assert image.format == "PNG"
    assert body["model"] == IMAGE_MODEL and body["image_model"] == "codex_default"
    assert body["usage"] is None and body["gateway_usage"]["image"] is None
    assert body["gateway_usage"]["agent"]["input_tokens"] == 10
    assert response.headers["x-gateway-billing-source"] == "codex_plan"
    assert not backend.requests
    journal = store.recent()
    assert [row["event"] for row in journal] == ["started", "completed"]
    assert journal[-1]["usage"]["agent"]["output_tokens"] == 3
    assert REQUEST["prompt"] not in json.dumps(journal)
    assert body["data"][0]["b64_json"] not in json.dumps(journal)
    home = Path(os.environ["CODEX_HOME"])
    calls = [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 1 and not Path(calls[0]["workdir"]).exists()
    assert not list((home / "generated_images").iterdir())


def test_real_openai_sdk_generation_and_multipart_edit(image_client, store, png):
    sdk = OpenAI(
        base_url="http://127.0.0.1:8317/api/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=image_client,
    )
    assert sdk.images.generate(**REQUEST).data[0].b64_json
    assert sdk.images.edit(**REQUEST, image=("reference.png", png, "image/png")).data[0].b64_json
    calls = Path(os.environ["CODEX_HOME"], "calls.jsonl").read_text().splitlines()
    assert json.loads(calls[-1])["references"] is True


@pytest.mark.parametrize("path", ["/v1/images/edits", "/api/v1/images/edits"])
def test_json_edit_accepts_data_url_reference(image_client, png, path):
    reference = "data:image/png;base64," + base64.b64encode(png).decode()
    assert image_client.post(path, json={**REQUEST, "image": reference}).status_code == 200
    assert image_client.post(path, json=REQUEST).status_code == 400


def test_openrouter_chat_image_output_and_reference(image_client, png):
    reference = "data:image/png;base64," + base64.b64encode(png).decode()
    response = image_client.post(
        "/api/v1/chat/completions",
        json={
            "model": IMAGE_MODEL,
            "modalities": ["image", "text"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Make the mug blue"},
                        {"type": "image_url", "image_url": {"url": reference}},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["images"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert body["usage"] is None and body["image_model_verified"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"model": "openai/gpt-image-2"},
        {"model": "openai/gpt-image-2.5-flare"},
        {"model": "codex/sunburst"},
        {"n": True},
        {"n": 2},
        {"quality": "high"},
        {"size": "1024x1024"},
        {"seed": 1},
        {"background": "transparent"},
        {"response_format": "url"},
        {"output_format": "jpeg"},
        {"stream": True},
        {"image": "https://example.test/image.png"},
        {"image": "file:///secret"},
        {"image": "data:image/png;base64,bad!"},
        {"prompt": ""},
        {"provider": {"only": ["anthropic"]}},
        {"provider": {"allow_fallbacks": True}},
        {"provider": {"data_collection": "deny"}},
    ],
)
def test_unsupported_requests_do_not_start_worker(image_client, change):
    response = image_client.post("/v1/images/generations", json={**REQUEST, **change})
    assert response.status_code == 400
    assert not Path(os.environ["CODEX_HOME"], "calls.jsonl").exists()


@pytest.mark.parametrize(
    "name,status,code",
    [
        ("no_image", 422, "image_not_produced"),
        ("empty", 422, "image_not_produced"),
        ("outside", 422, "image_not_produced"),
        ("symlink", 422, "image_not_produced"),
        ("invalid_image", 422, "image_not_produced"),
        ("incomplete", 502, "incomplete_stream"),
        ("error", 502, "codex_image_failed"),
        ("quota", 429, "codex_usage_limit"),
    ],
)
def test_worker_failures_never_masquerade_as_images_or_retry(
    image_client, codex, store, name, status, code
):
    mode(codex, name)
    response = image_client.post("/v1/images/generations", json=REQUEST)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["x-gateway-provider"] == "codex"
    assert len(Path(os.environ["CODEX_HOME"], "calls.jsonl").read_text().splitlines()) == 1
    assert "private failure details" not in response.text
    assert store.recent()[-1]["event"] == "failed"
    if status == 422:
        assert store.recent()[-1]["usage"]["agent"]["input_tokens"] == 10


async def test_timeout_and_cancellation_kill_worker_and_release_slots(codex, store):
    worker = CodexImageClient(
        replace(codex, timeout_seconds=0.15), store, slots=asyncio.Semaphore(1)
    )
    mode(codex, "hang")
    with pytest.raises(GatewayError, match="timed out"):
        await worker.generate(
            ImageRequest("test"), request_id="timeout", endpoint="test", client_name="test"
        )
    assert store.recent()[-1]["code"] == "upstream_timeout"
    worker.settings = replace(codex, timeout_seconds=10)
    task = asyncio.create_task(
        worker.generate(
            ImageRequest("test"), request_id="cancel", endpoint="test", client_name="test"
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.recent()[-1]["event"] == "interrupted"
    mode(codex, "success")
    result = await worker.generate(
        ImageRequest("test"), request_id="next", endpoint="test", client_name="test"
    )
    assert result["data"]
    calls = await asyncio.to_thread(Path(os.environ["CODEX_HOME"], "calls.jsonl").read_text)
    for call in calls.splitlines():
        with pytest.raises(ProcessLookupError):
            os.kill(json.loads(call)["pid"], 0)


async def test_image_pool_shares_global_capacity_and_rejects_busy(codex, store, monkeypatch):
    slots = asyncio.Semaphore(2)
    worker = CodexImageClient(replace(codex, admission_timeout_seconds=0.02), store, slots=slots)
    running, release = asyncio.Event(), asyncio.Event()

    async def execute(_):
        running.set()
        await release.wait()
        return b"test", {"agent": {}, "image": None}

    monkeypatch.setattr(worker, "_execute", execute)
    first = asyncio.create_task(
        worker.generate(ImageRequest("test"), request_id="one", endpoint="test", client_name="test")
    )
    await running.wait()
    # One image occupies one shared slot; the second slot is available for text.
    await asyncio.wait_for(slots.acquire(), 0.1)
    slots.release()
    with pytest.raises(GatewayError) as busy:
        await worker.generate(
            ImageRequest("test"), request_id="two", endpoint="test", client_name="test"
        )
    assert busy.value.code == "gateway_busy"
    release.set()
    await first
    await asyncio.wait_for(slots.acquire(), 0.1)
    await asyncio.wait_for(slots.acquire(), 0.1)


async def test_full_shared_pool_cannot_be_bypassed_by_image_pool(codex, store, monkeypatch):
    slots = asyncio.Semaphore(0)
    worker = CodexImageClient(replace(codex, admission_timeout_seconds=0.02), store, slots=slots)
    with pytest.raises(GatewayError) as busy:
        await worker.generate(
            ImageRequest("test"), request_id="busy", endpoint="test", client_name="test"
        )
    assert busy.value.code == "gateway_busy"
    assert slots.locked()
    slots.release()
    result = await worker.generate(
        ImageRequest("test"), request_id="next", endpoint="test", client_name="test"
    )
    assert result["data"]
    await asyncio.wait_for(slots.acquire(), 0.1)


async def test_http_disconnect_cancels_image_task_and_records_interruption(
    codex, store, monkeypatch
):
    app = create_app(codex)
    async with app.router.lifespan_context(app):
        running = asyncio.Event()
        messages = asyncio.Queue()
        await messages.put(
            {"type": "http.request", "body": json.dumps(REQUEST).encode(), "more_body": False}
        )

        async def execute(_):
            running.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(app.state.images, "_execute", execute)
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/images/generations",
                "headers": [(b"authorization", ("Bearer " + store.api_key).encode())],
            },
            receive=messages.get,
        )
        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", None) == "/v1/images/generations"
        )
        task = asyncio.create_task(endpoint(request))
        await asyncio.wait_for(running.wait(), 2)
        await messages.put({"type": "http.disconnect"})
        with pytest.raises(GatewayError) as interrupted:
            await asyncio.wait_for(task, 2)
        assert interrupted.value.status == 499
        assert store.recent()[-1]["event"] == "interrupted"
        await asyncio.wait_for(app.state.images._image_slots.acquire(), 0.1)


def test_cli_capabilities_lists_the_effective_configuration(image_client, monkeypatch, capsys):
    from chatgpt_gateway.cli import main

    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(image_client.app.state.store.directory))
    monkeypatch.setattr("chatgpt_gateway.cli.httpx.Client", lambda **_: nullcontext(image_client))
    monkeypatch.setattr(sys, "argv", ["chatgpt-gateway", "capabilities"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["concurrency"]["shared_max"] == 12
    assert result["providers"]["codex"]["selectable_image_model"] is False


def test_worker_environment_does_not_inherit_funding_or_session_variables(monkeypatch):
    for name in [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CODEX_API_KEY",
        "CODEX_THREAD_ID",
        "CHATGPT_GATEWAY_KEY",
    ]:
        monkeypatch.setenv(name, "must-not-reach-worker")
    assert "must-not-reach-worker" not in worker_environment().values()


async def test_unsupported_worker_platform_is_not_advertised(codex, store, monkeypatch):
    worker = CodexImageClient(codex, store, slots=asyncio.Semaphore(1))
    monkeypatch.setattr("chatgpt_gateway.codex_images.os", SimpleNamespace(name="nt"))
    assert await worker.catalog() == []
    assert (await worker.availability())["reason"] == "codex_platform_unsupported"


@pytest.mark.parametrize(
    "name,value",
    [
        ("CHATGPT_GATEWAY_CODEX_IMAGES", "yes"),
        ("CHATGPT_GATEWAY_IMAGE_MAX_CONCURRENCY", "0"),
        ("CHATGPT_GATEWAY_IMAGE_MAX_CONCURRENCY", "13"),
        ("CHATGPT_GATEWAY_CODEX_BINARY", ""),
    ],
)
def test_invalid_image_settings_are_refused(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()
