# Gobby Roadmap

Gobby is a local-first control plane for AI coding tools: persistent sessions,
task graphs, workflows, hooks, MCP proxying, agents, memory, and deterministic
automation around the tools developers already use.

Last refreshed: July 10, 2026.

The data migration is complete. PostgreSQL is the runtime hub, FalkorDB is the
graph backend, and `.gobby/tasks.jsonl` is a gitignored local backup rather than
a checked-in projection. The roadmap now tracks the post-migration release line.

## 0.5.0 - New Baseline

0.5.0 is the new baseline release after the PostgreSQL and FalkorDB cutover.

- Ship the current Python daemon as the supported local-first runtime.
- Treat PostgreSQL as the only runtime hub and FalkorDB as the supported graph
  backend across daemon, web UI, admin payloads, setup, and docs.
- Keep `.gobby/tasks.jsonl` and `.gobby/memories.jsonl` as automated pre-push
  exports with manual-only import, so a lost hub is recoverable locally.
- Tighten operator docs, install/status output, and release notes around the
  new storage baseline.

## 0.5.0+ - UI Hardening And Rust Port Work

After the baseline release, the main work is web UI hardening and preparing the
Rust port.

- Harden chat, sessions, tasks, workflows, cron, projects, compact layouts, and
  shared design tokens until the web UI is solid enough for daily Gobby Pro use.
- Close attached-session parity gaps: context usage, mode/model sync,
  attachments, persona switching, STT/TTS, and first-class web chat behavior.
- Continue plan registry APIs and UI editors so stage and build-profile shape
  can evolve without hand-editing storage or YAML internals.
- Finish logging cleanup before enforcing logging-format rules: config reset,
  runtime-vs-app log separation, normalized handlers, automation logs for cron
  and dispatch, and quieter routine logging.
- Prepare the Rust port by freezing route contracts (safe to start pre-ship;
  they are contract docs and test artifacts). Compare/delegation plumbing and
  sidecar implementation start after 0.5.0 ships. Shared-primitive extraction
  under `crates/` is already essentially complete in `gobby-core`.

## 0.6.0 - Rust Port Release

0.6.0 is the Rust port release. It is an incremental strangler port, not a
rewrite. The destination shape is decided in
`docs/architecture/evolution.md`: one daemon binary named `gdaemon`
(package `gobby-daemon`) with standalone/hub/node modes. The interactive
product people run is `gobby` (the TUI). The sidecar below is the
transition vehicle, not the end state.

- Python remains the public daemon and behavioral reference until each boundary
  passes parity, observability, and rollback gates.
- The `gobby-daemon` sidecar (`gdaemon`) runs on internal port `:60890`, with
  Python delegating selected route families behind explicit flags.
- Compare mode calls both implementations and returns the Python response until
  parity is proven.
- First wave: contract fixtures, compare/delegation plumbing, the sidecar
  shell with health/config reads, then reduced `GET /api/tasks` as the first
  DB-backed boundary.
- Second wave: the external-MCP transport multiplexer as a delegated backend
  behind Python's front door; internal MCP servers migrate only after it
  exists.
- The bridgehead lives under `crates/`: `gcode`, `ghook`, `gwiki`, and
  `gobby-core` shared primitives.
- Execution order and milestones live in `docs/plans/rust-migration-epic.md`.

## 0.6.0+ - Gobby Pro Sync

Gobby Pro starts with remote sync from multiple Gobby-controlled machines.

- Multi-daemon discovery and handshake.
- Opt-in encrypted sync for tasks, memories, and session metadata.
- Operator controls for machines you own, with local-first behavior preserved.
- Sync conflict handling, audit trails, and release packaging for the commercial
  layer.

## 0.7.0+ - Gobby Pro Beta

The Pro beta introduces fleet management and a shared dashboard for all Gobby
machines.

- Fleet inventory, health, and remote command.
- Shared task boards, team workflows, and review state across machines.
- Dashboard views for sessions, builds, agents, validation, and sync health.
- Enterprise controls for audit, policy, and team operations.

## Later - 1.0.0 Prep

Once the Pro beta stabilizes, the focus moves to 1.0.0 readiness.

- Stabilize public APIs, configuration, workflow definitions, and hook behavior.
- Polish install, upgrade, recovery, and operator documentation.
- Add SWE-bench evaluation from `docs/plans/SWE-BENCH.md`: eval run/result
  storage, `gobby eval`, Docker-backed harness, trajectory capture, leaderboard
  artifacts, score tracking, and Gobby-enabled vs baseline A/B tests.
- Use the benchmark results to drive final release gates for 1.0.0.

## Baseline Already Shipped

0.4.x shipped the task-to-PR loop as the supported path: persistent sessions,
cross-CLI handoffs, task lifecycle and validation, MCP progressive discovery,
workflows, pipelines, rule enforcement, agent spawning, memory search, skills,
integrations, and the web UI surfaces needed to operate Gobby locally.
