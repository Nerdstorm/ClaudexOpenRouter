"""The portable OpenRouter Chat subset served by the Anthropic API."""

import json
from copy import deepcopy
from typing import Any

from anthropic import transform_schema

from .config import Settings
from .errors import invalid
from .protocol import CHAT_FIELDS, Prepared, format_schema, provider_preferences, unknown_fields

# OpenRouter ids are public client ids; API ids are explicit, never guessed or upgraded.
CLAUDE_MODELS = {
    "anthropic/claude-sonnet-5.5": ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    "anthropic/claude-opus-5.5": ("claude-opus-5-5", "Claude Opus 5.5"),
}
CLAUDE_FIELDS = CHAT_FIELDS | {"cache_control", "max_tokens", "max_completion_tokens"}
JSON_INSTRUCTION_VERSION = "validated-json.v2"


def cache_control(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid("cache_control must be an object.", "cache_control")
    unknown_fields(value, {"type", "ttl"})
    if value.get("type") != "ephemeral" or value.get("ttl", "5m") not in ("5m", "1h"):
        raise invalid("Use ephemeral cache_control with ttl 5m or 1h.", "cache_control")
    return dict(value)


def text_blocks(value: Any) -> list[dict]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list) or not value:
        raise invalid("Claude messages require text or nonempty text blocks.", "messages.content")
    blocks = []
    for part in value:
        if not isinstance(part, dict):
            raise invalid("Invalid Claude content block.", "messages.content")
        unknown_fields(part, {"type", "text", "cache_control"})
        if part.get("type") != "text" or not isinstance(part.get("text"), str):
            raise invalid("This Claude adapter supports text blocks only.", "messages.content")
        block = {"type": "text", "text": part["text"]}
        marker = cache_control(part.get("cache_control"))
        if marker is not None:
            if not part["text"]:
                raise invalid("Empty text cannot be a cache breakpoint.", "cache_control")
            block["cache_control"] = marker
        blocks.append(block)
    return blocks


def claude_request(body: dict[str, Any], settings: Settings) -> Prepared:
    unknown_fields(body, CLAUDE_FIELDS)
    if not isinstance(body.get("model"), str) or body["model"] not in CLAUDE_MODELS:
        raise invalid("Select an exact Claude model id from /v1/models.", "model")
    overridden = provider_preferences(
        body.get("provider"),
        accept_chatgpt_data_controls=settings.accept_anthropic_data_controls,
        provider_name="anthropic",
        data_controls="Claude Console",
    )
    if body.get("tools") is not None and body["tools"] != []:
        raise invalid("This Claude route is text-only; tools are unavailable.", "tools")
    if body.get("tool_choice") not in (None, "none"):
        raise invalid("No Claude tool is available.", "tool_choice")
    if body.get("parallel_tool_calls") is not None:
        raise invalid("Claude function tools are unavailable.", "parallel_tool_calls")
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise invalid("Only one completion per request is supported.", "n")
    if not isinstance(body.get("stream", False), bool):
        raise invalid("stream must be a boolean.", "stream")
    options = body.get("stream_options")
    options = {} if options is None else options
    if not isinstance(options, dict):
        raise invalid("stream_options must be an object.", "stream_options")
    unknown_fields(options, {"include_usage"})
    if "include_usage" in options and not isinstance(options["include_usage"], bool):
        raise invalid("include_usage must be a boolean.", "stream_options.include_usage")
    if body.get("max_tokens") is not None and body.get("max_completion_tokens") is not None:
        raise invalid("Use max_tokens or max_completion_tokens, not both.", "max_tokens")
    ceiling = body.get("max_tokens")
    if ceiling is None:
        ceiling = body.get("max_completion_tokens")
    if ceiling is None:
        ceiling = settings.anthropic_max_tokens
    if type(ceiling) is not int or not 1 <= ceiling <= 128000:
        raise invalid(
            "The Claude output ceiling must be an integer from 1 to 128000.", "max_tokens"
        )
    if body.get("reasoning_effort") is not None and body.get("reasoning") is not None:
        raise invalid("Use reasoning_effort or reasoning, not both.", "reasoning")
    thought = body.get("reasoning")
    if thought is not None:
        if not isinstance(thought, dict):
            raise invalid("reasoning must be an object.", "reasoning")
        unknown_fields(thought, {"effort"})
    effort = body.get("reasoning_effort")
    if effort is None:
        effort = (thought or {}).get("effort")
    if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise invalid("Claude effort must be low, medium, high, xhigh or max.", "reasoning_effort")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise invalid("A nonempty messages array is required.", "messages")
    system, messages = [], []
    for message in raw_messages:
        if not isinstance(message, dict):
            raise invalid("Each message must be an object.", "messages")
        unknown_fields(message, {"role", "content", "refusal"})
        if message.get("refusal"):
            raise invalid(
                "Refusal history cannot be converted to ordinary text.", "messages.refusal"
            )
        role = message.get("role")
        blocks = text_blocks(message.get("content"))
        if role in ("system", "developer"):
            if messages:
                raise invalid(
                    "Claude system/developer messages must precede the conversation.",
                    "messages.role",
                )
            system.extend(blocks)
        elif role in ("user", "assistant"):
            messages.append({"role": role, "content": blocks})
        else:
            raise invalid("Unsupported Claude message role.", "messages.role")
    if not messages or messages[0]["role"] != "user" or messages[-1]["role"] != "user":
        raise invalid("Claude conversations must start and end with a user message.", "messages")

    blocks = [*system, *(b for m in messages for b in m["content"])]
    markers = [b["cache_control"] for b in blocks if "cache_control" in b]
    automatic = cache_control(body.get("cache_control"))
    # Never move or infer a boundary inside application text. Cache only leading system
    # blocks when the caller provided no cache controls, and allow per-request null opt-out.
    if (
        "cache_control" not in body
        and not markers
        and system
        and settings.anthropic_system_cache_ttl != "off"
    ):
        if system[-1]["text"]:
            marker = {"type": "ephemeral", "ttl": settings.anthropic_system_cache_ttl}
            system[-1]["cache_control"] = marker
            markers.append(marker)
    if len(markers) + bool(automatic) > 4:
        raise invalid(
            "Claude supports at most four cache breakpoints including automatic caching.",
            "cache_control",
        )
    seen_short = False
    for marker in [*markers, *([automatic] if automatic else [])]:
        if marker.get("ttl", "5m") == "5m":
            seen_short = True
        elif seen_short:
            raise invalid(
                "One-hour cache breakpoints must precede five-minute breakpoints.", "cache_control"
            )

    output_format = format_schema(body.get("response_format"))
    if output_format and output_format["type"] == "json_object":
        raise invalid("Claude JSON output requires an explicit json_schema.", "response_format")
    upstream: dict[str, Any] = {
        "model": CLAUDE_MODELS[body["model"]][0],
        "messages": messages,
        "max_tokens": ceiling,
    }
    if system:
        upstream["system"] = system
    if automatic:
        upstream["cache_control"] = automatic
    output_config = {}
    structured_mode = None
    if output_format and output_format["type"] == "json_schema":
        structured_mode = settings.anthropic_structured_output
        # Use the vendor's supported schema transformation. Always validate against the
        # untouched original at completion, including constraints the API cannot enforce.
        if structured_mode == "native":
            output_config["format"] = {
                "type": "json_schema",
                "schema": transform_schema(deepcopy(output_format["schema"])),
            }
        else:
            # Explicit operator mode for schemas too large for Anthropic's grammar.
            # The format is a protocol instruction, above the user's task. Append it
            # after caller system blocks, without moving any caller/cache boundary.
            system.append(
                {
                    "type": "text",
                    "text": (
                        "You are a JSON API. Your entire final answer must be one JSON value "
                        "that conforms to the following JSON Schema. "
                        "Do not include markdown fences, a preamble or trailing commentary. "
                        "Encode strings as JSON strings, escaping embedded quotes and newlines. "
                        "Follow the order of properties in each schema object when writing "
                        "the JSON. Include every required property and respect all constraints.\n"
                        + json.dumps(
                            output_format["schema"], ensure_ascii=False, separators=(",", ":")
                        )
                    ),
                }
            )
            upstream["system"] = system
    if effort:
        output_config["effort"] = effort
        upstream["thinking"] = {"type": "adaptive"}
    if output_config:
        upstream["output_config"] = output_config
    return Prepared(
        upstream,
        body.get("stream", False),
        bool(options.get("include_usage")),
        output_format,
        data_policy_override=overridden,
        provider="anthropic",
        structured_output_mode=structured_mode,
        structured_output_version=JSON_INSTRUCTION_VERSION
        if structured_mode == "validated_json"
        else None,
    )


def claude_usage(
    value: dict | None, rates: dict | None = None, model: str | None = None
) -> dict | None:
    """Anthropic usage in the gateway's OpenRouter-shaped form, priced when rates are configured.

    The cost is the configured rates applied to the exact counts Anthropic returned, the same
    arithmetic Anthropic bills by; it is labelled with the rates' date and is never invented
    when a count or the model's rate is missing."""
    if value is None:
        return None
    fresh, read, write = (
        value.get(k)
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )
    total_input = (
        sum((fresh, read, write))
        if all(type(n) is int and n >= 0 for n in (fresh, read, write))
        else None
    )
    output = value.get("output_tokens")
    total = total_input + output if total_input is not None and type(output) is int else None
    details = {"cached_tokens": read, "cache_write_tokens": write, "uncached_tokens": fresh}
    if value.get("cache_creation") is not None:
        details["cache_write_tokens_details"] = value["cache_creation"]
    usage = {
        "input_tokens": total_input,
        "output_tokens": output,
        "total_tokens": total,
        "input_tokens_details": details,
        "anthropic": value,
    }
    rate = ((rates or {}).get("models") or {}).get(model or "")
    if rate and total is not None:
        lifetimes = value.get("cache_creation") or {}
        short, long = (
            lifetimes.get(k) for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
        if not (type(short) is int and type(long) is int and short + long == write):
            short, long = write, 0  # the five-minute lifetime is Anthropic's default
        cost = (
            fresh * rate["input"]
            + short * rate["cache_write_5m"]
            + long * rate["cache_write_1h"]
            + read * rate["cache_read"]
            + output * rate["output"]
        ) / 1_000_000
        usage["cost"] = round(cost, 8)
        usage["cost_details"] = {"source": "anthropic_rates", "rates_as_of": rates["as_of"]}
    return usage


def cache_policy(body: dict) -> dict:
    blocks = [*body.get("system", []), *(b for m in body["messages"] for b in m["content"])]
    return {
        "automatic_ttl": body.get("cache_control", {}).get("ttl", "5m")
        if body.get("cache_control")
        else None,
        "block_ttls": [b["cache_control"].get("ttl", "5m") for b in blocks if "cache_control" in b],
    }
