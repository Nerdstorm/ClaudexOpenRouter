# Project guidance

- This gateway is standalone. Do not import another application's code or embed recipe-specific prompts.
- Use the documented Sign in with ChatGPT public-client flow and public Responses API. Never read desktop/browser credentials or call private ChatGPT backend endpoints.
- Claude uses the official Anthropic API with an explicitly configured API key, behind the same OpenRouter-compatible Chat interface. Keep vendor integration out of client applications. Included Claude credits and any purchased balance are controlled by Claude Console; never claim to verify a credit balance or change billing routes automatically.
- Preserve caller cache markers and prompt ordering. The optional system-cache default may mark only the end of leading system/developer context, never infer boundaries inside application text. Preserve original JSON schemas for local validation when using Anthropic's documented schema transformation.
- Keep account identity, issued client ID and token set together. Validate OAuth state, PKCE, signed identity and granted plan scopes. Refresh rotating credentials serially and write them atomically with owner-only permissions.
- Never log credentials, authorization URLs, prompts, response bodies or schema contents. The request journal records identifiers, hashes, settings, status and usage only.
- Preserve exact model selection and fail explicitly on unsupported parameters, missing grants, exhausted limits and interrupted streams. Do not add model or billing fallbacks.
- Keep configuration, credential storage, OAuth, protocol translation and upstream transport separate and test their boundaries.
- The server binds to loopback by default and requires its own local bearer key. Authenticate management actions too. OAuth callback access logs must remain disabled.
- Keep UI presentation in external stylesheets if a UI is added; no inline styles or CSS-in-JS.
- Run `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest` after changes. Update README and compatibility documentation alongside protocol changes.
- Use Workstate first for project lookup and prior decisions; record useful outcomes when recording is enabled. Use repo name `chatgpt-plan-gateway` for ledger entries. Follow the user's current Workstate guidance.
