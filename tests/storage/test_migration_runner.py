from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Lock
from types import MethodType
from typing import Any

import pytest

from gobby.storage.migrations import Migration, MigrationUnsupportedError

pytestmark = pytest.mark.unit


def _migration_module() -> Any:
    return importlib.import_module("gobby.storage.migrations")


def _split(sql: str) -> list[str]:
    module = _migration_module()
    return [
        statement.strip()
        for statement in module._split_statements_respecting_dollar_quotes(sql)
        if statement.strip()
    ]


class _Result:
    def __init__(self, rows: Iterable[dict[str, Any]] = ()) -> None:
        self._rows = list(rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _PostgresMigrationHub:
    dialect = "postgres"

    def __init__(self) -> None:
        self.tables = {"schema_migrations"}
        self.applied: list[int] = []

    @contextmanager
    def transaction(self) -> Iterator[_PostgresMigrationHub]:
        yield self

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        if "to_regclass" in sql:
            return _Result([{"table_exists": params[0] in self.tables}])
        if "pg_advisory_xact_lock" in sql:
            return _Result()
        if "WHERE version = %s" in sql:
            rows = [{"version": params[0]}] if params[0] in self.applied else []
            return _Result(rows)
        raise AssertionError(f"unexpected query: {sql}")


def test_postgres_pending_migration_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    module = _migration_module()
    hub = _PostgresMigrationHub()
    runner = module.MigrationRunner(hub)
    migration = Migration(
        version=295,
        name="add_needed_column",
        path=Path("unused.sql"),
    )

    def ensure_schema_migrations_table(self: Any) -> None:
        return None

    def read_applied_versions(self: Any) -> set[int]:
        return set()

    def discover_migrations(self: Any) -> list[Migration]:
        return [migration]

    def run_migration(self: Any, txn: _PostgresMigrationHub, discovered: Migration) -> None:
        assert txn is hub
        assert discovered is migration

    def record_applied_version(self: Any, txn: _PostgresMigrationHub, version: int) -> None:
        assert txn is hub
        hub.applied.append(version)

    runner._ensure_schema_migrations_table = MethodType(ensure_schema_migrations_table, runner)
    runner._read_applied_versions = MethodType(read_applied_versions, runner)
    runner._discover_migrations = MethodType(discover_migrations, runner)
    runner._is_non_transactional = MethodType(lambda self, item: False, runner)
    runner._run_migration = MethodType(run_migration, runner)
    runner._record_applied_version = MethodType(record_applied_version, runner)

    with caplog.at_level("INFO", logger="gobby.storage.migrations"):
        runner.apply_pending()

    assert hub.applied == [295]
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Applying PostgreSQL migration"
    )
    assert record.levelname == "INFO"
    assert record.__dict__["migration_version"] == 295
    assert record.__dict__["migration_name"] == "add_needed_column"


class _ConcurrentMigrationTransaction:
    def __init__(self, hub: _ConcurrentMigrationHub) -> None:
        self._hub = hub
        self._locked = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        if "SELECT version FROM schema_migrations WHERE" in sql:
            assert self._locked
            version = params[0]
            rows = [{"version": version}] if version in self._hub.applied else []
            return _Result(rows)
        if sql == "SELECT version FROM schema_migrations":
            rows = [{"version": version} for version in self._hub.applied]
            self._hub.initial_reads.wait()
            return _Result(rows)
        if "pg_advisory_xact_lock" in sql:
            assert "hashtext(current_schema())" in sql
            self._hub.advisory_lock.acquire()
            self._locked = True
            self._hub.lock_acquisitions += 1
            return _Result()
        if "INSERT INTO schema_migrations" in sql:
            assert self._locked
            self._hub.applied.add(params[0])
            return _Result()
        raise AssertionError(f"unexpected query: {sql}")

    def close(self) -> None:
        if self._locked:
            self._hub.advisory_lock.release()


class _ConcurrentMigrationHub:
    dialect = "postgres"

    def __init__(self) -> None:
        self.advisory_lock = Lock()
        self.initial_reads = Barrier(2)
        self.applied: set[int] = set()
        self.lock_acquisitions = 0
        self.migration_runs = 0

    @contextmanager
    def transaction(self) -> Iterator[_ConcurrentMigrationTransaction]:
        txn = _ConcurrentMigrationTransaction(self)
        try:
            yield txn
        finally:
            txn.close()


def test_apply_pending_serializes_concurrent_migrators_and_rechecks_version() -> None:
    module = _migration_module()
    hub = _ConcurrentMigrationHub()
    migration = Migration(version=321, name="concurrent", path=Path("unused.sql"))
    runners = [module.MigrationRunner(hub), module.MigrationRunner(hub)]

    for runner in runners:
        runner._ensure_schema_migrations_table = MethodType(lambda self: None, runner)
        runner._discover_migrations = MethodType(lambda self: [migration], runner)
        runner._is_non_transactional = MethodType(lambda self, item: False, runner)

        def run_migration(
            self: Any, txn: _ConcurrentMigrationTransaction, discovered: Migration
        ) -> None:
            assert discovered is migration
            assert hub.advisory_lock.locked()
            hub.migration_runs += 1

        runner._run_migration = MethodType(run_migration, runner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(runner.apply_pending) for runner in runners]
        for future in futures:
            future.result(timeout=5)

    assert hub.lock_acquisitions == 2
    assert hub.migration_runs == 1
    assert hub.applied == {321}


def test_postgres_migration_discovery_reports_invalid_filenames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _migration_module()
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / ".gitkeep").touch()
    (migrations_dir / "321_valid.sql").touch()
    (migrations_dir / "invalid.sql").touch()
    monkeypatch.setattr(module.importlib.resources, "files", lambda _package: tmp_path)

    with caplog.at_level("WARNING", logger="gobby.storage.migrations"):
        discovered = module.MigrationRunner(_PostgresMigrationHub())._discover_migrations()

    assert [(migration.version, migration.name) for migration in discovered] == [(321, "valid")]
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Ignoring invalid migration filename"
    )
    assert record.__dict__["migration_filename"] == "invalid.sql"
    assert ".gitkeep" not in caplog.text


@pytest.mark.parametrize("version", [295, 305])
def test_postgres_migration_discovery_rejects_reserved_versions(
    version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _migration_module()
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / f"{version}_reused_with_different_sql.sql").touch()
    monkeypatch.setattr(module.importlib.resources, "files", lambda _package: tmp_path)

    with pytest.raises(
        RuntimeError,
        match=rf"Migration v{version} reuses a version reserved by baseline v305",
    ):
        module.MigrationRunner(_PostgresMigrationHub())._discover_migrations()


def test_postgres_migration_discovery_rejects_duplicate_post_baseline_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _migration_module()
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "321_first.sql").touch()
    (migrations_dir / "321_second.postgres.sql").touch()
    monkeypatch.setattr(module.importlib.resources, "files", lambda _package: tmp_path)

    with pytest.raises(RuntimeError, match="Duplicate migration file for v321"):
        module.MigrationRunner(_PostgresMigrationHub())._discover_migrations()


def test_postgres_migration_discovery_finds_all_post_baseline_migrations() -> None:
    module = _migration_module()
    hub = _PostgresMigrationHub()
    runner = module.MigrationRunner(hub)

    discovered = runner._discover_migrations()

    known_prefix = [
        (306, "reconcile_live_hub_schema_drift"),
        (307, "cron_run_scheduler_owner"),
        (308, "recall_signal_hub"),
        (309, "github_triage_delivery_leases"),
        (310, "github_triage_build_dispatches"),
        (311, "model_costs_provider_key"),
        (312, "session_digest_pair_index"),
        (313, "memory_source_session_set_null"),
        (314, "memory_graph_retry_state"),
        (315, "session_title_synthesis_digest_hash"),
        (316, "memory_vector_reindex_state"),
        (317, "sync_tombstones"),
        (318, "chat_messages_sequence_unique"),
        (319, "worktree_last_activity"),
        (320, "projects_active_name_unique"),
        (321, "session_variables_session_cascade"),
        (322, "agent_run_capture_termination"),
        (323, "recall_usefulness_digest_shadow"),
        (324, "drop_sync_tombstones"),
        (325, "recall_usefulness_shadow_index"),
        (326, "validate_recall_usefulness_label_source"),
        (327, "failure_category_taxonomy"),
        (328, "memory_global_visibility"),
        (329, "memory_type_enum"),
        (330, "rename_epic_qa"),
        (331, "external_issue_sync_coordinator"),
        (332, "attention_states"),
        (333, "detection_manifests"),
        (334, "verification_receipts"),
        (335, "memories_dream_due_version"),
        (336, "model_metadata_rename"),
        (337, "verification_receipts_default"),
        (338, "plan_review_evidence"),
        (339, "expired_plan_review_round_retry"),
        (340, "tool_results"),
        (341, "digest_owned_session_titles"),
    ]
    actual = [(migration.version, migration.name) for migration in discovered]

    assert actual[: len(known_prefix)] == known_prefix
    assert [version for version, _ in actual] == list(range(306, 306 + len(actual)))


class _AutocommitMigrationState:
    def __init__(self) -> None:
        self.applied: set[int] = set()
        self.locked = False
        self.closed = 0
        self.unlocked = 0
        self.index_runs = 0
        self.index_valid: bool | None = None
        self.index_lookups: list[str] = []
        self.index_drops: list[str] = []


class _AutocommitMigrationConnection:
    def __init__(self, state: _AutocommitMigrationState) -> None:
        self.state = state

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        if "pg_advisory_lock" in sql and "unlock" not in sql:
            assert "hashtext(current_schema())" in sql
            assert not self.state.locked
            self.state.locked = True
            return _Result()
        if "pg_advisory_unlock" in sql:
            assert "hashtext(current_schema())" in sql
            assert self.state.locked
            self.state.locked = False
            self.state.unlocked += 1
            return _Result()
        if "SELECT version FROM schema_migrations WHERE" in sql:
            rows = [{"version": params[0]}] if params[0] in self.state.applied else []
            return _Result(rows)
        if "NOT index_state.indisvalid" in sql:
            assert "pg_catalog.format('%%I.%%I'" in sql
            assert params == ("idx_shadow",)
            self.state.index_lookups.append(params[0])
            rows = (
                [{"qualified_name": '"tenant;archive"."idx_shadow"'}]
                if self.state.index_valid is False
                else []
            )
            return _Result(rows)
        if sql.startswith("DROP INDEX CONCURRENTLY"):
            assert self.state.locked
            assert self.state.index_valid is False
            self.state.index_drops.append(sql)
            self.state.index_valid = None
            return _Result()
        if "CREATE INDEX CONCURRENTLY" in sql:
            assert self.state.locked
            if self.state.index_valid is None:
                self.state.index_runs += 1
                self.state.index_valid = True
            return _Result()
        if "INSERT INTO schema_migrations" in sql:
            self.state.applied.add(params[0])
            return _Result()
        raise AssertionError(f"unexpected query: {sql}")

    def close(self) -> None:
        self.state.closed += 1


def test_non_transactional_migration_retries_after_unrecorded_index_creation(
    tmp_path: Path,
) -> None:
    module = _migration_module()
    migration_path = tmp_path / "325_shadow_index.sql"
    migration_path.write_text(
        "-- gobby:non-transactional\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shadow ON recall_usefulness(id);\n"
    )
    migration = Migration(version=325, name="shadow_index", path=migration_path)
    state = _AutocommitMigrationState()

    def make_runner() -> Any:
        runner = module.MigrationRunner(
            _PostgresMigrationHub(),
            autocommit_connection=lambda: _AutocommitMigrationConnection(state),
        )
        runner._ensure_schema_migrations_table = MethodType(lambda self: None, runner)
        runner._read_applied_versions = MethodType(lambda self: set(state.applied), runner)
        runner._discover_migrations = MethodType(lambda self: [migration], runner)
        return runner

    first = make_runner()

    def fail_record_once(self: Any, txn: Any, version: int) -> None:
        raise RuntimeError("simulated crash after index creation")

    first._record_applied_version = MethodType(fail_record_once, first)
    with pytest.raises(RuntimeError, match="simulated crash"):
        first.apply_pending()

    make_runner().apply_pending()

    assert state.applied == {325}
    assert state.index_runs == 1
    assert state.index_drops == []
    assert state.index_lookups == ["idx_shadow", "idx_shadow"]
    assert state.unlocked == 2
    assert state.closed == 2


def test_non_transactional_migration_repairs_invalid_concurrent_index(
    tmp_path: Path,
) -> None:
    module = _migration_module()
    migration_path = tmp_path / "325_shadow_index.sql"
    migration_path.write_text(
        "-- gobby:non-transactional\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shadow ON recall_usefulness(id);\n"
    )
    migration = Migration(version=325, name="shadow_index", path=migration_path)
    state = _AutocommitMigrationState()
    state.index_valid = False
    runner = module.MigrationRunner(
        _PostgresMigrationHub(),
        autocommit_connection=lambda: _AutocommitMigrationConnection(state),
    )
    runner._ensure_schema_migrations_table = MethodType(lambda self: None, runner)
    runner._read_applied_versions = MethodType(lambda self: set(), runner)
    runner._discover_migrations = MethodType(lambda self: [migration], runner)

    runner.apply_pending()

    assert state.applied == {325}
    assert state.index_lookups == ["idx_shadow"]
    assert state.index_drops == ['DROP INDEX CONCURRENTLY IF EXISTS "tenant;archive"."idx_shadow"']
    assert state.index_runs == 1
    assert state.index_valid is True
    assert state.unlocked == 1
    assert state.closed == 1


def test_sync_tombstone_database_objects_are_removed(postgres_db: Any) -> None:
    row = postgres_db.fetchone(
        """
        SELECT
            to_regclass(current_schema() || '.sync_tombstones') AS tombstone_table,
            to_regprocedure(current_schema() || '.capture_sync_tombstone()') AS capture_function,
            (
                SELECT COUNT(*)
                FROM pg_trigger AS trigger
                JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE tgname IN (
                    'tasks_capture_sync_tombstone',
                    'memories_capture_sync_tombstone'
                )
                AND NOT tgisinternal
                AND namespace.nspname = current_schema()
            ) AS capture_trigger_count
        """
    )

    assert row is not None
    assert row["tombstone_table"] is None
    assert row["capture_function"] is None
    assert row["capture_trigger_count"] == 0


def test_memory_source_session_upgrade_preserves_memory(postgres_db: Any) -> None:
    """Migration 313 replaces a legacy restrictive FK with SET NULL."""
    schema = f"migration_313_{uuid.uuid4().hex}"
    migration_path = (
        Path(__file__).parents[2]
        / "src"
        / "gobby"
        / "storage"
        / "migrations"
        / "313_memory_source_session_set_null.sql"
    )
    session_id = str(uuid.uuid4())
    memory_id = str(uuid.uuid4())

    postgres_db.execute(f'CREATE SCHEMA "{schema}"')  # nosec B608 - generated UUID suffix
    try:
        with postgres_db.transaction() as conn:
            conn.execute(f'SET LOCAL search_path TO "{schema}"')  # nosec B608
            conn.execute("CREATE TABLE sessions (id UUID PRIMARY KEY)")
            conn.execute(
                """
                CREATE TABLE memories (
                    id UUID PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_session_id UUID REFERENCES sessions(id)
                )
                """
            )
            conn.execute("INSERT INTO sessions (id) VALUES (%s)", (session_id,))
            conn.execute(
                "INSERT INTO memories (id, content, source_session_id) VALUES (%s, %s, %s)",
                (memory_id, "Keep this memory", session_id),
            )
            for statement in _split(migration_path.read_text(encoding="utf-8")):
                conn.execute(statement)

            conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            row = conn.execute(
                "SELECT content, source_session_id FROM memories WHERE id = %s",
                (memory_id,),
            ).fetchone()

            assert row is not None
            assert row["content"] == "Keep this memory"
            assert row["source_session_id"] is None
    finally:
        postgres_db.execute(
            f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'  # nosec B608 - generated UUID suffix
        )


def test_memory_graph_retry_state_upgrade_backfills_pending_and_completed(
    postgres_db: Any,
) -> None:
    """Migration 314 backfills explicit graph queue state from the legacy boolean."""
    schema = f"migration_314_{uuid.uuid4().hex}"
    migration_path = (
        Path(__file__).parents[2]
        / "src"
        / "gobby"
        / "storage"
        / "migrations"
        / "314_memory_graph_retry_state.sql"
    )
    pending_id = str(uuid.uuid4())
    completed_id = str(uuid.uuid4())

    postgres_db.execute(f'CREATE SCHEMA "{schema}"')  # nosec B608 - generated UUID suffix
    try:
        with postgres_db.transaction() as conn:
            conn.execute(f'SET LOCAL search_path TO "{schema}"')  # nosec B608
            conn.execute(
                """
                CREATE TABLE memories (
                    id UUID PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    graph_processed BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            conn.execute(
                "INSERT INTO memories (id, graph_processed) VALUES (%s, FALSE), (%s, TRUE)",
                (pending_id, completed_id),
            )
            for statement in _split(migration_path.read_text(encoding="utf-8")):
                conn.execute(statement)

            rows = conn.execute(
                "SELECT id, graph_attempts, graph_status FROM memories ORDER BY id"
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}

            assert by_id[pending_id]["graph_attempts"] == 0
            assert by_id[pending_id]["graph_status"] == "pending"
            assert by_id[completed_id]["graph_attempts"] == 0
            assert by_id[completed_id]["graph_status"] == "completed"
    finally:
        postgres_db.execute(
            f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'  # nosec B608 - generated UUID suffix
        )


def test_split_statements_respecting_dollar_quotes_keeps_function_bodies_intact() -> None:
    statements = _split(
        """
        CREATE FUNCTION f() RETURNS void AS $$
        BEGIN
            PERFORM 1;
            PERFORM 2;
        END;
        $$ LANGUAGE plpgsql;
        SELECT 1;
        """
    )

    assert len(statements) == 2
    assert "PERFORM 1;\n            PERFORM 2;" in statements[0]
    assert statements[1] == "SELECT 1"


def test_split_statements_respecting_dollar_quotes_handles_nested_and_adjacent_tags() -> None:
    statements = _split(
        """
        SELECT $outer$begin $inner$still; text$inner$ end;$outer$;
        SELECT $tag1$a;b$tag1$ || $tag2$c;d$tag2$;
        SELECT $$empty-tag; body$$;
        """
    )

    assert len(statements) == 3
    assert "$inner$still; text$inner$" in statements[0]
    assert "$tag1$a;b$tag1$ || $tag2$c;d$tag2$" in statements[1]
    assert "$$empty-tag; body$$" in statements[2]


def test_split_statements_respecting_dollar_quotes_ignores_strings_and_comments() -> None:
    statements = _split(
        """
        SELECT 'can''t split; here';
        SELECT "odd;identifier";
        -- comment with a semicolon; and $tag$
        SELECT 1;
        /* block comment; with %s */
        SELECT '$$not a tag;$$';
        """
    )

    assert len(statements) == 4
    assert statements[0] == "SELECT 'can''t split; here'"
    assert statements[1] == 'SELECT "odd;identifier"'
    assert "SELECT 1" in statements[2]
    assert "SELECT '$$not a tag;$$'" in statements[3]


def test_split_statements_respecting_dollar_quotes_handles_escape_strings() -> None:
    statements = _split(r"SELECT E'escaped quote \'; still string'; SELECT 2;")

    assert statements == [r"SELECT E'escaped quote \'; still string'", "SELECT 2"]


def test_split_statements_respecting_dollar_quotes_ignores_mixed_contexts_inside_body() -> None:
    statements = _split(
        """
        CREATE FUNCTION noisy() RETURNS void AS $body$
        BEGIN
            RAISE NOTICE 'string; still body';
            -- comment; still body
            /* block; still body */
            PERFORM $$inner dollar; still body$$;
        END;
        $body$ LANGUAGE plpgsql;
        SELECT 'outside; string';
        """
    )

    assert len(statements) == 2
    assert "RAISE NOTICE 'string; still body';" in statements[0]
    assert "PERFORM $$inner dollar; still body$$;" in statements[0]
    assert statements[1] == "SELECT 'outside; string'"


def test_split_statements_respecting_dollar_quotes_handles_checked_in_postgres_ddl() -> None:
    sql = (
        Path(__file__).resolve().parents[2] / "src/gobby/storage/postgres_baseline_schema.sql"
    ).read_text()
    statements = _split(sql)

    function_statements = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("CREATE FUNCTION")
    ]

    assert function_statements
    assert any(
        "enforce_chat_attachments_bound_at_write_once" in statement
        and "RAISE EXCEPTION 'chat_attachments.bound_at is write-once';" in statement
        for statement in function_statements
    )
    assert any(
        "refresh_task_state_bucket_from_stage" in statement
        and "IF TG_OP = 'DELETE' THEN" in statement
        for statement in function_statements
    )


def test_migration_runner_rejects_non_postgres_hubs_after_cutover() -> None:
    class LegacyHub:
        dialect = "legacy"

    migration_module = _migration_module()

    with pytest.raises(MigrationUnsupportedError, match="only supports PostgreSQL"):
        migration_module.MigrationRunner(LegacyHub())
