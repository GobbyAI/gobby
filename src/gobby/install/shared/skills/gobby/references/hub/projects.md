# Discover projects and diagnose checkouts

Load when selecting a project or handling missing/moved checkout metadata.
Fetch `gobby-hub:list_all_projects`, then call with `{}`. Use
`include_system=true` only when system projects are relevant.

Results contain project UUID, name, task count, and session count, ordered by
name. Soft-deleted projects are absent. By default names starting with
`_orphaned`, `_migrated`, `_personal`, or `_global` are hidden. There is no
cursor or checkout-path field. Counts include all matching task/session rows,
including closed tasks and system-source sessions.

Choose a returned name or UUID and pass it through the target tool's project
selection contract. For example, `gobby-tasks:create_task` accepts `project`;
load task-creation guidance and its current schema before creating anything.
A menu or project listing grants no mutation authority.

## Operator project management

The `gobby projects` CLI and `/api/projects` HTTP routes manage projects and
local checkout registration. They are not extra hub MCP tools. See the
[CLI inventory](../../../../../../../../docs/guides/cli-commands.md#gobby-projects)
and [project HTTP contract](../../../../../../../../docs/guides/http-endpoints.md#project-identity-and-checkouts).

- `gobby projects list --json` and `show PROJECT --json` include this machine's
  checkout object or null. CLI `list --all` includes underscore-prefixed names;
  it does not restore deleted projects.
- Initialize a local directory with `gobby init`, or HTTP `POST /api/projects/init`.
  Existing markers and validated identity govern registration.
- Preview drift from the checkout root with `gobby projects repair`; `--fix`
  registers a missing checkout only when the marker/root is valid. A moved
  checkout uses `gobby projects rebind PROJECT /absolute/root`, with a matching
  marker. Rebinding a deleted project does not restore it.
- Rename/update change shared metadata. Refreshing verification previews
  changes to `.gobby/project.json`; `--fix` writes them. Use `--ai off` for a
  deterministic preview that does not call a model.
- Delete is soft deletion. Purge is a separate lifecycle-safe destructive
  campaign through the running daemon. Both CLI commands require the exact
  project name through `--confirm`; system-project protection still applies.

On marker mismatch, foreign-machine rebind, invalid root, or ambiguous name,
inspect the returned conflict and verify UUID/root ownership. Do not repair by
copying another project's marker or writing database rows directly. Refresh
long-lived consumers after rebind as the CLI's cache hint instructs.
