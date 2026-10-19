"""Explicit text/JSON subset for the native Claude Code subscription route."""

from dataclasses import dataclass, replace

from .anthropic_protocol import claude_request
from .config import Settings
from .errors import invalid

CLAUDE_CODE_CONTRACT = "claude-code.v1"


@dataclass(frozen=True)
class ClaudeCodeRequest:
    model: str
    native_model: str
    system: str
    prompt: str
    max_tokens: int
    effort: str | None
    schema: dict | None
    data_policy_override: bool


def subscription_request(body: dict, settings: Settings) -> ClaudeCodeRequest:
    # Reuse common Chat validation, not the API transport or its credentials. The
    # explicit CLI contract uses one validated JSON answer without native re-prompts.
    if "cache_control" in body:
        raise invalid(
            "Claude Code manages caching automatically; use the API route for cache controls.",
            "cache_control",
        )
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(part, dict) and "cache_control" in part for part in content
            ):
                raise invalid(
                    "Per-block cache markers require the Anthropic API route.", "cache_control"
                )
    prepared = claude_request(
        body,
        replace(
            settings,
            anthropic_system_cache_ttl="off",
            anthropic_structured_output="validated_json",
            anthropic_max_tokens=settings.claude_code_max_tokens,
        ),
        data_controls="Claude account",
    )
    if prepared.stream:
        raise invalid("The Claude Code route supports completed, nonstreaming answers.", "stream")
    if body.get("stream_options") is not None:
        raise invalid(
            "stream_options requires streaming, unavailable on this route.", "stream_options"
        )
    conversation = prepared.body["messages"]
    if len(conversation) != 1 or conversation[0]["role"] != "user":
        raise invalid(
            "The Claude Code route accepts leading system/developer context and one user "
            "message. Use the API route for native user/assistant history.",
            "messages",
        )
    # Concatenating text blocks preserves their order. Explicit cache boundaries
    # were refused above; this does not claim API block or history parity.
    return ClaudeCodeRequest(
        model=body["model"],
        native_model=prepared.body["model"],
        system="\n\n".join(block["text"] for block in prepared.body.get("system", [])),
        prompt="\n\n".join(block["text"] for block in conversation[0]["content"]),
        max_tokens=prepared.body["max_tokens"],
        effort=prepared.body.get("output_config", {}).get("effort"),
        schema=prepared.schema,
        data_policy_override=prepared.data_policy_override,
    )
