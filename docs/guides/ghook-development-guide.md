# ghook Development Guide

Technical internals for developers and agents working in the ghook codebase.

## Architecture Overview

```text
host AI CLI (Claude Code / Codex / Qwen / Droid / Grok / Agy)
  │  spawns: ghook --gobby-owned --cli=<c> --type=<t> [--detach]
  │  pipes:  stdin = hook payload (JSON object)
  ▼
main.rs::run_gobby_owned
  │
  ├─ planned_shutdown::should_skip_dispatch
  │     ── Stop only; fresh marker + unreachable health endpoint exits 0
  │
  ├─ project::find_project_root + read_project_id      (gobby-core)
  │     ── walk-up BEFORE any detach; sandbox-safe.
  │
  ├─ stdin → serde_json::from_slice
  │     ── on malformed: transport::quarantine_malformed → exit
  │
  ├─ cli_config::CliConfig::for_dispatch(cli)
  │     ── per-CLI critical-hook registry
  │
  ├─ dispatch::inject_machine_identity
  │     ── machine_id + os, or machine_id_error
  │
  ├─ terminal_context::inject  (if TMUX + valid TMUX_PANE are present)
  │
  ├─ envelope::Envelope::new
  │
  ├─ transport::enqueue_to(envelope, ~/.gobby/hooks/inbox/)
  │     ── atomic write: tmp → fsync → rename
  │
  ├─ detach::detach()  (if --detach: setsid on Unix)
  │
  └─ transport::post_and_cleanup
        │
        ├─ POST {daemon_url}/api/hooks/execute  (30s timeout)
        ├─ 2xx     → fs::remove_file(envelope) → ExitCode::SUCCESS
        └─ failure → leave envelope               → ExitCode::SUCCESS or 2
                    └─ planned Stop shutdown race may delete envelope + continue
                                                    (drain worker replays)
```

Spool-first ordering is load-bearing. The envelope is on disk before anything risky (network I/O, detach) happens, so the daemon's drain worker is the source of truth even if ghook dies mid-POST.

### Why a Separate Binary?

The original Python `hook_dispatcher.py` ran inside the daemon process. That made it sensitive to daemon downtime: hook → daemon socket → daemon process. ghook is a small standalone binary so:

1. It can run when the daemon is dead. Envelopes spool to disk.
2. It can survive sandbox FS-read denials that would crash an embedded interpreter.
3. Host CLIs can invoke it from any context (including detached sessions).
4. Failure modes are constrained: writing one file, sending one HTTP request.

## Module Map

`crates/ghook/src/`:

| Module | Responsibility |
|--------|----------------|
| `main.rs` | Arg parsing (clap), mode dispatch (`--gobby-owned`/`--diagnose`/`--version`), orchestrates the dispatch flow. |
| `cli_config.rs` | Per-CLI registry (claude/codex/qwen/droid/grok/agy) — which hooks are critical. Compile-time frozen. `grok` uses native snake_case hook names (`session_start`, `session_end`, `pre_compact`, `stop`); `agy` uses Antigravity's PascalCase hook names. |
| `envelope.rs` | `Envelope` struct + `SCHEMA_VERSION = 1`. Serializes to the inbox JSON shape. |
| `planned_shutdown.rs` | Stop-only planned shutdown markers, daemon health preflight, and post-enqueue daemon-death suppression. |
| `transport.rs` | Inbox path resolution, atomic write, enqueue, POST + cleanup, quarantine for malformed stdin. |
| `terminal_context.rs` | Captures parent PID, TTY, tmux pane/socket, `TERM_PROGRAM`, `GOBBY_*` env vars when `TMUX_PANE` is valid. Injects under `input_data.terminal_context`. |
| `diagnose.rs` | `--diagnose` mode — pure introspection, no I/O side effects. Returns `DiagnoseOutput` matching `schemas/diagnose-output.v2.schema.json`. |
| `detach.rs` | Unix `setsid(2)` / Windows `FreeConsole()` — best-effort detach from controlling TTY and process group. |

`crates/ghook/schemas/`:

| File | Validated against in |
|------|----------------------|
| `inbox-envelope.v1.schema.json` | `envelope::tests::envelope_validates_against_v1_schema` |
| `diagnose-output.v2.schema.json` | `diagnose::tests::diagnose_output_validates_against_v2_schema` |

## Envelope Schema (v1)

The envelope is what ghook writes to `~/.gobby/hooks/inbox/` and what the daemon's drain worker replays. Schema is frozen at v1 — consumers must reject unknown versions.

```rust
pub struct Envelope {
    pub schema_version: u32,            // const 1
    pub enqueued_at: String,            // RFC 3339 UTC
    pub critical: bool,
    pub hook_type: String,              // host-CLI-specific
    pub input_data: Value,              // stdin plus machine identity and optional tmux terminal_context
    pub source: String,                 // "claude" / "codex" / "qwen" / "droid" / "grok" / "agy" / passthrough
    pub headers: BTreeMap<String, String>,
}
```

### Field Semantics

| Field | Why It Exists |
|-------|---------------|
| `schema_version` | Forward-compat. Daemon rejects unknown versions rather than parsing partial data. |
| `enqueued_at` | Lets the drain worker compute hook latency and detect very-stale envelopes. |
| `critical` | Recorded so the daemon knows whether the host CLI was told this hook fail-closed. Influences alerting. |
| `hook_type` | Opaque — exact identifier the host CLI's hook system uses (`session-start`, `PreToolUse`, etc.). |
| `input_data` | Original stdin with dispatch-owned enrichment. `machine_id` + `os` are stamped from local Gobby machine identity, or `machine_id_error` is emitted when unavailable. When `TMUX` is set and `TMUX_PANE` matches `^%\d+$`, `terminal_context` is *injected* into the existing object (mirrors Python's `setdefault`) — never overwritten if already present. |
| `source` | Recognized CLI → canonical name from `CliConfig::source`. Unknown CLI → the `--cli` value verbatim, so future CLIs route correctly without code changes. |
| `headers` | Mirrors what ghook sent (or would have sent) on the POST. Omitted headers are absent keys; **empty-string values are never emitted** — this matches `hook_dispatcher.py:695-700` behavior and is enforced by the schema (`additionalProperties.minLength: 1`). |

### Standard Headers

| Header | When Present | Source |
|--------|--------------|--------|
| `X-Gobby-Project-Id` | Project root resolved AND `project.json` has an `id`/`project_id` field | `gobby_core::project::read_project_id` |
| `X-Gobby-Session-Id` | `input_data.session_id` is a non-empty string | `input_data["session_id"]` |

Both are inserted only when non-empty. The schema enforces `minLength: 1` on header values to match.

## Diagnose Output Schema (v2)

`--diagnose` returns a JSON object validated against `schemas/diagnose-output.v2.schema.json`. It runs the same config-resolution code paths as `--gobby-owned` but stops short of any I/O side effect.

```rust
pub struct DiagnoseOutput {
    pub schema_version: u32,                 // const 2
    pub ghook_version: &'static str,         // env!("CARGO_PKG_VERSION")
    pub cli: String,
    pub hook_type: String,
    pub source: Option<String>,              // null if cli not recognized
    pub critical: bool,                      // would this hook be critical for this CLI?
    pub terminal_context_enabled: bool,
    pub daemon_url: String,
    pub daemon_host: String,
    pub daemon_port: u16,
    pub project_root: Option<PathBuf>,
    pub project_id: Option<String>,
    pub terminal_context_preview: Option<Value>,  // populated when tmux pane context is valid
    pub cli_recognized: bool,
    pub install_method: Option<String>,           // from .ghook-install.json sidecar
    pub install_source_url: Option<String>,       // from .ghook-install.json sidecar
}
```

The `terminal_context_preview` field is the actual context that *would* be injected when the current process has `TMUX` and a valid `TMUX_PANE` — operators can inspect what the daemon will receive without sending a real hook.

`install_method` and `install_source_url` are sourced from the install-provenance sidecar described below. Both fields are `null` when no sidecar is present.

### Install-Provenance Sidecar Contract

The sidecar is the cross-tool boundary between *whoever installs `ghook`* and `ghook --diagnose`. It is the only mechanism by which a `ghook` binary learns how it got onto disk.

| Property | Value |
|----------|-------|
| Path | `<dirname of the running ghook binary>/.ghook-install.json` |
| Writer | The installer (e.g. the Gobby Python installer in `src/gobby/cli/install_setup.py`) — written atomically (temp + rename) every time the installer places a `ghook` binary |
| Reader | `ghook::diagnose::read_install_provenance` — best-effort, never load-bearing; any failure (missing file, malformed JSON, IO error) collapses to `(None, None)` |
| Required for ghook to function? | No — ghook works fine without it; only `--diagnose` provenance fields go `null` |

Schema:

```json
{
  "install_method": "github-release",
  "install_source_url": "https://github.com/GobbyAI/gobby-cli/releases/download/ghook-v0.7.0/ghook-aarch64-apple-darwin.tar.gz",
  "installed_version": "0.7.0",
  "installed_at": "2026-04-22T18:30:00Z"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `install_method` | string \| null | Conventional values: `github-release`, `crates-binstall`, `cargo-install`, `manual`, `unknown`. `ghook` does not validate the value — installers are free to add new ones, but any consumer should fall back to `unknown` for values it doesn't recognize. |
| `install_source_url` | string \| null | URL the binary was fetched from. `null` for `cargo install` (no URL); the asset URL for `github-release`; a crate URL for `cargo-binstall` if available. |
| `installed_version` | string | The version the installer believed it was placing. Currently informational only (not surfaced via `--diagnose` in v2) — kept in the file so future schema versions can detect installer/binary version mismatches. |
| `installed_at` | string | UTC ISO-8601 timestamp. Currently informational only. |

The sidecar is *next to the binary*, not at a fixed path, so `cargo install` ↔ Gobby installer ↔ manual placement all work consistently. `read_install_provenance` resolves it via `std::env::current_exe()`'s parent directory.

## Transport: Spool & POST

**File:** `src/transport.rs`

### Filename Shape

```text
~/.gobby/hooks/inbox/<prefix>-<ts13>-<uuid>.json
                      └c│n┘ └13-digit ms┘ └v4┘
```

| Field | Purpose |
|-------|---------|
| `prefix` | `c` for critical, `n` for non-critical. Lets the drain worker prioritize critical envelopes. |
| `ts13` | Zero-padded 13-digit ms-since-epoch. Lex-sortable, so drain order matches enqueue order even within the same second. |
| `uuid` | Random v4. Disambiguates simultaneous enqueues from concurrent hook fires. |

`.tmp` suffix is reserved for the intermediate atomic-write stage. The drain ignores `*.tmp` — it's never a valid replay target.

### Atomic Write

```rust
atomic_write(final_path, bytes):
    create_dir_all(parent)
    tmp = final_path.with_suffix(".tmp")
    File::create(tmp).write_all(bytes).sync_all()  // fsync
    fs::rename(tmp, final_path)                    // atomic on POSIX
```

`sync_all()` is critical — without it, `rename` makes the directory entry visible but the file's contents may not have hit disk. A crash between `rename` and the OS's deferred write would leave a zero-byte envelope that the drain would parse-fail on.

### POST + Cleanup

`post_and_cleanup` POSTs a Python-compatible hook payload to `{daemon_url}/api/hooks/execute` with a 30-second timeout. The envelope's `headers` are mirrored as HTTP headers. On 2xx, the inbox file is deleted; otherwise it's left in place.

The 30s timeout is deliberately generous — the daemon may be doing real work (DB writes, agent reconciliation). `--detach` is the escape hatch for hooks where the host CLI tears down its session before 30s.

### Planned Shutdown Stops

`planned_shutdown` handles intentional daemon stop/restart windows without
changing the envelope schema. Before any project lookup, stdin read,
terminal-context injection, or enqueue, Stop hooks check
`shutdown_intent_active.json` under `$GOBBY_HOME` or `~/.gobby`. Fresh markers
are accepted for `intent` values
`stop`/`restart` or source prefixes `cli_`, `http_`, `service_`, and `mcp_`.

Accepted markers trigger a short GET to `{daemon_url}/api/admin/health`. Any
HTTP response means the daemon is reachable; transport failures mean it is
unreachable. Fresh marker plus unreachable daemon returns `{"continue":true}`
with exit 0 and no stdin/enqueue side effects.

After enqueue, the same marker rule suppresses only Stop live POST failures
classified as `Connect` or `Timeout`. `ghook` removes the just-enqueued Stop
envelope first; if removal fails, the normal critical-hook failure path stays in
force. HTTP failures are treated as daemon responses and are never suppressed.

Environment:

- `GOBBY_DAEMON_URL` overrides the daemon URL. The override is applied inside
  `gobby_core::daemon_url::daemon_url()`, so it covers the preflight, live
  POSTs, and the statusline POST uniformly.
- `GOBBY_PORT` overrides only the port, dialed on `127.0.0.1`, when
  `GOBBY_DAEMON_URL` is not set.
- Bootstrap `daemon_url` wins over separate bootstrap host/port fields when no
  env override is present.
- `GOBBY_HOME` controls marker lookup.
- `GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS` overrides the default 120-second marker
  window when positive and parseable.

### Quarantine for Malformed Stdin

When stdin can't be parsed as JSON, `quarantine_malformed_at` writes two files into `~/.gobby/hooks/inbox/quarantine/`:

| File | Contents |
|------|----------|
| `<stem>.json` | `{"quarantined": true, "stdin_bytes_b64": "..."}` |
| `<stem>.meta.json` | `{"reason": "malformed_stdin", "json_error": "<parse error>", "stdin_bytes_b64": "..."}` |

The drain never replays quarantined envelopes — they surface via `gobby status` and daemon logs. Putting them under the inbox tree (rather than alongside) means they share the same disk-space-management story without polluting drain attempts.

## Terminal Context

**File:** `src/terminal_context.rs`

Captures the caller's process context for any dispatch where tmux context is
valid. `TMUX` must be set and `TMUX_PANE` must match `^%\d+$`; the pane value is
passed through verbatim.

| Field | Source | Notes |
|-------|--------|-------|
| `parent_pid` | `libc::getppid()` (Unix) / null (Windows) | The host CLI's PID — daemon uses this to reconcile spawned-terminal agents. |
| `tty` | `libc::ttyname(0)` | Controlling terminal device path. |
| `tmux_pane` | `TMUX_PANE` env var, **only if `TMUX` is set and the pane matches `^%\d+$`** | Sharp edge from dispatcher `:205` — `TMUX_PANE` is inherited by children spawned into *other* terminals (e.g. Ghostty), so emitting it without checking `TMUX` would point `kill_agent` at the parent's pane. |
| `tmux_socket_path` | First comma-separated segment of `TMUX` | Mirror of `gobby.sessions.tmux_context.parse_tmux_socket_path`. |
| `term_program` | `TERM_PROGRAM` env var | |
| `gobby_session_id`, `gobby_parent_session_id`, `gobby_agent_run_id`, `gobby_project_id`, `gobby_workflow_name` | Eponymous env vars | Set by the Gobby daemon when it spawns the host CLI; let us correlate hooks back to the spawning context. |

`inject(input_data)` only adds `terminal_context` when:

1. `input_data` is a JSON object (not an array, scalar, etc.).
2. The key isn't already present (`setdefault` semantics from dispatcher `:682`).
3. `TMUX` is present and `TMUX_PANE` is non-empty and valid.

This means a host CLI can pre-populate `terminal_context` and ghook will respect it.

## Detach Semantics

**File:** `src/detach.rs`

`--detach` is requested for hooks where the host CLI exits very quickly after firing (e.g. `Stop`). On Unix it calls `setsid(2)` to escape the controlling terminal and the parent's process group — the host CLI can wait for ghook's exit code without ghook being killed when the host's session tears down.

On Windows, `setsid` doesn't exist. `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` would be the parallel, but those flags apply at `CreateProcess` time, not to an already-running process. `FreeConsole()` is the closest honest equivalent — it disables console I/O for the current process. The function exists on Windows so callers don't need `cfg` checks, but it does less than its Unix counterpart.

**Critical ordering** (`main.rs:127-128`): project-root walk-up happens *before* `detach::detach()`. macOS sandbox FS-read denials and the cwd semantics of detached processes would otherwise surprise us — the project root is captured while we still have the host CLI's full file-access context.

## Critical vs Non-Critical Exit Semantics

`ghook` keeps enqueue-first transport internals, but host-visible stdout/stderr/exit behavior follows the current per-CLI hook protocol rather than exposing separate Rust-specific delivery semantics.

## Testing

### Unit Tests

Each module has `#[cfg(test)] mod tests` with comprehensive coverage:

- **envelope.rs**: serialization shape, schema validation against `inbox-envelope.v1.schema.json`, empty-headers serializing as empty object.
- **transport.rs**: 13-digit timestamp shape, filename prefix matches `critical`, atomic-write creates parents, no `.tmp` left on success, enqueue produces valid filename, quarantine pair structure.
- **planned_shutdown.rs**: marker parsing and freshness, allowed intents and source prefixes, env overrides, Stop matching, health reachability, and post-enqueue race suppression.
- **diagnose.rs**: unknown CLI → not recognized + null source; known CLI/hook combos hit the right critical flags and terminal-context capability; schema validation for both recognized and unrecognized CLIs.
- **dispatch.rs**: machine identity stamping replaces stale host-provided
  identity and reports stable `machine_id_error` codes when local identity is
  missing or empty.
- **cli_config.rs**: per-CLI critical-hook membership; case-insensitive CLI lookup; unknown CLIs remain unrecognized for diagnose and fall back to conservative Claude-like config on the live dispatch path.
- **terminal_context.rs**: tmux socket-path parsing edge cases, tmux pane validation, `inject` respects existing context, `inject` no-ops on non-objects, `capture` emits all expected keys when valid.

### Schema Validation in Tests

Both schemas are validated end-to-end at test time using `jsonschema`:

```rust
let schema_bytes = include_bytes!("../schemas/inbox-envelope.v1.schema.json");
let schema: Value = serde_json::from_slice(schema_bytes).unwrap();
let compiled = jsonschema::JSONSchema::options()
    .with_draft(jsonschema::Draft::Draft7)
    .compile(&schema)
    .expect("schema compiles");
let instance = serde_json::to_value(&envelope).unwrap();
compiled.validate(&instance)?;
```

This means changing the Rust struct without updating the schema (or vice versa) breaks the build — they're kept in lockstep by the test suite.

### Running Tests

```bash
cargo nextest run -p gobby-hooks
cargo test --doc -p gobby-hooks
```

No integration tests — ghook's I/O is contained (one file write, one HTTP POST), and both are covered by unit tests using `tempfile::tempdir()` and dummy daemon URLs.

## Adding a New Host CLI

The flow to support a new CLI (say, "cursor"):

1. **Add a registry entry** in `cli_config.rs::CliConfig::for_cli`:

   ```rust
   "cursor" => Some(Self {
       source: "cursor",
       critical_hooks: ["session-start"].into_iter().collect(),
       json_error_exit_code: 2,
   }),
   ```

2. **Add a unit test** for the new entry in `cli_config.rs::tests`.

3. **Add a diagnose test** in `diagnose.rs::tests` confirming the new CLI is recognized.

4. **No envelope schema changes** — the schema is CLI-agnostic. `source` is just a string.

5. **No transport changes** — same inbox, same daemon endpoint.

Unknown CLIs fall back to conservative Claude-like dispatch behavior on the live path. Diagnose mode still reports them as unrecognized. Adding a registry entry upgrades that path from fallback behavior to first-class parity.

## Adding a New Hook Type

Almost always config-only. ghook treats `--type` as opaque. To make a hook critical, add the hook type to the CLI's `critical_hooks` set in `CliConfig`. Terminal-context enrichment is CLI-agnostic and only depends on valid tmux pane env vars.

## Versioning

ghook is at `0.7.0`. The envelope `SCHEMA_VERSION` is `1`; the diagnose-output schema is `2`. The three version numbers are independent:

- **Crate version** bumps for any code change (binary behavior, dependencies, perf, etc.).
- **Envelope `SCHEMA_VERSION`** bumps only when the inbox envelope shape changes in a way the daemon must explicitly handle.
- **Diagnose-output schema version** bumps when `--diagnose`'s JSON output adds, removes, or changes fields.

`--version` writes `~/.gobby/bin/.ghook-runtime.json` with the crate and envelope-schema numbers, so the daemon can detect mismatches at startup and refuse to drain envelopes from a future envelope schema it doesn't understand.

### Release-Time Tag/Version Alignment

The `release-ghook` workflow runs a guard step before publishing that asserts the pushed `ghook-v{X}` tag's version suffix equals the version in `crates/ghook/Cargo.toml`. This prevents the failure mode described in [GobbyAI/gobby-cli#4](https://github.com/GobbyAI/gobby-cli/issues/4): if the tag and the crate version drift, the public installer's `crates.io → ghook-v{version}` GitHub-asset lookup silently misses, which then collapses to slow `cargo install` fallbacks and reproduces as "bare `Blocked by hook` because users are on an old binary." The guard fails the workflow before any artifact reaches crates.io or the GitHub release.
