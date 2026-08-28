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
cargo build --release -p gobby-terminal --features vt-engine

# Workspace client (Zig-free)
cargo build --release -p gobby-client
```

End users receive prebuilt GitHub release assets. The installer local-workspace
fallback for `gterm` builds `--features vt-engine` with a 600s timeout; if
`zig` is missing it skips that step with an explicit reason and continues to
Gobby-hosted GitHub assets. `gclient`'s local build is ordinary cargo.

### Rebuild and reinstall

A crate change is live only after rebuild **and** reinstall via a new inode.
macOS kills processes that exec an in-place-overwritten signed binary:

```bash
cargo build --release -p gobby-terminal --features vt-engine
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

## Default backend and rollback

Gobby-owned launches use `terminals.default_backend: native`
(`TerminalConfig.default_backend`). tmux remains the backend for externally
discovered sessions (`ownership: external`) and stays selectable per spawn via
`terminal_backend`. Evidence for the flip lives in
`docs/evidence/native-backend-flip.md`.

Rollback: set `terminals.default_backend` back to `tmux` in
`src/gobby/install/shared/config/config.yaml` (and any overlay that copies it).
Running native terminals finish in place — symmetric to the forward migration.
Do not kill live native PTYs as part of the rollback.

## Landing worktree

- **worktree registered**: `0.5.0-test` (gobby-worktrees id `d2a661ee`) at
  `~/.gobby/worktrees/gobby/0.5.0-test`, branched from `0.5.0` at `e19caa9a9f`.
- **merge provenance**: `wt-task-20255-m4` (`518cec5c41`, 25 commits, merge-base
  `b89f371a15`) was merged once with `git merge --no-ff`; the merge commit's parents
  are `e19caa9a9f` and `518cec5c41`. Resolution rules: 0.5.0 behaviour wins,
  worktree structure wins (`TerminalRuntime`, `terminals` rows,
  `agent_runs.terminal_id`, backend-neutral WS messages), pins stay at schema 407
  until migration 408 lands, tests take the union with 0.5.0 assertions ported to
  the renamed seams (`manager_for_terminal_context`, `snapshot_lines`,
  `dispatch_keys`). The tmux PTY bridge (`pty_bridge.py`, `tmux_activation.py`,
  `history.py`, `alt_screen.py`) is superseded by the gterm host proxy; the #20805
  no-op-resize guard now lives in
  `src/gobby/servers/websocket/terminal_ws.py::_handle_terminal_resize`.

## Guard set G

Every leaf of the landing epic closes against this set, run from the `0.5.0-test`
root with `DATABASE_URL` pointed at the isolated test hub
(`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
`GOBBY_TEST_PROTECT=1`:

1. `uv run pytest tests/test_runner_lifecycle_restart_replay.py tests/agents/test_resume_executor.py tests/agents/test_spawn_executor.py tests/agents/test_tmux.py tests/agents/test_lifecycle_monitor.py tests/agents/test_capture_consumers.py tests/config/test_runtime_config_contract.py tests/config/test_terminals.py tests/cli/test_install_setup_gterm.py tests/gterminal/test_vendor_layer.py tests/mcp_proxy/tools/sessions/test_terminal.py tests/mcp_proxy/tools/sessions/test_terminal_clear.py tests/mcp_proxy/tools/test_spawn_agent_speed.py tests/servers/test_tmux_mixin.py tests/servers/test_admin_health.py tests/install/test_version_pins.py tests/install/test_distribution.py tests/tasks/test_validation_evidence.py`
2. `uv run pytest tests/terminals tests/storage/test_terminals.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_rename.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_native_web_proxy.py tests/servers/test_attention_respond.py tests/mcp_proxy/test_sessions_terminal_tools.py` (DB-backed; run with `GOBBY_POSTGRES_TEST_DSN` exported)
3. `cargo build -p gobby-terminal --release --features vt-engine && cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings && cargo nextest run -p gobby-terminal -p gobby-client`
4. `cargo nextest run -p gobby-core -p gobby-daemon` (schema identity and grant pins)
5. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/ && uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
6. `cd web && npx vitest run src/hooks src/components/activity`
7. Host leak check: the set of `gterm host` PIDs after groups 2–3 equals the set
   before, and no surviving `gterm host` references a state directory the run
   created.

Carve-outs are explicit and end when their owner closes. At 1.1 close, group 2 and
group 4's schema-identity tests are carved out (owner 1.2: the installed `gdaemon`
is at schema 407, so `agent_runs.terminal_id` and the `terminals` table are absent
from the test hub — `psycopg.errors.UndefinedColumn: column ar.terminal_id does not
exist`). At 1.2 close, the nine red tests named in 1.4 and
`tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`
(owner 1.3) are carved out. A carved-out test must fail for the behavioural reason
recorded at `518cec5c41` (an assertion or mock-call failure), never at collection.
