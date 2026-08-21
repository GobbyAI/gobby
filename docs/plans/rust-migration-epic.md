# Rust Migration Epic

> **Status: superseded 2026-08-05.** The `gobbyd`/port `60890` sidecar design in
> this document is stale. `.gobby/plans/gcore-schema-authority.md` is the
> canonical implementation plan for `gcore` schema authority and `gdaemon`.
> Do not implement legacy behavior from this file.

## Summary

This is the active Rust migration plan for Gobby. It replaces the earlier
`rust-port` drafts with an execution-focused epic that is structured around
phases, gates, and backlog-ready atomic work items.

This is not a monolithic rewrite. It is an incremental strangler migration for
a live Python daemon:

- Rust migration code lives in `~/Projects/gobby/crates`
- Python in `~/Projects/gobby` remains the behavioral reference until explicit
  cutover
- Traffic shifts by route family or boundary, not by big-bang daemon swap
- Parity, observability, and rollback safety come before redesign
- The atomic work items below are planning inventory only; they are not created
  as Gobby tasks by this document

## Current State

Refreshed 2026-07-10 after the monorepo merge and 0.5.0 branch work.

- `~/Projects/gobby` is the product repo and current source of truth
- `~/Projects/gobby/crates` is the long-lived Rust workspace and contains
  `gcode`, `ghook`, `gwiki`, and `gobby-core`
- The `gobby-cli` repo merge and post-merge hygiene are complete (epics
  #17469 and #17470 are closed); the old repo is retired
- Phase 2 foundation extraction is essentially complete: `crates/gcore/src/`
  already provides `bootstrap.rs`, `daemon_url.rs`, `postgres.rs` (TLS/sslmode
  handling plus `read_config_value`), `falkor.rs`, `qdrant/`, `secrets.rs`,
  `machine.rs`, `local_token.rs`, `config/`, and `ai/` (daemon HTTP client).
  Remaining Phase 2 work is a closing audit, not new extraction
- Daemon auth is landing on the 0.5.0 branch
  (`.gobby/plans/daemon-auth-0-5-0.md`): `/api/config/*` and `/api/tasks`
  require `Authorization: Bearer` with the install-scoped token from
  `~/.gobby/local_cli_token`, while `/api/health` stays public. Auth
  semantics are part of every frozen contract
- Python owns the live daemon, schema policy, fixtures, and rollout control
- Rust does not yet have a `gobby-daemon` crate

## Non-Negotiable Decisions

### Repo Ownership

- `~/Projects/gobby` owns current behavior, compatibility fixtures, rollout
  gates, the authoritative Python implementation, Rust crates, helper
  binaries, shared extractions, and replacement implementations

### Migration Shape

- Migration is boundary-first, not storage-first
- The public daemon stays Python-fronted during migration
- Rust runs beside Python on alternate internal ports and is cut in behind
  explicit routing or feature flags
- We do not start with a full storage port or a workflow/rule rewrite

### Branching

- No long-lived Rust migration branch in `gobby`
- Migration work lands continuously in the `gobby` mainline
- Python/Rust behavior is stabilized with fixtures and parity checks, not with a
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

Rust implementation work beyond low-risk extraction and prototyping starts
only after `0.5.0` ships; the pre-ship window is reserved for bug-fix
hardening. Phase 0 contract-freezing may proceed earlier because it produces
only contract docs and test artifacts. Phase 2 extraction is already
essentially complete.

The readiness gate is met when all of the following are true:

- `0.5.0` has shipped and remaining Python work is hardening, not major
  feature churn
- The daemon-auth enforcement flip has landed, so 401 bodies and header
  semantics are final before fixtures are captured on authenticated routes
- The first migration surfaces are frozen as exact contracts:
  - `GET /api/health`
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

### Python Responsibilities

- Define the behavioral contract for each migrated boundary
- Own the canonical schema and migration policy until final cutover
- Host parity fixtures and acceptance criteria
- Remain the public listener on `:60887` and `:60888` during strangler phases
- Provide feature flags or explicit delegation and proxy points for migrated
  boundaries

### Rust Workspace Responsibilities

- Add shared crates and replacement binaries in `crates/`
- Preserve `gcode`, `ghook`, and `gwiki` behavior while foundations are
  extracted
- Implement Rust replacements against Python-owned fixtures
- Run side-by-side with Python on alternate ports and processes

## Rollout Model

### Public Front Door

- Python remains the public daemon on `:60887` and `:60888`
- Rust runs on alternate internal ports during migration
- `:60890` is the default first HTTP port for a Rust sidecar daemon
  (`:60889` is already the dev web UI and `:60891` the managed Postgres hub)
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
- The Rust hook shell already exists: `ghook` owns transport, enqueue-first
  durability, fail-safe classification, and provider-native response shaping
  against the public daemon contract
- Hook cutover is allowed only after HTTP-side parity and degraded behavior are
  proven

## Phase Overview

- Phase 0 freezes contracts and builds the fixture corpus
- Phase 1 adds Python-side compare and delegation plumbing
- Phase 2 extracts shared Rust foundations in `crates/` (essentially complete)
- Phase 3 migrates low-risk read-only boundaries
- Phase 4 migrates the first DB-backed task read boundary
- Phase 5 migrates reduced session reads
- Phase 6 handles late complex boundaries
- Phase 7 covers cutover, fallback, and retirement of migration scaffolding

## First Wave Execution Order (post-0.5.0)

The first wave is one epic: the sidecar framework proven end-to-end, ending at
the first DB-backed boundary. It sequences the Phase 0–4 atomic items into
dependency-ordered milestones. Each milestone is one boundary, one rollback
story, one validation target.

- `M1` Epic doc corrections (this refresh)
- `M2` Committed config artifacts plus a CI freshness gate:
  `schemas/daemon-config.schema.json` and `schemas/daemon-config.defaults.json`
  generated from `DaemonConfig`, with a drift-fails-CI test (R0-18 groundwork)
- `M3` `rust_migration` config section on `DaemonConfig`: per-route
  `off | compare | delegate` flags, config-store-backed so rollback is a
  hot-reloaded config save (R1-01)
- `M4` Contract docs, fixture corpus, and Python replay harness for the three
  low-risk routes (R0-01..R0-03, R0-05..R0-07, R0-09..R0-11, R0-13..R0-15,
  R0-17). Fixtures live in `tests/contracts/http/` following the
  `tests/contracts/gwiki.contract.json` precedent; one corpus is consumed by
  both pytest and Rust
- `M5` Compare/delegation plumbing in Python: a route-scoped `APIRoute`
  subclass, fire-and-forget compare, delegate-with-fallback, an in-memory
  mismatch latch, and telemetry counters (R1-02..R1-05)
- `M6` `crates/gdaemon` scaffold — package `gobby-daemon`, binary `gobbyd`,
  axum on tokio, bind `127.0.0.1:60890`, bearer-auth extractor, FastAPI-shaped
  error envelope — plus the health route (R3-01..R3-03)
- `M7` Rust config routes: `/api/config/schema` served from the committed M2
  artifact; `/api/config/values` layered from the defaults artifact, bootstrap,
  and config-store overlay with secret masking, reading Postgres through
  `gobby-core` (R3-04, R3-05)
- `M8` Dual-daemon e2e smoke: zero-mismatch compare soak, sidecar-kill
  fallback proof, delegate flip and rollback (R1-06, R3-06..R3-14)
- `M9` Reduced `GET /api/tasks` v1: brief-shaped task rows plus build-state
  and owner-session enrichment; stage filters and hierarchy sort are excluded
  from v1; the compare wrapper gains a contract-guard predicate so
  out-of-contract requests skip Rust entirely (R4-01..R4-06)

Sidecar supervision is deferred to the delegation-by-default phase. Compare
mode must treat sidecar absence as a first-class state anyway, so development
runs `gobbyd` manually or via `cargo install --root ~/.gobby`, matching the
`gcode` install pattern.

## Phase 0: Contract Freeze and Fixture Corpus

**Goal:** Turn the first migration surfaces into exact replayable contracts
owned by Python.

### Scope

- Freeze exact contracts for the first four migration surfaces
- Contracts include auth semantics: the bearer requirement, the exact 401
  body, and the `X-Gobby-Local-Token` header alias; `/api/health` stays
  public
- Contracts state the global exception-handler quirk explicitly: uncaught
  errors return HTTP 200 with
  `{"status":"error","message":"Internal error occurred but request acknowledged","error_logged":true}`
  (`src/gobby/servers/exception_handlers.py`). Rust mirrors it, or the
  contract scopes it out per route
- Record success, error, and degraded-daemon fixtures
- Build a Python-baseline replay harness

### Non-goals

- Serving production traffic from Rust
- Porting storage internals
- Defining hook execution parity in full

### Backlog-Ready Atomic Items

- `R0-01` Define the exact contract for `GET /api/health`
- `R0-02` Define the exact contract for `GET /api/config/schema`
- `R0-03` Define the exact contract for `GET /api/config/values`
- `R0-04` Define the reduced v1 contract for `GET /api/tasks`
- `R0-05` Capture success fixtures for `GET /api/health`
- `R0-06` Capture success fixtures for `GET /api/config/schema`
- `R0-07` Capture success fixtures for `GET /api/config/values`
- `R0-08` Capture success fixtures for `GET /api/tasks`
- `R0-09` Capture error fixtures for `GET /api/health`
- `R0-10` Capture error fixtures for `GET /api/config/schema`
- `R0-11` Capture error fixtures for `GET /api/config/values`
- `R0-12` Capture error fixtures for `GET /api/tasks`
- `R0-13` Capture degraded-daemon fixtures for `GET /api/health`
- `R0-14` Capture degraded-daemon fixtures for `GET /api/config/schema`
- `R0-15` Capture degraded-daemon fixtures for `GET /api/config/values`
- `R0-16` Capture degraded-daemon fixtures for `GET /api/tasks`
- `R0-17` Build a replay harness that runs the fixture corpus against Python
- `R0-18` Define fixture storage format, directory layout, and schema version field

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

## Phase 2: `crates/` Foundation Extraction

**Status (2026-07-10):** essentially complete. R2-01 through R2-09 landed
organically in `gobby-core` (see Current State). The remaining work is
R2-10-style verification that `gcode`, `ghook`, and `gwiki` stay green on the
extracted helpers — a closing audit, not new extraction.

**Goal:** Turn the existing Rust utilities into a reusable foundation for the
daemon migration without destabilizing them.

### Scope

- Continue extracting shared foundations into `gobby-core`
- Extract only the shared seams required by current crates or near-term daemon
  work
- Keep `gcode`, `ghook`, and `gwiki` healthy while extractions land

### Non-goals

- Building the daemon itself
- Porting schema ownership to Rust
- Extracting speculative abstractions that no boundary needs yet

### Backlog-Ready Atomic Items

- `R2-01` Confirm the `gobby-core` crate scaffold and ownership metadata
- `R2-02` Extract bootstrap resolution into `gobby-core`
- `R2-03` Extract daemon URL resolution into `gobby-core`
- `R2-04` Extract project root helpers into `gobby-core`
- `R2-05` Extract project metadata helpers into `gobby-core`
- `R2-06` Extract daemon HTTP client utilities into `gobby-core`
- `R2-07` Extract PostgreSQL connection helpers into `gobby-core`
- `R2-08` Migrate `gcode` to the extracted `gobby-core` helpers
- `R2-09` Migrate `ghook` and `gwiki` to extracted `gobby-core` helpers where
  they duplicate shared behavior
- `R2-10` Verify `gcode`, `ghook`, and `gwiki` test coverage stays green after
  extraction

### Exit Criteria

- `gcode`, `ghook`, and `gwiki` use `gobby-core` for shared concerns where
  appropriate
- Existing Rust utility behavior is preserved
- The extracted APIs are sufficient to bootstrap `gobby-daemon`

## Phase 3: Rust Daemon Shell and Low-Risk Reads

**Goal:** Introduce a Rust daemon process beside Python and cut in the safest
read-only boundaries first.

### Scope

- Add `gobby-daemon` to `crates/`
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
- `R3-03` Implement `GET /api/health` in Rust
- `R3-04` Implement `GET /api/config/schema` in Rust
- `R3-05` Implement `GET /api/config/values` in Rust
- `R3-06` Enable compare mode for `GET /api/health`
- `R3-07` Enable compare mode for `GET /api/config/schema`
- `R3-08` Enable compare mode for `GET /api/config/values`
- `R3-09` Enable opt-in delegation for `GET /api/health`
- `R3-10` Enable opt-in delegation for `GET /api/config/schema`
- `R3-11` Enable opt-in delegation for `GET /api/config/values`
- `R3-12` Prove route-scoped rollback for `GET /api/health`
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

## Boundary Inventory Beyond the First Wave

Verified against the daemon architecture on 2026-07-10.

### Second wave: external-MCP transport multiplexer

The external-MCP transport half of the proxy (`MCPClientManager`,
`mcp_proxy/transports/`, `mcp_proxy/client_manager/`, `mcp_proxy/lazy.py`) is
the cleanest high-value seam after the first wave. It is a pure protocol
boundary — connection pooling, schema caching, health monitoring, circuit
breaking — whose only daemon dependencies are Postgres-backed config and
metrics, both already covered by `gobby-core`.

It migrates as a delegated backend behind Python's front door: Python keeps
`ToolProxyService` enforcement (the rule engine and session resolution are
Python-entangled) and delegates external-server fan-out to the sidecar. The
routing seam already exists as the `is_internal()` branch in
`src/gobby/mcp_proxy/services/tool_execution.py`.

### Ordering rule: multiplexer before internal MCP servers

Internal `gobby-*` MCP servers are in-process Python closures over daemon
managers; they never touch the transport machinery. A ported internal server
needs a Rust proxy to register into, and porting one earlier would require
throwaway per-server transport plumbing. Internal MCP servers migrate only as
their underlying managers move — the tool surface re-fronts logic that already
migrated. The MCP front-door and enforcement flip happens last, gated on rules
and sessions.

### Candidate data-plane boundary: memory and skill search

A memory/skill search port would reuse existing `gobby-core` primitives
(`search.rs` `rrf_merge`, `qdrant/`, `falkor.rs`); `gcode` already ported the
code-index searcher to `crates/gcode/src/search/rrf.rs`. Listed as a candidate
boundary, not scheduled work.

### Second migration pattern: versioned CLI contract plus Python gateway

Alongside the sidecar pattern, the gwiki panel work proved a second pattern: a
Rust CLI emitting versioned JSON envelopes
(`crates/gwiki/contract/gwiki.contract.json`) consumed through a thin Python
gateway (`src/gobby/gwiki_gateway.py`). The CLI-contract pattern fits
compute-shaped subsystems; the sidecar pattern fits route families.

### Phase 6 deferrals, confirmed with evidence

- The hook path's latency-sensitive shell is already Rust: `ghook` owns
  enqueue-first durability, fail-safe classification, and provider-native
  response shaping. The remainder (`HookManager` fan-out plus the rule
  engine) is dominated by Postgres, session-state, and in-process MCP-proxy
  coupling and stays Python until those subsystems move
- The WebSocket chat server is the least portable component (Python LLM SDKs,
  per-provider streaming backends) and migrates last, if at all

## First-Wave Non-Goals

- No long-lived Rust branch in `gobby`
- No monolithic rewrite outside this repo's `crates/` workspace
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
- Regression protection for `gcode`, `ghook`, and `gwiki` after shared
  extraction

At minimum, the migration test corpus must cover:

- HTTP success responses for each migrated route family
- HTTP error semantics and status codes
- Degraded-daemon behavior for each migrated boundary
- Hook allow, block, error, and degraded behavior once hooks enter scope
- Database coexistence for any Rust-owned storage path
- Startup and shutdown behavior for side-by-side daemons

## Assumptions

- `0.5.0` pre-ship work is bug-fix hardening, not a new feature wave; Rust
  implementation starts after it ships
- `gobby/crates` is the long-lived Rust home for migration work
- `gobby-cli` is retired; the merge and post-merge hygiene epics (#17469,
  #17470) are closed
- The old `rust-port` docs remain historical notes, not active plans
- Shared Rust extraction should be driven by current code in `gcode` and
  the helper crates, not by stale estimates
