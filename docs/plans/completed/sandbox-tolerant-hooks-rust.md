# Hand-off to the daemon-side agent — integrated with Rust migration epic

## Context

We're the Rust side in `gobby-cli`. A parallel agent is working the Python
daemon side in `gobby`. Shared brief: `docs/plans/sandbox-tolerant-hooks.md`.

Two things reshape the original plan:

1. **Hooks are registered globally** in `~/.claude/settings.json`,
   `~/.codex/config.toml`, etc. — one command string shared across all
   projects. Implication: the registered command MUST be an absolute path
   (`~/.gobby/bin/ghook` expanded). Hook *invocation* cwd is set by the
   host CLI at spawn to whatever dir the user launched from — walk-up
   finds the current project if one exists, else project-id header is
   omitted. Matches dispatcher behavior.

2. **The `rust-migration-epic.md` already plans the shared crate.** It's
   `gobby-core`, not `gobby-common`. R2-01 scaffolds it; R2-02/R2-04/R2-05
   extract exactly what ghook needs (bootstrap resolution, project root,
   project metadata). The epic's Hook Integration section (`:151-157`)
   anticipates a Rust `gobby-hooks` binary and permits it against the stable
   public daemon contract. Daemon-side hook *execution* migration is
   Phase 6 (R6-04 through R6-07) — out of scope here. ghook is client-side
   only; daemon stays Python.

`hook_dispatcher.py` is long-stable code, so parity is achievable against
a fixed reference.

## Approach

Ship four PRs in sequence (gobby-cli), satisfying the epic's "one primary
boundary per atomic item" standard. Each maps to an R-item from the epic.

### PR 1 — `gobby-core` scaffold + project helpers (R2-01 + R2-04 + R2-05)

- New crate `crates/gcore/` — workspace lib, no binaries.
- Move `find_project_root` and `read_project_id` from `gcode/src/project.rs`
  into `gobby-core::project`. Expose as public API.
- `gcode` keeps its local copy temporarily (R2-08 is the later migration
  item). No behavior change to gcode. No tests move.
- Cargo.toml: add `gobby-core` to workspace members; default profile.

### PR 2 — bootstrap + daemon-URL helpers in `gobby-core` (R2-02 + R2-03)

- Port `hook_dispatcher.py:145-175` port-resolution logic: read
  `~/.gobby/bootstrap.yaml`, extract `daemon_port`, default `60887` on
  missing/malformed. Use `serde_yaml = "0.9"` (matches gsqz/gcode workspace
  consistency).
- `dirs::home_dir()` for `~` expansion — gsqz config resolver already uses
  this pattern; copy.
- No consumers yet — ghook is the first in PR 3.

### PR 3 — `ghook` crate (this is the sandbox-tolerant-hooks implementation)

- New `crates/ghook/` binary. Depends on `gobby-core`.
- CLI surface per the plan:
  `ghook --gobby-owned --cli=<...> --type=<...> [--critical] [--detach]`,
  `ghook --diagnose --cli=<...> --type=<...>`, `ghook --version`.
- Behavior mirrors `hook_dispatcher.py` where possible:
  - stdin-JSON payload (line `:637-638`).
  - **Terminal-context enrichment** (line `:640-643`): if `hook_type` is in
    the per-CLI `terminal_context_hooks` set (from `CLIConfig` `:61`), call
    the ported equivalent of `get_terminal_context()` (`:181-223`) and
    `input_data.setdefault("terminal_context", ...)` before envelope
    build. This is load-bearing — daemon uses it to reconcile
    spawned-terminal agents (`hooks/event_handlers/_session_start.py:191`).
    Capture: `parent_pid`, `tty`, `tmux_pane`, `tmux_socket_path`,
    `term_program`, and env vars `GOBBY_SESSION_ID`, `GOBBY_PARENT_SESSION_ID`,
    `GOBBY_AGENT_RUN_ID`, `GOBBY_PROJECT_ID`, `GOBBY_WORKFLOW_NAME`.
    **Sharp edge** (`:205`): only emit `TMUX_PANE` when `TMUX` is also
    set — parent/child tmux-pane confusion breaks `kill_agent`.
  - `X-Gobby-Project-Id` from `_find_project_config(cwd).id` (line `:657`).
    Omit header when missing — never empty string. Walk-up **must happen
    before detach** — chdir/fd semantics inside a detached Rust process
    surprise on macOS.
  - `X-Gobby-Session-Id` from `input_data["session_id"]` (line `:659`).
    Same omit-on-missing semantics.
  - Detach: single `setsid` (matches dispatcher's `start_new_session=True`
    at `:697`) on Unix; `FreeConsole()` on Windows. `DETACHED_PROCESS` and
    `CREATE_NEW_PROCESS_GROUP` are `CreateProcess` parent-side flags and
    cannot be self-applied from the already-spawned child — `FreeConsole`
    is the correct post-spawn analog (release the inherited console
    handle).
- Upgrades beyond dispatcher:
  - **Enqueue-first**: write envelope to
    `~/.gobby/hooks/inbox/<p>-<ts13>-<uuid>.json.tmp` → `fsync` → rename.
    Then POST. On 2xx, delete. Prefix `c`/`n` for critical/non-critical;
    `ts13` is zero-padded 13-digit millis for lex-sort stability.
  - **Unparseable stdin**: dispatcher drops on floor (`:647-651`). Ghook
    writes **directly** to
    `~/.gobby/hooks/inbox/quarantine/<p>-<ts13>-<uuid>.json` with a
    `.meta.json` sidecar: `{reason: "malformed_stdin", json_error: "...",
    stdin_bytes_b64: "..."}`. Drain never replays from quarantine — it
    surfaces via `gobby status` / logs. Keeps the normal envelope schema
    clean (no `malformed` branch in schema v1). Exit 0 (non-critical) or 2
    (critical).
- `serde_yaml = "0.9"`, `ureq` matching gsqz's feature set (no TLS; loopback
  only), `clap` derive, `anyhow`.
- Schema files in `crates/ghook/schemas/`:
  - `inbox-envelope.v1.schema.json`
  - `diagnose-output.v1.schema.json`
  - `cargo test` validates serialized output against both.
- `~/.gobby/bin/.ghook-compatibility` written by `ghook --version` on first
  run: `{ "schema_version": 1, "ghook_version": "0.1.0" }`.
- CI: `.github/workflows/release-ghook.yml` mirrors `release-gcode.yml` +
  binary-specific tag prefix (`gobby-hooks-v<semver>`, per commit `bf9eb40`).
  Targets: `darwin-arm64`, `darwin-x86_64`, `linux-x86_64`, `linux-arm64`,
  `windows-x86_64` (mirrors `install_setup.py:249-250` triples for
  gsqz/gcode). Publish to crates.io as `gobby-hooks` so
  `cargo-binstall` / `cargo install` fallbacks work in the daemon's
  `_install_ghook()`.
- **Publish-order constraint.** `ghook` depends on `gobby-core`, so
  `gobby-core` must publish to crates.io before any `ghook` release can
  complete — crates.io rejects uploads with path-only dependencies. PR 1
  adds a sibling `.github/workflows/release-gcore.yml` that
  publishes `gobby-core`, and `crates/ghook/Cargo.toml` declares its
  `gobby-core` dep with both `version = "0.x.y"` and
  `path = "../gcore"` — crates.io consumes the `version`, workspace
  builds honor the `path`. The first `gobby-hooks-v<semver>` tag cannot
  ship until the matching `gobby-core` version is live on the registry.

### PR 4 — migrate `gcode` to `gobby-core` (R2-08)

- Delete `gcode/src/project.rs`'s `find_project_root` + `read_project_id`.
- Update `gcode/src/config.rs` (line `:195`, `detect_project_root`) and
  any other call sites to import from `gobby_core::project`.
- Clean `cargo test -p gobby-code` pass.
- `gsqz` migration (R2-09) is a separate follow-up — gsqz doesn't currently
  use walk-up helpers, so it waits for R2-06 (HTTP client utilities) in a
  later PR.

## What we (Rust side) commit to — frozen contracts

### Crate & distribution

- Cargo package names: `gobby-core` (lib), `gobby-hooks` (bin `ghook`).
- Binary install: `~/.gobby/bin/ghook` + stamp `~/.gobby/bin/.ghook-version`.
- Crates.io: `gobby-core` and `gobby-hooks` both published. `gobby-core`
  must publish first — crates.io rejects packages with path-only
  dependencies, and `ghook` depends on `gobby-core`. `ghook`'s
  `Cargo.toml` declares `gobby-core = { version = "0.x.y", path = "..." }`
  so workspace builds stay path-backed while registry builds resolve the
  version.
- GitHub release tag: `gobby-hooks-v<semver>`. Tarball:
  `ghook-<target-triple>.tar.gz`.
- Windows: `ghook.exe`.

### Envelope schema v1 (committed to `crates/ghook/schemas/`)

```json
{
  "schema_version": 1,
  "enqueued_at": "<iso8601>",
  "critical": false,
  "hook_type": "session-start",
  "input_data": { "...": "original stdin payload" },
  "source": "claude",
  "headers": {
    "X-Gobby-Project-Id": "...",
    "X-Gobby-Session-Id": "..."
  }
}
```

Omitted headers are absent from the `headers` object entirely — no
empty-string values.

### Quarantine sidecar

When the drain gives up on an envelope, writes `<name>.meta.json` next to
the quarantined envelope: `{ attempt_count, first_seen, last_error,
last_attempt }`. Agreed with daemon agent.

### Exit codes

- `0` — success OR non-critical failure (enqueued, will replay).
- `2` — critical failure (enqueued, will replay — signals the host CLI).

## All contract questions resolved

- **Q1.1** → stdin-only for headers. Env vars enter as terminal_context
  *data*, not as headers. Parity with dispatcher `:659`. Empty stdin
  normalizes to `input_data = {}` (ghook); dispatcher exits non-zero on
  empty (`hook_dispatcher.py:677`). Documented transition-window
  divergence — exit code is still governed by `--critical` alone, no
  silent drop.
- **Q1.2** → omit header on missing, mirror `:657-661`.
- **Q1.3** → write directly to `inbox/quarantine/` with `.meta.json`
  sidecar. No `malformed` flag in envelope schema. Drain never replays.
- **Q1.4** mechanism → port `_find_project_config` verbatim (`:527`).
- **Q1.4** assumption → cwd-inheritance confirmed across all four CLIs.
  Sharp edge: strict sandbox FS-read denials on parent dirs → walk-up
  returns None → header absent → daemon middleware treats as "no project
  context" and executes hook un-scoped. Correct behavior, no header
  fabrication.
- **Q2** (current.json) → moot, dropped.
- **Q3** (Windows) → in scope; mirror `install_setup.py:249-250` triples.
- **Q4** (inbox naming) → `<p>-<ts13>-<uuid>.json`, lex sort, ignore `*.tmp`.
- **Q5a** → bare 40-byte hex SHA + newline in `gobby/schemas/SOURCE_COMMIT`.
  Sync metadata lives in git log; file list implicit from `ls`.
- **Q5b** → `gobby-cli` is public (confirmed via `install_setup.py:234,237,538,541`
  fetching release URLs with no auth). CI uses
  `raw.githubusercontent.com/GobbyAI/gobby-cli/<SOURCE_COMMIT>/schemas/*`.
- **Q6** (serde_yaml) → `0.9`, workspace consistency.
- **Detach** → single `setsid` (Unix, matches `:697`) / `FreeConsole()`
  (Windows — post-spawn release of the inherited console; `DETACHED_PROCESS`
  and `CREATE_NEW_PROCESS_GROUP` are `CreateProcess` parent-side flags, not
  self-applicable from the child). No double-fork.
- **Terminal context** → ported from `get_terminal_context()` `:181-223`
  with the `TMUX`/`TMUX_PANE` inheritance rule at `:205`. Gated by
  per-CLI `terminal_context_hooks` set from `CLIConfig` `:61`.
- **POST body shape** → daemon's `/api/hooks/execute` endpoint
  (`servers/routes/mcp/hooks.py:258`) reads `hook_type`, `input_data`,
  and `source` from the top level and hands the full payload through to
  adapters, which only read the fields they need. Both the legacy
  bare-body shape `{hook_type, input_data, source}` and the schema-v1
  envelope `{schema_version, enqueued_at, critical, hook_type,
  input_data, source, headers}` are accepted — envelope extras fall
  through as silent metadata. ghook sends envelope shape; the Python
  plan's §2.8 adds a handler test that pins this tolerance so a future
  refactor can't silently regress envelope-aware clients.

Daemon agent has signed off. Green light to proceed with PRs 1–4.

## Critical files (this repo)

**New (PR 1):**
- `crates/gcore/Cargo.toml`
- `crates/gcore/src/lib.rs`
- `crates/gcore/src/project.rs` — moved from gcode
- Root `Cargo.toml` — add `gobby-core` to workspace members

**New (PR 2):**
- `crates/gcore/src/bootstrap.rs`
- `crates/gcore/src/daemon_url.rs`

**New (PR 3):**
- `crates/ghook/Cargo.toml`
- `crates/ghook/src/main.rs` — clap entry
- `crates/ghook/src/envelope.rs`
- `crates/ghook/src/transport.rs`
- `crates/ghook/src/diagnose.rs`
- `crates/ghook/src/detach.rs` — `#[cfg(unix)]` / `#[cfg(windows)]` split
- `crates/ghook/src/terminal_context.rs` — Rust port of
  `hook_dispatcher.py:181-223` + per-CLI `terminal_context_hooks` gate
- `crates/ghook/schemas/inbox-envelope.v1.schema.json`
- `crates/ghook/schemas/diagnose-output.v1.schema.json`
- `.github/workflows/release-ghook.yml`
- Root `Cargo.toml` — add `ghook` to workspace + `opt-level="z"` override

**Modified (PR 4):**
- `crates/gcode/src/project.rs` — remove migrated helpers
- `crates/gcode/src/config.rs:195` — update `detect_project_root` imports
- Any other gcode call sites

## Verification

- `cargo test --workspace` green at each PR boundary.
- `cargo clippy --workspace -- -D warnings` green.
- `cargo build --release -p gobby-hooks` produces binary in
  `gsqz`/`gloc` size ballpark (`opt-level="z"` target).
- PR 3 manual:
  - `echo '{"session_id":"test"}' | target/release/ghook --gobby-owned
    --cli=claude --type=session-start` with daemon up — POST succeeds,
    no inbox file lingers.
  - Same with daemon down — inbox file persists, exit 0 (non-critical) or
    2 (critical).
  - `ghook --diagnose --cli=codex --type=session-start` emits valid
    diagnose JSON.
  - `ghook --version` writes `.ghook-compatibility`.
- PR 4: `cargo test -p gobby-code` green; `detect_project_root` behavior
  unchanged.

## Not gated on 0.4.0

Per epic `:88-89`: Phase 2 low-risk extraction proceeds in parallel with
0.4.0 hardening. PR 1 and PR 2 are Phase 2 extractions. PR 3 (ghook) is
client-side against the stable `hook_dispatcher.py` contract, permitted by
the epic's Hook Integration section `:151-157`. None of this is Phase 3+
implementation work against churning surfaces.
