import hashlib
import json
import os
import secrets
import tempfile
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self._lock = FileLock(directory / "state.lock")
        with self._lock:
            identity = read_json(directory / "host.json")
            if identity is None:
                identity = {
                    "host_id": f"urn:uuid:{uuid.uuid4()}",
                    "api_key": secrets.token_urlsafe(32),
                }
                atomic_json(directory / "host.json", identity)
        self.host_id: str = identity["host_id"]
        self.api_key: str = identity["api_key"]

    def accounts(self) -> list[dict[str, Any]]:
        return [read_json(path) for path in sorted((self.directory / "accounts").glob("*.json"))]

    def get(self, account_id: str) -> dict[str, Any] | None:
        if len(account_id) != 24 or any(c not in "0123456789abcdef" for c in account_id):
            return None
        return read_json(self.directory / "accounts" / f"{account_id}.json")

    def active(self) -> dict[str, Any] | None:
        account_id = (read_json(self.directory / "active.json", {}) or {}).get("account_id")
        return self.get(account_id) if account_id else None

    def select(self, account_id: str) -> bool:
        with self._lock:
            if self.get(account_id) is None:
                return False
            atomic_json(self.directory / "active.json", {"account_id": account_id})
        return True

    def save(self, account: dict[str, Any], *, active: bool = False) -> dict[str, Any]:
        identity = [account["issuer"], account["client_id"], account["subject"]]
        account = {
            **account,
            "id": hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24],
        }
        with self._lock:
            atomic_json(self.directory / "accounts" / f"{account['id']}.json", account)
            if active:
                atomic_json(self.directory / "active.json", {"account_id": account["id"]})
        return account

    def clear_tokens(self, account_id: str) -> None:
        with self._lock:
            account = self.get(account_id)
            if account:
                for name in ("access_token", "refresh_token", "id_token"):
                    account.pop(name, None)
                account["expires_at"] = 0
                atomic_json(self.directory / "accounts" / f"{account_id}.json", account)

    def journal(self, event: dict[str, Any]) -> None:
        # Callers supply metadata only. No prompt, token or output is accepted here.
        allowed = {
            "request_id",
            "event",
            "endpoint",
            "model",
            "requested_model",
            "client",
            "account",
            "body_sha256",
            "duration_ms",
            "admission_wait_ms",
            "status",
            "code",
            "upstream_request_id",
            "usage",
            "reasoning",
            "format",
            "stream",
            "error_param",
            "data_policy_override",
            "provider",
            "billing_source",
            "cache_policy",
            "structured_output_mode",
            "structured_output_version",
            "first_token_ms",
            "image_model",
            "image_contract_version",
            "native_contract_version",
            "native_cli_version",
            "native_turns",
            "native_retry_events",
        }
        if set(event) - allowed:
            raise ValueError("Unexpected journal field")
        path = self.directory / "requests.jsonl"
        record = {"at": datetime.now(UTC).isoformat(), **event}
        with self._lock:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with (self.directory / "requests.jsonl").open(encoding="utf-8") as stream:
                return [json.loads(line) for line in deque(stream, maxlen=limit)]
        except FileNotFoundError:
            return []


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": account["id"],
        "email": account.get("email"),
        "label": f"{account.get('email') or 'ChatGPT account'} · {account['client_id'][-8:]}",
        "signed_in": bool(account.get("access_token")),
        "plan_enabled": "chatgpt.tokens.use.direct" in account.get("scopes", []),
        "expires_at": account.get("expires_at"),
    }
