# PostgreSQL Migration for Hub Storage

## Summary

Replace SQLite as Gobby's runtime hub database with PostgreSQL.

Do not run PostgreSQL in the same OS container as Qdrant or Neo4j. The
packaging model stays:

- one unified `docker-compose.yml`
- one separate `postgres` service
- one separate `qdrant` service
- one separate `neo4j` service

Because this is an internal migration with no external users, do **not** build
a long-lived dual-write rollout. That complexity solves the wrong problem.

Use a cold migration instead:

1. add PostgreSQL service support and bootstrap-level database selection
2. add a temporary storage compatibility layer to port runtime SQL
3. build PostgreSQL schema and search parity
4. migrate existing SQLite data into PostgreSQL with the daemon stopped
5. cut runtime reads and writes over to PostgreSQL
6. remove SQLite as a runtime backend after validation

End state:

- PostgreSQL is the only runtime hub database
- fresh installs initialize PostgreSQL directly
- SQLite remains only as migration input and a short-lived backup artifact
- SQLite-specific runtime paths are removed

## Current Constraints in the Repo

This is not a connection-string swap.

- `src/gobby/storage/database.py` exposes `sqlite3.Connection`,
  `sqlite3.Row`, savepoints, and SQLite transaction semantics through
  `DatabaseProtocol`.
- `src/gobby/storage/baseline_schema.sql` and
  `src/gobby/storage/migrations.py` depend on SQLite-only features such as
  `datetime('now')`, `AUTOINCREMENT`, `PRAGMA`, `json_extract`, and FTS5
  virtual tables and triggers.
- storage modules under `src/gobby/storage/` use raw SQL directly, including
  SQLite conflict syntax such as `INSERT OR IGNORE` and `INSERT OR REPLACE`.
- some write paths depend on SQLite cursor behavior such as `lastrowid` for
  integer primary keys.
- bootstrap and startup still assume a filesystem SQLite database path:
  `database_path` in config/bootstrap, `load_config()`, and `runner_init()`.
- search currently assumes SQLite FTS5 for tasks, memories, skills, and code
  index data.
- comments and operational behavior still refer to SQLite as the hub source of
  truth.

Because of that, the work splits into three concrete tracks:

1. package PostgreSQL as a first-class local service
2. port runtime storage semantics away from SQLite-specific behavior
3. migrate existing hub data once, then remove SQLite from runtime

## Target Architecture

### Service packaging

Add a `postgres` service to `src/gobby/data/docker-compose.services.yml` with:

- image `postgres:17`
- named volume `gobby_postgres_data`
- Docker Compose profiles `postgres` and `all`
- `pg_isready` healthcheck
- environment-backed defaults for database name, user, password, and port

This mirrors the current local service model used for Qdrant and Neo4j:

- `gobby postgres install`
- `gobby postgres status`
- `gobby postgres uninstall`

Qdrant and Neo4j remain separate supporting stores in the same compose project.

### Runtime database model

PostgreSQL becomes the only runtime hub database.

SQLite is retained temporarily for:

- reading legacy hub data during migration
- rollback during the short validation window immediately after cutover

SQLite is **not** a permanent fallback backend after this work completes.

### Bootstrap and configuration

Database backend selection must happen before DB-backed config is available.

Add bootstrap-level settings:

- `hub_backend`: `sqlite` or `postgres`
- `database_url`: PostgreSQL DSN used when `hub_backend=postgres`
- `database_path`: legacy SQLite path used when `hub_backend=sqlite` and during
  one-shot import

Selection rules:

- bootstrap decides which hub database to open
- runtime startup must not infer the backend from DB-stored config
- after cutover, normal startup uses `hub_backend=postgres`
- after SQLite runtime removal, `database_path` remains only for import tooling

Do **not** add `database_shadow_url`. There is no shadow-write phase in this
plan.

### Storage compatibility layer

Keep raw SQL. Do not add SQLAlchemy or Alembic in this migration.

Introduce a temporary backend-neutral hub database layer with two purposes:

- port runtime call sites off direct `sqlite3` types
- isolate SQL and behavior that differ between SQLite and PostgreSQL

That layer is migration scaffolding, not a promise of permanent dual-backend
support.

Compatibility requirements:

- row parsers consume `Mapping[str, Any]` instead of `sqlite3.Row`
- storage managers stop depending on raw `sqlite3.Connection` and
  `sqlite3.Cursor` types
- generated-key behavior is explicit instead of relying on `lastrowid`
- conflict behavior is explicit instead of relying on `INSERT OR IGNORE` or
  `INSERT OR REPLACE`
- nested transactions and after-commit callbacks preserve current semantics
- SQL that differs by backend lives in one obvious place instead of being
  scattered through managers

### Search architecture

Search must stop assuming SQLite FTS5 as a runtime primitive.

PostgreSQL runtime search should use:

- `to_tsvector('simple', ...)` indexes for keyword search
- `tsquery` / `websearch_to_tsquery` query construction as appropriate
- `pg_trgm` for permissive fuzzy matching where FTS5 behavior was loose

Search should route through logical backends for:

- task search
- memory search
- skill search
- code index search

### Search fusion and Neo4j boost continuity

The PostgreSQL seam must preserve the current hybrid search behavior used by the
Rust `gcode` search stack rather than replacing it with a keyword-only backend.

- `pick_search_backend(...)` should continue to dispatch concrete keyword and
  vector backends such as `BM25SearchBackend` and `QdrantSearchBackend`
- an RRF orchestrator should merge those ranked lists so the existing
  Reciprocal Rank Fusion behavior survives the storage migration
- Neo4j graph relevance boosting should remain an explicit step in that flow,
  either as a separate `Neo4jSearchBackend` whose scores feed the fusion layer,
  or as a post-keyword/post-vector boost applied before final fusion

Rust migration checklist for the search path:

- identify the current `gcode` Rust modules that perform FTS5 symbol/content
  search
- identify the modules that perform Qdrant vector lookup
- identify the graph-boost / Neo4j scoring layer
- preserve their composition under the new PostgreSQL seam so FTS5/BM25,
  vector search, and Neo4j boosting still combine through RRF after cutover

## Implementation Plan

### Phase 1: PostgreSQL service and bootstrap support

- Add `postgres` to the unified compose template.
- Add installer, uninstaller, and health-check code parallel to the existing
  Qdrant and Neo4j flows.
- Extend bootstrap config and startup so the hub backend is selected before any
  DB-backed config is read.
- Update daemon service startup to include the `postgres` profile when the
  bootstrap/runtime configuration enables PostgreSQL.
- Add CLI commands for install, status, uninstall, migration, and activation.

This phase is mandatory before any storage work, because the current runtime
still assumes a SQLite path during bootstrap.

### Phase 2: Temporary backend-neutral storage seam

- Split the current `LocalDatabase` role into backend-specific implementations:
  one temporary SQLite import/legacy implementation and one PostgreSQL runtime
  implementation.
- Narrow `DatabaseProtocol` so storage modules depend on backend-neutral rows,
  execution helpers, and transaction scopes rather than `sqlite3`.
- Move row parsers to `Mapping[str, Any]`.
- Replace direct SQLite assumptions in managers:
  - `sqlite3.Row`
  - `sqlite3.Connection`
  - `sqlite3.Cursor`
  - `lastrowid`
  - `INSERT OR IGNORE`
  - `INSERT OR REPLACE`
  - SQLite-specific date math and JSON expressions
- Preserve nested transaction and after-commit behavior during the port.

Do not let this phase become a permanent two-backend architecture. The SQLite
implementation exists only to support migration and short-term rollback.

### Phase 3: PostgreSQL schema and query parity

- Add a PostgreSQL baseline schema and PostgreSQL migration registry alongside
  the current SQLite assets.
- Convert schema concepts to PostgreSQL-native types:
  - `TEXT` timestamps -> `TIMESTAMPTZ`
  - integer booleans -> `BOOLEAN`
  - `BLOB` -> `BYTEA`
  - structured JSON text -> `JSONB`
- Replace SQLite identity columns with PostgreSQL identity/sequence-backed
  columns and define how inserts retrieve generated keys.
- Replace SQLite conflict syntax with explicit PostgreSQL `ON CONFLICT` rules.
- Replace SQLite date arithmetic with PostgreSQL interval-based expressions.
- Replace `json_extract(...)` usage with PostgreSQL JSONB operators/functions.
- Replace FTS5 tables and triggers with PostgreSQL search indexes and query
  paths.

This phase should include a portability audit of runtime SQL, not just schema
translation.

### Phase 4: One-shot SQLite -> PostgreSQL migration

- Add `gobby postgres migrate-from-sqlite`.
- Run the migration with the daemon stopped.
- Migration command behavior:
  - open the legacy SQLite hub database read-only
  - create the PostgreSQL schema in a target database
  - bulk copy all tables in dependency-safe order
  - preserve existing primary keys, including integer identities
  - reseed PostgreSQL sequences/identity values after import
  - rebuild PostgreSQL search structures/indexes as needed
  - validate row counts, foreign-key integrity, and targeted content hashes

Validation must be deterministic, not "looks close enough." At minimum:

- row counts for every migrated table
- foreign-key and uniqueness validation
- representative record checks for sessions, tasks, memories, config, code
  index, agents, metrics, and workflow data
- sequence/identity reseed verification for tables with generated integer IDs

Leave the SQLite file untouched after import so it can serve as rollback input
during the short validation window.

### Phase 5: Cold cutover to PostgreSQL runtime

- Stop the daemon.
- Run the final migration into PostgreSQL.
- switch bootstrap/runtime configuration to `hub_backend=postgres`
- restart the daemon against PostgreSQL only
- run smoke checks against the daemon and key storage surfaces

There is no shadow-write mode in this phase:

- reads come from PostgreSQL
- writes go to PostgreSQL
- startup should not open the SQLite hub database in normal operation

Rollback rule during the validation window:

- stop the daemon
- restore bootstrap/runtime configuration to `hub_backend=sqlite`
- restart against the untouched SQLite database

Rollback exists only as short-term migration safety, not as a permanent product
feature.

### Phase 6: Remove SQLite runtime support

- Make fresh installs initialize PostgreSQL directly.
- Remove SQLite from normal startup and runtime storage wiring.
- Remove or isolate SQLite-specific migrations, FTS5 runtime code, and schema
  assumptions that are no longer needed.
- Keep only the minimum SQLite importer code required for one-time legacy
  migrations, if any.
- Update docs, comments, and user-facing text that still refer to SQLite as the
  hub database.

This phase is part of the migration, not optional cleanup. The goal is to stop
carrying dead dual-backend complexity.

## CLI and Interface Changes

New CLI surface:

- `gobby postgres install`
- `gobby postgres status`
- `gobby postgres uninstall`
- `gobby postgres migrate-from-sqlite`
- `gobby postgres activate`

Bootstrap/config changes:

- add `hub_backend`
- add `database_url`
- keep `database_path` temporarily for SQLite import and rollback
- remove any need for `database_shadow_url`

Type and interface changes:

- row parsers move from `sqlite3.Row` assumptions to mapping rows
- the hub database contract stops exposing `sqlite3` types
- search implementations stop depending directly on SQLite FTS5 tables
- generated-key and conflict behavior become explicit in the storage layer

## Test Plan

### Unit tests

- bootstrap precedence and validation across `hub_backend`, `database_url`, and
  `database_path`
- PostgreSQL adapter execution, fetch, transaction, nested transaction, and
  after-commit behavior
- row parsing compatibility with mapping-style rows
- generated-key behavior for sequence/identity-backed tables
- PostgreSQL search parity for tasks, memories, skills, and code index queries
- SQL portability coverage for paths previously using SQLite-only syntax

### Service/installer tests

- compose template includes a `postgres` service, profile, volume, and
  healthcheck
- install, uninstall, status, and activate commands follow the same behavior
  pattern as the other local services

### Migration tests

- migrate a populated SQLite fixture database into PostgreSQL
- verify row counts for every table
- verify foreign-key integrity after import
- verify sequence/identity reseeding after import
- verify representative reads and writes for sessions, tasks, memories, config,
  code index, metrics, agents, and workflows

### Cutover tests

- end-to-end cold cutover from SQLite fixture to PostgreSQL primary
- daemon boots and runs with PostgreSQL only after activation
- startup does not require or open the SQLite runtime database after cutover
- rollback to SQLite works during the short validation window

### Cleanup tests

- fresh installs initialize PostgreSQL without creating `~/.gobby/gobby-hub.db`
- SQLite-only runtime code paths are no longer used after the migration is
  complete

## Acceptance Criteria

- PostgreSQL is installable as a first-class service in the existing Docker
  Compose stack.
- Bootstrap can select the hub database backend before DB-backed config is
  loaded.
- The daemon can boot and run against PostgreSQL without opening a SQLite
  runtime database.
- Existing SQLite hub data can be imported into PostgreSQL with validated
  counts and integrity checks.
- Search behavior remains available after replacing SQLite FTS5 assumptions with
  PostgreSQL search support.
- Fresh installs initialize PostgreSQL directly.
- SQLite remains available only as temporary migration input/backup during the
  migration window, then is removed from runtime use.

## Assumptions

- Scope is the full hub database, not a partial migration.
- PostgreSQL runs in the same compose project as Qdrant and Neo4j, but in a
  separate container.
- Raw SQL remains the storage implementation style for this migration.
- There are no external users, so a cold migration is preferable to dual-write
  rollout complexity.
- The compatibility layer introduced during the migration is temporary and will
  be removed once PostgreSQL is the only runtime backend.
- Qdrant and Neo4j remain supporting stores; they are not replaced by
  PostgreSQL as part of this work.
