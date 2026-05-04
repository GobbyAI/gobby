# Gobby CLI Commands

Complete reference for all Gobby CLI commands.

## Global Options

```bash
gobby [--config PATH] <command>
```

| Option | Description |
|--------|-------------|
| `--config` | Path to custom configuration file |

## Command Groups

| Group | Description |
|-------|-------------|
| `agents` | Manage subagent runs |
| `clones` | Manage git clones for parallel development |
| `cron` | Manage scheduled jobs and orchestration ticks |
| `github` | GitHub integration commands |
| `hooks` | Manage hook system configuration |
| `linear` | Linear integration commands |
| `mcp-proxy` | Manage MCP proxy servers and tools |
| `memory` | Manage persistent memories |
| `merge` | AI-powered merge conflict resolution |
| `pipelines` | Manage pipeline definitions and executions |
| `plugins` | Manage Python hook plugins |
| `projects` | Manage Gobby projects |
| `rules` | Manage standalone workflow rules |
| `sessions` | Manage Gobby sessions |
| `skills` | Manage Gobby skills |
| `tasks` | Manage development tasks |
| `webhooks` | Manage webhook endpoints |
| `workflows` | Manage Gobby workflows |
| `worktrees` | Manage git worktrees |

---

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
- Starts HTTP server (default: port 60887)
- Starts WebSocket server (default: port 60888)
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

### `gobby status`

Show daemon status and information.

```bash
gobby status
```

Displays running state, PID, uptime, ports, and log locations.

---

## Project Management

### `gobby init`

Initialize a new Gobby project in the current directory.

```bash
gobby init [--name NAME] [--github-url URL] [-C PATH]
```

| Option | Description |
|--------|-------------|
| `--name` | Project name (auto-detected from directory) |
| `--github-url` | GitHub repository URL (auto-detected from git remote) |
| `-C`, `--path` | Target directory (default: current directory) |

Creates `.gobby/project.json`.

### `gobby install`

Install Gobby hooks to AI coding CLIs and Git.

```bash
gobby install [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--claude` | Install Claude Code hooks only |
| `--gemini` | Install Gemini CLI hooks only |
| `--codex` | Install Codex hooks only |
| `--all` | Install to all detected CLIs (default) |
| `-C`, `--path` | Target directory (default: current directory) |

Auto-detects installed CLIs and installs appropriate hooks. Also installs Git hooks for task sync (pre-commit, post-merge, post-checkout).

**What gets installed:**

| CLI | Hook Location | Config File |
|-----|--------------|-------------|
| Claude Code | `.claude/hooks/` | `.claude/settings.json` |
| Gemini CLI | `.gemini/hooks/` | `.gemini/settings.json` |
| Codex | `.codex/hooks/` | `.codex/hooks.json` |

### `gobby uninstall`

Remove Gobby hooks from AI coding CLIs.

```bash
gobby uninstall [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--claude` | Remove Claude Code hooks only |
| `--gemini` | Remove Gemini CLI hooks only |
| `--codex` | Remove Codex hooks only |
| `--all` | Remove from all CLIs (default) |

### `gobby mcp-server`

Run the stdio MCP server for MCP-capable CLI integrations.

```bash
gobby mcp-server
```

Point your client at `gobby mcp-server` using that client's MCP registration
flow. The exact command differs between Claude, Codex, Gemini, and other
integrations.

### `gobby ui`

Launch the Gobby TUI dashboard.

```bash
gobby ui
```

---

## Task Management

### `gobby tasks list`

List tasks with optional filters.

```bash
gobby tasks list [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--status STATUS` | Filter by status: `open`, `in_progress`, `review`, `closed`, `blocked` |
| `--assignee NAME` | Filter by assignee |
| `--ready` | Show only ready tasks (no blocking dependencies) |
| `--limit N` | Maximum tasks to show (default: 50) |
| `--json` | Output as JSON |

### `gobby tasks create`

Create a new task.

```bash
gobby tasks create TITLE [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-d`, `--description` | Task description |
| `-p`, `--priority` | Priority: 1=High, 2=Medium (default), 3=Low |
| `-t`, `--type` | Task type: task, bug, feature, epic, chore, docs |

### `gobby tasks show`

Show details for a task.

```bash
gobby tasks show TASK_ID
```

### `gobby tasks update`

Update task fields.

```bash
gobby tasks update TASK_ID [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--title` | New title |
| `--status` | New status |
| `--priority` | New priority (1-3) |
| `--assignee` | New assignee |

### `gobby tasks close`

Close one or more tasks.

```bash
gobby tasks close TASK_ID [--reason REASON]
```

### `gobby tasks reopen`

Reopen a closed or review task.

```bash
gobby tasks reopen TASK_ID
```

### `gobby tasks delete`

Delete one or more tasks.

```bash
gobby tasks delete TASK_ID [--cascade]
```

| Option | Description |
|--------|-------------|
| `--cascade` | Also delete child tasks |

### `gobby tasks ready`

List tasks with no unresolved blocking dependencies.

```bash
gobby tasks ready [--limit N]
```

### `gobby tasks blocked`

List blocked tasks with what blocks them.

```bash
gobby tasks blocked
```

### `gobby tasks suggest`

Suggest the next task to work on based on priority and readiness.

```bash
gobby tasks suggest
```

### `gobby tasks search`

Search tasks using semantic TF-IDF search.

```bash
gobby tasks search QUERY [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--status` | Filter by status |
| `--type` | Filter by type |
| `--limit N` | Max results |
| `--min-score F` | Minimum relevance score |
| `--all-projects` | Search across all projects |
| `--json` | Output as JSON |

### `gobby tasks reindex`

Rebuild the task search index.

```bash
gobby tasks reindex [--all-projects]
```

### `gobby tasks sync`

Sync tasks with `.gobby/tasks.jsonl`.

```bash
gobby tasks sync [--import] [--export]
```

| Option | Description |
|--------|-------------|
| `--import` | Import tasks from JSONL file |
| `--export` | Export tasks to JSONL file |

Without flags, performs bidirectional sync.

### `gobby tasks stats`

Show task statistics.

```bash
gobby tasks stats
```

### `gobby tasks dep`

Manage task dependencies.

```bash
gobby tasks dep add TASK BLOCKER      # Add dependency
gobby tasks dep remove TASK BLOCKER   # Remove dependency
gobby tasks dep tree TASK             # Show dependency tree
gobby tasks dep cycles                # Detect circular dependencies
```

### `gobby tasks label`

Manage task labels.

```bash
gobby tasks label add TASK LABEL      # Add label
gobby tasks label remove TASK LABEL   # Remove label
```

### `gobby tasks commit`

Manage commit links for tasks.

```bash
gobby tasks commit link TASK SHA      # Link commit to task
gobby tasks commit unlink TASK SHA    # Unlink commit
gobby tasks commit auto               # Auto-link commits by message
```

### `gobby tasks diff`

Show diff for all commits linked to a task.

```bash
gobby tasks diff TASK_ID
```

### `gobby tasks validate`

Validate a task.

```bash
gobby tasks validate TASK_ID
```

### `gobby tasks generate-criteria`

Generate validation criteria for a task.

```bash
gobby tasks generate-criteria TASK_ID
gobby tasks generate-criteria --all   # Generate for all open tasks
```

### `gobby tasks validation-history`

View or clear validation history for a task.

```bash
gobby tasks validation-history TASK_ID
gobby tasks validation-history TASK_ID --clear
```

### `gobby tasks de-escalate`

Return an escalated task to open status.

```bash
gobby tasks de-escalate TASK_ID
```

### `gobby tasks complexity`

Analyze task complexity based on subtasks or description.

```bash
gobby tasks complexity TASK_ID
gobby tasks complexity --all
```

### `gobby tasks doctor`

Validate task data integrity.

```bash
gobby tasks doctor
```

### `gobby tasks clean`

Fix data integrity issues (remove orphans).

```bash
gobby tasks clean
```

### `gobby tasks import`

Import tasks from external sources.

```bash
gobby tasks import FILE
```

### `gobby tasks compact`

Task compaction commands.

```bash
gobby tasks compact
```

---

## Session Management

### `gobby sessions list`

List sessions with optional filtering.

```bash
gobby sessions list [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--project REF` | Filter by project name or UUID |
| `--status STATUS` | Filter by status (`active`, `completed`, `handoff_ready`) |
| `--limit N` | Max sessions to show (default: 20) |
| `--json` | Output as JSON |

### `gobby sessions show`

Show details for a session.

```bash
gobby sessions show SESSION_ID
```

### `gobby sessions messages`

Show messages (transcript) for a session.

```bash
gobby sessions messages SESSION_ID [--limit N] [--role ROLE]
```

### `gobby sessions search`

Search messages across sessions.

```bash
gobby sessions search QUERY [OPTIONS]
```

### `gobby sessions stats`

Show session statistics.

```bash
gobby sessions stats
```

### `gobby sessions create-handoff`

Create handoff context for a session.

```bash
gobby sessions create-handoff SESSION_ID
```

### `gobby sessions delete`

Delete a session.

```bash
gobby sessions delete SESSION_ID
```

---

## Agent Management

### `gobby agents spawn`

Spawn a new agent with the given prompt.

```bash
gobby agents spawn "PROMPT" --session SESSION_ID [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--session` | Parent session ID (required) |
| `--workflow` | Workflow to activate |
| `--task` | Task ID to link |
| `--terminal` | Terminal mode: `auto`, `ghostty`, `iterm`, `kitty`, `wezterm`, `terminal` |
| `--provider` | Provider override |
| `--model` | Model override |
| `--timeout` | Execution timeout |
| `--max-turns` | Maximum turns |
| `--context` | Context source for the child session |
| `--json` | Output as JSON |

### `gobby agents list`

List agent definitions.

```bash
gobby agents list [--enabled | --disabled] [--surface SURFACE] [--json]
```

### `gobby agents show`

Show details for an agent definition.

```bash
gobby agents show NAME [--json]
```

### `gobby agents runs list`

List agent runs.

```bash
gobby agents runs list [--session SESSION_ID] [--status STATUS] [--limit N] [--json]
```

### `gobby agents runs show`

Show details for an agent run.

```bash
gobby agents runs show RUN_ID [--json]
```

### `gobby agents status`

Check status of an agent run.

```bash
gobby agents status RUN_ID
```

### `gobby agents stop`

Stop a running agent (marks as cancelled, does not kill process).

```bash
gobby agents stop RUN_ID
```

### `gobby agents kill`

Kill a running agent process.

```bash
gobby agents kill RUN_ID
```

### `gobby agents stats`

Show agent run statistics.

```bash
gobby agents stats
```

### `gobby agents cleanup`

Clean up stale agent runs.

```bash
gobby agents cleanup
```

---

## Memory Management

### `gobby memory create`

Create a new memory.

```bash
gobby memory create "CONTENT" [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--type` | Memory type: fact, preference, pattern, context |
| `--importance` | Importance level 0.0-1.0 |
| `--tags` | Comma-separated tags |
| `--global` | Create as global memory |

### `gobby memory list`

List all memories with optional filtering.

```bash
gobby memory list [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--type` | Filter by type |
| `--min-importance` | Minimum importance |
| `--limit N` | Max results |
| `--include-global` | Include global memories |
| `--tags-all` | Require ALL specified tags |
| `--tags-any` | Require ANY specified tags |
| `--tags-none` | Exclude memories with tags |

### `gobby memory recall`

Retrieve memories with optional tag filtering.

```bash
gobby memory recall [QUERY] [OPTIONS]
```

### `gobby memory show`

Show details of a specific memory.

```bash
gobby memory show MEMORY_ID
```

### `gobby memory update`

Update an existing memory.

```bash
gobby memory update MEMORY_ID [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--content` | New content |
| `--importance` | New importance |
| `--tags` | New tags |

### `gobby memory delete`

Delete a memory by ID.

```bash
gobby memory delete MEMORY_ID
```

### `gobby memory export`

Export memories as markdown.

```bash
gobby memory export [--output FILE]
```

### `gobby memory stats`

Show memory system statistics.

```bash
gobby memory stats
```

---

## Workflow Management

### `gobby workflows list`

List available workflows.

```bash
gobby workflows list [--all] [--json]
```

### `gobby workflows show`

Show workflow details.

```bash
gobby workflows show NAME [--json]
```

### `gobby workflows check`

Validate a workflow definition without executing it.

```bash
gobby workflows check NAME [--json]
```

### `gobby workflows status`

Show current workflow state for a session.

```bash
gobby workflows status [--session ID] [--json]
```

### `gobby workflows reload`

Reload workflow definitions from disk.

```bash
gobby workflows reload
```

### `gobby workflows import`

Import a workflow from a file or URL.

```bash
gobby workflows import SOURCE [--name NAME] [--global]
```

### `gobby workflows audit`

View workflow audit log (explainability/debugging).

```bash
gobby workflows audit [--session ID] [--limit N]
```

### `gobby workflows set-var`

Set a workflow variable for the current session.

```bash
gobby workflows set-var KEY VALUE [--session ID]
```

### `gobby workflows get-var`

Get workflow variable(s) for the current session.

```bash
gobby workflows get-var [KEY] [--session ID]
```

### `gobby workflows reinstall`

Delete and reinstall workflow definitions from bundled templates.

```bash
gobby workflows reinstall [--type TYPE] [--force]
```

Current workflow CLI commands are focused on inspection, validation, import,
reload, and variable management. Runtime application of agent personas and
pipeline-driven activation happens through the MCP/runtime layer.

---

## Pipeline Management

### `gobby pipelines list`

List available pipeline definitions.

```bash
gobby pipelines list [--json]
```

### `gobby pipelines show`

Show a pipeline definition.

```bash
gobby pipelines show NAME [--json]
```

### `gobby pipelines run`

Run a pipeline by name.

```bash
gobby pipelines run NAME [-i KEY=VALUE ...] [--json]
```

### `gobby pipelines runs show`

Show execution status for a pipeline run.

```bash
gobby pipelines runs show EXECUTION_ID [--json]
```

### `gobby pipelines approve`

Approve a pipeline waiting on an approval token.

```bash
gobby pipelines approve TOKEN [--json]
```

### `gobby pipelines reject`

Reject a pipeline waiting on an approval token.

```bash
gobby pipelines reject TOKEN [--json]
```

### `gobby pipelines history`

Show execution history for one pipeline.

```bash
gobby pipelines history NAME [--limit N] [--json]
```

### `gobby pipelines runs list`

List executions across pipelines.

```bash
gobby pipelines runs list [--status STATUS] [--name NAME] [--limit N] [--json]
```

### `gobby pipelines search`

Search execution history by pipeline name or error text.

```bash
gobby pipelines search QUERY [--status STATUS] [--no-errors] [--limit N] [--json]
```

### `gobby pipelines import`

Import an external pipeline file into Gobby format.

```bash
gobby pipelines import PATH [-o OUTPUT]
```

---

## Worktree Management

### `gobby worktrees create`

Create a new worktree for parallel development.

```bash
gobby worktrees create BRANCH_NAME [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--base` | Base branch (default: current) |
| `--task` | Link to task ID |

### `gobby worktrees list`

List worktrees.

```bash
gobby worktrees list [--status STATUS]
```

### `gobby worktrees show`

Show details for a worktree.

```bash
gobby worktrees show WORKTREE_ID
```

### `gobby worktrees claim`

Claim a worktree for a session.

```bash
gobby worktrees claim WORKTREE_ID --session SESSION_ID
```

### `gobby worktrees release`

Release a worktree.

```bash
gobby worktrees release WORKTREE_ID
```

### `gobby worktrees sync`

Sync worktree with its base branch.

```bash
gobby worktrees sync WORKTREE_ID
```

### `gobby worktrees delete`

Delete a worktree.

```bash
gobby worktrees delete WORKTREE_ID
```

### `gobby worktrees stale`

Detect stale worktrees.

```bash
gobby worktrees stale [--days N]
```

### `gobby worktrees cleanup`

Clean up stale worktrees.

```bash
gobby worktrees cleanup [--days N] [--dry-run]
```

### `gobby worktrees stats`

Show worktree statistics.

```bash
gobby worktrees stats
```

---

## Clone Management

### `gobby clones create`

Create a new clone for parallel development.

```bash
gobby clones create [--branch NAME] [--task TASK_ID]
```

### `gobby clones list`

List clones.

```bash
gobby clones list [--status STATUS]
```

### `gobby clones delete`

Delete a clone.

```bash
gobby clones delete CLONE_ID
```

### `gobby clones sync`

Sync clone with remote.

```bash
gobby clones sync CLONE_ID
```

### `gobby clones merge`

Merge clone branch to target branch.

```bash
gobby clones merge CLONE_ID [--target BRANCH]
```

### `gobby clones spawn`

Spawn an agent to work in a clone.

```bash
gobby clones spawn CLONE_ID "PROMPT" [OPTIONS]
```

---

## Merge Operations

### `gobby merge start`

Start a merge operation with AI-powered conflict resolution.

```bash
gobby merge start SOURCE_BRANCH [--target TARGET_BRANCH]
```

### `gobby merge status`

Show the status of current merge operation.

```bash
gobby merge status
```

### `gobby merge resolve`

Resolve a specific file conflict.

```bash
gobby merge resolve FILE_PATH [--ai] [--ours] [--theirs]
```

### `gobby merge apply`

Apply resolved changes and complete the merge.

```bash
gobby merge apply
```

### `gobby merge abort`

Abort the current merge operation.

```bash
gobby merge abort
```

---

## Skill Management

### `gobby skills list`

List installed skills.

```bash
gobby skills list [--category CAT] [--enabled/--disabled]
```

### `gobby skills show`

Show details of a specific skill.

```bash
gobby skills show NAME
```

### `gobby skills install`

Install a skill from a source.

```bash
gobby skills install SOURCE [--name NAME]
```

SOURCE can be:
- Local path: `/path/to/skill` or `./skill`
- GitHub URL: `https://github.com/user/repo`
- GitHub shorthand: `user/repo`

### `gobby skills remove`

Remove an installed skill.

```bash
gobby skills remove NAME
```

### `gobby skills update`

Update an installed skill from its source.

```bash
gobby skills update NAME
```

### `gobby skills enable`

Enable a skill.

```bash
gobby skills enable NAME
```

### `gobby skills disable`

Disable a skill.

```bash
gobby skills disable NAME
```

### `gobby skills init`

Initialize skills directory for the current project.

```bash
gobby skills init
```

### `gobby skills new`

Create a new skill scaffold.

```bash
gobby skills new NAME [--category CAT]
```

### `gobby skills validate`

Validate a SKILL.md file against the Agent Skills specification.

```bash
gobby skills validate PATH
```

### `gobby skills doc`

Generate documentation for installed skills.

```bash
gobby skills doc [--output FILE]
```

### `gobby skills meta`

Manage skill metadata fields.

```bash
gobby skills meta NAME --set KEY=VALUE
gobby skills meta NAME --get KEY
```

---

## Dispatch Entry Points

Current task automation starts with `gobby build`, which writes dispatch state
onto a plan, epic, or leaf task and kicks the state-driven dispatcher.

Use:

- `gobby build <plan_file>` to create and automate a planning epic from a plan
- `gobby build <#taskref>` to opt an existing epic or leaf into automation
- `gobby unbuild <#taskref>` to stop automation for an existing built task or
  subtree without deleting task history or build artifacts
- `gobby build --profile quick|review|full|full-yolo|auto ...` to use a
  build-time preset
- `gobby pipelines run ...` for deterministic sequences outside task dispatch
- `gobby agents ...` for direct worker management when needed

The old conductor command surface is retired. Any remaining references to it in
templates should be treated as tombstone or migration context.

---

## GitHub Integration

### `gobby github status`

Show GitHub integration status.

```bash
gobby github status
```

### `gobby github link`

Link a GitHub repo to this project.

```bash
gobby github link REPO_URL
```

### `gobby github unlink`

Remove GitHub repo link from this project.

```bash
gobby github unlink
```

### `gobby github import`

Import GitHub issues as gobby tasks.

```bash
gobby github import [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--state` | Issue state: open, closed, all |
| `--labels` | Filter by labels |
| `--limit N` | Max issues to import |

### `gobby github sync`

Sync a task to its linked GitHub issue.

```bash
gobby github sync TASK_ID
```

### `gobby github pr`

Create a GitHub PR for a task.

```bash
gobby github pr TASK_ID [OPTIONS]
```

---

## Linear Integration

### `gobby linear status`

Show Linear integration status.

```bash
gobby linear status
```

### `gobby linear link`

Link a Linear team to this project.

```bash
gobby linear link TEAM_ID
```

### `gobby linear unlink`

Remove Linear team link from this project.

```bash
gobby linear unlink
```

### `gobby linear import`

Import Linear issues as gobby tasks.

```bash
gobby linear import [OPTIONS]
```

### `gobby linear sync`

Sync a task to its linked Linear issue.

```bash
gobby linear sync TASK_ID
```

### `gobby linear create`

Create a Linear issue from a gobby task.

```bash
gobby linear create TASK_ID
```

---

## MCP Proxy Management

### `gobby mcp-proxy status`

Show MCP proxy status and health.

```bash
gobby mcp-proxy status
```

### `gobby mcp-proxy list-servers`

List all configured MCP servers.

```bash
gobby mcp-proxy list-servers
```

### `gobby mcp-proxy add-server`

Add a new MCP server configuration.

```bash
gobby mcp-proxy add-server NAME --transport TYPE [OPTIONS]
```

### `gobby mcp-proxy remove-server`

Remove an MCP server configuration.

```bash
gobby mcp-proxy remove-server NAME
```

### `gobby mcp-proxy import-server`

Import MCP server(s) from various sources.

```bash
gobby mcp-proxy import-server [OPTIONS]
```

### `gobby mcp-proxy list-tools`

List tools from MCP servers.

```bash
gobby mcp-proxy list-tools [--server NAME]
```

### `gobby mcp-proxy get-schema`

Get full schema for a specific tool.

```bash
gobby mcp-proxy get-schema SERVER TOOL
```

### `gobby mcp-proxy call-tool`

Execute a tool on an MCP server.

```bash
gobby mcp-proxy call-tool SERVER TOOL [--args JSON]
```

### `gobby mcp-proxy recommend-tools`

Get AI-powered tool recommendations for a task.

```bash
gobby mcp-proxy recommend-tools "TASK_DESCRIPTION"
```

### `gobby mcp-proxy search-tools`

Search for tools using semantic similarity.

```bash
gobby mcp-proxy search-tools "QUERY"
```

### `gobby mcp-proxy refresh`

Refresh MCP tools - detect schema changes and re-index.

```bash
gobby mcp-proxy refresh
```

---

## Project Management

### `gobby projects list`

List all known projects.

```bash
gobby projects list
```

### `gobby projects show`

Show details for a project.

```bash
gobby projects show PROJECT_ID
```

---

## ID Resolution

Gobby CLI commands support a unified ID resolution strategy for **Tasks**, **Sessions**, and **Agent Runs**.

You can reference items using:

1. **Sequence Number**: `#123` (Sessions and Tasks only, project-scoped)
2. **Full ID**: `gt-a1b2c3...` or `uuid-string`
3. **Prefix**: `gt-a1` or `a1b2` (must be unique)

If a prefix matches multiple items, all matches are displayed for disambiguation.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error or daemon not running |

---

## See Also

- [mcp-tools.md](mcp-tools.md) - MCP tool reference
- [tasks.md](tasks.md) - Task system guide
- [sessions.md](sessions.md) - Session management guide
- [memory.md](memory.md) - Memory system guide
- [rules.md](rules.md) - Rule engine guide
