"""One explicit default-Codex image request, independent of any client application."""

import argparse
import base64
import json
import os
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, action="append", default=[])
    parser.add_argument("--base-url", default="http://127.0.0.1:8317/api/v1")
    args = parser.parse_args()
    key = os.environ.get("CHATGPT_GATEWAY_KEY")
    if not key:
        parser.error("Set CHATGPT_GATEWAY_KEY to your local gateway key.")
    if args.output.exists():
        parser.error("Choose a new output path; the probe does not overwrite files.")
    if len(args.reference) > 5:
        parser.error("At most five reference images are supported.")
    started = time.monotonic()
    with httpx.Client(
        base_url=args.base_url.rstrip("/") + "/",
        timeout=660,
        trust_env=False,
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        if args.reference:
            files = [("image[]", (path.name, path.read_bytes())) for path in args.reference]
            response = client.post(
                "images/edits", data={"model": "codex/imagegen", "prompt": args.prompt}, files=files
            )
        else:
            response = client.post(
                "images/generations", json={"model": "codex/imagegen", "prompt": args.prompt}
            )
        if response.status_code != 200:
            print(json.dumps({"status": response.status_code, **response.json()}, indent=2))
            raise SystemExit(1)
        result = response.json()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as target:
        target.write(base64.b64decode(result["data"][0]["b64_json"], validate=True))
    print(
        json.dumps(
            {
                "model": result["model"],
                "image_model": result["image_model"],
                "image_model_verified": result["image_model_verified"],
                "seconds": round(time.monotonic() - started, 2),
                "output": str(args.output.resolve()),
                "gateway_usage": result.get("gateway_usage"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
