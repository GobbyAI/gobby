# Sandbox Configuration

This guide explains Gobby's daemon-owned sandbox configuration for web chat and
spawned agents. For the compatibility test matrix and public `ghook` artifact
validation, see [sandbox-compatibility.md](./sandbox-compatibility.md).

## Overview

Gobby resolves sandbox policy at the daemon layer, then translates that policy
to the current provider-specific runtime surface. Web chat and spawned agents
both default to daemon-owned sandboxing enabled through these config fields:

- `web_chat_sandbox`
- `agent_sandbox`

The resolved daemon-owned policy uses permissive mode with external network
access enabled. The active workspace is writable, and `~/.gobby` is readable so
sandboxed runtimes can resolve required daemon state.

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
|-----------|------|---------|-------------|
| `enabled` | bool | `true` | Enables or disables daemon-owned sandboxing for the surface |
| `extra_read_paths` | list | `[]` | Additional paths granted read access |
| `extra_write_paths` | list | `[]` | Additional paths granted write access |

The daemon-owned resolver fixes mode to `permissive` and external network
access to enabled. The lower-level `SandboxConfig` model still supports
restrictive mode for provider translation tests and direct resolver use.

## CLI-Specific Sandbox Behavior

Each CLI implements sandboxing differently:

### Claude Code

Claude Code uses the `--settings` flag with a JSON configuration:

```bash
claude --settings '{"allowManagedPermissionRulesOnly":true,"sandbox":{"enabled":true,"autoAllowBashIfSandboxed":false,"allowUnsandboxedCommands":false}}'
```

Gobby enables the sandbox, uses managed permission rules only, disables
unsandboxed command fallback, and leaves undocumented outbound-network wildcard
settings unset.

### Codex (OpenAI)

Codex uses the `--sandbox` flag with mode selection:

```bash
# Permissive mode - can write to workspace
codex --sandbox workspace-write

# Restrictive mode - read-only
codex --sandbox read-only

# Extra writable paths
codex --sandbox workspace-write --add-dir /extra/path
```

### Gemini CLI

Gemini uses the `-s` flag and `SEATBELT_PROFILE` environment variable (macOS):

```bash
# Permissive mode
SEATBELT_PROFILE=permissive-open gemini -s

# Restrictive mode
SEATBELT_PROFILE=restrictive-open gemini -s
```

For spawned Gemini/Qwen agent sessions, Gobby still uses that CLI-level `-s`
path. Daemon-owned Gemini/Qwen ACP web chat is different: Gobby does not launch
the ACP subprocess with daemon-applied Seatbelt flags because full-process
Seatbelt blocked ACP startup on macOS. Those sessions still rely on ACP's
proxied filesystem model and Gemini's tool-level sandboxing, but that is a
different isolation layer than wrapping the entire ACP process in Gobby-managed
Seatbelt.

## Sandbox Modes

### Permissive Mode

- Allows writes to workspace directory
- Allows read access to common system paths
- Network access (if `allow_network=True`)
- Good for development and debugging

### Restrictive Mode

- Read-only access to workspace
- Minimal system path access
- Limited network (if `allow_network=True`)
- Available in the lower-level resolver model; daemon-owned defaults do not
  expose restrictive mode as an operator setting

## Limitations and Caveats

1. **CLI must support sandboxing**: The sandbox feature only works if the
   underlying CLI supports it. Unsupported CLIs ignore sandbox configuration.

2. **Platform-specific**: Some sandbox features are platform-specific, such as
   Gemini and Qwen `SEATBELT_PROFILE` handling on macOS.

3. **Gobby daemon access**: Sandboxed runtimes need access to the local daemon
   port, which defaults to `60887`.

4. **Extra paths are CLI-dependent**: Not all CLIs support every extra path
   concept. Codex supports `--add-dir` for extra write paths.

5. **Sandbox is not a security boundary**: CLI sandboxes reduce accidental
   damage. They are not a hard boundary against malicious actors.

## Example: Spawning a Sandboxed Agent

```python
result = spawn_agent(
    prompt="Refactor the authentication module",
    isolation="worktree",
)
```

`spawn_agent` inherits daemon-owned sandbox defaults. It does not accept
per-call sandbox parameters in the current MCP schema.

_Last verified: 2026-05-04_
