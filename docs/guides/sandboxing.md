# Sandbox Configuration

This guide explains Gobby's daemon-owned sandbox configuration for web chat and
spawned agents. For the compatibility test matrix and public `ghook` artifact
validation, see [sandbox-compatibility.md](./sandbox-compatibility.md).

## Overview

Gobby resolves sandbox policy at the daemon layer, then translates that policy
to the provider-specific runtime surface. Two daemon config fields own the
operator-facing defaults:

- `web_chat_sandbox`
- `agent_sandbox`

Both fields default to enabled. The daemon-owned resolver fixes the effective
mode to `permissive`, enables external network access, preserves explicit extra
read/write paths, and computes the concrete paths that each runtime needs.

For every sandboxed runtime, Gobby keeps the launched workspace writable and
keeps `~/.gobby` readable so the runtime can resolve daemon state such as
`machine_id`. Worktree launches also add the resolved Git metadata directories
to the writable set so commits from a sandboxed worktree can update the real
repository metadata.

Web chat stores a `sandbox_policy_hash` with each conversation. If the daemon
policy changes after a chat was created, Gobby blocks resume and asks the user
to continue in a new chat under the current policy.

## Config Shape

Configure daemon-owned sandbox defaults in Gobby's daemon config:

```yaml
web_chat_sandbox:
  enabled: true
  extra_read_paths: []
  extra_write_paths: []

agent_sandbox:
  enabled: true
  extra_read_paths: []
  extra_write_paths: []
```

### Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Enables daemon-owned sandboxing for this surface |
| `extra_read_paths` | list | `[]` | Additional paths included in the resolved read set |
| `extra_write_paths` | list | `[]` | Additional paths included in the resolved write set |

The daemon config surface does not expose `mode` or `allow_network`. Those are
fixed by the daemon-owned resolver. The lower-level `SandboxConfig` model still
supports restrictive mode and network toggling for provider translation tests
and direct resolver use.

## Runtime Mapping

| Surface | Provider | Mapping |
| --- | --- | --- |
| Web chat | Claude | Materialized Claude settings file passed through the SDK |
| Web chat | Codex | Codex app-server `thread/start` sandbox policy |
| Web chat | Gemini | Shared ACP backend; Gobby does not wrap ACP startup in Seatbelt |
| Web chat | Qwen | Same ACP startup behavior as Gemini |
| Spawned agents | Claude | CLI `--settings <json>` |
| Spawned agents | Codex | CLI `--sandbox <mode>` plus `--add-dir` for extra write paths |
| Spawned agents | Gemini | CLI `-s` plus `SEATBELT_PROFILE` |
| Spawned agents | Qwen | Same sandbox contract as Gemini |

## Provider Details

### Claude Code

Claude receives a sandbox settings payload. Spawned agents get it through
`--settings`; web chat materializes the same overlay into the settings file
passed to the Claude SDK.

```json
{
  "allowManagedPermissionRulesOnly": true,
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": false,
    "allowUnsandboxedCommands": false,
    "excludedCommands": [],
    "network": {
      "allowUnixSockets": [],
      "allowAllUnixSockets": false,
      "allowLocalBinding": false,
      "allowedDomains": []
    },
    "enableWeakerNestedSandbox": false
  }
}
```

Gobby enables the sandbox, uses managed permission rules only, disables
unsandboxed command fallback, and leaves undocumented outbound-network wildcard
settings unset.

### Codex

Spawned Codex agents use the CLI sandbox flag:

```bash
codex --sandbox workspace-write
codex --sandbox read-only
codex --sandbox workspace-write --add-dir /extra/path
```

Daemon-owned permissive mode maps to `workspace-write`; restrictive mode in the
lower-level resolver maps to `read-only`. Gobby does not emit
`danger-full-access` for daemon-owned sandboxes. Extra writable paths become
additional `--add-dir` arguments.

Codex web chat is different because it uses the app server. Gobby starts the
thread with the app-server sandbox policy string derived from the same daemon
config instead of launching a CLI process with `--sandbox`.

### Gemini And Qwen

Spawned Gemini and Qwen agents use the CLI `-s` flag and `SEATBELT_PROFILE`
environment variable on macOS:

```bash
SEATBELT_PROFILE=permissive-open gemini -s
SEATBELT_PROFILE=restrictive-open gemini -s
SEATBELT_PROFILE=permissive-open qwen -s
SEATBELT_PROFILE=restrictive-open qwen -s
```

The lower-level resolver chooses `permissive` or `restrictive` from sandbox
mode, then chooses `open` or `proxied` from network policy. Daemon-owned config
currently resolves to `permissive-open`.

Daemon-owned Gemini/Qwen ACP web chat does not launch the shared ACP subprocess
with Gobby-managed Seatbelt flags because full-process Seatbelt blocked ACP
startup on macOS. Those sessions still use ACP's proxied filesystem model and
the upstream CLI's tool-level sandboxing, which is separate from wrapping the
entire ACP process.

## Path Resolution

`compute_sandbox_paths()` builds a concrete path policy before provider
translation:

- `write_paths` starts with the workspace, worktree, or clone root.
- Git metadata directories from `git rev-parse --git-dir --git-common-dir` are
  added when they can be resolved.
- `extra_write_paths` are appended if they are not already present.
- `read_paths` starts with `~/.gobby`, then appends `extra_read_paths`.
- The daemon port defaults to `60887` for local daemon communication.

Provider support for these resolved paths is CLI-dependent. Codex currently
uses extra write paths as `--add-dir`; other providers may consume the computed
paths indirectly through their own sandbox implementation.

## Example: Spawning A Sandboxed Agent

```python
result = spawn_agent(
    prompt="Refactor the authentication module",
    isolation="worktree",
)
```

`spawn_agent` inherits `agent_sandbox` from the daemon config. The current MCP
schema does not accept per-call sandbox parameters; the exposed parameters are
for the prompt, agent selection, isolation, branch/workspace selection,
workflow, provider/model overrides, reasoning, timeout, parent session, and
project path.

Sandbox policy is independent from agent lifecycle. A spawned agent that has
finished its workflow must still call `gobby-agents:end_agent_run` to release
its agent-run resources.

## Limitations And Caveats

1. **Provider support varies**: Gobby has resolvers for Claude, Codex, Gemini,
   and Qwen. Each provider exposes different sandbox knobs.

2. **Platform behavior varies**: Gemini and Qwen Seatbelt profiles are macOS
   behavior. Other platforms depend on the upstream CLI's sandbox support.

3. **Extra paths are provider-dependent**: The daemon computes extra read and
   write paths, but each provider decides which path categories can be
   represented in its runtime API.

4. **Sandboxing is not a hard security boundary**: CLI sandboxes reduce
   accidental damage. They are not a complete defense against malicious code or
   hostile prompts.

_Last verified: 2026-05-07_
