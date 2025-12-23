# Gobby CLI Commands

Complete reference for all Gobby CLI commands.

## Global Options

```bash
gobby [--config PATH] <command>
```

| Option | Description |
|--------|-------------|
| `--config` | Path to custom configuration file |

## Daemon Management

### `gobby start`

Start the Gobby daemon.

```bash
gobby start [--verbose]
```

| Option | Description |
|--------|-------------|
| `--verbose` | Enable verbose debug output |

The daemon runs in the background and:

- Initializes local storage
- Starts HTTP server (default: port 8765)
- Starts WebSocket server (default: port 8766)
- Connects to configured MCP servers

### `gobby stop`

Stop the Gobby daemon.

```bash
gobby stop
```

### `gobby restart`

Restart the daemon (stop then start).

```bash
gobby restart [--verbose]
```

| Option | Description |
|--------|-------------|
| `--verbose` | Enable verbose debug output |

### `gobby status`

Show daemon status and information.

```bash
gobby status
```

Displays:

- Running state and PID
- Uptime
- HTTP and WebSocket ports
- Log file locations

## Project Management

### `gobby init`

Initialize a new Gobby project in the current directory.

```bash
gobby init [--name NAME] [--github-url URL]
```

| Option | Description |
|--------|-------------|
| `--name` | Project name (auto-detected from directory if not provided) |
| `--github-url` | GitHub repository URL (auto-detected from git remote if not provided) |

Creates:

- `.gobby/project.json` - Project configuration

### `gobby install`

Install Gobby hooks to AI coding CLIs.

```bash
gobby install [--claude] [--gemini] [--codex] [--all]
```

| Option | Description |
|--------|-------------|
| `--claude` | Install Claude Code hooks only |
| `--gemini` | Install Gemini CLI hooks only |
| `--codex` | Configure Codex notify integration |
| `--all` | Install to all detected CLIs (default) |

By default (no flags), installs to all detected CLIs in the current project directory.

### `gobby uninstall`

Remove Gobby hooks from AI coding CLIs.

```bash
gobby uninstall [--claude] [--gemini] [--codex] [--all]
```

| Option | Description |
|--------|-------------|
| `--claude` | Uninstall from Claude Code only |
| `--gemini` | Uninstall from Gemini CLI only |
| `--codex` | Remove Codex integration |
| `--all` | Uninstall from all CLIs (default) |

## MCP Server

### `gobby mcp-server`

Run stdio MCP server for Claude Code integration.

```bash
gobby mcp-server
```

This command starts a stdio-based MCP server that:

- Auto-starts the daemon if not running
- Provides daemon lifecycle tools (start/stop/restart)
- Proxies all MCP tools from the daemon

**Usage with Claude Code:**

```bash
claude mcp add --transport stdio gobby-daemon -- gobby mcp-server
```

## Task Management

### `gobby tasks list`

List tasks with optional filters.

```bash
gobby tasks list [--status STATUS] [--assignee NAME] [--ready] [--limit N] [--json]
```

| Option | Description |
|--------|-------------|
| `--status` | Filter by status: `open`, `in_progress`, `completed`, `blocked` |
| `--assignee` | Filter by assignee |
| `--ready` | Show only ready tasks (open with no blocking dependencies) |
| `--limit` | Maximum tasks to show (default: 50) |
| `--json` | Output as JSON |

### `gobby tasks create`

Create a new task.

```bash
gobby tasks create TITLE [-d DESCRIPTION] [-p PRIORITY] [-t TYPE]
```

| Option | Description |
|--------|-------------|
| `-d`, `--description` | Task description |
| `-p`, `--priority` | Priority: 1=High, 2=Medium (default), 3=Low |
| `-t`, `--type` | Task type: task (default), bug, feature, epic |

### `gobby tasks show`

Show details for a task.

```bash
gobby tasks show TASK_ID
```

Displays full task information including title, status, priority, type, timestamps, assignee, and description.

### `gobby tasks update`

Update task fields.

```bash
gobby tasks update TASK_ID [--title TITLE] [--status STATUS] [--priority N] [--assignee NAME]
```

| Option | Description |
|--------|-------------|
| `--title` | New title |
| `--status` | New status |
| `--priority` | New priority (1-3) |
| `--assignee` | New assignee |

### `gobby tasks close`

Close a task.

```bash
gobby tasks close TASK_ID [--reason REASON]
```

| Option | Description |
|--------|-------------|
| `--reason` | Reason for closing (default: "completed") |

### `gobby tasks delete`

Delete a task (requires confirmation).

```bash
gobby tasks delete TASK_ID [--cascade]
```

| Option | Description |
|--------|-------------|
| `--cascade` | Also delete child tasks |

### `gobby tasks sync`

Sync tasks with `.gobby/tasks.jsonl`.

```bash
gobby tasks sync [--direction DIRECTION]
```

| Option | Description |
|--------|-------------|
| `--direction` | `import`, `export`, or `both` (default) |

Synchronizes tasks between SQLite database and JSONL file for git-based sharing.

## Task ID Resolution

Task commands accept either:

- **Full ID**: `gt-a1b2c3`
- **Prefix**: `gt-a1` (if unique)

If a prefix matches multiple tasks, all matches are displayed for disambiguation.

## Examples

```bash
# Start daemon with verbose logging
gobby start --verbose

# Initialize project and install hooks
gobby init
gobby install

# Create and manage tasks
gobby tasks create "Fix authentication bug" -p 1 -t bug
gobby tasks list --status open
gobby tasks list --ready              # Show tasks ready to work on
gobby tasks update gt-abc123 --status in_progress
gobby tasks close gt-abc123 --reason "Fixed in commit abc"

# Sync tasks for git collaboration
gobby tasks sync --direction export
git add .gobby/tasks.jsonl
git commit -m "Update tasks"
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error or daemon not running |

## See Also

- [MCP_TOOLS.md](MCP_TOOLS.md) - MCP tool reference
- [README.md](README.md) - Project overview
- [ROADMAP.md](ROADMAP.md) - Implementation roadmap
