import asyncio
import json
import os
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from pydantic import BaseModel
from test_anthropic import ClaudeBackend

from chatgpt_gateway.app import completed_job, create_app
from chatgpt_gateway.claude_code import ClaudeCodeClient, worker_environment
from chatgpt_gateway.claude_code_protocol import subscription_request
from chatgpt_gateway.cli import main
from chatgpt_gateway.config import Settings
from chatgpt_gateway.errors import GatewayError

MODEL = "anthropic/claude-sonnet-5.5"
REQUEST = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "Stable caller instructions."},
        {"role": "user", "content": "Private test question."},
    ],
}
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string", "const": "yes"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}


@pytest.fixture
def native_claude(settings, tmp_path, monkeypatch):
    home = tmp_path / "native-claude"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    binary = tmp_path / "fake-claude"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,sys,time
from pathlib import Path
home=Path(os.environ["CLAUDE_CONFIG_DIR"])
mode=(home/"mode").read_text() if (home/"mode").exists() else "success"
assert sys.argv[1:4] == ["--safe-mode","--setting-sources",""]
if sys.argv[4:] == ["--version"]:
 print("2.1.100 (Claude Code)" if mode == "old" else "2.1.289 (Claude Code)")
 sys.exit(0)
if sys.argv[4:] == ["auth","status","--json"]:
 print(json.dumps({"loggedIn":mode!="logged_out",
  "authMethod":"api_key" if mode=="api_key" else "claude.ai",
  "apiProvider":"firstParty","subscriptionType":"max"}))
 sys.exit(0)
assert "-p" in sys.argv and "--no-session-persistence" in sys.argv
assert "--disable-slash-commands" in sys.argv and "--no-chrome" in sys.argv
assert sys.argv[sys.argv.index("--tools")+1] == ""
assert sys.argv[sys.argv.index("--max-turns")+1] == "1"
assert json.loads(sys.argv[sys.argv.index("--mcp-config")+1]) == {"mcpServers":{}}
assert not any(k.startswith(("ANTHROPIC_","OPENAI_","CHATGPT_GATEWAY_")) for k in os.environ)
assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
assert os.environ["MAX_STRUCTURED_OUTPUT_RETRIES"] == "1"
system=Path(sys.argv[sys.argv.index("--system-prompt-file")+1])
assert system.stat().st_mode & 0o077 == 0
assert system.read_text().startswith("Stable caller instructions.")
prompt=sys.stdin.read()
assert prompt == "Private test question."
with (home/"calls.jsonl").open("a") as f:
 f.write(json.dumps({"pid":os.getpid(),"workdir":os.getcwd(),"max_tokens":os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],"args":sys.argv})+"\\n")
model=sys.argv[sys.argv.index("--model")+1]
def emit(v):print(json.dumps(v),flush=True)
emit({"type":"system","subtype":"init",
 "model":"other" if mode=="wrong_init" else model,
 "tools":["Bash"] if mode=="tools" else [],"mcp_servers":[],
 "apiKeySource":"api_key" if mode=="wrong_billing" else "none"})
if mode=="hang":time.sleep(60)
if mode=="stderr_hang":
 print("x"*100000,file=sys.stderr,flush=True)
 time.sleep(60)
if mode=="retry":
 emit({"type":"system","subtype":"api_retry","error":"rate_limit","error_status":429})
 time.sleep(60)
emit({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}})
emit({"type":"assistant","message":{
 "model":"other" if mode=="wrong_message" else model,
 "content":[{"type":"text","text":"partial"}]}})
if mode=="incomplete":sys.exit(0)
usage={"input_tokens":10,"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":5}
answer={"answer":"yes"}
result={"type":"result","subtype":"success","is_error":False,"num_turns":1,"stop_reason":"end_turn","result":json.dumps(answer),"usage":usage,"modelUsage":{model:{"provider":"firstParty","canonicalModel":model}}}
if mode=="invalid_json":result["result"]="private invalid answer"
if mode=="invalid_schema":result["result"]='{"answer":"no"}'
if mode=="missing_text":del result["result"]
if mode=="wrong_result":result["modelUsage"]={"other":{}}
if mode=="missing_models":del result["modelUsage"]
if mode=="wrong_canonical":result["modelUsage"][model]["canonicalModel"]="other"
if mode=="second_model":result["modelUsage"]["other"]={}
if mode=="truncated":result["stop_reason"]="max_tokens"
if mode=="many_turns":result["num_turns"]=2
if mode=="missing_usage":del result["usage"]
if mode=="failed":result.update(is_error=True,error="billing_error")
if mode=="refusal":result.update(stop_reason="refusal",result="Cannot help.")
emit(result)
if mode=="trailing":emit({"type":"assistant"})
"""
    )
    binary.chmod(0o700)
    return replace(settings, claude_code_enabled=True, claude_binary=str(binary))


@pytest.fixture
def native_client(native_claude, store, backend):
    app = create_app(native_claude, transport=httpx.MockTransport(backend.handler))
    with TestClient(app, base_url=native_claude.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        yield client


def mode(value):
    Path(os.environ["CLAUDE_CONFIG_DIR"], "mode").write_text(value)


def calls():
    path = Path(os.environ["CLAUDE_CONFIG_DIR"], "calls.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_catalog_and_capabilities_show_distinct_subscription_backend(native_client, backend):
    rows = native_client.get("/claude-code/v1/models").json()["data"]
    assert {r["id"] for r in rows} == {MODEL, "anthropic/claude-opus-5.5"}
    assert all(r["billing_source"] == "claude_subscription" for r in rows)
    assert all(r["access_verified"] is False for r in rows)
    assert not backend.requests
    caps = native_client.get("/gateway/capabilities").json()
    assert caps["providers"]["claude_code"]["base_url"].endswith("/claude-code/v1")
    assert caps["concurrency"]["claude_code_max"] == 1
    assert caps["providers"]["claude_code"]["prompt_caching"] == "cli_managed"
    regular = native_client.get("/v1/models").json()["data"]
    assert all(not r["id"].startswith("anthropic/") for r in regular)


@pytest.mark.parametrize("model", [MODEL, "anthropic/claude-opus-5.5"])
def test_exact_models_json_schema_usage_and_no_api_calls(native_client, backend, store, model):
    r = native_client.post(
        "/claude-code/v1/chat/completions",
        json={
            **REQUEST,
            "model": model,
            "response_format": SCHEMA,
            "max_tokens": 2048,
            "reasoning_effort": "low",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == model and body["provider"] == "claude_code"
    assert json.loads(body["choices"][0]["message"]["content"]) == {"answer": "yes"}
    assert body["usage"]["prompt_tokens"] == 60 and body["usage"]["total_tokens"] == 65
    assert body["gateway_usage"]["num_turns"] == 1
    assert r.headers["x-gateway-billing-source"] == "claude_subscription"
    assert r.headers["x-gateway-structured-output-mode"] == "validated_json"
    assert not backend.requests
    record = calls()[-1]
    assert record["max_tokens"] == "2048" and "--effort" in record["args"]
    assert not Path(record["workdir"]).exists()
    events = store.recent()
    assert [e["event"] for e in events] == ["started", "completed"]
    assert events[-1]["native_turns"] == 1
    for secret in ("Private test question", "Stable caller instructions", '"answer": "yes"'):
        assert secret not in json.dumps(events)


def test_official_openai_sdk_parse(native_client, store):
    class Answer(BaseModel):
        answer: str

    sdk = OpenAI(
        base_url="http://127.0.0.1:8317/claude-code/v1",
        api_key=store.api_key,
        max_retries=0,
        http_client=native_client,
    )
    result = sdk.chat.completions.parse(
        model=MODEL, messages=REQUEST["messages"], response_format=Answer
    )
    assert result.choices[0].message.parsed.answer == "yes"


def test_existing_api_route_is_preserved_and_native_failures_never_fall_back(
    native_claude, store, backend
):
    api = ClaudeBackend()

    def handle(request):
        if request.url.host == "api.anthropic.com":
            return api.handler(request)
        return backend.handler(request)

    app = create_app(
        replace(native_claude, anthropic_api_key="test-anthropic-secret"),
        transport=httpx.MockTransport(handle),
    )
    with TestClient(app, base_url=native_claude.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        response = client.post("/api/v1/chat/completions", json=REQUEST)
        assert response.status_code == 200
        assert response.headers["x-gateway-billing-source"] == "anthropic_api"
        count = len(api.requests)
        assert calls() == []
        mode("wrong_result")
        response = client.post("/claude-code/v1/chat/completions", json=REQUEST)
        assert response.status_code == 502
        assert response.headers["x-gateway-billing-source"] == "claude_subscription"
        assert len(api.requests) == count


def test_disabled_backend_fails_without_starting_cli(client):
    assert client.get("/claude-code/v1/models").json()["data"] == []
    response = client.post("/claude-code/v1/chat/completions", json=REQUEST)
    assert response.status_code == 503 and response.json()["error"]["code"] == "disabled"


def test_api_rates_never_price_native_subscription_usage(native_claude, store, backend):
    rates = {
        "as_of": "2026-10-19",
        "models": {
            "claude-sonnet-5-5": {
                "input": 10,
                "output": 50,
                "cache_read": 1,
                "cache_write_5m": 12,
                "cache_write_1h": 20,
            }
        },
    }
    app = create_app(
        replace(native_claude, anthropic_rates=rates),
        transport=httpx.MockTransport(backend.handler),
    )
    with TestClient(app, base_url=native_claude.base_url) as client:
        client.headers["authorization"] = f"Bearer {store.api_key}"
        response = client.post("/claude-code/v1/chat/completions", json=REQUEST)
    assert response.status_code == 200
    assert response.headers["x-gateway-billing-source"] == "claude_subscription"
    assert response.json()["usage"].get("cost") is None
    assert response.json()["usage"].get("cost_details") is None
    assert store.recent()[-1]["usage"].get("cost") is None
    assert not backend.requests


def test_subscription_policy_override_is_explicit(settings):
    strict = {
        **REQUEST,
        "provider": {"only": ["anthropic"], "allow_fallbacks": False, "data_collection": "deny"},
    }
    with pytest.raises(GatewayError) as error:
        subscription_request(strict, settings)
    assert error.value.param == "provider.data_collection"
    assert "Claude account" in error.value.message
    assert subscription_request(
        strict, replace(settings, accept_anthropic_data_controls=True)
    ).data_policy_override


@pytest.mark.parametrize("value", ["api_key", "logged_out", "old"])
def test_native_auth_and_version_must_qualify(native_client, value):
    mode(value)
    assert native_client.get("/claude-code/v1/models").json()["data"] == []
    r = native_client.post("/claude-code/v1/chat/completions", json=REQUEST)
    assert r.status_code == 503
    assert calls() == []


@pytest.mark.parametrize(
    "value,code,status",
    [
        ("wrong_init", "model_mismatch", 502),
        ("wrong_message", "model_mismatch", 502),
        ("wrong_result", "model_mismatch", 502),
        ("wrong_canonical", "model_mismatch", 502),
        ("missing_models", "model_mismatch", 502),
        ("second_model", "model_mismatch", 502),
        ("tools", "unexpected_tool_call", 502),
        ("wrong_billing", "billing_route_mismatch", 502),
        ("truncated", "response_incomplete", 502),
        ("many_turns", "invalid_claude_output", 502),
        ("missing_text", "invalid_claude_output", 502),
        ("trailing", "invalid_claude_output", 502),
        ("incomplete", "incomplete_claude_output", 502),
        ("stderr_hang", "claude_output_too_large", 502),
        ("retry", "claude_rate_limited", 429),
        ("failed", "claude_billing_error", 402),
        ("invalid_json", "schema_validation_failed", 422),
        ("invalid_schema", "schema_validation_failed", 422),
    ],
)
def test_native_failures_are_explicit_and_not_retried(native_client, store, value, code, status):
    mode(value)
    r = native_client.post(
        "/claude-code/v1/chat/completions", json={**REQUEST, "response_format": SCHEMA}
    )
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    assert r.headers["x-gateway-provider"] == "claude_code"
    assert len(calls()) == 1
    assert not os.path.exists(calls()[-1]["workdir"])
    if status == 422:
        assert r.json()["usage"]["total_tokens"] == 65
        assert "private invalid answer" not in r.text
    assert store.recent()[-1]["event"] == "failed"


@pytest.mark.parametrize(
    "extra",
    [
        {"stream": True},
        {"stream_options": {"include_usage": True}},
        {"cache_control": None},
        {"temperature": 0.2},
        {"model": "anthropic/claude-haiku-5.5"},
        {"tools": [{"type": "function"}]},
        {"max_tokens": 0},
        {
            "messages": [
                *REQUEST["messages"],
                {"role": "assistant", "content": "old"},
                {"role": "user", "content": "fix"},
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
                    ],
                }
            ]
        },
        {"provider": {"only": ["openai"]}},
        {"provider": {"allow_fallbacks": True}},
    ],
)
def test_unsupported_semantics_fail_before_native_execution(native_client, extra):
    r = native_client.post("/claude-code/v1/chat/completions", json={**REQUEST, **extra})
    assert r.status_code == 400
    assert calls() == []


def test_unknown_usage_stays_unknown_and_refusal_stays_separate(native_client, store):
    mode("missing_usage")
    r = native_client.post("/claude-code/v1/chat/completions", json=REQUEST)
    assert r.status_code == 200 and r.json()["usage"] is None
    assert store.recent()[-1]["usage"] is None
    mode("refusal")
    r = native_client.post(
        "/claude-code/v1/chat/completions", json={**REQUEST, "response_format": SCHEMA}
    )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["refusal"]
    assert store.recent()[-1]["event"] == "refused"


def test_worker_environment_excludes_api_keys_and_session_overrides(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "HTTP_PROXY",
        "CLAUDECODE",
    ):
        monkeypatch.setenv(key, "private")
        assert key not in worker_environment()


def test_cli_status_uses_native_auth_and_redacts_identity(native_claude, monkeypatch, capsys):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(native_claude.data_dir))
    monkeypatch.setenv("CHATGPT_GATEWAY_CLAUDE_BINARY", native_claude.claude_binary)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-for-native-worker")
    monkeypatch.setattr(sys, "argv", ["chatgpt-gateway", "claude-status"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }
    assert calls() == []


def test_cli_models_selects_subscription_path(native_client, native_claude, monkeypatch, capsys):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(native_claude.data_dir))
    monkeypatch.setattr(sys, "argv", ["chatgpt-gateway", "models", "--backend", "claude-code"])
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: nullcontext(native_client))
    main()
    rows = json.loads(capsys.readouterr().out)["data"]
    assert all(r["gateway_provider"] == "claude_code" for r in rows)


def test_cli_login_explicitly_selects_subscription_without_api_credentials(
    native_claude, monkeypatch, capsys
):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(native_claude.data_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-for-native-worker")
    monkeypatch.setattr(sys, "argv", ["chatgpt-gateway", "claude-login"])

    def login(args, **kwargs):
        assert args[-3:] == ["auth", "login", "--claudeai"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("chatgpt_gateway.cli.subprocess.run", login)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0 and capsys.readouterr().out == ""


def test_ordered_context_and_original_schema_are_preserved(settings):
    body = {
        **REQUEST,
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
            },
            {"role": "developer", "content": "third"},
            REQUEST["messages"][-1],
        ],
        "response_format": SCHEMA,
    }
    before = json.dumps(body)
    prepared = subscription_request(body, settings)
    assert prepared.system.startswith("first\n\nsecond\n\nthird\n\nYou are a JSON API.")
    assert prepared.schema["schema"] == SCHEMA["json_schema"]["schema"]
    assert json.dumps(body) == before


async def test_timeout_cancellation_and_both_admission_limits(native_claude, store):
    mode("hang")
    pool = asyncio.Semaphore(1)
    native = ClaudeCodeClient(
        replace(native_claude, timeout_seconds=0.2, admission_timeout_seconds=0.02),
        store,
        slots=pool,
    )
    prepared = subscription_request(REQUEST, native_claude)
    with pytest.raises(GatewayError) as error:
        await native.generate(prepared, request_id="timeout", client_name="test")
    assert error.value.code == "claude_timeout"
    assert store.recent()[-1]["status"] == 504
    assert not await asyncio.to_thread(os.path.exists, calls()[-1]["workdir"])
    assert pool._value == 1 and native._native_slots._value == 1
    native.settings = replace(native_claude, timeout_seconds=30)
    task = asyncio.create_task(native.generate(prepared, request_id="cancel", client_name="test"))
    for _ in range(100):
        if len(calls()) > 1:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(GatewayError) as busy:
        await native.generate(prepared, request_id="busy", client_name="test")
    assert busy.value.code == "gateway_busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool._value == 1 and native._native_slots._value == 1
    assert store.recent()[-1]["event"] == "interrupted"
    assert not await asyncio.to_thread(os.path.exists, calls()[-1]["workdir"])
    with pytest.raises(ProcessLookupError):
        os.kill(calls()[-1]["pid"], 0)
    await pool.acquire()
    with pytest.raises(GatewayError) as busy:
        await native.generate(prepared, request_id="shared_busy", client_name="test")
    assert busy.value.code == "gateway_busy" and native._native_slots._value == 1
    pool.release()


async def test_http_thirteenth_subscription_call_waits_without_exceeding_twelve(
    native_claude, store, backend, monkeypatch
):
    settings = replace(native_claude, claude_code_max_concurrency=12)
    app = create_app(settings, transport=httpx.MockTransport(backend.handler))
    async with app.router.lifespan_context(app):
        native = app.state.claude_code
        inspect = native.availability
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def wait_for_release(**kwargs):
            nonlocal count
            count += 1
            if count == 12:
                entered.set()
            await release.wait()
            return await inspect(**kwargs)

        monkeypatch.setattr(native, "availability", wait_for_release)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=settings.base_url,
            headers={"authorization": f"Bearer {store.api_key}"},
        ) as client:
            tasks = [
                asyncio.create_task(
                    client.post(
                        "/claude-code/v1/chat/completions",
                        json={
                            **REQUEST,
                            "model": MODEL if index % 2 else "anthropic/claude-opus-5.5",
                        },
                    )
                )
                for index in range(12)
            ]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                extra = asyncio.create_task(
                    client.post(
                        "/claude-code/v1/chat/completions",
                        json=REQUEST,
                    )
                )
                tasks.append(extra)
                await asyncio.sleep(1.05)
                assert count == app.state.admission.active == 12
                assert app.state.admission.waiting == 1 and not extra.done()
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
                assert all(r.status_code == 200 for r in responses)
                assert all(
                    r.headers["x-gateway-billing-source"] == "claude_subscription"
                    for r in responses
                )
                assert len(calls()) == 13 and not backend.requests
                last_id = responses[-1].headers["x-gateway-request-id"]
                events = [e for e in store.recent(100) if e["request_id"] == last_id]
                assert events[-1]["event"] == "completed"
                assert events[-1]["admission_wait_ms"] >= 1000
            finally:
                release.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert app.state.admission.active == app.state.admission.waiting == 0
        assert native._slots._value == native._native_slots._value == 12


async def test_http_disconnect_cancels_native_work():
    class Disconnected:
        async def is_disconnected(self):
            return True

    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    with pytest.raises(GatewayError) as error:
        await completed_job(Disconnected(), work())
    assert error.value.code == "client_disconnected" and cancelled.is_set()


@pytest.mark.parametrize(
    "name,value",
    [
        ("CHATGPT_GATEWAY_CLAUDE_CODE", "maybe"),
        ("CHATGPT_GATEWAY_CLAUDE_CODE_MAX_CONCURRENCY", "0"),
        ("CHATGPT_GATEWAY_CLAUDE_CODE_MAX_CONCURRENCY", "13"),
        ("CHATGPT_GATEWAY_CLAUDE_CODE_MAX_TOKENS", "0"),
        ("CHATGPT_GATEWAY_CLAUDE_BINARY", " "),
    ],
)
def test_invalid_subscription_settings(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv("CHATGPT_GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()
