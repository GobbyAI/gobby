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
| `datastores` | Manage hub-side shared datastores: `expose`, `rotate-password`. | `src/gobby/cli/datastores.py` |
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
| `uninstall` | Remove installed hooks and managed tools; never touches Docker or data. | `src/gobby/cli/uninstall.py` |
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
gobby stop [--docker] [--terminals] [--wait | --force]
```

Pass `--docker` to also stop the managed PostgreSQL, Qdrant, and FalkorDB
containers with `docker compose stop`. Containers, data volumes, and their
`unless-stopped` policy stay in place; `gobby start` brings them back.

The `gterm host` process and every terminal it owns survive a stop by default:
the daemon closes its control connection and leaves the host running, and the
next `gobby start` adopts it, so attached `gclient` panes keep their shells.
Pass `--terminals` to drain the host and its terminals as part of the stop.
Setting `terminals.stop_host_on_shutdown: true` in config makes every stop
drain; the admin `POST /shutdown?terminals=true` and
`POST /restart?terminals=true` routes are the HTTP equivalents.

A restart-protected cron run (the nightly `gobby:memory-dream` sweep, which
runs for hours) holds a lease while it is active: `gobby stop` refuses and
prints the job name and elapsed time. `--wait` defers the stop until the run
reaches a terminal state, bounded by the run's own timeout; `--force`
interrupts it now. An interrupted run is recorded as `interrupted` (never
`failed`, no backoff), and the scheduler re-queues the job about a minute
after the next start so checkpointed work resumes.

### `gobby restart`

Stop and start the daemon.

```bash
gobby restart [--verbose] [--docker] [--terminals] [--wait | --force]
```

`--wait`, `--force`, and `--terminals` apply to the stop half exactly as for
`gobby stop`: without `--terminals` the restarted daemon adopts the surviving
`gterm host`, and attached `gclient` sessions reconnect to the same terminals.

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

Install Gobby, reinstall named components of an existing install, or remove what
was installed.

```bash
gobby install [OPTIONS] [COMPONENT]...
gobby uninstall [OPTIONS] [COMPONENT]...
```

Bare `gobby install` is the only full install. It claims the files home, writes
daemon config, provisions the managed PostgreSQL, Qdrant, and FalkorDB stack,
creates the account identity, installs hooks for every detected CLI and Git
hooks for the current repository, runs the RTK, web UI exposure, embedding, and
voice prompts, then starts the daemon. Naming one or more components requires an
existing install (`bootstrap.yaml` and `~/.gobby/bin/gdaemon`); the run executes
only those components, in the order given, and never touches daemon config,
managed services, identity, or daemon start. It exits 1 when any component
fails.

| Component | `gobby install` | `gobby uninstall` |
| --- | --- | --- |
| `claude`, `codex`, `grok`, `qwen`, `droid`, `agy` | Install that CLI's global hooks. | Remove that CLI's global hooks. |
| `git-hooks` | Install repository Git hooks (verification, JSONL export, code indexing) in the `-C` repository. | Remove them from the `-C` repository. Explicit only; bare uninstall never touches repositories. |
| `rtk` | Reconcile the RTK binary and the `rtk-command-rewrite` rule (details below). | Disable the rule and remove the managed fallback binary. |
| `impeccable` | Provision the Impeccable design runtime. | Remove the Impeccable runtime. |
| `voice` | Set `voice.enabled=true` in daemon config. | — |
| `embedding` | Configure the embedding provider, honoring the `--embedding-*` overrides. | — |
| `ide-settings` | Configure VS Code-family terminal integration. | — |

`gobby install` modifiers:

| Option | Purpose |
| --- | --- |
| `--embedding-url URL` | Use a custom embedding API endpoint. |
| `--embedding-provider PROVIDER` | Force embedding provider compatibility mode (`lmstudio`, `ollama`, `openai-compatible`, `vllm`). |
| `--embedding-model MODEL` | Override the embedding model. |
| `--embedding-dim N` | Override the embedding dimension. |
| `--no-interactive` | Run without prompts. |
| `--container-restarts`, `--no-container-restarts` | Enable or disable `unless-stopped` restart policies for managed service containers (enabled by default). |
| `--files-home DIR` | Existing absolute directory for hub-owned files (local install). |
| `-C`, `--path PATH` | Repository for `git-hooks` (default: current directory). |

The `--embedding-*` overrides apply to the full install or to the `embedding`
component; combined with other components only, they are a usage error.

`gobby uninstall` takes `-C`/`--path` (the `git-hooks` repository) and `--yes`
(skip the confirmation prompt). Bare `gobby uninstall` removes everything
installed: hooks from every detected CLI, the global hook dispatchers, the
Tailscale UI exposure, the RTK rule and managed binary, and the Impeccable
runtime. When no CLI hooks are detected it still cleans the RTK and Impeccable
artifacts. Docker containers, data volumes, `bootstrap.yaml`, secrets, and the
files home are never touched.

The full install requires a running Docker daemon and always provisions the
managed PostgreSQL, Qdrant, and FalkorDB profiles, independent of the
embedding-provider choice. The installer applies `unless-stopped` to new and
existing managed containers; use `--no-container-restarts` when another
supervisor owns their lifecycle. Re-running bare `gobby install` repairs the
selected policy with `docker update` and refreshes the managed Compose file.

On a fresh datastore, interactive installation prompts for the initial user's
name, email, password, and confirmation. It creates that user and assigns the
local machine before daemon startup. A fresh `--no-interactive` install refuses
to invent credentials; run one interactive installation first. Reruns preserve
the existing sole user and idempotently confirm local machine ownership.

Managed-service credentials are generated, never asked for, and never rotated
by an install:

- The PostgreSQL DSN in `~/.gobby/bootstrap.yaml` receives a random URL-safe
  password when the bootstrap is first created; every later install reuses the
  stored `database_url` verbatim, whatever the process environment says. A
  pre-existing `gobby_postgres_data` volume whose password differs fails the
  readiness check with a message naming the volume and `bootstrap.yaml`; Gobby
  never removes data volumes.
- The FalkorDB password is generated on first provisioning, printed once, and
  stored as the `falkordb_password` secret; later installs reuse it.
- Secrets start in the key-file KEK posture; `gobby secrets rekey --posture
  passphrase` is the passphrase opt-in.
- `gobby datastores rotate-password postgres|falkordb` rotates a credential on
  an existing install (see [Hub Backup Disaster Recovery](#hub-backup-disaster-recovery)).

`gobby stop --docker` stops the managed containers with `docker compose stop`
and never removes them; `gobby start` brings them back with `up -d`.

RTK integration is opt-in. Interactive full installs prompt with the currently
installed `rtk-command-rewrite` rule state as the default; fresh
`--no-interactive` installs leave it disabled. `gobby install rtk` accepts a
compatible stock RTK 0.45.0 or newer from PATH, uses Homebrew when available,
or installs a checksum-verified fallback in `~/.gobby/bin/`. Gobby never
invokes `rtk init`. The component run reconciles the binary and the rule,
prints the RTK status line, and stops without provisioning services, prompting
for the web UI, or re-running daemon setup.

When enabled, `ghook` remains every CLI's installed hook. Gobby calls
`rtk rewrite -- <command>` only for synchronous `before_tool` shell-command
rewrites on Claude Code, Codex, Qwen, Grok, Droid, and AGY. `rewrite` is the
same contract stock RTK host hooks use, so RTK's heredoc, command-substitution,
and file-redirect gates apply: exit 0 (allow) and exit 3 (ask) apply the
rewritten command from stdout; exit 1 (no equivalent) and exit 2 (deny) pass the
original command through untouched. Gobby ignores RTK's permission verdict and
preserves each host's native permission flow. RTK failures, timeouts, invalid
output, and unsupported providers fail open. RTK's existing `exclude_commands`,
`transparent_prefixes`, global `history.db`, and `tee/` configuration remain
authoritative. The compatibility probe (`--version` plus `rewrite --help`) runs
once per executable and is reused until the binary changes on disk.

`gobby hooks status` reports RTK binary path/version, installed rule state,
direct-hook conflicts, ownership, and health. Opt-in reconciliation backs up and
removes exact RTK-generated direct hooks, legacy scripts, instruction blocks, and
generated files; modified or unrelated content is preserved and reported.
Bare `gobby uninstall` disables the rule and removes the checksum-matching
Gobby fallback; CLI component uninstalls such as `gobby uninstall claude` leave
both as installed, and `gobby uninstall rtk` alone disables the rule and removes
the managed fallback while CLI hooks, other tools, and the service stay in
place. Homebrew and other user-managed RTK installations always remain
installed.

The V1 handler registry is internal and contains only `rtk`. Future versions may
add named trusted handlers, broader hook-event capabilities, and dedicated
management surfaces.

### `gobby cutover`

Build and activate one coherent set of the three schema-aware Rust binaries from
a Gobby source checkout:

```bash
gobby cutover [--path PATH]
```

The command runs one locked release build for `gcode`, `gdaemon`, and `ghook`,
then promotes all three through the shared workspace installer. That
path signs staged binaries when required, installs through new inodes, writes
version and install sidecars, and writes the installed schema identity pin only
after all three binaries promote. Before restart, cutover verifies that the exact
`gdaemon` resolved for daemon startup matches the installed pin; a mismatch
fails closed with the three-binary rebuild remedy. Promotion failures name the
members already promoted and those still unpromoted. Cutover does not claim to
restore binaries after a partial promotion.

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
| `--profile NAME` | Resolve an installed/project build preset (default name `default`). |
| `--quick` | Run one bounded lifecycle action, then leave target automation disabled. |
| `--project PROJECT` | Build or control automation in a target project by name or UUID. |
| `--coordinator [current\|SESSION_UUID]` | Wake a coordinator session when build-spawned agents complete. `current` resolves from `GOBBY_SESSION_ID`; with `--project`, use `current` or a full session UUID. |
| `--skip-stage STAGE` | Skip one lifecycle stage; repeat for multiple stages. |
| `--stage STAGE:KEY=VALUE` | Override stage settings such as review caps. |
| `--isolation MODE` | Set build isolation to `none`, `worktree`, or `clone`. Omitted isolation comes from the resolved profile. |
| `--clone` | Shorthand for `--isolation clone`; conflicts with `--isolation none` and `--isolation worktree`. |
| `--delivery-mode MODE` | Override `auto` or `pull_request` delivery intent. |
| `--delivery-target-repo OWNER/REPO` | Override the PR target repository. |
| `--no-merge` | Skip final promotion; requires worktree or clone isolation. |
| `--pr VALUE` | Configure PR delivery behavior. |
| `--target-branch BRANCH` | Override the target branch. |
| `--agent NAME` | Assign a specific agent definition. |
| `--reset-expansion-output` | Clear prior expansion output before building. |
| `--max-active-agents N` | Cap immediately active automation agents. |
| `--max-retries N` | Cap retries per build stage. |
| `--planning-seed-state STATE` | For plan-file builds, seed planning as `drafted`, `needs_review`, or `approved`. |
| `--completed-plan-review-rounds N` | Count already-completed plan adversary rounds when seeding from `needs_review` or `approved`. |
| `--plan-enhancement-rounds N` | Target constructive `plan-enhancer` rounds before the adversary gate (`0` disables; overrides the build profile default). |
| `--dry-run` | Preview launch, `clean`, or `restart`; control previews may record history. |
| `--delete-dirty-worktrees` | For `clean`, explicitly permit dirty descendant worktree deletion. |
| `--force` | Force destructive cleanup for `clean` or `restart`. |
| `--yes` | Confirm destructive `clean` or `restart` prompts. |
| `--no-resume` | For `restart`, reset state and leave automation paused. |

Without a ref, `gobby build stop` pauses future project ticks. With a ref it
disables the subtree, cancels active agents, clears mutexes/stale agent claims,
and resets stoppable stage work while retaining task history and artifacts.
Task-scoped resume preserves isolation; a new build request resolves profile
options again. Use a clean/restart preview before destructive recovery.
These are operator controls; agents use the corresponding `gobby-tasks-ops` tools.
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
stage. Bundled profiles default to `0` (no enhancement); inspect the installed row
before relying on that default. Pass `N > 0` to enable it when authorized. The explicit value wins over the build profile default, including an
explicit `0`. Enhancement rounds are counted independently of the adversary
review budget.

### Profiles And Stage Defaults

Operators use `gobby profiles list|show|create|update|enable|disable|restore|delete`
and `gobby stages list|show|update|restore|delete|defaults`. These are command
families, not literal pipe-separated commands; use each command's `--help` for
required fields. Profiles distinguish installed and project scope.
`gobby stages defaults TASK_TYPE` reads the default manifest; repeated
`--set STAGE:POSITION` values replace it. Agents use `gobby-profiles`,
`gobby-tasks` reads, and `gobby-tasks-ops` stage mutations.
See [Dispatch](./dispatch.md#stage-registry) for registry constraints.

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
| `tasks create` | `--description`, `--validation-criteria` (required except epics), `--priority`, `--type`, `--depends-on`, `--project` |
| `tasks update` | `--title`, `--validation-criteria`, `--priority`, `--parent`, `--task-type`, `--isolation` |
| `tasks close` | `--reason` |
| `tasks de-escalate` | `--reason`, `--reset-validation` |
| `tasks delete` | `--cascade`, `--unlink`, `--yes` |

Agents use `gobby-tasks` MCP lifecycle tools for claims and closure, and
`gobby-tasks-ops` for authorized stage review transitions. The CLI remains an
operator interface. Quote shell task references, for example `'#123'`.

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
gobby tasks validate TASK --summary SUMMARY
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

Leaf validation requires `--summary` or `--file`; it runs a bounded criteria
review and does not replace the agent close checklist.

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
gobby plans review-evidence [--plan PATH] [--open] [--json] [--limit N]
gobby plans review-runs TASK_REF
```

Plan registration needs a real root: supply `--root-task-ref` unless it can be
inferred from plan metadata or the filename. Project-aware validation uses
`-p <project-root>` and is required before review/expansion. `review-runs` prints
an expansion-QA pointer; use `review-evidence` to inspect recent evidence.
Agents use MCP for plan/task lifecycle writes; the task-expansion commands above
are operator-only.

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

Operator interfaces (agents use `gobby-sessions` MCP tools):

```bash
gobby sessions list [--project PROJECT] [--status STATUS] [--source SOURCE] [--limit N] [--json]
gobby sessions show SESSION [--json]
gobby sessions messages SESSION [--limit N] [--role ROLE] [--offset N] [--json]
gobby sessions stats [--project PROJECT]
gobby sessions summarize [NOTES] [--session-id SESSION] [--output db|file|all] [--path DIR]
gobby sessions restore SESSION [--path PATH] [--json]
gobby sessions restore --all [--json]
gobby sessions delete SESSION [--yes]
gobby sessions renumber --project PROJECT [--apply]
gobby sessions backfill-context-windows [--dry-run]
```

`summarize` creates archival output; it never stages a recoverable handoff.
`renumber` previews until `--apply`; context-window backfill writes unless
`--dry-run` is present. Restoration does not overwrite an existing transcript.
See the [session guide](sessions.md#cli-commands) for workflows and recovery.

### Agents

Operator interfaces; agents use `gobby-agents` and `gobby-workflows` MCP tools.

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
gobby agents kill RUN [--force] [--stop] [--yes]
gobby agents stats [--session SESSION]
gobby agents cleanup [--timeout MINUTES] [--dry-run]
```

`agents spawn` supports `--workflow`, `--task`, `--provider`, `--model`,
`--reasoning-effort`, `--reasoning-required/--no-reasoning-required`,
`--timeout`, `--terminal-backend tmux|native`, and `--json`.
The CLI requires a parent session and does not expose all MCP isolation/grant
fields. Cleanup mutates by default; use `--dry-run` to inspect stale candidates.
CLI kill defaults differ from MCP: pass `--stop` to stop the workflow as well.

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
gobby memory create CONTENT --rationale REASON [--type TYPE] [--project PROJECT]
gobby memory recall [QUERY] [--project PROJECT] [--limit N] [--tags-all TAGS] [--tags-any TAGS] [--tags-none TAGS]
gobby memory list [--type TYPE] [--limit N] [--project PROJECT] [--tags-all TAGS] [--tags-any TAGS] [--tags-none TAGS]
gobby memory show MEMORY [--project PROJECT]
gobby memory update MEMORY [--content CONTENT] [--tags TAGS] [--project PROJECT]
gobby memory delete MEMORY [--project PROJECT]
gobby memory export [--output PATH]
gobby memory stats
```

Memory also provides backup/restore, graph/index maintenance, dream run
observation/revert, and recall-signal diagnostics. See the audited
[Memory CLI reference](memory.md#cli-reference) and
[Dream operations](memory.md#dream-operations) for scope and recovery boundaries.

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

### `gobby datastores rotate-password`

Rotate a managed datastore credential on a local install:

```bash
gobby datastores rotate-password postgres
gobby datastores rotate-password falkordb
```

`postgres` generates a 32-byte URL-safe password, runs `ALTER ROLE` for the
`database_url` user over the current DSN, then rewrites `database_url` in
`~/.gobby/bootstrap.yaml`; if that write fails after the role changed, the new
DSN is printed on stderr for manual repair. `falkordb` stores a freshly
generated `falkordb_password` secret. Neither command prints the password on
success, invokes Docker, or restarts anything: run `gobby restart` afterwards
so the daemon and the containers pick up the new credential. Data volumes are
never touched. Remote installs (`datastore_mode: remote`) are refused with
exit 2 because remote clients hold no datastore credentials.

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

`gobby sync` writes the shared installed rows that every session reads. While a
daemon is running it refuses to sync from any checkout other than the one the
daemon serves (`install_dir` on `GET /api/health`), because the daemon keeps
running its own code against the overwritten rows; `--force` overwrites anyway
and prints a banner naming both checkouts. With no daemon reachable the sync
proceeds. `gobby install` re-points the service at the invoking checkout, so
its sync is a deliberate whole-checkout cutover.

To test a branch's bundled content before merge: for content-only changes
(rule and skill edits the running code already understands) run
`gobby sync --force` from the worktree, test, then restore the shared rows with
`gobby sync` from the daemon's checkout — the daemon's next restart restores
them too. Content that needs the branch's code (new effect handlers, new MCP
tools) needs the cutover above or the merge; forcing it in only produces rows
the running daemon cannot serve.

## Native Code Index

PostgreSQL BM25 health is available through `gobby postgres status --json`
(`code_index` in the payload). Operator `gobby postgres repair-code-index
[--json]` selectively repairs damaged BM25 indexes and exits 1 if still unhealthy.
It uses bootstrap credentials and the configured maintenance timeout; missing
indexes require setup/migrations. After failed startup recovery, repair and
coordinate a restart to enable workers. See
[PostgreSQL BM25 recovery](code-index.md#postgresql-bm25-recovery).

Use the separately installed `gcode` binary for code navigation. There is no
`gobby code-index` command or code-index MCP service in this checkout.
`gcode --help` lists the native surface; the
[gcode guide](gcode-user-guide.md) covers search, retrieval, graph views,
freshness, repair, and operator cleanup. `gcode init` requires a registered
checkout; it does not create standalone project identity. Runtime access uses
daemon-issued grants. Mutating index/cleanup commands need their own authority.

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
