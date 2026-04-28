# Factory Droid CLI Integration

Gobby treats Factory Droid as a first-class CLI source. Droid hooks report
session and tool events to the local Gobby daemon, Droid sessions can call the
Gobby MCP server, and `spawn_agent` can launch Droid-backed agents in the same
task and session graph as Claude Code, Gemini CLI, Qwen CLI, and Codex.

## Installation

Install Droid first, then install Gobby's Droid hooks from the project you want
to work in:

```bash
curl -fsSL https://app.factory.ai/cli | sh
gobby start
gobby init
gobby install --droid
```

For project-local hook files instead of global hook files:

```bash
gobby install --droid --project
```

`gobby install --droid` writes Droid hook configuration, installs the shared
Gobby hook helpers, and registers the Gobby MCP server for Droid. Droid hook
routing requires `ghook` 0.4.0 or newer; if the installer warns about an older
`ghook`, run:

```bash
gobby update
gobby install --droid
```

## Supported Hook Events

Gobby installs all Droid hook events from `DROID_PASCAL_HOOK_NAMES`:

| Droid hook | Gobby event | Response handling |
|------------|-------------|-------------------|
| `PreToolUse` | `before_tool` | Tool permission decisions use `hookSpecificOutput.permissionDecision`. |
| `PostToolUse` | `after_tool` | Blocks use Droid's top-level `decision: "block"` shape. |
| `UserPromptSubmit` | `before_agent` | Blocks use Droid's top-level `decision: "block"` shape; additional context is supported. |
| `Notification` | `notification` | Non-blocking; denial reasons are surfaced as messages. |
| `Stop` | `stop` | Blocks use Droid's top-level `decision: "block"` shape. |
| `SubagentStop` | `subagent_stop` | Blocks use Droid's top-level `decision: "block"` shape. |
| `PreCompact` | `pre_compact` | Non-blocking. |
| `SessionStart` | `session_start` | Adds session metadata and injected context through Droid additional context. |
| `SessionEnd` | `session_end` | Non-blocking. |

Factory's hook documentation describes the same hook structure as commands
under a `hooks` object, and recommends absolute command paths or
`$FACTORY_PROJECT_DIR` for project-relative commands. Gobby's installer writes
absolute `ghook --gobby-owned --cli=droid --type=<HookName>` commands for each
event.

## Config Files

Gobby uses these Droid paths:

| Scope | Hooks path | MCP path |
|-------|------------|----------|
| Global | `~/.factory/hooks/hooks.json` | `~/.factory/mcp.json` |
| Project | `.factory/hooks/hooks.json` | `.factory/mcp.json` |

Factory's public hook docs currently describe hooks inside settings files:
`~/.factory/settings.json`, `.factory/settings.json`, and
`.factory/settings.local.json`. The Droid binary also exposes a hooks file path
that Gobby targets through the installer. This avoids rewriting general Droid
settings while keeping Gobby-owned hook entries easy to add and remove. If
Droid's `/hooks` screen shows hooks disabled, check both locations:

```bash
cat ~/.factory/hooks/hooks.json
cat ~/.factory/settings.json
```

Project `.factory/settings.json` or `.factory/settings.local.json` files with
an empty `hooks` value can shadow user-level hooks. The installer warns about
that case; remove the empty project `hooks` key or install with `--project`.

## MCP Registration

The installer writes a Droid MCP config entry named `gobby` in Droid's
`mcp.json` file. The entry is a stdio server that runs Gobby's MCP endpoint:

```json
{
  "mcpServers": {
    "gobby": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "gobby", "mcp-server"]
    }
  }
}
```

Factory's MCP docs describe `~/.factory/mcp.json` for user-wide MCP servers and
`.factory/mcp.json` for project-shared MCP servers. Gobby follows those paths.
Inside Droid, `/mcp` should show the `gobby` server after installation.

## Spawning Droid Agents

Use the same `spawn_agent` MCP tool as other providers and set
`provider: "droid"`:

```json
{
  "prompt": "Investigate #123 and implement the smallest complete fix.",
  "provider": "droid",
  "isolation": "worktree",
  "task_id": "#123"
}
```

Gobby launches Droid through `droid exec --input-format stream-json --cwd
<worktree> --auto high` for spawned agents. The autonomy mapping is:

| Gobby spawn mode | Droid command behavior |
|------------------|------------------------|
| Normal interactive or web-chat Droid session | `--auto low` |
| Spawned autonomous agent | `--auto high` |

Pass `model` or `reasoning_effort` to `spawn_agent` when you need a specific
Droid model or reasoning level; Gobby forwards those as `--model` and
`--reasoning-effort`.

## Token Accounting

Droid JSONL transcripts do not always carry complete token usage on each
message. Gobby's Droid transcript parser side-reads a sibling settings sidecar
with the same basename and `.settings.json` suffix, for example:

```text
.factory/logs/<session-id>.jsonl
.factory/logs/<session-id>.settings.json
```

When present, Gobby reads `model` and `tokenUsage` from that sidecar. If the
sidecar is missing, the transcript still renders, but token totals can be
partial or zero until Droid writes the settings file.

## Troubleshooting

### `droid CLI not found in PATH`

Install Droid with Factory's installer, open a new shell, and verify:

```bash
droid --version
```

Then rerun `gobby install --droid`.

### `ghook does not support droid yet`

Upgrade Gobby's hook binary and reinstall the Droid hook entries:

```bash
gobby update
gobby install --droid
```

### Hooks are installed but do not fire

Check whether Droid has hooks globally disabled:

```bash
cat ~/.factory/settings.json
```

If `hooksDisabled` is `true`, turn hooks back on from Droid's `/hooks` or
`/settings` menu. Also check project settings for an empty `hooks` value, which
can shadow global hooks.

### MCP tools are missing inside Droid

Open Droid and run `/mcp`. If `gobby` is missing, reinstall:

```bash
gobby install --droid
```

If it is present but disconnected, confirm the Gobby daemon is running:

```bash
gobby status
```

## References

- Factory CLI reference: https://docs.factory.ai/reference/cli-reference
- Factory hooks reference: https://docs.factory.ai/reference/hooks-reference
- Factory MCP configuration: https://docs.factory.ai/cli/configuration/mcp
- Factory settings reference: https://docs.factory.ai/cli/configuration/settings
