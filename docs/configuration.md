# Configuration and operations

[Quick start](../README.md) · [API compatibility](compatibility.md) · [Architecture](architecture.md)

Settings are read from the server environment at startup; restart after changing them.
There is no automatic `.env` file loader. Keep provider keys in your shell environment,
a secret manager or the supported owner-only key file, outside this repository.

## Commands and settings

| Command | Purpose |
| --- | --- |
| `serve` | Start the loopback server |
| `login` | Authorise or renew the selected connection |
| `login --new` | Register another account/workspace |
| `login --account ACCOUNT_ID` | Reauthorise a particular saved registration |
| `login --no-browser` | Show the sign-in URL for manual browser opening; omits retained ID-token hints |
| `status` / `accounts` | Inspect connections without printing credentials |
| `use ACCOUNT_ID` | Select a saved registration |
| `logout` | Attempt remote revocation and clear local tokens; preserves registration |
| `models` | Refresh the active account's catalog |
| `requests` | Show recent admitted inference outcomes and token usage |
| `key` | Print the local client key |

All commands use the same environment settings. Put `--port` before the command when selecting another port, for example `chatgpt-gateway --port 8318 serve`.

| Environment variable | Default |
| --- | --- |
| `CHATGPT_GATEWAY_DATA_DIR` | `~/.local/share/chatgpt-plan-gateway` |
| `CHATGPT_GATEWAY_PORT` | `8317` |
| `CHATGPT_GATEWAY_MAX_CONCURRENCY` | `12` |
| `CHATGPT_GATEWAY_TIMEOUT_SECONDS` | `600` |
| `CHATGPT_GATEWAY_MAX_REQUEST_BYTES` | `8388608` (8 MiB) |
| `CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS` | `0` (strict data-policy handling) |
| `ANTHROPIC_API_KEY` | Unset; enables the Anthropic API route when configured |
| `CHATGPT_GATEWAY_ANTHROPIC_API_KEY_FILE` | `<data directory>/anthropic-api-key` (owner-only, optional) |
| `CHATGPT_GATEWAY_ANTHROPIC_MAX_TOKENS` | `32768`; used only when a Claude request omits its ceiling |
| `CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL` | `5m`; `off` and `1h` also supported |
| `CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT` | `native`; explicit `validated_json` mode supports larger schemas with full local validation |
| `CHATGPT_GATEWAY_ACCEPT_ANTHROPIC_DATA_CONTROLS` | `0`; separate explicit policy exception, never inherited from ChatGPT |

The server binds to `127.0.0.1`. Management and inference require the local key. Foreign browser origins are refused; this version supports local server/CLI clients rather than direct cross-origin browser applications. One server may use a data directory at a time, preventing refresh-token races.

For a Claude schema that exceeds Anthropic's native grammar limits, start the server with
`CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT=validated_json`. The gateway then appends the
original schema as a formatting instruction and validates the completed JSON against it,
including required fields, types, enum values and numerical constraints. Invalid or incomplete
answers fail. This mode is explicit and disclosed in capabilities, response headers and the
request journal; no automatic mode switch, extra model call or provider fallback is introduced.
The JSON format contract is a system instruction after caller context. Completed invalid answers
return HTTP 422 with bounded validation reasons and usage, allowing the client's correction loop
to ask again explicitly. Both the mode and instruction version are recorded.
It changes the model's formatting context, so record it when comparing benchmarks. The native
mode remains the default. See [the compatibility contract](compatibility.md#claude-chat-completions-input).

Concurrency limits model calls across all clients and **both providers combined** in this gateway process. The authenticated capability endpoint reports the configured `max_concurrency`. Callers should queue excess requests: an occupied gateway returns `429 gateway_busy` after one second. Application batch/job limits are separate and must also allow concurrent runs. Both providers' upstream account limits still apply; this local setting does not increase account allowance.

## Prompt caching

Caller-supplied OpenRouter `cache_control` markers are preserved, including five-minute and one-hour lifetimes. By default, when a request has no cache controls, the gateway marks only its final leading system/developer text block with a five-minute cache marker. It does not cache a changing user suffix automatically or rewrite prompts. Set `CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL=off` to disable this default, or `1h` to request a longer lifetime. A request-level `cache_control:null` disables the default for that request; explicit block markers still apply.

For portable caching when switching back to OpenRouter, put explicit markers in the request. See [the cache example and contract](compatibility.md#claude-chat-and-prompt-caching). Prefixes must be identical and large enough for the chosen model. A cold parallel group is not guaranteed to share a cache: the first real response must begin before later calls can reuse its entry. Writes have a premium; record read/write tokens and latency and compare warm and cold calls. The gateway does not make extra warm-up calls or serialize benchmark runs.

Anthropic usage is translated into OpenRouter's `cached_tokens` and `cache_write_tokens`, with uncached tokens and lifetime breakdown retained. Total prompt tokens include **uncached + cache-read + cache-write** tokens; they are not counted twice. Missing usage remains unknown. Each journal event labels the provider and Anthropic API billing source, and records duration and time to first text without recording text itself.

## OpenRouter data-policy options

OpenRouter `provider.data_collection="deny"` is refused by default. An operator who explicitly accepts **ChatGPT account data controls in place of that OpenRouter policy** may start the gateway with `CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS=1`. This is a policy exception, not an attestation that the original pin is fulfilled. The gateway cannot inspect or change the account's data controls. Each affected call is marked in response headers and the request journal; the capability endpoint exposes whether the exception is enabled. Keep the default for clients that require the original policy. Sampling parameters, token ceilings, provider restrictions and model/billing fallback prohibitions are not relaxed by this setting.

The Claude equivalent is `CHATGPT_GATEWAY_ACCEPT_ANTHROPIC_DATA_CONTROLS=1`.
These settings accept the respective account's data controls for local testing; they do
not certify OpenRouter's policy or change model selection or billing. Both default to `0`.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Local `401 invalid_api_key` | Use the key from `chatgpt-gateway key`, not a vendor key. |
| `sign_in_expired` or repeated `token_expired` | Run `chatgpt-gateway login` again. Eligible token renewal is automatic, with at most one retry of an explicitly rejected request. |
| `429 gateway_busy` | Queue calls in your client; the gateway's default shared concurrency is 12. |
| Upstream usage limit or insufficient credits | Check the selected provider's account limits and billing. The gateway never changes providers to get around them. |
| Unsupported model or field | Select an exact `models` ID and check the [compatibility contract](compatibility.md). This implements a subset of OpenRouter. |
| Claude rejects a large JSON schema | Consider the explicit `validated_json` mode documented above. Completed invalid output still fails validation. |
| Docker cannot connect | On Docker Desktop or OrbStack use `host.docker.internal`, not the container's `127.0.0.1`. Other Docker setups need explicit host networking configuration. |

Use `chatgpt-gateway requests` for request outcomes, latency and token usage.
The journal excludes prompts, answers and credentials. It is not a billing ledger.

## Development

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

The automated tests use mocked providers and consume no model allowance or API credits.
For comparisons, keep prompts, inputs and model IDs fixed; record failures alongside results.
The included probe uses no client-side retries. The gateway's single expired-token recovery
can repeat a rejected HTTP request, but never an already-started stream.
