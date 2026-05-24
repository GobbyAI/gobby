# Multica vs Gobby — research and borrowing recommendations

## Context

Multica (multica-ai/multica, ~24.7k stars, ~3k forks, public ~4 months) is the
closest thing to a direct gobby competitor. It launched with strong traction
and a clearer collaboration story. This doc captures what they do well, what
we do well, and which ideas are worth borrowing — verified against actual code
on both sides, not just READMEs.

**Sources read on multica side (verbatim):** `README.md`, `CLAUDE.md`,
`CLI_AND_DAEMON.md`, `docs/product-overview.md`, `server/migrations/001_init.up.sql`,
`server/migrations/012_inbox_actor.up.sql`,
`server/migrations/015_issue_subscriber.up.sql`,
`server/migrations/016_backfill_subscribers.up.sql`,
`server/migrations/042_autopilot.up.sql`,
`server/internal/daemonws/hub.go`,
`server/internal/daemon/client.go`,
`server/internal/daemon/execenv/codex_home.go`,
`server/internal/handler/skill.go`,
`server/internal/service/autopilot.go`, repo tree.

**Sources read on gobby side:** `CLAUDE.md`,
`.gobby/plans/task-12761-postgres-hub-migration.md`,
`src/gobby/sessions/`, `src/gobby/agents/spawners/command_builder.py`,
`src/gobby/servers/websocket/chat/{_session.py,_messaging.py,backends/*.py}`,
`src/gobby/storage/{session_tasks.py,clones.py,checkpoints.py}`,
`src/gobby/worktrees/git/`, grep across `src/gobby/`.

## Stack one-liner

| | gobby | multica |
|---|---|---|
| Backend | Python 3.13, FastAPI, PostgreSQL (Postgres migration in flight, plan #12761) | Go (Chi, sqlc, gorilla/ws), Postgres + pg extensions |
| Frontend | `./web/` (mixed) | Next.js 16 web + Electron desktop, monorepo (pnpm + Turborepo), strict `core/ui/views/` packages bridged via `NavigationAdapter` |
| Topology | Single local daemon, single user | Server (cloud or self-hosted Docker) + many local daemons; daemons connect via heartbeat **and** websocket push (`daemonws/hub.go` broadcasts `task_available` events) |
| Agent CLIs | claude, gemini, codex (3) | claude, codex, copilot, openclaw, opencode, hermes, gemini, pi, cursor-agent, kimi, kiro-cli (11) |
| Persistence | `~/.gobby/PostgreSQL hub` | Postgres rows scoped by `workspace_id` |

## Verified architectural differences

This section reports what's actually in the code, not what the docs claim.

### 1. Polymorphic actor model — confirmed at schema level

Multica's `001_init.up.sql` defines `assignee_type`/`creator_type` on `issue`,
`author_type` on `comment`, `recipient_type` on `inbox_item`, and `actor_type`
on `activity_log`, all with `CHECK (... IN ('member', 'agent'))` and a
companion UUID `*_id` column. `issue_subscriber` (migration 015) extends the
same pattern with `user_type` and a typed `reason` enum
(`'creator'|'assignee'|'commenter'|'mentioned'|'manual'`). Migration 016 then
backfills subscribers from existing creators/assignees/commenters in one
INSERT…SELECT.

Indexes are designed for the polymorphism: e.g.
`CREATE INDEX idx_inbox_recipient ON inbox_item(recipient_type, recipient_id, read)`.

Gobby has zero matches across `src/gobby/` for `actor_type|actor_id|assignee_type|created_by_type`.
Tasks implicitly belong to the agent that claimed them; humans aren't first-class
participants. Comments/discussions on tasks aren't a primitive at all.

### 2. Server/daemon split — verified, hybrid push+poll

Multica's `daemonws/hub.go` runs a websocket hub per workspace that pushes
`task_available` events to connected daemons. The daemon also polls via
`SendHeartbeat`, whose `HeartbeatResponse` carries pending updates
(`PendingUpdate`, `PendingModelList`, `PendingLocalSkills`,
`PendingLocalSkillImport`). So it's push-driven with poll fallback, not
the pure 3-second polling I summarized earlier.

The split means the **server holds task state and audit, the daemon holds
execution and FS**. Agent CLIs never see the server directly. This is the
architecture that lets multica run as cloud-hosted, self-hosted, or local —
without touching the trust boundary between Postgres and the user's API
keys / repo.

Gobby is purely local, single-process. We have no path to multi-user without
re-architecting this.

### 3. Task-keyed session resumption — multica does it; gobby has the data but not the wiring

Multica's `client.go` exposes `PinTaskSession(taskID, sessionID, workDir)`,
described in source as: *"persists the agent's session_id and work_dir on the
task row mid-flight so a daemon crash doesn't lose the resume pointer."*
Combined with `RecoverOrphans` on daemon startup, this gives task-keyed
resumption that survives both daemon crashes and re-assignment.

Gobby:
- Has `resume_session_id` plumbed through chat/websocket backends for
  claude (`backends/codex.py`), codex (`acp.py`), and droid_stream — so
  **interactive** resumption works, including the recent `compact_self`
  continuation (commits 7ee9490cd, 629b1e8b1).
- Has `session_tasks` linkage and `get_task_sessions(task_id)` ready to use.
- Has `--resume <session_id>` already wired in
  `src/gobby/agents/spawners/command_builder.py` line 93.
- Does **not** auto-populate `resume_session_id` on dispatcher-spawned agent
  re-claim. Grep for `resume_session_id\s*=` across `agents/`, `dispatch/`,
  and `workflows/` returns zero hits. The spawn path doesn't look at
  `session_tasks`.

So this isn't "build resumption" — it's wire two existing systems together.

### 4. Skill injection — different model, multica's is more isolated

Multica's `daemon/execenv/codex_home.go` builds a per-task `CODEX_HOME` and
*"writes the agent's active skills directly to `codex-home/skills/`"*. It
also sanitizes inherited config: *"Drop `[[skills.config]]` entries inherited
from the user's `~/.codex/config.toml`… Codex Desktop writes plugin-backed
skills with a `name` and no `path`, which the CLI's stricter TOML parser
rejects."* The injection is per-task, scoped to the task's runtime, with
config validation.

Gobby installs skills at `gobby install` time via `cli/installers/{claude,codex}.py`
and `skills/sync.py` — once per machine, into the user's global provider home.
Plus the gobby-skills MCP server for runtime discovery. Two real differences:

- **Per-task isolation**: multica can give task A skills that task B doesn't
  see; gobby can't.
- **Config sanitization on write**: gobby doesn't do this; we'd need it the
  first time a Codex Desktop user installs a Gobby skill bundle.

### 5. Inbox + auto-subscribe — fully in place; gobby has nothing equivalent

Schema-verified. `inbox_item` has `(recipient_type, recipient_id, severity, read, archived)`
with severity ∈ `('action_required', 'attention', 'info')`. `issue_subscriber`
auto-tracks who cares about an issue and why. WebSocket events route only to
relevant subscribers, broadcast within workspace.

Gobby has WebSocket broadcast and a `_notifier.py` for skills, but nothing
resembling a per-recipient inbox model. Search across `storage/` and `servers/`
turns up no subscriber/inbox table or route.

This pairs tightly with the actor work — only worth building once humans can
be actors.

### 6. Autopilot with explicit concurrency policy — verified

Multica migration 042 defines:

```sql
execution_mode TEXT CHECK (execution_mode IN ('create_issue', 'run_only')),
concurrency_policy TEXT NOT NULL DEFAULT 'skip'
    CHECK (concurrency_policy IN ('skip', 'queue', 'replace')),
```

Plus `autopilot_run` lifecycle states (`'pending', 'issue_created', 'running'`)
and `origin_type/origin_id` on `issue` to filter autopilot-created items.

Gobby has cron via `/schedule`, dispatch rules in `src/gobby/dispatch/rules.py`,
and pipelines with approval gates — but no canonical concurrency_policy enum
on triggers. We pick implicitly. The three-policy enum is the right shape and
the right vocabulary.

### 7. Frontend discipline — different league

Multica's `CLAUDE.md` mandates strict package boundaries:

- `packages/core/` — headless, all Zustand, no React DOM, no env access
- `packages/ui/` — atomic shadcn, **no business logic, no `@multica/core` imports**
- `packages/views/` — shared pages, **no Next.js, no React Router imports**
- Apps wire routing through `NavigationAdapter`

Plus: TanStack Query owns server state, Zustand owns client state, **never
duplicate**. WebSocket events invalidate queries instead of writing to stores.

This is the right architecture for shipping web + Electron from one codebase.
Our `web/` (haven't audited fully) almost certainly doesn't enforce this. If
we ever want a desktop app, we either adopt this discipline up front or pay
the migration tax later.

### 8. Polishing the daemon as a managed child — `daemon-manager.ts` 31KB

Multica's Electron app contains a 31 KB `daemon-manager.ts` that supervises
the daemon as a child process: log parsing
(`parse-daemon-log.{ts,test.ts}`), lifecycle, IPC bridge. This is the
"app feels native" plumbing that makes the daemon invisible to the user.
Gobby's local-first model doesn't *need* this today, but if we ship a desktop
app, this is the pattern.

## Verified gobby strengths

So we don't accidentally regress while borrowing. All verified by reading
gobby source:

- **Rule engine + workflow YAML state machines + pipelines with approval
  gates.** Multica has none of this; their automation is autopilot
  scheduling on top of a flat task queue.
- **MCP proxy with progressive discovery.** `list_mcp_servers` →
  `list_tools` → `get_tool_schema` → `call_tool`. Real token-savings,
  multi-server routing, schema caching. Multica doesn't expose tools this way.
- **Plan-coverage contract.** Acceptance items, manifest gates, typed
  deferrals, table-row decomposition rule (CLAUDE.md). Multica's plans are
  prose docs.
- **Memory system with embeddings + cross-references.** Multica has pgvector
  but no equivalent product surface to gobby-memory.
- **Dispatch architecture** (stage-manifest, mutex, audit log, allow_automation
  gate, artifacts table). Their daemon is heartbeat+push; ours is a state
  machine with ordered rules.
- **Local-first, zero-server.** Multica needs Postgres + a server even to
  self-host. Gobby works for solo devs with `uv sync` + `gobby start`.
  (Postgres migration #12761 keeps this property — see Phase 7 notes there.)
- **Cross-CLI session resumption with `compact_self` continuation.**
  Multica's session pinning is task-scoped persistence; gobby's resumption
  works across providers and across compactions, including for *interactive*
  sessions. Don't lose this.

## Recommendations — re-tiered given postgres migration is live

Plan #12761 is doing a cold-cutover migration to Postgres (psycopg v3, raw SQL
files, pg_search for FTS, Docker default). That changes the tier rankings:
schema-touching borrowings can ride the migration window almost for free, so
they move up.

### Tier 1 — borrow now, ride the postgres migration

- **Polymorphic actor schema.** Add `actor_type ENUM('human','agent','system')`
  and `actor_id UUID` to tasks, comments (if/when we add them),
  audit/dispatch logs, and any "assigned to" surface. Backfill all existing
  rows as `agent`. This is a fold-into-baseline-schema move while we're already
  rewriting DDL — much cheaper than doing it as a follow-on after Postgres
  cutover. **Not adversarial-review-worthy in isolation, but it should land as
  a coverage row inside the postgres-migration plan**, not as a separate
  ticket, because the read-path audit is part of that plan's existing scope.
- **Auto-wire dispatcher session resumption.** In `agents/spawners/`, when
  spawning for a task that has prior linked sessions for the same agent kind,
  populate `resume_session_id` from `get_task_sessions(task_id)` and reuse
  the prior worktree path. Add an opt-out path for stale-worktree /
  branch-divergence detection. **Probably one PR; tight plan with acceptance
  items, no adversary needed.**
- **Per-task execution-history endpoint.** We have transcripts; we just need
  `GET /api/tasks/:id/executions` returning one row per run with tool calls,
  reasoning, errors. Mostly a routes change in `servers/routes/tasks/`.

### Tier 2 — borrow soon

- **Per-task skill injection at agent spawn** (multica's
  `codex-home/skills/` model). Keep MCP-discovery for non-static skills, but
  also drop static skill markdown into a per-task provider home directory
  on spawn. Adds isolation we don't have today. Pairs naturally with our
  worktree/clone isolation. Borrow multica's TOML-sanitization pattern —
  Codex Desktop's plugin entries break the CLI parser.
- **Concurrency policy enum on triggers** (`skip|queue|replace`). Adopt the
  vocabulary on dispatch rules and on `/schedule`. Small change; prevents
  real bugs in overlap semantics.
- **Artifact-only worktree GC.** Add a glob-driven cleaner that preserves
  `.git` + tracked sources and drops `node_modules`, `.venv`, `dist`,
  `target`, `__pycache__`, etc. Expose as a third mode alongside our existing
  worktree lifecycle. Real disk-cost win for users with many tasks.
- **One-command `gobby setup`.** Wraps init + install + start (+ optional
  auth) into one verb. Lowers first-run friction.

### Tier 3 — defer until team mode is on the table

- **Server/daemon split (multi-user).** When we want multi-user, multica is
  the reference: server holds state, daemons push+poll, agent execution
  stays on user infra. Don't do this until product strategy says yes.
- **Inbox + issue_subscriber model.** Pairs with humans-as-actors. Only
  worth it once tasks have human participants beyond the "user who issued
  the prompt."
- **Multi-profile daemon.** Nice-to-have for power users; do it when one
  asks.
- **Provider plurality (3 → 11).** Each adapter is glue, not architecture.
  Add opportunistically when a user hits us with a request, not as a focused
  push.

### Won't borrow

- **Frontend discipline as a forced refactor.** Adopt the rules going forward
  for new code in `web/` and any future desktop app, but a from-scratch
  reorganization is a tax we don't need today.
- **Their lighter plan model.** Plan-coverage contract is genuinely better
  governance.
- **Replacing dispatch with polling.** Our state-machine dispatcher is more
  capable. If we add push wake-ups from the postgres tier (LISTEN/NOTIFY),
  do it as a perf optimization, not a model change.

## Suggested order of execution

1. **Fold actor schema into postgres-migration plan #12761** as new
   acceptance items in the relevant baseline-schema section. Do this before
   that plan exits its current expansion phase, otherwise it's a
   harder-to-justify follow-on.
2. **Auto-wire dispatcher session resumption** (independent of postgres
   work; can ship this week).
3. **Per-task execution-history endpoint** (independent; can ship in
   parallel with #2).
4. After postgres cutover lands: per-task skill injection, concurrency-policy
   enum, artifact-only GC, `gobby setup`.
5. Inbox/subscriber and team mode are gated on product direction, not
   engineering readiness.

## Open questions to clarify before execution

- Is the postgres migration plan past its adversary-review verdict? If yes,
  folding actor columns in is more disruptive — needs to go through plan
  amendment rather than an additive coverage row.
- For session resumption auto-wire: do we want it on by default, or opt-in
  per task? My read: on by default with a divergence check (HEAD changed,
  worktree gone, agent_kind mismatch) that falls back to fresh.
- Is there appetite for a `gobby setup` rename of the install flow, or do
  we keep the verbs separate?
