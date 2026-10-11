"""Make one explicit text/JSON probe; no retries or application-specific prompts."""

import argparse
import json
import os
import sys

from openai import OpenAI, OpenAIError
from pydantic import BaseModel


class Answer(BaseModel):
    answer: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact ID from chatgpt-gateway models")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--prompt", default="Reply with a brief greeting.")
    parser.add_argument(
        "--reasoning-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"]
    )
    args = parser.parse_args()
    key = os.environ.get("CHATGPT_GATEWAY_KEY")
    if not key:
        parser.error("Set CHATGPT_GATEWAY_KEY to the local gateway key first.")
    settings = {"reasoning_effort": args.reasoning_effort} if args.reasoning_effort else {}
    try:
        with OpenAI(
            base_url=os.environ.get("CHATGPT_GATEWAY_BASE_URL", "http://127.0.0.1:8317/v1"),
            api_key=key,
            max_retries=0,
            timeout=600,
            default_headers={"X-Title": "gateway-contract-probe"},
        ) as client:
            messages = [{"role": "user", "content": args.prompt}]
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
