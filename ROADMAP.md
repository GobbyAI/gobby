# Gobby Roadmap

Gobby is a local-first control plane for AI coding tools: persistent sessions,
task graphs, workflows, hooks, MCP proxying, agents, memory, and deterministic
automation around the tools developers already use.

This roadmap starts from the post-0.4.0 planning state.

- `>0.4.0`: active task queue plus active `.gobby/plans` work.
- `0.5.0`: Rust migration.
- Later: commercial sync, native apps, marketplace, and stack-specific bundles.

## Source Inventory

Last refreshed: May 8, 2026.

- Active task queue from `gobby-tasks`: 41 ready tasks and 58 blocked tasks were
  visible during refresh. The queue is summarized by roadmap theme rather than
  copied verbatim.
- Active plan files in `.gobby/plans`: SkillsMP install rewrite, FalkorDB graph
  migration, PostgreSQL hub migration, and memory recall helper.
- Top-level idea plans in `docs/plans`: SWE-bench evaluation, plugin system v2,
  UX/design agent pipeline, hook immutability, and Rust migration.
- `docs/plans/completed` and `docs/plans/abandoned` are treated as archive
  material unless a new task or active plan revives them.

## >0.4.0 Next

### Runtime Reliability

Current priority is to reduce operational friction in the live daemon and web
chat surfaces.

- Fix active web-chat failures: Gemini no-response investigation `#11641` and
  Codex interrupt RPC for new conversations `#11642`.
- Repair task dispatch and validation reliability: missing developer-agent
  targeting `#13822`, validator pre-flight worktree/session behavior `#12259`,
  and plan-adversary deadlock recovery `#13177`.
- Preserve terminal agents across daemon restart `#14424`.
- Stabilize Linear sync resilience `#14461`.

### Lifecycle, Planning, And Dispatch

The task system is moving from ad hoc lifecycle transitions toward manifest-led
stage state and stronger planning rails.

- Stage manifest cutover `#13826`: finish the 5-state lifecycle expansion
  target, then remove legacy review transition aliases `#13767`.
- Interactive plan-adversary loop `#12037`: make `/gobby plan` review loops
  reliable and non-deadlocking.
- Planning guardrails: block non-plan edits while the interactive planning lock
  is held `#12215`, raise expansion capacity as a short-term bridge `#13343`,
  and continue the integration worktree coordination task `#12624`.
- Registry editing: stage and build-profile APIs plus UI editors `#14140`.
- PR/merge automation: plan-aware PR and merge skill with AI conflict
  resolution `#13552`.

### Data And Persistence

These are the large platform migrations planned for the post-0.4 line.

- PostgreSQL hub migration `#12761` and `.gobby/plans/task-12761-postgres-hub-migration.md`
  replace SQLite as the runtime hub database. The plan uses a cold cutover,
  `psycopg` v3, plain SQL migrations, `pg_search`, a `HubDatabase` protocol,
  dual-backend test infrastructure, and a one-shot migration path.
- FalkorDB graph migration `#12746` and `.gobby/plans/task-12746-neo4j-falkordb-swap.md`
  replace Neo4j with FalkorDB across Python daemon graph writes, Rust read
  clients, web UI, admin payloads, setup wizard, and docs. Existing graph data
  is rebuilt from SQLite/Qdrant/code-index sources instead of migrated.
- Memory recall helper `#12898` and `.gobby/plans/task-12898-memory-recall-helper.md`
  add a bounded background helper agent that searches memory per turn, cancels
  stale helpers, and injects fresh results once into the parent session.

### Code Health

The queue still contains structural debt that blocks predictable automation.

- Active monolith refactor backlog `#12730`: continue splitting oversized
  Python/TypeScript/CSS modules so future agents have safer ownership
  boundaries.
- Logging cleanup `#12010`: standardize parameterized logger calls, separate
  runtime output from application logs, add automation logs, and enable Ruff
  logging-format enforcement after cleanup.
- Build service refactor `#14446`, shared code-block line-number rendering
  `#13794`, and shared activity filter primitives `#14308`.

### Extensibility

Gobby's extension model should become easier to use without weakening the MCP
proxy's progressive-discovery contract.

- SkillsMP install rewrite `#12068` and `.gobby/plans/task-12068-skillsmp-install-rewrite.md`
  treat SkillsMP as a search index over GitHub-hosted skills, ignore `skillUrl`
  as an install source, resolve supported GitHub URL shapes, and keep public MCP
  and storage interfaces stable.
- Plugin system v2 from `docs/plans/plugins-v2-draft.md`: first polish MCP
  routing DX with `gobby mcp`, enable/disable/test endpoints, validation on add,
  and `.gobby/mcp-servers.yaml` sync; then add plugin manifests that compose MCP
  servers, skills, rules, pipelines, and agents.
- Hook immutability from `docs/plans/hook-immutability-rtk-example.md`: make
  Gobby the protected owner of supported hook surfaces after install, preserve
  foreign rewrites for analysis, and offer explicit migration for known tools
  such as RTK.

### UX, Documentation, And Product Polish

Post-0.4 work includes both visible UI polish and operator documentation.

- UX improvements post-0.4.0 `#14327`: attached-session context indicators,
  attached-mode model/reasoning sync, attachments relay, persona switching, and
  STT/TTS follow-ups.
- Long-tail impeccable polish `#13463`: finish non-chat page Tailwind migration
  and anti-pattern cleanup.
- UI precision fixes such as matching the activity-panel toggle height to the
  New Chat button `#14463`.
- Uncovered feature guides `#14449`: web UI, canvas artifacts, cron, observability,
  prompts, plans, providers/models, test quality, admin operations, and guide
  index updates.
- UX/design pipeline from `docs/plans/impeccable-ux-agents-draft.md`: add a UX
  planning track, UX plan-draft/review skills, `ux-developer`, `ux-review` stage,
  design acceptance kinds, and screenshot-backed Chrome DevTools evidence.

### Evaluation

Gobby needs a repeatable benchmark story before claiming agent-quality gains.

- SWE-bench plan from `docs/plans/SWE-BENCH.md`: add eval run/result storage,
  a `gobby eval` CLI, Docker-backed harness, trajectory capture, leaderboard
  export artifacts, score tracking, and A/B tests for Gobby-enabled vs baseline
  Claude Code runs.

## 0.5.0 - Rust Migration

Rust migration is the 0.5.0 line, driven by `docs/plans/rust-migration-epic.md`.
It is an incremental strangler migration, not a rewrite.

### Direction

- Python remains the public daemon and behavioral reference until a boundary
  passes parity, observability, and rollback gates.
- Rust work lives in `~/Projects/gobby-cli`, preserving `gcode`, `gsqz`, and
  `gloc` while extracting shared foundations.
- Rust sidecars run on internal ports, with Python delegating selected route
  families behind explicit flags.
- Compare mode calls Rust, compares with Python, and returns the Python response
  until parity is proven.

### Readiness Gate

The 0.5.0 migration starts after 0.4 hardening has frozen the first boundary
contracts and produced fixtures for:

- `GET /api/admin/health`
- `GET /api/config/schema`
- `GET /api/config/values`
- `GET /api/tasks`

Phase 0 contract-freezing and low-risk Rust foundation work may run before full
cutover, but route migration belongs to the 0.5.0 track.

### Migration Phases

1. Freeze Python-owned contracts and replay fixtures.
2. Add Python compare and delegation plumbing.
3. Extract `gobby-cli` shared foundations.
4. Build the Rust daemon shell and migrate low-risk read routes.
5. Migrate the first DB-backed task read boundary.
6. Migrate reduced session reads.
7. Handle late complex boundaries.
8. Cut over, verify rollback, and retire migration scaffolding.

## Later

### Pro Sync And Multi-Daemon

- Multi-daemon discovery and handshake.
- Fleet inventory, health, and remote command.
- Opt-in encrypted sync for tasks, memories, and session metadata.
- Shared task boards, team workflows, audit, and enterprise controls.

### Native Apps

- Desktop app with tray lifecycle and bundled daemon.
- Mobile companion for observing sessions, reviewing tasks, and approving gates.

### Ecosystem

- Public plugin registry and compatibility checks.
- Stack-specific starter packs for hooks, workflows, skills, and task templates.
- Additional CLI integrations after the core Claude, Gemini, and Codex paths
  remain stable under the post-0.4 hardening work.

## Baseline Already Shipped

0.4.0 is treated as the current baseline for this roadmap. The shipped platform
includes persistent sessions, cross-CLI handoffs, task lifecycle and validation,
MCP progressive discovery, workflows, pipelines, rule enforcement, agent
spawning, memory search, skills, integrations, and the web UI surfaces needed to
operate Gobby locally.
