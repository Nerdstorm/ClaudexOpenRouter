import json
from dataclasses import dataclass
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from .errors import GatewayError, invalid

CHAT_FIELDS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "response_format",
    "reasoning_effort",
    "reasoning",
    "provider",
    "n",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
}
RESPONSE_FIELDS = {
    "model",
    "input",
    "instructions",
    "stream",
    "store",
    "reasoning",
    "text",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "include",
}


def model_slug(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise invalid("A model from /v1/models is required.", "model")
    slug = value.removeprefix("openai/")
    if "/" in slug or not slug.startswith(("gpt-", "o1", "o3", "o4")):
        raise invalid("Only OpenAI model ids from the connected account are supported.", "model")
    return slug


def unknown_fields(body: dict, allowed: set[str]) -> None:
    unknown = next((key for key in body if key not in allowed and body[key] is not None), None)
    if unknown:
        raise invalid(f"{unknown} is not supported by this gateway route.", unknown)


def local_schema_references(value: Any) -> None:
    # Validation must never fetch a URL supplied in a client's schema.
    if isinstance(value, dict):
        for name, child in value.items():
            if name in {"$ref", "$dynamicRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise invalid("Only local JSON schema references are supported.", "response_format")
            local_schema_references(child)
    elif isinstance(value, list):
        for child in value:
            local_schema_references(child)


def provider_preferences(
    preferences: Any,
    *,
    accept_chatgpt_data_controls: bool = False,
    provider_name: str = "openai",
    data_controls: str = "ChatGPT",
) -> bool:
    """Validate pins; return whether the operator explicitly overrides the data policy."""
    if preferences is None:
        return False
    if not isinstance(preferences, dict):
        raise invalid("provider must be an object.", "provider")
    permitted = {"only", "allow_fallbacks", "require_parameters", "data_collection"}
    unknown_fields(preferences, permitted)
    for name in ("allow_fallbacks", "require_parameters"):
        if preferences.get(name) is not None and not isinstance(preferences[name], bool):
            raise invalid(f"provider.{name} must be a boolean.", f"provider.{name}")
    if "only" in preferences and preferences["only"] != [provider_name]:
        raise invalid(f"This model requires the {provider_name} route.", "provider.only")
    if preferences.get("allow_fallbacks") is True:
        raise invalid("Provider fallback is unavailable.", "provider.allow_fallbacks")
    if preferences.get("data_collection") == "deny" and accept_chatgpt_data_controls:
        return True
    if preferences.get("data_collection") not in (None, "allow"):
        raise invalid(
            "The gateway cannot attest an OpenRouter data-collection policy. "
            f"Manage data controls in {data_controls} "
            "or use a provider that fulfils that requirement.",
            "provider.data_collection",
        )
    return False


def chat_tools(body: dict) -> list[dict]:
    """Translate the explicit OpenRouter native-search subset; never substitute engines."""
    tools = [] if body.get("tools") is None else body["tools"]
    if not isinstance(tools, list):
        raise invalid("tools must be an array.", "tools")
    if not tools:
        if body.get("tool_choice") not in (None, "none"):
            raise invalid("No tool is available for that tool_choice.", "tool_choice")
        return []
    if len(tools) != 1 or tools[0] != {
        "type": "openrouter:web_search",
        "parameters": {"engine": "native"},
    }:
        raise invalid(
            "Chat tools support only openrouter:web_search with engine:native. "
            "Use /v1/responses for native tool definitions.",
            "tools",
        )
    if body.get("tool_choice") not in (None, "auto", "none", "required"):
        raise invalid("Unsupported native search tool_choice.", "tool_choice")
    return [{"type": "web_search"}]


def reasoning(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid("reasoning must be an object.", "reasoning")
    unknown_fields(value, {"effort", "summary"})
    if value.get("effort") not in (None, "none", "minimal", "low", "medium", "high", "xhigh"):
        raise invalid(
            "Unknown reasoning effort; availability also depends on the model.", "reasoning.effort"
        )
    if value.get("summary") not in (None, "auto", "concise", "detailed"):
        raise invalid("Unknown reasoning summary setting.", "reasoning.summary")
    return value


def format_schema(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid("response_format must be an object.", "response_format")
    kind = value.get("type")
    if kind in ("text", "json_object"):
        unknown_fields(value, {"type"})
        return {"type": kind}
    if kind != "json_schema" or not isinstance(value.get("json_schema"), dict):
        raise invalid("Use text, json_object or json_schema response format.", "response_format")
    unknown_fields(value, {"type", "json_schema"})
    spec = value["json_schema"]
    unknown_fields(spec, {"name", "description", "schema", "strict"})
    if not isinstance(spec.get("name"), str) or not spec["name"]:
        raise invalid("A JSON schema name is required.", "response_format.json_schema.name")
    if not isinstance(spec.get("schema"), dict):
        raise invalid("A JSON schema object is required.", "response_format.json_schema.schema")
    local_schema_references(spec["schema"])
    try:
        Draft202012Validator.check_schema(spec["schema"])
    except SchemaError as error:
        raise invalid(
            "The response schema is invalid.", "response_format.json_schema.schema"
        ) from error
    return {"type": "json_schema", **spec}


def content_parts(value: Any, *, assistant: bool = False) -> list[dict]:
    if isinstance(value, str):
        return [{"type": "input_text", "text": value}]
    if not isinstance(value, list):
        raise invalid("Message content must be text or an array of content parts.", "messages")
    parts = []
    for part in value:
        if not isinstance(part, dict):
            raise invalid("Invalid message content part.", "messages")
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            unknown_fields(part, {"type", "text"})
            parts.append({"type": "input_text", "text": part["text"]})
        elif part.get("type") == "image_url" and not assistant:
            unknown_fields(part, {"type", "image_url"})
            image = part.get("image_url")
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise invalid("An image_url object with a URL is required.", "messages")
            unknown_fields(image, {"url", "detail"})
            if not image["url"].startswith(("https://", "data:image/")):
                raise invalid("Image inputs must be HTTPS URLs or image data URLs.", "messages")
            detail = image.get("detail", "auto")
            if detail not in {"auto", "low", "high", "original"}:
                raise invalid("Unknown image detail.", "messages")
            parts.append({"type": "input_image", "image_url": image["url"], "detail": detail})
        else:
            raise invalid("This content part is unsupported; use text or image_url.", "messages")
    return parts


@dataclass
class Prepared:
    body: dict[str, Any]
    stream: bool
    include_usage: bool = False
    schema: dict | None = None
    data_policy_override: bool = False
    provider: str = "chatgpt_plan"
    structured_output_mode: str | None = None
    structured_output_version: str | None = None


def chat_request(body: dict[str, Any], *, accept_chatgpt_data_controls: bool = False) -> Prepared:
    unknown_fields(body, CHAT_FIELDS)
    overridden = provider_preferences(
        body.get("provider"), accept_chatgpt_data_controls=accept_chatgpt_data_controls
    )
    if body.get("parallel_tool_calls") is not None:
        raise invalid(
            "Chat Completions function tools are unavailable; omit parallel_tool_calls.",
            "parallel_tool_calls",
        )
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise invalid("Only one completion per request is supported.", "n")
    if not isinstance(body.get("stream", False), bool):
        raise invalid("stream must be a boolean.", "stream")
    options = body.get("stream_options") or {}
    if not isinstance(options, dict):
        raise invalid("stream_options must be an object.", "stream_options")
    unknown_fields(options, {"include_usage"})
    if "include_usage" in options and not isinstance(options["include_usage"], bool):
        raise invalid("include_usage must be a boolean.", "stream_options.include_usage")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid("A nonempty messages array is required.", "messages")
    tools = chat_tools(body)
    inputs = []
    for message in messages:
        if not isinstance(message, dict):
            raise invalid("Each message must be an object.", "messages")
        unknown_fields(message, {"role", "content", "refusal"})
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise invalid(
                "Use system, developer, user or assistant text messages.", "messages.role"
            )
        if message.get("refusal"):
            raise invalid(
                "Refusal history cannot be translated into a normal text message.",
                "messages.refusal",
            )
        inputs.append(
            {
                "role": "developer" if role == "system" else role,
                "content": content_parts(message.get("content"), assistant=role == "assistant"),
            }
        )
    if body.get("reasoning_effort") is not None and body.get("reasoning") is not None:
        raise invalid("Use reasoning_effort or reasoning, not both.", "reasoning")
    thought = reasoning(
        {"effort": body["reasoning_effort"]}
        if body.get("reasoning_effort") is not None
        else body.get("reasoning")
    )
    output_format = format_schema(body.get("response_format"))
    upstream = {
        "model": model_slug(body.get("model")),
        "input": inputs,
        "store": False,
        "stream": True,
    }
    if thought:
        upstream["reasoning"] = thought
    if output_format:
        upstream["text"] = {"format": output_format}
    if tools:
        upstream["tools"] = tools
        if body.get("tool_choice") is not None:
            upstream["tool_choice"] = body["tool_choice"]
    return Prepared(
        upstream,
        body.get("stream", False),
        bool(options.get("include_usage")),
        output_format,
        data_policy_override=overridden,
    )


def responses_request(body: dict[str, Any]) -> Prepared:
    unknown_fields(body, RESPONSE_FIELDS)
    if body.get("store") not in (None, False):
        raise invalid("ChatGPT-plan requests require store:false.", "store")
    if not isinstance(body.get("stream", False), bool):
        raise invalid("stream must be a boolean.", "stream")
    inputs = body.get("input")
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    if not isinstance(inputs, list) or not inputs:
        raise invalid("A nonempty input array or text is required.", "input")
    # Explicit system-message items are not admitted by the direct plan route.
    inputs = [
        {**item, "role": "developer"}
        if isinstance(item, dict) and item.get("role") == "system"
        else item
        for item in inputs
    ]
    tools = body.get("tools") or []
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or tool.get("type") not in {"namespace", "web_search"}
        for tool in tools
    ):
        raise invalid(
            "Use supported namespace or web_search tools; availability depends on the account.",
            "tools",
        )
    for tool in tools:
        if tool.get("type") == "namespace":
            members = tool.get("tools")
            if (
                not isinstance(tool.get("name"), str)
                or not isinstance(members, list)
                or any(
                    not isinstance(item, dict) or item.get("type") not in {"function", "custom"}
                    for item in members
                )
            ):
                raise invalid("Namespaces require a name and function/custom tools only.", "tools")
    thought = reasoning(body.get("reasoning"))
    output_format = None
    if body.get("text") is not None:
        text = body["text"]
        if not isinstance(text, dict):
            raise invalid("text must be an object.", "text")
        unknown_fields(text, {"format", "verbosity"})
        raw_format = text.get("format")
        if raw_format:
            if not isinstance(raw_format, dict):
                raise invalid("The text format must be an object.", "text.format")
            output_format = format_schema(
                {
                    "type": "json_schema",
                    "json_schema": {k: v for k, v in raw_format.items() if k != "type"},
                }
                if raw_format.get("type") == "json_schema"
                else raw_format
            )
    upstream = {key: value for key, value in body.items() if value is not None}
    upstream.update(model=model_slug(body.get("model")), input=inputs, store=False, stream=True)
    if thought:
        upstream["reasoning"] = thought
    return Prepared(upstream, body.get("stream", False), schema=output_format)


def usage_counts(usage: dict | None, response: dict | None = None) -> dict | None:
    counts = (
        {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "prompt_tokens_details": usage.get("input_tokens_details"),
            "completion_tokens_details": usage.get("output_tokens_details"),
        }
        if usage is not None
        else None
    )
    if response is not None:
        calls = [
            item for item in response.get("output") or [] if item.get("type") == "web_search_call"
        ]
        if calls:
            counts = counts or {}
            counts["server_tool_use_details"] = {
                "web_search_requests": sum(item.get("status") == "completed" for item in calls)
            }
    return counts


def assistant_message(response: dict) -> dict:
    texts, refusals, annotations = [], [], []
    for item in response.get("output") or []:
        if item.get("type") == "function_call":
            raise GatewayError(
                502,
                "Unexpected tool call on the text-only Chat Completions route.",
                "unexpected_tool_call",
            )
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                texts.append(content.get("text", ""))
                for annotation in content.get("annotations") or []:
                    if annotation.get("type") == "url_citation":
                        annotations.append(
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    k: v for k, v in annotation.items() if k != "type"
                                },
                            }
                        )
                    else:
                        annotations.append(annotation)
            elif content.get("type") == "refusal":
                refusals.append(content.get("refusal", ""))
    message = {"role": "assistant", "content": "".join(texts) if texts else None}
    if refusals:
        message["refusal"] = "".join(refusals)
    if annotations:
        message["annotations"] = annotations
    return message


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _schema_failure(details: list[dict]) -> GatewayError:
    # Returned to the authenticated caller, never journaled. No instance values,
    # complete schema, model reply or jsonschema exception message is included.
    return GatewayError(
        502,
        "The model's completed answer did not match the requested JSON format.",
        "schema_validation_failed",
        body={
            "error": {
                "message": "The model's completed answer did not match the requested JSON format.",
                "type": "gateway_error",
                "code": "schema_validation_failed",
                "param": "response_format",
                "validation_errors": details,
            }
        },
    )


def validate_output(response: dict, schema: dict | None) -> None:
    if not schema or schema["type"] == "text":
        return
    message = assistant_message(response)
    if message.get("refusal"):
        return
    try:
        value = json.loads(message.get("content") or "", parse_constant=_reject_json_constant)
    except ValueError as error:
        detail = {"path": [], "validator": "json_syntax"}
        if isinstance(error, json.JSONDecodeError):
            detail.update(line=error.lineno, column=error.colno)
        raise _schema_failure([detail]) from error
    if schema["type"] == "json_schema":
        details = []
        for error in islice(Draft202012Validator(schema["schema"]).iter_errors(value), 8):
            detail = {"path": list(error.absolute_path), "validator": error.validator}
            if error.validator == "type":
                detail["expected_type"] = error.validator_value
            elif error.validator == "required":
                detail["missing_fields"] = [
                    key for key in error.validator_value if key not in error.instance
                ][:8]
            details.append(detail)
        if details:
            raise _schema_failure(details)


def completion(response: dict, request_id: str, requested_model: str) -> dict:
    served_model = response["model"]
    if requested_model.startswith("openai/"):
        served_model = f"openai/{served_model}"
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": response.get("created_at", 0),
        "model": served_model,
        "provider": response.get("gateway_provider", "chatgpt_plan"),
        "choices": [{"index": 0, "message": assistant_message(response), "finish_reason": "stop"}],
        "usage": usage_counts(response.get("usage"), response),
    }
