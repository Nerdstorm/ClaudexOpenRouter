"""One explicit Claude subscription probe; metadata output only, with no retries."""

import argparse
import json
import os
import time

import httpx
from jsonschema import Draft202012Validator, ValidationError


def large_schema() -> dict:
    """Generic nested schema, independent of any client application."""
    definitions = {}
    for index in range(8):
        definitions[f"Section{index}"] = {
            "type": "object",
            "description": (
                "A configuration section with a stable name, integer weight, readiness flag, "
                "empty comment, a single tag and an explicitly nullable alternative. "
                "All fields are required and additional properties are prohibited."
            ),
            "properties": {
                "label": {"type": "string", "const": "ok", "minLength": 2, "maxLength": 2},
                "weight": {"type": "integer", "const": 1, "minimum": 1, "maximum": 1},
                "ready": {"type": "boolean", "const": True},
                "comment": {"type": "string", "const": "", "maxLength": 0},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["ok"]},
                    "minItems": 1,
                    "maxItems": 1,
                },
                "alternative": {"anyOf": [{"type": "null"}, {"type": "string", "const": "ok"}]},
            },
            "required": ["label", "weight", "ready", "comment", "tags", "alternative"],
            "additionalProperties": False,
        }
    properties = {f"section_{i}": {"$ref": f"#/$defs/Section{i}"} for i in range(8)}
    return {
        "type": "object",
        "$defs": definitions,
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["sonnet", "opus"], required=True)
    parser.add_argument("--format", choices=["text", "json", "large-json"], default="json")
    args = parser.parse_args()
    key = os.environ.get("CHATGPT_GATEWAY_KEY")
    if not key:
        parser.error("Set CHATGPT_GATEWAY_KEY to the local gateway key.")
    model = f"anthropic/claude-{args.model}-5.5"
    schema = None
    if args.format != "text":
        schema = (
            large_schema()
            if args.format == "large-json"
            else {
                "type": "object",
                "properties": {"answer": {"type": "string", "const": "ready"}},
                "required": ["answer"],
                "additionalProperties": False,
            }
        )
    body = {
        "model": model,
        "max_tokens": 4096,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": "Answer briefly and follow the required output format."},
            {
                "role": "user",
                "content": "Reply exactly: ready"
                if schema is None
                else "Fill required schema properties with their constants or allowed values. "
                "Use null for nullable alternatives. Return the required JSON.",
            },
        ],
    }
    if schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "probe",
                "strict": True,
                "schema": schema,
            },
        }
    base = os.environ.get("CHATGPT_GATEWAY_BASE_URL", "http://127.0.0.1:8317/claude-code/v1")
    started = time.monotonic()
    with httpx.Client(timeout=180, trust_env=False, follow_redirects=False) as client:
        response = client.post(
            f"{base.rstrip('/')}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}", "X-Title": "claude-subscription-probe"},
        )
    data = response.json()
    summary = {
        "requested_model": model,
        "format": args.format,
        "http_status": response.status_code,
        "seconds": round(time.monotonic() - started, 3),
        "request_id": response.headers.get("x-gateway-request-id"),
    }
    if response.status_code != 200:
        summary["error_code"] = data.get("error", {}).get("code")
        print(json.dumps(summary, indent=2))
        raise SystemExit(1)
    text = data["choices"][0]["message"].get("content")
    try:
        if schema:
            Draft202012Validator(schema).validate(json.loads(text))
        elif text.strip() != "ready":
            raise ValueError("Unexpected text answer")
    except (ValueError, TypeError, AttributeError, ValidationError):
        summary["answer_verified"] = False
        print(json.dumps(summary, indent=2))
        raise SystemExit(1) from None
    if data["model"] != model or data.get("provider") != "claude_code":
        raise ValueError("The probe reached a different backend or model.")
    summary.update(
        answer_verified=True,
        model=data["model"],
        usage=data.get("usage"),
        gateway_usage=data.get("gateway_usage"),
        schema_bytes=len(json.dumps(schema).encode()) if schema else 0,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
