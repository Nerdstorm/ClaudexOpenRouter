"""Make one explicit text/JSON probe; no retries or application-specific prompts."""

import argparse
import json
import os
import sys
from pathlib import Path

from openai import OpenAI, OpenAIError
from pydantic import BaseModel


class Answer(BaseModel):
    answer: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact ID from chatgpt-gateway models")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--prompt", default="Reply with a brief greeting.")
    parser.add_argument("--system-file", type=Path, help="Optional reusable system context file")
    parser.add_argument(
        "--cache-ttl", choices=["5m", "1h"], help="Portable explicit system cache marker"
    )
    parser.add_argument("--max-tokens", type=int, help="Explicit Claude output ceiling")
    parser.add_argument(
        "--reasoning-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    )
    args = parser.parse_args()
    if args.cache_ttl and not args.system_file:
        parser.error("--cache-ttl requires --system-file to mark an explicit stable prefix")
    if args.max_tokens is not None and args.max_tokens < 1:
        parser.error("--max-tokens must be positive")
    key = os.environ.get("CHATGPT_GATEWAY_KEY")
    if not key:
        parser.error("Set CHATGPT_GATEWAY_KEY to the local gateway key first.")
    settings = {"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}
    if args.max_tokens is not None:
        settings["max_tokens"] = args.max_tokens
    try:
        with OpenAI(
            base_url=os.environ.get("CHATGPT_GATEWAY_BASE_URL", "http://127.0.0.1:8317/v1"),
            api_key=key,
            max_retries=0,
            timeout=600,
            default_headers={"X-Title": "gateway-contract-probe"},
        ) as client:
            messages = [{"role": "user", "content": args.prompt}]
            if args.system_file:
                block = {"type": "text", "text": args.system_file.read_text()}
                if args.cache_ttl:
                    block["cache_control"] = {"type": "ephemeral", "ttl": args.cache_ttl}
                messages.insert(0, {"role": "system", "content": [block]})
            if args.format == "json":
                result = client.chat.completions.parse(
                    model=args.model,
                    messages=messages,
                    response_format=Answer,
                    **settings,
                )
                message = result.choices[0].message
                answer = message.parsed.model_dump() if message.parsed else None
            else:
                result = client.chat.completions.create(
                    model=args.model, messages=messages, **settings
                )
                message = result.choices[0].message
                answer = message.content
            print(
                json.dumps(
                    {
                        "requested_model": args.model,
                        "served_model": result.model,
                        "id": result.id,
                        "answer": answer,
                        "refusal": message.refusal,
                        "usage": result.usage.model_dump() if result.usage else None,
                    },
                    indent=2,
                )
            )
            if message.refusal:
                raise SystemExit(2)
    except OpenAIError as error:
        print(f"Probe failed without retrying: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
