"""Optional native Claude Code worker; login and credentials remain owned by the CLI."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .anthropic_protocol import CLAUDE_MODELS, JSON_INSTRUCTION_VERSION, claude_usage
from .claude_code_protocol import CLAUDE_CODE_CONTRACT, ClaudeCodeRequest
from .config import Settings
from .errors import GatewayError
from .native_process import stop_process
from .protocol import completion, usage_counts, validate_output
from .storage import Store

MAX_CLI_OUTPUT = 16 * 1024 * 1024


def worker_environment() -> dict[str, str]:
    # In print mode an inherited API key wins over subscription authentication.
    # Do not inherit credentials, provider overrides, proxy settings or parent sessions.
    allowed = {"PATH", "HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "LANG", "LC_ALL"}
    return {key: value for key, value in os.environ.items() if key in allowed}


async def bounded_pipe(stream: asyncio.StreamReader, limit: int = 8192) -> bytes:
    result = bytearray()
    while chunk := await stream.read(65536):
        if len(result) + len(chunk) > limit:
            raise GatewayError(
                502, "Claude Code output exceeded the worker limit.", "claude_output_too_large"
            )
        result.extend(chunk)
    return bytes(result)


def native_error(event: dict) -> GatewayError:
    # Classify without echoing native diagnostics, which can contain caller text.
    status = event.get("error_status")
    category = event.get("error")
    text = event.get("result")
    if isinstance(text, str) and text.lower().startswith(
        ("you've hit your limit", "usage limit reached")
    ):
        category = "rate_limit"
    if category in {"rate_limit", "overloaded"} or status in {429, 529}:
        return GatewayError(
            429, "Claude's subscription is rate limited or busy.", "claude_rate_limited"
        )
    if category in {"authentication_failed", "oauth_org_not_allowed"} or status in {401, 403}:
        return GatewayError(
            401, "Sign Claude Code in again with your subscription.", "claude_login_required"
        )
    if category in {"billing_error", "account_on_hold"} or status == 402:
        return GatewayError(
            402, "Claude requires account or usage-credit attention.", "claude_billing_error"
        )
    if category == "model_not_found" or status == 404:
        return GatewayError(
            404,
            "This model is unavailable on the Claude subscription.",
            "model_not_available",
            "model",
        )
    return GatewayError(502, "Claude Code did not complete the request.", "claude_execution_failed")


class ClaudeCodeClient:
    def __init__(self, settings: Settings, store: Store, *, slots: asyncio.Semaphore):
        self.settings, self.store, self._slots = settings, store, slots
        self._native_slots = asyncio.Semaphore(settings.claude_code_max_concurrency)
        self._availability: tuple[float, dict] | None = None
        self._availability_lock = asyncio.Lock()

    def base_args(self) -> list[str]:
        return [self.settings.claude_binary, "--safe-mode", "--setting-sources", ""]

    async def inspect(self, *args: str) -> tuple[int, bytes]:
        process = None
        try:
            async with asyncio.timeout(10):
                process = await asyncio.create_subprocess_exec(
                    *self.base_args(),
                    *args,
                    env=worker_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                output, _, code = await asyncio.gather(
                    bounded_pipe(process.stdout), bounded_pipe(process.stderr), process.wait()
                )
                return code, output
        finally:
            if process:
                await stop_process(process)

    async def availability(self, *, fresh: bool = False) -> dict:
        async with self._availability_lock:
            if not fresh and self._availability and time.monotonic() - self._availability[0] < 15:
                return dict(self._availability[1])
            result = {"configured": self.settings.claude_code_enabled, "ready": False}
            if not self.settings.claude_code_enabled:
                return {**result, "reason": "disabled"}
            if os.name != "posix":
                return {**result, "reason": "claude_platform_unsupported"}
            if not shutil.which(self.settings.claude_binary):
                return {**result, "reason": "claude_not_installed"}
            try:
                code, output = await self.inspect("--version")
                match = re.match(rb"(\d+)\.(\d+)\.(\d+) \(Claude Code\)", output)
                version = tuple(int(part) for part in match.groups()) if match else None
                if code or version is None or version < (2, 1, 284):
                    result["reason"] = "claude_version_unsupported"
                else:
                    result["cli_version"] = ".".join(map(str, version))
                    code, output = await self.inspect("auth", "status", "--json")
                    auth = json.loads(output)
                    subscription = auth.get("subscriptionType")
                    eligible = (
                        code == 0
                        and auth.get("loggedIn") is True
                        and auth.get("authMethod") == "claude.ai"
                        and auth.get("apiProvider") == "firstParty"
                        and subscription in {"pro", "max", "team", "enterprise"}
                    )
                    result.update(
                        ready=eligible,
                        reason=None if eligible else "claude_subscription_login_required",
                        subscription_type=subscription if eligible else None,
                    )
            except (OSError, TimeoutError, ValueError, AttributeError, GatewayError):
                result["reason"] = "claude_unavailable"
            self._availability = (time.monotonic(), result)
            return dict(result)

    async def catalog(self) -> list[dict]:
        if not (await self.availability(fresh=True))["ready"]:
            return []
        return [
            {
                "id": slug,
                "object": "model",
                "created": 0,
                "owned_by": "anthropic",
                "name": name,
                "canonical_slug": native,
                "gateway_provider": "claude_code",
                "billing_source": "claude_subscription",
                "access_verified": False,
                "supported_endpoints": ["chat/completions"],
                "streaming": False,
                "history": "one_user_message",
                "prompt_caching": "cli_managed",
                "structured_output_mode": "validated_json",
                "contract_version": CLAUDE_CODE_CONTRACT,
            }
            for slug, (native, name) in CLAUDE_MODELS.items()
        ]

    async def generate(
        self, prepared: ClaudeCodeRequest, *, request_id: str, client_name: str
    ) -> dict:
        native_slot = shared_slot = False
        try:
            async with asyncio.timeout(1):
                await self._native_slots.acquire()
                native_slot = True
                await self._slots.acquire()
                shared_slot = True
        except TimeoutError as error:
            if native_slot:
                self._native_slots.release()
            raise GatewayError(
                429, "The Claude Code or shared pool is busy.", "gateway_busy", retry_after="1"
            ) from error
        except BaseException:
            if native_slot:
                self._native_slots.release()
            raise
        started, first_token = time.monotonic(), None
        outcome, status, code, began = "interrupted", 499, None, False
        process, usage, turns, retries = None, None, None, 0
        stderr_task = None
        metadata = {
            "request_id": request_id,
            "endpoint": "claude-code.chat.completions",
            "requested_model": prepared.model,
            "model": prepared.native_model,
            "provider": "claude_code",
            "billing_source": "claude_subscription",
            "client": client_name,
            "stream": False,
            "reasoning": prepared.effort,
            "format": (prepared.schema or {}).get("type", "text"),
            "cache_policy": "cli_managed",
            "data_policy_override": prepared.data_policy_override,
            "native_contract_version": CLAUDE_CODE_CONTRACT,
            "structured_output_mode": "validated_json"
            if (prepared.schema or {}).get("type") == "json_schema"
            else None,
            "structured_output_version": JSON_INSTRUCTION_VERSION
            if (prepared.schema or {}).get("type") == "json_schema"
            else None,
        }
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                # Fresh credentials check per request; never use a cached signed-in state
                # to continue after the user has switched native billing profiles.
                ready = await self.availability(fresh=True)
                if not ready["ready"]:
                    raise GatewayError(
                        503,
                        "Enable Claude Code and sign in with a Claude subscription.",
                        ready["reason"],
                    )
                metadata["native_cli_version"] = ready.get("cli_version")
                self.store.journal(
                    {
                        **metadata,
                        "event": "started",
                        "body_sha256": hashlib.sha256(
                            json.dumps(prepared.__dict__, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
                began = True
                with tempfile.TemporaryDirectory(prefix="gateway-claude-") as temporary:
                    system = Path(temporary) / "system.txt"
                    system.write_text(prepared.system or "Answer the user's request.")
                    system.chmod(0o600)
                    args = [
                        *self.base_args(),
                        "-p",
                        "--output-format",
                        "stream-json",
                        "--verbose",
                        "--include-partial-messages",
                        "--tools",
                        "",
                        "--strict-mcp-config",
                        "--mcp-config",
                        '{"mcpServers":{}}',
                        "--disable-slash-commands",
                        "--no-session-persistence",
                        "--no-chrome",
                        "--permission-mode",
                        "dontAsk",
                        "--permission-prompts",
                        "none",
                        "--max-turns",
                        "1",
                        "--model",
                        prepared.native_model,
                        "--system-prompt-file",
                        str(system),
                    ]
                    if prepared.effort:
                        args.extend(["--effort", prepared.effort])
                    env = worker_environment()
                    env.update(
                        CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(prepared.max_tokens),
                        MAX_STRUCTURED_OUTPUT_RETRIES="1",
                    )
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        cwd=temporary,
                        env=env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        limit=MAX_CLI_OUTPUT,
                    )
                    stderr_task = asyncio.create_task(bounded_pipe(process.stderr, 65536))

                    async def send_prompt():
                        process.stdin.write(prepared.prompt.encode())
                        await process.stdin.drain()
                        process.stdin.close()
                        await process.stdin.wait_closed()

                    sender = asyncio.create_task(send_prompt())
                    line_task = None
                    try:
                        result, seen_init, seen_assistant, total_bytes = None, False, False, 0
                        while True:
                            line_task = asyncio.create_task(process.stdout.readline())
                            await asyncio.wait(
                                [line_task, stderr_task], return_when=asyncio.FIRST_COMPLETED
                            )
                            if stderr_task.done():
                                stderr_task.result()
                            line = await line_task
                            if not line:
                                break
                            total_bytes += len(line)
                            if total_bytes > MAX_CLI_OUTPUT:
                                raise GatewayError(
                                    502,
                                    "Claude Code output exceeded the worker limit.",
                                    "claude_output_too_large",
                                )
                            event = json.loads(line)
                            if not isinstance(event, dict) or result is not None:
                                raise GatewayError(
                                    502,
                                    "Claude Code returned an invalid event sequence.",
                                    "invalid_claude_output",
                                )
                            kind = event.get("type")
                            if kind == "system" and event.get("subtype") == "init":
                                if seen_init or event.get("model") != prepared.native_model:
                                    raise GatewayError(
                                        502,
                                        "Claude Code selected a different model.",
                                        "model_mismatch",
                                    )
                                if event.get("tools") != [] or event.get("mcp_servers") != []:
                                    raise GatewayError(
                                        502,
                                        "The native session enabled unexpected tools.",
                                        "unexpected_tool_call",
                                    )
                                if event.get("apiKeySource") != "none":
                                    raise GatewayError(
                                        502,
                                        "Claude Code selected non-subscription credentials.",
                                        "billing_route_mismatch",
                                    )
                                seen_init = True
                            elif kind == "system" and event.get("subtype") == "api_retry":
                                retries += 1
                                # Stop on the retry notification, rather than accepting a
                                # silently retried benchmark or switching to the API route.
                                raise native_error(event)
                            elif kind == "assistant":
                                message = event.get("message", {})
                                if not seen_init or message.get("model") != prepared.native_model:
                                    raise GatewayError(
                                        502,
                                        "Claude Code served a different model.",
                                        "model_mismatch",
                                    )
                                blocks = message.get("content", [])
                                if not isinstance(blocks, list) or any(
                                    b.get("type") not in {"text", "thinking", "redacted_thinking"}
                                    for b in blocks
                                ):
                                    raise GatewayError(
                                        502,
                                        "Claude Code attempted an unexpected tool.",
                                        "unexpected_tool_call",
                                    )
                                seen_assistant = True
                            elif kind == "stream_event":
                                raw = event.get("event", {})
                                if (
                                    raw.get("type") == "content_block_delta"
                                    and raw.get("delta", {}).get("type") == "text_delta"
                                    and first_token is None
                                ):
                                    first_token = round((time.monotonic() - started) * 1000)
                            elif kind == "result":
                                result = event
                        await sender
                        returncode = await process.wait()
                        await stderr_task
                    finally:
                        sender.cancel()
                        if line_task:
                            line_task.cancel()
                        await asyncio.gather(
                            sender, *([line_task] if line_task else []), return_exceptions=True
                        )
                    if result:
                        raw_usage = result.get("usage")
                        usage = claude_usage(raw_usage) if isinstance(raw_usage, dict) else None
                        turns = result.get("num_turns")
                    if not result:
                        raise GatewayError(
                            502,
                            "Claude Code ended before a final result.",
                            "incomplete_claude_output",
                        )
                    if (
                        returncode
                        or result.get("is_error") is not False
                        or result.get("subtype") != "success"
                    ):
                        raise native_error(result)
                    if not seen_init or not seen_assistant or type(turns) is not int or turns != 1:
                        raise GatewayError(
                            502,
                            "Claude Code did not complete exactly one answer turn.",
                            "invalid_claude_output",
                        )
                    models = result.get("modelUsage")
                    if not isinstance(models, dict) or set(models) != {prepared.native_model}:
                        raise GatewayError(
                            502,
                            "Claude Code did not verify the exact served model.",
                            "model_mismatch",
                        )
                    model_usage = models[prepared.native_model]
                    if (
                        not isinstance(model_usage, dict)
                        or model_usage.get("provider") != "firstParty"
                        or model_usage.get("canonicalModel") != prepared.native_model
                    ):
                        raise GatewayError(
                            502,
                            "Claude Code returned unexpected model provenance.",
                            "model_mismatch",
                        )
                    if result.get("stop_reason") not in {"end_turn", "refusal"}:
                        raise GatewayError(
                            502, "Claude Code did not finish the answer.", "response_incomplete"
                        )
                    text = result.get("result")
                    if not isinstance(text, str) or not text.strip():
                        raise GatewayError(
                            502, "Claude Code returned no answer text.", "invalid_claude_output"
                        )
                    refused = result["stop_reason"] == "refusal"
                    normalized = {
                        "model": prepared.model,
                        "gateway_provider": "claude_code",
                        "created_at": int(time.time()),
                        "usage": usage,
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "refusal", "refusal": text}
                                    if refused
                                    else {"type": "output_text", "text": text}
                                ],
                            }
                        ],
                    }
                    try:
                        validate_output(normalized, prepared.schema)
                    except GatewayError as error:
                        if error.code == "schema_validation_failed":
                            error.status = 422
                            error.body["error"]["type"] = "model_output_error"
                            error.body.update(
                                provider="claude_code",
                                model=prepared.model,
                                usage=usage_counts(usage),
                            )
                        raise
                    response = completion(normalized, request_id, prepared.model)
                    response["gateway_usage"] = {
                        "provider": "claude_code",
                        "billing_source": "claude_subscription",
                        "contract_version": CLAUDE_CODE_CONTRACT,
                        "cli_version": ready.get("cli_version"),
                        "num_turns": turns,
                        "api_retry_events": retries,
                        "served_models": list(models),
                        "cache_policy": "cli_managed",
                        "cash_cost_verified": False,
                        "subscription_usage_percent": None,
                    }
                    outcome, status = "completed", 200
                    if refused:
                        outcome, code = "refused", "model_refusal"
                    return response
        except TimeoutError as error:
            outcome, status, code = "failed", 504, "claude_timeout"
            raise GatewayError(status, "Claude Code exceeded the gateway timeout.", code) from error
        except GatewayError as error:
            outcome, status, code = "failed", error.status, error.code
            raise
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError, KeyError) as error:
            outcome, status, code = "failed", 502, "invalid_claude_output"
            raise GatewayError(status, "Claude Code returned an invalid response.", code) from error
        finally:
            if process:
                await stop_process(process)
            if stderr_task:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if shared_slot:
                self._slots.release()
            if native_slot:
                self._native_slots.release()
            if began:
                self.store.journal(
                    {
                        **metadata,
                        "event": outcome,
                        "status": status,
                        "code": code,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "first_token_ms": first_token,
                        "usage": usage,
                        "native_turns": turns,
                        "native_retry_events": retries,
                    }
                )
