# Rust Migration Epic

## Summary

This is the active Rust migration plan for Gobby. It replaces the earlier
`rust-port` drafts with an execution-focused epic that is structured around
phases, gates, and backlog-ready atomic work items.

This is not a monolithic rewrite. It is an incremental strangler migration for
a live Python daemon:

- Rust migration code lives in `~/Projects/gobby-cli`
- Python in `~/Projects/gobby` remains the behavioral reference until explicit
  cutover
- Traffic shifts by route family or boundary, not by big-bang daemon swap
- Parity, observability, and rollback safety come before redesign
- The atomic work items below are planning inventory only; they are not created
  as Gobby tasks by this document

## Current State

- `~/Projects/gobby` is the product repo and current source of truth
- `~/Projects/gobby-cli` is the long-lived Rust workspace and already contains
  `gcode`, `gsqz`, and `gloc`
- Python owns the live daemon, schema policy, fixtures, and rollout control
- Rust does not yet have a shared `gobby-core` crate or a `gobby-daemon` crate

## Non-Negotiable Decisions

### Repo Ownership

- `~/Projects/gobby` owns current behavior, compatibility fixtures, rollout
  gates, and the authoritative Python implementation
- `~/Projects/gobby-cli` owns Rust crates, binaries, shared extractions, and
  replacement implementations

### Migration Shape

- Migration is boundary-first, not storage-first
- The public daemon stays Python-fronted during migration
- Rust runs beside Python on alternate internal ports and is cut in behind
  explicit routing or feature flags
- We do not start with a full storage port or a workflow/rule rewrite

### Branching

- No long-lived Rust migration branch in `gobby`
- Migration work lands continuously in the mainline of each repo
- Cross-repo behavior is stabilized with fixtures and parity checks, not with a
  giant integration branch

### Authority and Cutover

- Python is authoritative until a boundary passes parity, observability, and
  rollback gates
- A Rust component is not considered migrated because it exists; it must prove
  equivalent behavior against the Python contract

## Migration Principles

1. Preserve product behavior first.
2. Migrate externally visible boundaries before deep internals.
3. Keep every milestone reversible.
4. Move one responsibility boundary at a time.
5. Do not mix migration work with speculative architecture cleanup.
6. Keep existing Rust utilities healthy while extracting shared foundations.

## Atomic Task Standard

Every migration work item in this epic should be small enough to become a
single real Gobby task and a single focused PR.

An atomic item should satisfy all of the following:

- One repo owns the change
- One primary boundary or shared seam is being changed
- One rollback story exists
- One validation target is clear
- If the title naturally needs `and`, it should usually be split

If a proposed task includes multiple routes, multiple repos, or multiple
rollback mechanisms, split it.

## Readiness Gate

Rust implementation work beyond low-risk extraction and prototyping starts only
after the `0.4.0` hardening pass establishes stable contracts for the first
migration boundaries.

The readiness gate is met when all of the following are true:

- `0.4.0` launch blockers are resolved and remaining Python work is hardening,
  not major feature churn
- The first migration surfaces are frozen as exact contracts:
  - `GET /api/admin/health`
  - `GET /api/config/schema`
  - `GET /api/config/values`
  - `GET /api/tasks`
- Golden fixtures exist for success, error, and degraded-daemon behavior on
  those surfaces
- Python exposes enough comparison hooks and logging to detect Rust mismatches
  before any cutover
- Rollback to the pure Python path is verified for the first migration stage

The following boundaries are explicitly deferred until later phases:

- `GET /api/admin/status`
- `POST /api/hooks/execute`
- `GET /api/sessions` with `include_resumability=true`
- Storage-first migration of unrelated tables or managers

## Repository Contracts

### Python Repo Responsibilities

- Define the behavioral contract for each migrated boundary
- Own the canonical schema and migration policy until final cutover
- Host parity fixtures and acceptance criteria
- Remain the public listener on `:60887` and `:60888` during strangler phases
- Provide feature flags or explicit delegation and proxy points for migrated
  boundaries

### Rust Workspace Responsibilities

- Add shared crates and replacement binaries in `gobby-cli`
- Preserve `gcode`, `gsqz`, and `gloc` behavior while foundations are extracted
- Implement Rust replacements against Python-owned fixtures
- Run side-by-side with Python on alternate ports and processes

## Rollout Model

### Public Front Door

- Python remains the public daemon on `:60887` and `:60888`
- Rust runs on alternate internal ports during migration
- `:60889` is the default first HTTP port for a Rust sidecar daemon
- The first unit of traffic shifting is a route family or hook boundary, not an
  all-or-nothing daemon swap

### Strangler Routing

- Unmigrated routes stay in Python
- Migrated routes can be delegated from Python to Rust behind explicit feature
  flags
- Compare mode calls Rust, compares results, and still returns the Python
  response until parity is proven
- Any mismatch disables the route flag and falls back to Python without a full
  release rollback

### Hook Integration

- Hook migration stays behind the existing hook API contract
- If a Rust `gobby-hook` binary is introduced, it still targets the public
  daemon contract first
- Hook cutover is allowed only after HTTP-side parity and degraded behavior are
  proven

## Phase Overview

- Phase 0 freezes contracts and builds the fixture corpus
- Phase 1 adds Python-side compare and delegation plumbing
- Phase 2 extracts shared Rust foundations in `gobby-cli`
- Phase 3 migrates low-risk read-only boundaries
- Phase 4 migrates the first DB-backed task read boundary
- Phase 5 migrates reduced session reads
- Phase 6 handles late complex boundaries
- Phase 7 covers cutover, fallback, and retirement of migration scaffolding

## Phase 0: Contract Freeze and Fixture Corpus

**Goal:** Turn the first migration surfaces into exact replayable contracts
owned by Python.

### Scope

- Freeze exact contracts for the first four migration surfaces
- Record success, error, and degraded-daemon fixtures
- Build a Python-baseline replay harness

### Non-goals

- Serving production traffic from Rust
- Porting storage internals
- Defining hook execution parity in full

### Backlog-Ready Atomic Items

- `R0-01` Define the exact contract for `GET /api/admin/health`
- `R0-02` Define the exact contract for `GET /api/config/schema`
- `R0-03` Define the exact contract for `GET /api/config/values`
- `R0-04` Define the reduced v1 contract for `GET /api/tasks`
- `R0-05` Capture success fixtures for `GET /api/admin/health`
- `R0-06` Capture success fixtures for `GET /api/config/schema`
- `R0-07` Capture success fixtures for `GET /api/config/values`
- `R0-08` Capture success fixtures for `GET /api/tasks`
- `R0-09` Capture error fixtures for `GET /api/admin/health`
- `R0-10` Capture error fixtures for `GET /api/config/schema`
- `R0-11` Capture error fixtures for `GET /api/config/values`
- `R0-12` Capture error fixtures for `GET /api/tasks`
- `R0-13` Capture degraded-daemon fixtures for `GET /api/admin/health`
- `R0-14` Capture degraded-daemon fixtures for `GET /api/config/schema`
- `R0-15` Capture degraded-daemon fixtures for `GET /api/config/values`
- `R0-16` Capture degraded-daemon fixtures for `GET /api/tasks`
- `R0-17` Build a replay harness that runs the fixture corpus against Python

### Exit Criteria

- The first migration surfaces are explicitly named and frozen
- Fixtures are committed and stable
- The replay harness passes against Python as the baseline

## Phase 1: Python Compare and Delegation Plumbing

**Goal:** Make Python capable of comparing and delegating individual route
families to a Rust sidecar without surrendering authority.

### Scope

- Route-scoped feature flags
- Compare-mode invocation and mismatch handling
- Route-scoped rollback to Python
- Structured observability for side-by-side execution

### Non-goals

- Implementing Rust route handlers
- Switching the public listener to Rust

### Backlog-Ready Atomic Items

- `R1-01` Add route-scoped Rust target configuration in Python
- `R1-02` Add a reusable compare wrapper for delegated GET routes
- `R1-03` Add mismatch logging for compare-mode responses
- `R1-04` Add metrics for compare-mode requests, mismatches, and fallbacks
- `R1-05` Add route-scoped fallback behavior when Rust is unavailable
- `R1-06` Add developer smoke coverage for dual-daemon compare mode

### Exit Criteria

- Python can compare and delegate route families individually
- Mismatches are observable
- Route-scoped rollback is immediate and tested

## Phase 2: `gobby-cli` Foundation Extraction

**Goal:** Turn the existing Rust utilities into a reusable foundation for the
daemon migration without destabilizing them.

### Scope

- Add `gobby-core` to `gobby-cli`
- Extract only the shared seams required by current crates or near-term daemon
  work
- Keep `gcode`, `gsqz`, and `gloc` healthy while extractions land

### Non-goals

- Building the daemon itself
- Porting schema ownership to Rust
- Extracting speculative abstractions that no boundary needs yet

### Backlog-Ready Atomic Items

- `R2-01` Scaffold the `gobby-core` crate
- `R2-02` Extract bootstrap resolution into `gobby-core`
- `R2-03` Extract daemon URL resolution into `gobby-core`
- `R2-04` Extract project root helpers into `gobby-core`
- `R2-05` Extract project metadata helpers into `gobby-core`
- `R2-06` Extract daemon HTTP client utilities into `gobby-core`
- `R2-07` Extract SQLite connection helpers into `gobby-core`
- `R2-08` Migrate `gcode` to the extracted `gobby-core` helpers
- `R2-09` Migrate `gsqz` to the extracted `gobby-core` helpers
- `R2-10` Verify `gcode` and `gsqz` test coverage stays green after extraction

### Exit Criteria

- `gcode` and `gsqz` use `gobby-core` for shared concerns
- Existing Rust utility behavior is preserved
- The extracted APIs are sufficient to bootstrap `gobby-daemon`

## Phase 3: Rust Daemon Shell and Low-Risk Reads

**Goal:** Introduce a Rust daemon process beside Python and cut in the safest
read-only boundaries first.

### Scope

- Add `gobby-daemon` to `gobby-cli`
- Run it on an alternate internal port
- Implement the first low-risk read-only surfaces
- Wire compare mode and opt-in delegation per route

### Non-goals

- Porting all endpoints
- Migrating the task or session write paths
- Migrating `/api/admin/status`

### Backlog-Ready Atomic Items

- `R3-01` Scaffold the `gobby-daemon` crate and basic HTTP server
- `R3-02` Implement shared request context and JSON error behavior in Rust
- `R3-03` Implement `GET /api/admin/health` in Rust
- `R3-04` Implement `GET /api/config/schema` in Rust
- `R3-05` Implement `GET /api/config/values` in Rust
- `R3-06` Enable compare mode for `GET /api/admin/health`
- `R3-07` Enable compare mode for `GET /api/config/schema`
- `R3-08` Enable compare mode for `GET /api/config/values`
- `R3-09` Enable opt-in delegation for `GET /api/admin/health`
- `R3-10` Enable opt-in delegation for `GET /api/config/schema`
- `R3-11` Enable opt-in delegation for `GET /api/config/values`
- `R3-12` Prove route-scoped rollback for `GET /api/admin/health`
- `R3-13` Prove route-scoped rollback for `GET /api/config/schema`
- `R3-14` Prove route-scoped rollback for `GET /api/config/values`

### Exit Criteria

- Rust serves the low-risk route set on its own port
- Python can compare and delegate those routes selectively
- Rollback to Python is immediate and proven

## Phase 4: First DB-Backed Boundary

**Goal:** Migrate the first read-only route that exercises live product data
without opening the full storage-first trap.

### Scope

- `GET /api/tasks` as the first DB-backed route family
- A deliberately reduced v1 contract if needed
- Compare mode first, then opt-in delegation

### Non-goals

- Porting all task endpoints
- Porting task mutations
- Porting unrelated storage managers because they are nearby

### Backlog-Ready Atomic Items

- `R4-01` Narrow the supported v1 contract for `GET /api/tasks`
- `R4-02` Capture the `GET /api/tasks` fixture matrix for filters, paging, and
  error cases
- `R4-03` Implement `GET /api/tasks` in Rust
- `R4-04` Enable compare mode for `GET /api/tasks`
- `R4-05` Enable opt-in delegation for `GET /api/tasks`
- `R4-06` Prove route-scoped rollback for `GET /api/tasks`
- `R4-07` Define the exact contract for `GET /api/tasks/{task_id}`
- `R4-08` Implement `GET /api/tasks/{task_id}` in Rust after list parity is
  stable

### Exit Criteria

- The first DB-backed route family runs from Rust in compare mode and opt-in
  mode
- Parity holds for normal, empty, invalid, and degraded scenarios
- Rollback remains route-scoped and immediate

## Phase 5: Reduced Session Read Migration

**Goal:** Migrate session reads only after the simpler task boundary is stable,
and only for a reduced contract at first.

### Scope

- `GET /api/sessions` with a reduced v1 surface
- No resumability enrichment in the first pass
- Compare mode first, then opt-in delegation

### Non-goals

- Porting session writes
- Porting resumability logic in the first session-read pass
- Porting statusline ingestion or websocket-side behavior

### Backlog-Ready Atomic Items

- `R5-01` Define the reduced v1 contract for `GET /api/sessions`
- `R5-02` Capture fixtures for reduced `GET /api/sessions`
- `R5-03` Implement reduced `GET /api/sessions` in Rust
- `R5-04` Enable compare mode for reduced `GET /api/sessions`
- `R5-05` Enable opt-in delegation for reduced `GET /api/sessions`
- `R5-06` Decide whether resumability stays Python-only or becomes a later
  boundary

### Exit Criteria

- Reduced session reads are stable under compare mode and opt-in delegation
- Resumability remains explicitly deferred or separately planned

## Phase 6: Late Complex Boundaries

**Goal:** Migrate the boundaries that are too coupled or behavior-heavy to be
good early candidates.

### Scope

- `/api/admin/status`
- Hook execution and degraded behavior
- Session resumability enrichment if it still needs migration
- Any internal subsystem work required by those externally visible boundaries

### Non-goals

- A wholesale storage port before API parity
- A speculative rewrite of workflows, agents, memory, or pipelines

### Backlog-Ready Atomic Items

- `R6-01` Define a reduced or decomposed contract for `GET /api/admin/status`
- `R6-02` Capture success, error, and degraded fixtures for
  `GET /api/admin/status`
- `R6-03` Implement the agreed `GET /api/admin/status` contract in Rust
- `R6-04` Freeze the `POST /api/hooks/execute` contract
- `R6-05` Capture hook allow, block, error, and degraded-daemon fixtures
- `R6-06` Implement hook adapter-selection parity in Rust
- `R6-07` Implement graceful hook-error parity in Rust
- `R6-08` Define the parity contract for web-chat hold-open behavior
- `R6-09` Implement web-chat hold-open parity in Rust
- `R6-10` Implement session resumability parity only if it is still needed

### Exit Criteria

- The remaining complex externally visible boundaries are explicit, tested, and
  no longer hidden inside vague "selected routes" language
- Every Rust-owned persistence path has coexistence and rollback checks

## Phase 7: Cutover, Fallback, and Retirement

**Goal:** Promote Rust from delegated implementation to default runtime for the
migrated surface while preserving a real fallback window.

### Scope

- Expand migrated route coverage until the remaining Python-only surface is
  small and intentional
- Shift default traffic to Rust once parity and rollback gates are met
- Keep Python fallback available during a stabilization window
- Remove migration-only scaffolding after the fallback window closes

### Non-goals

- Removing Python immediately after first successful cutover
- Large redesigns unrelated to migration debt

### Backlog-Ready Atomic Items

- `R7-01` Enumerate the remaining Python-only boundary set before default
  cutover
- `R7-02` Shift default traffic to Rust for the migrated route set
- `R7-03` Keep Python fallback enabled for a defined stabilization window
- `R7-04` Remove obsolete Python compare and proxy paths after stabilization
- `R7-05` Remove temporary compare-only code and superseded migration docs
- `R7-06` Decide final schema-management ownership
- `R7-07` Decide remaining packaging responsibilities

### Exit Criteria

- Rust is the default implementation for the migrated boundaries
- Python fallback is no longer authoritative but remains available during the
  agreed stabilization window
- No production traffic depends on temporary migration scaffolding after final
  cleanup

## First-Wave Non-Goals

- No long-lived Rust branch in `gobby`
- No monolithic rewrite in a new repo unrelated to `gobby-cli`
- No storage-first replacement of the Python daemon
- No simultaneous rewrite of all CLI commands
- No large workflow, agent, or pipeline redesign before parity
- No premature removal of Python safety nets

## Validation Strategy

Every migrated boundary must satisfy all of the following before cutover:

- Fixture parity against the Python baseline
- Error-path parity, including invalid input and daemon-unavailable cases
- Side-by-side execution on separate ports and processes
- Route-scoped rollback without a release rollback
- Observability strong enough to detect mismatches quickly
- Regression protection for `gcode` and `gsqz` after shared extraction

At minimum, the migration test corpus must cover:

- HTTP success responses for each migrated route family
- HTTP error semantics and status codes
- Degraded-daemon behavior for each migrated boundary
- Hook allow, block, error, and degraded behavior once hooks enter scope
- Database coexistence for any Rust-owned storage path
- Startup and shutdown behavior for side-by-side daemons

## Assumptions

- `0.4.0` is a launch hardening phase, not a new feature wave
- `gobby-cli` is the long-lived Rust home for migration work
- The old `rust-port` docs remain historical notes, not active plans
- Shared Rust extraction should be driven by current code in `gcode` and
  `gsqz`, not by stale estimates
