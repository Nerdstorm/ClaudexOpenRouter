# ChatGPT Plan Gateway

A standalone local gateway that lets applications send OpenAI/OpenRouter-shaped requests through an authorised ChatGPT plan connection. It is independent of Recipe Designer and contains no application prompts or recipe logic.

The gateway supports text generation, JSON schemas, streaming, account-specific model discovery, and image inputs for models that accept them. Image generation and editing are unavailable through the documented plan route. Compatibility covers an explicit subset; see [the compatibility contract](docs/compatibility.md).

Completion is checked against the actual SSE events, including when the upstream omits a `Content-Type` header or leaves the terminal output list empty. Finished output items are retained for validation at completion. Partial text, missing completion and invalid JSON remain failures.

Models run at OpenAI. The gateway uses the documented [Sign in with ChatGPT plan-usage flow](https://developers.openai.com/siwc/token-sharing-open-source) and the public Responses API.

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

## Connect a client

| Setting | Value |
| --- | --- |
| OpenAI-shaped base URL | `http://127.0.0.1:8317/v1` |
| OpenRouter-shaped base URL | `http://127.0.0.1:8317/api/v1` |
| API key | Local gateway key from `uv run chatgpt-gateway key` |
| Model | Exact `id` from `models`, or its bare `canonical_slug` |
| SDK retries | `0`, so a benchmark records each failure without hidden repeat calls |

The local gateway key authenticates applications to this server. It is separate from the OpenAI credentials saved by sign-in.

For the included probe:

```sh
export CHATGPT_GATEWAY_KEY="$(uv run chatgpt-gateway key)"
uv run python examples/probe.py --model openai/EXACT_MODEL_ID
uv run python examples/probe.py --model openai/EXACT_MODEL_ID --format json
```

Replace `EXACT_MODEL_ID` with an ID from `models`. Each probe makes one inference request and consumes allowance. The probe prints the returned model, token usage when available, and answer. It does not retry or repair invalid JSON.

A Python client can also use `OpenAI(base_url=..., api_key=..., max_retries=0)` with `chat.completions.create`, `chat.completions.parse`, `responses.create`, or `responses.parse`. The compatibility tests exercise the real SDK against a mock upstream.

## Plan allowance and credits

Use [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) to manage this app's allowance and whether it may use credits after reaching plan limits. If you want subscription allowance only, disable credits for this app there. The gateway has no API-key billing route or provider fallback, but it cannot override the account's credit permission. A model's token counts do not establish a cash price.

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
| `CHATGPT_GATEWAY_MAX_CONCURRENCY` | `2` |
| `CHATGPT_GATEWAY_TIMEOUT_SECONDS` | `600` |
| `CHATGPT_GATEWAY_MAX_REQUEST_BYTES` | `8388608` (8 MiB) |

The server binds to `127.0.0.1`. Management and inference require the local key. Foreign browser origins are refused; this version supports local server/CLI clients rather than direct cross-origin browser applications. One server may use a data directory at a time, preventing refresh-token races.

## Benchmarking

For role comparisons, compare chef models with the judge and inputs held fixed. Compare judge models against the same saved, human-labelled recipes. Record the actual returned model, reasoning setting, schema/prompt version, completion status, latency, and usage. Keep quota failures and interrupted streams visible rather than counting them as model-quality results.

Use multiple recipe types and randomise/interleave model order. Repeated calls on one recipe are related observations; they cannot replace a diverse recipe sample. Determine a larger sample size from a pilot and a prespecified useful effect. Confirm shortlisted configurations on the production provider before changing production choices.

The gateway records metadata for those comparisons but does not run the benchmark suite. Recipe Designer has not been connected or changed. In particular, clients using OpenRouter `provider.data_collection="deny"`, sampling parameters, or token ceilings need an explicit compatibility decision; those requests fail here instead of silently changing their meaning.

## Storage, development, and current verification

Runtime state lives outside the source tree: stable host identity and local key, separate account records, active selection, locks, and `requests.jsonl`. Credential files use owner-only permissions and atomic writes. The journal records IDs, settings, a request-body hash, outcomes, and usage; it excludes prompts, answers, schemas, and tokens. Callback access logging is disabled because callback URLs contain temporary codes.

See [architecture](docs/architecture.md) for responsibilities, auth lifecycle, storage and request flow, and [compatibility](docs/compatibility.md) for exact accepted fields and failure handling.

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

Automated tests mock OpenAI; they cover protocol/SDK compatibility, signed OAuth identity checks, refresh rotation, local boundaries, and failure paths without consuming plan allowance. A successful local test suite does not establish that a particular real account or model can use this preview route. Account authorisation and a completed live probe are the remaining checks on a new installation.
