# Sandbox Compatibility

This is the verification reference for Gobby's daemon-owned sandbox contract.
Operator configuration is documented in [sandboxing.md](./sandboxing.md).

## Current Contract

- Managed terminal agents default to the pinned SRT 0.0.66 backend.
- Web chat defaults to `backend="srt"` with bounded provider, loopback, Git, and
  package-registry network access; `provider-native` is an explicit override.
- Backend selection is explicit and never downgrades on failure.
- SRT policy and violation files are generated under Gobby's private run
  directory, outside the workspace.
- The workspace and linked-worktree Git metadata are writable. The user's home
  and Gobby home are read-denied before narrow exceptions are applied.
- Provider model APIs and Gobby loopback hosts are allowed explicitly. Spawned
  agents receive scoped package-registry egress so cold per-run caches can
  resolve dependencies, including Cargo's crates.io endpoints. Git network
  access remains an independent capability.
- Provider credentials are masked and scoped to provider/API-base hosts.
- Every supported terminal provider command is wrapped once after command
  construction and before tmux creation.
- Daemon-stop resume creates a fresh policy and fails closed before spawning.
- A stale web-chat policy hash invalidates direct resume; continuation is recreated
  under the current policy snapshot.
- Web-chat provider process groups are session-owned and are terminated on detach,
  interrupt, timeout, removal, or daemon shutdown.
- Agent-run serialization exposes backend, SRT version, effective policy hash,
  and trusted violation data.

Run caches are isolated per sandbox run. A fresh Cargo cache is populated through
scoped registry egress; `cargo test --offline` succeeds after its dependencies are
present in that run cache.

## Runtime Matrix

| Surface | Provider | Default mapping | Compatibility invariant |
| --- | --- | --- | --- |
| Web chat | Claude/Codex/Qwen/Grok/Droid/AGY | SRT wraps each session-owned SDK, app-server, ACP, stream-jsonrpc, or stream-json provider process | Bounded network policy is daemon-owned; stale policy hashes invalidate resume; explicit `provider-native` overrides never fall back |
| Managed agent | Claude | SRT wraps complete `claude` argv | Claude approval/tool settings and MCP flags remain inside the wrapped argv |
| Managed agent | Codex | SRT wraps complete `codex` argv | No nested Codex OS sandbox; approval/config flags remain active |
| Managed agent | Qwen | SRT wraps complete `qwen` argv | No nested Seatbelt profile; Qwen flags and hooks remain active |
| Managed agent | Grok | SRT wraps complete `grok` argv | No nested Grok OS sandbox; headless/approval flags remain active |
| Managed agent | Droid | SRT wraps complete `droid` argv | SRT supplies the host boundary even though Droid has no provider-native renderer |
| Managed agent | AGY | SRT wraps complete `agy` argv | Interactive dispatch and terminal lifecycle use the same session-owned process boundary |

Explicit `provider-native` managed-agent mode is covered for Claude, Codex,
Qwen, and Grok. Selecting it for Droid fails closed. SRT supports both macOS
Seatbelt and Linux bubblewrap; Windows support in the pinned upstream release is
alpha and is not part of Gobby's supported compatibility gate.

## Policy Coverage

| Contract | Focused coverage |
| --- | --- |
| Backend defaults and explicit overrides | `tests/config/test_daemon_sandbox.py` |
| Canonical paths, home/Gobby denials, symlinks, linked Git metadata | `tests/agents/test_sandbox.py` |
| SRT settings schema, credential host scoping, private policy files, fail-closed network validation, installation verification | `tests/agents/test_srt_runtime.py` |
| Immutable tarball/lock installation flow | `tests/cli/test_install_setup_srt.py` |
| Once-only wrapping, Droid support, pre-tmux failure, wrapper-aware auth inference | `tests/agents/test_srt_spawn.py` |
| All managed provider command builders and native rollout renderers | `tests/agents/test_spawn_executor.py` |
| Fresh policy on daemon-stop resume and legacy sandbox-field non-replay | `tests/agents/test_resume_executor.py` |
| Run-record policy metadata and trusted violation projection | `tests/storage/test_agent_sandbox_records.py` |
| Web-chat SRT wrapping, session ownership, translation, and policy-hash resume behavior | `tests/servers/websocket/chat/test_runtime_manager.py`, `tests/servers/websocket/chat/test_agy_backend.py`, `tests/servers/test_session_control.py` |

## Tmux And Process-Tree Invariants

SRT changes the pane's top-level argv, not Gobby's lifecycle ownership. The
following established suites remain part of the compatibility gate:

| Invariant | Coverage |
| --- | --- |
| Interactive tmux creation, pane PID capture, resize, detach/reattach, and process-group cleanup | `tests/agents/test_tmux.py`, `tests/agents/test_tmux_integration.py` |
| Capture-before-kill and terminalization ordering | `tests/agents/test_capture.py`, `tests/agents/test_capture_consumers.py`, `tests/agents/test_agent_cleanup.py` |
| Daemon lifecycle reconciliation and descendant cleanup | `tests/agents/test_lifecycle_monitor.py` |
| Process-tree and aggregate memory accounting | `tests/agents/test_memory_watchdog.py` |
| Wrapper signal forwarding and inherited stdio | `src/gobby/agents/srt_runner.mjs`, validated by the SRT runner checks and host integration run |

The runner forwards `SIGINT`, `SIGTERM`, `SIGHUP`, and `SIGWINCH`; it resets SRT
after the provider exits and then preserves signal-style termination. The
provider command remains after the wrapper's `--` separator, so auth forwarding
continues to identify the real CLI rather than Node.

## Hook Binary Compatibility

The existing opt-in sandbox package validates installed `ghook` behavior. These
tests are separate from the SRT process wrapper because they prove the public
hook protocol and installed binary contract:

- `tests/integration/sandbox/test_runner_infrastructure.py`
- `tests/integration/sandbox/test_diagnose_schema.py`
- `tests/integration/sandbox/run_{claude,codex,qwen}_sandbox.py`
- `tests/integration/sandbox/test_public_ghook_install.py`

Run it explicitly:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/integration/sandbox/ -v --run-sandbox
```

Without `--run-sandbox`, pytest skips the package so local validation does not
invoke installed provider CLIs or public artifact downloads accidentally.

Useful focused commands:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/integration/sandbox/test_runner_infrastructure.py -v --run-sandbox
GOBBY_TEST_PROTECT=1 uv run pytest tests/integration/sandbox/run_codex_sandbox.py --collect-only
uv run mypy tests/integration/sandbox
```

## Public `ghook` Artifact Validation

The public-artifact validator installs a requested `gobby-hooks` release into a
temporary home and runs the same diagnose contract. The caller supplies both
version and installation source. For example:

```bash
GOBBY_INSTALL_GHOOK_VERSION=0.1.1 \
GOBBY_INSTALL_GHOOK_METHOD=github \
GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/integration/sandbox/test_public_ghook_install.py -v --run-sandbox
```

Supported methods are `github`, `cargo-binstall`, and `cargo-install`. The test
checks the isolated `~/.gobby/bin/ghook`, installation stamps, and live diagnose
matrix for Claude, Codex, and Qwen.

## Compatibility Gate

Before changing the SRT version, generated policy, runner, or provider command
boundary:

1. Update the pinned tarball checksum, npm integrity, generated lockfile, and
   Gobby receipt expectations together.
2. Run the focused policy, installer, spawn, resume, storage, tmux, capture,
   lifecycle, and memory tests above on macOS and Linux.
3. Run the opt-in host sandbox integration package where provider binaries are
   available.
4. Verify Ruff and mypy are clean and the packaged wheel contains
   `agents/srt_runner.mjs` and `install/srt-package-lock.json`.

SRT 0.0.66 cannot restrict loopback by destination port. A future pin must keep
that limitation documented or add a test proving exact port enforcement before
claiming it.

_Last verified: 2026-08-30_
