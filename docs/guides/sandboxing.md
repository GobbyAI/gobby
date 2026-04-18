# Sandbox Configuration for Spawned Agents

This guide explains how to enable OS-level sandbox isolation when spawning agents through Gobby.

## Overview

Gobby supports opt-in sandbox configuration for spawned agents. When enabled, Gobby passes the appropriate flags to each CLI's built-in sandbox implementation. The actual sandboxing is handled by the CLI itself (Claude Code, Codex, or Gemini CLI) - Gobby just configures and passes the right parameters.

**Key point**: Sandboxing is disabled by default to preserve existing behavior. You must explicitly enable it.

## Enabling Sandbox via MCP Tool

When using the `spawn_agent` MCP tool, you can enable sandboxing with these parameters:

```python
spawn_agent(
    prompt="Implement the feature",
    sandbox=True,                    # Enable sandbox
    sandbox_mode="permissive",       # "permissive" or "restrictive"
    sandbox_allow_network=True,      # Allow network access (default: True)
    sandbox_extra_paths=["/data"],   # Additional writable paths
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sandbox` | bool | `None` | Enable/disable sandbox. `None` inherits from agent definition |
| `sandbox_mode` | str | `"permissive"` | Strictness level: `"permissive"` or `"restrictive"` |
| `sandbox_allow_network` | bool | `True` | Allow external network access |
| `sandbox_extra_paths` | list | `[]` | Additional paths for write access |

## Enabling Sandbox via Agent Definition

You can configure sandbox settings in agent YAML definitions:

```yaml
# .gobby/agents/my-sandboxed-agent.yaml
name: my-sandboxed-agent
description: Agent with sandbox enabled

mode: headless
provider: claude

sandbox:
  enabled: true
  mode: permissive
  allow_network: true
  extra_read_paths: []
  extra_write_paths: []
```

### Built-in Example

Gobby includes a `sandboxed` agent definition with sandbox enabled:

```yaml
# src/gobby/install/shared/agents/sandboxed.yaml
name: sandboxed
description: |
  Agent that runs in an OS-level sandbox.
  Uses the CLI's built-in sandbox for isolation.

mode: headless
provider: claude
sandbox:
  enabled: true
  mode: permissive
  allow_network: true
```

Use it with: `spawn_agent(prompt="...", agent="sandboxed")`

## CLI-Specific Sandbox Behavior

Each CLI implements sandboxing differently:

### Claude Code

Claude Code uses the `--settings` flag with a JSON configuration:

```bash
claude --settings '{"sandbox":{"enabled":true,"autoAllowBashIfSandboxed":true}}'
```

- Sandbox is all-or-nothing (enabled/disabled)
- `autoAllowBashIfSandboxed: true` auto-approves bash commands when sandboxed
- Network to localhost (Gobby daemon) is always allowed

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
SEATBELT_PROFILE=restrictive-closed gemini -s
```

For terminal-spawned Gemini/Qwen sessions, Gobby still uses that CLI-level
`-s` path. Daemon-owned Gemini/Qwen ACP web chat is different: Gobby does not
launch the ACP subprocess with daemon-applied Seatbelt flags because full-process
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
- Better security but may break some operations

## Limitations and Caveats

1. **CLI must support sandboxing**: The sandbox feature only works if the underlying CLI supports it. Unsupported CLIs will ignore sandbox configuration.

2. **Platform-specific**: Some sandbox features are platform-specific (e.g., Gemini's SEATBELT_PROFILE is macOS-only).

3. **Gobby daemon access**: The sandbox always allows localhost access to the Gobby daemon port (60887 by default) for MCP communication.

4. **Extra paths are CLI-dependent**: Not all CLIs support `extra_read_paths` or `extra_write_paths`. Codex supports `--add-dir`, but other CLIs may ignore extra paths.

5. **Sandbox is not a security boundary**: The CLI sandboxes provide isolation but should not be considered a hard security boundary. They help prevent accidental damage, not malicious actors.

## Example: Spawning a Sandboxed Agent

```python
# Via MCP tool with explicit sandbox params
result = spawn_agent(
    prompt="Refactor the authentication module",
    isolation="worktree",
    sandbox=True,
    sandbox_mode="permissive",
)

# Via agent definition
result = spawn_agent(
    prompt="Refactor the authentication module",
    agent="sandboxed",
    isolation="worktree",
)
```
