# gterm and gclient Development Guide

Technical internals for developers and agents working in `crates/gterminal`
(`gobby-terminal` → `gterm`) and `crates/gclient` (`gobby-client` → `gclient`).

`gobby` remains the Python daemon and Click CLI. Users run `gclient` for the
workspace TUI. `gterm` is the supervised native PTY host. They are two
binaries on purpose: the Ghostty VT engine sits behind `gobby-terminal`'s
`vt-engine` feature and must not ship inside every client install.

## Build

Zig 0.15 is a **build-time** dependency only for `vt-engine` (the `gterm`
binary and its CI jobs). `gobby-client` never invokes Zig.

```bash
# Host (requires zig 0.15 on PATH)
cargo build --release -p gobby-terminal --features vt-engine --bin gterm

# Workspace client (Zig-free)
cargo build --release -p gobby-client
```

On macOS, Zig 0.15.2's libc++ build is incompatible with the macOS 27 SDK
(`INFINITY` is undeclared in `__random/clamp_to_integral.h`). Select an installed
Xcode with the macOS 26.5 SDK for this toolchain, for example:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun --sdk macosx --show-sdk-version
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cargo build --release -p gobby-terminal --features vt-engine --bin gterm
```

Verify that the first command reports `26.5`. `SDKROOT` does not override Zig's
`xcrun --sdk macosx` lookup. Keep SIMD enabled for normal builds. The optional
non-SIMD Darwin archive uses the same member-preserving normalization as the
SIMD archive; its focused build/link regression is:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cargo nextest run -p gobby-terminal --test build_env -E 'test(darwin_nonsimd_archive_links_every_member)'
```

End users receive prebuilt GitHub release assets. The installer local-workspace
fallback for `gterm` builds `--features vt-engine` with a 600s timeout; if
`zig` is missing it skips that step with an explicit reason and continues to
Gobby-hosted GitHub assets. `gclient`'s local build is ordinary cargo.

### Rebuild and reinstall

A crate change is live only after rebuild **and** reinstall via a new inode.
macOS kills processes that exec an in-place-overwritten signed binary:

```bash
cargo build --release -p gobby-terminal --features vt-engine --bin gterm
cargo build --release -p gobby-client
mkdir -p ~/.gobby/bin
cp target/release/gterm ~/.gobby/bin/.gterm.new
mv -f ~/.gobby/bin/.gterm.new ~/.gobby/bin/gterm
cp target/release/gclient ~/.gobby/bin/.gclient.new
mv -f ~/.gobby/bin/.gclient.new ~/.gobby/bin/gclient
chmod 755 ~/.gobby/bin/gterm ~/.gobby/bin/gclient
```

`install -m 755` over an existing path is not sufficient on macOS.

## Protocol contracts

Host sockets are Unix domain, mode 0600, under `~/.gobby/`:

| Socket | Protocol | Credential |
| --- | --- | --- |
| `gterm-control.sock` | JSON-lines control (`spawn`, `kill`, `resize`, `write`, `list`, …) | `~/.gobby/gterm-control.token` (daemon only) |
| `gterm-frames.sock` | bincode frames (`Hello`/`Welcome`, `AttachTerminal`, `Frame`, …) | `~/.gobby/local_cli_token` |

Golden corpus: `crates/gterminal/tests/fixtures/wire_golden/`. `gclient` attaches
frame streams from the host and talks to the daemon only through the public
HTTP/WS API. Writes never go on the frame socket.

Logs: `~/.gobby/logs/gterm.log` (host) and `~/.gobby/logs/gclient.log` (TUI).

## Release tags

Stage-0 ships four triples and no Windows assets:
`aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-unknown-linux-gnu`,
`aarch64-unknown-linux-gnu`.

Tag prefixes: `gterm-v*` and `gclient-v*`. **Publish `gobby-terminal` version
*V* before tagging `gclient-v*` that depends on *V*.** `release-gclient.yml`
preflights crates.io and fails before `cargo package` / `cargo publish` if that
version is unpublished or yanked. Do not invent a combined workflow.

## Backend status

`tmux` is the default backend: `terminals.default_backend: tmux` in the bundled
`config.yaml` and `TerminalConfig.default_backend`. Externally discovered sessions
(`ownership: external`) are always tmux. `native` is explicit opt-in — `backend:
native` on the spawn request, or `terminals.default_backend: native` — and requires
an installed `gterm`. When the host is unavailable a native spawn fails before fork
with the typed refusal `host_unavailable` (`HostUnavailableError`, a
`HostCommandError`); there is no silent tmux fallback. Native lifecycle, host
recovery, coordinated writes, and the workspace client are implemented; native
launches remain opt-in. The native default flip gate is
`docs/evidence/native-backend-flip.md`: P7's host-driven acceptance suite must be
green in ordinary CI on macOS and Linux at the same commit, with no later red
evidence row. Evidence rows are append-only in execution order. Until that gate is
satisfied, `tmux` remains the default. Roll back a native-default deployment with
`gobby config set terminals.default_backend tmux`.

## Landing worktree

This is historical landing provenance, not a claim that a worktree or carve-out
is currently active. Inspect installed worktree/task rows before acting on it.

- **historical worktree**: `0.5.0-test` (gobby-worktrees id `d2a661ee`) at
  `~/.gobby/worktrees/gobby/0.5.0-test`, branched from `0.5.0` at `e19caa9a9f`.
- **merge provenance**: `wt-task-20255-m4` (`518cec5c41`, 25 commits, merge-base
  `b89f371a15`) was merged once with `git merge --no-ff`; the merge commit's parents
  are `e19caa9a9f` and `518cec5c41`. Resolution rules: 0.5.0 behaviour wins,
  worktree structure wins (`TerminalRuntime`, `terminals` rows,
  `agent_runs.terminal_id`, backend-neutral WS messages), pins stay at schema 407
  until migration 408 lands, tests take the union with 0.5.0 assertions ported to
  the renamed seams (`manager_for_terminal_context`, `snapshot_lines`,
  `dispatch_keys`). Web delivery is split by backend (#21195): a `tmux` row is
  viewed through the tmux-client PTY bridge (`src/gobby/agents/tmux/pty_bridge.py`,
  `history.py`, `alt_screen.py` and `src/gobby/servers/websocket/tmux_activation.py`)
  — `terminal_attach` reserves, the browser's first `terminal_resize` spawns
  `tmux attach-session` in a PTY at that geometry, the bounded `capture-pane`
  history goes out as `terminal_attach_history`, raw PTY bytes stream as
  `terminal_output` keyed by attachment id, and `terminal_input` writes raw
  bytes to the PTY; a `native` row goes through the gterm host proxy. The #20805
  no-op-resize guard lives in `TmuxPTYBridge.resize` for tmux rows and in
  `src/gobby/servers/websocket/terminal_ws.py::_handle_terminal_resize` for native
  rows. The current gclient renders tmux rows through a gterm host observer
  (see *Client status*).

## Sandboxed validation by operating system

Managed-agent SRT launches on macOS permit Unix sockets beneath the canonical
current-run temp directory. Use `CLAUDE_CODE_TMPDIR` or the run's `TMPDIR`, with
short unique fixture names and an encoded path shorter than 104 bytes. Fixtures
own cleanup of their hosts, tmux servers, sockets, and temporary files, including
setup failures. The grant includes nested directories but excludes other runs
and symlink escapes. Separate operator grants remain intact and
`allowAllUnixSockets` remains false.

Linux and WSL2 receive no additional Unix-socket grant. Run-local directory
creation and writes remain allowed on both platforms. This change applies to
managed agents; web-chat socket permissions are separate.

The *Guard set H* section below carries the client-completion gate.
Socket-dependent validation includes group 2 (Rust terminal tests),
group 3 (Python terminal/runtime and websocket contracts), and group 6 (terminal
client stack e2e), plus `tests/e2e/test_external_terminal_attach.py`.
`tests/terminals/test_runtime_contract.py` belongs to group 3. Group 7 compares
host PID sets around groups 2, 3, and 6; record owned tmux processes too.

Coordinators must record the OS, provider, agent run/session IDs, checkout/commit,
generated policy and canonical temp path, exact commands, result counts, and
process sets before and after. Mark every skipped or policy-denied case
**UNVALIDATED**, including its reason; it does not satisfy required coverage.
When validation must run inside a spawned macOS Codex agent, use that managed
run with isolated test state, database schema, and ports. A parent-shell result
does not substitute for it. Policy changes also require actual socket
bind/listen/connect inside the grant, denial outside it (another run and symlink
escapes), and Linux/WSL directory-write and socket-restriction evidence.

## Guard set G (foundation history)

The original landing epic used this set, run from the `0.5.0-test`
root with `DATABASE_URL` pointed at the isolated test hub
(`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
`GOBBY_TEST_PROTECT=1`:

1. `uv run pytest tests/test_runner_lifecycle_restart_replay.py tests/agents/test_resume_executor.py tests/agents/test_spawn_executor.py tests/agents/test_tmux.py tests/agents/test_lifecycle_monitor.py tests/agents/test_capture_consumers.py tests/config/test_runtime_config_contract.py tests/config/test_terminal_config.py tests/cli/test_install_setup_gterm.py tests/gterminal/test_vendor_layer.py tests/mcp_proxy/tools/sessions/test_terminal.py tests/mcp_proxy/tools/sessions/test_terminal_clear.py tests/servers/test_tmux_mixin.py tests/servers/test_admin_health.py tests/install/test_version_pins.py tests/install/test_distribution.py tests/tasks/test_validation_evidence.py`
2. `uv run pytest tests/terminals tests/storage/test_terminals.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_rename.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_native_web_proxy.py tests/servers/test_attention_respond.py tests/mcp_proxy/test_sessions_terminal_tools.py` (DB-backed; run with `GOBBY_POSTGRES_TEST_DSN` exported)
3. `cargo build -p gobby-terminal --release --features vt-engine && cargo clippy -p gobby-terminal -p gobby-client --all-targets --features vt-engine -- -D warnings && cargo nextest run -p gobby-terminal -p gobby-client --features vt-engine`
4. `cargo nextest run -p gobby-core -p gobby-daemon` (schema identity and grant pins)
5. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/ && uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
6. `cd web && npx --no-install vitest run hooks/ activitySessionVisibility.test.ts`
   The directory-segment and unique-basename substrings deliberately select
   `src/hooks` plus `src/components/activity/__tests__/activitySessionVisibility.test.ts`.
   Bare `hooks` also selects two hook-named tests outside `src/hooks`, while adding
   slash-terminated filters for both original directories widens the run to the
   whole web suite under Vitest 4.
7. Host leak check: the set of `gterm host` PIDs after groups 2–3 equals the set
   before, and no surviving `gterm host` references a state directory the run
   created. For group 2 this is deterministic: the session fixture
   `_assert_no_leaked_hosts` in `tests/terminals/conftest.py` fails the run when a
   host under the run's temp roots survives or when a durable host (state directory
   outside every temp root, such as the daemon's `~/.gobby` host) that existed
   before the session is gone after it, so a green group 2 is the group 7 evidence
   for that group. Group 3 has no such fixture; compare `ps -Ao pid,lstart,comm`
   snapshots around it by hand.

The following records historical carve-outs, which ended when their owners
closed; they are not present-day exemptions. From 1.1
close: group 2 and group 4's schema-identity tests (owner 1.2, until 1.2 closes:
the installed `gdaemon` is at schema 407, so `agent_runs.terminal_id` and the
`terminals` table are absent from the test hub —
`psycopg.errors.UndefinedColumn: column ar.terminal_id does not exist`); the red
tests named in 1.4 — `tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation`,
`tests/agents/test_resume_executor.py::test_codex_resume_delivers_prompt_via_composer_not_argv`,
`tests/terminals/test_no_direct_tmux_spawn.py`, `tests/terminals/test_no_direct_tmux_consumers.py`
(owner 1.4, until 1.4 closes); and
`tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`
(owner 1.3, until 1.3 closes — it passes on the merged tree, so it is carved out only
if the default revert in 1.3 turns it red). A carved-out test must fail for the
behavioural reason recorded at `518cec5c41` (an assertion or mock-call failure),
never at collection.

## Client status

`gclient` is the workspace TUI: it lists terminals by project, renders terminal
panes, manages tabs and splits, persists layouts, shows attention, and takes or
releases control for input and resize. Roster, lifecycle, attention, and writes
use the daemon's public HTTP/WS API. Frame sockets remain read-only.

Three terminal paths are available:

| Terminal path | Frame delivery |
| --- | --- |
| Local native terminal | Direct semantic frames from `gterm-frames.sock`. |
| Local tmux terminal | Direct semantic frames from the gterm host's tmux observer, identified by socket, server PID/start time, and pane ID. |
| Remote terminal | Daemon WS proxy with `encoding: "semantic_frame"`; `terminal_frame` carries base64 bincode frames. |

The status bar reports `direct` or `proxy`. A failed direct connection falls back
to the proxy for that pane. The browser uses the ANSI proxy path described in
[the protocol contract](../contracts/gterm-protocols.md#daemon-websocket-messages).

### Remote use

The operator supplies the remote daemon endpoint and its credential:

1. On the daemon machine, set `bind_host` in `~/.gobby/bootstrap.yaml` to its
   tailnet address. Apply the change with a coordinated daemon restart from the
   main checkout. Ensure the daemon's HTTP port (default `60887`) is reachable
   over the tailnet and its gterm host is healthy.
2. Securely copy that machine's `~/.gobby/local_cli_token` to an owner-only file
   on the client machine, for example `~/.gobby/remote_local_cli_token`, and run
   `chmod 600 ~/.gobby/remote_local_cli_token`.
3. Run the client against that endpoint:

   ```bash
   gclient --daemon-url http://100.101.102.103:60887 --token-file ~/.gobby/remote_local_cli_token
   ```

Replace the example address with the daemon's tailnet address. The client uses
Bearer authentication for HTTP and `/ws` on that same endpoint.
`--token-file` defaults to `~/.gobby/local_cli_token`; use the explicit file to
keep the remote credential separate. Remote use needs no local gterm host, but
startup refuses an unavailable remote host. Machine registration alone supplies
neither the endpoint nor this credential.

## Guard set H

The authoritative seven checks follow unchanged. Apply the operational notes
below when executing them.

**Guard set H.** Every leaf's close gate runs from the `0.5.0` checkout with
`DATABASE_URL` pointed at the isolated test hub
(`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
`GOBBY_TEST_PROTECT=1`:

1. `cargo build --release -p gobby-client && cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings && cargo nextest run -p gobby-client`
2. `cargo nextest run -p gobby-terminal` (the embed suite's `gclient_views` source
   assertion and the host contract stay green)
3. `uv run pytest tests/terminals tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_native_web_proxy.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_tmux_mixin.py tests/servers/test_attention_respond.py tests/servers/websocket/test_broadcast.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/storage/test_terminals.py` (DB-backed; `GOBBY_POSTGRES_TEST_DSN` exported)
4. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/ && uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
5. `cd web && npx vitest run src/hooks src/components/activity`
6. From 4.3 close onward: `uv run pytest tests/e2e/test_terminal_client_stack.py`
   against `gclient` and `gterm` rebuilt from the tree and installed via new inode
   (`cp` to a dotfile, `mv -f` over the name, per this guide's § "Rebuild and
   reinstall"). macOS kills processes that exec an in-place-overwritten signed binary,
   so overwriting the installed path directly is not an option.
7. Host leak check: the set of `gterm host` PIDs after groups 2, 3, and 6 equals the
   set before.

### Operational notes

Export the test environment before running any group from the `0.5.0` checkout:

```bash
cd /Users/josh/Projects/gobby
export DATABASE_URL="postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
export GOBBY_TEST_PROTECT=1
export GOBBY_POSTGRES_TEST_DSN="$DATABASE_URL"
```

**Groups 1 and 4: separate calls.** Run each command below unpiped in its own
shell-tool call so each result has a definitive exit. Leading `cd path &&` and
`VAR=value` prefixes are credited through. The current evidence parser can also
credit recognized segments of a successful top-level `&&` validation chain;
pipes, fallback commands and trailing output remain uncredited. Separate calls
make a failure's coverage unambiguous. See the task closing reference for the
current evidence contract.

```bash
cargo build --release -p gobby-client
cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings
cargo nextest run -p gobby-client
uv run ruff check src/
uv run ruff format --check src/
uv run mypy src/
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new
```

**Group 5: working directory and worker limit.** Use this command:

```bash
cd /Users/josh/Projects/gobby/web && ./node_modules/.bin/vitest run src/hooks src/components/activity --maxWorkers=4
```

`vitest run --root <web>` does not change the working directory and can fail with
ENOENT. `--maxWorkers=4` is required: unrestricted runs produced roughly 19–28
spurious timeouts under load. The operator's verified run passed 116 test files
and 1055 tests. Run group 6 from the repository root again.

**Group 6: build the binary, then stage a new inode.** Before the stack test, run:

```bash
cargo build --release -p gobby-terminal --features vt-engine --bin gterm
```

Rebuild `gclient` as in group 1, then use the dotfile `cp` and `mv -f` sequence
under *Rebuild and reinstall* for both binaries. The `gterm` binary requires
`vt-engine`: bare `cargo build --release -p gobby-terminal` builds the library,
prints `Finished`, and exits 0 without building the binary, leaving any stale
`gterm` in place. Regression coverage:
`tests/terminals/test_host_manager.py::test_gterm_bin_requires_vt_engine`.
All `gobby-terminal` dependencies are third-party; it depends on no workspace
crates, so a `gcore` change does not invalidate a built `gterm`. Compare hashes
of built and installed binaries rather than inferring staleness from mtime.

**Group 7: record the host PID set.** Group 2 checks itself: its session fixture
in `tests/terminals/conftest.py` asserts that no host under the run's temp roots
survives and that every durable host present before the session is still present
after it. For groups 3 and 6, capture the set of `gterm host` PIDs before the
socket-dependent group and compare it after. Record start times to distinguish
PID reuse. If the worker sandbox denies process inspection, obtain this evidence
from the coordinator; mark the worker check unvalidated rather than attempting
`ps`/`pgrep` repeatedly. Never stop the operator's baseline host to make the sets
match.

_Last verified: 2026-09-12_
