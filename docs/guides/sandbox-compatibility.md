# Sandbox Compatibility

This guide is the internal compatibility reference for Gobby's daemon-owned
sandbox model and `ghook --diagnose` contract. It stays separate from
[sandboxing.md](./sandboxing.md) because that guide explains operator-facing
configuration, while this guide owns the test matrix that proves runtime
translation and installed hook binaries still agree.

The compatibility surface has two layers:

1. Runtime behavior that Gobby owns directly for web chat and spawned agents.
2. Local and public-artifact checks for the installed `ghook` and CLI binaries.

The hook runner names below use provider/runtime hook labels because they test
installed binary compatibility. Workflow rules are authored against semantic
events such as `turn_start` and `turn_end`; raw lifecycle labels such as
`before_agent`, `after_agent`, and `stop` are normalized runtime details, not
the primary rule-authoring API.

The local runner suite assumes the relevant CLI binaries and a compatible
`ghook` are already installed on the machine. The public-artifact validator is
opt-in: it installs a named released `ghook` from GitHub Releases or crates.io
inside a temporary `HOME`, then runs the same diagnose contract against that
installed binary.

## Current Contract

Gobby owns sandbox policy at the daemon layer. The provider-specific CLI flags
are implementation details produced from the daemon model.

- `web_chat_sandbox` controls daemon-owned web chat runtimes and defaults to
  enabled.
- `agent_sandbox` controls daemon-owned spawned agent runtimes and defaults to
  enabled.
- Both config fields expose only `enabled`, `extra_read_paths`, and
  `extra_write_paths`. The daemon-owned resolver fixes `mode` to `permissive`,
  enables network access, and preserves explicit extra read/write paths.
- Web chat policy is snapshotted at runtime-manager startup and tracked by
  `sandbox_policy_hash`; a mismatch forces continue-in-new-chat instead of SDK
  resume.
- `compute_sandbox_paths()` always keeps the active workspace writable and
  always grants read access to `~/.gobby` so sandboxed runtimes can resolve
  required daemon state such as `machine_id`.
- Linked worktree Git metadata directories from `git rev-parse --git-dir` and
  `--git-common-dir` are writable so sandboxed agents can commit from worktree
  isolation.
- Repo, worktree, and clone sessions inherit the same path rule: the launched
  workspace root is writable; extra paths must be explicitly added.
- Spawned Gemini/Qwen agents pass writable paths outside the workspace as
  repeated `--include-directories` arguments alongside `-s` and
  `SEATBELT_PROFILE`.
- Agent process termination is separate from sandbox compatibility. Spawned
  automation must still call `end_agent_run` to release agent-run resources.

## Coverage Map

These tests cover daemon-owned runtime behavior:

- `tests/config/test_daemon_sandbox.py`
  Verifies daemon config defaults and the supported override shape for
  `web_chat_sandbox` and `agent_sandbox`.
- `tests/servers/websocket/chat/test_runtime_manager.py`
  Verifies daemon-owned web chat defaults, provider translation layers, Codex
  app-server thread sandboxing, and the Gemini/Qwen ACP startup caveat.
- `tests/servers/test_session_control.py`
  Verifies policy-hash mismatch behavior during continue-in-chat.
- `tests/agents/test_sandbox.py`
  Verifies resolved sandbox paths, including workspace write access and
  required `~/.gobby` readability, plus linked-worktree Git metadata write
  access.

These tests cover the local hook binary contract:

- `tests/integration/sandbox/test_runner_infrastructure.py`
  Verifies the shared runner wiring and schema location.
- `tests/integration/sandbox/test_diagnose_schema.py`
  Validates live `ghook --diagnose` output against the mirrored schema.
- `tests/integration/sandbox/run_{claude,codex,gemini,qwen}_sandbox.py`
  Verifies the installed Gobby-managed hook command rewrites cleanly into the
  current `ghook --diagnose` branch for each supported CLI.
- `tests/integration/sandbox/test_public_ghook_install.py`
  Installs a released public `ghook` artifact in a temporary home directory and
  runs the same diagnose matrix against it.

## Runtime Matrix

| Surface | CLI | Current mapping | Key invariant |
| --- | --- | --- | --- |
| Web chat | Claude | `--settings` sandbox JSON | Resume blocked on policy-hash mismatch |
| Web chat | Codex | Codex app-server sandbox policy derived from daemon config | Default daemon-owned sandbox stays enabled |
| Web chat | Gemini | Shared ACP backend; daemon policy is tracked, but the ACP process is not wrapped in Gobby Seatbelt | ACP startup remains reliable on macOS |
| Web chat | Qwen | Same ACP caveat as Gemini | ACP startup remains reliable on macOS |
| Web chat | Droid | Per-session stream-jsonrpc backend; daemon policy is tracked, but no Gobby sandbox translation is applied | Droid availability and session metadata stay consistent |
| Spawned agents | Claude | `--settings` sandbox JSON | Sandbox stays enabled without unsandboxed fallback |
| Spawned agents | Codex | `--sandbox <mode>`, `sandbox_workspace_write.network_access`, plus `--add-dir` for extra write paths | Workspace boundary follows repo/worktree/clone root and loopback services stay reachable when network is enabled |
| Spawned agents | Gemini | `-s` plus `SEATBELT_PROFILE`; external write paths use repeated `--include-directories` | Workspace boundary follows repo/worktree/clone root |
| Spawned agents | Qwen | Same Gemini-compatible `-s`, `SEATBELT_PROFILE`, and `--include-directories` contract | Workspace boundary follows repo/worktree/clone root |
| Spawned agents | Droid | No daemon sandbox resolver; Droid uses its own `droid exec --auto high` permission path | Sandbox state is recorded, but no provider sandbox flags are emitted |

Claude's sandbox payload is intentionally conservative: Gobby enables the
sandbox, uses managed permission rules only, disables unsandboxed command
fallback, allows loopback domains for local Gobby services, and does not invent
undocumented outbound-network wildcard settings.
Codex maps permissive daemon mode to `workspace-write` and restrictive mode to
`read-only`; spawned Codex agents also force workspace-write network access from
daemon policy so local Gobby services, including Postgres, are reachable when
network is enabled. Gemini and Qwen share the Seatbelt profile naming contract:
`permissive-open`, `permissive-proxied`, `restrictive-open`, or
`restrictive-proxied`. That Seatbelt contract applies to spawned Gemini/Qwen
agents and hook-binary diagnostics; daemon-owned Gemini/Qwen web chat does not
launch ACP under full-process Seatbelt. Spawned Gemini/Qwen agents also pass
the external subset of resolved write paths through `--include-directories`;
the built-in Seatbelt profiles support up to five include directories.

## Running The Local Compatibility Suite

Run the sandbox package explicitly:

```bash
uv run pytest tests/integration/sandbox/ -v --run-sandbox
```

Without `--run-sandbox`, pytest skips the entire package so the suite never
bleeds into normal validation or pre-push flows.

Useful focused commands:

```bash
uv run pytest tests/integration/sandbox/test_runner_infrastructure.py -v --run-sandbox
uv run pytest tests/integration/sandbox/run_codex_sandbox.py --collect-only
uv run mypy tests/integration/sandbox
```

## Running Public `ghook` Artifact Validation

Use the public-artifact validator when you want to prove the released
`gobby-hooks` package installs and behaves correctly through Gobby's own
installer path. The version is intentionally supplied by the caller so this
test can validate any released artifact without editing the test.

GitHub Releases:

```bash
GOBBY_INSTALL_GHOOK_VERSION=0.1.1 \
GOBBY_INSTALL_GHOOK_METHOD=github \
uv run pytest tests/integration/sandbox/test_public_ghook_install.py -v --run-sandbox
```

crates.io via `cargo-binstall`:

```bash
GOBBY_INSTALL_GHOOK_VERSION=0.1.1 \
GOBBY_INSTALL_GHOOK_METHOD=cargo-binstall \
uv run pytest tests/integration/sandbox/test_public_ghook_install.py -v --run-sandbox
```

crates.io via `cargo install`:

```bash
GOBBY_INSTALL_GHOOK_VERSION=0.1.1 \
GOBBY_INSTALL_GHOOK_METHOD=cargo-install \
uv run pytest tests/integration/sandbox/test_public_ghook_install.py -v --run-sandbox
```

The validator installs into an isolated temporary `HOME`, checks the
resulting `~/.gobby/bin/ghook` and stamp files, and then runs the live
`ghook --diagnose` matrix for Claude, Codex, Gemini, and Qwen.

## Regenerating Observations

The runner suite uses the same Gobby-managed hook command that installers
prefer locally today, then rewrites the `--gobby-owned` branch into
`--diagnose`. That keeps the check aligned to the currently installed `ghook`
binary and the mirrored `schemas/diagnose-output.v2.schema.json` contract.
The provider hook names in these runners are compatibility inputs only; rule
templates should continue to target semantic workflow events.

If the Rust-side diagnose schema changes:

1. Mirror the new active schema into `schemas/` and keep older versioned schemas
   frozen for compatibility.
2. Update the runner expectations in `tests/integration/sandbox/runner.py`.
3. Re-run `uv run pytest tests/integration/sandbox/ -v --run-sandbox`.

_Last verified: 2026-05-19_
