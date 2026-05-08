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

- Active task list: supplied directly on May 8, 2026, and treated as
  authoritative for this roadmap refresh. Unlisted stale/open task metadata is
  intentionally omitted.
- Roadmap-active `.gobby/plans` files: FalkorDB graph migration, PostgreSQL hub
  migration, and memory recall helper. Other plan artifacts are omitted unless
  they appear in the active task list.
- Top-level idea plans in `docs/plans`: SWE-bench evaluation, plugin system v2,
  UX/design agent pipeline, hook immutability, and Rust migration.
- `docs/plans/completed` and `docs/plans/abandoned` are treated as archive
  material unless a new task or active plan revives them.

## >0.4.0 Next

### Data And Persistence

- PostgreSQL hub migration `#12761` and `.gobby/plans/task-12761-postgres-hub-migration.md`
  replace SQLite as the runtime hub database. The plan uses a cold cutover,
  `psycopg` v3, plain SQL migrations, `pg_search`, a `HubDatabase` protocol,
  dual-backend test infrastructure, and a one-shot migration path.
  - Phase 0: re-flatten the SQLite baseline `#14062`, with migration
    240-242 folding in `#14070`.
  - Phase 1: service and bootstrap support `#14063`, including failing tests
    `#14071`, driver dependency `#14072`, compose service `#14073`, Dockerfile
    `#14076`, installer/status CLI `#14074`, bootstrap config `#14075`,
    activate/deactivate commands `#14077`, and refactor pass `#14078`.
  - Phase 2: dual-backend test infrastructure `#14064`, including tests
    `#14079`, compose/CI `#14080`, refactor pass `#14081`, schema-per-worker
    fixture `#14082`, backend-parametrized fixtures `#14083`, and dialect
    parity tests `#14084`.
  - Phase 3: backend-neutral storage and migration runner `#14065`, including
    tests `#14085`, `HubDatabase` protocol `#14086`, SQLite shim `#14087`,
    migration runner rewrite `#14089`, Postgres implementation `#14088`,
    refactor pass `#14090`, and SQL/row-consumer portability tasks
    `#14091` through `#14094`.
  - Phase 4: PostgreSQL schema and query parity `#14066`, including tests
    `#14095`, baseline schema `#14096`, `pg_search` BM25 `#14097`, search
    backend port `#14098`, refactor pass `#14099`, and SQL parity/concurrency
    audits `#14100` through `#14103`.
  - Phase 5: one-shot SQLite to PostgreSQL migration tool `#14067`, including
    tests `#14104`, sequence reseed `#14107`, validation checks `#14106`,
    migrate-from-sqlite command `#14105`, and refactor pass `#14108`.
  - Phase 6: cold cutover to PostgreSQL runtime `#14068`, including tests
    `#14109`, audit log `#14110`, refactor pass `#14111`, concurrency re-audit
    `#14112`, cutover runbook `#14113`, and rollback runbook `#14114`.
  - Phase 7: remove SQLite runtime support `#14069`, including tests `#14115`,
    OS keyring credentials `#14116`, refactor pass `#14117`, SQLite runtime
    removal `#14118`, FTS5/SQLite migration removal `#14119`, and docs updates
    `#14120`.
- FalkorDB graph migration `#12746` and `.gobby/plans/task-12746-neo4j-falkordb-swap.md`
  replace Neo4j with FalkorDB across Python daemon graph writes, Rust read
  clients, web UI, admin payloads, setup wizard, and docs. Existing graph data
  is rebuilt from SQLite/Qdrant/code-index sources instead of migrated.
- Memory recall helper `#12898` and `.gobby/plans/task-12898-memory-recall-helper.md`
  add a bounded background helper agent that searches memory per turn, cancels
  stale helpers, and injects fresh results once into the parent session.

### Logging And Code Health

- Logging cleanup `#12010`: clean the repo logging system before enforcing
  logging-format rules.
  - Design and implement logging config reset `#13909`.
  - Separate runtime output from application logs `#13910`.
  - Normalize logger handlers, routing, and formats `#13911`.
  - Add automation log for cron and dispatch `#13912`.
  - Reduce noisy routine logging `#13913`.
  - Update operator docs for log files `#13914`.
  - Migrate parameterized logger calls by subsystem `#13915`, split across
    servers/MCP proxy `#13916`, agents/hooks/workflows `#13917`,
    storage/memory/sync/search `#13918`, and CLI/runtime/remaining modules
    `#13919`.
  - Enable Ruff logging-format enforcement `#13920`.

### Planning Infrastructure

- Plan registry APIs and UI editors `#14140`: expose stage and build-profile
  registries through APIs and editing surfaces so lifecycle shape can evolve
  without hand-editing storage or YAML internals.

### UX And Attached Sessions

- UX improvements post-0.4.0 `#14327` focus on attached-session parity with
  first-class web chat:
  - Attached-session context usage indicator `#14328`.
  - Sync `chat_mode`, `reasoning_effort`, and model for attached tmux sessions
    `#14334`.
  - Attachments relay in attached mode `#14329`.
  - Persona switching in attached mode via `/gobby persona` `#14330`.
  - STT/TTS in attached mode `#14331`.

### Candidate Ideas From `docs/plans`

These are idea sources, not active task-list items until promoted.

- SWE-bench plan from `docs/plans/SWE-BENCH.md`: add eval run/result storage,
  a `gobby eval` CLI, Docker-backed harness, trajectory capture, leaderboard
  export artifacts, score tracking, and A/B tests for Gobby-enabled vs baseline
  Claude Code runs.
- Plugin system v2 from `docs/plans/plugins-v2-draft.md`: first polish MCP
  routing DX, then add plugin manifests that compose MCP servers, skills, rules,
  pipelines, and agents.
- UX/design pipeline from `docs/plans/impeccable-ux-agents-draft.md`: add a UX
  planning track, UX plan-draft/review skills, `ux-developer`, `ux-review` stage,
  design acceptance kinds, and screenshot-backed Chrome DevTools evidence.
- Hook immutability from `docs/plans/hook-immutability-rtk-example.md`: protect
  Gobby-owned hook surfaces after install, preserve foreign rewrites for
  analysis, and offer explicit migration for known tools such as RTK.

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
