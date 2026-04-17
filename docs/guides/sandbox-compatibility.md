# Sandbox Compatibility

This guide is the internal reference for the daemon-owned sandbox model that
landed under the sandbox epic. It is intentionally split into two layers:

1. Runtime behavior that Gobby owns directly.
2. Local compatibility checks for the installed `ghook` + CLI binaries.

The local runner suite assumes prerelease binaries are installed on the
machine already. It does not assume GitHub Releases or crates.io are live.

## Current Contract

Gobby now owns sandbox policy at the daemon layer instead of exposing
provider-specific product settings.

- `web_chat_sandbox` controls daemon-owned web chat runtimes.
- `agent_sandbox` controls daemon-owned spawned terminal runtimes.
- Web chat policy changes are tracked by `sandbox_policy_hash`; a mismatch
  forces continue-in-new-chat instead of SDK resume.
- `compute_sandbox_paths()` always keeps the active workspace writable and
  always grants read access to `~/.gobby` so sandboxed runtimes can still
  resolve required daemon state such as `machine_id`.
- Repo, worktree, and clone sessions all inherit the same rule: the launched
  workspace root is writable; extra paths must be explicitly added.

## Coverage Map

These tests cover the daemon-owned behavior directly:

- `tests/servers/websocket/chat/test_runtime_manager.py`
  Verifies daemon-owned web-chat defaults and provider translation layers.
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
- `tests/integration/sandbox/run_{codex,claude,gemini,qwen}_sandbox.py`
  Verifies the installed Gobby-managed hook command rewrites cleanly into the
  current `ghook --diagnose` branch for each supported CLI.

## Runtime Matrix

| Surface | CLI | Current mapping | Key invariant |
| --- | --- | --- | --- |
| Web chat | Claude | Managed sandbox settings JSON | Resume blocked on policy-hash mismatch |
| Web chat | Codex | Codex app-server sandbox policy derived from daemon config | Default daemon-owned sandbox stays enabled |
| Web chat | Gemini | `-s` plus `SEATBELT_PROFILE` | Required daemon state remains readable via `~/.gobby` |
| Web chat | Qwen | Same contract as Gemini | Required daemon state remains readable via `~/.gobby` |
| Spawned agents | Claude | `--settings` sandbox payload | Agent runtime metadata records `sandbox_enabled` |
| Spawned agents | Codex | `--sandbox <mode>` | Workspace boundary follows repo/worktree/clone root |
| Spawned agents | Gemini | `-s` plus `SEATBELT_PROFILE` | Workspace boundary follows repo/worktree/clone root |
| Spawned agents | Qwen | `-s` plus `SEATBELT_PROFILE` | Workspace boundary follows repo/worktree/clone root |

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

## Regenerating Observations

The runner suite uses the same Gobby-managed hook command that installers
prefer locally today, then rewrites the `--gobby-owned` branch into
`--diagnose`. That keeps the check aligned to the currently installed `ghook`
binary and the mirrored `schemas/diagnose-output.v1.schema.json` contract.

If the Rust-side diagnose schema changes:

1. Mirror the schema into `schemas/`.
2. Update the runner expectations in `tests/integration/sandbox/runner.py`.
3. Re-run `uv run pytest tests/integration/sandbox/ -v --run-sandbox`.
