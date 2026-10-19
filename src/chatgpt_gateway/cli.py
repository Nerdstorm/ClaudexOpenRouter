import argparse
import json
import subprocess
import sys
import webbrowser
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import uvicorn

from .app import create_app
from .claude_code import worker_environment
from .config import Settings
from .storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Local ChatGPT-plan gateway")
    parser.add_argument("--port", type=int, help="Gateway port (default 8317)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Serve on 127.0.0.1")
    login = commands.add_parser("login", help="Connect through Continue with ChatGPT")
    login.add_argument("--new", action="store_true", help="Register another account/workspace")
    login.add_argument("--account", help="Reauthorize a saved account id")
    login.add_argument("--no-browser", action="store_true", help="Print the authorization URL")
    commands.add_parser("status", help="Show the active connection without credentials")
    commands.add_parser("accounts", help="List saved account registrations")
    use = commands.add_parser("use", help="Select a saved account")
    use.add_argument("account_id")
    commands.add_parser("logout", help="Revoke and clear the active renewable session")
    models = commands.add_parser(
        "models", help="List models from configured providers and optional Codex images"
    )
    models.add_argument("--backend", choices=["default", "claude-code"], default="default")
    commands.add_parser("capabilities", help="Show model capabilities and shared/image concurrency")
    commands.add_parser(
        "claude-login", help="Sign native Claude Code in with a Claude subscription"
    )
    commands.add_parser("claude-status", help="Show native Claude subscription sign-in status")
    commands.add_parser("requests", help="Show recent request outcomes and token usage")
    commands.add_parser("key", help="Print the LOCAL gateway key for client configuration")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        settings = replace(settings, port=args.port)
    store = Store(settings.data_dir)
    if args.command in {"claude-login", "claude-status"}:
        native_args = [settings.claude_binary, "--safe-mode", "--setting-sources", ""]
        try:
            if args.command == "claude-login":
                raise SystemExit(
                    subprocess.run(
                        [*native_args, "auth", "login", "--claudeai"], env=worker_environment()
                    ).returncode
                )
            result = subprocess.run(
                [*native_args, "auth", "status", "--json"],
                env=worker_environment(),
                capture_output=True,
                text=True,
                timeout=20,
            )
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                raise ValueError("Invalid native auth status")
            print(
                json.dumps(
                    {
                        k: payload.get(k)
                        for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
                    },
                    indent=2,
                )
            )
            raise SystemExit(result.returncode)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            print(
                "Claude Code is unavailable; install a supported CLI and try again.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    if args.command == "key":
        print(store.api_key)
        return
    if args.command == "serve":
        print(f"ChatGPT Plan Gateway: {settings.base_url}/v1", flush=True)
        # Callback URLs contain OAuth codes; disable access logs for every route.
        uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False)
        return
    headers = {"Authorization": f"Bearer {store.api_key}"}
    try:
        with httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            if args.command == "login":
                response = client.post(
                    "/gateway/login", json={"new": args.new, "account_id": args.account}
                )
            elif args.command == "use":
                response = client.post(
                    "/gateway/accounts/select", json={"account_id": args.account_id}
                )
            elif args.command == "logout":
                response = client.post("/gateway/logout")
            elif args.command == "models":
                response = client.get(
                    "/claude-code/v1/models" if args.backend == "claude-code" else "/v1/models"
                )
            else:
                response = client.get(f"/gateway/{args.command}")
            if response.status_code != 200:
                print(json.dumps(response.json(), indent=2), file=sys.stderr)
                raise SystemExit(1)
            payload = response.json()
            if args.command == "login":
                if args.no_browser:
                    # A manual URL must not expose a retained ID token in terminal output.
                    address = urlsplit(payload["authorization_url"])
                    query = [
                        (key, value)
                        for key, value in parse_qsl(address.query)
                        if key != "id_token_hint"
                    ]
                    print(urlunsplit(address._replace(query=urlencode(query))))
                else:
                    webbrowser.open(payload["authorization_url"])
                    print("Complete Continue with ChatGPT in your browser, then run status.")
            else:
                print(json.dumps(payload, indent=2))
    except httpx.HTTPError:
        print("The gateway is unavailable. Start chatgpt-gateway serve first.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
