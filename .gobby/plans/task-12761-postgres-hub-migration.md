# PostgreSQL Migration for Hub Storage

## Overview
`kind: framing`

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
`kind: framing`

- **Driver**: psycopg v3 (sync + async on one driver; pairs with `psycopg_pool`). This is the key migration choice because existing synchronous storage call sites can keep running while new async paths are added incrementally on the same driver. Not psycopg2 (sync-only, no async path). Not asyncpg (async-only would force a broad refactor of every synchronous storage call site up front instead of letting the codebase adopt async gradually). This is a Python-phase optimization only: it lets the Python codebase adopt async incrementally without a big-bang refactor, but the later Rust port on `sqlx` is still async-only and still requires a full sync→async conversion. That is why the migration artifacts themselves must stay Rust-portable (`sqlx migrate run`-compatible SQL files, `$1` placeholders, env-var-driven connection tuning, bootstrap-sourced DSN).
- **Migration tool**: raw SQL files + a handwritten migration registry. No SQLAlchemy, no Alembic.
- **Test backend**: compose-managed PostgreSQL reached via `DATABASE_URL`. Schema-per-xdist-worker plus test-scoped schema reset for isolation. Zero language-specific test infra so the pattern survives the Rust port.
- **Rust portability**: migrations must be pure `.sql` files (no Python callables). Parameter style standardized on `$1`. Runtime DSN lives in bootstrap for now; pool and connection tuning are still env-driven (`PGPOOL_MIN`, `PGPOOL_MAX`, `PGCONNECT_TIMEOUT`, `PGAPPNAME`) rather than embedded in Python.
- **Search**: `pg_search` (ParadeDB) consumed as a PostgreSQL extension. In Docker mode, Gobby ships the build recipe (Dockerfile) and the user's machine produces a local image; in native mode, Gobby fetches the upstream `.deb` and runs it on the user's machine; in external mode, the operator pre-installs the extension. **Gobby never publishes a binary that bundles pg_search.** BM25 ranking via Tantivy. Rust-portable indexes. Semantic search (Qdrant) is an explicit non-goal of this plan but the search backend dispatcher leaves a clean seam for it.
- **Install modes**: three supported install paths — `docker` (recommended), `native` (Debian/Ubuntu first-class via upstream `.deb`; macOS and other Linux print "use Docker" guidance and exit), and `external` (BYO DSN against a pre-installed Postgres + pg_search). The user opts into a mode explicitly via `gobby postgres install --mode {docker,native,external}`; the installer never silently switches modes. Tier-1 testing covers `docker` and Debian `native`; `external` and macOS-with-source-built-pg_search are best-effort with documented runbooks.
- **Gobby Pro compatibility**: Gobby Pro is assumed to be a sync / fleet-management hub, not a hosted-Gobby service. This plan supports running against an external Postgres (including managed services like RDS, Cloud SQL, Aurora) via `--mode external` — but only as best-effort: Gobby is tested against locally-installed Postgres, and operators of managed services must install pg_search themselves before pointing Gobby at the DSN. Gobby always runs locally on the user's machine; Gobby Pro talks to Gobby instances via API, not by sharing a database. Gobby Pro's own datastore is out of scope for this plan.
- **Licensing**: pg_search is AGPL-3.0. **Gobby does not distribute pg_search.** Docker mode ships a Dockerfile (build recipe), not an image; native mode fetches the upstream `.deb` directly from ParadeDB releases at install time on the user's machine; external mode requires the operator to install pg_search themselves. Used by Gobby as a separate PostgreSQL extension running inside the Postgres server process, it is not linked into Gobby's application code and does not by itself propagate copyleft to Gobby. AGPL obligations would apply if we modified pg_search itself, distributed a modified pg_search binary, or incorporated pg_search code directly into Gobby — none of which the supported install paths do. If AGPL posture becomes unacceptable for a specific distribution channel, ParadeDB commercial licensing is an explicit fallback option. The rest of the runtime stack is PostgreSQL-license, Apache-2.0 (Qdrant), and LGPL-3.0 (psycopg v3) — all permissive from Gobby's perspective.
- **Bootstrap security posture**: `bootstrap.yaml` remains the pre-DB source of truth during the overlap window, but plaintext `database_url` storage does not survive cleanup. Phase 7 makes OS keyring / secret-store integration a hard dependency before the migration is considered fully complete. Until that lands, startup must enforce `0600` permissions on `~/.gobby/bootstrap.yaml` and fail closed if the file is broader than owner read/write. Operator docs must treat `bootstrap.yaml` as a secret-bearing credential file during the cutover window.
- **Rollback window**: short. Stop daemon, restore `hub_backend=sqlite`, restart. Rollback is a migration safety net, not a permanent product feature. Writes made during the post-cutover validation window are at risk on rollback; they must be captured for forensics, but they are not auto-merged back into SQLite. The validation-window write-capture mechanism is therefore a cutover gate, not a nice-to-have.

> Warning: `bootstrap.yaml` containing `database_url` is equivalent to storing the Postgres password in plaintext, just like today's `neo4j_password`. Treat that file as a secret-bearing credential file.

## Current Constraints in the Repo
`kind: framing`

This is not a connection-string swap. Key SQLite-specific coupling the migration must break:

- **Type surface**: `src/gobby/storage/database.py` exposes `sqlite3.Connection`, `sqlite3.Row`, and `sqlite3.Cursor` through `DatabaseProtocol` (lines 70–88). Consumers depend on `sqlite3.Row` semantics (`.keys()`, `.values()`).
- **Transaction semantics**: savepoint nesting uses `conn.in_transaction` (SQLite-only attribute, line 309). After-commit callbacks fire post-`COMMIT` but pre-snapshot-propagation — under PostgreSQL MVCC this will expose latent consistency bugs that SQLite's serialized writes hide.
- **Identity keys**: `cursor.lastrowid` used at `task_dependencies.py:73`, `task_affected_files.py` (2 sites), `workflow_audit.py:102`. No PostgreSQL equivalent — must use `RETURNING id`.
- **Upserts**: `INSERT OR IGNORE` (8 sites across `projects.py`, `session_tasks.py`, `migrations.py`, `sessions.py`, `pipelines.py`) and `INSERT OR REPLACE` (1 site at `agents.py:360`). Must be rewritten to `ON CONFLICT DO NOTHING / DO UPDATE SET`.
- **Schema primitives**: `AUTOINCREMENT` (17 sites), `datetime('now')` (60+ DEFAULT expressions), `json_extract(...)` / `json_set(...)` (17 sites), `PRAGMA foreign_keys=ON`, `PRAGMA query_only=ON` (test read-only enforcement).
- **Search**: FTS5 virtual tables with content-synced triggers on `tasks`, `memories`, `code_symbols`, `code_content`, `skills` (contentless). 12+ triggers keep virtual tables in sync. No abstraction — managers call FTS5 directly using `MATCH` and `bm25()`.
- **Migration runner**: `src/gobby/storage/migrations.py` reads `baseline_schema.sql` as a string and executes it via `for stmt in sql.strip().split(";"): conn.execute(stmt)`. The naive `;` split cannot cross FTS5 trigger bodies (`BEGIN ... END;`) or Postgres function bodies (`$$ ... $$`); FTS5 setup is therefore extracted into five Python helpers in `src/gobby/storage/migration_helpers.py` (`_setup_code_symbols_fts`, `_setup_code_content_fts`, `_setup_tasks_fts`, `_setup_skills_fts`, `_setup_memories_fts`) that the runner calls after the baseline transaction commits. Version tracking uses a `schema_version (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))` table. As of writing, `BASELINE_VERSION = 239`, `_MIN_MIGRATION_VERSION = 239`, and `migrations.py::MIGRATIONS` contains four post-baseline entries: `(240, "Add task delivery state tables", _apply_delivery_state_schema)`, `(241, "Add GitHub issue triage tables", _apply_github_triage_schema)`, `(242, "Add review anchor default planning stage", _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA)` (inline-SQL: an `INSERT OR IGNORE INTO task_type_default_stages` seed row for `review_anchor`), and `(243, "Clean stale config store keys", _apply_config_store_cleanup)` (a one-shot data cleanup that DELETEs deprecated keys and prefixes from `config_store`; no DDL effect). The runner's `_run_migration_list` always inserts the version row itself after the action runs (`migrations.py:367-376`), so any inline-SQL action that *also* inserts into `schema_version` would fail the v-row insert with a UNIQUE PK violation. The runner also rejects databases below `_MIN_MIGRATION_VERSION` before considering `MIGRATIONS` (`migrations.py:407-423`). **Phase 0 of this plan folds the DDL and seed effects of 240/241/242 into the baseline, bumps `BASELINE_VERSION = 244`, holds `_MIN_MIGRATION_VERSION = 239` (so existing v239 users still flow through their callable upgrade path), keeps the callables for 240, 241, and 242 in `MIGRATIONS` (so users at v239 can still reach v244 by chained migration), drops the v243 callable entirely (personal data cleanup; no DDL; fresh installs have nothing to clean), and adds a v244 no-op-callable marker that lets the runner record the v243→v244 (and v242→v244 etc.) bump without double-inserting the version row.** If additional post-baseline migrations land between adversary review and Phase 0 implementation, Phase 0 folds them into the same flatten and bumps the marker version to `current_max + 1`; DDL/seed migrations contribute their effects to baseline AND keep their callable in `MIGRATIONS` for the upgrade band, while data-cleanup-only migrations (like v243) are dropped from `MIGRATIONS` without baseline effect — fresh installs have nothing to clean, and existing user DBs that missed the window simply do not run that cleanup. Pre-baseline databases (current_version below `_MIN_MIGRATION_VERSION`) are explicitly unsupported and raise `MigrationUnsupportedError`.
- **Bootstrap**: `src/gobby/config/bootstrap.py` has only `database_path`. No backend selection before DB-backed config loads.
- **Test infrastructure**: `tests/conftest.py` uses `:memory:` SQLite exclusively. No Postgres fixtures. 11k+ tests, ~48 files under `tests/storage/`. Any Postgres-only bug cannot be caught until production today.
- **Timestamps**: all `created_at` / `updated_at` stored as ISO8601 text. Python adapters assume UTC and add tzinfo; the `datetime('now')` DEFAULT produces naive UTC text. Migration must preserve UTC and align on `TIMESTAMPTZ`.

## Post-flattening starting point
`kind: framing`

A prior flatten consolidated the SQLite migration chain into baseline v239 at `src/gobby/storage/baseline_schema.sql`. Four post-baseline migrations have since landed in `migrations.py::MIGRATIONS`:

- `(240, "Add task delivery state tables", _apply_delivery_state_schema)` — adds `task_delivery_campaigns`, `task_delivery_units`, and their indexes; drops legacy delivery-artifact columns from `task_artifacts`.
- `(241, "Add GitHub issue triage tables", _apply_github_triage_schema)` — adds `project_github_triage_configs`, `gh_triage_deliveries`, `gh_issues_triaged`, and their indexes.
- `(242, "Add review anchor default planning stage", _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA)` — inline-SQL seed: `INSERT OR IGNORE INTO task_type_default_stages (task_type, stage_name, position) VALUES ('review_anchor', 'planning', 0)`.
- `(243, "Clean stale config store keys", _apply_config_store_cleanup)` — data-cleanup-only callable. DELETEs deprecated keys and prefixes from `config_store`; no DDL effect; copies a small set of `logging.*` config rows to their `telemetry.*` successor keys before deleting the originals.

Phase 0 of this plan performs another flatten — folding the DDL and seed effects of 240/241/242 into the baseline, dropping v243's cleanup callable entirely (no DDL to fold; fresh installs have nothing to clean), bumping `BASELINE_VERSION = 244`, and adding a v244 no-op-callable marker so the runner records the version bump on already-initialized databases — so the Postgres work begins from a clean prerequisite state:

- `src/gobby/storage/baseline_schema.sql` — single source-of-truth DDL. After Phase 0, it also contains the delivery-state and GitHub-triage tables, plus the `review_anchor → planning` row in `task_type_default_stages`. Phase 4.2's translation has one file to port, not a chain to replay.
- `src/gobby/storage/migrations.py::MIGRATIONS` is **rewritten by Phase 0** to a four-entry compatibility band: `(240, _apply_delivery_state_schema)`, `(241, _apply_github_triage_schema)`, `(242, _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA)`, `(244, "Phase 0 flatten marker", _apply_phase0_flatten_marker)` — where `_apply_phase0_flatten_marker` is a `def f(db): pass` no-op callable. Why this shape: the runner's `_run_migration_list` always inserts the version row itself after the action runs (`migrations.py:367-376`), so the marker MUST be a callable (or no-op-equivalent SQL that does not touch `schema_version`); an inline `INSERT INTO schema_version` would conflict with the runner's automatic insert under the unique PK and abort the migration. Keeping callables for 240/241/242 lets users still at v239 (or anywhere in the v239–v243 band) flow through chained migration to v244 without resetting their database. The v243 callable (`_apply_config_store_cleanup`) is dropped entirely: it's a one-shot personal data cleanup with no DDL effect; fresh installs have nothing to clean; users still at v240–v242 simply skip it and reach v244 directly via the marker; users already at v243 (most of HEAD) carry no residue worth chasing. Phase 3.7 supersedes the in-Python list with `src/gobby/storage/migrations/NNN_name.sql` files and re-empties `MIGRATIONS`. **No new in-Python entries may be added to `MIGRATIONS` between Phase 0 and Phase 3.7** — any post-baseline SQLite migration that lands in that window must wait for the file-based runner. After Phase 3.7, the runner consumes only `src/gobby/storage/migrations/NNN_name.sql` files; inline-SQL and callable entries are linted out. **`_MIN_MIGRATION_VERSION` stays at 239** during Phase 0 — Phase 3.7 (and later) is the right place to advance the floor once the file-based runner ships.
- The `_apply_config_store_cleanup` helper and its `_STALE_CONFIG_STORE_EXACT_KEYS` / `_STALE_CONFIG_STORE_PREFIXES` constants are deleted by Phase 0 (v243 dropped). The `_apply_delivery_state_schema`, `_apply_github_triage_schema`, `_DELIVERY_STATE_SCHEMA`, `_GITHUB_TRIAGE_SCHEMA`, `_LEGACY_DELIVERY_ARTIFACT_COLUMNS`, and `_REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA` symbols **stay in `migrations.py`** as long as their entries remain in `MIGRATIONS` — they are deleted alongside `MIGRATIONS = []` in Phase 3.7 once the file-based runner ships. Only the five FTS5 setup helpers persist beyond that, and they are SQLite-only by construction — they die alongside FTS5 in Phase 7.2 and never need Postgres equivalents.
- Pre-Phase-0 SQLite databases at any version in `[239, 244)` are upgraded to v244 by running Gobby once on the post-Phase-0 build. The runner applies the appropriate subset of `[240, 241, 242, 244]` from `MIGRATIONS` based on `current_version`. `gobby postgres migrate-from-sqlite` reuses the same `schema_version` gate — sources below `_MIN_MIGRATION_VERSION` are rejected before import. Users currently at v239–v242 who would otherwise have run v243's `_apply_config_store_cleanup` skip that cleanup; their stale `config_store` keys remain inert until any future cleanup workstream addresses them — preserving the "personal cleanup misfiled as a migration" outcome rather than codifying it forever.
- **Synchronize with parallel work.** If additional post-baseline migrations land between adversary review and Phase 0 implementation, Phase 0 folds them into the same flatten and bumps the marker to `current_max + 1`. DDL and seed-data migrations contribute their effects to `baseline_schema.sql` AND keep their callables in `MIGRATIONS` so the upgrade band remains intact for users below the new ceiling. Data-cleanup-only callables (no DDL, just `DELETE`/`UPDATE` against existing rows) are dropped without a baseline equivalent — fresh installs have nothing to clean. The flatten captures whatever the current `MIGRATIONS` list contains at implementation time; the invariants are: (a) the post-Phase-0 baseline is the full schema as of the new version, (b) `MIGRATIONS` retains the callable upgrade band so users on prior versions still reach the new ceiling, (c) the marker entry is a no-op callable (never a `schema_version`-touching string), (d) data-cleanup-only callables are dropped.

## Target Architecture
`kind: framing`

### Service packaging
`kind: framing`

Three install modes; Docker is the recommended path. The other two exist so users who can't or won't run Docker — limited hardware, distro-level Postgres already in use, container-disabled environments — are not locked out.

| Mode | DSN source | pg_search install | Use case |
|------|-----------|-------------------|----------|
| **`docker`** (recommended) | Compose-managed; written to bootstrap by the installer | Bundled by the local-build Dockerfile; pulled from upstream ParadeDB at build time | Default. First-time users, dev machines that already use Docker. |
| **`native`** | User-running local Postgres; installer writes the DSN to bootstrap after probe | Debian/Ubuntu: installer fetches the same upstream `.deb` and runs `dpkg -i` (sudo). macOS / non-Debian Linux: installer prints platform-specific guidance and exits with a "use `--mode docker`" recommendation. | Devs already running native Postgres, lightweight machines that can't afford a Docker daemon. |
| **`external`** | User-supplied via `--dsn`; installer only writes bootstrap (plus the ownership sentinel) | Probed read-only via `SELECT 1 FROM pg_extension WHERE extname='pg_search'`; fails closed with a manual install command if missing. Gobby never runs `CREATE EXTENSION` on the operator's database. | Self-hosted team Postgres, devs tunneling to staging, managed Postgres where the operator pre-installed pg_search. |

#### Docker mode (recommended)
`kind: framing`

Add `postgres` to `src/gobby/data/docker-compose.services.yml`:

- **`build:`** directive pointing at `src/gobby/data/postgres-pgsearch/Dockerfile` (built locally on the user's machine; not pushed to any registry). Local image tag `gobby-postgres-local:17-pgsearch`.
- named volume `gobby_postgres_data`
- Compose profiles `postgres` and `all`
- `pg_isready` healthcheck
- env-backed defaults for `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`

**Why local build, not a published image.** pg_search is AGPL-3.0. Distributing a Gobby-published image that bundles pg_search would put Gobby on the hook as the AGPL distributor for pg_search binaries. By shipping a Dockerfile and using compose's `build:` directive, the user's machine pulls upstream artifacts directly from ParadeDB and produces a local image that never leaves their machine. Gobby ships a build recipe, not a binary; AGPL distribution responsibility stays with ParadeDB.

**Why not `paradedb/paradedb:latest`.** That image bundles pg_analytics and pg_cron (both unused by Gobby), expands footprint to ~2GB, and broadens attack surface. The local-build Dockerfile adds only pg_search on top of `postgres:17`, keeping footprint close to stock Postgres (~600MB) and keeping Gobby-controlled version pinning.

#### Native mode
`kind: framing`

Skips compose entirely. Installer detects platform and:

- **Debian/Ubuntu (x86_64 + arm64)**: fetches the same upstream pg_search `.deb` with the same SHA pin used by the Dockerfile, prompts for sudo, runs `dpkg -i`, and probes `CREATE EXTENSION pg_search` against a user-provided or auto-discovered DSN.
- **macOS (Apple Silicon + Intel)**: prints "macOS native pg_search isn't supported upstream — use `--mode docker` (recommended) or follow the manual source-build runbook at `docs/runbooks/postgres-native-macos.md`." Exits non-zero.
- **Other Linux (RHEL/Fedora/Arch/Alpine)**: prints the source-build steps (cargo-pgrx + Postgres headers) and exits non-zero with the same "use `--mode docker`" recommendation.

The installer never silently downgrades Docker → native or native → docker; the user opts into a mode explicitly.

#### External mode (BYO DSN)
`kind: framing`

`gobby postgres install --mode external --dsn <url>` skips compose, skips installer-side pg_search install, and writes only bootstrap fields plus the `gobby_install_ownership` sentinel after probes pass. The probe phase is **strictly read-only** (see "Ownership contract for external mode" below) — `SELECT 1 FROM pg_extension WHERE extname='pg_search'` verifies the operator pre-installed pg_search, and the install exits with the upstream install command for the URL's reported `version()` platform if missing. Gobby never runs `CREATE EXTENSION` on the operator's database; the operator is responsible for keeping the extension installed and Gobby's role is to refuse to start against a Postgres without it.

**Ownership contract for external mode.** External installs must point Gobby at a **dedicated database** that Gobby **alone owns**. The DSN's database must be empty at install time (only `public` schema, no user objects) and must remain dedicated to Gobby thereafter. Per-schema isolation against a shared host database is **not supported in v1** — the failed-import recovery story (§5.1) needs an unconditional `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` (or equivalent), which is only safe when nothing else lives in the database. Operators who want schema-level isolation against a shared host create a dedicated database for Gobby first; a future workstream may add `--schema` support if usage justifies it.

The installer enforces this in two phases. **The probe phase is strictly read-only** — no `CREATE`, no `INSERT`, no `CREATE EXTENSION` against the target — so a probe failure leaves the operator's database byte-identical to its pre-probe state. Only after every probe passes does the install phase run any writes (extension load, sentinel table, sentinel row).

**Probe phase (read-only, ordered):**

1. **Schema enumeration**: `SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') AND nspname NOT LIKE 'pg_temp_%' AND nspname NOT LIKE 'pg_toast_temp_%'`. The result must be exactly `{'public'}` — any additional user schema (`gobby`, `auth`, `apps`, etc.) means the database is shared and external mode refuses.
2. **`public`-schema emptiness**: query `pg_class`, `pg_proc`, and `pg_type` filtered to `pg_namespace.oid = 'public'::regnamespace` and exclude system-generated rows (extension-owned via `pg_depend`, array types, composite types of system tables). Any user table, view, sequence, function, custom type, domain, or aggregate fails the probe. `information_schema.tables` was used previously and was insufficient — it does not catch functions, custom types, or non-relation objects, which is exactly the surface that `DROP SCHEMA public CASCADE` would later destroy.
3. **`pg_search` already installed**: `SELECT 1 FROM pg_extension WHERE extname = 'pg_search'`. **Read-only check**, not `CREATE EXTENSION IF NOT EXISTS` — the latter is write-capable and would mutate the target on probe failure modes (e.g. partial install where the binary is absent but the extension catalog row exists). If `pg_search` is missing, refuse with the upstream install command for the URL's reported `version()` platform.
4. **`pgaudit` availability** (advisory, non-blocking): `SELECT name FROM pg_available_extensions WHERE name = 'pgaudit'`. Surface in install output so the operator knows whether `--capture-sink pgaudit-file` is feasible later, but do not refuse install — pgAudit is opt-in for native/external (see §6.0).

**Install phase (writes the target, only if probes passed):**

1. **Sentinel write**: create the `gobby_install_ownership` table and INSERT the singleton row described below.

Gobby does **not** create `pg_search` in external mode — the probe phase already verified the operator installed it. If the probe finds the extension missing, install exits with the upstream install command for the operator's platform; lazy-creating the extension would conflict with the read-only-probe-only invariant the recovery story depends on.

The install fails closed at any probe step. Recovery hint on probe failure points the operator at a fresh database: `CREATE DATABASE gobby_hub;` and re-run with `--dsn postgresql://.../gobby_hub`. Don't suggest cleanup of the existing target — Gobby has no business advising what's safe to delete from someone else's database.

After both phases:

  ```sql
  CREATE TABLE gobby_install_ownership (
      key          TEXT PRIMARY KEY DEFAULT 'singleton'
                       CHECK (key = 'singleton'),
      installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      gobby_version TEXT NOT NULL
  );
  INSERT INTO gobby_install_ownership (gobby_version) VALUES ($1);
  ```

  The `key` PK with the `'singleton'` check guarantees at most one row, so subsequent operations (`status`, `activate`, `migrate-from-sqlite`) can confirm the install is still Gobby-managed by looking for that exact row. If it is missing, `status` and `activate` both refuse with "external-ownership sentinel missing — was the database recreated or is this a different install?" §5.2 parity validation **excludes** `gobby_install_ownership` from the row-count and content-hash comparisons because it has no SQLite counterpart by design.

This contract makes external mode's recovery story coherent without requiring Gobby to inspect or fence the host Postgres beyond its own object footprint.

#### CLI surface
`kind: framing`

Mirrors existing Qdrant / Neo4j installers, with the new `--mode` / `--dsn` flags:

- `gobby postgres install [--mode {docker,native,external}] [--dsn <url>]` — default `docker`
- `gobby postgres status` — reports active mode + extension presence
- `gobby postgres uninstall` — Docker mode tears down the compose profile and offers volume deletion; native mode prints the manual uninstall steps; external mode is a no-op against the database (only clears bootstrap fields)
- `gobby postgres migrate-from-sqlite`
- `gobby postgres activate` / `deactivate`

### Runtime database model
`kind: framing`

PostgreSQL is the only runtime hub database after cutover. SQLite is retained temporarily for migration input and rollback. SQLite is not a permanent fallback after Phase 7.

### Bootstrap and configuration
`kind: framing`

Backend selection must happen before DB-backed config is available. Bootstrap-level fields:

- `hub_backend`: `"sqlite"` or `"postgres"`
- `database_url`: psycopg v3 DSN used when `hub_backend=postgres`
- `database_path`: legacy SQLite path used when `hub_backend=sqlite` and during one-shot import
- `postgres_install_mode`: `"docker"`, `"native"`, or `"external"` — recorded by the installer so `uninstall` and `status` know which teardown path to use; absent until `gobby postgres install` runs

Rules:

- bootstrap decides which backend to open
- runtime startup must not infer the backend from DB-stored config (chicken-and-egg)
- after cutover, normal startup uses `hub_backend=postgres`
- after Phase 7 cleanup, `database_path` exists only for import tooling
- during the overlap window, `database_url` is stored directly in `bootstrap.yaml`; startup must check that file is `0600` and fail closed otherwise, and Phase 7 replaces inline storage with an OS keyring-backed reference before cleanup is considered done

There is no `database_shadow_url`. No shadow-write phase.

### Storage compatibility layer
`kind: framing`

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
`kind: framing`

Replace FTS5 with `pg_search` (ParadeDB, BM25 via Tantivy):

- `CREATE INDEX ... USING bm25` on content columns (tasks.title/description, memories.content/tags, code_symbols.name/body, code_content.content, skills.description/content).
- Query construction uses the `@@@` operator with pg_search's query DSL and the legacy `pdb.score(<key_field>)` scoring expression (see §4.4 for the version-pin contract): `WHERE title @@@ $1 ORDER BY pdb.score(id) DESC LIMIT $2`.
- `pg_trgm` (ships standard) remains available for trigram fuzzy matches where needed.

Ranking is BM25. This gives parity with the existing FTS5 `bm25()` ordering — user-visible search behavior does not regress during the migration. Phase 2.4 parity tests assert representative-query ordering matches across SQLite-FTS5 and Postgres-pg_search.

Search routes through `pick_search_backend(hub, table, mode)` for task, memory, skill, and code-index search. `mode` is a forward-compatible parameter: today the only value is `"keyword"` and dispatches to `BM25SearchBackend` (on Postgres) or `FTS5SearchBackend` (on SQLite during overlap). A future workstream adds `"semantic"` mode dispatching to a `QdrantSearchBackend`; this plan deliberately does not build that — the seam is the deliverable, not the implementation.

#### Hybrid / fused search seam
`kind: framing`

The current fused search behavior does not live in the Python daemon today; it
primarily lives in the Rust `gcode` search path, where keyword search, vector
search, and Neo4j relevance boosting are combined before final ranking. This
plan must not accidentally break that shape by introducing a keyword-only
Python seam.

Migration intent:

- preserve the existing Reciprocal Rank Fusion (RRF) behavior rather than
  deprecating it during the PostgreSQL migration
- keep `pick_search_backend(...)` as the dispatcher for concrete backends such
  as `BM25SearchBackend`, `FTS5SearchBackend`, `QdrantSearchBackend`, and a
  potential `Neo4jSearchBackend`
- compose those into a fused layer (`FusedSearchBackend` or equivalent
  orchestrator) so keyword, vector, and graph-boost signals still merge
  through RRF

Neo4j graph relevance boosting should remain explicit, either:

- as a `Neo4jSearchBackend` whose scores participate in fusion, or
- as a pre-fusion boost applied to keyword/vector candidates before final RRF

Rust search migration note: the Rust-side search modules that currently perform
keyword search, vector search, and Neo4j graph boost need to be ported or
wrapped against this seam, not replaced with a Postgres-only keyword path.

`tsvector` + GIN was considered as a simpler, core-Postgres alternative and rejected: it loses BM25 ranking parity with FTS5 (ordering shifts become migration noise users will feel), and it does not pay off until you leave pg_search's operational envelope — which Gobby doesn't (it runs locally, not on managed Postgres).

### Test architecture
`kind: framing`

- `docker compose -f docker-compose.test.yml up -d --build postgres-test` before running the suite (local). CI does **not** use a GitHub Actions `services:` container spec — service containers can only pull registry images, and Gobby never publishes a pg_search-bearing image (AGPL posture, see §1.4). Instead, CI runs explicit `docker build` + `docker run` steps before the test job, exactly mirroring the local build path. See §2.1 for the canonical CI snippet.
- both local compose and CI start the container with `command: postgres -c shared_preload_libraries=pg_search,pgaudit` so the preload contract is identical to the runtime install path (§1.4 + §6.0). The `command:` is set in `docker-compose.test.yml` and replicated in CI's `docker run`.
- tests read `DATABASE_URL` from env; psycopg v3 connects
- session-scoped pytest fixture creates a unique schema per xdist worker: `gobby_test_<worker_id>_<session_nonce>`
- migrations apply once per worker schema; per-test isolation is a schema reset (`TRUNCATE ... RESTART IDENTITY CASCADE`), not an outer savepoint
- no language-specific test infra (e.g. testcontainers-python). Compose + env vars port unchanged to the future Rust phase using `sqlx`
- `PG_SEARCH_VERSION` and `PG_SEARCH_SHA256` come from a single checked-in manifest at `src/gobby/data/postgres-pgsearch/version.json` (consumed by §1.1 compose, §1.2 native installer, §1.4 Dockerfile, §2.1 test compose / CI, and §6.0 pgAudit setup). Bumping pg_search means editing one file; CI's pg_search smoke tests gate the bump.

### Gobby Pro compatibility
`kind: framing`

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

## P0 Phase 0: Re-flatten SQLite baseline
`kind: framing`

**Goal**: fold the DDL and seed effects of migrations 240/241/242 into the SQLite baseline, drop the v243 config-store cleanup callable, keep the v240/v241/v242 callables in `MIGRATIONS` as the v239→v244 upgrade band so existing user databases are not stranded, add a callable v244 no-op flatten marker so the runner records the version row without double-inserting, bump `BASELINE_VERSION = 244`, and hold `_MIN_MIGRATION_VERSION = 239` until Phase 3.7's file-based runner can safely advance the floor. The Postgres work starts from a clean single-source-of-truth `baseline_schema.sql` while the in-Python `MIGRATIONS` retains the four-entry compatibility band described in §0.1; Phase 3.7 supersedes that list with `src/gobby/storage/migrations/NNN_name.sql` files and re-empties `MIGRATIONS`. If additional post-baseline migrations land before Phase 0 implementation, Phase 0 folds DDL/seed into baseline AND keeps their callables in the band; data-cleanup-only callables (like v243) are dropped without baseline equivalent.

This phase is a hard prerequisite gate. Phase 1 cannot start until Phase 0 lands and ships in a release; the post-Phase-0 baseline is what Phase 4.2's translator reads.

### 0.1 Fold migrations 240–242 DDL/seed into the SQLite baseline; drop v243; add v244 no-op marker [category: refactor]
`kind: deliverable`

Target: `src/gobby/storage/baseline_schema.sql`, `src/gobby/storage/migrations.py`, `tests/storage/test_phase0_flatten.py` (new)

Steps:

1. **Apply the schema and seed-data additions to baseline DDL.** In `baseline_schema.sql`, fold the effects currently produced by migrations 240, 241, and 242:
    - From migration 240 (`_DELIVERY_STATE_SCHEMA` in `migrations.py`): add `task_delivery_campaigns` and `task_delivery_units` table definitions, plus the `idx_task_delivery_units_task_id` and `idx_task_delivery_units_pr_url` indexes. Migration 240's `_apply_delivery_state_schema` also drops legacy delivery-artifact columns from `task_artifacts` (`pr_url`, `merge_commit_sha`, `pr_review_report`, `structured_pr_verdict`, `merge_campaign_report`); since the baseline starts from a clean schema, those columns must not appear on `task_artifacts` in the baseline at all.
    - From migration 241 (`_GITHUB_TRIAGE_SCHEMA` in `migrations.py`): add `project_github_triage_configs`, `gh_triage_deliveries`, and `gh_issues_triaged` table definitions, plus the `idx_gh_triage_deliveries_project_status`, `idx_gh_triage_deliveries_issue`, `idx_gh_issues_triaged_project_hash`, and `idx_gh_issues_triaged_task` indexes.
    - From migration 242 (`_REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA` in `migrations.py`): seed the `review_anchor → planning` row directly in baseline. Add `INSERT INTO task_type_default_stages (task_type, stage_name, position) VALUES ('review_anchor', 'planning', 0);` to the baseline's seed-data section alongside the other `task_type_default_stages` seed rows. Drop the `OR IGNORE` modifier — the baseline starts from a clean `task_type_default_stages` table; conflict is a real bug, not a tolerable noise.
    - Migration 243 (`_apply_config_store_cleanup`) contributes nothing to baseline. It's a one-shot data cleanup against `config_store`; fresh installs have no rows to clean. The function and its `_STALE_CONFIG_STORE_EXACT_KEYS` / `_STALE_CONFIG_STORE_PREFIXES` constants are deleted in step 2 below; no baseline change is needed.
    - Strip the `IF NOT EXISTS` clauses on the folded `CREATE TABLE` / `CREATE INDEX` statements — the baseline starts from an empty database and IF-NOT-EXISTS noise hides genuine schema conflicts.
2. **Restructure `MIGRATIONS` as a four-entry compatibility band.** In `migrations.py`, replace the `MIGRATIONS` list contents so it contains exactly four entries — the existing v240/v241/v242 callables, plus a new v244 no-op-callable marker:
    ```python
    def _apply_phase0_flatten_marker(db: LocalDatabase) -> None:
        """No-op v244 marker for Phase 0.

        The runner inserts the schema_version row itself after this callable
        returns; the body is intentionally empty. Users at v240–v242 chain
        through their respective callables and then this marker to reach v244.
        Users at v243 (post-cleanup HEAD) hit only this marker. Fresh installs
        skip the entire chain — they enter at BASELINE_VERSION = 244 via
        _apply_baseline.

        This must remain a callable (or no-op-equivalent SQL that does not
        touch schema_version). An inline 'INSERT INTO schema_version' would
        conflict under the unique PK with the runner's automatic insert at
        migrations.py:367-376 and abort the migration.
        """


    MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
        (240, "Add task delivery state tables", _apply_delivery_state_schema),
        (241, "Add GitHub issue triage tables", _apply_github_triage_schema),
        (242, "Add review anchor default planning stage", _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA),
        (244, "Phase 0 flatten marker", _apply_phase0_flatten_marker),
    ]
    ```
   In the same edit, delete the `_apply_config_store_cleanup` function and its `_STALE_CONFIG_STORE_EXACT_KEYS` / `_STALE_CONFIG_STORE_PREFIXES` constants — v243 is dropped from the chain entirely. **Keep** `_apply_delivery_state_schema`, `_apply_github_triage_schema`, `_DELIVERY_STATE_SCHEMA`, `_GITHUB_TRIAGE_SCHEMA`, `_LEGACY_DELIVERY_ARTIFACT_COLUMNS`, and `_REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA` — they remain in the upgrade band for users at v239 (or anywhere in the v239–v243 band) and are deleted in Phase 3.7 alongside `MIGRATIONS = []`. Keep the `MigrationAction` type alias and the runner docstring. **No additional entries may be added to `MIGRATIONS` between Phase 0 and Phase 3.7** — any post-baseline SQLite migration that lands in that window must wait for the file-based runner. Phase 3.7 supersedes this list with on-disk SQL files (canonical path defined there) and re-empties `MIGRATIONS`; it also widens the action type once the file-based runner lands.

   **Why callable, not inline SQL, for the marker.** The runner's `_run_migration_list` (`migrations.py:337-386`) wraps each migration in a transaction and unconditionally executes `INSERT INTO schema_version (version) VALUES (?)` with the migration's version after the action runs. For string actions, this happens *after* every statement parsed out of the inline SQL has executed; for callable actions, it happens after `action(db)` returns. An inline-SQL marker like `"INSERT INTO schema_version(version) VALUES (244) ON CONFLICT DO NOTHING"` would insert the row first, then collide with the runner's insert under the unique PK and abort the migration. A no-op callable lets the runner do the insert on its own — single insert, no collision, schema_version reaches 244 cleanly. (The same constraint applies to any future marker entry: keep them callable, or use SQL that does not touch `schema_version`.)

   **Synchronize with parallel work**: if additional post-baseline migrations have landed in `MIGRATIONS` by the time this Phase 0 task is implemented, fold them into the same flatten and bump the marker version to `current_max + 1`. DDL and seed-data migrations contribute their effects to `baseline_schema.sql` AND keep their callables in `MIGRATIONS` (so users on prior versions still reach the new ceiling). Data-cleanup-only callables (no DDL, just `DELETE`/`UPDATE` against existing rows) are dropped without a baseline equivalent — fresh installs have nothing to clean, and existing user DBs that missed the cleanup window simply do not run that cleanup. The flatten captures whatever the current `MIGRATIONS` list contains at implementation time; the invariants are: (a) post-Phase-0 baseline is the full schema as of the new ceiling, (b) `MIGRATIONS` retains the callable upgrade band, (c) the marker is a no-op callable, (d) data-cleanup-only callables are dropped.
3. **Bump the version constant.** In `migrations.py`, set `BASELINE_VERSION = 244`. Leave `_MIN_MIGRATION_VERSION = BASELINE_VERSION` **unchanged in form** — the existing source line `_MIN_MIGRATION_VERSION = BASELINE_VERSION` (`migrations.py:60`) currently binds it to 239. Replace that line with the explicit literal `_MIN_MIGRATION_VERSION = 239` to decouple the two, since after Phase 0 the upgrade band starts at v239 even though the baseline ships at v244. Update the constants' docstrings/comments to reflect "Phase 0 flatten — folds delivery-state (v240), GitHub-triage (v241), and review-anchor default-stage (v242) migrations into baseline; drops the v243 config-store cleanup callable as personal-only data hygiene; v240/v241/v242 callables remain in MIGRATIONS as the upgrade band; `_MIN_MIGRATION_VERSION` stays at 239 until Phase 3.7 (which advances the floor with the file-based runner)."
4. **Schema fingerprint check.** Add a one-shot test at `tests/storage/test_phase0_flatten.py::test_baseline_fingerprint_parity` that:
    - applies the pre-Phase-0 chain (v239 baseline + migrations 240, 241, 242) to a fresh in-memory SQLite database. Migration 243 is intentionally excluded: it is data-cleanup-only against rows that don't exist in a fresh DB, so including it adds noise to the comparison without changing the fingerprint outcome.
    - applies the post-Phase-0 baseline to a separate fresh in-memory SQLite database
    - asserts `sqlite_master` rows match exactly across both (table DDL, index DDL, trigger DDL, view DDL — order-independent comparison via sorted set of `(type, name, sql)` tuples)
    - asserts `task_type_default_stages` rows match exactly across both (so the v242 seed row is verified as folded)
    - asserts `schema_version` ends at 244 in the post-Phase-0 case

   It is a regression guard — once the baseline is updated and the test passes, the test stays in the suite as protection against future drift.
5. **User-database upgrade-band test.** Add `tests/storage/test_phase0_flatten.py::test_upgrade_band` that verifies the runner correctly walks each entry in the band:
    - For each starting version in `{239, 240, 241, 242, 243, 244}`, build a fresh SQLite DB seeded with the appropriate prior-state DDL (v239 baseline ± the corresponding callable applications) and `schema_version` set to that version.
    - Run `migrations.run_migrations(db)`.
    - Assert: final `schema_version = 244`; expected migrations applied (subset of `[240, 241, 242, 244]` based on starting version — e.g., v239 applies all four; v243 applies only the v244 marker; v244 applies nothing); table set matches the post-Phase-0 baseline; the v243 cleanup is **not** resurrected on the v243 fixture (its stale `config_store` rows remain inert).
    - Below `_MIN_MIGRATION_VERSION` (anything < 239): assert `MigrationUnsupportedError` is raised with a help message pointing the user at the prior gobby release.
6. **Verification on existing databases.** Run the daemon manually against the same six fixture versions used in step 5 (v239 / v240 / v241 / v242 / v243 / v244) checked into the test tree. Confirm all six daemons start cleanly, reach `schema_version = 244` (except v244 which stays put), and pass smoke checks: no DDL conflicts during migration, no spurious rewrites of `task_delivery_*` / `gh_*` data, no rows dropped from `task_artifacts`, and (on the v243 fixture) no resurrection of the dropped cleanup pass. This is a one-shot manual validation; the automated coverage lives in step 5's test.

Acceptance: the schema fingerprint test passes; fresh installs initialize at v244; users at any version in `[239, 244)` upgrade cleanly to v244 by chained migration through the band; v244 users are no-op; users below `_MIN_MIGRATION_VERSION = 239` are rejected with a clear `MigrationUnsupportedError`; `MIGRATIONS` contains exactly the four band entries (`240`, `241`, `242`, `244` no-op marker) until Phase 3.7 supersedes it; the v244 marker is a callable (never an inline `schema_version` write).

**Acceptance:**

- 0.1.1 — Migrations 240 (delivery-state DDL), 241 (GitHub-triage DDL), and 242 (review-anchor default-stage seed) are folded into the SQLite baseline so a fresh install initializes at v244 with the full schema; migration 243 (config-store cleanup) is dropped without a baseline equivalent. file: `src/gobby/storage/baseline_schema.sql`.
- 0.1.2 — `BASELINE_VERSION = 244`, `_MIN_MIGRATION_VERSION = 239`, and `MIGRATIONS` contains exactly four entries: `(240, _apply_delivery_state_schema)`, `(241, _apply_github_triage_schema)`, `(242, _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA)`, and `(244, "Phase 0 flatten marker", _apply_phase0_flatten_marker)` — where the v244 entry is a no-op callable, not an inline `INSERT INTO schema_version` (which would collide with the runner's automatic version-row insert under the unique PK). file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations._apply_phase0_flatten_marker`.
- 0.1.3 — Schema fingerprint test verifies that the pre-Phase-0 chain (v239 baseline + 240 + 241 + 242) and the post-Phase-0 baseline produce identical `sqlite_master` and `task_type_default_stages` content. test: `tests/storage/test_phase0_flatten.py::test_baseline_fingerprint_parity`.
- 0.1.4 — Upgrade-band test verifies that user databases at any version in `{239, 240, 241, 242, 243, 244}` reach `schema_version = 244` after `run_migrations`, applying only the subset of `[240, 241, 242, 244]` that exceeds their current version, without resurrecting the v243 config-store cleanup; databases below 239 raise `MigrationUnsupportedError`. test: `tests/storage/test_phase0_flatten.py::test_upgrade_band`.

## P1 Phase 1: PostgreSQL service and bootstrap support
`kind: framing`

**Goal**: PostgreSQL runs as a first-class local service and bootstrap can select it before DB-backed config loads.

### 1.1 Add `postgres` service to compose template [category: config] (depends: P0)
`kind: deliverable`

Target: `src/gobby/data/docker-compose.services.yml`

Add a `postgres` service alongside the existing `qdrant` and `neo4j` services. Compose uses a local-build `build:` directive so no Gobby-published image is required (see Service packaging > Docker mode for the licensing rationale):

```yaml
services:
  postgres:
    build:
      context: ./postgres-pgsearch
      args:
        PG_SEARCH_VERSION: ${GOBBY_PG_SEARCH_VERSION:-0.17.0}
        PG_SEARCH_SHA256: ${GOBBY_PG_SEARCH_SHA256}
    image: gobby-postgres-local:17-pgsearch  # local tag only — never pushed
    container_name: gobby-postgres
    profiles: ["postgres", "all"]
    command:
      - postgres
      - -c
      - shared_preload_libraries=pg_search,pgaudit
      - -c
      - pgaudit.log=write
    environment:
      POSTGRES_DB: ${GOBBY_POSTGRES_DB:-gobby}
      POSTGRES_USER: ${GOBBY_POSTGRES_USER:-gobby}
      POSTGRES_PASSWORD: ${GOBBY_POSTGRES_PASSWORD:-gobby_dev}
    ports:
      - "${GOBBY_POSTGRES_PORT:-60891}:5432"
    volumes:
      - gobby_postgres_data:/var/lib/postgresql/data
      - gobby_pgaudit_log:/var/log/pgaudit
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${GOBBY_POSTGRES_USER:-gobby}"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  gobby_postgres_data:
  gobby_pgaudit_log:
```

`PG_SEARCH_VERSION` and `PG_SEARCH_SHA256` defaults are read from the checked-in pg_search version manifest under `src/gobby/data/postgres-pgsearch/` (single source of truth shared with §1.2 native installer, §1.4 Dockerfile, §2.1 test compose/CI, and §6.0 pgAudit setup). The manifest itself is owned and shipped by §1.4 — see §1.4 acceptance for path and schema. Compose loads the values via a `.env` shim that the installer regenerates from the manifest, so users running raw `docker compose up` without the installer still get a deterministic build. The Dockerfile body and version-manifest schema/values are task 1.4 — §1.1 consumes them but does not author them.

Verification (this section): static template checks only — YAML parses; `postgres` service block validates against the compose schema; image tag is exactly `gobby-postgres-local:17-pgsearch`; the `build:` directive points at `./postgres-pgsearch` with `PG_SEARCH_VERSION` and `PG_SEARCH_SHA256` build args; profiles list contains `postgres` and `all`; healthcheck and named volume are present; the `command:` line preloads both `pg_search` and `pgaudit`. **Build/runtime smoke** (`docker compose --profile postgres up -d postgres`, `pg_isready`, `CREATE EXTENSION`) is deferred to §1.4's CI smoke job, which is what builds the actual image — it cannot be exercised here because the Dockerfile and the version manifest do not exist until §1.4 lands.

**Acceptance:**

- 1.1.1 — `postgres` service block added to the compose template alongside qdrant/neo4j; static schema/template assertions pass; build/runtime smoke deferred to §1.4 CI. file: `src/gobby/data/docker-compose.services.yml`.

### 1.2 Add PostgreSQL installer, uninstaller, and status CLI [category: code] (depends: 1.1, 1.4)
`kind: deliverable`

Target: `src/gobby/cli/installers/postgres.py` (new), `src/gobby/cli/postgres.py` (new Click group), `src/gobby/cli/__init__.py`, `~/.gobby/bootstrap.yaml`, `~/.gobby/services/docker-compose.yml`, `~/.gobby/services/postgres-pgsearch/` (asset tree synced from `src/gobby/data/postgres-pgsearch/`), `~/.gobby/services/.env`, `docs/runbooks/postgres-native-macos.md` (new), `docs/runbooks/postgres-native-source.md` (new)

Mirror the functional pattern used by `src/gobby/cli/installers/qdrant.py` and `src/gobby/cli/installers/neo4j.py`. Do not invent a new installer base class. The installer dispatches by mode and writes `database_url` plus related defaults into `~/.gobby/bootstrap.yaml`. `hub_backend` stays `sqlite` until explicit activation regardless of mode.

Mode dispatch:

| Mode | Action |
|------|--------|
| `docker` (default) | Bring up compose profile (`docker compose --profile postgres up -d`), wait for `pg_isready`, probe `CREATE EXTENSION IF NOT EXISTS pg_search`, write bootstrap defaults including `database_url` pointing at `localhost:${GOBBY_POSTGRES_PORT}`. |
| `native` | Detect platform. Debian/Ubuntu: fetch upstream pg_search `.deb` (same SHA pin used in task 1.1's compose `args`), prompt for sudo, run `dpkg -i`, probe `CREATE EXTENSION pg_search` against `--dsn` (or auto-discovered local DSN if omitted), write bootstrap. macOS / non-Debian Linux: print platform-specific guidance referencing `docs/runbooks/postgres-native-macos.md` (macOS) or `docs/runbooks/postgres-native-source.md` (other Linux) and exit non-zero with a clear "use `--mode docker`" recommendation. |
| `external` | Skip compose, skip pg_search install. Require `--dsn`. Run the read-only probe phase from the "Ownership contract for external mode" section: `pg_namespace` + `pg_class` / `pg_proc` / `pg_type` ownership probe, `SELECT 1 FROM pg_extension WHERE extname='pg_search'` (read-only — Gobby never runs `CREATE EXTENSION` here), `pg_available_extensions` advisory check for pgaudit. Failures exit non-zero with the platform-specific upstream install command (for missing pg_search) or the dedicated-database hint (for ownership-probe failures). On all probes passing, run the write phase: create the `gobby_install_ownership` sentinel and write bootstrap. |

```python
# src/gobby/cli/installers/postgres.py
from typing import Literal

InstallMode = Literal["docker", "native", "external"]


def install_postgres(
    *,
    mode: InstallMode = "docker",
    dsn: str | None = None,
    gobby_home: Path | None = None,
    port: int = 60891,
) -> dict[str, Any]:
    """Dispatches on mode. Returns a structured result for rendering."""
    if mode == "docker":
        return _install_docker(gobby_home=gobby_home, port=port)
    if mode == "native":
        return _install_native(gobby_home=gobby_home, dsn=dsn)
    if mode == "external":
        if not dsn:
            raise click.ClickException("--mode external requires --dsn")
        return _install_external(gobby_home=gobby_home, dsn=dsn)
    raise click.ClickException(f"Unknown install mode: {mode}")


def _install_docker(*, gobby_home, port):
    compose_file = _ensure_unified_compose(...)
    # _sync_postgres_pgsearch_assets copies the full build context tree from
    # importlib.resources("gobby.data.postgres-pgsearch") into
    # ~/.gobby/services/postgres-pgsearch/ — Dockerfile, version.json, the
    # initdb.d/ directory (01-pg_search.sql, 02-pgaudit.sql), and the
    # scripts/ directory (pg_audit_export.sh). The compose file's
    # `build.context: ./postgres-pgsearch` is repo-root-relative for
    # development AND user-install layouts because both place the
    # postgres-pgsearch/ tree alongside the compose file.
    _sync_postgres_pgsearch_assets(gobby_home)
    # _write_compose_env reads version.json once and emits
    # ~/.gobby/services/.env with GOBBY_PG_SEARCH_VERSION=<version> and
    # GOBBY_PG_SEARCH_SHA256=<sha256>. The compose file references those
    # vars via ${GOBBY_PG_SEARCH_VERSION:-...}; the .env shim is what makes
    # raw `docker compose -f ~/.gobby/services/docker-compose.yml --profile
    # postgres up -d` deterministic without bespoke env exports.
    _write_compose_env(gobby_home)
    # docker compose --profile postgres up -d --remove-orphans (builds locally on first run)
    # wait for pg_isready
    # probe CREATE EXTENSION IF NOT EXISTS pg_search
    # write bootstrap defaults including database_url
    ...


def _install_native(*, gobby_home, dsn):
    platform = _detect_platform()
    if platform.os == "linux" and platform.distro in ("debian", "ubuntu"):
        return _install_native_debian(gobby_home=gobby_home, dsn=dsn)
    if platform.os == "darwin":
        raise click.ClickException(
            "macOS native pg_search is not supported upstream. "
            "Use `gobby postgres install --mode docker` (recommended), "
            "or follow the manual source-build runbook at "
            "docs/runbooks/postgres-native-macos.md, then re-run with --mode external."
        )
    raise click.ClickException(
        f"Native install on {platform.distro} requires building pg_search from source. "
        f"See docs/runbooks/postgres-native-source.md, or use `--mode docker` (recommended)."
    )


def _install_external(*, gobby_home, dsn):
    # READ-ONLY PROBE PHASE — no writes against the target until every probe
    # passes. See "Ownership contract for external mode" above for the full
    # rationale; the order below mirrors the prose.
    #
    # 1. Schema enumeration: pg_namespace must yield exactly {'public'} after
    #    excluding pg_catalog / information_schema / pg_toast / pg_temp_*.
    # 2. public-schema emptiness: pg_class + pg_proc + pg_type filtered to
    #    public, excluding extension-owned (via pg_depend) and system-generated
    #    rows. Any user table/view/sequence/function/type/domain refuses.
    # 3. pg_search presence: SELECT 1 FROM pg_extension WHERE extname='pg_search'.
    #    Read-only — do NOT use CREATE EXTENSION IF NOT EXISTS here. On miss,
    #    format upstream install command from server version() and exit.
    # 4. pgaudit availability (advisory): pg_available_extensions row presence.
    #    Surface in install output; do not refuse.
    #
    # WRITE PHASE — only after every probe passed. Gobby does NOT create
    # pg_search in external mode; the probe phase already verified the
    # operator installed it. Lazy-creating the extension would conflict
    # with the read-only-probe-only invariant the recovery story depends on.
    # 5. CREATE TABLE gobby_install_ownership (...) and INSERT the singleton
    #    row.
    # 6. Write bootstrap.yaml with database_url=<dsn>.
    ...


def uninstall_postgres(
    *,
    mode: InstallMode = "docker",
    gobby_home: Path | None = None,
    remove_data: bool = False,
) -> dict[str, Any]:
    if mode == "docker":
        # docker compose --profile postgres down
        # if remove_data: docker volume rm gobby_postgres_data gobby_pgaudit_log
        ...
    elif mode == "native":
        # print manual uninstall steps; do not run apt-get remove without explicit confirmation
        # if remove_data: also print the manual data-directory deletion step
        ...
    elif mode == "external":
        # no-op against the database server; clear bootstrap fields only.
        # if remove_data: refuse — external mode never deletes server-side data
        # (the dedicated-database ownership contract from §1.2 means Gobby
        # owns the whole database, and we still won't drop it for the
        # operator). Direct the operator at the §5.1 dedicated-database
        # recovery commands instead — `DROP SCHEMA public CASCADE;
        # CREATE SCHEMA public;` inside the dedicated database, or drop and
        # recreate the database itself.
        ...
    ...


async def get_postgres_status(...) -> dict[str, Any]:
    # Report a structured status payload with all of the following fields. The
    # field names are stable so §6.1 step 6 can grep / parse them as the
    # pre-cutover gate, and so the rollback runbook (§6.2) can read them post-
    # cutover.
    #
    # {
    #   "mode": "docker" | "native" | "external",
    #   "dsn_host": "localhost",          # never the password
    #   "dsn_db":   "gobby",
    #   "healthy":  bool,                 # pg_isready
    #   "extensions": {
    #       "pg_search": bool,
    #       "pgaudit":   bool,            # docker mode only — gates the cutover
    #   },
    #   "preload_libraries": ["pg_search", "pgaudit"],  # parsed from pg_settings
    #   "migration_complete": {
    #       "present": bool,              # SELECT 1 FROM gobby_migration_state
    #                                     #   WHERE key = 'imported_from_sqlite_at'
    #       "imported_at": "..." | null,  # value if present, ISO 8601
    #   },
    #   "ownership": {                    # external mode only; absent in docker/native
    #       "sentinel_present": bool,     # gobby_install_ownership singleton row
    #       "installed_at": "..." | null,
    #       "gobby_version": "..." | null,
    #   },
    # }
    #
    # §6.1 step 6 blocks cutover until migration_complete.present is true.
    # External-mode operations (`activate`, `migrate-from-sqlite`) refuse if
    # ownership.sentinel_present is false ("external-ownership sentinel
    # missing — was the database recreated or is this a different install?").
    ...
```

CLI wiring in `src/gobby/cli/postgres.py`:

```python
import asyncio

import click

@click.group("postgres")
def postgres_cli() -> None:
    """Manage the local PostgreSQL hub database."""

@postgres_cli.command("install")
@click.option(
    "--mode",
    type=click.Choice(["docker", "native", "external"]),
    default="docker",
    show_default=True,
    help="Install mode. docker is recommended.",
)
@click.option(
    "--dsn",
    default=None,
    help="psycopg DSN. Required for --mode external; optional for --mode native.",
)
def install_cmd(mode: str, dsn: str | None) -> None:
    result = install_postgres(mode=mode, dsn=dsn)
    _render_install_result(result)

@postgres_cli.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=(
        "Emit the status payload as JSON on stdout (the exact schema "
        "documented on get_postgres_status() above). Default output is "
        "human-readable text. The cutover runbook (§6.1) uses --json."
    ),
)
def status_cmd(as_json: bool) -> None:
    payload = asyncio.run(get_postgres_status())
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(render_postgres_status(payload))

@postgres_cli.command("uninstall")
@click.option(
    "--remove-data",
    is_flag=True,
    default=False,
    help=(
        "Docker mode: also delete the gobby_postgres_data and "
        "gobby_pgaudit_log named volumes. Native mode: print the manual "
        "data-directory deletion steps. External mode: refuses — Gobby "
        "never deletes server-side data on external installs. Reset the "
        "dedicated database with the §5.1 commands instead "
        "(`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` inside the "
        "dedicated database, or drop and recreate the database)."
    ),
)
def uninstall_cmd(remove_data: bool) -> None:
    result = uninstall_postgres(mode=_active_install_mode(), remove_data=remove_data)
    _render_uninstall_result(result)
```

Register the group in `src/gobby/cli/__init__.py`. The `_active_install_mode()` helper reads the mode that was used at install time (recorded in `bootstrap.yaml` as `postgres_install_mode`) so uninstall does not require the user to remember.

**Acceptance:**

- 1.2.1 — Docker-mode install action implemented per mode-dispatch table row 1: `docker compose --profile postgres up -d`, `pg_isready` wait, `CREATE EXTENSION IF NOT EXISTS pg_search` probe, write bootstrap defaults including `database_url=postgresql://...localhost:${GOBBY_POSTGRES_PORT}/gobby`. symbol: `gobby.cli.installers.postgres._install_docker`.
- 1.2.2 — Native-mode install action implemented per mode-dispatch table row 2: Debian/Ubuntu auto-install via upstream pg_search `.deb` (SHA-pinned to §1.1's compose args), sudo prompt + `dpkg -i`, `CREATE EXTENSION pg_search` probe against `--dsn` or auto-discovered local DSN, bootstrap write; macOS / non-Debian Linux refuse with platform-specific runbook pointer (`docs/runbooks/postgres-native-macos.md` / `postgres-native-source.md`) and "use `--mode docker`" recommendation. symbol: `gobby.cli.installers.postgres._install_native`.
- 1.2.3 — External-mode install action implemented per mode-dispatch table row 3: read-only probe phase (`pg_namespace` enumeration, `pg_class`/`pg_proc`/`pg_type` ownership filter, `SELECT 1 FROM pg_extension WHERE extname='pg_search'` read-only check, `pg_available_extensions` advisory pgaudit check), then write phase (`CREATE TABLE gobby_install_ownership` + singleton row insert + bootstrap write). Refuses to run `CREATE EXTENSION` against the operator's database. symbol: `gobby.cli.installers.postgres._install_external`.
- 1.2.4 — `gobby postgres install`, `uninstall`, and `status` CLI commands wired under the `postgres` Click group, registered in `src/gobby/cli/__init__.py`. symbol: `gobby.cli.postgres.postgres_cli`.
- 1.2.5 — `get_postgres_status()` returns the structured status payload with stable field names for §6.1 step 6's pre-cutover gate and §6.2's rollback runbook. symbol: `gobby.cli.installers.postgres.get_postgres_status`.
- 1.2.6 — Docker-mode install copies the full `src/gobby/data/postgres-pgsearch/` asset tree (Dockerfile, `version.json`, `initdb.d/`, `scripts/`) into `~/.gobby/services/postgres-pgsearch/` and emits a `.env` shim at `~/.gobby/services/.env` with `GOBBY_PG_SEARCH_VERSION` and `GOBBY_PG_SEARCH_SHA256` resolved from `version.json`, so a bare `docker compose -f ~/.gobby/services/docker-compose.yml --profile postgres up -d` from the user's install resolves the build context, image args, and initdb scripts without falling back to repo-relative paths. symbol: `gobby.cli.installers.postgres._sync_postgres_pgsearch_assets`.

### 1.3 Extend bootstrap config with `hub_backend`, `database_url`, and `postgres_install_mode` [category: code] (depends: 1.2, 3.3, 3.8)
`kind: deliverable`

Target: `src/gobby/config/bootstrap.py`, `~/.gobby/bootstrap.yaml` schema, `src/gobby/runner_init.py` (functions `init_hub_database` and `init_storage_and_config`)

Add three fields to `BootstrapConfig`:

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class BootstrapConfig:
    database_path: str = "~/.gobby/gobby-hub.db"

    hub_backend: Literal["sqlite", "postgres"] = "sqlite"
    database_url: str | None = None  # psycopg v3 DSN; required when hub_backend=postgres
    postgres_install_mode: Literal["docker", "native", "external"] | None = None
```

Validation:

- `hub_backend="postgres"` requires `database_url` non-empty
- `hub_backend="sqlite"` tolerates `database_url=None` and `postgres_install_mode=None`
- `postgres_install_mode` is set by `gobby postgres install` and is `None` until then; bootstrap parsing accepts it absent
- `database_url` is stored verbatim in `bootstrap.yaml` only during the overlap window; require `bootstrap.yaml` to be owner read/write only (`0600`), fail startup if that check fails, and complete the OS keyring migration in Phase 7 before cleanup is considered done
- parse errors raise `BootstrapConfigError` with field-level messages

Update `init_hub_database` (and `init_storage_and_config` if it duplicates the wiring) in `src/gobby/runner_init.py` to branch on `hub_backend` when constructing the hub database. Do not allow DB-stored config to override bootstrap-level backend selection. `postgres_install_mode` is read by `gobby postgres uninstall` and `gobby postgres status` — runtime startup itself does not branch on install mode (the DSN already encodes everything the runtime needs).

**Acceptance:**

- 1.3.1 — Bootstrap config extended with `hub_backend`, `database_url`, and `postgres_install_mode`. symbol: `gobby.config.bootstrap.BootstrapConfig`.

### 1.4 Add local-build Dockerfile for the Docker mode [category: config] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/data/postgres-pgsearch/Dockerfile` (new), `src/gobby/data/postgres-pgsearch/version.json` (new — single source of truth for `PG_SEARCH_VERSION` + `PG_SEARCH_SHA256`, consumed by §1.1 compose, §1.2 installer, §2.1 test compose/CI, and §6.0 pgAudit setup), CI smoke-test workflow

Ship a Dockerfile that builds locally on the user's machine via the compose `build:` directive in task 1.1. **No registry push, no Gobby-published image, no GHCR.** The Dockerfile is a build recipe; the resulting image is local to the user and never leaves their machine. This keeps Gobby off the AGPL-distribution hook for pg_search — see "Why local build, not a published image" in the Service packaging section.

```dockerfile
# src/gobby/data/postgres-pgsearch/Dockerfile
FROM postgres:17

# pg_search version + SHA come from src/gobby/data/postgres-pgsearch/version.json
# via --build-arg; build fails if either is missing (intentional — no silent
# fallback to a stale pin).
ARG PG_SEARCH_VERSION
ARG PG_SEARCH_SHA256

RUN test -n "$PG_SEARCH_VERSION" && test -n "$PG_SEARCH_SHA256" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg postgresql-17-pgaudit \
    && curl -fsSL "https://github.com/paradedb/paradedb/releases/download/v${PG_SEARCH_VERSION}/pg_search-v${PG_SEARCH_VERSION}-pg17-$(dpkg --print-architecture)-ubuntu2204.deb" \
        -o /tmp/pg_search.deb \
    && echo "${PG_SEARCH_SHA256}  /tmp/pg_search.deb" | sha256sum -c - \
    && dpkg -i /tmp/pg_search.deb \
    && rm /tmp/pg_search.deb \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

```

`postgresql-17-pgaudit` is included in the same `apt-get install` line because §6.0 requires pgAudit alongside pg_search in the Docker image; one extension layer keeps the build cache hot. The `test -n` guards mean the build fails fast if `version.json` wasn't piped in via build args.

The Dockerfile must pin an exact pg_search release artifact and checksum in the repo. Updating pg_search is a deliberate version bump, not "whatever ParadeDB ships today." Requirements:

- Tag locally as `gobby-postgres-local:17-pgsearch` via the compose `build:` directive's `image:` clause. **Never push to a registry** — not GHCR, not Docker Hub, not Gobby's image registry. Distribution stays with ParadeDB upstream.
- Start PostgreSQL with the **canonical command line** shared across §1.1 (compose), §2.1 (test compose / CI), §6.0 (pgAudit setup), and this section: `postgres -c shared_preload_libraries=pg_search,pgaudit -c pgaudit.log=write`. The preload list **must include both** `pg_search` and `pgaudit`; pgAudit is required by §6.0 and the entire Docker-mode cutover gate depends on it being live at boot. Drift on this line means the Dockerfile produces an image that the runbook's pgAudit healthcheck cannot pass — exactly the bug the local-build choice exists to prevent.
- CI builds the Dockerfile on every PR that touches `src/gobby/data/postgres-pgsearch/` and runs four smoke tests against the resulting image, **all of which must pass**:
  1. `pg_isready` returns healthy.
  2. `CREATE EXTENSION pg_search;` succeeds.
  3. `CREATE EXTENSION pgaudit;` succeeds.
  4. `SELECT setting FROM pg_settings WHERE name = 'shared_preload_libraries';` returns a value containing both `pg_search` and `pgaudit`.

  After the smoke checks the image is discarded — build-only verification; no `docker push`.
- Bump `PG_SEARCH_VERSION` and `PG_SEARCH_SHA256` together. The PR template for pg_search bumps requires (a) the SHA verified against the upstream release artifact and (b) a green CI run against the Postgres smoke/search test suite.
- Security checklist: monitor `pg_search` / ParadeDB advisories through GitHub security alerts or CVE feeds so future bumps are tracked intentionally.
- The same SHA pin is used by task 1.2's native-Debian/Ubuntu installer when fetching the upstream `.deb` directly. Single source of truth for "which pg_search version Gobby supports right now."

License notice (AGPL-3.0 for pg_search, PostgreSQL license for Postgres) is preserved in the locally built image via `/usr/share/doc/pg_search/copyright` already present in the upstream `.deb`. Because Gobby never distributes the resulting image, AGPL distribution obligations stay with ParadeDB.

**Acceptance:**

- 1.4.1 — Local-build Dockerfile for the `postgres-pgsearch` image lands in the data tree, builds against pinned `PG_SEARCH_VERSION` + `PG_SEARCH_SHA256` build args, and passes the four CI smoke tests (`pg_isready`, `CREATE EXTENSION pg_search`, `CREATE EXTENSION pgaudit`, `shared_preload_libraries` includes both). file: `src/gobby/data/postgres-pgsearch/Dockerfile`.
- 1.4.2 — `version.json` manifest commits the canonical `pg_search_version` and `pg_search_sha256` values used by §1.1 compose, §1.2 installer, §2.1 test compose/CI, and §6.0 pgAudit setup. The schema is `{"pg_search_version": "<semver>", "pg_search_sha256": "<hex>", "postgres_major": "17"}`; build args fail-fast if either field is missing. file: `src/gobby/data/postgres-pgsearch/version.json`.

### 1.5 Add `gobby postgres activate` and `deactivate` commands [category: code] (depends: 1.3, 1.4)
`kind: deliverable`

Target: `src/gobby/cli/postgres.py`

`activate` flips `hub_backend` from `sqlite` to `postgres` in `~/.gobby/bootstrap.yaml`. `deactivate` flips it back. Guardrails:

- refuse if the daemon is running (require `gobby stop` first)
- refuse if migration has not yet produced the canonical completion marker — the `imported_from_sqlite_at` row in the Postgres `gobby_migration_state` table (created by §4.2's baseline; written by §5.1 step 12). `schema_migrations` is reserved for applied migration versions and must not carry completion markers; activation reads `gobby_migration_state` only.
- write a dated backup of `bootstrap.yaml` before rewriting
- print the exact rollback command on success

The command itself is the enforcement point for migration completion. Ship it in Phase 1 if useful, but `gobby postgres activate` must perform the runtime validation above and fail clearly until Phase 5 has completed successfully.

```python
@postgres_cli.command("activate")
@click.option(
    "--capture-sink",
    default=None,
    metavar="TYPE:LOCATION",
    help=(
        "Native/external mode only. Declares the operator-wired write-capture "
        "sink. TYPE must be exactly 'pgaudit-file' or 'wal-archive'. "
        "Mutually exclusive with --accept-no-rollback-risk."
    ),
)
@click.option(
    "--accept-no-rollback-risk",
    is_flag=True,
    default=False,
    help=(
        "Native/external mode only. Acknowledges that no validation-window "
        "writes will be auto-captured; rollback will rely on the pre-cutover "
        "SQLite backup. Requires typing the confirmation phrase. Mutually "
        "exclusive with --capture-sink."
    ),
)
def activate_cmd(capture_sink: str | None, accept_no_rollback_risk: bool) -> None:
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    if not _postgres_migration_complete():  # SELECT 1 FROM gobby_migration_state WHERE key = 'imported_from_sqlite_at'
        raise click.ClickException("Run `gobby postgres migrate-from-sqlite` first")
    mode = _active_install_mode()
    if mode == "external":
        _require_ownership_sentinel_or_fail()  # SELECT 1 FROM gobby_install_ownership WHERE key='singleton'
    ticket: dict[str, Any]
    if mode == "docker":
        if capture_sink or accept_no_rollback_risk:
            raise click.ClickException(
                "Capture flags are not applicable in docker mode; pgAudit is the gate."
            )
        probe = _probe_pgaudit_or_fail()  # blocks if pgaudit not loaded or audit log not writable
        ticket = _build_cutover_ticket(
            mode=mode,
            capture_kind="pgaudit-managed",
            capture_value=None,
            verification={"state": "ok", "probe_detail": probe},
        )
    else:  # native / external
        if bool(capture_sink) == bool(accept_no_rollback_risk):
            raise click.ClickException(
                "Native/external mode requires exactly one of "
                "--capture-sink or --accept-no-rollback-risk."
            )
        if capture_sink:
            kind, _, location = capture_sink.partition(":")
            if kind not in {"pgaudit-file", "wal-archive"}:
                raise click.ClickException(
                    f"Unknown capture-sink type {kind!r}. "
                    f"Expected pgaudit-file or wal-archive."
                )
            probe = _probe_capture_sink_or_fail(kind, location)
            ticket = _build_cutover_ticket(
                mode=mode,
                capture_kind=kind,
                capture_value=location,
                verification={"state": "ok", "probe_detail": probe},
            )
        else:
            ack = _require_typed_acknowledgement("I accept no-rollback risk")
            ticket = _build_cutover_ticket(
                mode=mode,
                capture_kind="none",
                capture_value=None,
                verification={"state": "operator-attested", "probe_detail": None},
                acknowledgement=ack,
            )
    # ACTIVATION INVARIANT (load-bearing): hub_backend=postgres in bootstrap
    # exists if and only if the canonical cutover ticket exists at the path
    # echoed below. §6.1 / §6.2 treat ticket presence as the authoritative
    # "activation went live" signal, so leaving hub_backend=postgres without
    # the ticket would break the rollback-export selector and the validation-
    # window deadline contract.
    #
    # Order: pre-render ticket → backup → flip → publish-canonical →
    # rollback-on-publish-failure. If _write_cutover_ticket() fails after the
    # bootstrap flip (disk full, permission error, signal between
    # tmp-write and os.replace()), restore the bootstrap from the backup we
    # just took and re-raise so the operator sees a clean failure with
    # hub_backend=sqlite intact. A crash strictly between flip and the
    # try/except entry can still leave the invariant violated, but
    # _write_cutover_ticket()'s tmp-then-os.replace() pattern keeps that
    # window to a few syscalls; the rollback handles the realistic failure
    # modes (I/O errors, permission errors).
    backup_path = _backup_bootstrap()
    _set_bootstrap_field("hub_backend", "postgres")
    try:
        _write_cutover_ticket(ticket)  # ~/.gobby/migrations/cutover-<timestamp>.json
    except Exception:
        # Restore bootstrap so hub_backend goes back to sqlite. The next
        # gobby start will use sqlite, matching the no-canonical-ticket
        # state. Re-raise so the operator sees the original error and can
        # diagnose (disk full, permissions, etc.) before retrying activate.
        _restore_bootstrap(backup_path)
        raise
    click.echo("hub_backend set to postgres. To roll back:")
    click.echo("  gobby stop && gobby postgres deactivate && gobby start")
    click.echo(f"Cutover ticket: {ticket['_path']}")
    click.echo(f"Validation-window deadline: {ticket['deadline_at']}")
```

**Cutover-ticket artifact contract (full schema)**: `_build_cutover_ticket()` produces a dict matching the JSON schema below; `_write_cutover_ticket()` serializes it to `~/.gobby/migrations/cutover-<timestamp>.json` with `indent=2, sort_keys=True`. **All fields below are required** unless explicitly marked optional. §6.1 step 11 reads `deadline_at`; §6.2 step 2 reads `capture_kind`, `capture_value`, `activated_at`, and `acknowledgement` (when present). §6.0 describes the same schema for cross-reference; this section is the authoritative source.

```json
{
  "mode": "docker" | "native" | "external",
  "activated_at":   "<ISO 8601 UTC, second precision>",
  "deadline_at":    "<ISO 8601 UTC, activated_at + 48h>",
  "gobby_version":  "<semver from gobby.__version__>",
  "capture_kind":   "pgaudit-managed" | "pgaudit-file" | "wal-archive" | "none",
  "capture_value":  "<absolute path | dsn-style spec | null>",
  "verification": {
    "state":       "ok" | "operator-attested",
    "probed_at":   "<ISO 8601 UTC | null>",
    "probe_detail": "<JSON-serializable result from the writability probe | null>"
  },
  "acknowledgement": {                           // present only when capture_kind = "none"
    "phrase":     "I accept no-rollback risk",
    "operator":   "<whoami output, stripped>",
    "asked_at":   "<ISO 8601 UTC>"
  }
}
```

Mapping from `_build_cutover_ticket()` parameters to artifact fields (this is documentation of one helper's contract, not a manifest of separate work items):

- `mode` → `mode`: passed verbatim.
- (always emitted) → `activated_at`: `datetime.now(UTC).isoformat(timespec='seconds')`.
- (always emitted) → `deadline_at`: `activated_at + timedelta(hours=48)`.
- (always emitted) → `gobby_version`: `gobby.__version__`.
- `capture_kind` → `capture_kind`: one of the four enum values; rejected otherwise.
- `capture_value` → `capture_value`: required when `capture_kind` ∈ {pgaudit-file, wal-archive}; null otherwise.
- `verification` → `verification`: dict with `state`, `probed_at`, `probe_detail`; producer must set all three (probed_at = now() when `state="ok"`, null when `state="operator-attested"`).
- `acknowledgement` → `acknowledgement`: required when `capture_kind="none"`; rejected otherwise.

`_write_cutover_ticket()` writes the ticket via temp-file + atomic rename: it serializes to `<canonical_path>.tmp`, calls `os.replace()` to swap it to the canonical path, and only then injects `_path` (absolute path it wrote to) into the in-memory dict so the activator can echo it. The on-disk JSON does **not** include `_path`. Atomic publish is load-bearing for the cutover-ticket invariant: §6.1 / §6.2 treat the artifact's existence at the canonical path as proof the activation went live, so a half-written or pre-flip ticket would produce false-positive cutover state.

The activation invariant is "hub_backend=postgres in bootstrap exists if and only if the canonical ticket exists at the echoed path." `activate_cmd` enforces this with: pre-render ticket → backup bootstrap → flip → publish-canonical, with a try/except around publish that restores bootstrap from the backup if `_write_cutover_ticket()` fails. A crash mid-rename is impossible by `os.replace()`'s POSIX atomicity guarantee. A crash strictly between the flip and the try/except entry is the only window where the invariant could be violated, and `os.replace()`'s tmp-then-rename keeps that window to a few syscalls — recoverable by the operator running `gobby postgres deactivate` if observed. The realistic failure modes (I/O errors, permission errors during ticket write) are caught explicitly. `_restore_bootstrap(backup_path)` reverses the file copy `_backup_bootstrap()` produced.

```python

@postgres_cli.command("deactivate")
def deactivate_cmd() -> None:
    if _daemon_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    _backup_bootstrap()
    _set_bootstrap_field("hub_backend", "sqlite")
```

**Acceptance:**

- 1.5.1 — `gobby postgres activate` and `deactivate` CLI commands flip the runtime backend selection atomically. symbol: `gobby.cli.postgres`.

## P2 Phase 2: Test infrastructure for dual-backend work
`kind: framing`

**Goal**: stand up the PostgreSQL-side test infrastructure that the rest of the migration validates against. §2.1 (compose stack + CI) lands early as a Phase 1 dependent and is consumable by Phase 3 once it exists. §2.2 (`postgres_db` fixture), §2.3 (dual-backend `hub_db` fixture), and §2.4 (dialect-parity tests) require the Phase 3 adapter foundation (§§3.1, 3.2, 3.3) to import their target classes — they therefore depend forward into Phase 3 and land alongside the row-consumer / placeholder / upsert refactors that complete the parity work. Phase 2 is the test-infra spine; Phase 3 is the code spine; their leaves interleave by deliverable rather than by phase number.

### 2.1 Add PostgreSQL to the test compose stack and CI [category: config] (depends: 1.1, 1.4)
`kind: deliverable`

Target: `docker-compose.test.yml` (new), `.github/workflows/ci.yml`, `pyproject.toml`

Both the local test compose file and the CI job consume the **same `Dockerfile` from §1.4** via local build. **Never reference a published `gobby/postgres:*` image** — Gobby does not publish one (AGPL posture, see §1.4 and Service packaging > Docker mode). The local-build approach is what keeps test/CI consistent with the install path users actually exercise.

Add a test-scoped compose file exposing Postgres on a distinct port (60892) so it does not clash with a dev instance:

```yaml
services:
  postgres-test:
    build:
      context: ./src/gobby/data/postgres-pgsearch
      args:
        PG_SEARCH_VERSION: ${GOBBY_PG_SEARCH_VERSION:-0.17.0}
        PG_SEARCH_SHA256: ${GOBBY_PG_SEARCH_SHA256}
    image: gobby-postgres-local:17-pgsearch  # local tag only — never pushed
    command:
      - postgres
      - -c
      - shared_preload_libraries=pg_search,pgaudit
      - -c
      - pgaudit.log=write
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

The `command:` is identical to §1.1's runtime compose so local tests, CI, and the Docker install path all start Postgres with the same preload list. Drift on this line means tests pass against a different runtime than users get — exactly the bug the local-build choice exists to prevent.

Local invocation: `docker compose -f docker-compose.test.yml up -d --build postgres-test`. The `--build` flag is non-negotiable — the compose `image:` clause is just the local tag; without `--build` on first run there is no image to load.

The `tmpfs` entry at `/var/lib/postgresql/data` makes the PostgreSQL test container fully ephemeral. Tests cannot rely on data surviving `docker compose` restarts, CI workflows that restart services mid-run must recreate or reseed the database, and this is intentionally different from the persistent-volume configuration used in dev/prod.

CI cannot use a `services:` container spec because GitHub Actions service containers only accept registry-pulled images, and Gobby never publishes one. Instead, the CI job builds the image as an explicit step before tests run:

```yaml
jobs:
  tests:
    runs-on: ubuntu-latest
    env:
      GOBBY_PG_SEARCH_VERSION: 0.17.0
      GOBBY_PG_SEARCH_SHA256: ${{ vars.GOBBY_PG_SEARCH_SHA256 }}
      DATABASE_URL: postgresql://gobby_test:gobby_test@localhost:60892/gobby_test
    steps:
      - uses: actions/checkout@v4
      - name: Build Postgres test image
        run: |
          docker build \
            --build-arg PG_SEARCH_VERSION="${GOBBY_PG_SEARCH_VERSION}" \
            --build-arg PG_SEARCH_SHA256="${GOBBY_PG_SEARCH_SHA256}" \
            -t gobby-postgres-local:17-pgsearch \
            src/gobby/data/postgres-pgsearch
      - name: Start Postgres test container
        run: |
          docker run -d --name postgres-test \
            -e POSTGRES_DB=gobby_test \
            -e POSTGRES_USER=gobby_test \
            -e POSTGRES_PASSWORD=gobby_test \
            -p 60892:5432 \
            --tmpfs /var/lib/postgresql/data \
            --health-cmd "pg_isready -U gobby_test" \
            --health-interval 2s \
            gobby-postgres-local:17-pgsearch \
            postgres -c shared_preload_libraries=pg_search,pgaudit -c pgaudit.log=write
          # Wait for healthy
          for _ in $(seq 1 30); do
            [ "$(docker inspect -f '{{.State.Health.Status}}' postgres-test)" = "healthy" ] && break
            sleep 2
          done
      # ... checkout, uv sync, pytest steps follow
```

`PG_SEARCH_VERSION` and `PG_SEARCH_SHA256` are read from `src/gobby/data/postgres-pgsearch/version.json`, the single checked-in manifest shared with §1.1 (compose), §1.2 (native installer), §1.4 (Dockerfile), §6.0 (pgAudit setup), and this task. The CI workflow loads them via a `jq` step (e.g. `jq -r '.pg_search_version' src/gobby/data/postgres-pgsearch/version.json`) at the top of the job; bumping pg_search means editing one file and pushing the bump through the existing smoke-test gate.

Add `psycopg[binary,pool]>=3.2` and `pytest-xdist` to `pyproject.toml`. Confirm the local test runner can reach the compose-provided `DATABASE_URL`.

**Acceptance:**

- 2.1.1 — PostgreSQL added to the test compose stack and CI workflow. file: `.github/workflows/ci.yml`.

### 2.2 Add a schema-per-worker pytest fixture [category: test] (depends: 2.1, 3.3)
`kind: deliverable`

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
            try:
                conn.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
                )
            except Exception:
                logger.exception("Failed to drop orphaned schema %s", schema_name)


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


# Tables whose bookkeeping rows must survive reset verbatim. We never touch
# their contents — they are written once by apply_migrations() and never
# mutated by tests through the standard fixture path.
_BOOKKEEPING_TABLES: frozenset[str] = frozenset({"schema_migrations"})

# Static seed rows that fresh PostgresHubDatabase.apply_migrations() writes;
# tests can ADD rows to these tables, so we can't TRUNCATE them, but we also
# can't leave test-created rows behind. Reset deletes everything NOT in the
# canonical seed and leaves the canonical seed exactly as fresh-baseline
# produces it. The canonical seed is loaded once from the worker's freshly
# applied schema and cached for subsequent resets.
_SEED_BEARING_TABLES: frozenset[str] = frozenset({
    "projects",                  # _orphaned / _migrated / _personal / _global placeholders
    "task_type_default_stages",  # default stage manifests including review_anchor → planning
    "gobby_migration_state",     # cutover/import marker keys (empty in fresh baseline)
})


def _capture_canonical_seed(
    conn: psycopg.Connection,
) -> dict[str, list[tuple[Any, ...]]]:
    """Snapshot the seed rows of every _SEED_BEARING_TABLES table from a fresh
    apply_migrations() schema. Called once per worker before any test runs."""
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    for table in _SEED_BEARING_TABLES:
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
        ).fetchall()
        snapshot[table] = [tuple(r) for r in rows]
    return snapshot


def _reset_schema(
    url: str,
    schema: str,
    canonical_seed: dict[str, list[tuple[Any, ...]]],
) -> None:
    """Reset mutable rows in the worker schema while restoring canonical seed.

    Algorithm per table:
      - _BOOKKEEPING_TABLES: untouched (rows are write-once invariants).
      - _SEED_BEARING_TABLES: TRUNCATE then re-INSERT the canonical seed.
        Restores test-mutations of placeholder rows and removes any
        test-created rows beyond the seed (e.g., a project a test created).
      - all other application tables: TRUNCATE … RESTART IDENTITY CASCADE.

    After this call the schema is byte-for-byte equivalent to the state
    PostgresHubDatabase.apply_migrations() produces on a fresh schema —
    that's the invariant acceptance 2.2.3 enforces.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {schema}")
        all_tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
        }
        seed_tables = all_tables & _SEED_BEARING_TABLES
        truncate_tables = all_tables - _BOOKKEEPING_TABLES - _SEED_BEARING_TABLES

        with conn.transaction():
            if truncate_tables:
                joined = ", ".join(sorted(truncate_tables))
                conn.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")
            for table in sorted(seed_tables):
                conn.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.Identifier(table)
                    )
                )
                rows = canonical_seed.get(table, [])
                if rows:
                    placeholders = ", ".join(["%s"] * len(rows[0]))
                    conn.executemany(
                        sql.SQL("INSERT INTO {} VALUES ({})").format(
                            sql.Identifier(table), sql.SQL(placeholders)
                        ),
                        rows,
                    )


@pytest.fixture(scope="session")
def postgres_canonical_seed(postgres_schema: str) -> dict[str, list[tuple[Any, ...]]]:
    """One-time per-worker capture of fresh-baseline seed rows.

    Runs `PostgresHubDatabase.apply_migrations()` once against the worker
    schema, opens a connection with that schema on `search_path`, and snapshots
    every `_SEED_BEARING_TABLES` table. The snapshot is the source of truth
    for `_reset_schema`'s re-INSERT step on every per-test reset.
    """
    url = os.environ["DATABASE_URL"]
    db = PostgresHubDatabase(url + f"?options=-csearch_path%3D{postgres_schema}")
    db.apply_migrations()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {postgres_schema}")
        return _capture_canonical_seed(conn)


@pytest.fixture
def postgres_db(
    postgres_schema: str,
    postgres_canonical_seed: dict[str, list[tuple[Any, ...]]],
) -> Iterator[PostgresHubDatabase]:
    """Per-test hub database over a reset worker schema.

    Both entry and exit resets receive the same canonical seed snapshot
    captured once per worker by `postgres_canonical_seed`.
    """
    url = os.environ["DATABASE_URL"] + f"?options=-csearch_path%3D{postgres_schema}"
    db = PostgresHubDatabase(url)
    # apply_migrations is already idempotent; the session-scoped seed fixture
    # ran it before this test was created. Calling again is a no-op.
    db.apply_migrations()
    _reset_schema(os.environ["DATABASE_URL"], postgres_schema, postgres_canonical_seed)
    try:
        yield db
    finally:
        _reset_schema(os.environ["DATABASE_URL"], postgres_schema, postgres_canonical_seed)
```

The `Any` import comes from `typing`; add `from typing import Any` at the top of `tests/fixtures/postgres.py` alongside the existing `from collections.abc import Iterator`.

The reset-based approach is intentional: a single outer savepoint is not sufficient once the runtime uses pooled connections, because work can commit on a different connection and bypass that savepoint entirely. Resetting the worker schema gives real isolation without constraining production code to a single test-only connection model.

**Acceptance:**

- 2.2.1 — Per-worker `postgres_schema` session fixture creates `gobby_test_<epoch>_<pid>_<worker>_<nonce>` schemas, drops them on teardown, and sweeps aged orphans on startup. Per-test `postgres_db` fixture wires `PostgresHubDatabase` against the worker's schema with `TRUNCATE … RESTART IDENTITY CASCADE` reset on entry and exit. symbol: `tests.fixtures.postgres.postgres_schema`.
- 2.2.2 — `postgres_db` per-test fixture yields a `PostgresHubDatabase` scoped to the worker schema with reset semantics suitable for pooled connections. symbol: `tests.fixtures.postgres.postgres_db`.
- 2.2.3 — Session-scoped `postgres_canonical_seed` fixture runs `PostgresHubDatabase.apply_migrations()` once per worker and snapshots `_SEED_BEARING_TABLES` via `_capture_canonical_seed`. Per-test `postgres_db(postgres_schema, postgres_canonical_seed)` passes that snapshot to `_reset_schema` on both entry and exit. `_reset_schema` restores each test to a state byte-for-byte equivalent to a fresh `apply_migrations()` schema by (a) leaving `_BOOKKEEPING_TABLES` (`schema_migrations`) untouched, (b) TRUNCATE-ing each `_SEED_BEARING_TABLES` table (`projects`, `task_type_default_stages`, `gobby_migration_state`) and re-inserting the canonical seed snapshot, (c) TRUNCATE-ing all other application tables. Test inserts into `projects` or `gobby_migration_state` made by a prior test do not leak into the next. symbol: `tests.fixtures.postgres.postgres_canonical_seed`. symbol: `tests.fixtures.postgres._reset_schema`. test: `tests/fixtures/test_postgres_db_reset.py::test_seed_rows_survive_reset` (asserts: capture happens before the first reset; entry and exit resets receive the same snapshot; insert extra `projects` row → reset → only the four placeholders remain; mutate a `task_type_default_stages` row → reset → row matches the canonical snapshot; insert a `gobby_migration_state` key → reset → key is gone).

### 2.3 Parametrize storage fixtures over both backends [category: test] (depends: 2.2, 3.1, 3.2, 3.3)
`kind: deliverable`

Target: `tests/conftest.py`, selected storage test files under `tests/storage/`

Introduce a `hub_db` fixture that yields each backend in turn via `@pytest.fixture(params=["sqlite", "postgres"])`. Tests that previously used `temp_db` opt into the dual-backend fixture by renaming; tests asserting SQLite-specific behavior keep `temp_db` and are explicitly marked for deletion in Phase 7.

Expected footprint for this task: around 15 of the 48 `tests/storage/` files migrate to `hub_db` now. The remaining ~33 migrate as part of the Phase 3 port work that covers them.

**Acceptance:**

- 2.3.1 — Storage fixtures parametrize over SQLite and PostgreSQL backends. file: `tests/conftest.py`.

### 2.4 Add dialect parity regression tests [category: test] (depends: 2.3)
`kind: deliverable`

Target: `tests/storage/test_dialect_parity.py` (new)

Pin behavior that commonly diverges between SQLite and Postgres. Each assertion runs against both backends via the `hub_db` parametrization:

- upsert semantics (`INSERT OR IGNORE` vs `ON CONFLICT DO NOTHING`)
- generated-key consistency (`lastrowid` replacement via `RETURNING id`)
- JSON path extraction results (`json_extract(col, '$.k')` vs `col->>'k'`)
- timestamp default values are timezone-aware and UTC on both sides
- search ordering on representative queries — ordering must match; score values will not
- `UNIQUE (col, COALESCE(x, '__global__'))` behavior vs `UNIQUE NULLS NOT DISTINCT (col, x)`

These tests catch silent semantic drift during Phase 3–4 work.

**Acceptance:**

- 2.4.1 — Dialect parity regression suite covers SQL surface differences across both backends. file: `tests/storage/test_dialect_parity.py`.

## P3 Phase 3: Backend-neutral storage seam and migration runner
`kind: framing`

**Goal**: storage call sites depend on a backend-neutral `HubDatabase` protocol, and the migration runner works on both backends.

### 3.1 Define the `HubDatabase` protocol [category: code] (depends: P1)
`kind: deliverable`

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

**Acceptance:**

- 3.1.1 — `HubDatabase` protocol defines the backend-neutral storage seam. symbol: `gobby.storage.hub.protocol.HubDatabase`.

### 3.2 Implement `SqliteHubDatabase` shim [category: code] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/storage/hub/sqlite.py` (new), `tests/storage/hub/test_sqlite_placeholder_remap.py` (new)

Wraps existing `LocalDatabase`. Converts `sqlite3.Row` to plain `dict` at the adapter boundary. Translates `$N` placeholders to `?` and reorders/repeats the param tuple so the SQLite driver binds the same values the Postgres-style query would bind.

The naive `re.sub(r"\$\d+", "?", sql)` strategy drops ordinal information and silently mis-binds when a query repeats or reorders placeholders (`WHERE a = $2 AND b = $1`, `WHERE a = $1 OR b = $1`). A regex-with-context-guard is also insufficient: a `$1` token sitting inside a Postgres dollar-quoted body (`AS $$ BEGIN PERFORM $1; END; $$`) is valid PL/pgSQL — it is not a bind placeholder at the SQLite adapter boundary and must pass through byte-for-byte. The shim must therefore use an explicit scanner that tracks single-quoted strings (with `''` escapes), line and block comments, and active dollar-quote tags, substituting `$N` only when the cursor is in the top-level SQL state.

```python
# src/gobby/storage/hub/sqlite.py
from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from typing import Any

from gobby.storage.database import LocalDatabase
from gobby.storage.hub.protocol import HubDatabase, Transaction


def _remap_placeholders(
    sql: str, params: Sequence[Any]
) -> tuple[str, tuple[Any, ...]]:
    """Translate top-level `$N` placeholders to `?` and rebuild the param tuple.

    Walks ``sql`` once through a small state machine that recognizes single-
    quoted strings (with ``''`` escapes), ``--`` line comments, ``/* ... */``
    block comments, and Postgres dollar-quoted bodies (``$$ ... $$`` /
    ``$tag$ ... $tag$``). Inside any of those contexts the input is copied
    verbatim; only at the top level is ``$N`` rewritten to ``?`` and the
    matching value (``params[N - 1]``) appended to the new param tuple.

    Handles sequential, out-of-order, and repeated placeholders. Raises
    ``ValueError`` if the SQL references a ``$N`` index that isn't in ``params``
    or if a dollar-quote tag is unterminated.
    """
    out: list[str] = []
    new_params: list[Any] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]

        # Line comment: -- ... \n
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(sql[i:j])
            i = j
            continue

        # Block comment: /* ... */ (non-nesting per SQL spec)
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(sql[i:j])
            i = j
            continue

        # Single-quoted string with '' escape
        if c == "'":
            out.append(c)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    out.append("'")
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue

        # Dollar-quoted body or $N placeholder
        if c == "$":
            # If the `$` is preceded by an identifier-continuation character
            # (letter, digit, underscore), it is part of an identifier
            # (`foo$1`, `my_id$2`) and must pass through unchanged.
            if i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_"):
                out.append(c)
                i += 1
                continue
            tag_end = i + 1
            while tag_end < n and (sql[tag_end].isalnum() or sql[tag_end] == "_"):
                tag_end += 1
            if tag_end < n and sql[tag_end] == "$":
                tag = sql[i : tag_end + 1]
                close = sql.find(tag, tag_end + 1)
                if close < 0:
                    raise ValueError(f"unterminated dollar-quote tag {tag!r}")
                end = close + len(tag)
                out.append(sql[i:end])
                i = end
                continue
            digits = sql[i + 1 : tag_end]
            if digits and digits.isdigit():
                idx = int(digits)
                if idx < 1 or idx > len(params):
                    raise ValueError(
                        f"placeholder ${idx} has no matching param "
                        f"(query references {len(params)} params total)"
                    )
                new_params.append(params[idx - 1])
                out.append("?")
                i = tag_end
                continue
            out.append(c)
            i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out), tuple(new_params)


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

    def execute(self, sql: str, params: Sequence[Any] = ()):
        new_sql, new_params = _remap_placeholders(sql, params)
        cur = self._conn.execute(new_sql, new_params)
        return _SqliteCursor(cur)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        # Remap once on the first row to get the rewritten SQL; reorder each
        # row's params consistently so SQLite binds positionally.
        if not rows:
            return
        new_sql, _ = _remap_placeholders(sql, rows[0])
        remapped_rows = [_remap_placeholders(sql, row)[1] for row in rows]
        self._conn.executemany(new_sql, remapped_rows)

    # savepoint / after_commit implementations follow the same
    # boundary-translation pattern.
```

`tests/storage/hub/test_sqlite_placeholder_remap.py` must cover, with explicit assertions on the rewritten SQL and the new param tuple:

- sequential placeholders: `SELECT $1, $2`, params `("a", "b")` → `("SELECT ?, ?", ("a", "b"))`
- out-of-order placeholders: `WHERE a = $2 AND b = $1`, params `("x", "y")` → `("WHERE a = ? AND b = ?", ("y", "x"))`
- repeated placeholder: `WHERE a = $1 OR b = $1`, params `("z",)` → `("WHERE a = ? OR b = ?", ("z", "z"))`
- IN clause: `WHERE id IN ($1, $2, $3)`, params `(1, 2, 3)` → `("WHERE id IN (?, ?, ?)", (1, 2, 3))`
- skipped indices preserved: `SELECT $3, $1` is rewritten with the third and first params (callers may legitimately skip $2 in shared helpers)
- empty dollar-quote pass-through: `CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql` is unchanged and `new_params` is empty
- **dollar-quote with embedded `$N` pass-through**: `CREATE FUNCTION f(x int) RETURNS int AS $$ BEGIN RETURN $1 + 1; END; $$ LANGUAGE plpgsql` keeps `$1` byte-for-byte unchanged because the scanner recognizes the `$$ ... $$` body and stays in the dollar-quote state until the matching close tag (this is the F4 regression case from round 2 — required, not optional)
- named-tag dollar quote with embedded `$N` pass-through: `AS $body$ ... $1 ... $body$` keeps `$1` unchanged
- bind placeholder adjacent to a function body: `INSERT INTO t(name) VALUES ($1) WHERE id = (SELECT id FROM (SELECT $$abc$$, $2 AS k) s)` — first `$1` and `$2` are rewritten; `$$abc$$` body is unchanged
- bind placeholder inside a single-quoted string is preserved: `WHERE col = '$1'` is unchanged; `WHERE col = $1` is rewritten
- `''` escape inside a single-quoted string: `WHERE name = 'O''Brien' AND id = $1` rewrites only the `$1`
- bind placeholder inside a line comment: `-- $1 should not bind\nSELECT $1` rewrites only the second occurrence
- bind placeholder inside a block comment: `/* $1 */ SELECT $1` rewrites only the second occurrence
- identifier-like dollar suffix: `SELECT foo$1, $1 FROM t`, params `("a",)` → `("SELECT foo$1, ? FROM t", ("a",))` — `foo$1` is consumed as part of the identifier scan and never enters the dollar-quote/placeholder branch (the scanner only follows `$` when it isn't preceded by an identifier character; the test must include this exact input)
- `executemany` parity: same rewrite applied across every row tuple, with the SQL rewritten exactly once
- raises `ValueError` when SQL references a `$N` outside the param tuple's range
- raises `ValueError` on an unterminated dollar-quote tag

Upsert dialect translation is limited to placeholder rewriting; `ON CONFLICT` SQL is portable between SQLite 3.35+ and Postgres and does not need translation. SQL that genuinely differs (`datetime(...)`, `json_extract(...)`) is handled in Phase 4.6 with explicit dialect branches rather than runtime translation.

**Acceptance:**

- 3.2.1 — `SqliteHubDatabase` implements `HubDatabase` over the existing SQLite stack. symbol: `gobby.storage.hub.sqlite.SqliteHubDatabase`.
- 3.2.2 — `_remap_placeholders` rewrites `$N` to `?` and rebuilds the param tuple to handle sequential, out-of-order, repeated, and skipped indices; preserves Postgres dollar-quoted bodies untouched. symbol: `gobby.storage.hub.sqlite._remap_placeholders`.
- 3.2.3 — Placeholder-remap test suite covers sequential / out-of-order / repeated / IN-clause / skipped-index / dollar-quote / identifier-suffix / `executemany` / out-of-range-index cases. file: `tests/storage/hub/test_sqlite_placeholder_remap.py`.

### 3.3 Implement `PostgresHubDatabase` [category: code] (depends: 3.1, 3.7, 4.2)
`kind: deliverable`

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

**Placeholder remap — `$N` → `%s` for psycopg.** psycopg v3's Python binding API uses `%s` (positional) and `%(name)s` (named) placeholders, **not** `$N`. The plan standardizes author-facing SQL on `$N` (Rust/sqlx-portable). Therefore every parametrized execute against psycopg must remap `$N` placeholders to `%s` before psycopg sees the query, and reorder/duplicate the param tuple to match `%s` positional binding. The remapper lives next to `_PostgresTransaction` and is the symmetric Postgres counterpart of §3.2's SQLite-side `_remap_placeholders` (`$N` → `?`).

```python
def _remap_placeholders_to_psycopg(
    sql: str, params: Sequence[Any]
) -> tuple[str, tuple[Any, ...]]:
    """Rewrite top-level $N -> %s and rebuild the param tuple.

    Same scanner contract as gobby.storage.hub.sqlite._remap_placeholders:
    skip dollar-quoted bodies ($$...$$ and $tag$...$tag$), single-quoted
    strings (with '' escape), line comments (-- ...), block comments
    (/* ... */), and identifier-suffix `foo$N` patterns. Top-level $N is
    rewritten to a positional %s and the new tuple lists params in the
    order they appear in the SQL, duplicating values for repeated $N.

    Raises ValueError on unterminated dollar-quote tags or $N references
    outside the input tuple's range.
    """


class _PostgresTransaction:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        new_sql, new_params = _remap_placeholders_to_psycopg(sql, tuple(params))
        return self._conn.execute(new_sql, new_params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        # Mirror §3.2's SQLite executemany: derive new_sql and the index
        # permutation from the first row, then apply the permutation to every
        # subsequent row WITHOUT rescanning SQL.
        # The remapper is NEVER called with params=() when placeholders are
        # present — that combination is a contract violation that raises
        # ValueError on the empty-tuple validation step.
        materialized = list(rows)
        if not materialized:
            # Empty rows: psycopg's executemany no-op. Skip the remapper entirely
            # because we cannot validate $N indices without a row, and there is
            # no work to send to the server.
            return
        first = tuple(materialized[0])
        new_sql, first_permuted = _remap_placeholders_to_psycopg(sql, first)
        # Build the integer index permutation from the first call so subsequent
        # rows skip the SQL scan; signature: list[int] mapping output position
        # to input-tuple index. _build_param_permutation is exposed by the
        # remapper as a sibling helper for exactly this case.
        permutation = _build_param_permutation(sql, len(first))
        permuted_rows = [first_permuted, *(tuple(row[i] for i in permutation) for row in materialized[1:])]
        self._conn.executemany(new_sql, permuted_rows)
```

**Migration files vs application code.** SQL files under `src/gobby/storage/migrations/` (Phase 3.7) are **DDL** in the common case — no parameter binding — and therefore pass through psycopg unchanged with `params=()`. Postgres reads `$N` natively on the wire, so a parameter-less migration file written in `$N` form works as-is. Migration files that DO bind parameters must declare so explicitly and route through the same `_remap_placeholders_to_psycopg` helper so the runtime contract is uniform.

Required tests in `tests/storage/hub/test_postgres_placeholder_remap.py` (mirroring §3.2 test coverage):

- sequential: `SELECT $1, $2`, params `("a","b")` → `("SELECT %s, %s", ("a","b"))`
- out-of-order: `WHERE a = $2 AND b = $1`, `("x","y")` → `("WHERE a = %s AND b = %s", ("y","x"))`
- repeated: `WHERE a = $1 OR b = $1`, `("z",)` → `("WHERE a = %s OR b = %s", ("z","z"))`
- IN clause: `WHERE id IN ($1, $2, $3)`, `(1,2,3)` → `("WHERE id IN (%s, %s, %s)", (1,2,3))`
- skipped indices preserved: `SELECT $3, $1` rewrites with the third and first params
- empty dollar-quote pass-through: `CREATE FUNCTION ... AS $$ BEGIN PERFORM 1; END; $$` unchanged, `new_params=()`
- dollar-quote with embedded `$N` pass-through: `AS $$ BEGIN RETURN $1 + 1; END; $$` keeps `$1` byte-for-byte
- named-tag dollar quote with embedded `$N` pass-through: `AS $body$ ... $1 ... $body$` unchanged
- bind placeholder adjacent to a function body: only top-level `$1`/`$2` are rewritten; `$$abc$$` body is unchanged
- bind placeholder inside a single-quoted string preserved; `''` escape inside string handled correctly
- bind placeholder inside line/block comments preserved
- identifier-suffix: `SELECT foo$1, $1 FROM t` keeps `foo$1` and rewrites only the bare `$1`
- `executemany` parity: SQL rewritten exactly once via the first row, subsequent rows permuted by the cached index map without rescanning SQL; remapper is never invoked with `params=()` when placeholders are present
- `executemany` empty-rows branch: rows iterator is empty → no SQL rewrite, no server call, returns cleanly
- raises `ValueError` when SQL references a `$N` outside the param tuple's range (single `execute` and first-row of `executemany`)
- raises `ValueError` on an unterminated dollar-quote tag

**Postgres baseline application.** A fresh Postgres database (no `schema_migrations` table, no `gobby_migration_state` table) **must** receive `postgres_baseline_schema.sql` before the file-based migration runner walks the on-disk migration directory. `PostgresHubDatabase.apply_migrations()` ensures this by gating on the presence of `schema_migrations`:

```python
def apply_migrations(self) -> None:
    runner = MigrationRunner(self)
    if not self._postgres_baseline_already_applied():
        self._apply_postgres_baseline()  # reads postgres_baseline_schema.sql
    runner.apply_pending()  # walks src/gobby/storage/migrations/*.sql
```

`_apply_postgres_baseline` uses an advisory-lock guard (`SELECT pg_advisory_lock(...)`) so concurrent daemon starts cannot double-apply, executes `postgres_baseline_schema.sql` inside a transaction, and seeds `schema_migrations` with `(244, NOW())` on commit. `_postgres_baseline_already_applied` returns `True` iff the public-schema-equivalent search-path contains `schema_migrations` and a row at version 244 already exists.

**Pre-baseline infrastructure allowlist.** Two valid install paths intentionally create tables in the target database **before** `apply_migrations()` ever runs, and those tables must not be classified as a corrupt partial baseline:

```python
_PRE_BASELINE_INFRA_TABLES: frozenset[str] = frozenset({
    "gobby_install_ownership",  # written by §1.2 external-mode installer
    "_pgaudit_probe",           # written by §6.0 Docker-mode initdb.d/pgaudit-bootstrap.sh
})
```

The partial-state classifier ignores any table in `_PRE_BASELINE_INFRA_TABLES` when deciding "is this a corrupt partial baseline?" — only **application tables** (anything not in the allowlist, not `schema_migrations`, not `gobby_migration_state`) trigger the error path:

```python
def _classify_baseline_state(conn: psycopg.Connection) -> Literal[
    "fresh", "fresh_with_install_infra", "already_baselined", "corrupt_partial"
]:
    tables = {row[0] for row in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    ).fetchall()}
    has_bookkeeping = "schema_migrations" in tables
    application_tables = tables - _PRE_BASELINE_INFRA_TABLES - {
        "schema_migrations", "gobby_migration_state"
    }

    if has_bookkeeping and _has_baseline_version(conn, 244):
        return "already_baselined"
    if has_bookkeeping and not application_tables:
        # bookkeeping table only; no baseline rows, no app tables → fresh-ish
        return "fresh"
    if not has_bookkeeping and not application_tables:
        # zero or more pre-baseline infra tables, nothing else → fresh
        return "fresh_with_install_infra" if (tables & _PRE_BASELINE_INFRA_TABLES) else "fresh"
    return "corrupt_partial"
```

`_apply_postgres_baseline` runs for `fresh` / `fresh_with_install_infra` (single, idempotent transaction); skips for `already_baselined`; raises `MigrationUnsupportedError` with explicit recovery guidance ("dump-and-restore from a known-good baseline") for `corrupt_partial`.

Required tests in `tests/storage/hub/test_postgres_baseline_application.py`:

- **fresh**: empty Postgres DB → `apply_migrations()` creates baseline tables, `schema_migrations`, and `gobby_migration_state`; subsequent `apply_migrations()` is idempotent.
- **fresh_with_install_infra (external)**: Postgres DB with only `gobby_install_ownership` (as written by §1.2 external-mode installer) → `apply_migrations()` succeeds; baseline tables created alongside the existing ownership row.
- **fresh_with_install_infra (Docker pgAudit)**: Postgres DB with only `_pgaudit_probe` (as written by §6.0 initdb seed) → `apply_migrations()` succeeds; probe row preserved.
- **already_baselined**: Postgres DB at v244 → `_apply_postgres_baseline` is skipped; runner walks file-based migrations only.
- **corrupt_partial**: Postgres DB with a real application table (e.g., `tasks`) but no `schema_migrations` → raises `MigrationUnsupportedError` with the dump-and-restore guidance.
- **corrupt_partial with infra-only false-positive guard**: Postgres DB with `gobby_install_ownership` AND `tasks` (real app table) → still raises `MigrationUnsupportedError` (the allowlist does not whitewash genuine corruption).
- **concurrent**: `apply_migrations()` calls from two pool connections racing → advisory lock serializes them; baseline is applied exactly once.

**Acceptance:**

- 3.3.1 — `PostgresHubDatabase` implements `HubDatabase` over psycopg/PostgreSQL. symbol: `gobby.storage.hub.postgres.PostgresHubDatabase`.
- 3.3.2 — `_remap_placeholders_to_psycopg` rewrites `$N` to `%s` and rebuilds the param tuple to handle sequential, out-of-order, repeated, IN-clause, skipped-index, dollar-quote, identifier-suffix, `executemany`, and out-of-range-index cases; preserves dollar-quoted bodies, single-quoted strings (with `''` escape), and line/block comments untouched. symbol: `gobby.storage.hub.postgres._remap_placeholders_to_psycopg`. test: `tests/storage/hub/test_postgres_placeholder_remap.py`.
- 3.3.3 — `PostgresHubDatabase.apply_migrations()` applies `postgres_baseline_schema.sql` exactly once on an uninitialized database before running file-based migrations, is idempotent under repeat calls, serializes concurrent calls via a Postgres advisory lock, and raises `MigrationUnsupportedError` on partial-baseline states. symbol: `gobby.storage.hub.postgres.PostgresHubDatabase._apply_postgres_baseline`. test: `tests/storage/hub/test_postgres_baseline_application.py`.

### 3.4 Port row consumers off `sqlite3.Row` [category: refactor] (depends: 3.2, 3.3)
`kind: deliverable`

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

**Acceptance:**

- 3.4.1 — Row consumers ported from `sqlite3.Row` to backend-neutral dict-like row access. file: `src/gobby/storage/`.

### 3.5 Replace `lastrowid` with `RETURNING id` [category: refactor] (depends: 3.4)
`kind: deliverable`

Target: `src/gobby/storage/task_dependencies.py:73`, `src/gobby/storage/task_affected_files.py:78` and `:114`, `src/gobby/storage/workflow_audit.py:102`, `src/gobby/storage/tasks/_lifecycle_events.py:89`, plus any additional `lastrowid` sites surfaced by a fresh `rg "\.lastrowid"` over `src/gobby/storage/` at implementation time. (`src/gobby/storage/token_events.py:158` uses `lastrowid` as a Python-internal "did the row commit?" boolean rather than capturing a generated ID — handle by checking `cursor.rowcount > 0` instead, or carry a dialect branch.)

Rewrite these sites to capture generated IDs via `RETURNING`. SQLite 3.35+ supports `RETURNING` natively; no SQLite fallback needed. The fresh-grep clause is non-negotiable: post-#13935 the storage layer accumulates new write paths, and a hardcoded inventory drifts every time storage gains a manager.

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

**Acceptance:**

- 3.5.1 — `RETURNING id` replaces `lastrowid` across hub-storage writes. file: `src/gobby/storage/`.

### 3.6 Replace `INSERT OR IGNORE` / `INSERT OR REPLACE` with `ON CONFLICT` [category: refactor] (depends: 3.4)
`kind: deliverable`

Target: `src/gobby/storage/projects.py:164`, `src/gobby/storage/session_tasks.py`, `src/gobby/storage/sessions/` (package — sweep `_crud.py`, `_field_update.py`, `_bulk_update.py`), `src/gobby/storage/pipelines.py`, `src/gobby/storage/agents.py:360`, plus any additional `INSERT OR IGNORE` / `INSERT OR REPLACE` sites surfaced by a fresh repo grep at implementation time (≈8 total)

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

For `INSERT OR REPLACE`-style full-row replacement (`agents.py:360`), spell the column list explicitly in `ON CONFLICT ... DO UPDATE SET col = EXCLUDED.col, ...`. No silent "replace everything" semantics — every replaced column is enumerated.

**Acceptance:**

- 3.6.1 — `ON CONFLICT` replaces SQLite-specific upsert syntax across hub-storage writes. file: `src/gobby/storage/`.

### 3.7 Rewrite the migration runner with dollar-quote-aware splitting for both backends [category: code] (depends: 3.1, 3.2)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py`, `src/gobby/storage/migrations/` (new data directory; sibling-by-name to the `migrations.py` runner module — Python disambiguates: `migrations.py` is the importable module, `migrations/` is a data directory loaded via `importlib.resources.files("gobby.storage").joinpath("migrations")`. The directory MUST NOT contain an `__init__.py` — adding one would make it a package and shadow the module.)

The current runner splits `baseline_schema.sql` on `;` and executes statements one at a time. That strategy never handled FTS5 trigger bodies (hence the five Python helpers in `migration_helpers.py`) and will not handle Postgres `CREATE FUNCTION ... $$ ... $$ LANGUAGE plpgsql` bodies. The rewrite replaces both the split logic and the `_setup_*_fts` Python helpers with a dollar-quote-aware statement splitter that reads migration files from disk, applies them atomically per version, and records applied versions in a shared `schema_migrations` table.

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

`_split_statements_respecting_dollar_quotes` must treat semicolons outside any active quote/comment boundary as the only split points. It needs unit tests for:

- nested dollar-quoted tags (for example `$outer$...$inner$...$outer$`)
- adjacent dollar-quoted tags (`$tag1$...$tag1$ $tag2$...$tag2$`)
- semicolons inside single-quoted strings
- semicolons inside line (`--`) and block (`/* */`) comments
- dollar-like patterns inside comments and single-quoted strings
- empty dollar quotes (`$$...$$`)
- escaped single quotes such as `'can''t'`
- mixed contexts where strings/comments appear inside dollar-quoted bodies
- a split→join→execute smoke/fuzz test against real PostgreSQL DDL

Production use stays blocked until those tests exist and pass in CI. The merge
gate is CI coverage plus green parser tests, not a brittle runtime heuristic.

Migration file layout (all paths repo-relative under `src/gobby/storage/migrations/`):

- `src/gobby/storage/migrations/NNN_name.sql` — shared, works on both backends
- `src/gobby/storage/migrations/NNN_name.sqlite.sql` / `src/gobby/storage/migrations/NNN_name.postgres.sql` — dialect-specific

The runner discovers files via `importlib.resources.files("gobby.storage").joinpath("migrations").iterdir()`, sorts them by integer-prefix `NNN`, and routes each entry through `path_for_dialect()` based on filename suffix. The directory has no `__init__.py` so it stays a data directory; the runner module remains `gobby.storage.migrations` (the `.py` file).

`schema_migrations` schema is identical on both backends:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
);
```

For Postgres, `NOW()` returns `TIMESTAMPTZ`. For SQLite during overlap, `NOW()` is not a builtin; the shared migration table creation uses `CURRENT_TIMESTAMP` which both backends accept — and the runner's insert uses `NOW()` on Postgres and `CURRENT_TIMESTAMP` on SQLite via a dialect-check in `_run_migration`'s bookkeeping step.

**Table rename, with a self-contained one-shot migration**: the SQLite baseline today (and the Phase 0 baseline at v244) uses `schema_version (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))`. This task renames it to `schema_migrations` on both backends to align with the `sqlx` convention that the later Rust port will consume. Existing post-Phase-0 v244 SQLite databases carry `schema_version` only and must reach `schema_migrations` exactly once, atomically, without losing any version rows. The runner ships a startup bookkeeping-migration step that runs before any file-based migration and is safe under all input states:

```python
def _migrate_bookkeeping_table(conn: HubConnection) -> None:
    """Idempotently rename schema_version -> schema_migrations on SQLite startup.

    Pre-3.7 databases carry schema_version only.
    Post-3.7 databases carry schema_migrations only.
    A divergent state (both tables present with mismatched rows) is a
    corruption marker and aborts startup with a precise error message
    pointing at backup recovery.
    """
    has_old = _table_exists(conn, "schema_version")
    has_new = _table_exists(conn, "schema_migrations")

    if has_new and not has_old:
        return  # already migrated; idempotent no-op

    if has_old and not has_new:
        with conn.transaction():
            conn.execute("ALTER TABLE schema_version RENAME TO schema_migrations")
            # Backfill applied_at if the column type/default needs alignment;
            # for SQLite the prior TEXT DEFAULT (datetime('now')) is preserved
            # by RENAME and remains valid under the new name.
        return

    if has_old and has_new:
        old_rows = conn.execute("SELECT version FROM schema_version").fetchall()
        new_rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        if {r[0] for r in old_rows} == {r[0] for r in new_rows}:
            with conn.transaction():
                conn.execute("DROP TABLE schema_version")
            return
        raise MigrationUnsupportedError(
            "Both schema_version and schema_migrations exist with divergent rows. "
            "This indicates a corrupted bookkeeping state; restore ~/.gobby/gobby-hub.db "
            "from a backup before continuing."
        )

    # Neither table exists: brand-new database. The Phase 3.7 runner creates
    # schema_migrations on first use; nothing to migrate.
```

`MigrationRunner.apply_pending` calls `_migrate_bookkeeping_table` first, then `_ensure_schema_migrations_table` (idempotent CREATE TABLE IF NOT EXISTS), then proceeds with file-based migrations. Postgres has no pre-existing `schema_version` table (Postgres ships with `schema_migrations` from the v244-era baseline translation in §4.2), so the bookkeeping-migration step is a no-op on Postgres but still runs for symmetry.

Required tests in `tests/storage/test_migration_runner.py`:

- v244 SQLite fixture with only `schema_version` (containing rows {244, 240, 241, 242, 244} matching the v244 post-Phase-0 history): runner renames to `schema_migrations`, all rows preserved, `schema_version` no longer exists, and a re-run is a no-op.
- Brand-new SQLite database (neither table): runner creates `schema_migrations` lazily, no spurious rename.
- Both-tables-exist with identical row sets: runner drops the legacy `schema_version`, retains `schema_migrations`, idempotent.
- Both-tables-exist with divergent row sets: runner raises `MigrationUnsupportedError` with the recovery message.
- Postgres fixture with only `schema_migrations`: bookkeeping step is a no-op, runner proceeds.

§5.1's source-schema gate (the SQLite reader feeding `gobby postgres migrate-from-sqlite`) reads `schema_migrations` post-3.7; pre-3.7 sources still expose `schema_version` only and must continue to be probed against the post-Phase-0 baseline floor. Update §5.1 to reference `schema_migrations` for post-3.7 sources and document fallback to `schema_version` for the pre-3.7 source-DB band.

**Acceptance:**

- 3.7.1 — Migration runner uses dollar-quote-aware statement splitting and works against both backends. symbol: `gobby.storage.migrations.MigrationRunner`.
- 3.7.2 — `_migrate_bookkeeping_table` renames `schema_version` to `schema_migrations` exactly once on existing SQLite databases, preserves all version rows, is idempotent under repeat runs, drops a duplicate identical-rows `schema_version` if both tables exist, and raises `MigrationUnsupportedError` on divergent state. symbol: `gobby.storage.migrations._migrate_bookkeeping_table`. test: `tests/storage/test_migration_runner.py::test_bookkeeping_table_rename_paths`.

### 3.8 Port storage managers off `DatabaseProtocol` onto `HubDatabase` [category: refactor] (depends: 3.7)
`kind: deliverable`

Target: every module under `src/gobby/storage/` that imports `DatabaseProtocol` or calls `db.execute` / `db.executemany` / `db.fetchone` / `db.fetchall` / `db.safe_update` / `db.connection` directly (roughly the same ~20-module footprint as §3.4).

**Why this exists.** `HubDatabase` (defined in §3.1) is a transaction-only protocol — `transaction()`, `apply_migrations()`, `close()`. The current managers reach the database via `DatabaseProtocol`, which exposes manager-facing convenience methods (`execute`, `executemany`, `fetchone`, `fetchall`, `safe_update`, `connection`). Without this task, swapping `runner_init.py` to construct a `HubDatabase` (§1.3 runtime wiring) would leave every manager calling methods absent from `PostgresHubDatabase` / `SqliteHubDatabase` — runtime AttributeError on the first manager call.

**Approach.** Extend `HubDatabase` (and both adapters) with a manager-facing convenience layer that wraps `transaction()` + `Transaction.execute()` for the simple read/write cases, while leaving `transaction()` available for callers that need explicit boundaries. Specifically:

```python
# src/gobby/storage/hub/protocol.py — extension to §3.1
class HubDatabase(Protocol):
    dialect: ClassVar[str]

    @contextmanager
    def transaction(self) -> Iterator[Transaction]: ...
    def apply_migrations(self) -> None: ...
    def close(self) -> None: ...

    # Manager-facing convenience layer (auto-commit per call; thin wrapper
    # around transaction()+Transaction.execute()). Both adapters implement
    # these by acquiring a short-lived transaction internally.
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...
    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None: ...
    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Mapping[str, Any] | None: ...
    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]: ...
    def safe_update(self, sql: str, params: Sequence[Any] = ()) -> int: ...  # returns rowcount
```

`SqliteHubDatabase` and `PostgresHubDatabase` implement each method by entering `self.transaction()`, calling the matching `Transaction` method, and returning the result. The Postgres side routes every call through `_remap_placeholders_to_psycopg` (already specified in §3.3); the SQLite side routes through `_remap_placeholders` (§3.2). `connection` (raw connection access) is **deliberately not preserved** — it leaked SQLite-specific semantics; managers that need explicit transaction boundaries migrate to `with db.transaction() as txn:` instead.

**Scope of the consumer migration.** Sweep `src/gobby/storage/` for every `DatabaseProtocol` import and every `db.connection` / `db.cursor()` reference. Replace `DatabaseProtocol` imports with `HubDatabase`. Replace each `db.connection` / `db.cursor()` block with the equivalent `with db.transaction() as txn: txn.execute(...)` form. Leave `db.execute` / `db.fetchone` / `db.fetchall` / `db.safe_update` calls structurally unchanged — the new convenience layer keeps them working.

Add a lint in `src/gobby/storage/` that fails on any new import of the legacy `DatabaseProtocol` for runtime use (test fixtures may keep referencing it during the transition; the lint scopes to non-test files). Phase 7's SQLite removal deletes `DatabaseProtocol` outright.

**Tests.** A representative manager (e.g., `LocalTaskManager`) is parametrized over `hub_db` and exercised against both backends covering read, write, multi-statement transaction, and a `safe_update` rowcount assertion. The §2.3 dual-backend `hub_db` fixture is the natural test surface.

**Acceptance:**

- 3.8.1 — `HubDatabase` protocol extended with `execute`, `executemany`, `fetchone`, `fetchall`, `safe_update` manager-facing convenience methods; both adapters implement them by routing through `transaction()` + `Transaction.execute()`. symbol: `gobby.storage.hub.protocol.HubDatabase`.
- 3.8.2 — Every manager under `src/gobby/storage/` that previously imported `DatabaseProtocol` now imports `HubDatabase`; no runtime call site references `db.connection` or `db.cursor()`. file: `src/gobby/storage/`. behavior: "no runtime imports of legacy DatabaseProtocol" enforced by lint.
- 3.8.3 — A representative manager parametrized through `hub_db` passes against both SQLite and Postgres adapters, covering read / write / multi-statement transaction / `safe_update` rowcount. test: `tests/storage/test_manager_surface_parity.py::test_local_task_manager_dual_backend`.

## P4 Phase 4: PostgreSQL schema and query parity
`kind: framing`

**Goal**: every query path runs natively on Postgres, FTS5 is replaced with `pg_search` BM25 indexes, and all Rust-portability hygiene is applied.

### 4.1 Verify no Python migration callables survive into Postgres paths [category: refactor] (depends: 3.7)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py`, `src/gobby/storage/migration_helpers.py`, lint rule under `src/gobby/storage/`

**Scope after Phase 0 + flattening**: the only Python callables remaining in the migration path are the five FTS5 setup helpers in `migration_helpers.py` (`_setup_code_symbols_fts`, `_setup_code_content_fts`, `_setup_tasks_fts`, `_setup_skills_fts`, `_setup_memories_fts`). These are SQLite-specific by construction — they set up FTS5 virtual tables and triggers that have no Postgres equivalent; `pg_search` BM25 indexes replace them in task 4.4, and the helpers themselves are deleted in Phase 7.2.

This task therefore becomes a **verification pass**:

1. Confirm `migrations.MIGRATIONS` contains exactly the four Phase 0 band entries from §0.1 step 2 — `(240, _apply_delivery_state_schema)`, `(241, _apply_github_triage_schema)`, `(242, _REVIEW_ANCHOR_DEFAULT_STAGE_SCHEMA)`, and `(244, "Phase 0 flatten marker", _apply_phase0_flatten_marker)` — or, after Phase 3.7 lands, is empty. No new in-Python entries may be added between Phase 0 and Phase 3.7. Post-3.7, all entries must use the file-based shape (`src/gobby/storage/migrations/NNN_name.sql`).
2. Confirm `migration_helpers.py` is referenced only from `migrations._apply_baseline` (the SQLite-only path) and the FTS5 backend code. It must never be invoked from the Postgres path.
3. Add a lint in `src/gobby/storage/` that:
    - Pre-3.7: fails if `MIGRATIONS` contains anything other than the four Phase 0 band entries (exact match on the four `(version, name, action)` tuples — versions `240`, `241`, `242`, `244`; the `244` entry's action MUST be a callable, not a string, since an inline `schema_version`-touching string would collide with the runner's automatic insert).
    - Post-3.7: fails if `MIGRATIONS` is non-empty, and fails on any `Callable` or string entry being added.

No new `.sql` files are produced by this task. The prior-revision scope (port `_migrate_claimed_by_session_id` and friends to SQL) is obsolete — those callables were folded into the original v219 baseline by commit `4be00747a` and no longer exist.

**Acceptance:**

- 4.1.1 — No Python migration callables survive into the Postgres path; only declarative SQL files under `src/gobby/storage/migrations/`. The SQLite-only `_apply_*` callables in `migrations.py` and the FTS5 setup helpers in `migration_helpers.py` may persist (they are gated to the SQLite baseline path), but neither is invoked from the Postgres dialect path. behavior: "no Python migration callables in Postgres migration path" in `src/gobby/storage/migrations/`.

### 4.2 Add `postgres_baseline_schema.sql` [category: code] (depends: P0)
`kind: deliverable`

Target: `src/gobby/storage/postgres_baseline_schema.sql` (new)

**Source artifact**: `src/gobby/storage/baseline_schema.sql` (1209 lines, 68 `CREATE TABLE`, 173 `CREATE INDEX`, seed `INSERT`s for the `_orphaned`, `_migrated`, `_personal`, and `_global` placeholder projects). This is the only SQL file to translate — there is no 219-step chain to replay.

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
| `INSERT INTO … VALUES (…, datetime('now'), datetime('now'))` (seed rows) | `INSERT INTO … VALUES (…, NOW(), NOW())` — or elide the explicit timestamps entirely and let the column `DEFAULT NOW()` populate them |

**Version table**: the Postgres baseline ships the applied-migration table as `schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`, matching the rename adopted in task 3.7.

**Cutover marker table**: the Postgres baseline also ships `gobby_migration_state (key TEXT PRIMARY KEY, value TEXT)`. The SQLite→Postgres importer (task 5.1 step 12) writes `('imported_from_sqlite_at', NOW()::text)` here; `gobby postgres activate` checks for that key before flipping `hub_backend`.

Every FTS5 virtual table is replaced by a `pg_search` BM25 index on the content table (task 4.4). pg_search's index access method keeps the index in sync automatically — no `BEFORE UPDATE` trigger or shadow column is required.

Every partial index in the SQLite schema is preserved — SQL syntax is portable.

Foreign keys in the Postgres baseline are declared `DEFERRABLE INITIALLY IMMEDIATE`. The migration tool uses `SET CONSTRAINTS ALL DEFERRED` during the one-shot import so cyclical references such as `sessions` ↔ `agent_runs` validate at `COMMIT` instead of forcing unsafe trigger-disabling hacks.

`PRAGMA foreign_keys=ON` has no Postgres equivalent; foreign keys are always enforced. `PRAGMA query_only=ON` (used for test read-only enforcement) becomes `SET TRANSACTION READ ONLY` at the adapter level for the read-only fixture path.

**Acceptance:**

- 4.2.1 — `TEXT` ISO 8601 timestamp columns translated to `TIMESTAMPTZ` (translation table row 1). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.2 — `TEXT DEFAULT datetime('now')` columns translated to `TIMESTAMPTZ NOT NULL DEFAULT NOW()` (translation table row 2). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.3 — `INTEGER` 0/1 boolean columns translated to `BOOLEAN` (translation table row 3). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.4 — `BLOB` columns translated to `BYTEA` (translation table row 4). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.5 — `TEXT` JSON columns translated to `JSONB` (translation table row 5). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.6 — `INTEGER PRIMARY KEY AUTOINCREMENT` columns translated to `INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY` (translation table row 6). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.7 — `UNIQUE (col, COALESCE(x, '__global__'))` constraints translated to `UNIQUE NULLS NOT DISTINCT (col, x)` (translation table row 7). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.8 — Seed `INSERT … VALUES (…, datetime('now'), datetime('now'))` rows translated to use `NOW()` or rely on the column-level `DEFAULT NOW()` (translation table row 8). file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.9 — `schema_migrations` (renamed in §3.7) and `gobby_migration_state` cutover-marker tables ship in the baseline. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.2.10 — Foreign keys declared `DEFERRABLE INITIALLY IMMEDIATE` so the §5.1 importer can use `SET CONSTRAINTS ALL DEFERRED` for cyclical references like `sessions` ↔ `agent_runs`. file: `src/gobby/storage/postgres_baseline_schema.sql`.

### 4.3 Standardize parameter style on `$1` [category: refactor] (depends: 3.2, 3.3, 3.8)
`kind: deliverable`

Target: every `.execute()` / `.executemany()` call site that currently uses `?`

Rewrite `?` → `$1`, `$2`, ... by query shape, not by blind regex. Dynamic `IN (...)` builders and reusable clause helpers need explicit rewrites so numbering stays valid when placeholder counts vary.

Execute as an audited pass grouped by query class:

- fixed-arity statements: direct manual renumbering
- dynamic `IN (...)` builders: replace with a helper that emits numbered placeholders from a starting offset
- shared clause helpers: rewrite once, then reuse from callers

Add a lint (custom ruff plugin or pre-commit grep) in `src/gobby/storage/` that fails on new `?` placeholders outside the shim itself.

**Acceptance:**

- 4.3.1 — Hub-storage SQL uses `$1` parameter placeholders consistently across both backends. file: `src/gobby/storage/`.

### 4.4 Replace FTS5 with `pg_search` BM25 indexes [category: code] (depends: 3.3, 4.2)
`kind: deliverable`

Target: `src/gobby/storage/postgres_baseline_schema.sql`, migration files for `tasks_fts`, `memories_fts`, `code_symbols_fts`, `code_content_fts`, `skills_fts` replacements

`pg_search` maintains its own inverted index transparently — no `tsvector` column, no refresh trigger. For each content table, create a BM25 index covering the searchable columns:

**Extension creation belongs to install/provisioning, not the schema/migration path.** §1.1 (Docker compose) and §1.2 native-mode installer are responsible for `CREATE EXTENSION pg_search` against their owned databases (Docker via the local-build image's initdb seed; native via the installer running with sudo against the user's local Postgres). §1.2 external mode is contractually **read-only** against the operator's database — it probes `SELECT 1 FROM pg_extension WHERE extname='pg_search'` and fails closed with the documented manual-install guidance if absent. Therefore §4.4 **must not** issue `CREATE EXTENSION` from `postgres_baseline_schema.sql` or any migration file: doing so would violate the external-mode least-privilege contract and would fail for any operator role lacking the `CREATEEXTENSION` privilege on their own database.

The runner's responsibility is reduced to a runtime probe with a clear error message: `_apply_postgres_baseline` checks `SELECT 1 FROM pg_extension WHERE extname='pg_search'` before reading `postgres_baseline_schema.sql`; if absent, raise `MigrationUnsupportedError("pg_search extension is not present on this database. Docker mode: rebuild the image. Native mode: rerun 'gobby postgres install --mode native'. External mode: install pg_search per docs/runbooks/postgres-pgsearch-install.md.")`. This shifts mode-specific responsibility to the install path while keeping the runtime check enforceable.

Required tests (added to `tests/storage/hub/test_postgres_baseline_application.py`):

- pg_search present → `apply_migrations()` proceeds.
- pg_search absent → raises `MigrationUnsupportedError` with the mode-aware guidance string; no `CREATE EXTENSION` statement is executed.
- external-mode target (probe-only) where pg_search is pre-installed → `apply_migrations()` succeeds and the runner issues no extension DDL (assert via `pg_stat_statements` or via wrapping the connection in a recorder that captures every executed SQL string).

```sql
-- pg_search extension is provisioned by install (Docker initdb / native installer),
-- not by this schema. The runner probes for its presence and refuses to baseline
-- without it. See "Extension creation belongs to install/provisioning" above.

-- Tasks
CREATE INDEX tasks_search_bm25 ON tasks
USING bm25 (id, title, description)
WITH (key_field='id');

-- Memories — stringified JSON tags via a generated column because BM25
-- indexes want text, not JSONB, and we need tag content searchable.
--
-- Generated columns cannot contain subqueries directly, so the flattening
-- subquery lives inside an IMMUTABLE SQL function. PostgreSQL allows
-- function calls in generated-column expressions when the function is
-- marked IMMUTABLE; the immutability contract is the user's responsibility
-- to uphold (memories_tags_to_text is deterministic — same jsonb in,
-- same text out).
ALTER TABLE memories
ADD CONSTRAINT tags_is_array
CHECK (tags IS NULL OR jsonb_typeof(tags) = 'array');

CREATE OR REPLACE FUNCTION memories_tags_to_text(tags jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
RETURNS NULL ON NULL INPUT
AS $$
    -- ORDER BY ord is load-bearing: jsonb arrays preserve element order, but
    -- string_agg's input order is undefined unless an explicit ORDER BY is
    -- supplied. Without it, the function is not demonstrably deterministic
    -- and the IMMUTABLE marker on this function (and the generated column
    -- it backs) is unsafe.
    SELECT COALESCE(string_agg(value, ' ' ORDER BY ord), '')
    FROM jsonb_array_elements_text(tags) WITH ORDINALITY AS t(value, ord);
$$;

ALTER TABLE memories
ADD COLUMN tags_text TEXT
GENERATED ALWAYS AS (memories_tags_to_text(tags)) STORED;

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

The `tags_is_array` constraint keeps the helper function honest if `memories.tags` drifts later. `RETURNS NULL ON NULL INPUT` plus the `COALESCE(..., '')` inside the function body keep `tags_text` non-NULL and TEXT-compatible for empty tag arrays while still producing NULL when `tags` itself is NULL — which is what the BM25 index wants. The function is `IMMUTABLE PARALLEL SAFE` so query planners can inline and parallelize it; both flags are required for the generated column to be eligible for parallel scans during BM25 index builds.

Queries use the `@@@` operator with pg_search's DSL. The pinned `pg_search` version (see §1.4 `version.json` `pg_search_version`) ships the **legacy** scoring surface, where the score expression is `pdb.score(<key_field>)`, not the removed `paradedb.rank_bm25(...)` function:

```sql
SELECT id, title, pdb.score(id) AS score
FROM tasks
WHERE title @@@ $1 OR description @@@ $1
ORDER BY score DESC
LIMIT $2;
```

If the `pg_search_version` pin is later bumped to a v2-syntax release, this query and §4.5's `BM25SearchBackend` must be rewritten to the v2 scoring surface in lockstep — the API change is breaking and there is no shim. The §1.4 PR template for `pg_search` bumps must include a checklist item asserting the scoring expressions in §4.4 and §4.5 still compile against the new pinned version.

pg_search keeps its index in sync automatically on `INSERT` / `UPDATE` / `DELETE` via Postgres's index access method hooks — **no application-side refresh, no `BEFORE UPDATE` trigger needed**. This is a material simplification over tsvector + trigger-maintained columns.

For the `memories` tag-stripping case, the `tags_text` generated column (backed by the IMMUTABLE `memories_tags_to_text(jsonb)` function) keeps the BM25 index clean of JSON syntax without duplicating state — the generated column is deterministic from `tags`, so there is no sync risk and no trigger maintenance.

`skills_fts` (contentless in SQLite) and the other four tables all have identical handling now: one `CREATE INDEX ... USING bm25` per table, plus any generated columns required to flatten non-text content into searchable text.

**Acceptance:**

- 4.4.1 — `pg_search` BM25 indexes replace FTS5 on the PostgreSQL path. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.4.2 — Minimal `pdb.score(id)` smoke test compiles and orders correctly against the pinned `pg_search` version: seed three rows in `tasks` (titles "alpha alpha", "alpha beta", "gamma"), query `SELECT id, pdb.score(id) AS score FROM tasks WHERE title @@@ 'alpha' ORDER BY score DESC`, assert two rows returned (the "gamma" row excluded), the higher-frequency match ranks first, and no SQL compile error is raised. test: `tests/storage/hub/test_pg_search_score_api.py::test_pdb_score_compiles_and_orders`.

### 4.5 Port search backends [category: code] (depends: 4.4)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_search.py`, `src/gobby/memory/manager.py` (keyword/vector/graph fan-out + `_rrf_merge`/`_rrf_scores` at lines 503–534), `src/gobby/storage/skills/` (package — sweep `_manager.py`, `_metadata.py` for FTS5 query construction), `src/gobby/search/` code-index search code, `tests/storage/test_dialect_parity.py` (extend §2.4 with fused-search parity cases)

This task has two layers and the second is **load-bearing for memory search parity**: the keyword-backend dispatch (`KeywordSearchBackend` interface, `FTS5` and `BM25` implementations), and the fused-ranking layer (`MemoryManager._rrf_merge` / `_rrf_scores` and the keyword/vector/graph fan-out at `manager.py:840`) that consumes whichever keyword backend is active. Replacing only the keyword dispatch — i.e. shipping `BM25SearchBackend` while leaving fused/RRF behavior unverified — would silently degrade memory search by changing the keyword input to RRF without any guarantee that representative-query top-N ordering still matches.

**Layer 1 — keyword backend dispatch:**

```python
from typing import Literal, Protocol

from gobby.storage.hub.protocol import HubDatabase

SearchMode = Literal["keyword", "semantic"]


class SearchHit:
    id: str
    score: float
    snippet: str | None


class KeywordSearchBackend(Protocol):
    """Single-source keyword search. Returns a ranked list per call."""
    def search(self, query: str, limit: int) -> list[SearchHit]: ...


def pick_search_backend(
    hub: HubDatabase,
    table: str,
    mode: SearchMode = "keyword",
) -> KeywordSearchBackend:
    if mode == "semantic":
        raise NotImplementedError(
            "Semantic search is a follow-up workstream; use mode='keyword' today."
        )
    if hub.dialect == "sqlite":
        return FTS5SearchBackend(hub, table)
    return BM25SearchBackend(hub, table)
```

The Postgres keyword backend (`BM25SearchBackend`) uses `@@@` with pg_search's query DSL and `pdb.score(id)` for scoring (legacy pg_search API; see §4.4 for the version-pin contract). Ranking behavior is **BM25 on both backends** (FTS5 `bm25()` on SQLite, pg_search BM25 on Postgres); exact scores differ because scoring parameters (k1, b) are implementation-specific, but representative-query top-N ordering must remain stable across backends.

The `mode` parameter holds the seam for a future Qdrant-backed `SemanticSearchBackend`. This plan does not implement it; the companion semantic-search workstream plugs in here without touching callers.

**Layer 2 — fused-ranking preservation:**

`src/gobby/memory/manager.py` runs a fan-out search that merges three signals via Reciprocal Rank Fusion: FTS5 keyword (`MemoryManager._fts_search` at `manager.py:840`), Qdrant vector search, and a Neo4j graph-augmented vector path gated by the `enable_graph_augmented_search` config flag (`config/persistence.py:91`). The merge runs through `MemoryManager._rrf_merge` and `_rrf_scores` (`manager.py:503–534`) with two RRF constants (`rrf_k` for keyword+vector, `neo4j_rrf_k` for the graph signal). All three inputs and both constants must survive the migration unchanged in semantic effect — only the keyword input swaps from FTS5 SQL to a `KeywordSearchBackend` call.

Concrete rewrites in `manager.py`:

- `_fts_search` becomes a thin wrapper over `pick_search_backend(hub, "memories").search(query, limit)`. The function signature stays the same; callers see no behavior change beyond the dialect-correct SQL.
- `_rrf_merge`, `_rrf_scores`, the Qdrant fan-out, and the Neo4j graph-augmented branch are not modified by this task. The intent is preservation, not refactor.

`tests/storage/test_dialect_parity.py` (defined in §2.4 as the cross-backend parity suite) gains fused-search cases that run a representative query against the `hub_db` parametrization and assert:

- `MemoryManager.search` top-N ID ordering matches between SQLite (FTS5 + Qdrant + Neo4j) and Postgres (pg_search + Qdrant + Neo4j) for representative queries spanning keyword-only, vector-only, graph-only, and combined-signal cases.
- Disabling `enable_graph_augmented_search` leaves keyword+vector ordering identical across backends.
- The `rrf_k` and `neo4j_rrf_k` constants reach `_rrf_scores` unchanged from `MemoryManager.__init__` after the keyword-backend swap.

**Acceptance:**

- 4.5.1 — Keyword search backends route through the `HubDatabase` seam, supporting SQLite (FTS5) and PostgreSQL (`pg_search`); the `mode="semantic"` parameter raises `NotImplementedError` to hold the seam for the companion semantic-search workstream. symbol: `gobby.storage.tasks._search.pick_search_backend`.
- 4.5.2 — `MemoryManager._fts_search` rewires to consume `KeywordSearchBackend.search` while `_rrf_merge`, `_rrf_scores`, the Qdrant fan-out, and the Neo4j graph-augmented branch (gated by `enable_graph_augmented_search`) keep their current contracts. `rrf_k` and `neo4j_rrf_k` reach `_rrf_scores` unchanged. symbol: `gobby.memory.manager.MemoryManager._fts_search`.
- 4.5.3 — Cross-backend fused-search parity tests cover keyword-only, vector-only, graph-only, and combined-signal representative queries; top-N ID ordering matches between SQLite and PostgreSQL for each case. file: `tests/storage/test_dialect_parity.py`.

### 4.6 Port remaining SQL (`json_extract`, `datetime`, `strftime`, `julianday`) [category: refactor] (depends: 4.2, 4.3, 3.8)
`kind: deliverable`

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

**Acceptance:**

- 4.6.1 — Remaining SQLite-specific SQL functions (`json_extract`, `datetime`, `strftime`, `julianday`) ported to PostgreSQL equivalents. file: `src/gobby/storage/`.

### 4.7 Audit PostgreSQL concurrency semantics under MVCC [category: research] (depends: 3.4, 3.5, 3.6, 3.7, 4.1, 4.3, 4.4, 4.5, 4.6)
`kind: deliverable`

Target: `docs/postgres-concurrency-audit.md` (new), `tests/storage/test_postgres_mvcc.py` (new), and every call site of `after_commit` callbacks or write path that assumes SQLite serialization (`after_commit`, `_run_after_commit_callbacks`, `savepoint()`, `conn.in_transaction`, read-modify-write updates).

For each callback site or concurrency-sensitive write path, document:

1. What the callback does.
2. Whether it reads database state — and if so, whether the reading session is the same as the writing session, a different session, or async.
3. Whether it mutates external state (files, network, in-memory structures).
4. Whether it performs a read-modify-write sequence that now needs
   `SELECT ... FOR UPDATE`, optimistic locking, or an atomic `UPDATE`.
5. Whether it assumes SQLite-style transaction visibility or immediate
   constraint failures.

Any callback that reads database state from a session other than the writing one is flagged as "requires explicit transaction boundary." Phase 4.7 does not stop at reporting: fix each such case by moving the affected read inside the originating transaction, by forcing a fresh `BEGIN` on the reading session before the read, or by moving the logic into a post-snapshot-safe transaction. Audit every `savepoint()` / `conn.in_transaction` usage that assumes SQLite semantics and remediate the broken code paths in the same phase.

Risk classification criteria:

- High risk: callback reads from a different session, expects uncommitted writes to be visible, and lacks an explicit transaction boundary.
- Medium risk: callback reads from a different session, but has a safe fallback when the read is stale.
- Low risk: callback only reads through the same session, or it only performs external side-effects after commit.

Deliverables:

- a short report committed under `docs/postgres-concurrency-audit.md`, referenced from the cutover runbook (6.1)
- remediation changes for every high-risk callback / transaction-boundary bug found by the audit
- remediation PRs for any read-modify-write, isolation, or constraint-timing assumptions found by the audit
- integration tests that open separate sessions under PostgreSQL MVCC and verify after-commit callbacks do not observe or expose stale / partial state across sessions

Required integration scenarios:

- callback spawns async work that reads from a pooled connection after commit; verify it sees only committed state
- callback runs while another session holds a long-running transaction; verify snapshot isolation and stale-read handling
- callback logic remains correct when wrapped in `savepoint()` / rollback flows that previously relied on SQLite `conn.in_transaction` semantics
- read-modify-write paths remain correct under concurrent writers
- code assuming immediate `SQLITE_CONSTRAINT` semantics remains correct when Postgres constraints are deferrable

Example test names:

- `test_workflow_audit_mvcc_safe`
- `test_after_commit_async_reader_uses_committed_state`
- `test_savepoint_callback_rollback_safe_with_pgbouncer`

Audit report template:

| Callback Site | Risk Level | Read-Modify-Write Risk | Isolation Assumption | Constraint Handling | Remediation | Test Coverage |
| --- | --- | --- | --- | --- | --- | --- |
| `TaskManager._notify_listeners` | Low | None | Same-session read only | None | No change required | `test_workflow_audit_mvcc_safe` |

Cutover is blocked until:

- the Phase 4.7 audit report has zero unresolved High or Medium items
- all remediation PRs identified by the audit are merged
- all new MVCC integration tests pass in CI for three consecutive runs
- no known broken `savepoint()` / `conn.in_transaction` usages remain
- **Re-audit gate**: after Phase 5 completes (the importer can introduce new transaction/savepoint/after-commit usage), 4.7's audit is re-run end-to-end against the integrated codebase. The cutover-blocking gate referenced by §6.1 is the **post-Phase-5 audit report version**, not the original. If Phase 5 (or any intervening task) introduces a new callback / read-modify-write / constraint-timing assumption that the original audit didn't cover, those items are added to the report and must clear the same High/Medium / remediation / CI thresholds before cutover. The re-audit is part of §6.0's setup work; it is not a separate task because the audit methodology and remediation playbook are already defined here.

**Acceptance:**

- 4.7.1 — Concurrency audit report committed; every High and Medium finding has a remediation PR merged and is covered by an MVCC integration test. file: `docs/postgres-concurrency-audit.md`.

## P5 Phase 5: One-shot SQLite → PostgreSQL migration tool
`kind: framing`

**Goal**: a single command imports an entire SQLite hub database into a fresh Postgres database, validates deterministically, and leaves SQLite untouched.

### 5.1 Implement `gobby postgres migrate-from-sqlite` [category: code] (depends: P4)
`kind: deliverable`

Target: `src/gobby/cli/postgres.py`, `src/gobby/storage/migration/sqlite_to_postgres.py` (new)

Command signature:

```
gobby postgres migrate-from-sqlite
    --source ~/.gobby/gobby-hub.db
    --target $DATABASE_URL
    [--batch-size 1000]
    [--dry-run]
```

`--resume` is **deliberately not supported in v1**. The importer runs as a single all-or-nothing transaction with `SET CONSTRAINTS ALL DEFERRED` so cyclical FKs validate atomically at commit; partial-progress checkpointing would either roll back with the data (no durable resume point) or commit progress on a separate connection (reporting tables complete while their data is still uncommitted and invisible to the validating session). Either path defeats the deferred-FK safety story. If a future workstream needs resumability, it must redesign around per-table commits + a separate validation/finalization phase, which is out of scope here. For now, a failed migration is restarted from scratch.

**Per-mode recovery on failed import.** SQLite source is never modified. Recovery on the Postgres target depends on install mode:

- **Docker mode**: `gobby postgres uninstall --remove-data` (tears down the compose profile and deletes the `gobby_postgres_data` + `gobby_pgaudit_log` named volumes) followed by `gobby postgres install`. Volume deletion is the only durable reset because the import transaction rollback leaves Postgres-side WAL state and pg_search index files behind that can survive a `down`/`up` cycle.
- **Native mode**: stop Postgres, delete the data directory the operator points us at (printed by `uninstall --remove-data`), restart, and re-run `gobby postgres install --mode native --dsn ...`. Native mode never auto-deletes a system-managed Postgres data directory; the operator runs the printed `rm -rf` themselves.
- **External mode**: requires the **Gobby ownership contract** documented in §1.2 — the operator commits at install time to a **dedicated database** (`POSTGRES_DB=gobby` recommended) that Gobby alone owns. Per-schema isolation against a shared host database is **not supported in v1**; see §1.2 "Ownership contract for external mode." Recovery is one of two equivalent forms inside that dedicated database, both safe by virtue of the dedicated-database contract:
  - `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` (preserves the database object, drops everything Gobby owns inside it), **or**
  - `DROP DATABASE gobby; CREATE DATABASE gobby;` if the operator has the privilege and prefers full reset.

  After either form, re-run `gobby postgres install --mode external --dsn ...` to recreate the `gobby_install_ownership` sentinel, then restart the import. **Do not use `DROP SCHEMA gobby` or any `--schema`-flavored reset** — those refer to a per-schema isolation path explicitly out of scope for v1.

Execution order:

1. Assert daemon is stopped (refuses otherwise).
2. Open SQLite source read-only (`file:<path>?mode=ro&immutable=1`).
3. Compare the SQLite source schema fingerprint / semantic schema version to the expected baseline and fail early if they differ.
3a. **External-mode preflight** (skip for `docker` / `native` modes): connect to the Postgres target and run `SELECT 1 FROM gobby_install_ownership WHERE key = 'singleton'`. If zero rows are returned, fail with the same missing-sentinel error used by `status` and `activate`: "external-ownership sentinel missing — was the database recreated or is this a different install? Re-run `gobby postgres install --mode external --dsn ...` to recreate the sentinel, then restart the import." This catches the case where an operator drops or recreates the external database between install and import; without this preflight, the importer would proceed against a target that no longer satisfies the ownership contract, defeating §1.2's design.
4. Open Postgres target and run `apply_migrations()` to create the schema.
5. Drop BM25 indexes created by the baseline schema so bulk load does not pay per-row index maintenance cost.
6. Begin a single import transaction and `SET CONSTRAINTS ALL DEFERRED`. Because the Postgres baseline uses deferrable FKs, this preserves real FK validation at `COMMIT` while still allowing cyclical references to load.
7. For each table in dependency-safe order, stream rows from SQLite and bulk-insert via psycopg v3 `copy()`:

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

8. Commit the import transaction. This is the FK-validation point: any deferred-constraint violation aborts the migration.
9. Recreate BM25 indexes. `CREATE INDEX ... USING bm25 ...` rebuilds from the just-loaded data. Validate index size and row count are non-zero.
10. Reseed sequences (task 5.3).
11. Run validation (task 5.2).
12. Insert a `('imported_from_sqlite_at', NOW()::text)` row into the `gobby_migration_state (key TEXT PRIMARY KEY, value TEXT)` table (created by the Postgres baseline, task 4.2). `gobby postgres activate` checks for that key before flipping `hub_backend`. A sentinel version in `schema_migrations` is avoided because that table is reserved for applied migration versions — mixing a "finished importing" marker with real version rows is brittle and blocks schema-version sanity checks later.

If a table fails mid-copy, the entire import transaction rolls back. The operator follows the per-mode recovery path documented above and reruns. **The `_migration_progress` table is removed from the design entirely** — without `--resume` it has no operational purpose, and any diagnostic state on the target is destroyed by the recovery reset anyway. Per-table progress for diagnostics is written to a local artifact at `~/.gobby/migrations/import-<timestamp>.log` (NDJSON, one record per table-copy start/end) instead, which survives the target reset and is what an operator wants for post-mortem analysis.

**Acceptance:**

- 5.1.1 — `gobby postgres migrate-from-sqlite` performs the one-shot data migration. symbol: `gobby.cli.postgres.migrate_from_sqlite`.

### 5.2 Implement validation checks [category: code] (depends: 5.1)
`kind: deliverable`

Target: `src/gobby/storage/migration/validation.py` (new)

Runs after bulk copy. All checks must pass for the migration command to exit 0.

- **Schema parity baseline**: before comparing data, verify the SQLite source schema fingerprint / semantic schema version matches the migration baseline expected by the importer. Fail early if the source schema is older/newer/drifted.
- **Postgres-only table exclusion list**: a small set of tables exists on the Postgres side without a SQLite counterpart by design and is excluded from every comparison below. Currently: `gobby_install_ownership` (external-mode ownership sentinel, §1.2), `gobby_migration_state` (cutover marker, §4.2), `schema_migrations` (applied versions). Validation iterates Postgres tables minus this list when building the comparison set; iterating SQLite tables and checking that each maps to a Postgres counterpart catches anything dropped in error.
- **Row counts**: `SELECT COUNT(*) FROM <t>` on both sides for every table in the comparison set; must match exactly.
- **FK integrity**: primarily validated by the commit of the deferred-constraint import transaction in step 5.1.7. Validation also runs explicit orphan checks generated from `pg_constraint` metadata so we do not trust transaction success alone.
- **Content hashes**: for a representative set of tables (`sessions`, `tasks`, `memories`, `config_store`, `code_symbols`, `agents`, `metrics`, workflow audit), compute an order-independent hash of canonical JSON-encoded rows on both sides and compare. Order-independence is achieved by sorting by primary key before hashing; JSON encoding uses sorted keys.
- **Sequence reseed**: for every identity column, `SELECT last_value FROM <sequence>` must equal `MAX(id) + 1` (or `MAX(id)` if the sequence `is_called=false`).
- **BM25 index coverage**: for each FTS-replacement table, confirm the BM25 index exists (`pg_class` lookup), was populated (`pg_stat_user_indexes.idx_tup_read > 0` after a smoke query), and returns non-zero hits on a canned query built from a random sampled row's searchable content. This catches silent index-build failures that pg_search can in principle produce on malformed input.
- **CHECK constraints**: enumerate CHECK constraints from `pg_constraint`, evaluate `NOT (<check_expression>)` counts per table, and require zero violations. Emit `✓` / `✗` lines and include sampled failing rows on error.
- **UNIQUE constraints**: enumerate UNIQUE constraints, run `GROUP BY ... HAVING COUNT(*) > 1` checks for each constrained key set, and require zero duplicates. Emit `✓` / `✗` lines and include sampled failing groups on error.
- **NOT NULL columns**: enumerate NOT NULL columns and count `NULL` rows per column. Require zero. Emit `✓` / `✗` lines and include sampled failing rows on error.

Output format: one line per check with `✓` / `✗`, plus a summary JSON artifact written to `~/.gobby/migrations/validate-<timestamp>.json` for auditing. The same artifact carries failing row samples for any CHECK / UNIQUE / NOT NULL failure.

**Acceptance:**

- 5.2.1 — Migration validation checks compare row counts and content invariants between SQLite and PostgreSQL. file: `src/gobby/storage/migration/validation.py`.

### 5.3 Implement sequence / identity reseed [category: code] (depends: 5.1)
`kind: deliverable`

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

**Acceptance:**

- 5.3.1 — Sequence/identity reseed runs after data migration so PG identities continue from the SQLite max. file: `src/gobby/storage/migration/reseed.py`.

## P6 Phase 6: Cold cutover to PostgreSQL runtime
`kind: framing`

**Goal**: flip the daemon to Postgres with a documented rollback window and zero tolerance for silent failures.

### 6.0 Implement validation-window audit log (Docker mode only, v1) [category: code] (depends: P5)
`kind: deliverable`

Target: `src/gobby/data/postgres-pgsearch/Dockerfile`, `src/gobby/data/postgres-pgsearch/initdb.d/02-pgaudit.sql` (new), `src/gobby/data/postgres-pgsearch/scripts/pg_audit_export.sh` (new), `src/gobby/data/docker-compose.services.yml`, `docs/runbooks/postgres-cutover.md`

Chosen technology: `pgAudit` for the validation window. It minimizes app-side
changes and keeps write capture inside PostgreSQL rather than building a
parallel application middleware path.

**Scope is Docker mode only for v1.** pgAudit provisioning, healthchecks, and runbook tooling are wired into the Gobby-controlled Docker image (§1.4) and compose baseline (§1.1) — the only install path where Gobby controls the runtime environment end-to-end. Native and `--mode external` operators must take responsibility for their own write capture during the validation window (or accept no-rollback risk); they do **not** get pgAudit out-of-the-box from this plan, and `gobby postgres activate` does not gate on pgAudit presence in those modes (see install-mode dispatch below).

Why narrow rather than expand: adding pgAudit to native (Debian: `postgresql-17-pgaudit` apt package; macOS: source build with `pg_config`) and external (probe + fail-closed runbook for managed-Postgres operators) would double the surface of §6.0 and double the test matrix without changing Docker mode's safety story. A future workstream can add native/external coverage if usage justifies it.

Requirements (Docker mode):

- Add `pgaudit` to the §1.4 Dockerfile build. It is a standard PostgreSQL extension bundled with `postgresql-contrib` on Debian/Ubuntu base layers, so the install reduces to one extra `apt-get install postgresql-17-pgaudit` line in the Dockerfile.
- Initialize the extension and the dedicated probe table on first boot. The Dockerfile copies `src/gobby/data/postgres-pgsearch/initdb.d/02-pgaudit.sql` into `/docker-entrypoint-initdb.d/`. Contents:

    ```sql
    CREATE EXTENSION IF NOT EXISTS pgaudit;

    -- Audit-only probe row. Created here so the §6.0 healthcheck has a
    -- stable target on every boot — independent of the app schema, which
    -- doesn't exist until the §3.7 migration runner first runs and which
    -- isn't created at all on freshly-built containers before the daemon
    -- has connected. Updating this row generates an `AUDIT: ... UPDATE`
    -- entry in the pgAudit log, which is what the live-capture probe
    -- reads back.
    CREATE TABLE IF NOT EXISTS _pgaudit_probe (
        id              INTEGER PRIMARY KEY,
        last_probed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    INSERT INTO _pgaudit_probe (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;
    ```

    The official Postgres image auto-runs each SQL file under `/docker-entrypoint-initdb.d/` against the `POSTGRES_DB` database during `initdb`. The pg_search extension is initialized by a sibling SQL file added in §1.4 (`01-pg_search.sql`); the numeric prefix on the pgaudit file pins it to run after pg_search. The probe table sits in the application database (not in the app schema) and is harmless: `gobby postgres uninstall --remove-data` removes the data volume entirely; the §5.1 importer ignores it; the §3.7 migration runner ignores it because it isn't versioned.
- Surface the runtime knobs explicitly in the §1.1 compose service `command:` so the runtime config and the audit-sink contract are colocated:

    ```yaml
    command:
      - postgres
      - -c
      - shared_preload_libraries=pg_search,pgaudit   # pg_search must remain per §1.4
      - -c
      - pgaudit.log=write
      - -c
      - pgaudit.log_catalog=off
      - -c
      - logging_collector=on
      - -c
      - log_destination=stderr
      - -c
      - log_directory=/var/log/pgaudit
      - -c
      - log_filename=pgaudit-%Y-%m-%d_%H%M%S.log
      - -c
      - log_rotation_age=1d
      - -c
      - log_rotation_size=0
      - -c
      - log_file_mode=0640
      - -c
      - log_min_messages=log                          # so AUDIT lines aren't filtered
    ```

  The standard PostgreSQL setting `log_min_messages=log` is required because pgAudit emits AUDIT entries at `LOG` severity; the default `warning` level would silently drop them. `pgaudit.log_catalog=off` keeps catalog lookups out of the validation-window log so the export step is bounded by application writes.

- Mount a named volume `gobby_pgaudit_log` at `/var/log/pgaudit` (declared alongside `gobby_postgres_data` in `data/docker-compose.services.yml`). The Dockerfile prepares the path at build time with `RUN mkdir -p /var/log/pgaudit && chown postgres:postgres /var/log/pgaudit && chmod 0750 /var/log/pgaudit` so the postgres user can write to it once the volume is mounted on first boot. Volume retention spans container restarts; `gobby postgres uninstall --remove-data` removes both `gobby_postgres_data` and `gobby_pgaudit_log` together (already wired in §1.2).
- Healthcheck (composed into §1.1's existing `pg_isready` check) probes all four invariants in sequence; failure leaves the container `unhealthy` so `gobby postgres activate` cannot proceed:
    1. `psql -tAc 'SELECT 1 FROM pg_extension WHERE extname=$$pgaudit$$'` returns exactly `1` (dollar-quoted literal so the inner string can pass through compose YAML quoting without escaping).
    2. `psql -tAc 'SHOW pgaudit.log'` returns `write`.
    3. `test -d /var/log/pgaudit && find /var/log/pgaudit -name 'pgaudit-*.log' -size +0c | head -n1` finds at least one non-zero log file owned by `postgres` with mode `0640` (verified with `stat -c '%U %a' <file>`).
    4. Live-capture probe: `psql -c 'UPDATE _pgaudit_probe SET last_probed_at = NOW() WHERE id = 1 RETURNING last_probed_at;'` followed by reading the newest `pgaudit-*.log` file and matching the last line against the regex `LOG:  AUDIT: SESSION,.*UPDATE`. The probe runs against the dedicated `_pgaudit_probe` row seeded by `02-pgaudit.sql` during `initdb`, so it works from container first boot — independent of `gobby_install_ownership` or any other application table that doesn't exist until §5.1's import runs.
- Runbook commands shipped in `docs/runbooks/postgres-cutover.md`:
  - `docker exec gobby-postgres ls -lh /var/log/pgaudit/` — confirm log files exist and are growing.
  - Validation-window export: a small `pg_audit_export.sh` helper script ships in `src/gobby/data/postgres-pgsearch/scripts/`, takes `--start <iso8601>` and `--end <iso8601>` arguments, and emits all `AUDIT:` lines in the window to stdout for archival before deactivation. The runbook calls it as `docker exec gobby-postgres /usr/local/bin/pg_audit_export.sh --start <activated_at> --end <deadline_at>`. The `<activated_at>` and `<deadline_at>` values come from the `cutover-<timestamp>.json` ticket.
  - Live tail: a single-line recipe shipped in the runbook tails the newest `pgaudit-*.log` file under `/var/log/pgaudit` to confirm capture is live before `gobby postgres activate`.

Install-mode dispatch:

- **Docker mode**: `gobby postgres activate` blocks unless pgAudit is loaded (`SELECT 1 FROM pg_extension WHERE extname = 'pgaudit'`) AND the audit log is writable (probe write + read back). No flags required — Docker mode owns the capture mechanism.
- **Native mode** and **external mode**: `gobby postgres activate` requires **one of two structured flags** (mutual exclusion, neither default):
    - `--capture-sink <type>:<location>` where `<type>` is one of exactly two values — `pgaudit-file` (location is an absolute path the sink writes to; activator runs file existence + write-test) or `wal-archive` (location is a DSN-style spec for an archive endpoint; activator checks `pg_replication_slots` for a matching slot). A `custom` escape hatch is **deliberately not provided** — it would re-introduce the click-through risk R2-F5 closed by allowing any sink with no probe. Operators with bespoke capture mechanisms must use `--accept-no-rollback-risk` and document their mechanism externally.
    - `--accept-no-rollback-risk` requires the operator to type the literal phrase `I accept no-rollback risk` at a confirmation prompt (no `--yes` bypass).
- The `_active_install_mode()` helper is what gates which flag set is required; Docker mode rejects either flag with "not applicable in docker mode — pgAudit is the gate."
- **Every** successful activation — Docker, native, or external — emits the cutover-ticket artifact at `~/.gobby/migrations/cutover-<timestamp>.json` after mode-specific gates pass. The artifact has one stable schema (see "Cutover-ticket artifact schema" below) so §6.1 step 11 (validation-window deadline tracking) and §6.2 step 2 (rollback export) can parse it the same way regardless of mode. This makes the runbook branch a structured choice that survives across mode boundaries, not a Docker-vs-non-Docker fork.

**Cutover-ticket artifact schema** (`~/.gobby/migrations/cutover-<timestamp>.json`) — duplicated here for runbook context. **§1.5 is the authoritative producer spec** including the `_build_cutover_ticket()` parameter-to-field mapping table; if the two ever drift, §1.5 wins and this block is the bug:

```json
{
  "mode": "docker" | "native" | "external",
  "activated_at":   "<ISO 8601 UTC>",
  "deadline_at":    "<ISO 8601 UTC, activated_at + 48h>",
  "gobby_version":  "<semver>",
  "capture_kind":   "pgaudit-managed" | "pgaudit-file" | "wal-archive" | "none",
  "capture_value":  "<path | dsn | null>",
  "verification": {
    "state":      "ok" | "operator-attested",
    "probed_at":  "<ISO 8601 UTC | null>",
    "probe_detail": "<stringified result from the writability probe>"
  },
  "acknowledgement": {                           // present only when capture_kind = "none"
    "phrase":     "I accept no-rollback risk",
    "operator":   "<whoami output>",
    "asked_at":   "<ISO 8601 UTC>"
  }
}
```

`capture_kind="pgaudit-managed"` is the Docker branch (pgAudit lives inside Gobby's image, no sink path needed; `verification.state="ok"` with the pgAudit healthcheck output in `probe_detail`). `capture_kind="pgaudit-file"` and `wal-archive` are the native/external structured branches with a probed sink. `capture_kind="none"` is `--accept-no-rollback-risk`; verification is `operator-attested` and the `acknowledgement` block is required.

Alternatives considered but not chosen for v1:

- WAL logical decoding
- trigger-backed `_audit` tables
- application-level middleware capture
- expanding pgAudit to native/external (deferred to future work)

Cutover in Docker mode remains blocked unless validation-window write capture is live and
observable. Cutover in native/external mode proceeds with an explicit operator acknowledgement
that rollback safety is reduced.

**Acceptance:**

- 6.0.1 — Validation-window audit log captures cutover-period writes for retroactive reconciliation (Docker mode v1). file: `src/gobby/data/postgres-pgsearch/Dockerfile`.

### 6.1 Cutover runbook [category: docs] (depends: 6.0)
`kind: deliverable`

Target: `docs/runbooks/postgres-cutover.md` (new)

> Warning: once `gobby postgres activate` runs, Postgres becomes the live write target for the validation window. If rollback is required, writes made in Postgres during that validation window are at risk and must be captured before deactivation.

Step-by-step:

1. Announce cutover, schedule window.
2. `gobby stop`.
3. Back up `~/.gobby/gobby-hub.db` to a dated path; record the SHA-256 for later verification.
4. `gobby postgres install` if not already installed.
5. `gobby postgres migrate-from-sqlite --source ~/.gobby/gobby-hub.db --target $DATABASE_URL`.
6. Verify the validation output exits 0, then read the canonical completion marker out of the structured status output:

   ```bash
   gobby postgres status --json | jq -e '.migration_complete.present == true'
   ```

   This grep-and-fail-fast contract is the exact reason `gobby postgres status --json` exists (see §1.2 — the field name `migration_complete.present` is stable). If the assertion fails, `gobby postgres activate` will refuse anyway (§1.5 calls the same `_postgres_migration_complete()` helper), but failing here is louder and gives the operator the actual `gobby_migration_state` row content to look at via `... | jq '.migration_complete'`. Do **not** proceed to step 7 until this assertion passes.

   For external mode, also assert the ownership sentinel is intact:

   ```bash
   gobby postgres status --json | jq -e '.ownership.sentinel_present == true'
   ```

   If this fails, the database has been recreated since install and `activate` will refuse with the same missing-sentinel error.
7. Enable validation-window write capture on the Postgres target before activation:
   - **Docker mode**: the `pgAudit`-backed append-only audit log from §6.0 must be live and observable. The activator probes the audit log automatically; no operator flag is required. If the probe fails, activation is blocked.
   - **Native / external mode**: pass one of the two structured flags from §6.0 install-mode dispatch — `--capture-sink <type>:<location>` where `<type>` is exactly `pgaudit-file` or `wal-archive` (operator-wired capture; sink is probed and recorded) or `--accept-no-rollback-risk` (typed-phrase confirmation; recorded with operator + timestamp). A generic `--yes` bypass is intentionally not provided, and `custom:` is intentionally not supported (use `--accept-no-rollback-risk` and document your mechanism externally).
   - **All modes** emit the cutover-ticket artifact at `~/.gobby/migrations/cutover-<timestamp>.json` after gates pass. The artifact's path is printed at the end of `activate` and must be attached to the cutover ticket. §6.2 reads it to choose the rollback export branch.
8. `gobby postgres activate`.
9. `gobby start`.
10. Run the smoke suite: `gobby status`, `gobby sessions list`, `gobby tasks list`, `gobby memory search "foo"`, `gobby code search "bar"`. Each must return expected data within expected latency.
11. Announce cutover complete; the validation window starts now. The maximum validation window is 48h from `gobby postgres activate` — `deadline_at` in the cutover-ticket artifact emitted at activation. If unresolved blocking regressions remain at that deadline, roll back instead of extending the window silently. The deadline is what `gobby postgres status` should be enhanced to surface as a warning when it approaches (followup; not blocking for v1).

Explicit watch-list for the validation window:

- MVCC-driven callback regressions (see the Phase 4.7 audit report)
- search result ordering drift on representative queries
- latency regressions > 2× baseline on storage-bound endpoints
- health of the `pgAudit` append-only write log enabled in step 7

Do not enter the validation window until the Phase 4.7 callback remediation gate is green.

**Acceptance:**

- 6.1.1 — Cutover runbook documented end-to-end. file: `docs/runbooks/postgres-cutover.md`.

### 6.2 Rollback runbook [category: docs] (depends: 6.1)
`kind: deliverable`

Target: `docs/runbooks/postgres-rollback.md` (new)

When to roll back: any validation-window regression that cannot be fixed forward within 2h, OR any detected data corruption.

Steps:

1. `gobby stop`.
2. Export all Postgres-side writes made during the validation window to a safe artifact before flipping `hub_backend` back. Read the cutover-ticket artifact at `~/.gobby/migrations/cutover-<timestamp>.json` first — its `capture_kind` field selects the export path:
   - **`capture_kind="pgaudit-managed"`** (Docker mode): export from the `pgAudit` append-only audit log inside Gobby's container (§6.0). Filter by `activated_at` from the same ticket. Supplement with targeted `pg_dump` / SQL exports for tables that support `updated_at` filtering.
   - **`capture_kind="pgaudit-file"`** (native/external with operator-wired pgAudit): export from the path recorded in `capture_value`. Same `activated_at` window filter.
   - **`capture_kind="wal-archive"`** (native/external with WAL archiving): export from the archive endpoint at `capture_value` for the timestamp window. The operator's runbook for their archive product is what tells you the exact export command.
   - **`capture_kind="none"`** (native/external with `--accept-no-rollback-risk`): there is no auto-capture. Validation-window writes are forensic-only via `updated_at` filtering on tables that have it (best-effort) and the operator is expected to restore from the pre-cutover SQLite backup. The rollback ticket must include the cutover ticket's `acknowledgement` block so the audit chain is intact.
3. `gobby postgres deactivate` (flips `hub_backend=sqlite`).
4. The pre-cutover SQLite database is untouched; no restore needed if the rollback happens inside the validation window.
5. `gobby start`.
6. Attach the validation-window export artifact to the rollback / post-mortem task for forensic analysis and any later partial-merge work.
7. File a task to re-migrate after the blocking regression is fixed.

Explicit data-loss rule: writes made to Postgres during the validation window are at risk on rollback and are not merged back into SQLite automatically. The export step above exists for forensic analysis and potential partial-merge tooling later, not for automatic recovery. If the validation window closes without rollback, a later rollback requires a reverse migration (Postgres → SQLite) which is explicitly out of scope for this plan.

**Acceptance:**

- 6.2.1 — Rollback runbook documented end-to-end. file: `docs/runbooks/postgres-rollback.md`.

## P7 Phase 7: Remove SQLite runtime support
`kind: framing`

**Goal**: stop carrying dual-backend complexity once Postgres has proven stable in production.

### 7.0 Move bootstrap Postgres credentials into OS keyring [category: code] (depends: 6.2)
`kind: deliverable`

Target: `src/gobby/config/bootstrap.py`, `~/.gobby/bootstrap.yaml`, secret-store / keyring integration, startup validation

- replace inline `database_url` storage in `bootstrap.yaml` with a keyring-backed
  reference before migration cleanup is considered complete
- migrate existing plaintext `database_url` entries into the OS keyring
- fail startup if `bootstrap.yaml` permissions are broader than `0600`
- document operator rollback behavior for both the overlap window and post-cutover
  steady state

Phase 7 is not complete until this keyring migration lands. Plaintext
`database_url` storage is an allowed cutover-window compromise, not the final
state.

**Acceptance:**

- 7.0.1 — Bootstrap Postgres credentials moved from disk to OS keyring. file: `src/gobby/config/bootstrap.py`.

### 7.1 Remove `SqliteHubDatabase` from runtime wiring [category: refactor] (depends: P6)
`kind: deliverable`

Target: `src/gobby/storage/hub/__init__.py`, `src/gobby/runner.py`, `src/gobby/config/bootstrap.py`

- Delete the `SqliteHubDatabase` class and all import sites.
- Remove the `hub_backend` branch from `runner_init()`; Postgres becomes the only runtime path.
- Keep `hub_backend` as a bootstrap field for parse compatibility, but emit a warning when set to `sqlite` and then raise.
- Remove the `_pg_to_sqlite_params` shim and the placeholder-translation regex.
- Remove every manager branch of the form `if hub.dialect == "sqlite": ...`. After this task, `dialect` as a dispatch key is no longer used in runtime code (it survives in the migration tool, task 7.2).

**Acceptance:**

- 7.1.1 — `SqliteHubDatabase` removed from runtime wiring; `PostgresHubDatabase` is the only runtime backend. file: `src/gobby/runner.py`.

### 7.2 Remove FTS5 runtime code and SQLite-specific migrations [category: refactor] (depends: 7.1)
`kind: deliverable`

Target: `src/gobby/storage/baseline_schema.sql`, `src/gobby/storage/migrations/*.sqlite.sql`, `src/gobby/storage/migrations/*.postgres.sql`, `src/gobby/search/fts5.py`, `src/gobby/storage/tasks/_search.py`

- Delete `src/gobby/storage/baseline_schema.sql`.
- Delete the legacy SQLite migration files under `src/gobby/storage/migrations/` (the dialect-specific files with the `.sqlite.sql` suffix) that have a Postgres counterpart with the `.postgres.sql` suffix.
- Delete `FTS5SearchBackend` and all `MATCH` / `bm25(...)` SQL.
- Keep only the code path required for `migrate-from-sqlite` to read a legacy SQLite database via stdlib `sqlite3`. That path does not touch Gobby's storage layer.

**Acceptance:**

- 7.2.1 — FTS5 runtime code and SQLite-specific migrations removed. file: `src/gobby/storage/`.

### 7.3 Update docs, comments, and user-facing text [category: docs] (depends: 7.2)
`kind: deliverable`

Target: `CLAUDE.md`, `README.md`, `docs/`, in-code comments referencing SQLite as the hub database

- Replace SQLite references with Postgres where they describe the hub database.
- Note that `gobby postgres migrate-from-sqlite` remains available for users importing legacy databases.
- Update the "Common Issues" table in CLAUDE.md.
- Update any "Key File Locations" tables that list `~/.gobby/gobby-hub.db` — after this phase the runtime hub is reached via Postgres `database_url` / bootstrap config, not a local file path.

**Acceptance:**

- 7.3.1 — Docs, comments, and user-facing text updated to reflect PostgreSQL-only runtime. file: `README.md`.

## CLI and Interface Changes
`kind: framing`

New commands:

- `gobby postgres install [--mode {docker,native,external}] [--dsn <url>]` — default `docker`
- `gobby postgres status`
- `gobby postgres uninstall` — uses `postgres_install_mode` from bootstrap to pick the teardown path
- `gobby postgres migrate-from-sqlite`
- `gobby postgres activate` / `deactivate`

Bootstrap fields:

- `hub_backend` (new; `sqlite` | `postgres`)
- `database_url` (new; psycopg v3 DSN)
- `postgres_install_mode` (new; `docker` | `native` | `external`; recorded by `install`, read by `uninstall` and `status`)
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
`kind: framing`

- PostgreSQL installs via `gobby postgres install` with the same ergonomics as the Qdrant / Neo4j installers, across all three install modes (`docker`, `native`, `external`).
- The Docker mode uses a local-build compose `build:` directive — no Gobby-published Postgres image, no GHCR push. Gobby ships the Dockerfile; the user's machine builds the image and pulls `pg_search.deb` from upstream ParadeDB releases at build time.
- Native mode (Debian/Ubuntu) auto-installs `pg_search` from the same upstream `.deb`. Native mode (other Linux / macOS) prints platform-specific guidance and exits with a clear "use `--mode docker` (recommended)" message.
- External mode (`gobby postgres install --mode external --dsn <url>`) skips compose entirely, runs the strictly read-only probe phase (`pg_namespace` + object-catalog ownership checks plus `SELECT 1 FROM pg_extension WHERE extname='pg_search'`), and on success writes the `gobby_install_ownership` sentinel + bootstrap fields. Gobby never runs `CREATE EXTENSION` against the operator's database; failure to find pg_search exits non-zero with the manual install command for the user's platform, leaving the target byte-identical to its pre-probe state.
- Bootstrap selects the hub backend before DB-backed config loads; incorrect combinations are rejected with clear error messages.
- The daemon boots and runs against PostgreSQL without opening any SQLite file.
- `gobby postgres migrate-from-sqlite` imports an existing hub database with deterministic row-count, FK integrity, content-hash, and sequence reseed checks — all exit 0.
- Search behavior on tasks, memories, skills, and code index uses BM25 ranking on both SQLite (FTS5) and Postgres (pg_search). Representative-query top-N ordering matches across backends during the overlap and continues to return expected results post-cutover.
- The local-build Dockerfile passes `pg_isready` and `CREATE EXTENSION pg_search` smoke tests in CI on every PR that touches it. Pin updates (PG_SEARCH_VERSION + SHA256) are gated on those smoke tests and a green search test suite.
- The test suite runs against PostgreSQL via compose + `DATABASE_URL` with schema-per-xdist-worker plus test-scoped schema reset isolation; every Phase 3–5 task is covered by tests running against the real backend.
- All **post-baseline** migration files are pure `.sql` with `$1`-style placeholders. The five SQLite-only FTS5 setup helpers in `migration_helpers.py` persist through the overlap window as SQLite-specific scaffolding and are deleted in Phase 7.2 with the rest of the SQLite runtime. Runtime DSN bootstrap is explicit and pool/connection tuning is env-var-driven.
- Fresh installs initialize PostgreSQL directly; `~/.gobby/gobby-hub.db` is not created.
- Phase 7 migrates bootstrap Postgres credentials out of plaintext `bootstrap.yaml`
  into OS keyring storage, and startup fails if `bootstrap.yaml` is not `0600`
- Phase 7 removes `SqliteHubDatabase`, FTS5 code paths, and related shims; the codebase has one runtime backend.

## Assumptions
`kind: framing`

- Scope is the full hub database, not a partial migration.
- Docker mode is the recommended install path. PostgreSQL runs in the same compose project as Qdrant and Neo4j; each stays in its own container. Native and external modes exist for users who can't or won't run Docker, and are tested on Debian/Ubuntu (native) and against ad-hoc DSNs (external) but not at the same depth as Docker mode.
- Raw SQL remains the storage implementation style for this migration.
- There are no external users, so a cold cutover is preferable to dual-write rollout complexity.
- The compatibility layer (`SqliteHubDatabase`, dialect branches) is temporary scaffolding removed in Phase 7.
- Qdrant and Neo4j remain supporting stores; they are not replaced by PostgreSQL as part of this work.
- The Python codebase will be ported to Rust in a later effort. This plan biases toward choices that survive the port unchanged.
- Phase 0 ships and reaches users before Phase 1 starts. Users on databases at any version in the `[239, 244)` band reach v244 by chained migration through the v240/v241/v242 callables and the v244 no-op marker that Phase 0 leaves in `MIGRATIONS`; users below v239 hit `MigrationUnsupportedError` and must reset or recover from backup. Anyone already at v244 (fresh installs from the post-Phase-0 build) is no-op.

