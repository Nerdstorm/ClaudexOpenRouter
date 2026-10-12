"""One-shot Anthropic API transport; no agent harness, retries or provider fallback."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator

import anthropic
import httpx

from .anthropic_protocol import CLAUDE_MODELS, cache_policy, claude_usage
from .config import Settings
from .errors import GatewayError
from .protocol import Prepared, usage_counts, validate_output
from .storage import Store

ERROR_STATUS = {
    "overloaded_error": 529,
    "rate_limit_error": 429,
    "authentication_error": 401,
    "permission_error": 403,
    "invalid_request_error": 400,
    "api_error": 500,
}


def api_error(error: anthropic.APIError) -> GatewayError:
    body = error.body if isinstance(error.body, dict) else {}
    detail = body.get("error", body)
    detail = detail if isinstance(detail, dict) else {}
    code = detail.get("type") or "anthropic_upstream_error"
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    # SDK errors raised from an HTTP-200 SSE stream can retain that transport status.
    # Never return it as a successful nonstreaming inference.
    if not isinstance(status, int) or not 400 <= status <= 599:
        status = ERROR_STATUS.get(code, 502)
    if (
        status == 400
        and code == "invalid_request_error"
        and str(detail.get("message", "")).startswith(
            ("The compiled grammar is too large", "Schema is too complex for compilation")
        )
    ):
        # Classify a known error without forwarding a message that may echo private input.
        return GatewayError(
            400,
            "Anthropic cannot compile this output schema. Select the gateway's explicit "
            "validated_json mode or simplify the schema.",
            "schema_compilation_limit",
            "response_format.json_schema.schema",
            request_id=getattr(error, "request_id", None),
        )
    return GatewayError(
        status,
        "Anthropic rejected or interrupted the request.",
        str(code),
        request_id=getattr(error, "request_id", None),
        retry_after=response.headers.get("retry-after") if response is not None else None,
    )


class ClaudeClient:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: httpx.AsyncClient,
        *,
        slots: asyncio.Semaphore,
    ):
        self.settings, self.store, self._slots = settings, store, slots
        self.api = (
            anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                base_url="https://api.anthropic.com",
                http_client=client,
                max_retries=0,
                timeout=settings.timeout_seconds,
            )
            if settings.anthropic_api_key
            else None
        )
        self._catalog: tuple[float, list[dict]] | None = None

    def require_api(self) -> anthropic.AsyncAnthropic:
        if self.api is None:
            raise GatewayError(
                503,
                "Configure an Anthropic API key to enable Claude.",
                "provider_not_configured",
            )
        return self.api

    async def catalog(self, *, fresh: bool = False) -> list[dict]:
        api = self.require_api()
        if self._catalog and not fresh and time.time() - self._catalog[0] < 60:
            return self._catalog[1]
        try:
            page = await api.models.list(limit=100)
            available = {row.id async for row in page}
        except anthropic.APIError as error:
            raise api_error(error) from error
        rows = [
            {
                "id": slug,
                "object": "model",
                "created": 0,
                "owned_by": "anthropic",
                "name": name,
                "canonical_slug": native,
                "gateway_provider": "anthropic",
                "access_verified": False,
                "billing_source": "anthropic_api",
            }
            for slug, (native, name) in CLAUDE_MODELS.items()
            if native in available
        ]
        self._catalog = (time.time(), rows)
        return rows

    async def events(
        self,
        prepared: Prepared,
        *,
        request_id: str,
        endpoint: str,
        requested_model: str,
        client_name: str,
    ) -> AsyncIterator[dict]:
        api = self.require_api()
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=1)
        except TimeoutError as error:
            raise GatewayError(
                429, "The gateway is busy. Try again shortly.", "gateway_busy", retry_after="1"
            ) from error
        started, first_token = time.monotonic(), None
        outcome, status, code = "interrupted", 499, None
        usage, upstream_id, began = None, None, False
        native = prepared.body["model"]
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                rows = await self.catalog()
                if requested_model not in {r["id"] for r in rows}:
                    raise GatewayError(
                        404,
                        "This Claude model is not listed for the API key.",
                        "model_not_available",
                        "model",
                    )
                self.store.journal(
                    {
                        "request_id": request_id,
                        "event": "started",
                        "endpoint": endpoint,
                        "requested_model": requested_model,
                        "model": native,
                        "client": client_name,
                        "provider": "anthropic",
                        "billing_source": "anthropic_api",
                        "account": None,
                        "body_sha256": hashlib.sha256(
                            json.dumps(prepared.body, sort_keys=True).encode()
                        ).hexdigest(),
                        "reasoning": prepared.body.get("output_config", {}).get("effort"),
                        "format": (prepared.schema or {}).get("type", "text"),
                        "stream": prepared.stream,
                        "cache_policy": cache_policy(prepared.body),
                        "data_policy_override": prepared.data_policy_override,
                        "structured_output_mode": prepared.structured_output_mode,
                        "structured_output_version": prepared.structured_output_version,
                    }
                )
                began = True
                seen_start, seen_stop, seen_delta = False, False, False
                open_blocks, finished_blocks = set(), set()
                async with api.messages.stream(**prepared.body) as stream:
                    upstream_id = stream.request_id
                    async for event in stream:
                        raw = event.model_dump(mode="json", exclude_unset=True, warnings=False)
                        kind = raw["type"]
                        if seen_stop and kind != "ping":
                            raise GatewayError(
                                502,
                                "Claude sent data after message_stop.",
                                "invalid_upstream_stream",
                            )
                        if kind == "message_start":
                            message = raw.get("message", {})
                            if seen_start or message.get("model") != native:
                                raise GatewayError(
                                    502,
                                    "Claude returned a different or missing model.",
                                    "model_mismatch",
                                )
                            seen_start = True
                            usage = message.get("usage")
                            yield {
                                "type": "response.created",
                                "response": {"id": message["id"], "model": requested_model},
                            }
                        elif kind == "content_block_start":
                            index, block = raw.get("index"), raw.get("content_block", {})
                            if (
                                not seen_start
                                or type(index) is not int
                                or index < 0
                                or index in open_blocks | finished_blocks
                            ):
                                raise GatewayError(
                                    502,
                                    "Claude returned an invalid content block.",
                                    "invalid_upstream_stream",
                                )
                            if block.get("type") == "fallback":
                                raise GatewayError(
                                    502, "Claude attempted model fallback.", "model_mismatch"
                                )
                            if block.get("type") not in {"text", "thinking", "redacted_thinking"}:
                                raise GatewayError(
                                    502,
                                    "Claude returned an unsupported tool/content block.",
                                    "unexpected_tool_call",
                                )
                            open_blocks.add(index)
                        elif kind == "content_block_delta":
                            if raw.get("index") not in open_blocks:
                                raise GatewayError(
                                    502,
                                    "Claude returned a delta without an open block.",
                                    "invalid_upstream_stream",
                                )
                            delta = raw.get("delta", {})
                            if delta.get("type") == "text_delta":
                                if first_token is None:
                                    first_token = round((time.monotonic() - started) * 1000)
                                yield {"type": "response.output_text.delta", "delta": delta["text"]}
                        elif kind == "content_block_stop":
                            index = raw.get("index")
                            if index not in open_blocks:
                                raise GatewayError(
                                    502,
                                    "Claude stopped an unopened content block.",
                                    "invalid_upstream_stream",
                                )
                            open_blocks.remove(index)
                            finished_blocks.add(index)
                        elif kind == "message_delta":
                            if not seen_start:
                                raise GatewayError(
                                    502, "Claude omitted message_start.", "invalid_upstream_stream"
                                )
                            seen_delta = True
                            delta = raw.get("delta", {})
                            if delta.get("model") not in (None, native):
                                raise GatewayError(
                                    502,
                                    "Claude changed model during the response.",
                                    "model_mismatch",
                                )
                            if isinstance(raw.get("usage"), dict):
                                usage = {**(usage or {}), **raw["usage"]}
                        elif kind == "message_stop":
                            if seen_stop or not seen_start or not seen_delta or open_blocks:
                                raise GatewayError(
                                    502,
                                    "Claude did not finish every content block.",
                                    "invalid_upstream_stream",
                                )
                            seen_stop = True
                    if not seen_stop:
                        raise GatewayError(
                            502, "Claude ended before message_stop.", "incomplete_stream"
                        )
                    message = await stream.get_final_message()
                    if message.model != native:
                        raise GatewayError(
                            502, "Claude completed with a different model.", "model_mismatch"
                        )
                    if message.stop_reason not in {"end_turn", "refusal"}:
                        raise GatewayError(
                            502,
                            "Claude did not finish the requested answer.",
                            "response_incomplete",
                        )
                    text = "".join(block.text for block in message.content if block.type == "text")
                    content = (
                        {"type": "refusal", "refusal": text}
                        if message.stop_reason == "refusal"
                        else {"type": "output_text", "text": text, "annotations": []}
                    )
                    result = {
                        "id": message.id,
                        "model": requested_model,
                        "created_at": int(time.time()),
                        "status": "completed",
                        "gateway_provider": "anthropic",
                        "output": [{"type": "message", "role": "assistant", "content": [content]}],
                        "usage": claude_usage(usage),
                    }
                    try:
                        validate_output(result, prepared.schema)
                    except GatewayError as failure:
                        if failure.code == "schema_validation_failed":
                            # A completed bad answer needs a corrected request, not a
                            # transport retry of the same prompt. Keep the billed usage.
                            failure.status = 422
                            failure.request_id = upstream_id
                            failure.body["error"]["type"] = "model_output_error"
                            failure.body.update(
                                provider="anthropic",
                                model=requested_model,
                                usage=usage_counts(result["usage"], result),
                            )
                        raise
                    outcome, status = "completed", 200
                    if message.stop_reason == "refusal":
                        outcome, code = "refused", "model_refusal"
                        yield {"type": "response.refusal.delta", "delta": text}
                    yield {"type": "response.completed", "response": result}
        except (anthropic.APITimeoutError, TimeoutError) as error:
            outcome, status, code = "failed", 504, "upstream_timeout"
            raise GatewayError(status, "Claude exceeded the gateway timeout.", code) from error
        except anthropic.APIError as error:
            failure = api_error(error)
            upstream_id = failure.request_id or upstream_id
            outcome, status, code = "failed", failure.status, failure.code
            raise failure from error
        except (ValueError, RuntimeError) as error:
            outcome, status, code = "failed", 502, "invalid_upstream_stream"
            raise GatewayError(status, "Claude returned an invalid event stream.", code) from error
        except GatewayError as error:
            outcome, status, code = "failed", error.status, error.code
            raise
        finally:
            self._slots.release()
            if began:
                self.store.journal(
                    {
                        "request_id": request_id,
                        "event": outcome,
                        "status": status,
                        "code": code,
                        "model": native,
                        "requested_model": requested_model,
                        "account": None,
                        "provider": "anthropic",
                        "billing_source": "anthropic_api",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "first_token_ms": first_token,
                        "upstream_request_id": upstream_id,
                        "usage": claude_usage(usage),
                        "data_policy_override": prepared.data_policy_override,
                        "structured_output_mode": prepared.structured_output_mode,
                        "structured_output_version": prepared.structured_output_version,
                    }
                )
