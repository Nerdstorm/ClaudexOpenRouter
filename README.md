# ChatGPT Plan Gateway

A standalone local gateway that accepts OpenAI/OpenRouter-shaped requests and routes them to an authorised ChatGPT plan connection or the official Claude API. It is independent of Recipe Designer and contains no application prompts or recipe logic.

The gateway supports text generation, JSON schemas, streaming, account-specific model discovery, and image inputs for models that accept them. Image generation and editing are unavailable through the documented plan route. Compatibility covers an explicit subset; see [the compatibility contract](docs/compatibility.md).

Completion is checked against the actual SSE events, including when the upstream omits a `Content-Type` header or leaves the terminal output list empty. Finished output items are retained for validation at completion. Partial text, missing completion and invalid JSON remain failures.

OpenAI models use the documented [Sign in with ChatGPT plan-usage flow](https://developers.openai.com/siwc/token-sharing-open-source) and public Responses API. Claude uses the official Anthropic Messages API with a separately configured Console API key, without Claude Code or an agent harness. Both sit behind the gateway's OpenRouter-compatible Chat endpoint.

## Start and connect

The standalone checkout is `~/src/chatgpt-plan-gateway`. Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
cd ~/src/chatgpt-plan-gateway
uv sync --frozen
uv run chatgpt-gateway serve
```

Keep the server running. In another terminal:

```sh
cd ~/src/chatgpt-plan-gateway
uv run chatgpt-gateway login
uv run chatgpt-gateway status
uv run chatgpt-gateway models
```

`login` opens the system browser at OpenAI. Complete **Continue with ChatGPT** and authorise plan usage for **ChatGPT Plan Gateway**. The callback returns to this local server. Your desktop sign-in can help the browser recognise you, but this application needs its own connection permission. Successful credentials are saved and renewed automatically; normal server restarts do not require another sign-in.

A model appearing in `models` is a catalog choice, not proof of inference access. Start with one small probe for the exact selected model. Terra and Sol are available only if your connected account exposes and permits their exact IDs.

## Enable Claude and prompt caching

Set `ANTHROPIC_API_KEY` in the server's environment, or save your Claude Console API key in `~/.local/share/chatgpt-plan-gateway/anthropic-api-key` with file permissions `0600`. An alternative owner-only file can be selected with `CHATGPT_GATEWAY_ANTHROPIC_API_KEY_FILE`. Restart the server after configuring the key. The key never belongs in this repository or in client requests; clients continue using the local gateway key. `status` reports whether Claude is configured without disclosing its key.

The gateway checks Anthropic's actual model catalogue and exposes `anthropic/claude-sonnet-5.5` and `anthropic/claude-opus-5.5` only when those exact API models are listed. Claude works without a ChatGPT login; OpenAI calls still require that login. The `/v1/responses` route remains OpenAI-only; use `/api/v1/chat/completions` for Claude text, JSON-schema output and streaming.

Caller-supplied OpenRouter `cache_control` markers are preserved, including five-minute and one-hour lifetimes. By default, when a request has no cache controls, the gateway marks only its final leading system/developer text block with a five-minute cache marker. It does not cache a changing user suffix automatically or rewrite prompts. Set `CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL=off` to disable this default, or `1h` to request a longer lifetime. A request-level `cache_control:null` disables the default for that request; explicit block markers still apply.

For portable caching when switching back to OpenRouter, put explicit markers in the request. See [the cache example and contract](docs/compatibility.md#claude-chat-and-prompt-caching). Prefixes must be identical and large enough for the chosen model. A cold parallel group is not guaranteed to share a cache: the first real response must begin before later calls can reuse its entry. Writes have a premium; record read/write tokens and latency and compare warm and cold calls. The gateway does not make extra warm-up calls or serialize benchmark runs.

Anthropic usage is translated into OpenRouter's `cached_tokens` and `cache_write_tokens`, with uncached tokens and lifetime breakdown retained. Total prompt tokens include **uncached + cache-read + cache-write** tokens; they are not counted twice. Missing usage remains unknown. Each journal event labels the provider and Anthropic API billing source, and records duration and time to first text without recording text itself.

Included Max credits must be claimed in Claude Console. Calls use that organization's API balance; purchased credits or invoiced usage can follow after included credits run out. The gateway cannot verify or enforce an included-credits-only balance. Use Console billing controls and workspace spend limits. There is no model/provider/billing fallback in the gateway. See [Anthropic's included-credit documentation](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers).

Switching a client back to `https://openrouter.ai/api/v1` and an OpenRouter key preserves the supported model IDs, request fields and caching markers. This switches billing to OpenRouter and may start with a cold cache. Claude-plan API credits do not fund OpenRouter. Provider-specific options remain explicit errors on routes that do not support them.

## Connect a client

| Setting | Value |
| --- | --- |
| OpenAI-shaped base URL | `http://127.0.0.1:8317/v1` |
| OpenRouter-shaped base URL | `http://127.0.0.1:8317/api/v1` |
| Base URL from Docker Desktop / OrbStack containers | `http://host.docker.internal:8317/api/v1` |
| API key | Local gateway key from `uv run chatgpt-gateway key` |
| Model | Exact `id` from `models`; OpenAI also accepts its bare `canonical_slug` |
| SDK retries | `0`, so a benchmark records each failure without hidden repeat calls |

The local gateway key authenticates applications to this server. It is separate from the OpenAI credentials saved by sign-in and the server's Anthropic API key.

For the included probe:

```sh
export CHATGPT_GATEWAY_KEY="$(uv run chatgpt-gateway key)"
uv run python examples/probe.py --model openai/EXACT_MODEL_ID
uv run python examples/probe.py --model openai/EXACT_MODEL_ID --format json
```

Replace `EXACT_MODEL_ID` with an ID from `models`. Each probe makes one inference request and consumes allowance. The probe prints the returned model, token usage when available, and answer. It does not retry or repair invalid JSON.

A Python client can also use `OpenAI(base_url=..., api_key=..., max_retries=0)` with `chat.completions.create`, `chat.completions.parse`, `responses.create`, or `responses.parse`. The compatibility tests exercise the real SDK against a mock upstream.

## Plan allowance and credits

Use [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) to manage this app's allowance and whether it may use credits after reaching plan limits. If you want subscription allowance only, disable credits for this app there. The OpenAI route has no API-key billing fallback, and neither route has a provider fallback. The gateway cannot override the account's credit permission. A model's token counts do not establish a cash price.

Plan usage can share limits with other applications, and account/workspace restrictions still apply. Quota failures are returned as failures. There is no automatic retry, model substitution, or switch to another billing route. See [OpenAI's accounts and usage documentation](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions).

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
mode remains the default. See [the compatibility contract](docs/compatibility.md#claude-chat-completions-input).

Concurrency limits model calls across all clients and **both providers combined** in this gateway process. The authenticated capability endpoint reports the configured `max_concurrency`. Callers should queue excess requests: an occupied gateway returns `429 gateway_busy` after one second. Application batch/job limits are separate and must also allow concurrent runs. Both providers' upstream account limits still apply; this local setting does not increase account allowance.

## Benchmarking

For role comparisons, compare chef models with the judge and inputs held fixed. Compare judge models against the same saved, human-labelled recipes. Record the actual returned model, reasoning setting, schema/prompt version, completion status, latency, and usage. Keep quota failures and interrupted streams visible rather than counting them as model-quality results.

Use multiple recipe types and randomise/interleave model order. Repeated calls on one recipe are related observations; they cannot replace a diverse recipe sample. Determine a larger sample size from a pilot and a prespecified useful effect. Confirm shortlisted configurations on the production provider before changing production choices.

The gateway records metadata for those comparisons but does not run an application's benchmark suite. Clients may use the existing OpenRouter base-URL and API-key overrides; their application source need not change. The OpenRouter native web-search tool is translated to OpenAI's native Responses search where the model and account permit it.

OpenRouter `provider.data_collection="deny"` is refused by default. An operator who explicitly accepts **ChatGPT account data controls in place of that OpenRouter policy** may start the gateway with `CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS=1`. This is a policy exception, not an attestation that the original pin is fulfilled. The gateway cannot inspect or change the account's data controls. Each affected call is marked in response headers and the request journal; the capability endpoint exposes whether the exception is enabled. Keep the default for clients that require the original policy. Sampling parameters, token ceilings, provider restrictions and model/billing fallback prohibitions are not relaxed by this setting.

## Storage, development, and current verification

Runtime state lives outside the source tree: stable host identity and local key, separate account records, active selection, locks, and `requests.jsonl`. Credential files use owner-only permissions and atomic writes. The journal records IDs, settings, a request-body hash, outcomes, and usage; it excludes prompts, answers, schemas, and tokens. Callback access logging is disabled because callback URLs contain temporary codes.

See [architecture](docs/architecture.md) for responsibilities, auth lifecycle, storage and request flow, and [compatibility](docs/compatibility.md) for exact accepted fields and failure handling.

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

Automated tests mock OpenAI and Anthropic; they cover protocol/SDK compatibility, signed OAuth identity checks, refresh rotation, cache accounting, original-schema validation, shared concurrency, local boundaries and failure paths without consuming allowance or API credits. A successful local test suite does not establish that a particular real account or model can use either route. Configured credentials and a completed live probe remain necessary on a new installation.
