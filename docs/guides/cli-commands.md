# Gobby CLI Commands

Source-backed reference for the current Gobby CLI surface. Command names are
registered in `src/gobby/cli/__init__.py`; command-specific flags are defined in
the corresponding module under `src/gobby/cli/`. This reference is not
exhaustive — some groups carry additional maintenance subcommands and flags;
run `gobby <group> --help` for the complete surface of any group.

## Global Form

```bash
gobby [--config PATH] COMMAND [ARGS]...
```

| Option | Purpose |
| --- | --- |
| `--config PATH` | Load a custom configuration file before dispatching the command. |
| `--version` | Print the Gobby version and exit. |

Most commands that inspect daemon-backed state expect the daemon to be running.
Start it with `gobby start` and check it with `gobby status` or `gobby health`.

## Top-Level Commands

| Command | Purpose | Source |
| --- | --- | --- |
| `agents` | Manage agent definitions and agent runs. | `src/gobby/cli/agents.py` |
| `auth` | Reset the installed account password and manage the local daemon API token. | `src/gobby/cli/auth.py` |
| `build` | Start and control lifecycle automation for plans, epics, and tasks. | `src/gobby/cli/build.py` |
| `clones` | Manage isolated clone workspaces. | `src/gobby/cli/clones.py` |
| `comms` | Manage inter-session communication channels. | `src/gobby/cli/communications.py` |
| `cron` | Manage scheduled jobs and dispatcher ticks. | `src/gobby/cli/cron.py` |
| `datastores` | Manage hub-side shared datastores. | `src/gobby/cli/datastores.py` |
| `embeddings` | Manage the embedding service and indices. | `src/gobby/cli/embeddings.py` |
| `github` | Manage GitHub integration. | `src/gobby/cli/github.py` |
| `health` | Check daemon health. | `src/gobby/cli/daemon.py` |
| `hooks` | Manage hook endpoints and configuration. | `src/gobby/cli/extensions.py` |
| `hub-backup` | Back up and verify every hub datastore. | `src/gobby/cli/hub_backup/` |
| `hub-maintenance` | Run fenced, verified hub maintenance. | `src/gobby/cli/hub_maintenance.py` |
| `init` | Initialize `.gobby/project.json`. | `src/gobby/cli/init.py` |
| `install` | Install required infrastructure, configuration, integrations, and hooks. | `src/gobby/cli/install.py` |
| `lease` | Inspect and control single-active-daemon ownership. | `src/gobby/cli/daemon_lease.py` |
| `linear` | Manage Linear integration. | `src/gobby/cli/linear.py` |
| `mcp-proxy` | Inspect and call tools through the MCP proxy. | `src/gobby/cli/mcp_proxy.py` |
| `mcp-server` | Run the stdio MCP server. | `src/gobby/cli/mcp.py` |
| `memory` | Manage persistent memories. | `src/gobby/cli/memory/` |
| `merge` | Manage merge assistance. | `src/gobby/cli/merge.py` |
| `observations` | Inspect unmodeled transcript observations. | `src/gobby/cli/observations.py` |
| `pack` | Pack project context. | `src/gobby/cli/pack.py` |
| `pipelines` | Manage pipeline definitions and runs. | `src/gobby/cli/pipelines.py` |
| `plan` | Run plan utility commands. | `src/gobby/cli/plan.py` |
| `plans` | Manage DB-backed plan records. | `src/gobby/cli/plans.py` |
| `postgres` | Manage the PostgreSQL hub (status and migrations). | `src/gobby/cli/postgres.py` |
| `profiles` | Manage build profile registry rows. | `src/gobby/cli/profiles.py` |
| `projects` | Inspect known Gobby projects. | `src/gobby/cli/projects.py` |
| `qdrant` | Manage Qdrant helper commands. | `src/gobby/cli/qdrant.py` |
| `restart` | Restart the daemon. | `src/gobby/cli/daemon.py` |
| `rules` | Manage workflow rules. | `src/gobby/cli/rules.py` |
| `schema` | Inspect or apply the hub schema migration chain. | `src/gobby/cli/schema.py` |
| `secrets` | Manage stored secrets. | `src/gobby/cli/secrets.py` |
| `service` | Manage service installation/runtime helpers. | `src/gobby/cli/service.py` |
| `sessions` | Inspect stored sessions and transcripts. | `src/gobby/cli/sessions.py` |
| `skills` | Manage installed skills. | `src/gobby/cli/skills.py` |
| `stages` | Manage the task stage registry. | `src/gobby/cli/stages.py` |
| `start` | Start the daemon. | `src/gobby/cli/daemon.py` |
| `status` | Show daemon status. | `src/gobby/cli/daemon.py` |
| `stop` | Stop the daemon. | `src/gobby/cli/daemon.py` |
| `sync` | Sync bundled content to the database. | `src/gobby/cli/sync.py` |
| `tasks` | Manage development tasks. | `src/gobby/cli/tasks/` |
| `test-quality` | Run test quality helpers. | `src/gobby/cli/test_quality.py` |
| `test-types` | Audit Python test types. | `src/gobby/cli/test_types.py` |
| `tokens` | Audit token usage ledgers. | `src/gobby/cli/tokens.py` |
| `ui` | Manage and launch the web UI. | `src/gobby/cli/ui.py` |
| `uninstall` | Remove installed integrations and hooks. | `src/gobby/cli/install.py` |
| `unpack` | Unpack project context. | `src/gobby/cli/pack.py` |
| `variables` | Get or set live session variables. | `src/gobby/cli/variables.py` |
| `webhooks` | Manage webhook endpoints. | `src/gobby/cli/extensions.py` |
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

`gobby start` always starts PostgreSQL, Qdrant, and FalkorDB with Docker
Compose and waits for container health before launching the daemon.
The Web UI lifecycle follows the persistent `ui.enabled` setting. When it is
enabled, the daemon owns production UI serving and the development-server
lifecycle; set it to `false` persistently to run the daemon without the UI.

### `gobby stop`

Stop the daemon.

```bash
gobby stop [--docker]
```

Pass `--docker` to stop the managed datastore containers as well as the daemon.

### `gobby restart`

Stop and start the daemon.

```bash
gobby restart [--verbose] [--docker]
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

`gobby install` options:

| Option | Purpose |
| --- | --- |
| `--claude` | Install Claude Code integration assets. |
| `--agy` | Install AGY integration assets. |
| `--codex` | Install Codex integration assets. |
| `--droid` | Install Droid integration assets. |
| `--qwen` | Install QwenCode integration assets. |
| `--hooks`, `--git-hooks` | Aliases for one flag: install repository git hooks (verification, JSONL export, code indexing). |
| `--all` | Install all supported integrations. |
| `--config-only` | Configure Gobby and provision required infrastructure without CLI or Git hooks. |
| `--falkordb-password-stdin` | Read the FalkorDB password from standard input. |
| `--project` | Install project-scoped configuration. |
| `--voice` | Install voice support assets. |
| `--embedding-url URL` | Use a custom embedding API endpoint. |
| `--embedding-provider PROVIDER` | Force embedding provider compatibility mode (`lmstudio`, `ollama`, `openai-compatible`, `vllm`). |
| `--embedding-model MODEL` | Override the embedding model. |
| `--embedding-dim N` | Override the embedding dimension. |
| `--secret-kek-posture [key-file|passphrase]` | Select daemon-local secret KEK storage. |
| `--container-restarts`, `--no-container-restarts` | Enable or disable `unless-stopped` restart policies for managed service containers (enabled by default). |
| `--no-interactive` | Run without prompts. |
| `-C`, `--path PATH` | Install against a specific path. |

Default, `--all`, and `--config-only` installs require a running Docker daemon
and always provision the managed PostgreSQL, Qdrant, and FalkorDB profiles.
Those services are independent of the embedding-provider choice. The installer
applies `unless-stopped` to new and existing managed containers. Use
`--no-container-restarts` when another supervisor owns their lifecycle.
Re-running `gobby install --config-only` repairs the selected policy with
`docker update` and refreshes the managed Compose file.

On a fresh datastore, interactive installation prompts for the initial user's
name, email, password, and confirmation. It creates that user and assigns the
local machine before daemon startup. A fresh `--no-interactive` install refuses
to invent credentials; run one interactive installation first. Reruns preserve
the existing sole user and idempotently confirm local machine ownership.

CLI-targeted flags and `--hooks` are maintenance operations. In particular,
`gobby install --hooks` only ensures the personal marker and reinstalls
repository Git hooks; it skips daemon configuration and managed services.

`gobby uninstall` options:

| Option | Purpose |
| --- | --- |
| `--claude` | Remove Claude Code integration assets. |
| `--agy` | Remove AGY integration assets. |
| `--codex` | Remove Codex integration assets. |
| `--droid` | Remove Droid integration assets. |
| `--qwen` | Remove QwenCode integration assets. |
| `--all` | Remove all supported integration assets. |
| `--falkordb` | Remove FalkorDB graph backend data and configuration. |
| `--volumes` | Remove service volumes where supported. |
| `--project` | Remove project-scoped configuration. |
| `-C`, `--path PATH` | Uninstall from a specific path. |

### `gobby cutover`

Build and activate one coherent set of the four schema-aware Rust binaries from
a Gobby source checkout:

```bash
gobby cutover [--path PATH]
```

The command builds `gcode`, `gdaemon`, `ghook`, and `gwiki` in release mode,
installs all four through new inodes, regenerates the packaged schema identity
pin from the installed `gdaemon`, restarts the daemon, and runs an installed-`gcode`
grant smoke. A failed promotion, restart, or smoke restores the prior binaries
and pin before restarting the prior daemon.

### `gobby auth`

Reset the sole installed user's browser password and manage the install-scoped
daemon API token.

```bash
gobby auth credentials
gobby auth token [--show] [--rotate]
```

| Command or option | Purpose |
| --- | --- |
| `credentials` | Prompt for and set a new Argon2id password for the sole installed user's email. |
| `token` | Show token path, file presence, stored hash prefix, and file/DB agreement. |
| `token --show` | Print the plaintext token for deliberate client provisioning. |
| `token --rotate` | Replace the token file and stored hash; recopy the file to other machines. |

The token command reads `$GOBBY_HOME/local_cli_token` (default
`~/.gobby/local_cli_token`). Rotation is picked up by running clients within
about five seconds.

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
gobby build [INPUT_REF] [--project PROJECT] [--coordinator [current|SESSION_UUID]] [OPTIONS]
gobby build stop [REF] [--project PROJECT]
gobby build resume [REF] [--project PROJECT]
gobby build clean REF [--project PROJECT] [--dry-run] [--force] [--yes]
gobby build restart REF [--project PROJECT] [--dry-run] [--force] [--yes] [--no-resume]
```

| Option | Purpose |
| --- | --- |
| `--quick` | Use quick build defaults. |
| `--project PROJECT` | Build or control automation in a target project by name or UUID. |
| `--coordinator [current\|SESSION_UUID]` | Wake a coordinator session when build-spawned agents complete. `current` resolves from `GOBBY_SESSION_ID`; with `--project`, use `current` or a full session UUID. |
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
| `--planning-seed-state STATE` | For plan-file builds, seed planning as `drafted`, `needs_review`, or `approved`. |
| `--completed-plan-review-rounds N` | Count already-completed plan adversary rounds when seeding from `needs_review` or `approved`. |
| `--plan-enhancement-rounds N` | Target constructive `plan-enhancer` rounds before the adversary gate (`0` disables; overrides the build profile default). |
| `--dry-run` | Preview `clean` or `restart` effects. |
| `--force` | Force destructive cleanup for `clean` or `restart`. |
| `--yes` | Confirm destructive `clean` or `restart` prompts. |
| `--no-resume` | For `restart`, reset state and leave automation paused. |

Use `gobby build stop [REF]` to pause future dispatch work for a target.
Explicit `--project` rejects project-local coordinator refs such as `#N` or
bare numbers because they would resolve in the target project.

Cross-project coordinator launch:

```bash
gobby build '#14354' --project gobby-cli --coordinator current
```

For `/gobby plan` handoff, use:

```bash
gobby build .gobby/plans/example.md --planning-seed-state approved --completed-plan-review-rounds 1
```

`planning_seed_state=approved` starts at expansion. `needs_review` starts at
the remaining planning review loop with the completed round count already
applied. `drafted` starts from planning.

`--plan-enhancement-rounds N` seeds the target number of constructive
`plan-enhancer` rounds that run as a pre-adversary sub-loop inside the planning
stage. Autonomous builds default to `0` (no enhancement); pass `N > 0` to enable
it. The explicit value wins over the build profile default, including an
explicit `0`. Enhancement rounds are counted independently of the adversary
review budget.

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
| `tasks list` | `--active`, `--project`, `--stage`, `--state`, `--claimed`, `--unclaimed`, `--ready`, `--blocked`, `--closed`, `--escalated`, `--limit`, `--group`, `--json` |
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
| `tasks close` | `--reason` |
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
gobby tasks backup [--output PATH] [--quiet]
gobby tasks restore [--input PATH] [--quiet]
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
  --task-tree matrix-file --matrix-file MATRIX.coverage.yaml
```

`db` mode reads the live task database and requires both `--root-task` and
`--project-id`. `matrix-file` mode reads a YAML or JSON mapping with `header`
and `rows` from `--matrix-file`; it does not accept DB scope flags. Both modes
write a coverage manifest and print its path. Use `--manifest` to choose that
path explicitly; otherwise Gobby writes beneath `.gobby/plans/coverage/`.

| Option | Purpose |
| --- | --- |
| `--plan PATH` | Plan file to evaluate. |
| `--plan-id ID` | Stable plan identifier. |
| `--plan-hash HASH` | Expected plan content hash. |
| `--task-tree db\|matrix-file` | Choose live DB evaluation or a supplied coverage matrix. |
| `--root-task TASK` | Root task ref for `db` mode. |
| `--project-id PROJECT` | Project UUID for `db` mode. |
| `--matrix-file PATH` | YAML/JSON matrix with `header` and `rows`; required for `matrix-file`. |
| `--evidence SPEC` | Evidence source for coverage validation. |
| `--manifest PATH` | Write manifest to a specific path. |
| `--regenerate` | Regenerate an existing manifest. |

### `gobby plans`

Manage DB-backed plan records.

```bash
gobby plans list [--state active|archived] [--kind implementation|strategy] [--project PROJECT]
gobby plans show PLAN_ID [--project PROJECT]
gobby plans register PLAN_PATH [--plan-id ID] [--kind KIND] [--root-task-ref TASK] [--project PROJECT]
gobby plans validate PLAN_FILE [--project PROJECT] [--mode standard|expansion]
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
| `recommend-tools` | `--agent`, `--mode`, `--top-k`, `--json` |
| `search-tools` | `--top-k`, `--min-similarity`, `--server`, `--json` |
| `refresh` | `--force`, `--server`, `--json` |

## Sessions And Agents

### Sessions

```bash
gobby sessions list [--project PROJECT] [--status STATUS] [--source SOURCE] [--limit N] [--json]
gobby sessions show SESSION [--json]
gobby sessions messages SESSION [--limit N] [--role ROLE] [--offset N] [--json]
gobby sessions stats
gobby sessions create-handoff [NOTES] [--session-id SESSION] [--output db|file|all] [--path DIR]
gobby sessions delete SESSION
```

### Agents

```bash
gobby agents list [--enabled | --disabled] [--surface SURFACE] [--json]
gobby agents show NAME [--json]
gobby agents spawn PROMPT --session SESSION [OPTIONS]
gobby agents steps [--session SESSION] [--json]
gobby agents check NAME [--json]
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
gobby worktrees claim WORKTREE SESSION_ID
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

## Memory, Skills, Variables, And Rules

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
gobby skills new NAME [--description DESC]
gobby skills validate PATH
gobby skills doc [--output PATH]
gobby skills meta get NAME KEY
gobby skills meta set NAME KEY VALUE
gobby skills meta unset NAME KEY
```

### Variables

```bash
gobby variables get [NAME] [--session SESSION] [--json]
gobby variables set NAME VALUE [--session SESSION] [--json]
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
gobby pipelines check NAME [--json]
gobby pipelines run NAME [-i KEY=VALUE ...] [--json]
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

```

## Hub Backup Disaster Recovery

Restore the verified PostgreSQL artifact from a hub backup into an explicit
target database:

```bash
gobby stop
gobby hub-backup restore BACKUP_ROOT \
  --database-url postgresql://USER:PASSWORD@HOST:PORT/DATABASE \
  --clean \
  --yes
```

The target must be the Gobby-managed PostgreSQL service. The command verifies
the hub manifest and every recorded artifact before restoring PostgreSQL. When
the backup contains an armed maintenance login fence, restore discovers the
epoch from the restored target, binds that epoch for its release connection,
and records `released_by_command = 'restore'`. Normal connections need no
maintenance GUC after the command completes. The required `--database-url`
keeps the configured origin database outside the restore mutation path.

Hub backup drains run-scoped PostgreSQL logins before dumping cluster globals.
The globals artifact includes the stable issuer, daemon-runtime, and capability
roles; reserved `gobby_agent_<execution>_<generation>` login roles are excluded.
Restore replays those stable roles, restores data, then removes any reserved
login and retires its restored binding before the daemon can start.

Qdrant, FalkorDB, and volume artifacts remain separate from this PostgreSQL
restore command.

### Managed credential recovery

Inspect active scoped authority without displaying passwords, DSNs, tokens, or
KEK material:

```bash
gobby postgres scoped-roles
gobby postgres scoped-roles --json
```

Force-revoke one managed execution after confirming its execution ID:

```bash
gobby postgres force-revoke-run EXECUTION_UUID
```

Use these recovery sequences:

- **Failed rotation:** list scoped roles, force-revoke the affected execution,
  restart the daemon, then list again. Rotation rollback and startup
  reconciliation remove predecessor and partial-successor authority.
- **Daemon outage:** restore daemon service first when live agents will resume.
  For a run that must remain stopped, force-revoke its execution before daemon
  restart. Startup reconciliation removes expired, terminal, and orphan roles.
- **Database restore:** keep the daemon stopped until `gobby hub-backup restore`
  succeeds. The restore command removes reserved-prefix logins and reconciles
  restored bindings before returning.
- **Stale role:** list scoped roles and force-revoke its execution. A role with
  no binding is an orphan; daemon startup reconciliation disables it,
  terminates its sessions, and drops it before agent recovery proceeds.

Generated runtime cleanup removes legacy shared-DSN bootstraps and runtime KEK
links or copies before a managed gcode subprocess is launched. A runtime home
is reusable only when its bootstrap user matches the scoped-role format.

## Admin And Diagnostics

```bash
gobby sync [--force] [--verify-only] [--type TYPE] [--verbose] [--reinstall rules|agents|pipelines|variables|all]
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

_Last verified: 2026-08-14_
