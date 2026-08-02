from __future__ import annotations

import hashlib
import importlib
import json
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

    def record_applied_version(
        self: Any,
        txn: _PostgresMigrationHub,
        discovered: Migration,
    ) -> None:
        assert txn is hub
        hub.applied.append(discovered.version)

    runner._ensure_schema_migrations_table = MethodType(ensure_schema_migrations_table, runner)
    runner._read_applied_versions = MethodType(read_applied_versions, runner)
    runner._discover_migrations = MethodType(discover_migrations, runner)
    runner._is_non_transactional = MethodType(lambda self, item: False, runner)
    runner._is_destructive = MethodType(lambda self, item: False, runner)
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


def test_default_apply_halts_before_destructive_migration_after_safe_prefix(
    tmp_path: Path,
) -> None:
    module = _migration_module()
    hub = _PostgresMigrationHub()
    runner = module.MigrationRunner(hub)
    safe_path = tmp_path / "354_bookkeeping.sql"
    safe_path.write_text("SELECT 354;\n", encoding="utf-8")
    destructive_path = tmp_path / "355_drop_legacy.sql"
    destructive_path.write_text(
        "-- gobby:destructive\nDROP TABLE legacy_data;\n",
        encoding="utf-8",
    )
    migrations = [
        Migration(version=354, name="bookkeeping", path=safe_path),
        Migration(version=355, name="drop_legacy", path=destructive_path),
    ]
    executed: list[int] = []

    runner._ensure_schema_migrations_table = MethodType(lambda self: None, runner)
    runner._read_applied_versions = MethodType(lambda self: {353}, runner)
    runner._discover_migrations = MethodType(lambda self: migrations, runner)
    runner._is_non_transactional = MethodType(lambda self, item: False, runner)
    runner._run_migration = MethodType(
        lambda self, txn, migration: executed.append(migration.version),
        runner,
    )
    runner._record_applied_version = MethodType(
        lambda self, txn, migration: hub.applied.append(migration.version),
        runner,
    )

    with pytest.raises(
        MigrationUnsupportedError,
        match=r"gobby schema apply --destructive",
    ):
        runner.apply_pending()

    assert executed == [354]
    assert hub.applied == [354]


def test_retire_review_anchor_migration_is_classified_destructive() -> None:
    module = _migration_module()
    runner = module.MigrationRunner(_PostgresMigrationHub())

    migration = next(
        migration for migration in runner._discover_migrations() if migration.version == 361
    )

    assert migration.path.name == "361_retire_review_anchor.sql"
    assert runner._is_destructive(migration)


def test_fresh_schema_apply_may_cross_destructive_marker(tmp_path: Path) -> None:
    module = _migration_module()
    hub = _PostgresMigrationHub()
    runner = module.MigrationRunner(hub)
    migration_path = tmp_path / "355_drop_legacy.sql"
    migration_path.write_text(
        "-- gobby:destructive\nDROP TABLE legacy_data;\n",
        encoding="utf-8",
    )
    migration = Migration(version=355, name="drop_legacy", path=migration_path)
    executed: list[int] = []

    runner._ensure_schema_migrations_table = MethodType(lambda self: None, runner)
    runner._read_applied_versions = MethodType(lambda self: {354}, runner)
    runner._discover_migrations = MethodType(lambda self: [migration], runner)
    runner._is_non_transactional = MethodType(lambda self, item: False, runner)
    runner._run_migration = MethodType(
        lambda self, txn, discovered: executed.append(discovered.version),
        runner,
    )
    runner._record_applied_version = MethodType(
        lambda self, txn, discovered: hub.applied.append(discovered.version),
        runner,
    )

    runner.apply_pending(fresh_schema=True)

    assert executed == [355]
    assert hub.applied == [355]


def test_apply_pending_rejects_gap_in_bookkeeping_chain(tmp_path: Path) -> None:
    module = _migration_module()
    hub = _PostgresMigrationHub()
    runner = module.MigrationRunner(hub)
    migration_path = tmp_path / "356_skipped_slot.sql"
    migration_path.write_text("SELECT 356;\n", encoding="utf-8")
    migration = Migration(version=356, name="skipped_slot", path=migration_path)

    runner._ensure_schema_migrations_table = MethodType(lambda self: None, runner)
    runner._read_applied_versions = MethodType(lambda self: {354}, runner)
    runner._discover_migrations = MethodType(lambda self: [migration], runner)

    with pytest.raises(MigrationUnsupportedError, match=r"missing migration v355"):
        runner.apply_pending()


def test_bookkeeping_records_filename_and_checksum_from_version_354(
    tmp_path: Path,
) -> None:
    module = _migration_module()
    runner = module.MigrationRunner(_PostgresMigrationHub())
    migration_path = tmp_path / "354_migration_bookkeeping.sql"
    payload = "SELECT 354;\n"
    migration_path.write_text(payload, encoding="utf-8")
    migration = Migration(
        version=354,
        name="migration_bookkeeping",
        path=migration_path,
    )
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    class Recorder:
        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
            recorded.append((sql, params))
            return _Result()

    runner._record_applied_version(Recorder(), migration)

    assert recorded == [
        (
            """
            INSERT INTO schema_migrations(version, filename, checksum, applied_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (
                354,
                "354_migration_bookkeeping.sql",
                hashlib.sha256(payload.encode()).hexdigest(),
            ),
        )
    ]


class _BatchState:
    def __init__(self) -> None:
        self.applied: dict[int, tuple[str | None, str | None]] = {
            354: (
                "354_migration_bookkeeping.sql",
                "a" * 64,
            )
        }
        self.migration_plan: list[dict[str, str]] = []
        self.intent: dict[str, object] = {"campaign": "schema-apply"}
        self.epoch_open = True
        self.locked = False
        self.lock_acquisitions = 0
        self.unlocks = 0
        self.closed = 0
        self.mutations: list[int] = []
        self.epoch_id = str(uuid.uuid4())
        self.batch_id = str(uuid.uuid4())
        self.manifest_sha256 = "b" * 64


class _BatchTransaction:
    def __init__(self, state: _BatchState) -> None:
        self._state = state
        self._applied = dict(state.applied)
        self._migration_plan = list(state.migration_plan)
        self._intent = dict(state.intent)
        self._mutations = list(state.mutations)

    def commit(self) -> None:
        self._state.applied = self._applied
        self._state.migration_plan = self._migration_plan
        self._state.intent = self._intent
        self._state.mutations = self._mutations

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        normalized = " ".join(sql.split())
        normalized = normalized.removeprefix("-- gobby:destructive ")
        if normalized.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            return _Result()
        if normalized == "SELECT version FROM schema_migrations":
            return _Result([{"version": version} for version in sorted(self._applied)])
        if "FROM destructive_batches AS batch" in normalized:
            return _Result(
                [
                    {
                        "id": self._state.batch_id,
                        "maintenance_epoch_id": self._state.epoch_id,
                        "campaign": "schema-apply",
                        "status": "pending",
                        "backup_manifest_sha256": self._state.manifest_sha256,
                        "migration_plan": self._migration_plan,
                        "intent": self._intent,
                        "released_at": None if self._state.epoch_open else "released",
                        "opened_by": "hub-maintenance:schema-apply",
                    }
                ]
            )
        if normalized.startswith("SELECT MAX(version) AS version FROM schema_migrations"):
            return _Result([{"version": max(self._applied)}])
        if normalized.startswith("UPDATE destructive_batches"):
            self._migration_plan = json.loads(params[0])
            self._intent["backup_starting_head"] = params[1]
            return _Result()
        if (
            "SELECT version, filename, checksum FROM schema_migrations" in normalized
            and "version = ANY" in normalized
        ):
            versions = set(params[0])
            return _Result(
                [
                    {
                        "version": version,
                        "filename": receipt[0],
                        "checksum": receipt[1],
                    }
                    for version, receipt in sorted(self._applied.items())
                    if version in versions
                ]
            )
        if (
            "SELECT version, filename, checksum FROM schema_migrations" in normalized
            and "version = %s" in normalized
        ):
            version = int(params[0])
            receipt = self._applied.get(version)
            return _Result(
                []
                if receipt is None
                else [
                    {
                        "version": version,
                        "filename": receipt[0],
                        "checksum": receipt[1],
                    }
                ]
            )
        if "INSERT INTO schema_migrations(version, filename, checksum" in normalized:
            self._applied[int(params[0])] = (str(params[1]), str(params[2]))
            return _Result()
        if normalized.startswith("SELECT ") and normalized.removeprefix("SELECT ").isdigit():
            self._mutations.append(int(normalized.removeprefix("SELECT ")))
            return _Result()
        raise AssertionError(f"unexpected batch query: {normalized}")


class _BatchHub:
    dialect = "postgres"

    def __init__(self, state: _BatchState) -> None:
        self.state = state

    @contextmanager
    def transaction(self) -> Iterator[_BatchTransaction]:
        transaction = _BatchTransaction(self.state)
        try:
            yield transaction
        except BaseException:
            raise
        else:
            transaction.commit()


class _BatchLockConnection:
    def __init__(self, state: _BatchState) -> None:
        self._state = state

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Result:
        if "pg_advisory_lock" in sql and "unlock" not in sql:
            assert not self._state.locked
            self._state.locked = True
            self._state.lock_acquisitions += 1
            return _Result()
        if "pg_advisory_unlock" in sql:
            assert self._state.locked
            self._state.locked = False
            self._state.unlocks += 1
            return _Result()
        raise AssertionError(f"unexpected lock query: {sql}")

    def close(self) -> None:
        self._state.closed += 1


def _destructive_runner_fixture(
    tmp_path: Path,
) -> tuple[Any, _BatchState, list[Migration], Any]:
    module = _migration_module()
    state = _BatchState()
    migrations: list[Migration] = []
    for version, directive in ((355, "-- gobby:destructive\n"), (356, "")):
        path = tmp_path / f"{version}_migration_{version}.sql"
        path.write_text(f"{directive}SELECT {version};\n", encoding="utf-8")
        migrations.append(Migration(version=version, name=f"migration_{version}", path=path))
    context = module.DestructiveMigrationContext(
        epoch_id=state.epoch_id,
        batch_id=state.batch_id,
        manifest_sha256=state.manifest_sha256,
        backup_starting_head=354,
    )
    return module, state, migrations, context


def _new_destructive_runner(
    module: Any,
    state: _BatchState,
    migrations: list[Migration],
) -> Any:
    runner = module.MigrationRunner(
        _BatchHub(state),
        autocommit_connection=lambda: _BatchLockConnection(state),
    )
    runner._discover_migrations = MethodType(lambda self: migrations, runner)
    return runner


def test_destructive_batch_resumes_from_committed_database_receipt(
    tmp_path: Path,
) -> None:
    module, state, migrations, context = _destructive_runner_fixture(tmp_path)
    first = _new_destructive_runner(module, state, migrations)
    apply_locked = first._apply_transactional_locked

    def crash_after_first_commit(self: Any, migration: Migration) -> None:
        apply_locked(migration)
        if migration.version == 355:
            raise RuntimeError("simulated process crash after database commit")

    first._apply_transactional_locked = MethodType(crash_after_first_commit, first)
    with pytest.raises(RuntimeError, match="after database commit"):
        first.apply_destructive(context)

    assert sorted(state.applied) == [354, 355]
    assert state.mutations == [355]
    assert [item["version"] for item in state.migration_plan] == ["355", "356"]

    resumed = _new_destructive_runner(module, state, migrations)
    resumed.apply_destructive(context)

    assert sorted(state.applied) == [354, 355, 356]
    assert state.mutations == [355, 356]
    assert state.lock_acquisitions == 2
    assert state.unlocks == 2
    assert state.closed == 2


def test_destructive_batch_rejects_nonprefix_database_receipts(tmp_path: Path) -> None:
    module, state, migrations, context = _destructive_runner_fixture(tmp_path)
    state.migration_plan = [
        {
            "version": str(migration.version),
            "filename": migration.path.name,
            "checksum": hashlib.sha256(migration.path.read_bytes()).hexdigest(),
        }
        for migration in migrations
    ]
    state.intent["backup_starting_head"] = 354
    state.applied[356] = (
        migrations[1].path.name,
        hashlib.sha256(migrations[1].path.read_bytes()).hexdigest(),
    )

    with pytest.raises(MigrationUnsupportedError, match="exact prefix"):
        _new_destructive_runner(module, state, migrations).apply_destructive(context)


def test_destructive_batch_rejects_different_bytes_for_same_version(
    tmp_path: Path,
) -> None:
    module, state, migrations, context = _destructive_runner_fixture(tmp_path)
    state.migration_plan = [
        {
            "version": "355",
            "filename": migrations[0].path.name,
            "checksum": "c" * 64,
        },
        {
            "version": "356",
            "filename": migrations[1].path.name,
            "checksum": hashlib.sha256(migrations[1].path.read_bytes()).hexdigest(),
        },
    ]
    state.intent["backup_starting_head"] = 354

    with pytest.raises(MigrationUnsupportedError, match="different local migration bytes"):
        _new_destructive_runner(module, state, migrations).apply_destructive(context)


def test_destructive_batch_requires_open_maintenance_epoch(tmp_path: Path) -> None:
    module, state, migrations, context = _destructive_runner_fixture(tmp_path)
    state.epoch_open = False

    with pytest.raises(MigrationUnsupportedError, match="maintenance epoch is not open"):
        _new_destructive_runner(module, state, migrations).apply_destructive(context)


def test_destructive_batch_requires_exact_prebatch_head(tmp_path: Path) -> None:
    module, state, migrations, context = _destructive_runner_fixture(tmp_path)
    context = module.DestructiveMigrationContext(
        epoch_id=context.epoch_id,
        batch_id=context.batch_id,
        manifest_sha256=context.manifest_sha256,
        backup_starting_head=353,
    )

    with pytest.raises(
        MigrationUnsupportedError, match="Backup starting head 353.*current head 354"
    ):
        _new_destructive_runner(module, state, migrations).apply_destructive(context)


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
        runner._is_destructive = MethodType(lambda self, item: False, runner)

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
    future_versions = [version for version, _ in actual if version >= 354]
    assert future_versions == list(range(354, 354 + len(future_versions)))


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

    def fail_record_once(self: Any, txn: Any, migration: Migration) -> None:
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
