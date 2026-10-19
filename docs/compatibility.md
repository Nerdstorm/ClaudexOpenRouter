# Compatibility contract (v0.4)

This is a local adapter for the documented ChatGPT-plan Responses route, official Anthropic
Messages API, optional native Claude subscription backend and Codex default image worker. OpenAI/OpenRouter-shaped paths
and supported fields are compatible; complete provider/API parity is not claimed. Unsupported
non-null parameters produce a client error before inference. Provider selection follows the
exact model id, with no model or provider fallback.
The separate `/claude-code/v1` base path explicitly selects subscription usage for Claude;
the same Claude IDs on the original base paths retain API billing.

## Routes

All routes in the following table require the local bearer key.

| Route | Behaviour |
| --- | --- |
| `GET /v1/models` or `/api/v1/models` | Real configured-provider catalogues; exact OpenRouter ids, no fabricated prices or access guarantee |
| `POST /v1/chat/completions` or `/api/v1/chat/completions` | OpenAI text/image input, Claude text, or the explicit Codex image subset; images do not stream |
| `POST /v1/responses` or `/api/v1/responses` | OpenAI-only supported Responses body; object or native SSE events |
| `GET /gateway/capabilities` | Gateway capability summary, distinct from real model entitlement |
| `GET /gateway/status`, `/gateway/accounts`, `/gateway/requests` | Connection information and admitted request metadata |
| `POST /gateway/login`, `/gateway/accounts/select`, `/gateway/logout` | Authorise, select or end a connection |
| `POST /v1/images/generations` or `/api/v1/images/generations` | Optional `codex/imagegen` default image generation; JSON or multipart input, inline PNG output |
| `POST /v1/images/edits` or `/api/v1/images/edits` | Same worker, requiring 1–5 reference images |
| `POST /api/v1/images` | Alias for image generation |
| `GET /claude-code/v1/models` | Native CLI/version/subscription readiness plus gateway-supported Sonnet/Opus IDs; no entitlement guarantee |
| `POST /claude-code/v1/chat/completions` | Optional subscription text/validated-JSON subset, nonstreaming |

`GET /healthz` and `/` are public local service information. `/auth/callback` is protected by one-time OAuth state/nonce/PKCE instead of a client bearer. Callback stylesheet assets are public. Other paths are outside the contract.

Local containers on Docker Desktop or OrbStack may use `http://host.docker.internal:8317/api/v1`.
That hostname is admitted alongside the loopback names; the server still binds to `127.0.0.1`,
requires the local bearer key and rejects foreign browser origins.

## Claude Code subscription

Explicit base URL: `http://127.0.0.1:8317/claude-code/v1`, using the same local bearer key.
Supported model IDs are exactly `anthropic/claude-sonnet-5.5` and `anthropic/claude-opus-5.5`.
The original base URLs retain API billing for those IDs. Native discovery checks installation,
version and subscription sign-in; model entitlement is established only by successful inference.

| Field | Subscription contract |
| --- | --- |
| `messages` | Leading system/developer text, then exactly one user message; ordered text blocks are joined with blank lines. No native assistant/history import. |
| `response_format` | Text or explicit `json_schema`; `validated_json` formatting plus original local validation. No native schema tool/re-prompt loop. |
| `max_tokens` / `max_completion_tokens` | One ceiling, integer 1–128000, default 32768; mapped to the CLI's documented output ceiling, subject to its model limit. |
| `reasoning_effort` / `reasoning.effort` | One of low, medium, high, xhigh or max, passed explicitly to the CLI. Native model support still applies. |
| `stream` | Omitted or false; streaming and stream options are refused. |
| `provider`, `n`, empty tools | Same explicit provider pins, no fallback, n=1, no function/search tools. Account data controls replace any explicitly enabled OpenRouter data-policy override. |
| `cache_control` or block cache markers | Refused, including root null. CLI-managed caching does not promise API TTLs, breakpoints or a caller cache opt-out. |

Image input, Responses, temperature/top-p/seed, function/search tools, history replay and
automatic fallback are outside this contract. Unsupported requests fail before native inference.
Use the API backend when these supported API features are needed. No history is silently
flattened, no assistant answer is generated to replay supplied context, and sessions are not resumed.

Completed Chat responses identify `provider:"claude_code"` and keep the requested public model
ID only after validating exact native model evidence. Headers identify subscription billing,
native contract and structured-output version. `gateway_usage` includes actual served models,
turn count and retry-event count. Token/cache usage includes CLI overhead, not just user text.
The gateway does not expose the CLI's API-equivalent price as cash spend or infer quota percentage.

Partial output, truncation, multiple turns, missing completion/model evidence and tool/billing
substitution fail explicitly. Native API retry notifications terminate the task; no replay is
initiated by the gateway. Completed invalid JSON is HTTP 422 with safe validation details and
known usage; a correction requires a new caller request. HTTP disconnects/timeouts kill the
native process group and release both admission pools.

Each request benchmarks Claude Code's harness with a replaced system prompt and disabled tools,
not an identical bare Messages API transaction. See [setup, account controls and process details](configuration.md#native-claude-subscription-worker).

## Codex default image generation

Enable `CHATGPT_GATEWAY_CODEX_IMAGES=1` and native Codex ChatGPT login. The only image model
ID is **`codex/imagegen`**, a capability alias for **Codex's default image engine**. It is
not a pinned `gpt-image-*` ID. Sunburst/Flare and other engine selections are refused, not
translated to the default. `image_model:"codex_default"`, `selectable_image_model:false` in
discovery, and `image_model_verified:false` distinguish this from verified model selection.
The default can change with Codex; no underlying snapshot or paid API route is promised.

Generation JSON example:

```json
{
  "model": "codex/imagegen",
  "prompt": "One blue ceramic mug on a white background, no text.",
  "n": 1
}
```

Accepted image fields:

| Field | Contract |
| --- | --- |
| `model` | Exactly `codex/imagegen` |
| `prompt` | Nonempty string, at most 32000 characters |
| `image` | Optional base64 data URL or list of 1–5 data URLs in JSON; PNG/JPEG/WebP only. Required on edits. Multipart accepts uploaded `image` or repeated `image[]` files. |
| `n` | Omitted or integer `1`; one final output image per request |
| `size`, `quality`, `background` | Omitted, null or `auto` only. Other values cannot be enforced through this worker and are refused. |
| `output_format` | Omitted, null or `png`; worker validates and normalises output to PNG |
| `response_format` | Omitted, null or `b64_json`; no generated URLs |
| `stream` | Omitted, null or JSON `false`; no image SSE/partial previews |
| `provider` | Optional `only:["openai"]`, `allow_fallbacks:false`, `require_parameters` boolean, with existing explicit ChatGPT data-policy handling |

Unknown non-null fields are refused, including engine overrides, seed, masks, crop controls,
reasoning settings and token ceilings. JSON image references must be inline data URLs: the
gateway never downloads remote references or accepts caller filesystem paths. Multipart is
compatible with `OpenAI(...).images.edit(image=("reference.png", bytes, "image/png"), ...)`.
The combined request limit is 8 MiB by default, including multipart/base64 overhead. Input and
output are limited to static PNG/JPEG/WebP with at most 20 million pixels; output PNG is capped
at 32 MiB. Unsupported payloads fail before launching inference.

OpenRouter-shaped image Chat requests use the same backend, with an explicit image model:

```json
{
  "model": "codex/imagegen",
  "modalities": ["image", "text"],
  "messages": [{"role":"user","content":[
    {"type":"text","text":"Make the mug blue, preserving the composition."},
    {"type":"image_url","image_url":{"url":"data:image/png;base64,REFERENCE_BYTES"}}
  ]}]
}
```

This subset accepts user messages only, text and image URL parts (with data URLs only),
`modalities:["image"]` or either order of `["image","text"]`, optional `n:1`, `stream:false`
and the same provider pins. It does not reinterpret ordinary OpenAI/Claude text models as
image generators. Native `/responses` continues to exclude hosted image generation.

Image endpoints return `data[0].b64_json`; Chat returns
`choices[0].message.images[0].image_url.url` containing a PNG data URL. Both return
`model:"codex/imagegen"`, `provider:"codex"`, `image_model:"codex_default"`,
`image_model_verified:false`, `usage:null` and `gateway_usage:{agent:...,image:null}`.
Agent counters are actual CLI token values, including harness context; image token/cash usage
is unknown. No cost or zero usage is invented. Headers expose `x-gateway-provider:codex`,
`x-gateway-billing-source:codex_plan`, `x-gateway-data-controls:codex_account`,
`x-gateway-image-model:codex_default` and `x-gateway-image-contract-version:codex-image.v1`.
These are Codex account controls, not a claim to fulfil OpenRouter's data-collection contract.

The image cap defaults to **1 within the shared 12 calls**, with no separate allowance or
additional 12-call pool. A saturated image or shared pool returns `429 gateway_busy` after
at most one second, with `Retry-After:1`. Requests have the shared timeout (600 seconds by
default); cancellation stops their native process group and cleans request artifacts.
Completed turns without valid current-thread images fail with `422 image_not_produced` and
known agent usage. Missing completion fails with `502 incomplete_stream`; native usage-limit
failures return `429 codex_usage_limit`. There is no regeneration retry or engine/billing fallback.
The model catalogue's `access_verified:false` means readiness is not a live entitlement check.

## OpenAI Chat Completions input

- `model`: exact catalog slug, with optional `openai/` prefix. Other providers, automatic model choice and fallback lists are unavailable.
- `messages`: nonempty array of `system`, `developer`, `user` and `assistant` messages. Context comes only from this request. System messages become developer messages because the plan route rejects explicit system items.
- `content`: a string or array of text parts; user images use `image_url` with an HTTPS URL or `data:image/` URL. Image detail can be `auto`, `low`, `high`, or `original`. Availability depends on the selected model. Assistant image and refusal history are outside this version's translator.
- `response_format`: `text`, `json_object`, or named `json_schema`. The JSON schema is checked before sending; local fragment references are permitted, external references are rejected. At completion, JSON is parsed and schema-validated locally. Model refusals are preserved.
- `reasoning_effort`, or `reasoning` with `effort`/`summary`: passed through after validation. Efforts are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`; the selected model may reject otherwise valid settings. Both forms together are refused.
- `stream`: boolean, default false. `stream_options.include_usage` requests a final usage chunk.
- `n`: exactly `1`.
- `tools`: accepts exactly one `{"type":"openrouter:web_search","parameters":{"engine":"native"}}`, translated to a native Responses `web_search`. Search tool choice may be `auto`, `required`, `none` or omitted. Other engines, parameters, duplicate tools and Chat function tools are refused. Empty tools or `tool_choice:"none"` are accepted. Use supported native Responses namespaces for function definitions. Search remains subject to model/account admission.
- `provider`: `only:["openai"]`, `allow_fallbacks:false` and `require_parameters` are accepted compatibility preferences. `data_collection:"allow"` is accepted; `"deny"` is refused by default because this gateway cannot attest OpenRouter's data-policy contract. Only an explicit operator exception (`CHATGPT_GATEWAY_ACCEPT_CHATGPT_DATA_CONTROLS=1`) permits that request under the connected account's ChatGPT data controls. This setting does not enforce or verify a no-training policy. Such calls carry `x-gateway-policy-override: provider.data_collection` and `x-gateway-data-controls: chatgpt_account`, and journal `data_policy_override:true`. All other pins remain checked. `/gateway/capabilities` reports the exception's state.

Sampling/ceiling controls such as `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `seed`, penalties and stop sequences are refused on the OpenAI plan route. They are not silently discarded, replaced with defaults, or recorded as if applied.

## Claude Chat and prompt caching

Claude accepts the same Chat paths and uses exact `anthropic/claude-sonnet-5.5` or `anthropic/claude-opus-5.5` ids, explicitly mapped to `claude-sonnet-5-5` and `claude-opus-5-5`. The actual API catalogue and returned model must match. Missing credentials return `503 provider_not_configured`; an unlisted model returns `404 model_not_available`. The catalogue can list Claude without a ChatGPT login. Unsupported aliases, fallbacks, image inputs, tools, sampling controls, Responses input and assistant prefill are errors.

Leading `system`/`developer` text becomes Anthropic system blocks, in order, without added instructions. User/assistant conversation text remains in order and must start and end with a user turn. Late system messages are refused rather than moved. Text content arrays accept `type`, `text`, and optional `cache_control`. Plain strings are also accepted.

`stream`, `stream_options.include_usage` and `n:1` work as on the OpenAI route. `max_tokens` or `max_completion_tokens` provides an explicit output ceiling of 1–128000 tokens; supplying both is an error. When neither is supplied, the mandatory Anthropic ceiling uses the disclosed server default, 32768. The chosen upstream model may impose a tighter limit. `reasoning_effort`, or `reasoning.effort`, supports `low`, `medium`, `high`, `xhigh` and `max`, passed as Anthropic `output_config.effort` with adaptive thinking. Unsupported effort/summary fields are rejected, not approximated.

JSON output requires an explicit `response_format.type:json_schema`. The vendor's schema transformer translates unsupported constraints into the documented supported form. The unchanged original schema, including numerical/string constraints, is checked locally at completion. Schema failures, refusals, output truncation and quota failures are distinct. There is no follow-up call to repair a response. `json_object` without a schema is refused rather than turning it into an empty-object schema or adding a prompt.

Both request-level automatic caching and content-block breakpoints accept `cache_control:{"type":"ephemeral"}` with optional `ttl:"5m"` or `ttl:"1h"`. There are at most four slots including an automatic marker. Longer-lived breakpoints must precede shorter-lived ones. Explicit markers suppress the gateway's system-cache default. With no caller cache controls, that default marks only the final leading system block; it adds no marker to varying user data. Request-level `cache_control:null` opts out of this default. The server's default can also be `off` or `1h`.

Portable example for either the gateway or OpenRouter:

```json
{
  "model": "anthropic/claude-sonnet-5.5",
  "messages": [
    {"role":"system","content":[
      {"type":"text","text":"REUSABLE INSTRUCTIONS AND REFERENCE CONTEXT",
       "cache_control":{"type":"ephemeral","ttl":"1h"}}
    ]},
    {"role":"user","content":"CURRENT TASK AND CHANGING DETAILS"}
  ],
  "max_tokens": 4096
}
```

The placeholder text must be replaced with a real sufficiently long prefix. Exact matching, model-specific minimums and expiry determine cache hits. Explicit request controls remain portable; gateway-injected defaults do not automatically apply on OpenRouter. Caches are not assumed shared across model/provider/organization changes. The gateway neither warms caches with dummy calls nor serializes matching requests. See [Anthropic caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) and [OpenRouter caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).

Claude completion uses the ordinary Chat object/chunk shape, actual OpenRouter model id and `provider:"anthropic"`. Headers disclose `x-gateway-provider:anthropic`, `x-gateway-billing-source:anthropic_api` and `x-gateway-data-controls:claude_console`. Usage normalizes native `cache_read_input_tokens` and `cache_creation_input_tokens` into `prompt_tokens_details.cached_tokens` and `cache_write_tokens`; total input is uncached + read + write. Original native usage and cache-write lifetime breakdowns are retained in journal usage; unknown counts remain null. Time to first text and total duration are recorded separately. No balance or included-credit guarantee is fabricated. A cash price is reported only when the operator configures a rates file: `usage.cost` is then those rates applied to the exact counts, labelled with the rates' date (see configuration).

The same global concurrency pool limits all providers combined, including Codex images.
Anthropic SDK retries are disabled. A validated `message_stop`, completed blocks, correct
model, accepted stop reason and original-schema validation are required before success.
SSE errors whose transport status is HTTP 200 are translated into failure statuses for
nonstreaming clients; streaming clients receive an error without a successful finish marker.
`max_tokens`, server-side fallback and tool blocks cannot masquerade as successful answers.
Closing or timing out a stream releases its slot and records the outcome.

`provider.only:["anthropic"]`, `allow_fallbacks:false` and `require_parameters` are validated. `provider.data_collection:"deny"` remains refused by default because the gateway cannot attest OpenRouter's policy contract. An explicit `CHATGPT_GATEWAY_ACCEPT_ANTHROPIC_DATA_CONTROLS=1` exception accepts Claude Console controls instead and marks affected responses and journal entries. This is separate from the ChatGPT exception; enabling one never enables the other. The gateway does not inspect or change Console training/retention or billing settings.

### Explicit structured-output modes

`CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT=native` is the default. It uses Anthropic's
schema transformation and native grammar, then validates against the client's original schema.
Known grammar-size rejections return `400 schema_compilation_limit`, with a safe explanation
and upstream request ID; native error text is not copied into the response or journal.

When Anthropic refuses a call because the API organization's credit balance is too low, the
gateway returns `402 insufficient_credit` with the upstream request ID instead of a generic
`400`, so a client can stop sending work until credit is added in Claude Console. The gateway
reports the refusal only; it never reads or claims to verify the balance.
This applies to the API backend. The explicit `/claude-code/v1` subscription backend uses
its native Claude account limits, and is never selected as an automatic fallback.

An operator may select `validated_json` when the native compiler cannot support the contract.
The Chat `response_format` and completed response shape stay the same. The gateway appends a
separate system block after caller system context containing a JSON-only instruction and the
complete original schema, preserving property insertion order and all original caller blocks/cache
markers. It sends no native `output_config.format`. Before success, the unchanged original
schema validates the completed JSON, including required fields, additional-property rules,
enum values, types and constraints. Invalid JSON, refusals, truncation and incomplete streams
retain their existing outcomes; failures cause no gateway retry or model fallback.

Capabilities report `providers.anthropic.structured_output_mode` and `structured_output_version`.
Schema requests disclose
`x-gateway-structured-output-mode: native|validated_json`, and both started/terminal journal
events record `structured_output_mode`. Validated JSON also discloses
`x-gateway-structured-output-version` and journals the instruction version (`validated-json.v2`).
Text requests leave these fields null. The format block
is model context and affects tokens and benchmark comparability; this is not native grammar
enforcement. Mode selection is fixed at server startup; a native failure never silently
switches to validated JSON. The OpenAI route is unaffected.

A completed Claude answer that fails JSON/schema validation returns HTTP **422** with
`schema_validation_failed`, at most eight `validation_errors` (paths, rule names and type/missing-field
hints), normalized token usage and the usual route/version headers. Invalid answers and schema
contents are not included in this error. This is a model-output failure, not a transient server
failure: a caller can use the reasons in its bounded correction loop rather than a transport
retry of the unchanged prompt. The gateway sends no additional inference request. Streaming
clients get the same diagnostic error without a successful finish marker; the journal records
failure and usage, without diagnostic paths or reply contents.

## Native Responses input

Accepted top-level fields are `model`, `input`, `instructions`, `stream`, `store`, `reasoning`, `text`, `tools`, `tool_choice`, `parallel_tool_calls`, and `include`. A string input is wrapped as a user message; an explicit system-role input becomes developer-role input. Other native input items are passed to OpenAI for validation and model/policy admission.

Upstream `store` is always false and `stream` is always true, independently of client-facing streaming. `store:true`, `previous_response_id`, conversation persistence, background mode, metadata, token ceilings and sampling controls are unavailable. Required history must be included explicitly in every request.

Native tools may contain `namespace` groups with function/custom members, or `web_search` where model/account policy permits it. The gateway returns tool-call items but does not execute client functions. Flat function-tool definitions, image generation, file search, Code Interpreter, native computer use, hosted MCP and tool search are refused.

Files can be supplied as native model-compatible inputs; this gateway does not implement a Files upload endpoint. Audio/video APIs, embeddings, transcription and WebSockets are outside this version's contract.

The upstream preview constraints are documented in [OpenAI's preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).

## Outputs, errors, and benchmark interpretation

A nonstreaming Chat result reports the actual completed model, its text/refusal and supplied token usage. Initial Chat stream chunks may name the requested alias; completed finish/usage chunks report the actual served model snapshot. Native output retains the upstream response object/events. A different model family is a failure, not a successful substituted run. Usage remains unknown when not supplied. Catalog creation time and unknown Chat creation time use `0` as a compatibility placeholder.

Streaming clients must consume through the terminal result. HTTP `200` and partial text do not establish success: a quota or permission error may arrive after streaming begins. On failure, an error is emitted and no successful finish chunk appears. Native clients must see `response.completed`; Chat clients must see a successful finish marker and no error. The OpenAI SDK's stream iterator surfaces error payloads as exceptions.

Upstream responses with an absent `Content-Type` header are admitted to the SSE parser because the live plan route has returned valid streams that way. A supplied non-SSE media type is refused. This does not relax completion, model, schema or error checks.

If the upstream terminal response leaves `output` empty, the gateway fills it from that request's finalized `response.output_item.done` events before schema validation. Native terminal events/objects and Chat results therefore contain the finished answer. The gateway does not synthesize completion from partial deltas or an item finishing by itself; terminal completion, ordered item coverage and the original validation rules still apply.

Local JSON/schema validation is performed at completion, so streamed JSON fragments are provisional until the final marker. Invalid JSON, schema mismatch, refusal, interrupted output and quota failures should remain distinct benchmark outcomes. The gateway never attempts an additional model call to fix them.

`x-gateway-request-id` identifies an admitted API call; `x-upstream-request-id` is supplied on errors when known. `X-Title` labels a client in the metadata journal. HTTP retry hints are preserved. Keep SDK automatic retries off (`max_retries=0`) and decide benchmark retry policy explicitly.

Chat output sets `provider:"chatgpt_plan"`, without fabricating OpenRouter prices or BYOK claims. Completed native `web_search_call` items supply `usage.server_tool_use_details.web_search_requests`; this counts completed tool invocations, not the search backend's individual HTTP requests. Providing a search tool without evidence of its execution never invents a call. Native URL citations are translated into Chat's `url_citation` object shape. Usage chunks include the same tool evidence.

OpenAI inference can reuse a same-account catalogue up to five minutes old during a temporary model-list outage. Explicit `GET /models` remains fresh. A model-list token-expired rejection can renew eligible credentials and retry that GET once; this never substitutes models. Inference has the same bounded recovery only for HTTP `401 token_expired` before streaming: eligible renewal, then one identical request with a changed token. Invalid keys and other authentication errors fail immediately; second rejections, renewal failures and errors inside a started stream are never replayed. Concurrent expired-token rejections share the existing serialized refresh and remain on the same account. Preflight failures are recorded separately as `preflight_failed`.

The model catalog contains `access_verified:false`: access is confirmed only by a completed real inference for the selected model/account. The journal is a diagnostic record, not a billing ledger. Missing usage is `null`, never fabricated as zero, and no cash price is estimated. Account credits remain controlled by ChatGPT usage settings, even though there is no API-key billing fallback.

See [models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) and [errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery) for the official admission and terminal-error contracts.
