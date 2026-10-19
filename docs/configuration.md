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
| `models --backend claude-code` | List the separate subscription backend's supported models |
| `claude-login` / `claude-status` | Native Claude subscription sign-in / redacted auth status; does not need a running gateway |
| `capabilities` | Inspect provider readiness, image-model restrictions and concurrency |
| `requests` | Show recent admitted inference outcomes and token usage |
| `key` | Print the local client key |

All commands use the same environment settings. Put `--port` before the command when selecting another port, for example `chatgpt-gateway --port 8318 serve`.

| Environment variable | Default |
| --- | --- |
| `CHATGPT_GATEWAY_DATA_DIR` | `~/.local/share/chatgpt-plan-gateway` |
| `CHATGPT_GATEWAY_PORT` | `8317` |
| `CHATGPT_GATEWAY_MAX_CONCURRENCY` | `12` |
| `CHATGPT_GATEWAY_ADMISSION_TIMEOUT_SECONDS` | `120`; positive, finite maximum wait for capacity |
| `CHATGPT_GATEWAY_MAX_QUEUED_REQUESTS` | `48`; maximum waiting requests across all providers, `0` refuses immediately when busy |
| `CHATGPT_GATEWAY_TIMEOUT_SECONDS` | `600`; total capacity-wait plus execution budget |
| `CHATGPT_GATEWAY_MAX_REQUEST_BYTES` | `8388608` (8 MiB) |
| `CHATGPT_GATEWAY_CODEX_IMAGES` | `0`; set `1` to enable native Codex default image generation |
| `CHATGPT_GATEWAY_CODEX_BINARY` | `codex`; executable name or absolute path |
| `CHATGPT_GATEWAY_IMAGE_MAX_CONCURRENCY` | `1`; must be between 1 and the shared maximum |
| `CHATGPT_GATEWAY_CLAUDE_CODE` | `0`; set `1` to enable the separate Claude Code subscription route |
| `CHATGPT_GATEWAY_CLAUDE_BINARY` | `claude`; executable name or absolute path |
| `CHATGPT_GATEWAY_CLAUDE_CODE_MAX_CONCURRENCY` | `1`; within the shared maximum |
| `CHATGPT_GATEWAY_CLAUDE_CODE_MAX_TOKENS` | `32768`; native output ceiling when the caller omits one, 1–128000 |
| `CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS` | `0` (strict data-policy handling) |
| `ANTHROPIC_API_KEY` | Unset; enables the Anthropic API route when configured |
| `CHATGPT_GATEWAY_ANTHROPIC_API_KEY_FILE` | `<data directory>/anthropic-api-key` (owner-only, optional) |
| `CHATGPT_GATEWAY_ANTHROPIC_MAX_TOKENS` | `32768`; used only when a Claude request omits its ceiling |
| `CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL` | `5m`; `off` and `1h` also supported |
| `CHATGPT_GATEWAY_ANTHROPIC_RATES_FILE` | unset; a JSON rates file (see `examples/anthropic-rates.json`) that prices Anthropic API calls; native subscription usage is unpriced |
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

Concurrency limits model calls across all clients and **all providers combined**, including
Codex images and Claude Code. The shared maximum defaults to 12; the nested image and
Claude Code maxima each default to 1.
An image takes one image slot and one shared slot. Images waiting for their narrower pool do
not consume text capacity. For example, 11 text calls plus 1 image fill the default pool.
Set the variables before starting the server to change them. Image and Claude concurrency cannot
exceed the shared maximum. All transports share one bounded waiting budget: at most 48 requests,
each waiting at most 120 seconds by default, or the remaining total request timeout if shorter.
Acquire the narrower image/Claude pool before the shared slot, so requests waiting for their
own pool do not occupy shared execution slots. Eligible text calls can proceed while images wait.
Waiting is in memory, with no durable job, inference replay or automatic model retry. Queue
capacity is returned on cancellation, HTTP disconnect or timeout. A full queue or exhausted
waiting budget returns `429 gateway_busy`, with `Retry-After: 1`. Provider quota refusals are
still returned directly. Clients should allow for the total request timeout.
The authenticated capability endpoint reports `max_concurrency` and
`concurrency.{shared_max,image_max,claude_code_max,images_use_shared_pool,claude_code_uses_shared_pool,admission_timeout_seconds,queue,queue_max,queue_persistent,waiting,active}`.
`active` counts requests holding execution slots; `waiting` counts requests waiting for either
pool. Completed request journal entries include `admission_wait_ms` separately from execution
`duration_ms` and `first_token_ms`, so queueing does not look like slower model generation.
Application batch/job limits and upstream account quotas are separate. A local cap does not
increase allowance or guarantee that the provider accepts that many concurrent tasks.

## Native Claude subscription worker

Enable `CHATGPT_GATEWAY_CLAUDE_CODE=1`, use `claude-login` and complete native subscription
sign-in, then check `claude-status`. The worker requires POSIX process groups and Claude Code
2.1.284 or later; tested with 2.1.289. `--bare` is deliberately avoided because it skips native
OAuth/keychain authentication. Safe mode and empty setting sources disable user/project
customizations while preserving native login. Managed policy still applies.

The gateway removes inherited API keys, bearer tokens, cloud/provider overrides, proxy variables
and parent session settings from the native environment. It checks native auth before every
execution and requires `claude.ai`, `firstParty` and an eligible subscription type. A session
that reports an API-key source is refused. Credentials stay in Claude Code's own storage;
this route does not use the gateway's Anthropic key file or public ChatGPT OAuth registration.

Only `/claude-code/v1/models` and `/claude-code/v1/chat/completions` select this backend.
The existing `/api/v1` and `/v1` Claude model IDs continue to use the Console API key. There
is no automatic fallback in either direction. No client application's settings are changed.
Native discovery lists the two gateway-supported model pins after version/auth checks; it
does not call a subscription catalogue or prove model entitlement. Every completed answer
must verify the pinned model in session, assistant and per-model usage metadata.

Requests run in private temporary directories with tools/MCP/slash commands/browser integration
disabled, caller system context replacing the coding prompt, and session persistence disabled.
The subprocess receives prompt text over stdin and a private system file; neither raw output
nor prompt/schema content is journalled. CLI sessions are limited to one answer turn and the
gateway accepts only a final success result, one turn, the expected model and a completed
stop reason. Timeout, HTTP disconnect, unexpected tools/models and failure terminate the process
group and release both pools. Native API retry notifications stop the worker; the gateway
never replays inference or switches billing routes. Native internals remain a benchmark variable.

Structured output uses the explicit `validated_json` contract, with the original schema in a
formatting instruction and original local validation. No native schema tool/re-prompt loop is
enabled. Invalid completed JSON returns HTTP 422 with bounded validation reasons and known
usage. The CLI manages prompt caching automatically; block markers, TTLs and cache opt-outs
are refused. Native output ceilings use documented `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; Claude
applies its model limits. Conversation history and streaming require the API route for now.

Response headers and `gateway_usage` identify `claude_code`, `claude_subscription`, contract
version, actual served model, turn count and retry events. Native fresh/read/write tokens are
normalized using the same accounting rules as the API backend. Missing usage stays null.
These counters include Claude Code overhead; the gateway does not infer remaining subscription
percentage or cash spend from the CLI's API-equivalent cost estimate. Account usage-credit
settings may permit paid continuation; the gateway neither verifies nor changes that setting.

References: [native automation](https://code.claude.com/docs/en/headless),
[safe mode and flags](https://code.claude.com/docs/en/cli-reference),
[authentication precedence](https://code.claude.com/docs/en/authentication),
[model fallback and identity](https://code.claude.com/docs/en/model-config).

## Native Codex image worker

Images are opt-in and require POSIX process groups (macOS/Linux); this worker does not
support Windows process management yet. Start a recent native Codex CLI with `codex login`, choose ChatGPT, and
verify `codex login status`; then set `CHATGPT_GATEWAY_CODEX_IMAGES=1` in the gateway environment.
Tested with Codex CLI `0.163.0-alpha.10.2`. This worker requires CLI support for `exec`,
`--ignore-user-config`, `--ignore-rules`, `--ephemeral`, JSON events, output schemas and
built-in image generation. It uses Codex's own credential store. The gateway never extracts
tokens or installs a private backend proxy. Codex login and the gateway's public OAuth
connection are independent. API-key-only native login is refused and API-key variables are
not passed to the worker. No paid Images API route or fallback exists.

Only the `codex/imagegen` capability alias is exposed: **the default image engine selected by
Codex**, not selectable GPT Image models. Current official docs identify the built-in default
as `gpt-image-2`; the gateway reports `codex_default` and `image_model_verified:false` because
it does not independently verify or pin that engine/snapshot. Size, quality and background
are automatic; explicit values are refused. A natural-language request can describe a desired
composition, but it is not an enforced API setting. One final PNG is returned per request.

Readiness checks the executable and ChatGPT login, cached for 15 seconds. It does not spend
allowance or prove generation entitlement. Missing readiness hides the model from `/models`
and explains the reason under `providers.codex` in `/gateway/capabilities`. A configured but
unavailable image request fails explicitly. An enabled Codex-only gateway can list and use
its image entry without a separate gateway OAuth connection or Anthropic key.

Each request runs an ephemeral Codex agent task in a private temporary directory, with user
configuration/rules, project instruction discovery, shell, browser and app tools disabled.
Prompts go over stdin, references are validated PNG/JPEG/WebP inputs, and local API keys are
excluded from the environment. A completed turn and a valid image within that newly created
Codex thread's generated-image directory are required. Unrelated paths and symlink escapes
are rejected. The worker validates and returns a PNG, then removes its temporary references
and current-thread generated images. Raw CLI output and prompts are never journalled.
Timeouts and caller disconnects terminate the worker process group and release both pools.

`CHATGPT_GATEWAY_TIMEOUT_SECONDS` also bounds image tasks, including capacity wait and native login preflight;
it defaults to 600 seconds. CLI output and returned images have size limits. Malformed or
missing artifacts after a completed turn return `422 image_not_produced`; a partial turn is
not success. Usage limits fail explicitly. There is no gateway retry to regenerate an image.
Each request is an agent workflow, so its latency and reported agent tokens include harness
overhead. Native image token/cash usage is not separately available: `usage` is null,
`gateway_usage.agent` contains only actual CLI token counters and `gateway_usage.image` stays
null. Unknown usage is never zero or a fabricated cash price. Record the backend and contract
version when comparing these outputs to direct API generations.

See [image request formats](compatibility.md#codex-default-image-generation) and
[OpenAI's Codex image documentation](https://learn.chatgpt.com/docs/image-generation).

## Prompt caching

Caller-supplied OpenRouter `cache_control` markers are preserved, including five-minute and one-hour lifetimes. By default, when a request has no cache controls, the gateway marks only its final leading system/developer text block with a five-minute cache marker. It does not cache a changing user suffix automatically or rewrite prompts. Set `CHATGPT_GATEWAY_ANTHROPIC_SYSTEM_CACHE_TTL=off` to disable this default, or `1h` to request a longer lifetime. A request-level `cache_control:null` disables the default for that request; explicit block markers still apply.

For portable caching when switching back to OpenRouter, put explicit markers in the request. See [the cache example and contract](compatibility.md#claude-chat-and-prompt-caching). Prefixes must be identical and large enough for the chosen model. A cold parallel group is not guaranteed to share a cache: the first real response must begin before later calls can reuse its entry. Writes have a premium; record read/write tokens and latency and compare warm and cold calls. The gateway does not make extra warm-up calls or serialize benchmark runs.

Anthropic usage is translated into OpenRouter's `cached_tokens` and `cache_write_tokens`, with uncached tokens and lifetime breakdown retained. With `CHATGPT_GATEWAY_ANTHROPIC_RATES_FILE` set, each Anthropic API call's usage also carries OpenRouter's `cost` field: the file's rates (USD per million tokens, per native model, with separate five-minute and one-hour cache-write rates) applied to Anthropic's exact counts, labelled `cost_details.source:"anthropic_rates"` with the file's `as_of` date. These rates never price the native subscription backend. Without the file, or for a model it does not list, no cost is reported. Keep the file current with Anthropic's pricing page; the example holds the published rates on its date. Total prompt tokens include **uncached + cache-read + cache-write** tokens; they are not counted twice. Missing usage remains unknown. Each journal event labels the provider and Anthropic API billing source, and records duration and time to first text without recording text itself.

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
| `429 gateway_busy` | The bounded waiting queue is full or its deadline expired. Reduce demand or adjust the waiting limit/budget; shared execution stays at 12 by default. |
| Codex image entry missing | Enable `CHATGPT_GATEWAY_CODEX_IMAGES=1`, check the executable and ChatGPT native login, then inspect `capabilities`. Readiness caches for 15 seconds. |
| Image model, size or quality refused | Only `codex/imagegen` and default (`auto`) settings are supported; see the image contract. |
| Upstream usage limit or insufficient credits | Check the selected provider's account limits and billing. The gateway never changes providers to get around them. |
| Claude `402 insufficient_credit` | Anthropic refused the API balance. Add API credit in Claude Console, or explicitly use the enabled `/claude-code/v1` subscription backend. The gateway never switches routes automatically. |
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
