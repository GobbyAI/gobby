# Sandbox-compatible hooks across Claude / Codex / Gemini / QwenCode

## Context

Every Gobby adapter registers the same hook shape today: the CLI shells out to
`uv run $HOOKS_DIR/hook_dispatcher.py --cli=<x> --type=<y>`, which HTTP-POSTs
to `127.0.0.1:60887/api/hooks/execute`. That invocation has three hidden
dependencies the host CLI's sandbox can refuse:

1. **Exec**: running `uv` and reading `~/.cache/uv/` + the dispatcher file.
2. **Filesystem**: reading `~/.gobby/bootstrap.yaml` and the hooks dir.
3. **Network**: loopback to `127.0.0.1:60887`.

Codex CLI's default `sandbox_mode: workspace-write` has
`network_access: false`; Claude `sandbox: true`, Gemini strict profiles, and
QwenCode + OpenSandbox all hit one or more of the three.

User priority: **Codex → Gemini → Claude Code → QwenCode.** Primary observed
symptom: "hook command won't start" — the `uv run` front end fails before
Python even loads. Loopback failure is secondary.

**Implementation strategy: Rust.** Gobby is mid-migration from Python to
Rust; `~/Projects/gobby-cli` already ships `gcode`, `gsqz`, and `gloc` as
standalone binaries. The hook dispatcher is exactly the shape of code being
ported and is also the failure case we need to fix. Writing it in Python as a
`gobby-hook` console_script and then porting later would be a double-
implementation tax. Do it in Rust now, matching the existing crate pattern.

Chosen posture on sandbox config: Gobby declares its requirements as data per
adapter and writes the corresponding entries into each CLI's settings at
`gobby install` time automatically, with `--dry-run` for visibility.

Target outcome: a plain `gobby install` on a machine with `ghook` present in
`~/.gobby/bin/` and any of the four CLIs running in its strictest default
sandbox mode produces working hooks — no hand-edited CLI config.

## Resolved questions (from prior review)

- **Does plain `gobby install` always yield working hooks?** Yes — sandbox
  mutations are default behavior, not opt-in. Visibility via install
  transcript + new `--dry-run` flag. No `--apply-sandbox` gate.
- **Inbox record format** (minimum viable replay envelope):
  ```json
  {
    "schema_version": 1,
    "enqueued_at": "<iso8601>",
    "critical": false,
    "hook_type": "session-start",
    "input_data": { ... original payload ... },
    "source": "claude",
    "headers": {
      "X-Gobby-Project-Id": "...",
      "X-Gobby-Session-Id": "..."
    }
  }
  ```
  Drain replays as an authenticated POST with these exact headers. Critical
  hooks drain in enqueue order; non-critical hooks may be drained
  concurrently within a given (session, hook_type) pair.
- **Python vs Rust:** Rust. `ghook` is the fourth crate in the gobby-cli
  workspace.
- **Binary distribution model:** match `gcode` / `gsqz` exactly, including
  **automatic install via `gobby install`**. `install_setup.py` already has
  a three-tier fallback (`_install_gsqz_from_github` → `cargo-binstall` →
  `cargo install`) with crates.io version resolution, stamp files
  (`~/.gobby/bin/.gsqz-version`), and PATH setup across shells. A new
  `_install_ghook()` mirrors that one-for-one. Python side resolves
  `~/.gobby/bin/ghook` first, then `shutil.which()`.

## Critical files

**New in `~/Projects/gobby-cli`:**
- `crates/ghook/Cargo.toml`
- `crates/ghook/src/main.rs` — `clap` entry point
- `crates/ghook/src/envelope.rs` — replay envelope struct + serde
- `crates/ghook/src/transport.rs` — enqueue-first flow (file write + HTTP)
- `crates/ghook/src/diagnose.rs` — sandbox probe for `--diagnose`
- `crates/ghook/src/config.rs` — reads `~/.gobby/bootstrap.yaml` for port
- `.github/workflows/release-ghook.yml` — mirrors `release-gcode.yml`
- Root `Cargo.toml` — add `ghook` to workspace members, opt level "z"

**Modified in `~/Projects/gobby` (the Python daemon side):**

Binary installer — new `ghook` lane mirroring `gcode`/`gsqz`:
- `src/gobby/cli/install_setup.py` — add `_install_ghook()`,
  `_get_latest_ghook_version()`, `_get_installed_ghook_version()`,
  `_write_ghook_version_stamp()`, `_install_ghook_from_github()`, and the
  cargo-binstall / cargo-install fallbacks, parallel to the existing
  `_install_gsqz` / `_install_gcode` structure at `install_setup.py:232+`.
  Hook the new lane into the main install flow around
  `install_setup.py:183`.

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
- New: `src/gobby/hooks/inbox.py` — daemon-side drain watcher
- `src/gobby/runner_maintenance.py` — wire drain into maintenance tick

Cross-binary resolution utility:
- New: `src/gobby/utils/native_bin.py` — one resolver used by `ghook`
  invocation, plus migrate `gcode` (in `src/gobby/code_index/maintenance.py`)
  and `gsqz` (in `src/gobby/llm/sdk_utils.py`) to use it. Small, safe
  refactor that pays down existing duplication.

**To be retired (one release compatibility window):**
- `src/gobby/install/shared/hooks/hook_dispatcher.py` — remains installed
  for pre-upgrade cleanup detection only; no longer invoked by new installs.

**Reuse:**
- `src/gobby/agents/sandbox.py` — existing sandbox-profile vocabulary; model
  adapter-side declarations on the same primitives where applicable.

## Sequencing & independence

The two repos work fully in parallel. There is **no hard synchronization
point** — the daemon does runtime detection of `ghook` and chooses between
the new and legacy code path on every `gobby install`. Whoever ships first
just works; the other side lights up when its piece arrives.

**Detection rule (evaluated at `gobby install` time, per CLI):**

```
ghook_bin = native_bin.resolve("ghook")   # ~/.gobby/bin/ghook, then PATH
if ghook_bin:
    register_hook_command(f"{ghook_bin} --gobby-owned --cli=X --type=Y")
    apply_sandbox_writes(SandboxRequirements(exec_paths=[ghook_bin], ...))
    record_manifest(hook_bin=ghook_bin, sandbox_writes=...)
else:
    register_hook_command("uv run $HOOKS_DIR/hook_dispatcher.py ...")  # legacy
    skip_sandbox_writes()                  # don't loosen user's sandbox
                                           # for a binary they don't have
    print_warning("ghook not installed — sandboxed hooks may fail. "
                  "`gobby install` will retry installing it next run.")
    record_manifest(hook_bin="legacy", sandbox_writes=None)
```

`_install_ghook()` is invoked before this check on every `gobby install` and
tolerates release-not-yet-available cleanly (returns `skipped`). So the
typical user path is: one `gobby install` run after `ghook` lands in
releases → binary downloaded → subsequent per-CLI registration picks the
`ghook` branch → sandbox writes applied → everything works.

**What this buys us:**

- No coordinated release day. Daemon PRs merge whenever; `ghook` PRs merge
  whenever.
- Users who upgrade the Gobby daemon before `ghook` ships are unaffected —
  the legacy `hook_dispatcher.py` path remains the default until `ghook`
  can be installed.
- Users who upgrade and *do* have `ghook` get the new flow automatically
  without re-running anything special.
- Downgrade and partial-install scenarios stay sane: if `ghook` disappears
  from `~/.gobby/bin/` (manual delete, corrupt upgrade), next `gobby
  install` detects its absence and flips back to legacy. The manifest
  records which branch is active so uninstall knows what to remove.

**Daemon-side invariants during the transition:**

1. `hook_dispatcher.py` stays shipped in the Gobby wheel until `ghook` has
   been released for one full version cycle. It is the fallback, not dead
   code.
2. The drain (Phase 2.7) always runs regardless of which branch is active;
   it watches the inbox dir and no-ops when empty. `hook_dispatcher.py`
   will be retrofitted to write envelopes to the inbox on HTTP failure as
   part of Phase 2.7 so the legacy path also benefits from loss-free
   replay. (This is a small addition — the dispatcher already has all the
   context it needs.)
3. Sandbox writes are strictly gated on `ghook` being present. We never
   loosen a user's sandbox for a binary they don't have.
4. The install manifest carries a `branch: "ghook" | "legacy"` field so
   re-runs can detect branch transitions and clean up the old entries
   before writing the new ones.

**Concretely for the `gobby-cli` agent:** you can ship `ghook` on whatever
timeline you want. Nothing on the daemon side changes the contract you're
writing against. The envelope and diagnose-output schemas live in
`gobby-cli` under your control; when `ghook` v0.1.0 lands, the daemon
picks it up on next `gobby install`.

## Cross-repo coordination

Work splits across two repos — no shared-dependency merge, but the coupling
seams are pinned as explicit schemas so the Rust-side agent (in
`gobby-cli`) and the Python-side agent (in `gobby`) can work in parallel
against a stable contract.

**Canonical plan location:** `~/Projects/gobby-cli/docs/plans/sandbox-tolerant-hooks.md`
(copy of this plan file). The Rust-side agent owns Phases 0.6, 1.2, 2.1–2.6,
2.8, and the CI release. The Python-side agent owns everything else.

**Pinned contracts (live in `gobby-cli`, consumed by both sides):**
- `gobby-cli/schemas/inbox-envelope.v1.schema.json` — the replay envelope
  structure defined in "Resolved questions" above. Rust serializes, Python
  deserializes; both validate on write/read at least in tests.
- `gobby-cli/schemas/diagnose-output.v1.schema.json` — the JSON shape that
  `ghook --diagnose` emits. Python test harness parses against this.
- `~/.gobby/bin/.ghook-compatibility` — written by `ghook` itself (or by
  `_install_ghook()` at install time). Records `{schema_version: 1}`. Gobby
  daemon reads this on start; if incompatible, refuses to start and tells
  the user to upgrade `ghook`.

## Phase 0 — Prerequisites

Pre-existing bugs / missing surfaces that must land first.

1. **Fix Qwen package data.** Add `install/qwen/*` to `package-data` /
   `include` in `pyproject.toml:95`. Built distributions are currently
   missing `install/qwen/hooks-template.json`, which `installers/qwen.py`
   expects.
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
   Replaces the current substring-based ownership probes. Install writes it;
   uninstall consumes it.
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
6. **Pin the coupling schemas in `gobby-cli`.** Write
   `gobby-cli/schemas/inbox-envelope.v1.schema.json` and
   `gobby-cli/schemas/diagnose-output.v1.schema.json`. Add a `cargo test`
   that validates `ghook`'s serialized output against them, and a pytest
   equivalent in `gobby` that validates the Python-side drain's parsing.
   These files ship in the `gobby-cli` repo and are the source of truth
   for both sides.
7. **Copy this plan to `gobby-cli`.** `cp` the final version of this file
   to `~/Projects/gobby-cli/docs/plans/sandbox-tolerant-hooks.md` so the
   Rust-side agent has the same brief. Keep the file at
   `~/.claude/plans/hi-gobby-i-see-nested-hellman.md` as Claude's working
   copy for this session; the `gobby-cli` copy is the durable one.

## Phase 1 — Sandbox test harness & compatibility matrix

Goal: reproducible matrix that boots each CLI in its strictest default
sandbox mode and fires every registered hook event, capturing which
dependency (exec / FS read / FS write / loopback) is denied.

1. `tests/integration/sandbox/` with one runner per CLI:
   `run_codex_sandbox.py`, `run_claude_sandbox.py`, `run_gemini_sandbox.py`,
   `run_qwen_sandbox.py`.
2. `ghook --diagnose` — probes exec (can I read my own binary?), FS read
   (`~/.gobby/bootstrap.yaml`), FS write (`~/.gobby/hooks/inbox/test.tmp`),
   loopback (TCP connect to `127.0.0.1:60887`). Emits JSON. Runners invoke
   it through the registered hook command string so measurements reflect
   the real in-sandbox context, not the host.
3. `docs/sandbox-compatibility.md` — matrix of `(cli, sandbox mode, hook
   event) → diagnose output`. Internal reference.
4. Mark runners `@pytest.mark.integration` + `@pytest.mark.slow`; gate
   behind `--run-sandbox`. Not run pre-push.

## Phase 2 — Rust `ghook` binary + enqueue-first transport

Goal: replace `uv run hook_dispatcher.py` with a single static binary
implementing a loss-free enqueue-first flow.

1. **Scaffold the crate.** `crates/ghook/` in `gobby-cli`. Conventions match
   existing crates: `anyhow::Result`, `clap` derive, `serde_json`,
   `ureq` HTTP with 1s connect / 5s total for critical hooks and 500ms
   total for non-critical, no tokio, fail-open pattern from `gsqz`.
2. **CLI surface:**
   ```
   ghook --cli=<claude|codex|gemini|qwen> --type=<hook-type> [--critical]
   ghook --diagnose --cli=<...> --type=<...>
   ghook --version
   ```
   Reads JSON payload from stdin (matching current
   `hook_dispatcher.py:653`). Reads context headers from environment
   (`GOBBY_PROJECT_ID`, `GOBBY_SESSION_ID`) which the host CLI passes
   through; falls back to reading them from
   `~/.gobby/sessions/current.json` if not set.
3. **Port resolution.** Read `~/.gobby/bootstrap.yaml` with `serde_yaml`,
   extract `daemon_port`, default 60887. Mirror
   `hook_dispatcher.py:145-175` exactly.
4. **Enqueue-first flow.** Every invocation:
   1. Build the replay envelope.
   2. Atomically write to `~/.gobby/hooks/inbox/<ts>-<uuid>.json` (write
      `.tmp`, `fsync`, rename).
   3. POST payload + headers to the daemon with short timeout.
   4. On 2xx: delete inbox file, exit 0.
   5. On connect/timeout: keep file; exit 0 non-critical, 2 critical.
   6. On HTTP 4xx/5xx: keep file for diagnostics; exit per criticality.
5. **SessionEnd detach.** Replace the current detached-curl fork with
   `ghook --detach` — on Unix, `setsid` + double-fork, then run the same
   enqueue-first flow. Because the file write precedes the POST, the event
   is durable even if the parent CLI kills the child mid-POST.
6. **Stable ownership marker.** Every registered hook command includes a
   literal `--gobby-owned` flag (no-op at runtime, ownership signal for
   probes). Same-PR migration of the three current substring probes:
   - `installers/shared.py:86`
   - `installers/codex.py:182`
   - `utils/deps.py:111`
   Keep an "old dispatcher detected" compatibility branch in each for one
   release so upgrades clean up pre-existing installs.
7. **Inbox drain (Python side).** `src/gobby/hooks/inbox.py`:
   - Scan `~/.gobby/hooks/inbox/` on daemon start and via
     `runner_maintenance.py` tick.
   - Replay in filename (timestamp) order; critical first.
   - POST to the same internal hook-execute entry point the HTTP route
     uses, with the envelope's headers.
   - On success: delete. On failure: exponential backoff with cap; after
     N failures quarantine to `inbox/quarantine/` with a log line.
8. **CI + release.** `.github/workflows/release-ghook.yml` mirrors
   `release-gcode.yml`: multi-target (darwin-arm64, darwin-x86_64,
   linux-x86_64, linux-arm64) tarballs to GitHub Releases. Publish `ghook`
   to crates.io so the cargo-binstall / cargo-install fallback tiers work
   out of the box — `install_setup.py`'s latest-version check hits
   `crates.io/api/v1/crates/<name>` today, and `ghook` needs to be there
   for the same check to succeed.
9. **Update hook templates** in all four
   `src/gobby/install/<cli>/hooks-template.json` to emit the new command
   string: `<ghook_bin> --gobby-owned --cli=<x> --type=<y>` where
   `<ghook_bin>` substitutes to the absolute resolved path from
   `native_bin.py`.

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
     / `fs_write_paths` / `exec_paths` to
     `sandbox.filesystem.allowRead` / `allowWrite` /
     `sandbox.exec.allowBinaries` respectively. **Do not** create a
     `sandbox` block if absent.
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
   mutations (before → after), and manifest diff. Exits 0 without writing.

## Out of scope

- MCP-over-stdio subprocess sandboxing — separate plan after this lands.
- Full port of the Python daemon to Rust — unchanged by this plan.
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
   `utils/deps.py`) — they simplify to only checking the
   `--gobby-owned` marker.
5. The retrofit of `hook_dispatcher.py` that writes to the inbox on HTTP
   failure (Phase 2.7, invariant 2) — no longer reachable.
6. The `branch` field in the install manifest (or keep it and make
   `"ghook"` the only valid value; decide at cleanup time).

**Sunset criteria (all must be true before this task runs):**
- `ghook` has been in GitHub Releases + crates.io for ≥ N releases
  (propose N=3; revisit at task time).
- Telemetry (or a canary `gobby status` probe across active users) shows
  the legacy branch is effectively unused. If no telemetry exists, use
  "time elapsed since `ghook` first shipped ≥ 30 days" as a proxy.
- `_install_ghook()`'s fallback chain (GitHub → cargo-binstall → cargo
  install) has a confirmed success rate on all four supported platforms
  — no user population is stuck on legacy because their platform doesn't
  get a binary.

**Why file this now and not at cleanup time:** the runtime-detection
branch is the kind of thing that silently becomes permanent if nobody
owns its removal. Filing the ticket alongside the initial implementation
is the forcing function.

## Verification

1. **Rust side** (`gobby-cli`): `cargo test -p ghook`, `cargo clippy -p
   ghook -- -D warnings`, `cargo build --release -p ghook`.
2. **Python side** (`gobby`): `uv run ruff check src/` +
   `uv run mypy src/` clean.
3. `uv run pytest tests/cli/installers/ -v` — installer unit tests cover
   idempotent writes, manifest round-trips, dry-run output, and the
   ownership-marker migration.
4. `uv run pytest tests/integration/sandbox/ -v --run-sandbox` — all four
   CLIs report all hook events firing in diagnose mode.
5. Manual end-to-end per CLI (only real way to catch sandbox drift):
   - Fresh shell → `gobby install --dry-run` → inspect → `gobby install`
     — verify the `ghook` install lane runs (GitHub tarball path first,
     cargo fallbacks second), `~/.gobby/bin/ghook` appears with a stamp
     file at `.ghook-version`, and PATH is set up.
   - Start each CLI in default sandbox mode → quick prompt → confirm
     `gobby sessions` and daemon log show hooks firing.
   - Simulate a GitHub Releases outage (block the tarball URL) and confirm
     cargo-binstall / cargo-install fallbacks produce a working
     `~/.gobby/bin/ghook`.
6. Loss-free replay: `gobby stop`, fire hooks (including SessionEnd),
   confirm envelopes in `~/.gobby/hooks/inbox/`, `gobby start`, confirm
   drain processes every entry with correct project/session headers.
7. Idempotence: `gobby install` twice → second run is no-op (empty
   manifest diff).
8. Uninstall: `gobby uninstall` consults manifest, removes only Gobby-
   owned keys.
