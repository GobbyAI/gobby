# Sandbox-compatible hooks across Claude / Codex / Gemini / QwenCode — rev 1

> **Status:** Supersedes `sandbox-tolerant-hooks.md`. Reflects the frozen
> contract with the `gobby-cli` agent's counterpart plan at
> `~/Projects/gobby-cli/docs/plans/sandbox-tolerant-hooks-rust.md`.
> Both sides have signed off. This is the authoritative Python-side spec.

## Context

Every Gobby adapter registers the same hook shape today: the CLI shells out
to `uv run $HOOKS_DIR/hook_dispatcher.py --cli=<x> --type=<y>`, which
HTTP-POSTs to `127.0.0.1:60887/api/hooks/execute`. That invocation has three
hidden dependencies the host CLI's sandbox can refuse:

1. **Exec**: running `uv` and reading `~/.cache/uv/` + the dispatcher file.
2. **Filesystem**: reading `~/.gobby/bootstrap.yaml` and the hooks dir.
3. **Network**: loopback to `127.0.0.1:60887`.

Codex CLI's default `sandbox_mode: workspace-write` has
`network_access: false`; Claude `sandbox: true`, Gemini strict profiles, and
QwenCode + OpenSandbox all hit one or more of the three.

User priority: **Codex → Gemini → Claude Code → QwenCode.** Primary
symptom: "hook command won't start" — the `uv run` front end fails before
Python even loads. Loopback failure is secondary.

**Implementation strategy: Rust.** `~/Projects/gobby-cli` already ships
`gcode`, `gsqz`, and `gloc` as standalone binaries. The hook dispatcher is
exactly the shape of code being ported and the failure case we need to fix.
Writing it in Python as a `gobby-hook` console_script and then porting
later would be a double-implementation tax.

Chosen posture on sandbox config: Gobby declares its requirements as data
per adapter and writes the corresponding entries into each CLI's settings
at `gobby install` time automatically, with `--dry-run` for visibility.

Target outcome: a plain `gobby install` on a machine with `ghook` present
in `~/.gobby/bin/` and any of the four CLIs running in its strictest
default sandbox mode produces working hooks — no hand-edited CLI config.

## Resolved questions

- **Does plain `gobby install` always yield working hooks?** Yes — sandbox
  mutations are default behavior, not opt-in. Visibility via install
  transcript + new `--dry-run` flag. No `--apply-sandbox` gate.
- **Python vs Rust:** Rust. `ghook` is the fourth crate in the `gobby-cli`
  workspace.
- **Binary distribution model:** match `gcode` / `gsqz` exactly, including
  **automatic install via `gobby install`**. `install_setup.py` already has
  a three-tier fallback (GitHub Releases tarball → `cargo-binstall` →
  `cargo install`) with crates.io version resolution, stamp files
  (`~/.gobby/bin/.gsqz-version`), and PATH setup across shells. A new
  `_install_ghook()` mirrors that one-for-one. Python side resolves
  `~/.gobby/bin/ghook` first, then `shutil.which()`.
- **Cross-platform targets:** `darwin-arm64`, `darwin-x86_64`,
  `linux-x86_64`, `linux-arm64`, `windows-x86_64`. Mirrors
  `install_setup.py:249-250` triples already used for `gsqz`/`gcode`.
  Windows is best-effort consistent with the rest of the product's Windows
  posture — not tested in CI, not a release blocker.
- **Context resolution:** headers come from the stdin payload and
  walk-up of `.gobby/project.json`. No env vars, no `current.json`.
  See §2.2.
- **`GOBBY_*` env vars:** enter the flow as `terminal_context` *data*,
  not as headers. See §2.4a.

## Replay envelope (schema v1 — frozen)

Committed to `gobby-cli/schemas/inbox-envelope.v1.schema.json`. Mirrored
into `gobby/schemas/` via the SOURCE_COMMIT mechanism (see Cross-repo
coordination). Rust serializes on write; Python validates on drain.

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

Omitted headers are absent from the `headers` object entirely — never
empty-string values. Drain replays as an authenticated POST with these
exact headers. Critical hooks drain in enqueue order; non-critical may
drain concurrently within a given `(session, hook_type)` pair.

## Critical files

**New in `~/Projects/gobby-cli`:**
- `crates/gobby-core/` — shared lib (project helpers, bootstrap,
  daemon_url). Scaffolded in PR 1 (R2-01/R2-04/R2-05) and PR 2
  (R2-02/R2-03) of the Rust-side plan.
- `crates/ghook/Cargo.toml`
- `crates/ghook/src/main.rs` — `clap` entry point
- `crates/ghook/src/envelope.rs` — replay envelope struct + serde
- `crates/ghook/src/transport.rs` — enqueue-first flow (file write + HTTP)
- `crates/ghook/src/diagnose.rs` — sandbox probe for `--diagnose`
- `crates/ghook/src/detach.rs` — `#[cfg(unix)]` / `#[cfg(windows)]` split
- `crates/ghook/src/terminal_context.rs` — Rust port of
  `hook_dispatcher.py:181-223` + per-CLI `terminal_context_hooks` gate
- `crates/ghook/schemas/inbox-envelope.v1.schema.json`
- `crates/ghook/schemas/diagnose-output.v1.schema.json`
- `.github/workflows/release-ghook.yml` — mirrors `release-gcode.yml`,
  tag prefix `gobby-hook-v<semver>`, tarball
  `ghook-<target-triple>.tar.gz`.
- Root `Cargo.toml` — add `ghook` to workspace members, `opt-level="z"`.

**Modified in `~/Projects/gobby` (this repo):**

Binary installer — new `ghook` lane mirroring `gcode`/`gsqz`:
- `src/gobby/cli/install_setup.py` — add `_install_ghook()`,
  `_get_latest_ghook_version()`, `_get_installed_ghook_version()`,
  `_write_ghook_version_stamp()`, `_install_ghook_from_github()`, and the
  cargo-binstall / cargo-install fallbacks, parallel to the existing
  `_install_gsqz` / `_install_gcode` structure at `install_setup.py:232+`.
  Hook the new lane into the main install flow around
  `install_setup.py:183`. Include `windows-x86_64` target triple.

Adapters & templates — update the registered hook command string:
- `src/gobby/adapters/claude_code.py`
- `src/gobby/adapters/gemini.py`
- `src/gobby/adapters/codex_impl/adapter.py`
- `src/gobby/adapters/qwen.py`
- `src/gobby/install/{claude,gemini,codex,qwen}/hooks-template.json`

Installers — write sandbox config, own the manifest:
- `src/gobby/cli/installers/claude.py:219`
- `src/gobby/cli/installers/gemini.py:102`
- `src/gobby/cli/installers/qwen.py:56,86`
- `src/gobby/cli/installers/codex.py:38,182` — swap regex TOML edits for
  `tomli`/`tomli_w`
- `src/gobby/cli/installers/shared.py:86` — ownership probe migration
- `src/gobby/cli/install.py:153` — add `--dry-run`
- `src/gobby/utils/deps.py:111` — ownership probe migration

Server / drain:
- `src/gobby/servers/routes/mcp/hooks.py:288` — drain replay must preserve
  `X-Gobby-Project-Id` / `X-Gobby-Session-Id`
- New: `src/gobby/hooks/inbox.py` — daemon-side drain watcher. Includes
  quarantine handling for malformed-stdin envelopes (§2.7a).
- `src/gobby/runner_maintenance.py` — wire drain into maintenance tick

Cross-binary resolution utility:
- New: `src/gobby/utils/native_bin.py` — one resolver used by `ghook`
  invocation, plus migrate `gcode` (in `src/gobby/code_index/maintenance.py`)
  and `gsqz` (in `src/gobby/llm/sdk_utils.py`) to use it.

Schema mirror:
- New: `schemas/inbox-envelope.v1.schema.json` — hand-mirrored from
  `gobby-cli/schemas/`. Source of truth for Python drain validation.
- New: `schemas/diagnose-output.v1.schema.json` — same.
- New: `schemas/SOURCE_COMMIT` — bare 40-byte hex SHA + trailing newline,
  pins which `gobby-cli` commit the mirror was synced from.
- New: `.github/workflows/schema-mirror-check.yml` — CI job that fetches
  `raw.githubusercontent.com/GobbyAI/gobby-cli/<SOURCE_COMMIT>/schemas/*`
  and hash-diffs against the local mirror. Fails if drift. No auth needed
  — `gobby-cli` is public.

**To be retired (one release compatibility window):**
- `src/gobby/install/shared/hooks/hook_dispatcher.py` — remains installed
  for pre-upgrade cleanup detection only; no longer invoked by new installs.

**Reuse:**
- `src/gobby/agents/sandbox.py` — existing sandbox-profile vocabulary.

## Sequencing & independence

The two repos work fully in parallel. There is **no hard synchronization
point** — the daemon does runtime detection of `ghook` and chooses between
the new and legacy code path on every `gobby install`.

**Detection rule (evaluated at `gobby install` time, per CLI):**

```
ghook_bin = native_bin.resolve("ghook")   # ~/.gobby/bin/ghook, then PATH
if ghook_bin:
    register_hook_command(f"{ghook_bin} --gobby-owned --cli=X --type=Y")
    apply_sandbox_writes(SandboxRequirements(exec_paths=[ghook_bin], ...))
    record_manifest(hook_bin=ghook_bin, sandbox_writes=...)
else:
    register_hook_command("uv run $HOOKS_DIR/hook_dispatcher.py ...")  # legacy
    skip_sandbox_writes()
    print_warning("ghook not installed — sandboxed hooks may fail. "
                  "`gobby install` will retry installing it next run.")
    record_manifest(hook_bin="legacy", sandbox_writes=None)
```

`_install_ghook()` is invoked before this check on every `gobby install`
and tolerates release-not-yet-available cleanly (returns `skipped`).

**Daemon-side invariants during the transition:**

1. `hook_dispatcher.py` stays shipped in the Gobby wheel until `ghook` has
   been released for one full version cycle. It is the fallback, not dead
   code.
2. The drain (§2.7) always runs regardless of which branch is active; it
   watches the inbox dir and no-ops when empty. `hook_dispatcher.py` will
   be retrofitted to write envelopes to the inbox on HTTP failure as part
   of §2.7 so the legacy path also benefits from loss-free replay.
3. Sandbox writes are strictly gated on `ghook` being present. We never
   loosen a user's sandbox for a binary they don't have.
4. The install manifest carries a `branch: "ghook" | "legacy"` field so
   re-runs can detect branch transitions and clean up the old entries
   before writing the new ones.
5. `.ghook-compatibility` is written by `ghook --version` on first run.
   Shape: `{ "schema_version": 1, "ghook_version": "0.1.0" }`. Daemon
   reads this on start; on `schema_version` mismatch, refuses to start
   and tells the user to upgrade `ghook`.

## Cross-repo coordination

**Canonical plan location:** this file for the Python side;
`~/Projects/gobby-cli/docs/plans/sandbox-tolerant-hooks-rust.md` for the
Rust side. The Rust-side agent owns §0.6, §1.2, §2.1–2.6, §2.8, and the CI
release. The Python-side agent owns everything else.

**Pinned contracts (source of truth in `gobby-cli`, mirrored into `gobby`):**
- `inbox-envelope.v1.schema.json` — Rust serializes, Python deserializes;
  both validate on write/read at least in tests.
- `diagnose-output.v1.schema.json` — the JSON shape that `ghook --diagnose`
  emits. Python test harness parses against this.

**Schema mirror mechanism:**
- Source of truth: `gobby-cli/schemas/*.schema.json`.
- Python-side mirror: `gobby/schemas/*.schema.json`.
- Pin file: `gobby/schemas/SOURCE_COMMIT` — bare 40-byte hex SHA + trailing
  newline. Nothing else. Sync metadata lives in git log.
- Sync operation: manual PR in `gobby` — bump `SOURCE_COMMIT`, re-copy
  files, CI validates.
- CI check: GitHub Action fetches
  `https://raw.githubusercontent.com/GobbyAI/gobby-cli/<SOURCE_COMMIT>/schemas/<file>`
  for each schema, `sha256sum`s both sides, fails on drift. No auth
  required — `gobby-cli` is public (`install_setup.py:234,237,538,541`
  confirm unauthenticated release URLs).

## Phase 0 — Prerequisites

Pre-existing bugs / missing surfaces that must land first.

1. **Fix Qwen package data.** Add `install/qwen/*` to `package-data` /
   `include` in `pyproject.toml:95`. Built distributions are currently
   missing `install/qwen/hooks-template.json`. *(Already landed in commit
   6507b9a6c — keep the item here for historical completeness.)*
2. **Structured install manifest.** New `~/.gobby/install-manifest.json`
   records, per CLI, exactly which JSON keys / TOML paths Gobby owns:
   ```json
   {
     "schema_version": 1,
     "clis": {
       "claude": {
         "settings_path": "~/.claude/settings.json",
         "owned_keys": ["hooks.SessionStart", "allowedHttpHookUrls"],
         "hook_bin": "<absolute path to ghook>"
       },
       "codex": { ... },
       "gemini": { ... },
       "qwen": { ... }
     }
   }
   ```
   Replaces the current substring-based ownership probes. Install writes
   it; uninstall consumes it.
3. **Swap Codex TOML edits to a library.** Replace the regex edits in
   `installers/codex.py:38` with `tomli` + `tomli_w`. Prerequisite for
   idempotent merges in Phase 3.
4. **`gobby install --dry-run`.** Extends `install.py:153` to compute all
   writes (hook entries, sandbox mutations, manifest updates) and print
   them without touching the filesystem.
5. **`native_bin.py` resolver + migrate existing callers.** Pays down
   duplicated `~/.gobby/bin/<name>` lookup logic now (currently two call
   sites for `gcode` and `gsqz`) so Phase 2's `ghook` lookup is the third
   user of one resolver, not new duplication.
6. **Fix dispatcher `get_daemon_url` to honor `bind_host`.** The legacy
   Python dispatcher at `hook_dispatcher.py:172` hardcodes `localhost`
   regardless of the `bind_host` value in `~/.gobby/bootstrap.yaml`. Users
   with `bind_host: 192.168.x.y` or similar silently fail against the
   daemon today. Since the dispatcher is the transition-window fallback
   (until `ghook` ships and lands on all installs), it has to be correct.
   Fix reads `bind_host` (default `"localhost"`), normalizes wildcard
   listen-addresses (`0.0.0.0`, `::`, `::0`) to `127.0.0.1`, brackets IPv6
   literals for URL syntax, and passes everything else through. The
   `gobby-core::daemon_url` Rust helper adopts the same spec — one
   canonical resolution rule on both sides.
7. **Pin the coupling schemas in `gobby-cli`.** Write
   `gobby-cli/schemas/inbox-envelope.v1.schema.json` and
   `gobby-cli/schemas/diagnose-output.v1.schema.json`. Add a `cargo test`
   that validates `ghook`'s serialized output against them. Mirror into
   `gobby/schemas/` with `SOURCE_COMMIT` and CI hash check.

## Phase 1 — Sandbox test harness & compatibility matrix

Goal: reproducible matrix that boots each CLI in its strictest default
sandbox mode and fires every registered hook event, capturing which
dependency (exec / FS read / FS write / loopback) is denied.

1. `tests/integration/sandbox/` with one runner per CLI:
   `run_codex_sandbox.py`, `run_claude_sandbox.py`, `run_gemini_sandbox.py`,
   `run_qwen_sandbox.py`.
2. `ghook --diagnose` — probes exec (can I read my own binary?), FS read
   (`~/.gobby/bootstrap.yaml`), FS write (`~/.gobby/hooks/inbox/test.tmp`),
   loopback (TCP connect to `127.0.0.1:60887`). Emits JSON per
   `diagnose-output.v1.schema.json`. Runners invoke it through the
   registered hook command string so measurements reflect the real
   in-sandbox context.
3. `docs/sandbox-compatibility.md` — matrix of `(cli, sandbox mode, hook
   event) → diagnose output`. Internal reference.
4. Mark runners `@pytest.mark.integration` + `@pytest.mark.slow`; gate
   behind `--run-sandbox`. Not run pre-push.

## Phase 2 — Rust `ghook` binary + enqueue-first transport

Goal: replace `uv run hook_dispatcher.py` with a single static binary
implementing a loss-free enqueue-first flow.

### 2.1 Scaffold the crate

`crates/ghook/` in `gobby-cli`. Conventions match existing crates:
`anyhow::Result`, `clap` derive, `serde_json`, `ureq` HTTP with 1s connect
/ 5s total for critical hooks and 500ms total for non-critical, no tokio,
fail-open pattern from `gsqz`. `serde_yaml = "0.9"` for bootstrap parse —
matches `gsqz`/`gcode` workspace consistency.

### 2.2 CLI surface

```
ghook --gobby-owned --cli=<claude|codex|gemini|qwen> --type=<hook-type> [--critical] [--detach]
ghook --diagnose --cli=<...> --type=<...>
ghook --version
```

- `--gobby-owned` is a literal no-op marker. Installers' ownership probes
  match on the substring.
- Reads JSON payload from stdin (matching current
  `hook_dispatcher.py:637-638`).
- **Headers come from stdin + walk-up, not env vars:**
  - `X-Gobby-Project-Id` ← walk up from cwd, find `.gobby/project.json`,
    read `id`. Port of `_find_project_config` at
    `hook_dispatcher.py:527`. Omit header when absent — never empty string.
  - `X-Gobby-Session-Id` ← `input_data["session_id"]` from stdin. Port of
    `hook_dispatcher.py:659-661`. Omit header when absent.
- Walk-up **must happen before detach.** Chdir / fd semantics inside a
  detached Rust process surprise on macOS; resolve the path while the
  parent's cwd is still intact.

### 2.3 Daemon URL resolution

Lives in `gobby-core::daemon_url` (R2-02/R2-03 on the Rust side). Shared
between `ghook` and any future `gobby-core` consumer; the Python
dispatcher's fixed `get_daemon_url` (§Phase 0 item 6) implements the
same spec.

Rules:
1. Read `~/.gobby/bootstrap.yaml` with `serde_yaml`. Use `dirs::home_dir()`
   for `~` expansion (matches `gsqz` config resolver).
2. Extract `daemon_port` (default `60887`) and `bind_host` (default
   `"localhost"` — matches `BootstrapConfig` at
   `src/gobby/config/bootstrap.py:31`).
3. Normalize the host for dialing:
   - `"0.0.0.0"`, `"::"`, `"::0"` → `"127.0.0.1"` (wildcard listen-addrs
     aren't dialable)
   - IPv6 literals containing `:` (e.g. `"::1"`, `"fe80::1"`) get bracketed
     (`"[::1]"`) for URL syntax
   - Everything else (hostnames, IPv4 literals, already-bracketed IPv6)
     passes through
4. Return `http://<normalized_host>:<port>`.

No `GOBBY_PORT` env override in the core helper. `gcode`'s current
`resolve_daemon_url` at `gobby-cli/crates/gcode/src/config.rs:216-246`
honors `GOBBY_PORT`; when `gcode` migrates under R2-06 it can keep that
affordance as a thin local wrapper over `gobby_core::daemon_url`.

API shape (Rust side):

```rust
pub struct DaemonEndpoint { pub host: String, pub port: u16 }
pub fn read_daemon_endpoint() -> DaemonEndpoint;
pub fn read_daemon_endpoint_at(path: &Path) -> DaemonEndpoint;
pub fn daemon_url() -> String;       // http://<host>:<port>, normalized
pub fn daemon_url_at(path: &Path) -> String;
```

No process cache — sync Rust, cheap.

### 2.4 Enqueue-first flow

Every invocation:

1. Build the replay envelope (§Replay envelope).
2. Atomically write to
   `~/.gobby/hooks/inbox/<p>-<ts13>-<uuid>.json` — write `.tmp`, `fsync`,
   rename.
   - `<p>` is `c` for critical, `n` for non-critical. Lexical sort yields
     critical-first then FIFO in one `readdir` + sort.
   - `<ts13>` is zero-padded 13-digit Unix epoch millis (stays
     lex-correct past year 2286).
   - Drain must ignore `*.tmp` — those are in-flight writes.
3. POST payload + headers to the daemon with short timeout.
4. On 2xx: delete inbox file, exit 0.
5. On connect/timeout: keep file; exit 0 non-critical, 2 critical.
6. On HTTP 4xx/5xx: keep file for diagnostics; exit per criticality.

### 2.4a Terminal context enrichment

**Load-bearing — do not drop.** Before building the envelope, for hooks
in the per-CLI `terminal_context_hooks` set, inject a `terminal_context`
object into `input_data`. This is the sole route by which `GOBBY_*` env
vars enter the hook flow, and the daemon relies on it to reconcile
spawned-terminal agents back to their Gobby session (see
`hooks/event_handlers/_session_start.py:191`).

Port of `hook_dispatcher.py:181-223` (`get_terminal_context`). Captures:

- `parent_pid` — `getppid()` equivalent
- `tty` — `ttyname(0)` equivalent, `null` on error
- `tmux_pane` — `$TMUX_PANE`, but **only when `$TMUX` is also set**
  (dispatcher `:205` — preventing parent/child tmux-pane confusion that
  breaks `kill_agent`)
- `tmux_socket_path` — parsed from `$TMUX` via the same regex
  `parse_tmux_socket_path` uses
- `term_program` — `$TERM_PROGRAM`
- `gobby_session_id` — `$GOBBY_SESSION_ID`
- `gobby_parent_session_id` — `$GOBBY_PARENT_SESSION_ID`
- `gobby_agent_run_id` — `$GOBBY_AGENT_RUN_ID`
- `gobby_project_id` — `$GOBBY_PROJECT_ID`
- `gobby_workflow_name` — `$GOBBY_WORKFLOW_NAME`

Gate: `terminal_context_hooks` set per-CLI (dispatcher `CLIConfig:61`).
`claude`: `{session-start}`. `gemini`/`qwen`: `{SessionStart}`. `codex`:
`{SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop}`. Use
`setdefault` semantics — never overwrite a `terminal_context` the host CLI
may have already placed.

### 2.5 Detach semantics

`ghook --detach`:
- Unix: single `setsid` — matches dispatcher's `start_new_session=True`
  at `:697`. No double-fork. The file-write-before-POST durability
  guarantee makes double-fork unnecessary.
- Windows: `std::os::windows::process::CommandExt` with `DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP` creation flags. No external crate needed.

Detached grandchild runs the normal enqueue-first flow. File write precedes
POST, so the event is durable even if the parent CLI kills the child
mid-POST.

### 2.6 Stable ownership marker

Every registered hook command includes a literal `--gobby-owned` flag
(no-op at runtime, ownership signal for probes). Same-PR migration of the
three current substring probes:
- `installers/shared.py:86`
- `installers/codex.py:182`
- `utils/deps.py:111`

Keep an "old dispatcher detected" compatibility branch in each for one
release so upgrades clean up pre-existing installs.

### 2.7 Inbox drain (Python side)

`src/gobby/hooks/inbox.py`:
- Scan `~/.gobby/hooks/inbox/` on daemon start and via
  `runner_maintenance.py` tick.
- Replay in filename order (lexical sort on `<p>-<ts13>-<uuid>` gives
  critical-first + FIFO).
- Ignore `*.tmp` and anything under `inbox/quarantine/`.
- POST to the same internal hook-execute entry point the HTTP route uses,
  with the envelope's `headers` preserved verbatim.
- On success: delete.
- On failure: exponential backoff with cap; after N failures move to
  `inbox/quarantine/` and write a `.meta.json` sidecar:
  ```json
  {
    "attempt_count": 5,
    "first_seen": "<iso8601>",
    "last_error": "HTTP 500: session not found",
    "last_attempt": "<iso8601>"
  }
  ```
  Drain never replays from quarantine — operator surfaces via
  `gobby status` / logs.

### 2.7a Malformed-stdin quarantine path

Where dispatcher drops malformed stdin on the floor
(`hook_dispatcher.py:647-651`), `ghook` preserves it as a diagnostic
artifact. On JSON parse failure, write **directly** to
`~/.gobby/hooks/inbox/quarantine/<p>-<ts13>-<uuid>.json` (bypass the normal
inbox entirely) with a `.meta.json` sidecar:

```json
{
  "reason": "malformed_stdin",
  "json_error": "expected value at line 1 column 1",
  "stdin_bytes_b64": "..."
}
```

The envelope file itself holds whatever structural fields `ghook` could
determine (`hook_type`, `source`, `enqueued_at`, `critical`) — `input_data`
is `null`. Drain never tries to replay these; the sidecar exists for
debuggability. Exit code follows criticality (0 non-critical, 2 critical).
Envelope schema v1 stays clean — no `malformed` branch.

### 2.8 CI + release

`.github/workflows/release-ghook.yml` mirrors `release-gcode.yml`:
- Multi-target matrix: `darwin-arm64`, `darwin-x86_64`, `linux-x86_64`,
  `linux-arm64`, `windows-x86_64`.
- Tarball naming: `ghook-<target-triple>.tar.gz`.
- Tag prefix: `gobby-hook-v<semver>` (binary-specific tag scheme per
  `gobby-cli` commit `bf9eb40`).
- Publish `gobby-hook` to crates.io so the `cargo-binstall` /
  `cargo install` fallback tiers work — `install_setup.py`'s
  latest-version check hits `crates.io/api/v1/crates/<name>`.

### 2.9 Update hook templates

All four `src/gobby/install/<cli>/hooks-template.json` emit the new command
string: `<ghook_bin> --gobby-owned --cli=<x> --type=<y>` where `<ghook_bin>`
substitutes to the absolute resolved path from `native_bin.py`.

## Phase 3 — Declarative sandbox permissions per adapter

Goal: adapters declare what they need; installers translate to idempotent
settings writes tracked by the install manifest.

1. **`SandboxRequirements` dataclass** in
   `src/gobby/adapters/sandbox_declaration.py`:
   - `loopback_hosts: list[str]`
     (default: `["127.0.0.1:60887", "127.0.0.1:60888"]`)
   - `fs_read_paths: list[str]`
     (default: `["~/.gobby/bootstrap.yaml", "~/.gobby/hooks/"]`)
   - `fs_write_paths: list[str]` (default: `["~/.gobby/hooks/inbox/"]`)
   - `exec_paths: list[str]` (default: `["~/.gobby/bin/ghook"]`)

   Each adapter returns its requirements via `sandbox_requirements()`.

2. **Installer translation (all default-on during `gobby install`,
   recorded in manifest, revertible via uninstall):**
   - **Codex** (`installers/codex.py`): using `tomli_w`, set
     `sandbox_workspace_write.network_access = true` in
     `~/.codex/config.toml`. If Codex later exposes a loopback-only
     allowlist, switch to that.
   - **Claude Code** (`installers/claude.py`): JSON-merge
     `allowedHttpHookUrls: ["http://127.0.0.1:60887/*"]` and — when the
     user already has a `sandbox` block — append Gobby's `fs_read_paths`
     / `fs_write_paths` / `exec_paths` to `sandbox.filesystem.allowRead` /
     `allowWrite` / `sandbox.exec.allowBinaries` respectively. **Do not**
     create a `sandbox` block if absent.
   - **Gemini** (`installers/gemini.py`): when a sandbox profile is
     configured, write `~/.gemini/sandbox-profiles/gobby.sb` (macOS) or
     `gobby.bwrap` (Linux) and register it via the profile include
     mechanism. No-op otherwise.
   - **Qwen** (`installers/qwen.py`): same as Gemini; when OpenSandbox is
     configured, add a host-network bridge directive.

3. **Idempotence via manifest, not comment fences.** Strict-JSON (Claude,
   Gemini, Qwen) and TOML (Codex) cannot carry inline ownership comments
   reliably.
   - Before writing, read current value, diff against adapter
     requirements, apply only the delta.
   - After writing, record the exact JSON path / TOML path in the
     manifest's `owned_keys`.
   - Re-running install diffs manifest against adapter requirements;
     adds/removes accordingly — no duplication.

4. **`gobby install --dry-run` output** prints hook entries, sandbox
   mutations (before → after), and manifest diff. Exits 0 without
   writing.

## Out of scope

- MCP-over-stdio subprocess sandboxing — separate plan after this lands.
- Full port of the Python daemon to Rust — tracked in
  `docs/plans/rust-migration-epic.md`. Daemon-side hook execution
  migration is epic Phase 6 (R6-04 through R6-07) — not this plan.
- End-user docs. `docs/sandbox-compatibility.md` is internal.

## Follow-up cleanup (filed as a gobby-task on execution kickoff)

**Title:** Remove legacy `hook_dispatcher.py` and runtime-detection branch
once `ghook` is universal.

**What gets removed:**
1. `src/gobby/install/shared/hooks/hook_dispatcher.py` — the Python hook
   dispatcher itself.
2. The `branch == "legacy"` code path in every adapter/installer that
   runtime-detects `ghook`. After cleanup, adapters register the `ghook`
   command unconditionally.
3. The "ghook not installed" warning path in the installer transcript.
4. The one-release substring-match compatibility branch in the three
   ownership probes (`installers/shared.py`, `installers/codex.py`,
   `utils/deps.py`) — they simplify to only checking the `--gobby-owned`
   marker.
5. The retrofit of `hook_dispatcher.py` that writes to the inbox on HTTP
   failure (§2.7, invariant 2) — no longer reachable.
6. The `branch` field in the install manifest (or keep it and make
   `"ghook"` the only valid value; decide at cleanup time).

**Sunset criteria (all must be true before this task runs):**
- `ghook` has been in GitHub Releases + crates.io for ≥ N releases
  (propose N=3; revisit at task time).
- Telemetry (or a canary `gobby status` probe across active users) shows
  the legacy branch is effectively unused. If no telemetry exists, use
  "time elapsed since `ghook` first shipped ≥ 30 days" as a proxy.
- `_install_ghook()`'s fallback chain (GitHub → cargo-binstall → cargo
  install) has a confirmed success rate on all five supported platforms.

**Why file this now and not at cleanup time:** the runtime-detection
branch is the kind of thing that silently becomes permanent if nobody
owns its removal. Filing the ticket alongside the initial implementation
is the forcing function.

## Verification

1. **Rust side** (`gobby-cli`): `cargo test --workspace`, `cargo clippy
   --workspace -- -D warnings`, `cargo build --release -p gobby-hook`.
2. **Python side** (`gobby`): `uv run ruff check src/` +
   `uv run mypy src/` clean.
3. `uv run pytest tests/cli/installers/ -v` — installer unit tests cover
   idempotent writes, manifest round-trips, dry-run output, and the
   ownership-marker migration.
4. `uv run pytest tests/integration/sandbox/ -v --run-sandbox` — all four
   CLIs report all hook events firing in diagnose mode.
5. **Schema mirror drift test**: `uv run pytest tests/schemas/ -v` —
   pytest validates a canonical envelope against the local mirror. CI
   schema-mirror-check job verifies the mirror matches `gobby-cli` at the
   pinned `SOURCE_COMMIT`.
6. Manual end-to-end per CLI (only real way to catch sandbox drift):
   - Fresh shell → `gobby install --dry-run` → inspect → `gobby install`
     — verify the `ghook` install lane runs (GitHub tarball path first,
     cargo fallbacks second), `~/.gobby/bin/ghook` appears with a stamp
     file at `.ghook-version`, and PATH is set up.
   - Start each CLI in default sandbox mode → quick prompt → confirm
     `gobby sessions` and daemon log show hooks firing.
   - Simulate a GitHub Releases outage (block the tarball URL) and
     confirm cargo-binstall / cargo-install fallbacks produce a working
     `~/.gobby/bin/ghook`.
7. Loss-free replay: `gobby stop`, fire hooks (including SessionEnd),
   confirm envelopes in `~/.gobby/hooks/inbox/`, `gobby start`, confirm
   drain processes every entry with correct project/session headers.
8. Malformed-stdin handling: pipe garbage into `ghook`, confirm artifact
   lands in `inbox/quarantine/` with `.meta.json` sidecar and drain does
   not attempt replay.
9. Idempotence: `gobby install` twice → second run is no-op (empty
   manifest diff).
10. Uninstall: `gobby uninstall` consults manifest, removes only
    Gobby-owned keys.
