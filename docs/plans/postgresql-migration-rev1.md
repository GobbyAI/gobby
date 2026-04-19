# PostgreSQL Migration for Hub Storage

## Overview

Replace SQLite as Gobby's runtime hub database with PostgreSQL. This is an internal migration with no external users, so we use a cold cutover (stop daemon, migrate, restart) rather than a long-lived dual-write rollout — dual-write solves a problem we do not have.

Service packaging stays unified but services stay isolated:

- one `docker-compose.yml`
- separate `postgres`, `qdrant`, and `neo4j` services (each in its own container)

End state:

- PostgreSQL is the only runtime hub database
- Fresh installs initialize PostgreSQL directly
- SQLite remains only as migration input and a short-lived rollback artifact
- SQLite-specific runtime paths are removed

The Python codebase is expected to be ported to Rust in a later effort. This plan prefers choices that survive that port unchanged: plain SQL migrations (consumable by `sqlx migrate run`), `$1` parameter placeholders, a bootstrap-sourced runtime DSN plus env-var-driven pool tuning, and a compose-based test harness that does not depend on language-specific testcontainers wiring.

## Constraints

- **Driver**: psycopg v3 (sync + async on one driver; pairs with `psycopg_pool`). This is the key migration choice because existing synchronous storage call sites can keep running while new async paths are added incrementally on the same driver. Not psycopg2 (sync-only, no async path). Not asyncpg (async-only would force a broad refactor of every synchronous storage call site up front instead of letting the codebase adopt async gradually).
- **Migration tool**: raw SQL files + a handwritten migration registry. No SQLAlchemy, no Alembic.
- **Test backend**: compose-managed PostgreSQL reached via `DATABASE_URL`. Schema-per-xdist-worker plus test-scoped schema reset for isolation. Zero language-specific test infra so the pattern survives the Rust port.
- **Rust portability**: migrations must be pure `.sql` files (no Python callables). Parameter style standardized on `$1`. Runtime DSN lives in bootstrap for now; pool and connection tuning are still env-driven (`PGPOOL_MIN`, `PGPOOL_MAX`, `PGCONNECT_TIMEOUT`, `PGAPPNAME`) rather than embedded in Python.
- **Search**: `pg_search` (ParadeDB) shipped via a custom `gobby/postgres:17-pgsearch` image. BM25 ranking via Tantivy. Rust-portable indexes. Semantic search (Qdrant) is an explicit non-goal of this plan but the search backend dispatcher leaves a clean seam for it.
- **Gobby Pro compatibility**: Gobby Pro is assumed to be a sync / fleet-management hub, not a hosted-Gobby service. This plan does not need to run on managed Postgres (RDS, Cloud SQL, Aurora). Gobby always runs locally on the user's machine; Gobby Pro talks to Gobby instances via API, not by sharing a database. Gobby Pro's own datastore is out of scope for this plan.
- **Licensing**: pg_search is AGPL-3.0. Used unmodified as a Postgres extension (separate process), it does not propagate to Gobby's application code. The software is distributed to end users for local execution, which is the standard AGPL / GPL case: source of pg_search itself must be available (it is, on GitHub), and no copyleft reaches Gobby. The rest of the runtime stack is PostgreSQL-license, Apache-2.0 (Qdrant), and LGPL-3.0 (psycopg v3) — all permissive from Gobby's perspective.
- **Bootstrap security posture**: `bootstrap.yaml` remains the pre-DB source of truth and stores the runtime `database_url` in plaintext for now, consistent with today's local-first bootstrap model (which already stores `neo4j_password` there). This is tracked technical debt for the migration, not invisible risk. Minimum operator guidance for this phase: restrict `~/.gobby/bootstrap.yaml` to owner read/write only (`0600`). Mitigation timeline: move Postgres credentials behind OS keyring / secret-store integration in the first post-cutover `v0.4.x` hardening cycle, with a migration path from inline `database_url` to a keyring-backed reference.
- **Rollback window**: short. Stop daemon, restore `hub_backend=sqlite`, restart. Rollback is a migration safety net, not a permanent product feature. Writes made during the post-cutover validation window are at risk on rollback; they must be captured for forensics, but they are not auto-merged back into SQLite.

> Warning: `bootstrap.yaml` containing `database_url` is equivalent to storing the Postgres password in plaintext, just like today's `neo4j_password`. Treat that file as a secret-bearing credential file.

## Current Constraints in the Repo

This is not a connection-string swap. Key SQLite-specific coupling the migration must break:

- **Type surface**: `src/gobby/storage/database.py` exposes `sqlite3.Connection`, `sqlite3.Row`, and `sqlite3.Cursor` through `DatabaseProtocol` (lines 70–88). Consumers depend on `sqlite3.Row` semantics (`.keys()`, `.values()`).
- **Transaction semantics**: savepoint nesting uses `conn.in_transaction` (SQLite-only attribute, line 309). After-commit callbacks fire post-`COMMIT` but pre-snapshot-propagation — under PostgreSQL MVCC this will expose latent consistency bugs that SQLite's serialized writes hide.
- **Identity keys**: `cursor.lastrowid` used at `task_dependencies.py:73`, `task_affected_files.py` (2 sites), `workflow_audit.py:102`. No PostgreSQL equivalent — must use `RETURNING id`.
- **Upserts**: `INSERT OR IGNORE` (8 sites across `projects.py`, `session_tasks.py`, `migrations.py`, `sessions.py`, `pipelines.py`) and `INSERT OR REPLACE` (1 site at `agents.py:213`). Must be rewritten to `ON CONFLICT DO NOTHING / DO UPDATE SET`.
- **Schema primitives**: `AUTOINCREMENT` (17 sites), `datetime('now')` (60+ DEFAULT expressions), `json_extract(...)` / `json_set(...)` (17 sites), `PRAGMA foreign_keys=ON`, `PRAGMA query_only=ON` (test read-only enforcement).
- **Search**: FTS5 virtual tables with content-synced triggers on `tasks`, `memories`, `code_symbols`, `code_content`, `skills` (contentless). 12+ triggers keep virtual tables in sync. No abstraction — managers call FTS5 directly using `MATCH` and `bm25()`.
- **Migration runner**: `src/gobby/storage/migrations.py` uses `conn.executescript()` (no PostgreSQL equivalent), splits SQL on `;` (breaks for trigger bodies), embeds Python callable migrations (`_setup_code_symbols_fts`, `_setup_code_content_fts`, `_setup_tasks_fts`, `_setup_memories_fts`, `_migrate_claimed_by_session_id`, etc.), uses `PRAGMA table_info(...)` for idempotency checks.
- **Bootstrap**: `src/gobby/config/bootstrap.py` has only `database_path`. No backend selection before DB-backed config loads.
- **Test infrastructure**: `tests/conftest.py` uses `:memory:` SQLite exclusively. No Postgres fixtures. 11k+ tests, ~48 files under `tests/storage/`. Any Postgres-only bug cannot be caught until production today.
- **Timestamps**: all `created_at` / `updated_at` stored as ISO8601 text. Python adapters assume UTC and add tzinfo; the `datetime('now')` DEFAULT produces naive UTC text. Migration must preserve UTC and align on `TIMESTAMPTZ`.

## Target Architecture

### Service packaging

Add `postgres` to `src/gobby/data/docker-compose.services.yml`:

- image `gobby/postgres:17-pgsearch` (custom image — stock `postgres:17` plus the pg_search extension; built and published as part of this plan, see task 1.4)
- named volume `gobby_postgres_data`
- Compose profiles `postgres` and `all`
- `pg_isready` healthcheck
- env-backed defaults for `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`

Image rationale: using `paradedb/paradedb:latest` directly would bundle pg_analytics and pg_cron (both unused by Gobby), expand image size to ~2GB, and broaden the attack surface. The custom image adds only pg_search on top of `postgres:17`, keeping footprint close to stock Postgres (~600MB) and keeping Gobby-controlled versioning.

CLI surface mirrors existing Qdrant / Neo4j installers:

- `gobby postgres install`
- `gobby postgres status`
- `gobby postgres uninstall`
- `gobby postgres migrate-from-sqlite`
- `gobby postgres activate` / `deactivate`

### Runtime database model

PostgreSQL is the only runtime hub database after cutover. SQLite is retained temporarily for migration input and rollback. SQLite is not a permanent fallback after Phase 7.

### Bootstrap and configuration

Backend selection must happen before DB-backed config is available. Bootstrap-level fields:

- `hub_backend`: `"sqlite"` or `"postgres"`
- `database_url`: psycopg v3 DSN used when `hub_backend=postgres`
- `database_path`: legacy SQLite path used when `hub_backend=sqlite` and during one-shot import

Rules:

- bootstrap decides which backend to open
- runtime startup must not infer the backend from DB-stored config (chicken-and-egg)
- after cutover, normal startup uses `hub_backend=postgres`
- after Phase 7 cleanup, `database_path` exists only for import tooling
- for this phase, `database_url` is stored directly in `bootstrap.yaml`; no pre-DB secret indirection is introduced, so operators must keep `bootstrap.yaml` locked down to owner read/write (`0600`) until the planned OS keyring migration lands

There is no `database_shadow_url`. No shadow-write phase.

### Storage compatibility layer

Keep raw SQL. Introduce a temporary `HubDatabase` protocol with two implementations:

- `SqliteHubDatabase` — wraps current `LocalDatabase`; lives until Phase 7
- `PostgresHubDatabase` — psycopg v3 + `psycopg_pool.ConnectionPool`; becomes the only implementation after Phase 7

Protocol requirements:

- row parsers consume `Mapping[str, Any]`, not `sqlite3.Row`
- transaction scope is explicit via context manager; no `conn.in_transaction` introspection
- generated-key retrieval is explicit (`RETURNING`)
- upsert behavior is explicit — managers emit `ON CONFLICT` SQL directly; the SQLite shim translates at the adapter boundary during the overlap window
- after-commit callbacks are defined with MVCC semantics in mind: callbacks fire after `COMMIT` returns, but cross-session reads may still see pre-commit snapshots until each reader starts a new transaction. Phase 4.7 audits every callback against that guarantee.

### Search architecture

Replace FTS5 with `pg_search` (ParadeDB, BM25 via Tantivy):

- `CREATE INDEX ... USING bm25` on content columns (tasks.title/description, memories.content/tags, code_symbols.name/body, code_content.content, skills.description/content).
- Query construction uses the `@@@` operator with pg_search's query DSL: `WHERE title @@@ $1 ORDER BY paradedb.rank(...) DESC LIMIT $2`.
- `pg_trgm` (ships standard) remains available for trigram fuzzy matches where needed.

Ranking is BM25. This gives parity with the existing FTS5 `bm25()` ordering — user-visible search behavior does not regress during the migration. Phase 2.4 parity tests assert representative-query ordering matches across SQLite-FTS5 and Postgres-pg_search.

Search routes through `pick_search_backend(hub, table, mode)` for task, memory, skill, and code-index search. `mode` is a forward-compatible parameter: today the only value is `"keyword"` and dispatches to `BM25SearchBackend` (on Postgres) or `FTS5SearchBackend` (on SQLite during overlap). A future workstream adds `"semantic"` mode dispatching to a `QdrantSearchBackend`; this plan deliberately does not build that — the seam is the deliverable, not the implementation.

`tsvector` + GIN was considered as a simpler, core-Postgres alternative and rejected: it loses BM25 ranking parity with FTS5 (ordering shifts become migration noise users will feel), and it does not pay off until you leave pg_search's operational envelope — which Gobby doesn't (it runs locally, not on managed Postgres).

### Test architecture

- `docker compose up postgres-test` before running the suite (local) or a `services.postgres` container spec in CI
- tests read `DATABASE_URL` from env; psycopg v3 connects
- session-scoped pytest fixture creates a unique schema per xdist worker: `gobby_test_<worker_id>_<session_nonce>`
- migrations apply once per worker schema; per-test isolation is a schema reset (`TRUNCATE ... RESTART IDENTITY CASCADE`), not an outer savepoint
- no language-specific test infra (e.g. testcontainers-python). Compose + env vars port unchanged to the future Rust phase using `sqlx`

### Gobby Pro compatibility

Gobby Pro is assumed to be a **sync and fleet-management hub**, not a hosted Gobby service. Local Gobby instances push/pull data to Gobby Pro over an API; Gobby Pro does not host a customer's Gobby database. This reframing has two important consequences for this plan:

- **Managed-Postgres compatibility is not required.** Local Gobby runs on the user's machine with a Gobby-shipped Postgres image, so we are free to ship any extension we want (including pg_search).
- **Gobby Pro's own datastore is out of scope for this plan.** Whatever database Gobby Pro runs (likely stock Postgres on a managed service, for a different OLTP workload: device registry, sync queues, telemetry) is a separate planning exercise.

What this plan does preserve, so Gobby Pro integration is not harder later:

- **Schema-per-tenant-ready DDL**: `CREATE TABLE tasks`, not `CREATE TABLE gobby.tasks`. Unqualified object names mean an operator could later run multiple Gobby instances against one Postgres with separate schemas if that ever becomes useful (e.g. a self-hosted team deployment). `pg_get_serial_sequence(table, col)` is used for sequence reseeds (task 5.3) — no hardcoded sequence names.
- **Session-pool compatibility**: savepoints and nested transactions work under pgbouncer / RDS Proxy session pooling. Transaction pool mode is *not* compatible today; documented as future work, not fixed here.
- **Secret handling**: for this phase, local runtime credentials live in `bootstrap.yaml` as part of `database_url`. That is intentional but temporary tracked debt. Gobby Pro integration does not get harder because Gobby Pro still talks to local Gobby instances over API, not by sharing their database credentials, and the roadmap is to replace inline `database_url` storage with an OS keyring-backed reference after cutover hardening.
- **API-not-DB integration**: Gobby Pro talks to Gobby instances through Gobby's existing HTTP API (`:60887`) and WebSocket (`:60888`). This plan does not alter those contracts.

Explicitly out of scope for this plan (tracked as follow-ups for Gobby Pro):

- Gobby Pro's control plane, device registry, sync protocol.
- Qdrant-backed semantic search integration. Keyword search via pg_search is delivered here; semantic is a companion workstream with the `pick_search_backend` seam as its insertion point.
- `gobby export` / `gobby import` for moving a user's Gobby state between machines — that's a Gobby Pro sync feature, not part of this hub-storage migration.
- OS keyring integration for Postgres credentials: replace inline `database_url` storage in `bootstrap.yaml` with a keyring-backed reference plus a migration step that rewrites existing plaintext entries.

## Phase 1: PostgreSQL service and bootstrap support

**Goal**: PostgreSQL runs as a first-class local service and bootstrap can select it before DB-backed config loads.

### 1.1 Add `postgres` service to compose template [category: config]

Target: `src/gobby/data/docker-compose.services.yml`

Add a `postgres` service alongside the existing `qdrant` and `neo4j` services:

```yaml
services:
  postgres:
    image: gobby/postgres:17-pgsearch
    container_name: gobby-postgres
    profiles: ["postgres", "all"]
    environment:
      POSTGRES_DB: ${GOBBY_POSTGRES_DB:-gobby}
      POSTGRES_USER: ${GOBBY_POSTGRES_USER:-gobby}
      POSTGRES_PASSWORD: ${GOBBY_POSTGRES_PASSWORD:-gobby_dev}
    ports:
      - "${GOBBY_POSTGRES_PORT:-60891}:5432"
    volumes:
      - gobby_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${GOBBY_POSTGRES_USER:-gobby}"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  gobby_postgres_data:
```

The image is built and published by task 1.4. Verification: `docker compose --profile postgres up -d postgres` succeeds and `pg_isready` exits 0 within 30s; `psql -c "CREATE EXTENSION pg_search"` inside the container succeeds.

### 1.2 Add PostgreSQL installer, uninstaller, and status CLI [category: code] (depends: 1.1)

Target: `src/gobby/cli/installers/postgres.py` (new), `src/gobby/cli/postgres.py` (new Click group)

Mirror the functional pattern used by `src/gobby/cli/installers/qdrant.py` and `src/gobby/cli/installers/neo4j.py`. Do not invent a new installer base class. The installer brings up the compose profile, waits for health, and writes `database_url` plus related defaults into `~/.gobby/bootstrap.yaml`. `hub_backend` stays `sqlite` until explicit activation. Status reads `pg_isready` via `docker compose exec`. Uninstall removes the compose profile and offers volume deletion.

```python
# src/gobby/cli/installers/postgres.py
def install_postgres(*, gobby_home: Path | None = None, port: int = 60891) -> dict[str, Any]:
    compose_file = _ensure_unified_compose(...)
    # docker compose --profile postgres up -d --remove-orphans
    # wait for pg_isready
    # write bootstrap defaults, including database_url
    ...

def uninstall_postgres(*, gobby_home: Path | None = None, remove_data: bool = False) -> dict[str, Any]:
    # docker compose --profile postgres down
    # optionally remove volume
    ...

async def get_postgres_status(...) -> dict[str, Any]:
    # report installed / healthy / configured DSN host+db (not password)
    ...
```

CLI wiring in `src/gobby/cli/postgres.py`:

```python
import click

@click.group("postgres")
def postgres_cli() -> None:
    """Manage the local PostgreSQL hub database."""

@postgres_cli.command("install")
def install_cmd() -> None:
    result = install_postgres()
    _render_install_result(result)

@postgres_cli.command("status")
def status_cmd() -> None:
    click.echo(asyncio.run(render_postgres_status()))

@postgres_cli.command("uninstall")
def uninstall_cmd() -> None:
    result = uninstall_postgres()
    _render_uninstall_result(result)
```

Register the group in `src/gobby/cli/__init__.py`.

### 1.3 Extend bootstrap config with `hub_backend` and `database_url` [category: code] (depends: 1.2)

Target: `src/gobby/config/bootstrap.py`, `~/.gobby/bootstrap.yaml` schema, `src/gobby/runner.py::runner_init`

Add two fields to `BootstrapConfig`:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class BootstrapConfig:
    database_path: str = "~/.gobby/gobby-hub.db"

    hub_backend: Literal["sqlite", "postgres"] = "sqlite"
    database_url: str | None = None  # psycopg v3 DSN; required when hub_backend=postgres
```

Validation:

- `hub_backend="postgres"` requires `database_url` non-empty
- `hub_backend="sqlite"` tolerates `database_url=None`
- `database_url` is stored verbatim in `bootstrap.yaml` for this phase; require `bootstrap.yaml` to be owner read/write only (`0600`) and track that plaintext credential storage as migration debt until OS keyring integration ships
- parse errors raise `BootstrapConfigError` with field-level messages

Update `runner_init()` to branch on `hub_backend` when constructing the hub database. Do not allow DB-stored config to override bootstrap-level backend selection.

### 1.4 Build and publish `gobby/postgres:17-pgsearch` image [category: config] (depends: 1.1)

Target: `src/gobby/data/postgres-pgsearch/Dockerfile` (new), CI publish workflow

Build a minimal Postgres image with pg_search added on top of `postgres:17`:

```dockerfile
# src/gobby/data/postgres-pgsearch/Dockerfile
FROM postgres:17

# Install build dependencies, pg_search, then clean up
ARG PG_SEARCH_VERSION=0.17.0
ARG PG_SEARCH_SHA256=<release-sha256>
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL "https://github.com/paradedb/paradedb/releases/download/v${PG_SEARCH_VERSION}/pg_search-v${PG_SEARCH_VERSION}-pg17-$(dpkg --print-architecture)-ubuntu2204.deb" \
        -o /tmp/pg_search.deb \
    && echo "${PG_SEARCH_SHA256}  /tmp/pg_search.deb" | sha256sum -c - \
    && dpkg -i /tmp/pg_search.deb \
    && rm /tmp/pg_search.deb \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Preload pg_search (required for query-time registration)
RUN echo "shared_preload_libraries = 'pg_search'" >> /usr/share/postgresql/postgresql.conf.sample
```

The image build must pin an exact pg_search release artifact and checksum in the repo. Updating pg_search is a deliberate version bump, not "whatever ParadeDB ships today." The image must:

- Tag as `gobby/postgres:17-pgsearch` (and matching `:17-pgsearch-<semver>`)
- Set `shared_preload_libraries = 'pg_search'` so pg_search can register its query hooks at startup
- Pass `pg_isready` and `CREATE EXTENSION pg_search` smoke tests in the build pipeline
- Publish to the Gobby image registry (GHCR or equivalent) on every release tag
- Bump `PG_SEARCH_VERSION` and `PG_SEARCH_SHA256` together, verify the checksum against the upstream release artifact before merge, and rerun the Postgres smoke/search test suite before publish
- Security checklist: monitor `pg_search` / ParadeDB advisories through GitHub security alerts or CVE feeds so future bumps are tracked intentionally

CI task publishes image on git tag. License notice (AGPL-3.0 for pg_search, PostgreSQL license for Postgres) is included in the image via `/usr/share/doc/pg_search/copyright` already present in the .deb.

### 1.5 Add `gobby postgres activate` and `deactivate` commands [category: code] (depends: 1.3, 1.4)

Target: `src/gobby/cli/postgres.py`

`activate` flips `hub_backend` from `sqlite` to `postgres` in `~/.gobby/bootstrap.yaml`. `deactivate` flips it back. Guardrails:

- refuse if the daemon is running (require `gobby stop` first)
- refuse if migration has not yet produced the `migration_complete` marker row in the Postgres `schema_migrations` table
- write a dated backup of `bootstrap.yaml` before rewriting
- print the exact rollback command on success

The command itself is the enforcement point for migration completion. Ship it in Phase 1 if useful, but `gobby postgres activate` must perform the runtime validation above and fail clearly until Phase 5 has completed successfully.

```python
@postgres_cli.command("activate")
def activate_cmd() -> None:
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    if not _postgres_migration_complete():
        raise click.ClickException("Run `gobby postgres migrate-from-sqlite` first")
    _backup_bootstrap()
    _set_bootstrap_field("hub_backend", "postgres")
    click.echo("hub_backend set to postgres. To roll back:")
    click.echo("  gobby stop && gobby postgres deactivate && gobby start")

@postgres_cli.command("deactivate")
def deactivate_cmd() -> None:
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    _backup_bootstrap()
    _set_bootstrap_field("hub_backend", "sqlite")
```

## Phase 2: Test infrastructure for dual-backend work

**Goal**: the suite runs against Postgres before Phase 3 lands any dual-backend code, so every subsequent task is validated against the real backend.

### 2.1 Add PostgreSQL to the test compose stack and CI [category: config] (depends: 1.1)

Target: `docker-compose.test.yml` (new), CI workflow config, `pyproject.toml`

Add a test-scoped compose file exposing Postgres on a distinct port (60892) so it does not clash with a dev instance:

```yaml
services:
  postgres-test:
    image: gobby/postgres:17-pgsearch
    environment:
      POSTGRES_DB: gobby_test
      POSTGRES_USER: gobby_test
      POSTGRES_PASSWORD: gobby_test
    ports:
      - "60892:5432"
    tmpfs:
      - /var/lib/postgresql/data  # fast and ephemeral: all database data disappears when the container stops
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U gobby_test"]
      interval: 2s
      timeout: 2s
      retries: 15
```

The `tmpfs` entry at `/var/lib/postgresql/data` makes the PostgreSQL test container fully ephemeral. Tests cannot rely on data surviving `docker compose` restarts, CI workflows that restart services mid-run must recreate or reseed the database, and this is intentionally different from the persistent-volume configuration used in dev/prod.

CI job uses a service container spec:

```yaml
jobs:
  tests:
    services:
      postgres:
        image: gobby/postgres:17-pgsearch
        env:
          POSTGRES_DB: gobby_test
          POSTGRES_USER: gobby_test
          POSTGRES_PASSWORD: gobby_test
        ports:
          - 60892:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 2s
    env:
      DATABASE_URL: postgresql://gobby_test:gobby_test@localhost:60892/gobby_test
```

Add `psycopg[binary,pool]>=3.2` and `pytest-xdist` to `pyproject.toml`. Confirm the local test runner can reach the compose-provided `DATABASE_URL`.

### 2.2 Add a schema-per-worker pytest fixture [category: test] (depends: 2.1)

Target: `tests/fixtures/postgres.py` (new), `tests/conftest.py`

Per-worker isolation without per-session container churn:

```python
# tests/fixtures/postgres.py
import logging
import os
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from gobby.storage.hub.postgres import PostgresHubDatabase

logger = logging.getLogger(__name__)


def _cleanup_orphaned_schemas(url: str, age_hours: int = 24) -> None:
    """Drop only aged `gobby_test_*` schemas from abandoned test runs."""
    cutoff_epoch = int(time.time()) - age_hours * 3600
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name LIKE 'gobby_test_%'
            """
        ).fetchall()
        for (schema_name,) in rows:
            parts = schema_name.split("_", 5)
            if len(parts) != 6:
                continue
            try:
                created_epoch = int(parts[2])
            except ValueError:
                continue
            if created_epoch > cutoff_epoch:
                continue
            logger.warning("Dropping orphaned Postgres test schema %s", schema_name)
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


@pytest.fixture(scope="session")
def postgres_schema(worker_id: str) -> Iterator[str]:
    """Create a unique schema for this xdist worker, drop it on teardown."""
    created_epoch = int(time.time())
    nonce = uuid.uuid4().hex[:6]
    schema = f"gobby_test_{created_epoch}_{os.getpid()}_{worker_id}_{nonce}"
    url = os.environ["DATABASE_URL"]
    _cleanup_orphaned_schemas(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield schema
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            ).fetchone()
            if exists:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _reset_schema(url: str, schema: str) -> None:
    """Truncate all user tables in the worker schema and reset identities."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {schema}")
        tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = current_schema()
                  AND tablename <> 'schema_migrations'
                """
            ).fetchall()
        ]
        if tables:
            joined = ", ".join(tables)
            conn.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")


@pytest.fixture
def postgres_db(postgres_schema: str) -> Iterator[PostgresHubDatabase]:
    """Per-test hub database over a reset worker schema."""
    url = os.environ["DATABASE_URL"] + f"?options=-csearch_path%3D{postgres_schema}"
    db = PostgresHubDatabase(url)
    db.apply_migrations()  # no-op after first run in this schema
    _reset_schema(os.environ["DATABASE_URL"], postgres_schema)
    try:
        yield db
    finally:
        _reset_schema(os.environ["DATABASE_URL"], postgres_schema)
```

The reset-based approach is intentional: a single outer savepoint is not sufficient once the runtime uses pooled connections, because work can commit on a different connection and bypass that savepoint entirely. Resetting the worker schema gives real isolation without constraining production code to a single test-only connection model.

### 2.3 Parametrize storage fixtures over both backends [category: test] (depends: 2.2)

Target: `tests/conftest.py`, selected storage test files under `tests/storage/`

Introduce a `hub_db` fixture that yields each backend in turn via `@pytest.fixture(params=["sqlite", "postgres"])`. Tests that previously used `temp_db` opt into the dual-backend fixture by renaming; tests asserting SQLite-specific behavior keep `temp_db` and are explicitly marked for deletion in Phase 7.

Expected footprint for this task: around 15 of the 48 `tests/storage/` files migrate to `hub_db` now. The remaining ~33 migrate as part of the Phase 3 port work that covers them.

### 2.4 Add dialect parity regression tests [category: test] (depends: 2.2)

Target: `tests/storage/test_dialect_parity.py` (new)

Pin behavior that commonly diverges between SQLite and Postgres. Each assertion runs against both backends via the `hub_db` parametrization:

- upsert semantics (`INSERT OR IGNORE` vs `ON CONFLICT DO NOTHING`)
- generated-key consistency (`lastrowid` replacement via `RETURNING id`)
- JSON path extraction results (`json_extract(col, '$.k')` vs `col->>'k'`)
- timestamp default values are timezone-aware and UTC on both sides
- search ordering on representative queries — ordering must match; score values will not
- `UNIQUE (col, COALESCE(x, '__global__'))` behavior vs `UNIQUE NULLS NOT DISTINCT (col, x)`

These tests catch silent semantic drift during Phase 3–4 work.

## Phase 3: Backend-neutral storage seam and migration runner

**Goal**: storage call sites depend on a backend-neutral `HubDatabase` protocol, and the migration runner works on both backends.

### 3.1 Define the `HubDatabase` protocol [category: code] (depends: Phase 2)

Target: `src/gobby/storage/hub/protocol.py` (new)

Replace `DatabaseProtocol`'s SQLite-specific surface with a backend-neutral contract:

```python
# src/gobby/storage/hub/protocol.py
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal, Protocol

Row = Mapping[str, Any]

class Cursor(Protocol):
    def fetchone(self) -> Row | None: ...
    def fetchall(self) -> Sequence[Row]: ...
    @property
    def rowcount(self) -> int: ...

class Savepoint(Protocol):
    def release(self) -> None: ...
    def rollback(self) -> None: ...

class Transaction(Protocol):
    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor: ...
    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None: ...
    def savepoint(self, name: str) -> Savepoint: ...
    def after_commit(self, callback: Callable[[], None]) -> None: ...

class HubDatabase(Protocol):
    dialect: Literal["sqlite", "postgres"]

    @contextmanager
    def transaction(self) -> Iterator[Transaction]: ...
    def apply_migrations(self) -> None: ...
    def close(self) -> None: ...
```

**After-commit callback contract**: the protocol documents that callbacks fire after `COMMIT` returns on the writing session. Cross-session reads remain subject to each reader's snapshot — under Postgres MVCC, a callback that spawns work on another session may observe pre-commit state until that session starts a new transaction. Code relying on "callback sees its own write" stays correct. Code that spawns cross-session reads is audited in task 4.7.

No row or cursor type leaks `sqlite3` objects. Transaction objects own savepoint creation; callers never sniff `conn.in_transaction`.

### 3.2 Implement `SqliteHubDatabase` shim [category: code] (depends: 3.1)

Target: `src/gobby/storage/hub/sqlite.py` (new)

Wraps existing `LocalDatabase`. Converts `sqlite3.Row` to plain `dict` at the adapter boundary. Translates `$N` placeholders to `?` so managers can use Postgres-native query strings during the overlap.

```python
# src/gobby/storage/hub/sqlite.py
import re
from contextlib import contextmanager
from collections.abc import Iterator

from gobby.storage.database import LocalDatabase
from gobby.storage.hub.protocol import HubDatabase, Transaction

_DOLLAR_RE = re.compile(r"\$(\d+)")


def _pg_to_sqlite_params(sql: str) -> str:
    """Translate $1, $2, ... to ?, ? keeping positional order."""
    return _DOLLAR_RE.sub("?", sql)


class SqliteHubDatabase:
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        self._local = LocalDatabase(path)

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self._local.transaction() as conn:
            yield _SqliteTransaction(conn)

    def apply_migrations(self) -> None:
        MigrationRunner(self).apply_pending()

    def close(self) -> None:
        self._local.close()


class _SqliteTransaction:
    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params=()):
        cur = self._conn.execute(_pg_to_sqlite_params(sql), tuple(params))
        return _SqliteCursor(cur)

    # savepoint / after_commit / executemany implementations follow the same
    # boundary-translation pattern.
```

Upsert dialect translation is limited to placeholder rewriting; `ON CONFLICT` SQL is portable between SQLite 3.35+ and Postgres and does not need translation. SQL that genuinely differs (`datetime(...)`, `json_extract(...)`) is handled in Phase 4.6 with explicit dialect branches rather than runtime translation.

### 3.3 Implement `PostgresHubDatabase` [category: code] (depends: 3.1)

Target: `src/gobby/storage/hub/postgres.py` (new)

psycopg v3 + `psycopg_pool.ConnectionPool` (sync; the codebase is sync today). All pool and connection tuning is env-var-driven so the Rust port (sqlx) inherits the same config surface unchanged.

```python
# src/gobby/storage/hub/postgres.py
import os
from contextlib import contextmanager
from collections.abc import Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gobby.storage.hub.protocol import HubDatabase, Transaction


class PostgresHubDatabase:
    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=int(os.getenv("PGPOOL_MIN", "2")),
            max_size=int(os.getenv("PGPOOL_MAX", "10")),
            timeout=int(os.getenv("PGCONNECT_TIMEOUT", "5")),
            kwargs={
                "application_name": os.getenv("PGAPPNAME", "gobby"),
                "row_factory": dict_row,
            },
        )

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self._pool.connection() as conn, conn.transaction():
            yield _PostgresTransaction(conn)

    def apply_migrations(self) -> None:
        MigrationRunner(self).apply_pending()

    def close(self) -> None:
        self._pool.close()
```

`dict_row` gives `Mapping[str, Any]` rows directly, eliminating per-query adapter code.

### 3.4 Port row consumers off `sqlite3.Row` [category: refactor] (depends: 3.2, 3.3)

Target: every module under `src/gobby/storage/` (roughly 20 modules; scope via grep for `sqlite3.Row`, `sqlite3.Cursor`, and `.keys()`/`.values()` on query results)

Change row parsers from `sqlite3.Row` to `Mapping[str, Any]`. Modules that own their own row → dataclass adapter keep that boundary; only the adapter signature changes.

```python
# before
def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(id=row["id"], title=row["title"], ...)

# after
from collections.abc import Mapping
from typing import Any

def _row_to_task(row: Mapping[str, Any]) -> Task:
    return Task(id=row["id"], title=row["title"], ...)
```

No behavioral changes; type annotation and import cleanup only. The Phase 2.3 parametrized fixtures must pass against both backends after this task.

### 3.5 Replace `lastrowid` with `RETURNING id` [category: refactor] (depends: 3.4)

Target: `src/gobby/storage/task_dependencies.py:73`, `src/gobby/storage/task_affected_files.py` (two sites), `src/gobby/storage/workflow_audit.py:102`

Rewrite these four sites to capture generated IDs via `RETURNING`. SQLite 3.35+ supports `RETURNING` natively; no SQLite fallback needed.

```python
# before
cur = conn.execute(
    "INSERT INTO workflow_audit(event, workflow_id, payload) VALUES (?, ?, ?)",
    (event, workflow_id, payload),
)
audit_id = cur.lastrowid

# after
row = conn.execute(
    "INSERT INTO workflow_audit(event, workflow_id, payload) "
    "VALUES ($1, $2, $3) RETURNING id",
    (event, workflow_id, payload),
).fetchone()
audit_id = row["id"]
```

### 3.6 Replace `INSERT OR IGNORE` / `INSERT OR REPLACE` with `ON CONFLICT` [category: refactor] (depends: 3.4)

Target: `src/gobby/storage/projects.py:164`, `src/gobby/storage/session_tasks.py`, `src/gobby/storage/sessions.py`, `src/gobby/storage/pipelines.py`, `src/gobby/storage/agents.py:213`, plus any additional sites surfaced by grep (≈8 total)

Write SQL in Postgres-native form. `ON CONFLICT` is portable to SQLite 3.24+.

```python
# before
conn.execute(
    "INSERT OR IGNORE INTO projects(id, name) VALUES (?, ?)",
    (project_id, name),
)

# after
conn.execute(
    "INSERT INTO projects(id, name) VALUES ($1, $2) "
    "ON CONFLICT (id) DO NOTHING",
    (project_id, name),
)
```

For `INSERT OR REPLACE`-style full-row replacement (`agents.py:213`), spell the column list explicitly in `ON CONFLICT ... DO UPDATE SET col = EXCLUDED.col, ...`. No silent "replace everything" semantics — every replaced column is enumerated.

### 3.7 Rewrite the migration runner to drop `executescript` and support both backends [category: code] (depends: 3.2, 3.3)

Target: `src/gobby/storage/migrations.py`

The current runner calls `conn.executescript()` (Postgres has no equivalent) and splits SQL on `;` (breaks trigger / function bodies). The rewrite discovers migration files from disk and executes statements one at a time with dollar-quote-aware splitting.

```python
# src/gobby/storage/migrations.py
from dataclasses import dataclass
from pathlib import Path

from gobby.storage.hub.protocol import HubDatabase, Transaction


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    shared_path: Path | None
    sqlite_path: Path | None
    postgres_path: Path | None

    def path_for_dialect(self, dialect: str) -> Path:
        if dialect == "sqlite" and self.sqlite_path is not None:
            return self.sqlite_path
        if dialect == "postgres" and self.postgres_path is not None:
            return self.postgres_path
        if self.shared_path is not None:
            return self.shared_path
        raise RuntimeError(f"No {dialect} migration file for v{self.version}")


class MigrationRunner:
    def __init__(self, hub: HubDatabase) -> None:
        self._hub = hub

    def apply_pending(self) -> None:
        self._ensure_schema_migrations_table()
        applied = self._read_applied_versions()
        for migration in self._discover_migrations():
            if migration.version in applied:
                continue
            with self._hub.transaction() as txn:
                self._run_migration(txn, migration)
                txn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES ($1, NOW())",
                    (migration.version,),
                )

    def _run_migration(self, txn: Transaction, migration: Migration) -> None:
        path = migration.path_for_dialect(self._hub.dialect)
        sql = path.read_text()
        for statement in _split_statements_respecting_dollar_quotes(sql):
            if statement.strip():
                txn.execute(statement)
```

`_split_statements_respecting_dollar_quotes` must treat semicolons outside any active quote/comment boundary as the only split points. It needs unit tests for nested or adjacent dollar-quoted tags (for example `$outer$...$inner$...$outer$`), single-quoted strings containing `;` or `$`, line comments and block comments containing `$tag$` patterns, and mixed contexts where quote-looking text appears inside a dollar-quoted body. Do not use the splitter in production migration runs until those tests pass.

Migration file layout:

- `migrations/NNN_name.sql` — shared, works on both backends
- `migrations/NNN_name.sqlite.sql` / `migrations/NNN_name.postgres.sql` — dialect-specific

`schema_migrations` schema is identical on both backends:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
);
```

For Postgres, `NOW()` returns `TIMESTAMPTZ`. For SQLite during overlap, `NOW()` is not a builtin; the shared migration table creation uses `CURRENT_TIMESTAMP` which both backends accept — and the runner's insert uses `NOW()` on Postgres and `CURRENT_TIMESTAMP` on SQLite via a dialect-check in `_run_migration`'s bookkeeping step.

## Phase 4: PostgreSQL schema and query parity

**Goal**: every query path runs natively on Postgres, FTS5 is replaced with `pg_search` BM25 indexes, and all Rust-portability hygiene is applied.

### 4.1 Convert Python migration callables to SQL-only files [category: refactor] (depends: 3.7)

Target: `src/gobby/storage/migrations.py`, new `src/gobby/storage/migrations/*.sql` files

Current callables identified in the audit: `_setup_code_symbols_fts`, `_setup_code_content_fts`, `_setup_tasks_fts`, `_setup_memories_fts`, `_migrate_claimed_by_session_id`, plus any others the first grep surfaces. Each callable's DDL is rewritten as a dialect-specific `.sql` file keyed to the same version number.

Imperative backfill logic (reading rows and writing derived values) becomes either `INSERT ... SELECT` or `UPDATE ... FROM (SELECT ...)`. Postgres supports both directly; SQLite supports `UPDATE ... FROM (SELECT ...)` as of 3.33.

If a specific callable genuinely cannot be expressed in pure SQL, the migration is blocked until that transformation is redesigned into SQL terms. Do not keep a Python fallback in the final cutover plan.

Rationale: these files must be consumable by `sqlx migrate run` after the Rust port. "Mostly SQL, plus a few Python fallbacks" is how this work rots immediately.

### 4.2 Add `postgres_baseline_schema.sql` [category: code] (depends: 3.7)

Target: `src/gobby/storage/postgres_baseline_schema.sql` (new)

Translate `baseline_schema.sql` to Postgres-native types:

| SQLite | Postgres |
| --- | --- |
| `TEXT` (timestamp, ISO8601) | `TIMESTAMPTZ` |
| `TEXT` with `DEFAULT datetime('now')` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `INTEGER` 0/1 boolean | `BOOLEAN` |
| `BLOB` | `BYTEA` |
| `TEXT` holding JSON | `JSONB` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| `UNIQUE (col, COALESCE(x, '__global__'))` | `UNIQUE NULLS NOT DISTINCT (col, x)` (PG15+) |

Every FTS5 virtual table is replaced by a `pg_search` BM25 index on the content table (task 4.4). pg_search's index access method keeps the index in sync automatically — no `BEFORE UPDATE` trigger or shadow column is required.

Every partial index in the SQLite schema is preserved — SQL syntax is portable.

Foreign keys in the Postgres baseline are declared `DEFERRABLE INITIALLY IMMEDIATE`. The migration tool uses `SET CONSTRAINTS ALL DEFERRED` during the one-shot import so cyclical references such as `sessions` ↔ `agent_runs` validate at `COMMIT` instead of forcing unsafe trigger-disabling hacks.

`PRAGMA foreign_keys=ON` has no Postgres equivalent; foreign keys are always enforced. `PRAGMA query_only=ON` (used for test read-only enforcement) becomes `SET TRANSACTION READ ONLY` at the adapter level for the read-only fixture path.

### 4.3 Standardize parameter style on `$1` [category: refactor] (depends: 3.2)

Target: every `.execute()` / `.executemany()` call site that currently uses `?`

Rewrite `?` → `$1`, `$2`, ... by query shape, not by blind regex. Dynamic `IN (...)` builders and reusable clause helpers need explicit rewrites so numbering stays valid when placeholder counts vary.

Execute as an audited pass grouped by query class:

- fixed-arity statements: direct manual renumbering
- dynamic `IN (...)` builders: replace with a helper that emits numbered placeholders from a starting offset
- shared clause helpers: rewrite once, then reuse from callers

Add a lint (custom ruff plugin or pre-commit grep) in `src/gobby/storage/` that fails on new `?` placeholders outside the shim itself.

### 4.4 Replace FTS5 with `pg_search` BM25 indexes [category: code] (depends: 4.2)

Target: `src/gobby/storage/postgres_baseline_schema.sql`, migration files for `tasks_fts`, `memories_fts`, `code_symbols_fts`, `code_content_fts`, `skills_fts` replacements

`pg_search` maintains its own inverted index transparently — no `tsvector` column, no refresh trigger. For each content table, create a BM25 index covering the searchable columns:

```sql
-- One-time extension enable (idempotent; migration checks FIRST)
CREATE EXTENSION IF NOT EXISTS pg_search;

-- Tasks
CREATE INDEX tasks_search_bm25 ON tasks
USING bm25 (id, title, description)
WITH (key_field='id');

-- Memories — stringified JSON tags via a generated column because BM25
-- indexes want text, not JSONB, and we need tag content searchable
ALTER TABLE memories
ADD CONSTRAINT tags_is_array
CHECK (tags IS NULL OR jsonb_typeof(tags) = 'array');

ALTER TABLE memories
ADD COLUMN tags_text TEXT
GENERATED ALWAYS AS (
    COALESCE((SELECT string_agg(value, ' ')
              FROM jsonb_array_elements_text(tags) AS value), '')
) STORED;

CREATE INDEX memories_search_bm25 ON memories
USING bm25 (id, content, tags_text)
WITH (key_field='id');

-- Code symbols
CREATE INDEX code_symbols_search_bm25 ON code_symbols
USING bm25 (id, name, signature, body)
WITH (key_field='id');

-- Code content
CREATE INDEX code_content_search_bm25 ON code_content
USING bm25 (id, content)
WITH (key_field='id');

-- Skills
CREATE INDEX skills_search_bm25 ON skills
USING bm25 (id, name, description, content)
WITH (key_field='id');
```

The `tags_is_array` constraint keeps the generated column honest if `memories.tags` drifts later, and the `COALESCE(..., '')` is required so `tags_text` stays TEXT-compatible for NULL / empty tag sets.

Queries use the `@@@` operator with pg_search's DSL:

```sql
SELECT id, title, paradedb.rank_bm25(id) AS score
FROM tasks
WHERE title @@@ $1 OR description @@@ $1
ORDER BY score DESC
LIMIT $2;
```

pg_search keeps its index in sync automatically on `INSERT` / `UPDATE` / `DELETE` via Postgres's index access method hooks — **no application-side refresh, no `BEFORE UPDATE` trigger needed**. This is a material simplification over tsvector + trigger-maintained columns.

For the `memories` tag-stripping case, the `tags_text` generated column keeps the BM25 index clean of JSON syntax without duplicating state — the generated column is deterministic from `tags`, so there is no sync risk.

`skills_fts` (contentless in SQLite) and the other four tables all have identical handling now: one `CREATE INDEX ... USING bm25` per table, plus any generated columns required to flatten non-text content into searchable text.

### 4.5 Port search backends [category: code] (depends: 4.4)

Target: `src/gobby/storage/tasks/_search.py`, `src/gobby/memory/` search code, `src/gobby/storage/skills.py`, `src/gobby/search/` code-index search code

Introduce a small dispatch layer so managers stop writing FTS5 SQL directly:

```python
from typing import Literal, Protocol

from gobby.storage.hub.protocol import HubDatabase

SearchMode = Literal["keyword", "semantic"]


class SearchHit:
    id: str
    score: float
    snippet: str | None


class SearchBackend(Protocol):
    def search(self, query: str, limit: int) -> list[SearchHit]: ...


def pick_search_backend(
    hub: HubDatabase,
    table: str,
    mode: SearchMode = "keyword",
) -> SearchBackend:
    if mode == "semantic":
        raise NotImplementedError(
            "Semantic search is a follow-up workstream; use mode='keyword' today."
        )
    if hub.dialect == "sqlite":
        return FTS5SearchBackend(hub, table)
    return BM25SearchBackend(hub, table)
```

Each caller (task search, memory search, skill search, code-index search) obtains a `SearchBackend` via `pick_search_backend` rather than writing SQL directly. The Postgres backend (`BM25SearchBackend`) uses `@@@` with pg_search's query DSL and `paradedb.rank_bm25(id)` for scoring.

Ranking behavior is **BM25 on both backends** (FTS5 `bm25()` on SQLite, pg_search BM25 on Postgres). The Phase 2.4 parity tests assert representative-query ordering matches across backends; exact scores differ because scoring parameters (k1, b) are implementation-specific, but ordering of top-N results on representative queries should be stable.

The `mode` parameter exists to hold the seam for a future Qdrant-backed `SemanticSearchBackend`. This plan does not implement it; the companion semantic-search workstream plugs in here without touching callers.

### 4.6 Port remaining SQL (`json_extract`, `datetime`, `strftime`, `julianday`) [category: refactor] (depends: 4.2)

Target: any manager using these functions; ~17 `json_extract` sites plus sporadic `strftime` / `julianday`

Translations:

- `json_extract(col, '$.key')` → `col->>'key'` (scalar text) or `col->'key'` (JSONB subvalue)
- `datetime('now', '-1 day')` → `NOW() - INTERVAL '1 day'`
- `strftime('%Y-%m-%d', col)` → `to_char(col, 'YYYY-MM-DD')`
- `RANDOM()` → `random()` (syntax identical; no change)
- `julianday(a) - julianday(b)` → `EXTRACT(EPOCH FROM (a - b)) / 86400.0`

The SQLite shim does not translate these at runtime — too many semantic edges. Each call site is rewritten to Postgres-native form. For the short overlap window where SQLite is still a runtime backend, managers use a dialect branch:

```python
if hub.dialect == "sqlite":
    sql = "SELECT json_extract(payload, '$.owner') FROM tasks WHERE id = $1"
else:
    sql = "SELECT payload->>'owner' FROM tasks WHERE id = $1"
```

These branches are deleted in Phase 7.2.

### 4.7 Audit after-commit callback semantics under MVCC [category: research] (depends: 3.1)

Target: every call site of `after_commit` callbacks (grep for `after_commit` / `_run_after_commit_callbacks`)

For each call site, document:

1. What the callback does.
2. Whether it reads database state — and if so, whether the reading session is the same as the writing session, a different session, or async.
3. Whether it mutates external state (files, network, in-memory structures).

Any callback that reads database state from a session other than the writing one is flagged as "requires explicit transaction boundary." Phase 4.7 does not stop at reporting: fix each such case by moving the affected read inside the originating transaction, by forcing a fresh `BEGIN` on the reading session before the read, or by moving the logic into a post-snapshot-safe transaction. Audit every `savepoint()` / `conn.in_transaction` usage that assumes SQLite semantics and remediate the broken code paths in the same phase.

Deliverables:

- a short report committed under `docs/postgres-callback-audit.md`, referenced from the cutover runbook (6.1)
- remediation changes for every high-risk callback / transaction-boundary bug found by the audit
- integration tests that open separate sessions under PostgreSQL MVCC and verify after-commit callbacks do not observe or expose stale / partial state across sessions

Cutover is blocked until the Phase 4.7 report shows no unresolved high-risk callbacks, no known broken `savepoint()` / `conn.in_transaction` usages remain, and all new MVCC integration tests pass.

## Phase 5: One-shot SQLite → PostgreSQL migration tool

**Goal**: a single command imports an entire SQLite hub database into a fresh Postgres database, validates deterministically, and leaves SQLite untouched.

### 5.1 Implement `gobby postgres migrate-from-sqlite` [category: code] (depends: Phase 4)

Target: `src/gobby/cli/postgres.py`, `src/gobby/storage/migration/sqlite_to_postgres.py` (new)

Command signature:

```
gobby postgres migrate-from-sqlite
    --source ~/.gobby/gobby-hub.db
    --target $DATABASE_URL
    [--batch-size 1000]
    [--resume]
    [--dry-run]
```

Execution order:

1. Assert daemon is stopped (refuses otherwise).
2. Open SQLite source read-only (`file:<path>?mode=ro&immutable=1`).
3. Open Postgres target and run `apply_migrations()` to create the schema.
4. Drop BM25 indexes created by the baseline schema so bulk load does not pay per-row index maintenance cost.
5. Begin a single import transaction and `SET CONSTRAINTS ALL DEFERRED`. Because the Postgres baseline uses deferrable FKs, this preserves real FK validation at `COMMIT` while still allowing cyclical references to load.
6. For each table in dependency-safe order, stream rows from SQLite and bulk-insert via psycopg v3 `copy()`:

    ```python
    with pg.cursor() as cur, cur.copy(
        "COPY tasks (id, title, description, created_at, updated_at) FROM STDIN"
    ) as copy:
        for batch in sqlite_reader.read_in_batches("tasks", batch_size):
            for row in batch:
                copy.write_row((
                    row["id"], row["title"], row["description"],
                    row["created_at"], row["updated_at"],
                ))
    ```

7. Commit the import transaction. This is the FK-validation point: any deferred-constraint violation aborts the migration.
8. Recreate BM25 indexes. `CREATE INDEX ... USING bm25 ...` rebuilds from the just-loaded data. Validate index size and row count are non-zero.
9. Reseed sequences (task 5.3).
10. Run validation (task 5.2).
11. Write a `migration_complete` marker row to `schema_migrations` so `postgres activate` knows the target is safe to flip to.

Resumability: record per-table progress in a `_migration_progress` table on the target. `--resume` picks up from the last completed table. If a table fails mid-copy, it is truncated and restarted — not resumed within-table. SQLite source is never modified.

### 5.2 Implement validation checks [category: code] (depends: 5.1)

Target: `src/gobby/storage/migration/validation.py` (new)

Runs after bulk copy. All checks must pass for the migration command to exit 0.

- **Row counts**: `SELECT COUNT(*) FROM <t>` on both sides for every table; must match exactly.
- **FK integrity**: primarily validated by the commit of the deferred-constraint import transaction in step 5.1.7. Validation also runs explicit orphan checks generated from `pg_constraint` metadata so we do not trust transaction success alone.
- **Content hashes**: for a representative set of tables (`sessions`, `tasks`, `memories`, `config_store`, `code_symbols`, `agents`, `metrics`, workflow audit), compute an order-independent hash of canonical JSON-encoded rows on both sides and compare. Order-independence is achieved by sorting by primary key before hashing; JSON encoding uses sorted keys.
- **Sequence reseed**: for every identity column, `SELECT last_value FROM <sequence>` must equal `MAX(id) + 1` (or `MAX(id)` if the sequence `is_called=false`).
- **BM25 index coverage**: for each FTS-replacement table, confirm the BM25 index exists (`pg_class` lookup), was populated (`pg_stat_user_indexes.idx_tup_read > 0` after a smoke query), and returns non-zero hits on a canned query built from a random sampled row's searchable content. This catches silent index-build failures that pg_search can in principle produce on malformed input.

Output format: one line per check with `✓` / `✗`, plus a summary JSON artifact written to `~/.gobby/migrations/validate-<timestamp>.json` for auditing.

### 5.3 Implement sequence / identity reseed [category: code] (depends: 5.1)

Target: `src/gobby/storage/migration/reseed.py` (new)

For every identity column on the target, reseed using the actual data max so subsequent inserts don't collide with migrated rows:

```sql
SELECT setval(
    pg_get_serial_sequence($1, 'id'),
    COALESCE((SELECT MAX(id) FROM <t>), 0) + 1,
    false
);
```

The table list is discovered dynamically from Postgres identity/sequence metadata (`information_schema.columns.is_identity = 'YES'` plus `pg_get_serial_sequence(...)` where applicable) — no hand-maintained list and no fragile `column_default LIKE 'nextval%'` heuristic.

## Phase 6: Cold cutover to PostgreSQL runtime

**Goal**: flip the daemon to Postgres with a documented rollback window and zero tolerance for silent failures.

### 6.1 Cutover runbook [category: docs] (depends: Phase 5)

Target: `docs/runbooks/postgres-cutover.md` (new)

> Warning: once `gobby postgres activate` runs, Postgres becomes the live write target for the validation window. If rollback is required, writes made in Postgres during that validation window are at risk and must be captured before deactivation.

Step-by-step:

1. Announce cutover, schedule window.
2. `gobby stop`.
3. Back up `~/.gobby/gobby-hub.db` to a dated path; record the SHA-256 for later verification.
4. `gobby postgres install` if not already installed.
5. `gobby postgres migrate-from-sqlite --source ~/.gobby/gobby-hub.db --target $DATABASE_URL`.
6. Verify the validation output exits 0 and writes the `migration_complete` marker row.
7. Enable validation-window write capture on the Postgres target before activation. Use an append-only audit log or change-capture stream, and record the sink / artifact location in the cutover ticket. If that capture is not live, cutover is blocked.
8. `gobby postgres activate`.
9. `gobby start`.
10. Run the smoke suite: `gobby status`, `gobby sessions list`, `gobby tasks list`, `gobby memory search "foo"`, `gobby code search "bar"`. Each must return expected data within expected latency.
11. Announce cutover complete; the validation window starts now. The maximum validation window is 48h from `gobby postgres activate`, recorded as an explicit deadline in the cutover ticket. If unresolved blocking regressions remain at that 48h deadline, roll back instead of extending the window silently.

Explicit watch-list for the validation window:

- MVCC-driven callback regressions (see the Phase 4.7 audit report)
- search result ordering drift on representative queries
- latency regressions > 2× baseline on storage-bound endpoints
- health of the append-only audit log / change-capture stream enabled in step 7

Do not enter the validation window until the Phase 4.7 callback remediation gate is green.

### 6.2 Rollback runbook [category: docs] (depends: 6.1)

Target: `docs/runbooks/postgres-rollback.md` (new)

When to roll back: any validation-window regression that cannot be fixed forward within 2h, OR any detected data corruption.

Steps:

1. `gobby stop`.
2. Export all Postgres-side writes made during the validation window to a safe artifact before flipping `hub_backend` back. Use the append-only audit log / change-capture stream enabled in the cutover runbook (6.1) as the primary source, and supplement it with targeted `pg_dump` / SQL exports for tables that support `updated_at` filtering.
3. `gobby postgres deactivate` (flips `hub_backend=sqlite`).
4. The pre-cutover SQLite database is untouched; no restore needed if the rollback happens inside the validation window.
5. `gobby start`.
6. Attach the validation-window export artifact to the rollback / post-mortem task for forensic analysis and any later partial-merge work.
7. File a task to re-migrate after the blocking regression is fixed.

Explicit data-loss rule: writes made to Postgres during the validation window are at risk on rollback and are not merged back into SQLite automatically. The export step above exists for forensic analysis and potential partial-merge tooling later, not for automatic recovery. If the validation window closes without rollback, a later rollback requires a reverse migration (Postgres → SQLite) which is explicitly out of scope for this plan.

## Phase 7: Remove SQLite runtime support

**Goal**: stop carrying dual-backend complexity once Postgres has proven stable in production.

### 7.1 Remove `SqliteHubDatabase` from runtime wiring [category: refactor] (depends: Phase 6)

Target: `src/gobby/storage/hub/__init__.py`, `src/gobby/runner.py`, `src/gobby/config/bootstrap.py`

- Delete the `SqliteHubDatabase` class and all import sites.
- Remove the `hub_backend` branch from `runner_init()`; Postgres becomes the only runtime path.
- Keep `hub_backend` as a bootstrap field for parse compatibility, but emit a warning when set to `sqlite` and then raise.
- Remove the `_pg_to_sqlite_params` shim and the placeholder-translation regex.
- Remove every manager branch of the form `if hub.dialect == "sqlite": ...`. After this task, `dialect` as a dispatch key is no longer used in runtime code (it survives in the migration tool, task 7.2).

### 7.2 Remove FTS5 runtime code and SQLite-specific migrations [category: refactor] (depends: 7.1)

Target: `src/gobby/storage/baseline_schema.sql`, FTS5-related migration files, `src/gobby/search/fts5.py`, `src/gobby/storage/tasks/_search.py`'s FTS5 branch

- Delete `baseline_schema.sql`.
- Delete every `migrations/*.sqlite.sql` that has a `.postgres.sql` counterpart.
- Delete `FTS5SearchBackend` and all `MATCH` / `bm25(...)` SQL.
- Keep only the code path required for `migrate-from-sqlite` to read a legacy SQLite database via stdlib `sqlite3`. That path does not touch Gobby's storage layer.

### 7.3 Update docs, comments, and user-facing text [category: docs] (depends: 7.2)

Target: `CLAUDE.md`, `README.md`, `docs/`, in-code comments referencing SQLite as the hub database

- Replace SQLite references with Postgres where they describe the hub database.
- Note that `gobby postgres migrate-from-sqlite` remains available for users importing legacy databases.
- Update the "Common Issues" table in CLAUDE.md.
- Update any "Key File Locations" tables that list `~/.gobby/gobby-hub.db` — after this phase the runtime hub is reached via Postgres `database_url` / bootstrap config, not a local file path.

## CLI and Interface Changes

New commands:

- `gobby postgres install`
- `gobby postgres status`
- `gobby postgres uninstall`
- `gobby postgres migrate-from-sqlite`
- `gobby postgres activate` / `deactivate`

Bootstrap fields:

- `hub_backend` (new; `sqlite` | `postgres`)
- `database_url` (new; psycopg v3 DSN)
- `database_path` (retained for SQLite import and short-window rollback; removed in Phase 7)

Env vars (new; Rust-portable):

- `DATABASE_URL` — test/CI DSN and optional explicit CLI override; not the normal runtime source of truth in this phase
- `PGPOOL_MIN`, `PGPOOL_MAX`, `PGCONNECT_TIMEOUT`, `PGAPPNAME` — pool/connection config
- `GOBBY_POSTGRES_DB`, `GOBBY_POSTGRES_USER`, `GOBBY_POSTGRES_PASSWORD`, `GOBBY_POSTGRES_PORT` — compose defaults

Type changes:

- `DatabaseProtocol` retired; `HubDatabase` protocol (`src/gobby/storage/hub/protocol.py`) is the new boundary
- row parsers consume `Mapping[str, Any]` instead of `sqlite3.Row`
- transaction objects expose `savepoint()` and `after_commit()` as methods; no implicit connection introspection
- search implementations dispatch via `pick_search_backend`; FTS5 direct calls removed in Phase 7

## Acceptance Criteria

- PostgreSQL installs via `gobby postgres install` with the same ergonomics as the Qdrant / Neo4j installers.
- Bootstrap selects the hub backend before DB-backed config loads; incorrect combinations are rejected with clear error messages.
- The daemon boots and runs against PostgreSQL without opening any SQLite file.
- `gobby postgres migrate-from-sqlite` imports an existing hub database with deterministic row-count, FK integrity, content-hash, and sequence reseed checks — all exit 0.
- Search behavior on tasks, memories, skills, and code index uses BM25 ranking on both SQLite (FTS5) and Postgres (pg_search). Representative-query top-N ordering matches across backends during the overlap and continues to return expected results post-cutover.
- `gobby/postgres:17-pgsearch` image is built, passes `CREATE EXTENSION pg_search` smoke tests, and is published to the Gobby image registry on release.
- The test suite runs against PostgreSQL via compose + `DATABASE_URL` with schema-per-xdist-worker plus test-scoped schema reset isolation; every Phase 3–5 task is covered by tests running against the real backend.
- All migration files are pure `.sql` (no Python callables). All parameter placeholders use `$1` style. Runtime DSN bootstrap is explicit and pool/connection tuning is env-var-driven.
- Fresh installs initialize PostgreSQL directly; `~/.gobby/gobby-hub.db` is not created.
- Phase 7 removes `SqliteHubDatabase`, FTS5 code paths, and related shims; the codebase has one runtime backend.

## Assumptions

- Scope is the full hub database, not a partial migration.
- PostgreSQL runs in the same compose project as Qdrant and Neo4j; each stays in its own container.
- Raw SQL remains the storage implementation style for this migration.
- There are no external users, so a cold cutover is preferable to dual-write rollout complexity.
- The compatibility layer (`SqliteHubDatabase`, dialect branches) is temporary scaffolding removed in Phase 7.
- Qdrant and Neo4j remain supporting stores; they are not replaced by PostgreSQL as part of this work.
- The Python codebase will be ported to Rust in a later effort. This plan biases toward choices that survive the port unchanged.

## Task Mapping

<!-- Updated after `/gobby expand <this-file>` creates tasks -->

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
