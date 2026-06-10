# JSON Security And Secrets

## Secrets

- Never commit raw tokens, API keys, private keys, passwords, session cookies,
  credentials, or sensitive endpoints in JSON.
- Prefer environment references, secret-manager references, CI secret contexts,
  or local ignored files documented by the repo.
- Redact fixtures and examples. Test data should preserve shape without using
  live credentials.

## Untrusted Input

- Treat JSON from users, APIs, files, queues, and webhooks as untrusted until
  parsed with limits and validated against a schema or boundary model.
- Limit payload size, nesting depth, array lengths, and string lengths when the
  parser or framework does not already enforce them.
- Reject or sanitize keys that can trigger prototype pollution or unsafe merges,
  such as `__proto__`, `prototype`, and `constructor` in JavaScript ecosystems.

## Package And Tool Config

- Review `package.json` scripts, registry URLs, lifecycle hooks, dependency
  sources, browser extension manifests, tool plugins, and CI-related config for
  privilege or supply-chain changes.
- Preserve lockfile integrity fields and package-manager provenance.
- Pin or verify external references where the platform supports immutable
  versions.

## Logging And Diagnostics

- Do not log entire JSON payloads when they may contain credentials, PII, or
  internal endpoints.
- Prefer path-specific validation errors and redacted summaries.
