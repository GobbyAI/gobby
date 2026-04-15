# Rust Migration Epic

## Summary

This is the canonical Rust migration plan for Gobby. It supersedes the earlier
`rust-port` drafts and defines the actual migration model we will execute:

- Rust migration code lives in the existing `~/Projects/gobby-cli` workspace
- Migration follows an HTTP/API strangler-fig pattern
- Python in `~/Projects/gobby` remains the behavioral reference
  implementation until explicit cutover
- No long-lived Rust migration branch will be used
- Parity and rollback safety come before redesign

This is not a monolithic rewrite plan. It is an incremental replacement plan
for a live Python system heading into the `0.4.0` launch.

## Current State

- `~/Projects/gobby` is the product repo and current source of truth
- `src/gobby` currently contains about 277K LOC across 1,553 files
- `src/gobby/storage/migrations.py` currently sets `BASELINE_VERSION = 211`
- `src/gobby/storage/baseline_schema.sql` currently contains 67 `CREATE TABLE`
  statements and 176 index statements
- FTS setup is partly managed by callable migration helpers, not only by the
  baseline schema
- `~/Projects/gobby-cli` is an existing Rust workspace with three crates:
  `gcode`, `gsqz`, and `gloc`
- `gobby-cli` does not yet have a shared core crate or a daemon crate

## Non-Negotiable Decisions

### Repo Ownership

- `~/Projects/gobby` owns current behavior, compatibility fixtures, rollout
  gates, and the authoritative Python implementation
- `~/Projects/gobby-cli` owns Rust crates, binaries, shared Rust extraction,
  and replacement implementations

### Migration Shape

- Migration is boundary-first, not storage-first
- The public daemon stays Python-fronted during migration
- Rust replacements run beside Python and are cut in behind explicit routing
  or feature flags
- We do not start with a full storage port or a full workflow/rule rewrite

### Branching

- No long-lived migration branch in `gobby`
- Migration work lands continuously in the mainline of each repo
- Cross-repo behavior is stabilized with fixtures and parity checks, not with a
  giant integration branch

### Authority and Cutover

- Python is authoritative until a boundary passes parity, observability, and
  rollback gates
- A Rust component is not considered "migrated" merely because it exists; it
  must prove equivalent behavior against the Python contract

## Migration Principles

1. Preserve product behavior first.
2. Migrate externally visible boundaries before deep internals.
3. Keep every milestone reversible.
4. Move one responsibility boundary at a time.
5. Do not mix migration work with speculative architecture cleanup.
6. Keep existing Rust utilities healthy while extracting shared foundations.

## Readiness Gate

Rust implementation work beyond low-risk extraction and prototyping starts only
after the `0.4.0` hardening pass establishes a stable contract for the first
migration boundaries.

The gate is met when all of the following are true:

- `0.4.0` launch blockers are resolved and remaining Python work is bug-fix
  hardening, not major feature churn
- The first migration surfaces are frozen as contracts:
  `GET /api/health`, `GET /api/status`, selected task/session read endpoints,
  selected config endpoints, and the hook execution request/response contract
- Golden fixtures exist for success, error, and degraded-daemon behavior on
  those surfaces
- The Python daemon exposes enough logging and comparison hooks to detect Rust
  mismatches before any cutover
- Rollback to the pure Python path is verified for the first migration stage

## Repository Contracts

### Python Repo Responsibilities

- Define the behavioral contract for each migrated boundary
- Own the canonical schema and migration policy until final cutover
- Host parity fixtures and acceptance criteria
- Remain the public listener on `:60887` and `:60888` during the strangler
  phases
- Provide feature flags or explicit delegation/proxy points for migrated
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
  `:60889` for HTTP is the default first choice
- The first unit of traffic shifting is a route family or hook boundary, not an
  all-or-nothing daemon swap

### Strangler Routing

- Unmigrated routes stay in Python
- Migrated routes can be delegated from Python to Rust behind explicit feature
  flags
- Shadow/compare mode should call Rust, compare results, and still return the
  Python response until parity is proven
- Any parity mismatch disables the route flag and falls back to Python without
  a release rollback

### Hook Integration

- Hook migration stays behind the existing hook API contract
- If a Rust `gobby-hook` binary is introduced, it still targets the public
  daemon contract first
- Hook cutover is allowed only after HTTP-side parity and degraded-daemon
  behavior are proven

## Sub-Epic 1: Contracts and Parity Harness

**Goal:** Freeze the first migration boundaries and make Python behavior
replayable.

**Scope**

- Define boundary contracts for health/status, first task/session read routes,
  first config routes, and hook execution
- Capture golden request/response fixtures
- Capture error behavior, timeout behavior, and degraded-daemon behavior
- Add a compare harness that can replay fixtures against Python and Rust

**Non-goals**

- Serving production traffic from Rust
- Porting storage internals

**Exit criteria**

- Fixtures are committed and stable
- Compare harness passes against Python as the baseline
- The first migration surfaces are explicitly named and frozen

## Sub-Epic 2: `gobby-cli` Foundation Extraction

**Goal:** Turn the existing Rust utilities into a reusable migration
foundation.

**Scope**

- Add `gobby-core` to `gobby-cli`
- Extract shared bootstrap, daemon-client, project, DB helper, and secret
  resolution code from `gcode` and `gsqz`
- Keep `gloc` unchanged unless a shared extraction clearly benefits it
- Define stable APIs in `gobby-core` for later daemon and hook crates

**Non-goals**

- Building the daemon itself
- Changing product behavior in `gcode` or `gsqz`

**Exit criteria**

- `gcode` and `gsqz` depend on `gobby-core` for shared concerns
- Existing Rust utility tests stay green
- The extracted APIs are sufficient to bootstrap new Rust services and binaries

## Sub-Epic 3: Rust Daemon Shell and Front-Door Strangler

**Goal:** Introduce a Rust daemon process that can be exercised beside Python
without taking public traffic directly.

**Scope**

- Add `gobby-daemon` to `gobby-cli`
- Run it on an alternate internal port
- Implement `GET /api/health`, `GET /api/status`, and the first read-only HTTP
  surfaces
- Add Python-side delegation or proxy points for migrated HTTP boundaries
- Add compare mode that logs mismatches while Python remains authoritative

**Non-goals**

- Porting all endpoints
- Swapping the public port to Rust

**Exit criteria**

- Rust serves the initial contract surfaces on its own port
- Python can delegate or compare selected routes against Rust
- Mismatches are observable and route-scoped rollback is immediate

## Sub-Epic 4: Early Boundary Migrations

**Goal:** Cut over the safest externally visible boundaries first.

**Scope**

- Health/status endpoints
- Read-only task and session query surfaces
- Low-risk config reads, then tightly scoped writes after parity proves out
- Optional hook-dispatcher binary work only after the daemon shell is stable

**Non-goals**

- Workflow engine replacement
- Agent/pipeline/process lifecycle replacement
- Storage-first migration of unrelated tables

**Exit criteria**

- Selected route families can run from Rust in developer-only or opt-in mode
- Rollback to Python is immediate and proven
- Parity holds under normal, error, and degraded-daemon scenarios

## Sub-Epic 5: Internal Subsystem Migrations

**Goal:** Move deeper internals only after the corresponding service boundary
is stable under Rust.

**Scope**

- Rust storage logic for already-migrated boundaries
- Config-store internals, then task/session storage paths behind migrated APIs
- Rule-engine and hook-evaluation parity only after boundary fixtures exist
- MCP transport or other hot-path internals only when they support a migrated
  external boundary

**Rules**

- Rust may read/write live SQLite tables only for boundaries already under Rust
  control or in compare-only mode with explicit safeguards
- Schema ownership remains in the Python repo until near-final cutover
- No subsystem is migrated purely because it is "easy" if it does not advance
  a real strangler boundary

**Non-goals**

- A wholesale storage port before API parity
- A speculative rewrite of workflows, agents, or memory systems

**Exit criteria**

- Rust owns the full internal stack for each cut-over boundary
- Database coexistence and parity checks are in place for every Rust-owned
  persistence path

## Sub-Epic 6: Cutover and Python Retirement

**Goal:** Promote Rust from delegated implementation to primary runtime.

**Scope**

- Expand route coverage until the remaining Python-only surface is small and
  intentional
- Shift default traffic to Rust once parity, observability, and rollback gates
  are satisfied
- Keep Python fallback available for at least one stabilization window after
  default cutover

**Non-goals**

- Removing Python immediately after the first successful cutover

**Exit criteria**

- Rust is the default implementation for the migrated boundaries
- Python fallback is still available but no longer authoritative
- Remaining Python-only responsibilities are explicitly tracked

## Sub-Epic 7: Post-Cutover Simplification

**Goal:** Remove migration scaffolding and legacy duplication.

**Scope**

- Remove obsolete Python proxy/delegation paths
- Remove superseded docs and temporary compare-only code
- Collapse duplicate behavior that only existed for side-by-side operation
- Decide final ownership of schema management and remaining packaging concerns

**Non-goals**

- Large redesigns unrelated to migration debt

**Exit criteria**

- No production traffic depends on migration scaffolding
- The architecture reflects the new steady state instead of the transition

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
- Side-by-side execution on separate ports/processes
- Route-scoped rollback without a release rollback
- Observability strong enough to detect mismatches quickly
- Regression protection for `gcode` and `gsqz` after shared extraction

At minimum, the migration test corpus must cover:

- HTTP success responses for the first route families
- HTTP error semantics and status codes
- Hook allow/block/error/degraded-daemon behavior
- Database coexistence for any Rust-owned storage path
- Startup/shutdown behavior for side-by-side daemons

## Assumptions

- `0.4.0` is a launch hardening phase, not a new feature wave
- `gobby-cli` is the long-lived Rust home for migration work
- The old `rust-port` docs are useful as historical notes, not active plans
- Shared Rust extraction should be driven by current code in `gcode` and
  `gsqz`, not by stale estimates from earlier drafts
