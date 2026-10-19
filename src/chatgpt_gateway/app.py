import asyncio
import base64
import hmac
import html
import json
import time
import uuid
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from starlette.datastructures import Headers, MutableHeaders, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .admission import Admission
from .anthropic_protocol import JSON_INSTRUCTION_VERSION, claude_request
from .anthropic_upstream import ClaudeClient
from .auth import Auth
from .claude_code import ClaudeCodeClient
from .claude_code_protocol import CLAUDE_CODE_CONTRACT, subscription_request
from .codex_images import IMAGE_CONTRACT_VERSION, CodexImageClient
from .config import Settings
from .errors import GatewayError, invalid
from .image_protocol import IMAGE_MODEL, image_chat_request, image_request, image_type
from .protocol import Prepared, chat_request, completion, responses_request, usage_counts
from .storage import Store, public_account
from .upstream import PlanClient


def error_headers(error: GatewayError) -> dict[str, str]:
    headers = dict(error.headers)
    if error.request_id:
        headers["x-upstream-request-id"] = error.request_id
    if error.retry_after:
        headers["retry-after"] = error.retry_after
    return headers


async def annotated_events(events, headers):
    try:
        async for event in events:
            yield event
    except GatewayError as error:
        error.headers.update(headers)
        raise
    finally:
        await events.aclose()


def sse(value: dict | str) -> bytes:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


class LocalBoundary:
    """Preserve the HTTP receive channel so queued callers can disconnect."""

    def __init__(self, app: ASGIApp, base_url: str):
        self.app, self.base_url = app, base_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "no-store"
                headers["x-content-type-options"] = "nosniff"
            await send(message)

        origin = Headers(scope=scope).get("origin")
        if origin and origin != self.base_url:
            response = JSONResponse(
                {
                    "error": {
                        "message": "Cross-origin requests are disabled.",
                        "code": "foreign_origin",
                    }
                },
                status_code=403,
            )
            return await response(scope, receive, secured_send)
        await self.app(scope, receive, secured_send)


async def read_bytes(request: Request, limit: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise GatewayError(
                413, "The request exceeds the gateway size limit.", "request_too_large"
            )
    return bytes(data)


async def read_body(request: Request, limit: int) -> dict[str, Any]:
    data = await read_bytes(request, limit)
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Not an object")
        return value
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise GatewayError(400, "Send a JSON object.", "invalid_json") from error


async def disconnected(request: Request, stopped: asyncio.Event):
    while not stopped.is_set():
        if await request.is_disconnected():
            return
        try:
            await asyncio.wait_for(stopped.wait(), 0.25)
        except TimeoutError:
            pass


async def completed_job(request: Request, work):
    """Cancel queued or running work when its HTTP client goes away."""
    job = asyncio.create_task(work)
    stopped = asyncio.Event()
    disconnect = asyncio.create_task(disconnected(request, stopped))
    try:
        done, _ = await asyncio.wait([job, disconnect], return_when=asyncio.FIRST_COMPLETED)
        if job not in done:
            raise GatewayError(499, "The client disconnected.", "client_disconnected")
        return await job
    finally:
        # Starlette probes use cancellation scopes: stop the probe cooperatively.
        stopped.set()
        job.cancel()
        await asyncio.gather(job, disconnect, return_exceptions=True)


async def managed_events(request: Request, events, *, streaming: bool):
    """Keep transport/timeout ownership in one task, including before streaming headers."""
    queue = asyncio.Queue(maxsize=1)
    stopped = asyncio.Event()

    async def produce():
        try:
            async for event in events:
                await queue.put(event)
        finally:
            await events.aclose()

    producer = asyncio.create_task(produce())
    disconnect = asyncio.create_task(disconnected(request, stopped))
    pending = None
    try:
        while True:
            pending = asyncio.create_task(queue.get())
            watched = {pending, producer}
            if disconnect is not None:
                watched.add(disconnect)
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if disconnect is not None and disconnect in done:
                raise GatewayError(499, "The client disconnected.", "client_disconnected")
            if pending not in done and producer in done and queue.empty():
                if producer.cancelled():
                    raise GatewayError(504, "The inference timed out.", "upstream_timeout")
                await producer
                return
            event = await pending
            pending = None
            if streaming and disconnect is not None:
                # StreamingResponse owns HTTP disconnects once the first event is ready.
                stopped.set()
                await disconnect
                disconnect = None
            yield event
    finally:
        stopped.set()
        producer.cancel()
        if pending is not None:
            pending.cancel()
        await asyncio.gather(
            producer,
            *([pending] if pending is not None else []),
            *([disconnect] if disconnect is not None else []),
            return_exceptions=True,
        )


async def read_image_body(request: Request, limit: int) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return await read_body(request, limit)
    data = await read_bytes(request, limit)

    async def parts():
        yield data

    try:
        form = await MultiPartParser(
            request.headers, parts(), max_files=5, max_fields=20, max_part_size=limit
        ).parse()
    except MultiPartException as error:
        raise invalid("Invalid image multipart request.", "image") from error
    body, images = {}, []
    try:
        for name, value in form.multi_items():
            if name in {"image", "image[]"}:
                if not isinstance(value, UploadFile):
                    raise invalid("Multipart reference images must be uploaded files.", "image")
                content = await value.read(limit + 1)
                images.append(
                    f"data:{image_type(content)};base64,{base64.b64encode(content).decode()}"
                )
            else:
                if name in body or not isinstance(value, str):
                    raise invalid("Image parameters must be supplied once as text fields.", name)
                body[name] = value
        if images:
            body["image"] = images
        if "n" in body:
            if body["n"] != "1":
                raise invalid("Codex image requests support n=1 only.", "n")
            body["n"] = 1
        return body
    finally:
        await form.close()


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Only one gateway may rotate the saved token set for this data directory.
        lock = FileLock(settings.data_dir / "gateway.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise RuntimeError("A gateway already uses this data directory.") from error
        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(settings.timeout_seconds, connect=20),
                follow_redirects=False,
            ) as client:
                app.state.auth = Auth(store, client, settings.callback_uri)
                slots = asyncio.Semaphore(settings.max_concurrency)
                admission = app.state.admission = Admission(settings)
                app.state.plan = PlanClient(
                    settings, store, app.state.auth, client, slots=slots, admission=admission
                )
                app.state.claude = ClaudeClient(
                    settings, store, client, slots=slots, admission=admission
                )
                app.state.images = CodexImageClient(
                    settings, store, slots=slots, admission=admission
                )
                app.state.claude_code = ClaudeCodeClient(
                    settings, store, slots=slots, admission=admission
                )
                yield
        finally:
            lock.release()

    app = FastAPI(
        title="ChatGPT Plan Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.store = store
    app.mount(
        "/static", StaticFiles(directory=str(files("chatgpt_gateway") / "static")), name="static"
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "host.docker.internal"],
    )

    app.add_middleware(LocalBoundary, base_url=settings.base_url)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, error: GatewayError):
        return JSONResponse(error.payload(), status_code=error.status, headers=error_headers(error))

    @app.exception_handler(httpx.HTTPError)
    async def handle_transport_error(request: Request, error: httpx.HTTPError):
        return JSONResponse(
            GatewayError(
                502,
                "The upstream provider is temporarily unreachable.",
                "upstream_connection_error",
            ).payload(),
            status_code=502,
        )

    def authorize(request: Request) -> None:
        expected = f"Bearer {store.api_key}"
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise GatewayError(401, "A valid local gateway key is required.", "invalid_api_key")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__, "provider": "chatgpt_plan"}

    @app.get("/")
    async def home():
        return {
            "name": "ChatGPT Plan Gateway",
            "version": __version__,
            "base_url": f"{settings.base_url}/v1",
            "connect": "Run chatgpt-gateway login; complete Continue with ChatGPT in your browser.",
            "usage_settings": "https://chatgpt.com/settings/usage",
        }

    @app.get("/gateway/status")
    async def status(request: Request):
        authorize(request)
        account = store.active()
        return {
            "active": public_account(account) if account else None,
            "version": __version__,
            "anthropic": {
                "configured": bool(settings.anthropic_api_key),
                "billing_source": "anthropic_api",
            },
            "codex": await app.state.images.availability(),
            "claude_code": await app.state.claude_code.availability(fresh=True),
        }

    @app.get("/gateway/accounts")
    async def accounts(request: Request):
        authorize(request)
        active = (store.active() or {}).get("id")
        return {
            "active_id": active,
            "accounts": [public_account(item) for item in store.accounts()],
        }

    @app.get("/gateway/capabilities")
    async def capabilities(request: Request):
        authorize(request)
        images = await app.state.images.availability()
        claude_code = await app.state.claude_code.availability()
        return {
            "provider": "chatgpt_plan",
            "chat_completions": True,
            "max_concurrency": settings.max_concurrency,
            "concurrency": {
                "shared_max": settings.max_concurrency,
                "image_max": settings.image_max_concurrency,
                "images_use_shared_pool": True,
                "claude_code_max": settings.claude_code_max_concurrency,
                "claude_code_uses_shared_pool": True,
                "admission_timeout_seconds": settings.admission_timeout_seconds,
                "queue": settings.max_queued_requests > 0,
                "queue_max": settings.max_queued_requests,
                "queue_persistent": False,
                "waiting": app.state.admission.waiting,
                "active": app.state.admission.active,
            },
            "responses": True,
            "structured_output": "subject_to_model_access",
            "image_input": "subject_to_model_access",
            "image_generation": images["ready"],
            "billing_fallback": False,
            "chat_web_search": "openrouter_native_subject_to_model_access",
            "data_controls": "chatgpt_account",
            "openrouter_data_policy_override_enabled": settings.accept_chatgpt_data_controls,
            "providers": {
                "claude_code": {
                    **claude_code,
                    "base_url": f"{settings.base_url}/claude-code/v1",
                    "chat_completions": True,
                    "responses": False,
                    "streaming": False,
                    "history": "one_user_message",
                    "structured_output_mode": "validated_json",
                    "structured_output_version": JSON_INSTRUCTION_VERSION,
                    "contract_version": CLAUDE_CODE_CONTRACT,
                    "max_concurrency": settings.claude_code_max_concurrency,
                    "max_tokens_default": settings.claude_code_max_tokens,
                    "max_turns": 1,
                    "prompt_caching": "cli_managed",
                    "billing_source": "claude_subscription",
                    "billing_controls": "claude_account",
                    "extra_usage_verified_disabled": False,
                    "access_verified": False,
                    "openrouter_data_policy_override_enabled": (
                        settings.accept_anthropic_data_controls
                    ),
                },
                "codex": {
                    **images,
                    "image_generation": images["ready"],
                    "model": IMAGE_MODEL if images["ready"] else None,
                    "image_model": "codex_default",
                    "selectable_image_model": False,
                    "image_model_verified": False,
                    "streaming": False,
                    "image_max_concurrency": settings.image_max_concurrency,
                    "billing_source": "codex_plan",
                    "data_controls": "codex_account",
                    "max_output_images": 1,
                    "max_reference_images": 5,
                    "size": ["auto"],
                    "quality": ["auto"],
                    "background": ["auto"],
                    "output_format": ["png"],
                    "reference_input": ["base64_data_url", "multipart"],
                    "contract_version": IMAGE_CONTRACT_VERSION,
                    "access_verified": False,
                    "openrouter_data_policy_override_enabled": (
                        settings.accept_chatgpt_data_controls
                    ),
                },
                "chatgpt_plan": {"chat_completions": True, "responses": True},
                "anthropic": {
                    "configured": bool(settings.anthropic_api_key),
                    "chat_completions": True,
                    "responses": False,
                    "structured_output": "json_schema",
                    "structured_output_mode": settings.anthropic_structured_output,
                    "structured_output_version": JSON_INSTRUCTION_VERSION
                    if settings.anthropic_structured_output == "validated_json"
                    else None,
                    "image_input": False,
                    "prompt_caching": {
                        "ttl": ["5m", "1h"],
                        "system_default": settings.anthropic_system_cache_ttl,
                    },
                    "max_tokens_default": settings.anthropic_max_tokens,
                    "billing_source": "anthropic_api",
                    "billing_controls": "claude_console",
                    "included_credit_balance_verified": False,
                    "openrouter_data_policy_override_enabled": (
                        settings.accept_anthropic_data_controls
                    ),
                },
            },
            "unsupported_parameters": [
                "temperature",
                "top_p",
                "max_tokens",
                "max_completion_tokens",
                "seed",
            ],
            "unsupported_parameters_scope": "chatgpt_plan",
        }

    @app.post("/gateway/accounts/select")
    async def select_account(request: Request):
        authorize(request)
        body = await read_body(request, 4096)
        account_id = body.get("account_id")
        if not isinstance(account_id, str) or not store.select(account_id):
            raise GatewayError(404, "That saved account was not found.", "account_not_found")
        return {"active": public_account(store.active())}

    @app.post("/gateway/login")
    async def login(request: Request):
        authorize(request)
        body = await read_body(request, 4096)
        if not isinstance(body.get("new", False), bool) or (
            body.get("account_id") is not None and not isinstance(body["account_id"], str)
        ):
            raise GatewayError(400, "Invalid sign-in selection.", "invalid_account_selection")
        return {
            "authorization_url": app.state.auth.start(
                body.get("account_id"), new=body.get("new", False)
            )
        }

    @app.get("/auth/callback")
    async def callback(request: Request):
        try:
            account = await app.state.auth.callback(dict(request.query_params))
            text = (
                "ChatGPT is connected. You can close this window and return to your application."
                if account["plan_enabled"]
                else "Sign-in succeeded, but ChatGPT plan usage was not enabled. "
                "Run login again to enable it."
            )
            return callback_page("Connection ready", text)
        except (GatewayError, httpx.HTTPError) as error:
            # Never reflect callback parameters or token endpoint messages into this page.
            code = error.code if isinstance(error, GatewayError) else "connection_error"
            reason = code if code in {"access_denied", "invalid_state"} else "sign_in_failed"
            return callback_page(
                "Sign-in did not complete",
                f"Return to the gateway and start sign-in again. Reason: {reason}",
                status=400,
            )

    @app.post("/gateway/logout")
    async def logout(request: Request):
        authorize(request)
        confirmed = await app.state.auth.logout()
        return {"signed_out": True, "revocation_confirmed": confirmed}

    @app.get("/gateway/requests")
    async def requests(request: Request, limit: int = 100):
        authorize(request)
        if not 1 <= limit <= 1000:
            raise GatewayError(400, "limit must be between 1 and 1000.", "invalid_limit")
        return {"events": store.recent(limit)}

    @app.get("/v1/models")
    @app.get("/api/v1/models")
    async def models(request: Request):
        authorize(request)
        rows = []
        if (store.active() or {}).get("access_token") or (
            not settings.anthropic_api_key and not settings.codex_images_enabled
        ):
            account = await app.state.auth.credentials()
            rows.extend(await app.state.plan.catalog(account, fresh=True))
        if settings.anthropic_api_key:
            rows.extend(await app.state.claude.catalog(fresh=True))
        rows.extend(await app.state.images.catalog())
        return {"object": "list", "data": rows}

    @app.get("/claude-code/v1/models")
    async def claude_code_models(request: Request):
        authorize(request)
        return {"object": "list", "data": await app.state.claude_code.catalog()}

    @app.post("/claude-code/v1/chat/completions")
    async def claude_code_completion(request: Request):
        authorize(request)
        body = await read_body(request, settings.max_request_bytes)
        prepared = await asyncio.to_thread(subscription_request, body, settings)
        request_id = uuid.uuid4().hex
        headers = {
            "x-gateway-request-id": request_id,
            "x-gateway-provider": "claude_code",
            "x-gateway-billing-source": "claude_subscription",
            "x-gateway-data-controls": "claude_account",
            "x-gateway-native-contract-version": CLAUDE_CODE_CONTRACT,
        }
        if prepared.schema and prepared.schema["type"] == "json_schema":
            headers["x-gateway-structured-output-mode"] = "validated_json"
            headers["x-gateway-structured-output-version"] = JSON_INSTRUCTION_VERSION
        if prepared.data_policy_override:
            headers["x-gateway-policy-override"] = "provider.data_collection"
        try:
            result = await completed_job(
                request,
                app.state.claude_code.generate(
                    prepared,
                    request_id=request_id,
                    client_name=request.headers.get("x-title", "unspecified")[:100],
                ),
            )
            return JSONResponse(result, headers=headers)
        except GatewayError as error:
            error.headers.update(headers)
            raise

    async def image_infer(request: Request, body: dict, *, chat: bool = False, edit: bool = False):
        prepared = (
            await asyncio.to_thread(image_chat_request, body, settings)
            if chat
            else (await asyncio.to_thread(image_request, body, settings, edit=edit))
        )
        request_id = uuid.uuid4().hex
        headers = {
            "x-gateway-request-id": request_id,
            "x-gateway-provider": "codex",
            "x-gateway-billing-source": "codex_plan",
            "x-gateway-data-controls": "codex_account",
            "x-gateway-image-model": "codex_default",
            "x-gateway-image-contract-version": IMAGE_CONTRACT_VERSION,
        }
        if prepared.data_policy_override:
            headers["x-gateway-policy-override"] = "provider.data_collection"
        work = app.state.images.generate(
            prepared,
            request_id=request_id,
            endpoint="chat.completions.images"
            if chat
            else "images.edits"
            if edit
            else "images.generations",
            client_name=request.headers.get("x-title", "unspecified")[:100],
        )
        try:
            result = await completed_job(request, work)
            if chat:
                result = {
                    "id": f"chatcmpl-{request_id}",
                    "object": "chat.completion",
                    "created": result["created"],
                    "model": IMAGE_MODEL,
                    "provider": "codex",
                    "image_model": "codex_default",
                    "image_model_verified": False,
                    "usage": None,
                    "gateway_usage": result["gateway_usage"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "images": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": "data:image/png;base64,"
                                            + result["data"][0]["b64_json"]
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            return JSONResponse(result, headers=headers)
        except GatewayError as error:
            error.headers.update(headers)
            raise

    async def infer(request: Request, *, chat: bool):
        authorize(request)
        body = await read_body(request, settings.max_request_bytes)
        if isinstance(body.get("model"), str) and body["model"].startswith("codex/"):
            if not chat:
                raise invalid(
                    "codex/imagegen is available on image and Chat endpoints only.", "model"
                )
            return await image_infer(request, body, chat=True)
        is_claude = isinstance(body.get("model"), str) and body["model"].startswith("anthropic/")
        if is_claude:
            if not chat:
                raise invalid(
                    "Claude is available through /v1/chat/completions, not /v1/responses.", "model"
                )
            prepared = claude_request(body, settings)
            upstream = app.state.claude
        else:
            prepared = (
                chat_request(
                    body, accept_chatgpt_data_controls=settings.accept_chatgpt_data_controls
                )
                if chat
                else responses_request(body)
            )
            upstream = app.state.plan
        request_id = uuid.uuid4().hex
        events = upstream.events(
            prepared,
            request_id=request_id,
            endpoint="chat.completions" if chat else "responses",
            requested_model=body["model"],
            client_name=request.headers.get("x-title", "unspecified")[:100],
        )
        headers = {"x-gateway-request-id": request_id, "x-gateway-provider": prepared.provider}
        headers["x-gateway-data-controls"] = "claude_console" if is_claude else "chatgpt_account"
        headers["x-gateway-billing-source"] = "anthropic_api" if is_claude else "chatgpt_plan"
        if prepared.structured_output_mode:
            headers["x-gateway-structured-output-mode"] = prepared.structured_output_mode
        if prepared.structured_output_version:
            headers["x-gateway-structured-output-version"] = prepared.structured_output_version
        if prepared.data_policy_override:
            headers["x-gateway-policy-override"] = "provider.data_collection"
        events = managed_events(
            request, annotated_events(events, headers), streaming=prepared.stream
        )
        if not prepared.stream:
            try:
                async for event in events:
                    if event["type"] == "response.completed":
                        result = event["response"]
                        return JSONResponse(
                            completion(result, request_id, body["model"]) if chat else result,
                            headers=headers,
                        )
            finally:
                await events.aclose()
            raise GatewayError(502, "No completed inference was received.", "incomplete_stream")
        # Read admission/first event before sending HTTP 200 to preserve real HTTP failures.
        try:
            first = await anext(events)
        except StopAsyncIteration as error:
            raise GatewayError(
                502, "No inference stream was received.", "incomplete_stream"
            ) from error
        stream = (
            chat_stream(events, first, prepared, request_id, body["model"])
            if chat
            else native_stream(events, first)
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    @app.post("/v1/chat/completions")
    @app.post("/api/v1/chat/completions")
    async def chat_completions(request: Request):
        return await infer(request, chat=True)

    @app.post("/v1/responses")
    @app.post("/api/v1/responses")
    async def responses(request: Request):
        return await infer(request, chat=False)

    @app.post("/v1/images/generations")
    @app.post("/api/v1/images/generations")
    @app.post("/api/v1/images")
    async def image_generations(request: Request):
        authorize(request)
        return await image_infer(
            request, await read_image_body(request, settings.max_request_bytes)
        )

    @app.post("/v1/images/edits")
    @app.post("/api/v1/images/edits")
    async def image_edits(request: Request):
        authorize(request)
        return await image_infer(
            request,
            await read_image_body(request, settings.max_request_bytes),
            edit=True,
        )

    return app


def callback_page(title: str, message: str, *, status: int = 200) -> HTMLResponse:
    template = (files("chatgpt_gateway") / "static" / "callback.html").read_text()
    content = template.replace("{{title}}", html.escape(title)).replace(
        "{{message}}", html.escape(message)
    )
    return HTMLResponse(content, status_code=status)


async def native_stream(events, first):
    try:
        yield sse(first)
        async for event in events:
            yield sse(event)
    except GatewayError as error:
        yield sse({"type": "error", **error.payload()})
    finally:
        await events.aclose()


async def chat_stream(events, first, prepared: Prepared, request_id: str, model: str):
    created = int(time.time())

    def chunk(delta: dict, finish: str | None = None):
        result = {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return result

    async def chunks(event):
        nonlocal model
        kind = event["type"]
        if kind == "response.output_text.delta":
            yield sse(chunk({"content": event["delta"]}))
        elif kind == "response.refusal.delta":
            yield sse(chunk({"refusal": event["delta"]}))
        elif kind == "response.completed":
            served = event["response"]["model"]
            model = f"openai/{served}" if model.startswith("openai/") else served
            yield sse(chunk({}, "stop"))
            if prepared.include_usage:
                usage_chunk = chunk({})
                usage_chunk.update(
                    choices=[],
                    usage=usage_counts(event["response"].get("usage"), event["response"]),
                )
                yield sse(usage_chunk)
            yield sse("[DONE]")

    try:
        yield sse(chunk({"role": "assistant"}))
        async for output in chunks(first):
            yield output
        async for event in events:
            async for output in chunks(event):
                yield output
    except GatewayError as error:
        yield sse(error.payload())
        yield sse("[DONE]")
    except asyncio.CancelledError:
        raise
    finally:
        await events.aclose()
