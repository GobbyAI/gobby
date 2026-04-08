## Decouple Title Synthesis From CLI Account State

### Summary
Keep session title synthesis as a feature-routed LLM capability, but harden it across Claude, Gemini, and Codex so provider outages, quota exhaustion, or account switches do not break the chat flow. Preserve configurability through [`SessionTitleConfig`](/Users/josh/Projects/gobby/src/gobby/config/sessions.py#L179), and make it practical to point titles at a local model later without more code changes.

### Implementation Changes
- Standardize title synthesis on feature-level provider routing everywhere it is used, using the existing `session_title.provider`, `session_title.model`, and `session_title.timeout` fields instead of any implicit default-provider fallback.
- Audit all internal title-generation paths, starting with [`analytics.py`](/Users/josh/Projects/gobby/src/gobby/servers/routes/sessions/analytics.py) and [`_actions.py`](/Users/josh/Projects/gobby/src/gobby/mcp_proxy/tools/sessions/_actions.py), so they share one failure policy and one provider-selection path.
- Treat title synthesis as non-critical background enrichment:
  - timeouts, quota errors, auth/account errors, and transient provider failures should log compact warnings
  - the session/chat request should continue normally
  - no raw stack trace or user-facing failure dump should be emitted for title-only failures
- Add provider-error classification around `generate_text(...)` calls so known upstream conditions like usage exhaustion, auth failure, or transport timeout are downgraded to soft failure rather than generic 500s.
- Ensure this soft-failure behavior is provider-agnostic, so the same logic applies whether `session_title.provider` points to `claude`, `gemini`, `codex`, or a future local provider.
- Keep the hook/approval transport separate from title synthesis; do not assume every hook issue is quota-related unless reproduced after the account switch. If hook-response hardening is included, scope it to "no user-visible stack dump on malformed internal hook response," not to provider routing.

### Interfaces
- No schema change required: reuse existing [`SessionTitleConfig`](/Users/josh/Projects/gobby/src/gobby/config/sessions.py#L179).
- Update title synthesis endpoints/tools to return soft-failure results instead of HTTP 500 for provider/account/quota/timeout issues.
- Document that `session_title.provider` and `session_title.model` can be pointed at a local model to avoid remote-account dependency for this feature.

### Test Plan
- Add route/tool tests showing title synthesis uses `session_title.provider` and `session_title.model`, not an implicit default provider.
- Add tests for timeout, quota/auth/provider exceptions, and generic upstream failures, asserting:
  - no uncaught stack trace behavior
  - no 500 for title-only failures
  - no unintended session title mutation on failure
- Add one provider-routing test per CLI-backed provider class where practical, focused on "feature config selects provider" rather than end-to-end quota simulation.
- Add a regression test proving a successful title call still updates the session title as before.
- If hook hardening is included in this pass, add a guard test that malformed internal hook translation does not surface a raw callback crash to the user.

### Assumptions
- Session title synthesis is optional metadata, not part of the critical chat path.
- The existing feature-routing config is the right abstraction; the code should not hardwire Claude for titles.
- Local-model routing for titles is a configuration choice we want to preserve, not a forced product default in this pass.
