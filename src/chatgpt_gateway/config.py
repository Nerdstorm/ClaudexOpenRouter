import os
from dataclasses import dataclass
from pathlib import Path

AUTH_ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{AUTH_ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{AUTH_ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
APP_NAME = "ChatGPT Plan Gateway"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8317
    max_concurrency: int = 2
    timeout_seconds: float = 600
    max_request_bytes: int = 8 * 1024 * 1024
    accept_chatgpt_data_controls: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def callback_uri(self) -> str:
        return f"{self.base_url}/auth/callback"

    @classmethod
    def from_env(cls) -> "Settings":
        data_controls = os.environ.get("CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS", "0")
        if data_controls not in {"0", "1"}:
            raise ValueError("CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS must be 0 or 1")
        settings = cls(
            data_dir=Path(
                os.environ.get("CHATGPT_GATEWAY_DATA_DIR", "~/.local/share/chatgpt-plan-gateway")
            )
            .expanduser()
            .resolve(),
            port=int(os.environ.get("CHATGPT_GATEWAY_PORT", "8317")),
            max_concurrency=int(os.environ.get("CHATGPT_GATEWAY_MAX_CONCURRENCY", "2")),
            timeout_seconds=float(os.environ.get("CHATGPT_GATEWAY_TIMEOUT_SECONDS", "600")),
            max_request_bytes=int(os.environ.get("CHATGPT_GATEWAY_MAX_REQUEST_BYTES", "8388608")),
            accept_chatgpt_data_controls=data_controls == "1",
        )
        if not 1 <= settings.port <= 65535:
            raise ValueError("CHATGPT_GATEWAY_PORT must be between 1 and 65535")
        if settings.max_concurrency < 1 or settings.timeout_seconds <= 0:
            raise ValueError("Concurrency and timeout must be positive")
        if settings.max_request_bytes < 1024:
            raise ValueError("Maximum request size must be at least 1024 bytes")
        return settings
