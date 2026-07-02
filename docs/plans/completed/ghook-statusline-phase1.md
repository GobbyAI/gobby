# ghook Statusline Phase 1

## Scope

This plan covers only the Rust-side `gobby-cli` work for Claude Code
statusline handling. Gobby/Python installer migration, rollback bake, and
eventual Python handler deletion are tracked outside this repository.

## Implemented

- `ghook --gobby-owned --cli=claude --type=statusline` is routed before the
  generic hook enqueue/POST flow.
- The statusline path writes downstream stdout bytes directly and does not
  use the shared JSON hook emitter.
- The handler extracts the Claude statusline usage payload and posts it to
  `/api/sessions/statusline` with best-effort failure semantics.
- The crate version is bumped to `0.3.1` so the Gobby installer can probe for
  statusline-capable `ghook` binaries before switching statusline config.

## Parity Contract

Golden fixtures live in `crates/ghook/tests/fixtures/statusline/`:

- `full-input.json` -> `full-payload.json`
- `defaults-input.json` -> `defaults-payload.json`

These fixtures capture the stable wire contract from Claude statusline stdin
to the daemon payload. Missing or falsy `session_id` produces no daemon POST.
Missing `cost`, `model`, or `context_window` fields use the same defaults as
the legacy Python middleware.

## Validation

Required checks before release:

- `cargo test -p gobby-hooks`
- `cargo test -p gobby-hooks --no-default-features`
- `cargo clippy -p gobby-hooks --all-targets -- -D warnings`
- `cargo build --release -p gobby-hooks`

Local install is intentionally out of scope. Do not copy the built artifact to
`~/.gobby/bin/ghook` as part of this phase.
