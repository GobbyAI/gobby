# Sandbox Compatibility

This guide is the internal compatibility reference for Gobby's daemon-owned
sandbox model. It stays separate from [sandboxing.md](./sandboxing.md) because
that guide explains operator-facing configuration, while this guide owns the
test matrix that proves the runtime and `ghook` hook binaries still agree.

The compatibility surface has two layers:

1. Runtime behavior that Gobby owns directly for web chat and spawned agents.
2. Local and public-artifact checks for the installed `ghook` and CLI binaries.

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
- Both fields resolve through the daemon-owned sandbox model: `mode` is fixed
  to `permissive`, network access is enabled, and explicit extra read/write
  paths are preserved.
- Web chat policy changes are tracked by `sandbox_policy_hash`; a mismatch
  forces continue-in-new-chat instead of SDK resume.
- `compute_sandbox_paths()` always keeps the active workspace writable and
  always grants read access to `~/.gobby` so sandboxed runtimes can resolve
  required daemon state such as `machine_id`.
- Repo, worktree, and clone sessions inherit the same path rule: the launched
  workspace root is writable; extra paths must be explicitly added.

## Coverage Map

These tests cover daemon-owned runtime behavior:

- `tests/servers/websocket/chat/test_runtime_manager.py`
  Verifies daemon-owned web chat defaults and provider translation layers.
- `tests/servers/test_session_control.py`
  Verifies policy-hash mismatch behavior during continue-in-chat.
- `tests/agents/test_sandbox.py`
  Verifies resolved sandbox paths, including workspace write access and
  required `~/.gobby` readability.

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
| Web chat | Gemini | `-s` plus `SEATBELT_PROFILE` | Required daemon state remains readable via `~/.gobby` |
| Web chat | Qwen | Same contract as Gemini | Required daemon state remains readable via `~/.gobby` |
| Spawned agents | Claude | `--settings` sandbox JSON | Sandbox stays enabled without unsandboxed fallback |
| Spawned agents | Codex | `--sandbox <mode>` plus `--add-dir` for extra write paths | Workspace boundary follows repo/worktree/clone root |
| Spawned agents | Gemini | `-s` plus `SEATBELT_PROFILE` | Workspace boundary follows repo/worktree/clone root |
| Spawned agents | Qwen | `-s` plus `SEATBELT_PROFILE` | Workspace boundary follows repo/worktree/clone root |

Claude's sandbox payload is intentionally conservative: Gobby enables the
sandbox, uses managed permission rules only, disables unsandboxed command
fallback, and does not invent undocumented outbound-network wildcard settings.
Codex maps permissive daemon mode to `workspace-write` and restrictive mode to
`read-only`. Gemini and Qwen share the Seatbelt profile naming contract:
`permissive-open`, `permissive-proxied`, `restrictive-open`, or
`restrictive-proxied`.

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

If the Rust-side diagnose schema changes:

1. Mirror the new active schema into `schemas/` and keep older versioned schemas
   frozen for compatibility.
2. Update the runner expectations in `tests/integration/sandbox/runner.py`.
3. Re-run `uv run pytest tests/integration/sandbox/ -v --run-sandbox`.

_Last verified: 2026-05-04_
