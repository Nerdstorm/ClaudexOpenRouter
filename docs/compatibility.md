# Compatibility contract (v0.1)

This is a local adapter for a documented ChatGPT-plan Responses route. OpenAI/OpenRouter-shaped paths and supported fields are compatible; complete provider/API parity is not claimed. Unsupported non-null parameters produce a client error before inference.

## Routes

All routes in the following table require the local bearer key.

| Route | Behaviour |
| --- | --- |
| `GET /v1/models` or `/api/v1/models` | Active account catalog, exact `openai/<slug>` IDs, no fabricated prices or access guarantee |
| `POST /v1/chat/completions` or `/api/v1/chat/completions` | Text/image-input message translation; JSON object or Chat SSE output |
| `POST /v1/responses` or `/api/v1/responses` | Native supported Responses body; object or native SSE events |
| `GET /gateway/capabilities` | Gateway capability summary, distinct from real model entitlement |
| `GET /gateway/status`, `/gateway/accounts`, `/gateway/requests` | Connection information and admitted request metadata |
| `POST /gateway/login`, `/gateway/accounts/select`, `/gateway/logout` | Authorise, select or end a connection |
| `POST /v1/images/generations`, `/v1/images/edits`, `/api/v1/images` | Explicit `400 unsupported_capability`; no upstream request |

`GET /healthz` and `/` are public local service information. `/auth/callback` is protected by one-time OAuth state/nonce/PKCE instead of a client bearer. Callback stylesheet assets are public. Other paths are outside the contract.

## Chat Completions input

- `model`: exact catalog slug, with optional `openai/` prefix. Other providers, automatic model choice and fallback lists are unavailable.
- `messages`: nonempty array of `system`, `developer`, `user` and `assistant` messages. Context comes only from this request. System messages become developer messages because the plan route rejects explicit system items.
- `content`: a string or array of text parts; user images use `image_url` with an HTTPS URL or `data:image/` URL. Image detail can be `auto`, `low`, `high`, or `original`. Availability depends on the selected model. Assistant image and refusal history are outside this version's translator.
- `response_format`: `text`, `json_object`, or named `json_schema`. The JSON schema is checked before sending; local fragment references are permitted, external references are rejected. At completion, JSON is parsed and schema-validated locally. Model refusals are preserved.
- `reasoning_effort`, or `reasoning` with `effort`/`summary`: passed through after validation. Efforts are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`; the selected model may reject otherwise valid settings. Both forms together are refused.
- `stream`: boolean, default false. `stream_options.include_usage` requests a final usage chunk.
- `n`: exactly `1`.
- `tools`: no Chat function tools in this version. Empty tools or `tool_choice:"none"` are accepted. Use supported native Responses namespaces for tool definitions.
- `provider`: `only:["openai"]`, `allow_fallbacks:false` and `require_parameters` are accepted compatibility preferences. `data_collection:"allow"` is accepted; `"deny"` is refused because this gateway cannot attest OpenRouter's data-policy contract. Manage the connected account's ChatGPT data controls directly.

Sampling/ceiling controls such as `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `seed`, penalties and stop sequences are refused. They are not silently discarded, replaced with defaults, or recorded as if applied.

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

The model catalog contains `access_verified:false`: access is confirmed only by a completed real inference for the selected model/account. The journal is a diagnostic record, not a billing ledger. Missing usage is `null`, never fabricated as zero, and no cash price is estimated. Account credits remain controlled by ChatGPT usage settings, even though there is no API-key billing fallback.

See [models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) and [errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery) for the official admission and terminal-error contracts.
