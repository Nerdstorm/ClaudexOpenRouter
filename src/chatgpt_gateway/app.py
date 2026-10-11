import asyncio
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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .auth import Auth
from .config import Settings
from .errors import GatewayError
from .protocol import Prepared, chat_request, completion, responses_request, usage_counts
from .storage import Store, public_account
from .upstream import PlanClient


def error_headers(error: GatewayError) -> dict[str, str]:
    headers = {}
    if error.request_id:
        headers["x-upstream-request-id"] = error.request_id
    if error.retry_after:
        headers["retry-after"] = error.retry_after
    return headers


def sse(value: dict | str) -> bytes:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


async def read_body(request: Request, limit: int) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise GatewayError(
                413, "The request exceeds the gateway size limit.", "request_too_large"
            )
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Not an object")
        return value
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise GatewayError(400, "Send a JSON object.", "invalid_json") from error


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
                app.state.plan = PlanClient(settings, store, app.state.auth, client)
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Cookies are never used for API authentication; foreign web pages cannot start actions.
        origin = request.headers.get("origin")
        if origin and origin != settings.base_url:
            return JSONResponse(
                {
                    "error": {
                        "message": "Cross-origin requests are disabled.",
                        "code": "foreign_origin",
                    }
                },
                status_code=403,
            )
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, error: GatewayError):
        return JSONResponse(error.payload(), status_code=error.status, headers=error_headers(error))

    @app.exception_handler(httpx.HTTPError)
    async def handle_transport_error(request: Request, error: httpx.HTTPError):
        return JSONResponse(
            GatewayError(
                502, "OpenAI is temporarily unreachable.", "upstream_connection_error"
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
        return {"active": public_account(account) if account else None, "version": __version__}

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
        return {
            "provider": "chatgpt_plan",
            "chat_completions": True,
            "responses": True,
            "structured_output": "subject_to_model_access",
            "image_input": "subject_to_model_access",
            "image_generation": False,
            "billing_fallback": False,
            "chat_web_search": "openrouter_native_subject_to_model_access",
            "data_controls": "chatgpt_account",
            "openrouter_data_policy_override_enabled": settings.accept_chatgpt_data_controls,
            "unsupported_parameters": [
                "temperature",
                "top_p",
                "max_tokens",
                "max_completion_tokens",
                "seed",
            ],
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
        account = await app.state.auth.credentials()
        return {"object": "list", "data": await app.state.plan.catalog(account, fresh=True)}

    async def infer(request: Request, *, chat: bool):
        authorize(request)
        body = await read_body(request, settings.max_request_bytes)
        prepared = (
            chat_request(body, accept_chatgpt_data_controls=settings.accept_chatgpt_data_controls)
            if chat
            else responses_request(body)
        )
        request_id = uuid.uuid4().hex
        events = app.state.plan.events(
            prepared,
            request_id=request_id,
            endpoint="chat.completions" if chat else "responses",
            requested_model=body["model"],
            client_name=request.headers.get("x-title", "unspecified")[:100],
        )
        headers = {"x-gateway-request-id": request_id, "x-gateway-provider": "chatgpt_plan"}
        headers["x-gateway-data-controls"] = "chatgpt_account"
        if prepared.data_policy_override:
            headers["x-gateway-policy-override"] = "provider.data_collection"
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
    @app.post("/v1/images/edits")
    @app.post("/api/v1/images")
    async def unsupported_image_generation(request: Request):
        authorize(request)
        raise GatewayError(
            400,
            "Image generation and editing are unavailable through the ChatGPT-plan route.",
            "unsupported_capability",
            "image_generation",
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
