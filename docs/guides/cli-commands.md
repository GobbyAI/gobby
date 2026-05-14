# Gobby CLI Commands

Source-backed reference for the current Gobby CLI surface. Command names are
registered in `src/gobby/cli/__init__.py`; command-specific flags are defined in
the corresponding module under `src/gobby/cli/`.

## Global Form

```bash
gobby [--config PATH] COMMAND [ARGS]...
```

| Option | Purpose |
| --- | --- |
| `--config PATH` | Load a custom configuration file before dispatching the command. |

Most commands that inspect daemon-backed state expect the daemon to be running.
Start it with `gobby start` and check it with `gobby status` or `gobby health`.

## Top-Level Commands

| Command | Purpose | Source |
| --- | --- | --- |
| `agents` | Manage agent definitions and agent runs. | `src/gobby/cli/agents.py` |
| `auth` | Set up or reset web UI authentication credentials. | `src/gobby/cli/auth.py` |
| `build` | Start and control lifecycle automation for plans, epics, and tasks. | `src/gobby/cli/build.py` |
| `clones` | Manage isolated clone workspaces. | `src/gobby/cli/clones.py` |
| `comms` | Manage inter-session communication channels. | `src/gobby/cli/communications.py` |
| `cron` | Manage scheduled jobs and dispatcher ticks. | `src/gobby/cli/cron.py` |
| `export` | Export project resources. | `src/gobby/cli/export_import.py` |
| `github` | Manage GitHub integration. | `src/gobby/cli/github.py` |
| `health` | Check daemon health. | `src/gobby/cli/daemon.py` |
| `hooks` | Manage hook endpoints and configuration. | `src/gobby/cli/extensions.py` |
| `import` | Import project resources. | `src/gobby/cli/export_import.py` |
| `init` | Initialize `.gobby/project.json`. | `src/gobby/cli/init.py` |
| `install` | Install Gobby integrations and hooks. | `src/gobby/cli/install.py` |
| `linear` | Manage Linear integration. | `src/gobby/cli/linear.py` |
| `mcp-proxy` | Inspect and call tools through the MCP proxy. | `src/gobby/cli/mcp_proxy.py` |
| `mcp-server` | Run the stdio MCP server. | `src/gobby/cli/mcp.py` |
| `memory` | Manage persistent memories. | `src/gobby/cli/memory.py` |
| `merge` | Manage merge assistance. | `src/gobby/cli/merge.py` |
| `pack` | Pack project context. | `src/gobby/cli/pack.py` |
| `pipelines` | Manage pipeline definitions and runs. | `src/gobby/cli/pipelines.py` |
| `plan` | Run plan utility commands. | `src/gobby/cli/plan.py` |
| `plans` | Manage DB-backed plan records. | `src/gobby/cli/plans.py` |
| `projects` | Inspect known Gobby projects. | `src/gobby/cli/projects.py` |
| `qdrant` | Manage Qdrant helper commands. | `src/gobby/cli/qdrant.py` |
| `restart` | Restart the daemon. | `src/gobby/cli/daemon.py` |
| `rules` | Manage workflow rules. | `src/gobby/cli/rules.py` |
| `secrets` | Manage stored secrets. | `src/gobby/cli/secrets.py` |
| `service` | Manage service installation/runtime helpers. | `src/gobby/cli/service.py` |
| `sessions` | Inspect stored sessions and transcripts. | `src/gobby/cli/sessions.py` |
| `setup` | Run the first-run setup wizard. | `src/gobby/cli/setup.py` |
| `skills` | Manage installed skills. | `src/gobby/cli/skills.py` |
| `start` | Start the daemon. | `src/gobby/cli/daemon.py` |
| `status` | Show daemon status. | `src/gobby/cli/daemon.py` |
| `stop` | Stop the daemon. | `src/gobby/cli/daemon.py` |
| `sync` | Sync bundled content to the database. | `src/gobby/cli/sync.py` |
| `tasks` | Manage development tasks. | `src/gobby/cli/tasks/` |
| `test-quality` | Run test quality helpers. | `src/gobby/cli/test_quality.py` |
| `tokens` | Audit token usage ledgers. | `src/gobby/cli/tokens.py` |
| `ui` | Manage and launch the web UI. | `src/gobby/cli/ui.py` |
| `uninstall` | Remove installed integrations and hooks. | `src/gobby/cli/install.py` |
| `unpack` | Unpack project context. | `src/gobby/cli/pack.py` |
| `webhooks` | Manage webhook endpoints. | `src/gobby/cli/extensions.py` |
| `workflows` | Inspect and manage workflow definitions. | `src/gobby/cli/workflows/` |
| `worktrees` | Manage isolated git worktrees. | `src/gobby/cli/worktrees.py` |

## Daemon And Setup

### `gobby start`

Start the daemon.

```bash
gobby start [--verbose]
```

| Option | Purpose |
| --- | --- |
| `--verbose` | Enable verbose startup output. |

### `gobby stop`

Stop the daemon.

```bash
gobby stop
```

### `gobby restart`

Stop and start the daemon.

```bash
gobby restart [--verbose]
```

### `gobby status`

Show daemon status, runtime information, and configured ports.

```bash
gobby status
```

### `gobby health`

Check daemon health.

```bash
gobby health
```

### `gobby init`

Create project metadata for the current or target directory.

```bash
gobby init [--name NAME] [--github-url URL] [--linear-setup | --no-linear-setup] [-C PATH]
```

| Option | Purpose |
| --- | --- |
| `--name NAME` | Set the project name. |
| `--github-url URL` | Set the GitHub repository URL. |
| `--linear-setup`, `--no-linear-setup` | Control guided Linear setup after initialization. |
| `--linear-team-id ID` | Set the Linear team used by guided setup. |
| `--linear-project-id ID` | Attach an existing Linear project during guided setup. |
| `-C`, `--path PATH` | Initialize a specific directory. |

### `gobby install` And `gobby uninstall`

Install or remove Gobby integrations.

```bash
gobby install [OPTIONS]
gobby uninstall [OPTIONS]
```

`gobby install` supports `--claude`, `--gemini`, `--codex`, `--droid`,
`--qwen`, `--hooks`, `--git-hooks`, `--all`, `--no-ext-services`,
`--neo4j-password`, `--project`, `--voice`, `--embedding-url`,
`--embedding-provider`, `--embedding-model`, `--embedding-dim`,
`--no-interactive`, and `-C, --path PATH`. `gobby uninstall` supports
`--claude`, `--gemini`, `--codex`, `--droid`, `--qwen`, `--all`, `--neo4j`,
`--volumes`, `--project`, and `-C, --path PATH`.

### `gobby mcp-server`

Run the stdio MCP server for MCP-capable clients.

```bash
gobby mcp-server
```

## Build Automation

`gobby build` starts lifecycle automation from a plan file, epic, or leaf task.
Control actions are implemented as `build` subcommands through the same Click
entry point.

```bash
gobby build [INPUT_REF] [OPTIONS]
gobby build stop [REF]
gobby build resume [REF]
gobby build clean REF [--dry-run] [--force] [--yes]
gobby build restart REF [--dry-run] [--force] [--yes] [--no-resume]
```

| Option | Purpose |
| --- | --- |
| `--quick` | Use quick build defaults. |
| `--skip-stage STAGE` | Skip one lifecycle stage; repeat for multiple stages. |
| `--stage STAGE:KEY=VALUE` | Override stage settings such as review caps. |
| `--isolation MODE` | Set build isolation to `none`, `worktree`, or `clone`. Omitted isolation defaults to `worktree`. |
| `--clone` | Shorthand for `--isolation clone`; conflicts with `--isolation none` and `--isolation worktree`. |
| `--no-merge` | Skip merge stage setup. |
| `--pr VALUE` | Configure PR delivery behavior. |
| `--target-branch BRANCH` | Override the target branch. |
| `--agent NAME` | Assign a specific agent definition. |
| `--reset-expansion-output` | Clear prior expansion output before building. |
| `--max-active-agents N` | Cap immediately active automation agents. |
| `--max-retries N` | Cap retries per build stage. |
| `--dry-run` | Preview `clean` or `restart` effects. |
| `--force` | Force destructive cleanup for `clean` or `restart`. |
| `--yes` | Confirm destructive `clean` or `restart` prompts. |
| `--no-resume` | For `restart`, reset state and leave automation paused. |

Use `gobby build stop [REF]` to pause future dispatch work for a target.

## Task Lifecycle

Task commands resolve task references such as `#123`, UUIDs, unique prefixes,
and path-style references when available.

### Listing And Inspection

```bash
gobby tasks list [OPTIONS]
gobby tasks ready [OPTIONS]
gobby tasks blocked [OPTIONS]
gobby tasks stats [OPTIONS]
gobby tasks show TASK
gobby tasks stages TASK
```

| Command | Key options |
| --- | --- |
| `tasks list` | `--active`, `--project`, `--stage`, `--state`, `--assignee`, `--claimed`, `--unclaimed`, `--ready`, `--blocked`, `--closed`, `--escalated`, `--limit`, `--group`, `--json` |
| `tasks ready` | `--limit`, `--project`, `--priority`, `--type`, `--json`, `--flat` |
| `tasks blocked` | `--limit`, `--project`, `--json` |
| `tasks stats` | `--project`, `--json` |

### Create, Update, Close

```bash
gobby tasks create TITLE [OPTIONS]
gobby tasks update TASK [OPTIONS]
gobby tasks close TASK... [OPTIONS]
gobby tasks reopen TASK [--reason REASON]
gobby tasks de-escalate TASK --reason REASON [--reset-validation]
gobby tasks delete TASKS... [OPTIONS]
```

| Command | Key options |
| --- | --- |
| `tasks create` | `--description`, `--priority`, `--type`, `--depends-on`, `--project` |
| `tasks update` | `--title`, `--priority`, `--parent`, `--task-type`, `--isolation` |
| `tasks close` | `--reason`, `--skip-validation`, `--force` |
| `tasks de-escalate` | `--reason`, `--reset-validation` |
| `tasks delete` | `--cascade`, `--yes` |

Agents should use the `gobby-tasks` MCP lifecycle tools for claims, closure, and
review transitions. The CLI remains useful for human inspection and maintenance.

### Stages And Review

```bash
gobby tasks advance TASK [--stage STAGE]
gobby tasks review TASK --submit
gobby tasks review TASK --approve
gobby tasks review TASK --reject --reason REASON
```

`advance` starts a ready current stage or completes a non-review stage.
Stages with required review must be submitted through `tasks review --submit`.

### Search, Validation, And Maintenance

```bash
gobby tasks search QUERY [OPTIONS]
gobby tasks reindex [OPTIONS]
gobby tasks validate TASK
gobby tasks validation-history TASK [--clear] [--json]
gobby tasks doctor
gobby tasks clean
gobby tasks repair-lifecycle [--task TASK] [--provenance LABEL] [--apply] [--force] [--json]
gobby tasks sync [--import] [--export] [--quiet]
```

`tasks search` supports `--type`, `--priority`, `--project`, `--all-projects`,
`--limit`, `--min-score`, and `--json`. `tasks reindex` supports
`--all-projects`.

### Dependencies, Labels, Commits, And Diffs

```bash
gobby tasks dep add TASK BLOCKER
gobby tasks dep remove TASK BLOCKER
gobby tasks dep tree TASK
gobby tasks dep cycles

gobby tasks label add TASK LABEL
gobby tasks label remove TASK LABEL

gobby tasks commit link TASK SHA
gobby tasks commit unlink TASK SHA
gobby tasks commit auto

gobby tasks diff TASK
```

### Expansion

```bash
gobby tasks expand validate-plan PLAN_FILE
gobby tasks expand compile TASK_REF [--plan-file PATH] [--provider NAME] [--model NAME] [--json-output]
gobby tasks expand apply RUN_ID [--session-id SESSION] [--json-output]
gobby tasks expand reset TASK_REF [--run-id RUN_ID] [--session-id SESSION] [--json-output]
gobby tasks expand status RUN_ID [--json-output]
gobby tasks expand resume RUN_ID [--session-id SESSION] [--json-output]
```

### Import And Compaction

```bash
gobby tasks import github URL [--limit N]
gobby tasks compact analyze [--days N]
gobby tasks compact apply --id TASK --summary SUMMARY
gobby tasks compact stats
```

## Plan Coverage And Plan Records

### `gobby plan coverage`

Evaluate a Plan-Coverage Contract document and write a manifest.

```bash
gobby plan coverage --plan PLAN.md --plan-id ID --plan-hash HASH --task-tree db \
  --root-task TASK --project-id PROJECT

gobby plan coverage --plan PLAN.md --plan-id ID --plan-hash HASH \
  --task-tree matrix-file --matrix-file MATRIX.json
```

| Option | Purpose |
| --- | --- |
| `--plan PATH` | Plan file to evaluate. |
| `--plan-id ID` | Stable plan identifier. |
| `--plan-hash HASH` | Expected plan content hash. |
| `--task-tree db|matrix-file` | Choose DB-backed task tree or matrix-file input. |
| `--root-task TASK` | Root task ref for `db` mode. |
| `--project-id PROJECT` | Project UUID for `db` mode. |
| `--matrix-file PATH` | Matrix file for `matrix-file` mode. |
| `--evidence SPEC` | Evidence source for coverage validation. |
| `--manifest PATH` | Write manifest to a specific path. |
| `--regenerate` | Regenerate an existing manifest. |

### `gobby plans`

Manage DB-backed plan records.

```bash
gobby plans list [--state active|archived] [--kind implementation|strategy] [--project PROJECT]
gobby plans show PLAN_ID [--project PROJECT]
gobby plans register PLAN_PATH [--plan-id ID] [--kind KIND] [--root-task-ref TASK] [--project PROJECT]
gobby plans validate PLAN_FILE [--project PROJECT] [--include-tests]
gobby plans archive PLAN_ID [--reason REASON] [--project PROJECT]
gobby plans review-runs PLANNING_TASK_REF
```

## MCP Proxy

The MCP proxy CLI calls the daemon HTTP API. Use it for inspection and manual
tool calls; automated agents should use MCP progressive discovery directly.

```bash
gobby mcp-proxy status [--json]
gobby mcp-proxy list-servers [--json]
gobby mcp-proxy list-tools [--server NAME] [--json]
gobby mcp-proxy get-schema SERVER TOOL
gobby mcp-proxy call-tool SERVER TOOL [--arg KEY=VALUE ...] [--json-args JSON] [--raw]
gobby mcp-proxy add-server NAME --transport http|stdio|websocket [OPTIONS]
gobby mcp-proxy remove-server NAME
gobby mcp-proxy import-server [OPTIONS]
gobby mcp-proxy recommend-tools TASK_DESCRIPTION [OPTIONS]
gobby mcp-proxy search-tools QUERY [OPTIONS]
gobby mcp-proxy refresh [OPTIONS]
```

| Command | Key options |
| --- | --- |
| `add-server` | `--transport`, `--url`, `--command`, `--args`, `--env`, `--headers`, `--disabled` |
| `import-server` | `--from-project`, `--github`, `--query`, `--server`, `--json` |
| `recommend-tools` | `--agent`, `--search-mode`, `--top-k`, `--json` |
| `search-tools` | `--top-k`, `--min-similarity`, `--server`, `--json` |
| `refresh` | `--force`, `--server`, `--json` |

## Sessions And Agents

### Sessions

```bash
gobby sessions list [--project PROJECT] [--status STATUS] [--source SOURCE] [--limit N] [--json]
gobby sessions show SESSION [--json]
gobby sessions messages SESSION [--limit N] [--role ROLE] [--offset N] [--json]
gobby sessions search QUERY [OPTIONS]
gobby sessions stats
gobby sessions create-handoff SESSION
gobby sessions delete SESSION
```

### Agents

```bash
gobby agents list [--enabled | --disabled] [--surface SURFACE] [--json]
gobby agents show NAME [--json]
gobby agents spawn PROMPT --session SESSION [OPTIONS]
gobby agents runs list [--session SESSION] [--status STATUS] [--limit N] [--json]
gobby agents runs show RUN [--json]
gobby agents status RUN
gobby agents stop RUN
gobby agents kill RUN
gobby agents stats
gobby agents cleanup
```

`agents spawn` supports `--workflow`, `--task`, `--provider`, `--model`,
`--reasoning-effort`, `--reasoning-required/--no-reasoning-required`,
`--timeout`, `--max-turns`, `--context`, and `--json`.

## Workspaces

### Worktrees

```bash
gobby worktrees create BRANCH_NAME [--base BRANCH] [--task TASK] [--json]
gobby worktrees list [--status STATUS] [--project PROJECT] [--json]
gobby worktrees show WORKTREE [--json]
gobby worktrees delete WORKTREE [--force] [--yes]
gobby worktrees claim WORKTREE --session SESSION
gobby worktrees release WORKTREE
gobby worktrees sync WORKTREE
gobby worktrees stale [--days N]
gobby worktrees cleanup [--days N] [--dry-run]
gobby worktrees stats
```

### Clones

```bash
gobby clones list [--status STATUS] [--project PROJECT] [--json]
gobby clones create BRANCH_NAME CLONE_PATH [--base BRANCH] [--task TASK] [--json]
gobby clones spawn CLONE PROMPT --parent-session-id SESSION [OPTIONS]
gobby clones sync CLONE
gobby clones merge CLONE [--target BRANCH]
gobby clones delete CLONE
```

`clones spawn` supports `--workflow`, `--reasoning-effort`,
`--reasoning-required/--no-reasoning-required`, and `--json`.

## Memory, Skills, Workflows, And Rules

### Memory

```bash
gobby memory create CONTENT [--type TYPE] [--project PROJECT]
gobby memory recall [QUERY] [--project PROJECT] [--limit N] [--tags-all TAGS] [--tags-any TAGS] [--tags-none TAGS]
gobby memory list [--type TYPE] [--limit N] [--project PROJECT] [--tags-all TAGS] [--tags-any TAGS] [--tags-none TAGS]
gobby memory show MEMORY [--project PROJECT]
gobby memory update MEMORY [--content CONTENT] [--tags TAGS] [--project PROJECT]
gobby memory delete MEMORY [--project PROJECT]
gobby memory export [--output PATH]
gobby memory stats
```

### Skills

```bash
gobby skills list [--category CATEGORY] [--tags TAGS] [--enabled | --disabled] [--limit N] [--json]
gobby skills show NAME [--json]
gobby skills install SOURCE [--project]
gobby skills remove NAME
gobby skills update NAME
gobby skills enable NAME
gobby skills disable NAME
gobby skills init
gobby skills new NAME [--category CATEGORY]
gobby skills validate PATH
gobby skills doc [--output PATH]
gobby skills meta NAME --set KEY=VALUE
gobby skills meta NAME --get KEY
```

### Workflows

```bash
gobby workflows list [--all] [--global] [--json]
gobby workflows show NAME [--json]
gobby workflows check NAME [--json]
gobby workflows status [--session SESSION] [--json]
gobby workflows reload
gobby workflows import SOURCE [--name NAME] [--global]
gobby workflows audit [--session SESSION] [--limit N]
gobby workflows set-var NAME VALUE [--session SESSION] [--json]
gobby workflows get-var [NAME] [--session SESSION] [--json]
gobby workflows reinstall [--type TYPE] [--force]
```

### Rules And Pipelines

```bash
gobby rules list
gobby rules show NAME
gobby rules enable NAME
gobby rules disable NAME
gobby rules import FILE
gobby rules export [--group GROUP]
gobby rules audit [--session SESSION] [--limit N] [--json]

gobby pipelines list [--json]
gobby pipelines show NAME [--json]
gobby pipelines run NAME [-i KEY=VALUE ...] [--json]
gobby pipelines status RUN [--json]
gobby pipelines runs list [--status STATUS] [--name NAME] [--limit N] [--offset N] [--json]
gobby pipelines runs show RUN [--json]
gobby pipelines approve TOKEN [--json]
gobby pipelines reject TOKEN [--json]
gobby pipelines history NAME [--limit N] [--offset N] [--json]
gobby pipelines search QUERY [--status STATUS] [--no-errors] [--limit N] [--offset N] [--json]
gobby pipelines import PATH [-o OUTPUT]
```

## Integrations And Resource Portability

```bash
gobby github status
gobby github link REPO_URL
gobby github unlink
gobby github import [OPTIONS]
gobby github sync TASK
gobby github pr TASK [OPTIONS]

gobby linear status
gobby linear link TEAM_ID
gobby linear unlink
gobby linear import [OPTIONS]
gobby linear sync TASK
gobby linear create TASK

gobby export workflow|agent|prompt|all [NAME] [--to PATH] [--global] [--dry-run]
gobby import workflow|agent|prompt|all [NAME] [--from PATH] [--from-project PATH]
```

## Admin And Diagnostics

```bash
gobby sync [--force] [--verify-only] [--type TYPE] [--verbose]
gobby tokens audit [--session SESSION] [--all] [--fix] [--project PROJECT]
gobby comms status
gobby comms send CHANNEL MESSAGE
gobby comms channels list
gobby comms channels add CHANNEL_TYPE NAME
gobby comms channels remove NAME
```

## ID Resolution

Task and session commands accept project-scoped sequence references such as
`#123`, full UUIDs, and unique prefixes. Task commands also accept path-style
references where the task tree has a path cache.

## Links

- [mcp-tools.md](mcp-tools.md) - MCP tool reference
- [tasks.md](tasks.md) - task system guide
- [sessions.md](sessions.md) - session management guide
- [memory.md](memory.md) - memory system guide
- [rules.md](rules.md) - rule engine guide
- [worktrees.md](worktrees.md) - worktree guide

_Last verified: 2026-05-07_
