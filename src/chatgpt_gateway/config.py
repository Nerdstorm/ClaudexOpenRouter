import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

AUTH_ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{AUTH_ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{AUTH_ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
APP_NAME = "ChatGPT Plan Gateway"
# One Claude model's rates, USD per million tokens, as a rates file gives them.
RATE_KEYS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")


def anthropic_rates(value: object) -> dict:
    """A rates file's content, checked: `as_of` and one complete rate per native model id."""
    if not isinstance(value, dict) or not isinstance(value.get("as_of"), str):
        raise ValueError("The Anthropic rates file needs an as_of date and a models object")
    models = value.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("The Anthropic rates file needs a models object")
    for model, rate in models.items():
        if not isinstance(rate, dict) or any(
            type(rate.get(key)) not in (int, float) or rate[key] < 0 for key in RATE_KEYS
        ):
            raise ValueError(
                f"Anthropic rates for {model!r} need nonnegative {', '.join(RATE_KEYS)}"
            )
    return {
        "as_of": value["as_of"],
        "models": {m: {k: float(r[k]) for k in RATE_KEYS} for m, r in models.items()},
    }


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8317
    max_concurrency: int = 12
    admission_timeout_seconds: float = 120
    max_queued_requests: int = 48
    timeout_seconds: float = 600
    max_request_bytes: int = 8 * 1024 * 1024
    accept_chatgpt_data_controls: bool = False
    anthropic_api_key: str | None = field(default=None, repr=False)
    accept_anthropic_data_controls: bool = False
    anthropic_max_tokens: int = 32768
    anthropic_system_cache_ttl: str = "5m"
    anthropic_structured_output: str = "native"
    codex_images_enabled: bool = False
    codex_binary: str = "codex"
    image_max_concurrency: int = 1
    claude_code_enabled: bool = False
    claude_binary: str = "claude"
    claude_code_max_concurrency: int = 1
    claude_code_max_tokens: int = 32768
    # Prices API calls only when the operator supplies rates; none are built in.
    anthropic_rates: dict | None = None

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
        anthropic_controls = os.environ.get("CHATGPT_GATEWAY_ACCEPT_ANTHROPIC_DATA_CONTROLS", "0")
        if anthropic_controls not in {"0", "1"}:
            raise ValueError("CHATGPT_GATEWAY_ACCEPT_ANTHROPIC_DATA_CONTROLS must be 0 or 1")
        codex_images = os.environ.get("CHATGPT_GATEWAY_CODEX_IMAGES", "0")
        if codex_images not in {"0", "1"}:
            raise ValueError("CHATGPT_GATEWAY_CODEX_IMAGES must be 0 or 1")
        claude_code = os.environ.get("CHATGPT_GATEWAY_CLAUDE_CODE", "0")
        if claude_code not in {"0", "1"}:
            raise ValueError("CHATGPT_GATEWAY_CLAUDE_CODE must be 0 or 1")
        data_dir = (
            Path(os.environ.get("CHATGPT_GATEWAY_DATA_DIR", "~/.local/share/chatgpt-plan-gateway"))
            .expanduser()
            .resolve()
        )
        key_file = Path(
            os.environ.get(
                "CHATGPT_GATEWAY_ANTHROPIC_API_KEY_FILE", str(data_dir / "anthropic-api-key")
            )
        ).expanduser()
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key and key_file.exists():
            if not key_file.is_file() or key_file.stat().st_mode & 0o077:
                raise ValueError("The Anthropic key file must have owner-only permissions (0600)")
            key = key_file.read_text().strip()
        if key is not None and (not key.strip() or any(c.isspace() for c in key)):
            raise ValueError("The Anthropic API key must be nonempty and contain no whitespace")
        rates_file = os.environ.get("CHATGPT_GATEWAY_ANTHROPIC_RATES_FILE")
        rates = None
        if rates_file:
            try:
                rates = anthropic_rates(json.loads(Path(rates_file).expanduser().read_text()))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"The Anthropic rates file could not be read: {error}") from error
        settings = cls(
            data_dir=data_dir,
            port=int(os.environ.get("CHATGPT_GATEWAY_PORT", "8317")),
            max_concurrency=int(os.environ.get("CHATGPT_GATEWAY_MAX_CONCURRENCY", "12")),
            admission_timeout_seconds=float(
                os.environ.get("CHATGPT_GATEWAY_ADMISSION_TIMEOUT_SECONDS", "120")
            ),
            max_queued_requests=int(os.environ.get("CHATGPT_GATEWAY_MAX_QUEUED_REQUESTS", "48")),
            timeout_seconds=float(os.environ.get("CHATGPT_GATEWAY_TIMEOUT_SECONDS", "600")),
            max_request_bytes=int(os.environ.get("CHATGPT_GATEWAY_MAX_REQUEST_BYTES", "8388608")),
            accept_chatgpt_data_controls=data_controls == "1",
            anthropic_api_key=key,
            accept_anthropic_data_controls=anthropic_controls == "1",
            anthropic_max_tokens=int(
                os.environ.get("CHATGPT_GATEWAY_ANTHROPIC_MAX_TOKENS", "32768")
            ),
            anthropic_system_cache_ttl=os.environ.get(
                "CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL", "5m"
            ),
            anthropic_structured_output=os.environ.get(
                "CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT", "native"
            ),
            codex_images_enabled=codex_images == "1",
            codex_binary=os.environ.get("CHATGPT_GATEWAY_CODEX_BINARY", "codex"),
            image_max_concurrency=int(os.environ.get("CHATGPT_GATEWAY_IMAGE_MAX_CONCURRENCY", "1")),
            claude_code_enabled=claude_code == "1",
            claude_binary=os.environ.get("CHATGPT_GATEWAY_CLAUDE_BINARY", "claude"),
            claude_code_max_concurrency=int(
                os.environ.get("CHATGPT_GATEWAY_CLAUDE_CODE_MAX_CONCURRENCY", "1")
            ),
            claude_code_max_tokens=int(
                os.environ.get("CHATGPT_GATEWAY_CLAUDE_CODE_MAX_TOKENS", "32768")
            ),
            anthropic_rates=rates,
        )
        if not 1 <= settings.claude_code_max_concurrency <= settings.max_concurrency:
            raise ValueError("Claude Code concurrency must be between 1 and the shared limit")
        if not 1 <= settings.claude_code_max_tokens <= 128000:
            raise ValueError("Claude Code max tokens must be between 1 and 128000")
        if not settings.claude_binary.strip():
            raise ValueError("CHATGPT_GATEWAY_CLAUDE_BINARY must name a Claude executable")
        if not 1 <= settings.port <= 65535:
            raise ValueError("CHATGPT_GATEWAY_PORT must be between 1 and 65535")
        if settings.max_concurrency < 1 or settings.timeout_seconds <= 0:
            raise ValueError("Concurrency and timeout must be positive")
        if not math.isfinite(settings.timeout_seconds):
            raise ValueError("Timeout must be finite")
        if (
            not math.isfinite(settings.admission_timeout_seconds)
            or settings.admission_timeout_seconds <= 0
        ):
            raise ValueError("Admission timeout must be positive and finite")
        if settings.max_queued_requests < 0:
            raise ValueError("Maximum queued requests must be nonnegative")
        if settings.max_request_bytes < 1024:
            raise ValueError("Maximum request size must be at least 1024 bytes")
        if not 1 <= settings.image_max_concurrency <= settings.max_concurrency:
            raise ValueError("Image concurrency must be between 1 and the shared concurrency limit")
        if not settings.codex_binary.strip():
            raise ValueError("CHATGPT_GATEWAY_CODEX_BINARY must name a Codex executable")
        if not 1 <= settings.anthropic_max_tokens <= 128000:
            raise ValueError("CHATGPT_GATEWAY_ANTHROPIC_MAX_TOKENS must be between 1 and 128000")
        if settings.anthropic_system_cache_ttl not in {"off", "5m", "1h"}:
            raise ValueError("CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL must be off, 5m or 1h")
        if settings.anthropic_structured_output not in {"native", "validated_json"}:
            raise ValueError(
                "CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT must be native or validated_json"
            )
        return settings
