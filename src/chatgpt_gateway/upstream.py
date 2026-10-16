import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .auth import Auth
from .config import RESOURCE, Settings
from .errors import GatewayError
from .protocol import Prepared, assistant_message, validate_output
from .storage import Store

logger = logging.getLogger(__name__)
CATALOG_TTL = 60
CATALOG_STALE_LIMIT = 300
CATALOG_RETRY_DELAY = 15

PLAN_ERROR_STATUS = {
    "subscription_sharing_user_not_eligible": 403,
    "subscription_sharing_usage_limit_exceeded": 429,
    "subscription_sharing_usage_unavailable": 503,
    "subscription_sharing_unsupported_capability": 400,
    "subscription_sharing_route_not_supported": 403,
    "subscription_sharing_invalid_user": 401,
    "subscription_sharing_user_unavailable": 503,
    "chatpass_v2_scope_not_authorized": 403,
    "chatpass_v2_invalid_authorization_context": 403,
}


def upstream_error(response: httpx.Response) -> GatewayError:
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = None
    except ValueError:
        body = None
    detail = (body or {}).get("error")
    detail = detail if isinstance(detail, dict) else {}
    return GatewayError(
        response.status_code,
        str(detail.get("message") or (body or {}).get("detail") or "OpenAI rejected the request."),
        str(detail.get("code") or "upstream_error"),
        detail.get("param"),
        body=body,
        request_id=response.headers.get("x-request-id")
        or response.headers.get("openai-request-id"),
        retry_after=response.headers.get("retry-after"),
    )


async def sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield decode_event("\n".join(data))
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield decode_event("\n".join(data))


def decode_event(value: str) -> dict:
    try:
        event = json.loads(value)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("Invalid event")
        return event
    except ValueError as error:
        raise GatewayError(
            502, "OpenAI returned an invalid event stream.", "invalid_upstream_stream"
        ) from error


class PlanClient:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        auth: Auth,
        client: httpx.AsyncClient,
        *,
        slots: asyncio.Semaphore | None = None,
    ):
        self.settings, self.store, self.auth, self.client = settings, store, auth, client
        self._slots = slots if slots is not None else asyncio.Semaphore(settings.max_concurrency)
        self._catalog: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._catalog_lock = asyncio.Lock()
        self._catalog_retry_at: dict[str, float] = {}

    async def catalog(self, account: dict, *, fresh: bool = False) -> list[dict]:
        # One refresh at a time, so parallel inference does not hammer a failing listing.
        async with self._catalog_lock:
            return await self._catalog_for(account, fresh=fresh)

    async def _catalog_for(self, account: dict, *, fresh: bool) -> list[dict]:
        cached = self._catalog.get(account["id"])
        age = time.monotonic() - cached[0] if cached else float("inf")
        if (
            cached
            and not fresh
            and (
                age < CATALOG_TTL
                or (
                    age < CATALOG_STALE_LIMIT
                    and time.monotonic() < self._catalog_retry_at.get(account["id"], 0)
                )
            )
        ):
            return cached[1]
        try:
            models = await self._fetch_catalog(account)
        except (GatewayError, httpx.TransportError) as error:
            recoverable = isinstance(error, httpx.TransportError) or (
                isinstance(error, GatewayError)
                and (
                    error.status in {408, 429, 500, 502, 503, 504}
                    or (error.status == 401 and error.code == "token_expired")
                )
            )
            age = time.monotonic() - cached[0] if cached else float("inf")
            if not recoverable:
                self._catalog.pop(account["id"], None)
                self._catalog_retry_at.pop(account["id"], None)
            if fresh or not cached or age >= CATALOG_STALE_LIMIT or not recoverable:
                raise
            self._catalog_retry_at[account["id"]] = time.monotonic() + CATALOG_RETRY_DELAY
            logger.warning(
                "Using recent model catalogue after upstream listing failure",
                extra={"code": getattr(error, "code", "transport_error"), "age_seconds": int(age)},
            )
            # Listing is metadata, not authorization: inference still goes upstream with
            # this account's credentials and its real failures remain failures.
            return cached[1]
        self._catalog[account["id"]] = (time.monotonic(), models)
        self._catalog_retry_at.pop(account["id"], None)
        return models

    async def _fetch_catalog(self, account: dict) -> list[dict]:
        response = await self.client.get(
            f"{RESOURCE}/models", headers={"Authorization": f"Bearer {account['access_token']}"}
        )
        if response.status_code == 401 and upstream_error(response).code == "token_expired":
            renewed = await self.auth.credentials(rejected=account)
            if renewed["access_token"] != account["access_token"]:
                account.update(renewed)
                response = await self.client.get(
                    f"{RESOURCE}/models",
                    headers={"Authorization": f"Bearer {account['access_token']}"},
                )
        if response.status_code != 200:
            raise upstream_error(response)
        payload = response.json()
        rows = payload.get("models", payload.get("data"))
        if not isinstance(rows, list):
            raise GatewayError(
                502, "OpenAI returned an invalid model catalog.", "invalid_model_catalog"
            )
        models = []
        for row in rows:
            if not isinstance(row, dict) or row.get("visibility", "list") != "list":
                continue
            slug = row.get("slug") or row.get("id")
            if not isinstance(slug, str) or not slug.startswith(("gpt-", "o1", "o3", "o4")):
                continue
            if slug.startswith(("gpt-image", "chatgpt-image")):
                continue
            models.append(
                {
                    "id": f"openai/{slug}",
                    "object": "model",
                    "created": 0,
                    "owned_by": "openai",
                    "name": row.get("display_name") or slug,
                    "canonical_slug": slug,
                    "gateway_provider": "chatgpt_plan",
                    "access_verified": False,
                }
            )
        return models

    async def events(
        self,
        prepared: Prepared,
        *,
        request_id: str,
        endpoint: str,
        requested_model: str,
        client_name: str,
    ) -> AsyncIterator[dict]:
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=1)
        except TimeoutError as error:
            raise GatewayError(
                429, "The gateway is busy. Try again shortly.", "gateway_busy", retry_after="1"
            ) from error
        started = time.monotonic()
        outcome = "interrupted"
        code = None
        status = 499
        usage = None
        upstream_id = None
        account = None
        began = False
        first_token_ms = None
        actual_model = prepared.body["model"]
        completed_items: dict[int, dict] = {}
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                account = await self.auth.credentials()
                models = await self.catalog(account)
                if actual_model not in {model["canonical_slug"] for model in models}:
                    raise GatewayError(
                        404,
                        "This model is not listed for the selected ChatGPT account.",
                        "model_not_available",
                        "model",
                    )
                self.store.journal(
                    {
                        "request_id": request_id,
                        "event": "started",
                        "endpoint": endpoint,
                        "requested_model": requested_model,
                        "model": actual_model,
                        "client": client_name,
                        "account": account["id"],
                        "provider": "chatgpt_plan",
                        "billing_source": "chatgpt_plan",
                        "body_sha256": hashlib.sha256(
                            json.dumps(prepared.body, sort_keys=True).encode()
                        ).hexdigest(),
                        "reasoning": prepared.body.get("reasoning"),
                        "format": (prepared.schema or {}).get("type", "text"),
                        "stream": prepared.stream,
                        "data_policy_override": prepared.data_policy_override,
                    }
                )
                began = True
                async with self.client.stream(
                    "POST",
                    f"{RESOURCE}/responses",
                    json=prepared.body,
                    headers={
                        "Authorization": f"Bearer {account['access_token']}",
                        "Accept": "text/event-stream",
                    },
                ) as response:
                    upstream_id = response.headers.get("x-request-id") or response.headers.get(
                        "openai-request-id"
                    )
                    if response.status_code != 200:
                        await response.aread()
                        raise upstream_error(response)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    content_type = content_type.strip().lower()
                    # A live plan response may omit this header while still carrying SSE.
                    # The parser and validated terminal event remain the completion gate.
                    if content_type and content_type != "text/event-stream":
                        raise GatewayError(
                            502,
                            "OpenAI did not return a Responses event stream.",
                            "invalid_upstream_stream",
                        )
                    async for event in sse_events(response):
                        kind = event["type"]
                        if kind == "response.output_text.delta" and first_token_ms is None:
                            first_token_ms = round((time.monotonic() - started) * 1000)
                        if kind == "response.output_item.done":
                            index, item = event.get("output_index"), event.get("item")
                            if type(index) is not int or index < 0 or not isinstance(item, dict):
                                raise GatewayError(
                                    502,
                                    "OpenAI returned an invalid finished output item.",
                                    "invalid_upstream_stream",
                                    request_id=upstream_id,
                                )
                            completed_items[index] = item
                        if kind in {"response.failed", "response.incomplete", "error"}:
                            terminal = event.get("response") or {}
                            usage = terminal.get("usage")
                            failure = terminal.get("error") or event.get("error") or event
                            code = str(failure.get("code") or "response_incomplete")
                            http_status = PLAN_ERROR_STATUS.get(code, 502)
                            raise GatewayError(
                                http_status,
                                str(failure.get("message") or "The inference did not complete."),
                                code,
                                failure.get("param"),
                                request_id=upstream_id,
                            )
                        if kind == "response.completed":
                            completed = event.get("response") or {}
                            if completed.get("status") != "completed":
                                raise GatewayError(
                                    502,
                                    "The completion event did not confirm a completed response.",
                                    "invalid_upstream_stream",
                                    request_id=upstream_id,
                                )
                            # The live route can leave terminal output empty; item.done
                            # carries the finished content. Never recover from partial deltas.
                            if completed.get("output") in (None, []) and completed_items:
                                indices = sorted(completed_items)
                                if indices != list(range(len(indices))):
                                    raise GatewayError(
                                        502,
                                        "The stream omitted a finished output item.",
                                        "invalid_upstream_stream",
                                        request_id=upstream_id,
                                    )
                                completed = {
                                    **completed,
                                    "output": [completed_items[i] for i in indices],
                                }
                                event = {**event, "response": completed}
                            usage = completed.get("usage")
                            returned_model = completed.get("model")
                            if not isinstance(returned_model, str) or not (
                                returned_model == actual_model
                                or returned_model.startswith(f"{actual_model}-")
                            ):
                                raise GatewayError(
                                    502,
                                    "The completed response named a different or missing model.",
                                    "model_mismatch",
                                )
                            actual_model = returned_model
                            if endpoint == "chat.completions":
                                assistant_message(completed)
                            validate_output(completed, prepared.schema)
                            outcome, status = "completed", 200
                            yield event
                            return
                        yield event
                    raise GatewayError(
                        502,
                        "The upstream stream ended before response.completed.",
                        "incomplete_stream",
                    )
        except TimeoutError as error:
            outcome, status, code = "failed", 504, "upstream_timeout"
            raise GatewayError(
                status, "The inference exceeded the gateway timeout.", code
            ) from error
        except httpx.HTTPError as error:
            outcome, status, code = "failed", 502, "upstream_connection_error"
            raise GatewayError(status, "The OpenAI connection was interrupted.", code) from error
        except GatewayError as error:
            outcome, status, code = "failed", error.status, error.code
            raise
        finally:
            self._slots.release()
            if began or outcome == "failed":
                self.store.journal(
                    {
                        "request_id": request_id,
                        "event": outcome if began else "preflight_failed",
                        "status": status,
                        "code": code,
                        "model": actual_model,
                        "requested_model": requested_model,
                        "account": account["id"] if account else None,
                        "provider": "chatgpt_plan",
                        "billing_source": "chatgpt_plan",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "first_token_ms": first_token_ms,
                        "upstream_request_id": upstream_id,
                        "usage": usage,
                        "data_policy_override": prepared.data_policy_override,
                    }
                )
