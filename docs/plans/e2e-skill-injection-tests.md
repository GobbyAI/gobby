# Provider Parity E2E Matrix

Date: 2026-04-19

## Summary

Add an automated parity matrix for `claude`, `codex`, `gemini`, and `qwen` covering both:

- web chat via app-server and websocket-managed sessions
- terminal/native-hook sessions validated from the web app

Use a mixed strategy:

- deterministic mocked Playwright coverage for baseline UI and restore behavior
- gated live-provider Playwright coverage for real daemon and provider validation

## Shared Harness

- Add shared helpers in `web/tests` for provider/model discovery, fresh session bootstrap, transcript polling, provider filtering, and cleanup.
- Split helpers by surface:
  - web chat helpers for browser interactions
  - terminal helpers for observed or attached session validation
- Gate live runs behind env vars so mocked coverage stays unconditional and live-provider coverage only runs when providers are available.

## Web Chat Matrix

Add a provider-parameterized live Playwright spec for `claude`, `codex`, `gemini`, and `qwen` that validates:

- a fresh web chat can send a prompt and receive an assistant response
- reload or reconnect resumes the same main-chat web-chat session
- provider and model selection stay stable across the resumed session

Keep plan-mode or approval-flow coverage as narrower follow-on specs rather than forcing those behaviors into the baseline provider matrix.

## Terminal Matrix

Add a provider-parameterized Playwright path for terminal-backed sessions validated through the web UI:

- start or locate a fresh provider-backed terminal session
- confirm it appears in the activity and session surfaces with the correct source
- confirm the web UI can observe or attach without misclassifying it as a web chat
- confirm reload restores watched or attached terminal state, or fails clear without auto-creating a replacement web chat

## Scope Of Assertions

Web-chat matrix assertions:

- correct provider source and selected model
- assistant response arrives without a generic generation error
- persisted main-chat session survives reload and remains active

Terminal matrix assertions:

- terminal session is sourced from the intended provider
- watched or attached terminal state survives reload without being replaced by a web chat
- daemon reconnect or startup races fail clearly without silently creating a new web-chat session

Skill-injection depth checks should stay in a separate targeted battery. This matrix is for end-to-end provider and surface parity.

## Test Files

- Keep deterministic mocked tests in `web/tests` for restore and session-classification behavior.
- Add live specs along these lines:
  - `provider-web-chat-matrix-live.spec.ts`
  - `provider-terminal-matrix-live.spec.ts`
- Refactor existing live Gemini or Codex specs into shared helpers where useful, but keep narrow provider-specific regressions as standalone tests if they still add coverage.

## Test Plan

- Mocked Playwright:
  - main-chat restore prefers the persisted web-chat session
  - watched or attached terminal state restores correctly and does not become a web chat
  - stale or terminal session restore fails clear without auto-creating a replacement web chat
- Live Playwright web-chat matrix:
  - all four providers can start a fresh web chat and answer
  - reload resumes the same web-chat session for all four providers
- Live Playwright terminal matrix:
  - all four providers produce observable terminal/native sessions
  - the web UI can restore watched or attached terminal state after reload
  - daemon-not-ready or reconnect races fail gracefully

## Assumptions

- “Both use cases” means `web chat` and `terminal/native-hook` coverage for each provider.
- Playwright is the primary E2E tool even for terminal validation; terminal behavior is asserted through the web app and daemon APIs rather than a separate browserless harness.
- Live-provider tests may skip per provider when `/api/providers/models` reports unavailable, but mocked restore and classification coverage should remain unconditional.
