# Compatibility contract (v0.2)

This is a local adapter for the documented ChatGPT-plan Responses route and official Anthropic Messages API. OpenAI/OpenRouter-shaped paths and supported fields are compatible; complete provider/API parity is not claimed. Unsupported non-null parameters produce a client error before inference. Provider selection follows the exact model id, with no model or provider fallback.

## Routes

All routes in the following table require the local bearer key.

| Route | Behaviour |
| --- | --- |
| `GET /v1/models` or `/api/v1/models` | Real configured-provider catalogues; exact OpenRouter ids, no fabricated prices or access guarantee |
| `POST /v1/chat/completions` or `/api/v1/chat/completions` | OpenAI text/image input or Claude text; JSON object or Chat SSE output |
| `POST /v1/responses` or `/api/v1/responses` | OpenAI-only supported Responses body; object or native SSE events |
| `GET /gateway/capabilities` | Gateway capability summary, distinct from real model entitlement |
| `GET /gateway/status`, `/gateway/accounts`, `/gateway/requests` | Connection information and admitted request metadata |
| `POST /gateway/login`, `/gateway/accounts/select`, `/gateway/logout` | Authorise, select or end a connection |
| `POST /v1/images/generations`, `/v1/images/edits`, `/api/v1/images` | Explicit `400 unsupported_capability`; no upstream request |

`GET /healthz` and `/` are public local service information. `/auth/callback` is protected by one-time OAuth state/nonce/PKCE instead of a client bearer. Callback stylesheet assets are public. Other paths are outside the contract.

Local containers on Docker Desktop or OrbStack may use `http://host.docker.internal:8317/api/v1`.
That hostname is admitted alongside the loopback names; the server still binds to `127.0.0.1`,
requires the local bearer key and rejects foreign browser origins.

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

Claude completion uses the ordinary Chat object/chunk shape, actual OpenRouter model id and `provider:"anthropic"`. Headers disclose `x-gateway-provider:anthropic`, `x-gateway-billing-source:anthropic_api` and `x-gateway-data-controls:claude_console`. Usage normalizes native `cache_read_input_tokens` and `cache_creation_input_tokens` into `prompt_tokens_details.cached_tokens` and `cache_write_tokens`; total input is uncached + read + write. Original native usage and cache-write lifetime breakdowns are retained in journal usage; unknown counts remain null. Time to first text and total duration are recorded separately. No unverified cash price, balance or included-credit guarantee is fabricated.

The same global concurrency pool limits both providers combined. Anthropic SDK retries are disabled. A validated `message_stop`, completed blocks, correct model, accepted stop reason and original-schema validation are required before success. SSE errors whose transport status is HTTP 200 are translated into failure statuses for nonstreaming clients; streaming clients receive an error without a successful finish marker. `max_tokens`, server-side fallback and tool blocks cannot masquerade as successful answers. Closing or timing out a stream releases its slot and records the outcome.

`provider.only:["anthropic"]`, `allow_fallbacks:false` and `require_parameters` are validated. `provider.data_collection:"deny"` remains refused by default because the gateway cannot attest OpenRouter's policy contract. An explicit `CHATGPT_GATEWAY_ACCEPT_ANTHROPIC_DATA_CONTROLS=1` exception accepts Claude Console controls instead and marks affected responses and journal entries. This is separate from the ChatGPT exception; enabling one never enables the other. The gateway does not inspect or change Console training/retention or billing settings.

### Explicit structured-output modes

`CHATGPT_GATEWAY_ANTHROPIC_STRUCTURED_OUTPUT=native` is the default. It uses Anthropic's
schema transformation and native grammar, then validates against the client's original schema.
Known grammar-size rejections return `400 schema_compilation_limit`, with a safe explanation
and upstream request ID; native error text is not copied into the response or journal.

An operator may select `validated_json` when the native compiler cannot support the contract.
The Chat `response_format` and completed response shape stay the same. The gateway appends a
separate block to the final user message containing a JSON-only instruction and the complete
original schema, preserving property insertion order and all original caller blocks/cache
markers. It sends no native `output_config.format`. Before success, the unchanged original
schema validates the completed JSON, including required fields, additional-property rules,
enum values, types and constraints. Invalid JSON, refusals, truncation and incomplete streams
retain their existing outcomes; failures cause no gateway retry or model fallback.

Capabilities report `providers.anthropic.structured_output_mode`. Schema requests disclose
`x-gateway-structured-output-mode: native|validated_json`, and both started/terminal journal
events record `structured_output_mode`. Text requests leave the mode null. The format block
is model context and affects tokens and benchmark comparability; this is not native grammar
enforcement. Mode selection is fixed at server startup; a native failure never silently
switches to validated JSON. The OpenAI route is unaffected.

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

The model catalog contains `access_verified:false`: access is confirmed only by a completed real inference for the selected model/account. The journal is a diagnostic record, not a billing ledger. Missing usage is `null`, never fabricated as zero, and no cash price is estimated. Account credits remain controlled by ChatGPT usage settings, even though there is no API-key billing fallback.

See [models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) and [errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery) for the official admission and terminal-error contracts.
