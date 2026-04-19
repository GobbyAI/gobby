# PostgreSQL Migration for Hub Storage

## Summary

Migrate Gobby's primary hub database from SQLite to PostgreSQL, but do **not**
run PostgreSQL in the same OS container as Qdrant or Neo4j.

The correct packaging is:

- keep one unified `docker-compose.yml`
- add PostgreSQL as a separate `postgres` service in that compose stack
- continue running Qdrant and Neo4j as separate services

This keeps the user-facing install model simple without turning the storage
stack into an operational mess. A single multi-process container for Postgres,
Qdrant, and Neo4j would be harder to test, upgrade, observe, and recover.

The migration itself should be a staged dual-write rollout:

1. add a database abstraction that supports SQLite and PostgreSQL
2. keep SQLite as primary while shadow-writing to PostgreSQL
3. backfill existing data and validate parity
4. cut reads and writes over to PostgreSQL
5. remove SQLite-only hub storage paths after one stable release cycle

## Current Constraints in the Repo

This is not a connection-string swap.

- `src/gobby/storage/database.py` is a SQLite-specific adapter with
  `sqlite3.Connection`, `sqlite3.Row`, savepoints, and SQLite transaction
  semantics baked into the interface.
- `src/gobby/storage/baseline_schema.sql` uses SQLite-specific features such as
  `datetime('now')`, `AUTOINCREMENT`, partial indexes, and FTS5 virtual tables.
- `src/gobby/storage/migrations.py` creates and maintains multiple FTS5-backed
  indexes with triggers for tasks, memories, skills, and code index search.
- Storage modules under `src/gobby/storage/` use raw SQL directly, so backend
  compatibility has to be handled in the adapter and schema layers.
- Memory reconciliation code currently treats the hub database as the source of
  truth and explicitly names SQLite in comments and user-facing behavior.

Because of that, the migration has two separate tracks:

1. operational packaging of PostgreSQL in the Docker-managed services stack
2. architectural replacement of SQLite-specific hub storage semantics

## Target Architecture

### Service packaging

Add a `postgres` service to `src/gobby/data/docker-compose.services.yml` with:

- image `postgres:17`
- named volume `gobby_postgres_data`
- Docker Compose profiles `postgres` and `all`
- `pg_isready` healthcheck
- environment-backed defaults for database name, user, password, and port

This should mirror the existing Qdrant and Neo4j installer model:

- `gobby postgres install`
- `gobby postgres status`
- `gobby postgres uninstall`

Qdrant and Neo4j stay exactly as they are: supporting stores managed in the
same compose project, not merged into PostgreSQL and not merged into one
container.

### Storage abstraction

Keep the current raw-SQL approach. Do not add SQLAlchemy or Alembic in this
migration.

Instead, introduce a dialect-neutral hub database layer:

- `SQLiteDatabase`
- `PostgresDatabase`
- a widened `DatabaseProtocol` that exposes the current execution,
  fetch, transaction, and after-commit behavior without leaking
  `sqlite3.Connection` or `sqlite3.Row`

Compatibility rules:

- call sites should consume mapping-like rows rather than `sqlite3.Row`
- storage code should continue using raw SQL, but SQL must be split by dialect
  where syntax diverges
- current behavior around nested transactions and after-commit callbacks must be
  preserved across both backends

### Configuration

Add new config keys:

- `database_url`: primary PostgreSQL DSN when PostgreSQL is the active hub DB
- `database_shadow_url`: optional PostgreSQL DSN used only for shadow dual-write

Keep `database_path` during migration as the SQLite fallback and rollback path.
Selection rules:

- if `database_url` is set, PostgreSQL is primary
- otherwise `database_path` remains primary
- if `database_shadow_url` is set, shadow dual-write is enabled

## Implementation Plan

### Phase 1: PostgreSQL service support

- Add `postgres` to the unified compose template.
- Add installer, uninstaller, and health-check code parallel to the existing
  Qdrant and Neo4j paths.
- Add CLI commands for install, status, uninstall, migration, and cutover.
- Persist PostgreSQL connection settings through the same config/bootstrap flow
  used by the rest of the daemon.

### Phase 2: backend-neutral hub database API

- Split the current `LocalDatabase` role into backend-specific implementations.
- Widen `DatabaseProtocol` so storage modules depend on generic rows and
  transactions rather than `sqlite3`.
- Update row parsers in storage modules to accept `Mapping[str, Any]`.
- Preserve the existing savepoint and after-commit semantics so workflow,
  session, and task logic does not regress.

This phase is the prerequisite for dual-write. Without it, shadow PostgreSQL
writes will become a pile of special cases.

### Phase 3: PostgreSQL schema and search parity

- Add a PostgreSQL baseline schema and PostgreSQL migration registry alongside
  the existing SQLite schema assets.
- Convert SQLite schema concepts to PostgreSQL-native equivalents:
  - `TEXT` timestamps -> `TIMESTAMPTZ`
  - `INTEGER` booleans -> `BOOLEAN`
  - `BLOB` -> `BYTEA`
  - JSON text blobs that are queried structurally -> `JSONB`
- Replace SQLite FTS5 dependencies with PostgreSQL full-text search and fuzzy
  matching support:
  - `to_tsvector('simple', ...)` expression indexes for keyword search
  - `pg_trgm` for fuzzy fallback where current search is permissive

The search layer should become logical rather than SQLite-specific:

- tasks search
- memories search
- skills search
- code index search

Each search feature should route through a backend-specific implementation
instead of directly assuming FTS5 virtual tables and triggers.

### Phase 4: data migration and dual-write

- Add `gobby postgres migrate-from-sqlite`.
- Migration command behavior:
  - open SQLite primary
  - create PostgreSQL schema
  - bulk copy all tables in dependency-safe order
  - validate row counts and representative checksums
- Enable dual-write mode:
  - reads stay on SQLite
  - writes go to SQLite and PostgreSQL
  - PostgreSQL failures must not corrupt or block primary SQLite writes
  - parity mismatches must emit logs and metrics

Update comments, endpoint descriptions, and operational docs that currently say
"SQLite is the source of truth" so they instead refer to the hub database as
the source of truth.

### Phase 5: cutover and rollback

- Stop the daemon.
- Run one final incremental SQLite -> PostgreSQL sync.
- Set `database_url` to the PostgreSQL DSN and restart the daemon.
- Keep the SQLite file untouched as rollback state.

Rollback rule:

- if cutover validation fails, remove `database_url`, disable dual-write, and
  restart back on SQLite

Do not delete the SQLite database during the first PostgreSQL-backed release
cycle.

## CLI and Interface Changes

New CLI surface:

- `gobby postgres install`
- `gobby postgres status`
- `gobby postgres uninstall`
- `gobby postgres migrate-from-sqlite`
- `gobby postgres cutover`

New config surface:

- `database_url`
- `database_shadow_url`

Type and interface changes:

- row parsers move from `sqlite3.Row` assumptions to mapping rows
- the hub database contract becomes backend-neutral
- search implementations stop depending directly on SQLite FTS5 tables

## Test Plan

### Unit tests

- config precedence across `database_path`, `database_url`, and
  `database_shadow_url`
- PostgreSQL adapter execution, fetch, transaction, nested transaction, and
  after-commit behavior
- row parsing compatibility with mapping-style rows
- PostgreSQL search parity for tasks, memories, skills, and code index queries

### Installer tests

- compose template includes a `postgres` service, profile, volume, and
  healthcheck
- install, uninstall, and status commands follow the same behavior pattern as
  Qdrant and Neo4j

### Migration tests

- migrate a populated SQLite fixture database into PostgreSQL
- verify row counts for every table
- verify foreign-key integrity after import
- verify representative reads and writes for sessions, tasks, memories, config,
  code index, and metrics

### Dual-write and cutover tests

- successful shadow writes
- shadow-write failure does not break primary writes
- mismatch detection emits observable diagnostics
- end-to-end cutover from SQLite fixture -> PostgreSQL primary -> rollback path

## Acceptance Criteria

- PostgreSQL is installable as a first-class service in the existing Docker
  Compose stack.
- The daemon can run against either SQLite or PostgreSQL through the same
  storage contract.
- The full hub database can be backfilled from SQLite into PostgreSQL.
- Dual-write parity can be observed before cutover.
- Search behavior remains available after replacing SQLite FTS5 assumptions with
  PostgreSQL search support.
- Rollback to SQLite is possible without data loss during the initial
  PostgreSQL rollout window.

## Assumptions

- Scope is the full hub database, not a partial migration.
- PostgreSQL runs in the same compose project as Qdrant and Neo4j, but in a
  separate container.
- Raw SQL remains the storage implementation style for this migration.
- Qdrant and Neo4j remain supporting stores; they are not replaced by
  PostgreSQL as part of this work.
