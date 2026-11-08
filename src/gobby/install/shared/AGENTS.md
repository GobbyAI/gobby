# Template Library

Everything in this directory is a **template**, not active enforcement.

- Templates are bundled with Gobby and synced to their DB registry tables by the sync
  modules (`sync_rules.py`, `sync_pipelines.py`, etc.)
- Templates have `enabled: true` by default — the template's `enabled` value is used
  directly when creating the installed DB row on first sync
- Gobby-owned bundled DB rows are refreshed from templates when definition drift is detected,
  preserving the user's enabled toggle for normal drift refreshes
- Migration and restore paths can re-apply the template `enabled` value, such as legacy
  `template` rows moving to `installed` or soft-deleted bundled rows becoming active again
- User/project-owned rows and project-local override copies are preserved by sync
- The `deprecated/` subdirectories are excluded from sync entirely
- The database is the source of truth for what's active, not these YAML files
- Installed rows belong to the checkout the running daemon serves. `gobby sync` from
  another checkout (a worktree on a branch) is refused unless `--force`, because the
  daemon keeps running its own code against the overwritten rows. To test
  content-only branch changes, `--force` from the worktree, test, then restore with
  `gobby sync` from the daemon's checkout (a daemon restart restores too). Content
  that needs branch code (new handlers, new MCP tools) needs `gobby install` cutover
  from the worktree or the merge

## Session Messaging Contract

Cross-session message content has no wake semantics. Omitting `wake` requests
immediate processing for direct `session`, `parent`, and `agent` targets. Fanout
(`global`, and `project` with `wake=true`) live-wakes only active recipients and
recipients holding a claimed task; an idle, paused, or waiting recipient with no
claimed task keeps its message queued for its next turn. Otherwise `project` fanout
stays queued without live wakes, and `wake=false` queues any target without a live
wake. `wake=true` on a direct target requests immediate processing and may steer an
active turn. Sessions that are interrupted, awaiting input, awaiting approval, or
awaiting handoff retain the durable mailbox row without daemon input until provider
evidence makes wake safe.

Recipient admission comes from installed agent definitions. A recipient's
`message_sender_capabilities` requires any listed capability from the sender's
definition; an empty list admits every sender. The daemon's system sender and
the operator's own unspawned terminal are exempt. The bundled Orchestrator
requires `message_restricted_seats`, granted to the Assistant, Triage,
DevOps and Orchestrator definitions. A refused direct send returns
`message_recipient_not_allowed`; fanout withholds that recipient. Triage sends
every message to the Orchestrator with `wake=true`, including digests. Omitted
`wake` follows the ordinary direct-target default for every definition.

Use a targetless `project` broadcast for coordination inside the current repository.
Use targetless `global` only for machine-local coordination across projects. The
daemon announces its own restarts and cutovers with system globals: a hold
notice before it stops and DAEMON BACK once it is healthy; no session sends them. Direct
`session` and `agent` sends require `target_id`. AGY sessions interrupted
through direct tmux input remain unconfirmed and therefore active; positive
hook or Gobby-mediated key/output evidence is required for protected interruption state.

## Configurability Convention

Bundled templates are Gobby-owned, immutable source inputs. Their installed DB rows are
also Gobby-owned and may be refreshed by bundled sync when definitions drift. Tooling
must customize by cloning or overriding templates rather than mutating installed bundled
rows in place.

User customization happens by cloning a bundled template into the matching project-local
path under `.gobby/install/<kind>/<name>/`. If the user copy keeps the same identifier as
a bundled template, it must include an explicit `override: true` label. Loading fails loud
when a project-local copy shadows a bundled template without that label, so accidental
shadowing cannot silently change runtime behavior.

MCP server templates and instances use dedicated roots instead of
`.gobby/install/<kind>/`:

- Project templates: `.gobby/mcp/templates/<name>.yaml`
- Project instances: `.gobby/mcp/servers/<name>.yaml`
- Machine templates: `~/.gobby/mcp/templates/<name>.yaml`
- Machine instances: `~/.gobby/mcp/servers/<name>.yaml`

A project or machine template that reuses a bundled MCP template name must set
`override: true`. A same-named project template shadows the global/bundled
template in that project whether or not it is enabled; instantiation then uses
the project row. See `mcp/AGENTS.md` for the template schema, secret-reference
rule, and instance YAML format.
