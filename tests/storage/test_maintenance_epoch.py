"""Shared maintenance-epoch fence and ledger behavior."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import gobby.runner_init.helpers as runner_helpers
import gobby.storage.hub.runtime as hub_runtime
import gobby.storage.maintenance_epoch as maintenance
from gobby.storage.maintenance_epoch import (
    CAMPAIGNS,
    MaintenanceEpochActiveError,
    MaintenanceEpochOwnershipError,
    abort_maintenance_epoch,
    admitted_database_url,
    bind_maintenance_epoch,
    create_destructive_batch,
    get_destructive_batch,
    maintenance_child_environment,
    open_maintenance_epoch,
    probe_maintenance_admission,
    release_maintenance_epoch,
    release_restored_maintenance_epoch,
    run_receipted_component,
)

_BASELINE_SQL = Path(__file__).resolve().parents[2] / "crates/gcore/assets/schema/baseline.sql"
_CAMPAIGN_CHECK_PATTERN = re.compile(
    r"CONSTRAINT (?P<name>(?:maintenance_epochs|destructive_batches)_campaign_check) "
    r"CHECK \(\(campaign = ANY \(ARRAY\[(?P<values>[^\]]+)\]\)\)\)"
)


def _scoped_dsn(database_url: str, schema: str) -> str:
    return database_url + f"?options=-csearch_path%3D{schema}"


@pytest.fixture
def epoch_admin(
    postgres_db: Any,
    postgres_database_url: str,
    postgres_schema: str,
) -> Iterator[tuple[psycopg.Connection[Any], str]]:
    """Keep a pre-fence connection available for reliable test cleanup."""
    del postgres_db
    scoped_dsn = _scoped_dsn(postgres_database_url, postgres_schema)
    connection = psycopg.connect(scoped_dsn, autocommit=True, row_factory=dict_row)
    try:
        yield connection, scoped_dsn
    finally:
        connection.execute(
            """
            UPDATE destructive_batches
            SET status = 'aborted',
                aborted_at = COALESCE(aborted_at, NOW()),
                abort_disposition = COALESCE(abort_disposition, 'test cleanup')
            WHERE status NOT IN ('verified', 'aborted')
            """
        )
        connection.execute(
            """
            UPDATE maintenance_epochs
            SET released_at = COALESCE(released_at, NOW()),
                released_by_command = COALESCE(
                    released_by_command,
                    'test cleanup'
                )
            WHERE released_at IS NULL
            """
        )
        connection.close()


def _insert_epoch(
    connection: psycopg.Connection[Any],
    *,
    campaign: str = "purge",
    opened_by: str | None = None,
) -> uuid.UUID:
    epoch_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO maintenance_epochs(id, campaign, opened_by, scope_note)
        VALUES (%s, %s, %s, %s)
        """,
        (
            epoch_id,
            campaign,
            opened_by or f"hub-maintenance:{campaign}",
            f"{campaign} test epoch",
        ),
    )
    return epoch_id


def test_migration_installs_ledgers_without_named_schema_login_fence(
    epoch_admin: tuple[psycopg.Connection[Any], str],
    postgres_schema: str,
) -> None:
    connection, _dsn = epoch_admin

    columns = {
        row["column_name"]
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'schema_migrations'
            """
        ).fetchall()
    }
    tables = {
        row["table_name"]
        for row in connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name IN ('maintenance_epochs', 'destructive_batches')
            """
        ).fetchall()
    }
    trigger = connection.execute(
        """
        SELECT
            event.evtname,
            event.evtevent,
            event.evtenabled,
            pg_catalog.pg_get_functiondef(event.evtfoid) AS function_definition
        FROM pg_catalog.pg_event_trigger AS event
        JOIN pg_catalog.pg_proc AS function ON function.oid = event.evtfoid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = %s
          AND function.proname = 'gobby_maintenance_epoch_login_guard'
        """,
        (postgres_schema,),
    ).fetchone()

    assert {"filename", "checksum"} <= columns
    assert tables == {"maintenance_epochs", "destructive_batches"}
    assert trigger is None


def test_named_schema_login_allows_bare_and_epoch_bound_clients(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    epoch_id = _insert_epoch(connection)

    with psycopg.connect(scoped_dsn) as bare:
        assert bare.execute("SELECT 1").fetchone() == (1,)

    with psycopg.connect(bind_maintenance_epoch(scoped_dsn, epoch_id)) as admitted:
        configured_epoch = admitted.execute(
            "SELECT current_setting('gobby.maintenance_epoch')"
        ).fetchone()
        assert configured_epoch == (str(epoch_id),)


def test_named_schema_login_allows_explicit_client_search_path(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    _insert_epoch(connection)

    with psycopg.connect(scoped_dsn, options="-csearch_path=pg_catalog") as client:
        assert client.execute("SELECT 1").fetchone() == (1,)


def test_named_schema_login_allows_external_libpq_clients(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("psql is required for the external libpq ingress check")
    epoch_id = _insert_epoch(connection, campaign="reconcile")

    bare = subprocess.run(
        [psql, "-X", scoped_dsn, "-Atqc", "SELECT 1"],
        capture_output=True,
        text=True,
        check=False,
    )
    admitted = subprocess.run(
        [
            psql,
            "-X",
            bind_maintenance_epoch(scoped_dsn, epoch_id),
            "-Atqc",
            "SELECT 1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert bare.returncode == 0, bare.stderr
    assert bare.stdout.strip() == "1"
    assert admitted.returncode == 0, admitted.stderr
    assert admitted.stdout.strip() == "1"


def test_logins_are_unaffected_without_an_open_epoch(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    _connection, scoped_dsn = epoch_admin

    with psycopg.connect(scoped_dsn) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_restored_epoch_is_released_without_touching_origin_state(
    epoch_admin: tuple[psycopg.Connection[Any], str],
    postgres_schema: str,
) -> None:
    connection, restored_dsn = epoch_admin
    restored_epoch_id = _insert_epoch(connection, campaign="reconcile")
    origin_epoch_id = uuid.uuid4()
    origin_schema = f"{postgres_schema}_restore_origin"
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(origin_schema)))
    try:
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.maintenance_epochs (LIKE maintenance_epochs INCLUDING ALL)"
            ).format(sql.Identifier(origin_schema))
        )
        connection.execute(
            sql.SQL(
                "INSERT INTO {}.maintenance_epochs"
                "(id, campaign, opened_by, scope_note) VALUES (%s, %s, %s, %s)"
            ).format(sql.Identifier(origin_schema)),
            (
                origin_epoch_id,
                "reconcile",
                "hub-maintenance:reconcile",
                "origin sentinel",
            ),
        )

        with psycopg.connect(restored_dsn) as pre_release:
            assert pre_release.execute("SELECT 1").fetchone() == (1,)

        released = release_restored_maintenance_epoch(restored_dsn)

        assert released is not None
        assert released.id == restored_epoch_id
        assert released.released_at is not None
        assert released.released_by_command == "restore"
        with psycopg.connect(restored_dsn) as admitted:
            assert admitted.execute("SELECT 1").fetchone() == (1,)
        origin_row = connection.execute(
            sql.SQL(
                "SELECT released_at, released_by_command FROM {}.maintenance_epochs WHERE id = %s"
            ).format(sql.Identifier(origin_schema)),
            (origin_epoch_id,),
        ).fetchone()
        assert origin_row == {"released_at": None, "released_by_command": None}
    finally:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(origin_schema)))


def test_named_schema_admission_probe_allows_open_epoch(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    _insert_epoch(connection, campaign="reconcile")

    assert probe_maintenance_admission(scoped_dsn) == scoped_dsn


def test_runtime_and_daemon_boot_use_courtesy_admission_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = MaintenanceEpochActiveError(uuid.uuid4())
    config = SimpleNamespace(
        hub_backend="postgres",
        database_url="postgresql://example/gobby",
        postgres_pool=object(),
    )

    # A managed execution opens the hub through its grant and never reaches the
    # admission check; the test must run as an operator process.
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)
    monkeypatch.setattr(hub_runtime, "load_bootstrap", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        hub_runtime,
        "admitted_database_url",
        lambda _dsn: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        runner_helpers,
        "admitted_database_url",
        lambda _dsn: (_ for _ in ()).throw(error),
    )

    with pytest.raises(MaintenanceEpochActiveError):
        with hub_runtime.runtime_hub_database(apply_migrations=False):
            pass
    with pytest.raises(MaintenanceEpochActiveError):
        runner_helpers.init_hub_database(config)


def test_epoch_token_is_bound_to_dsn_and_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch_id = uuid.uuid4()
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=5000")

    bound = bind_maintenance_epoch(
        "postgresql://gobby:secret@localhost/gobby?application_name=test",
        epoch_id,
    )
    environment = maintenance_child_environment(epoch_id)

    parsed = psycopg.conninfo.conninfo_to_dict(bound)
    parsed_options = str(parsed.get("options") or "")
    assert parsed["application_name"] == "test"
    assert f"gobby.maintenance_epoch={epoch_id}" in parsed_options
    assert environment["GOBBY_MAINTENANCE_EPOCH"] == str(epoch_id)
    assert "-c statement_timeout=5000" in environment["PGOPTIONS"]
    assert f"-c gobby.maintenance_epoch={epoch_id}" in environment["PGOPTIONS"]
    assert environment is not os.environ


class _FakeCursor:
    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []
        self.rowcount = 1

    def fetchone(self) -> dict[str, Any] | None:
        return self._row

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeEpochConnection:
    def __init__(
        self,
        epoch_id: uuid.UUID,
        *,
        foreign_counts: list[int] | None = None,
    ) -> None:
        self.epoch_id = epoch_id
        self.events: list[str] = []
        self._foreign_counts = [0] if foreign_counts is None else list(foreign_counts)

    def __enter__(self) -> _FakeEpochConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.events.append("close")

    def execute(self, sql: str, _params: object = ()) -> _FakeCursor:
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO maintenance_epochs"):
            self.events.append("insert")
            return _FakeCursor(
                row={
                    "id": self.epoch_id,
                    "campaign": "purge",
                    "opened_at": datetime.now(UTC),
                    "opened_by": "hub-maintenance:purge",
                    "scope_note": "test",
                    "released_at": None,
                    "released_by_command": None,
                }
            )
        if "pg_terminate_backend" in normalized:
            self.events.append("terminate")
            return _FakeCursor(rows=[])
        if "pg_stat_clear_snapshot" in normalized:
            self.events.append("clear-stat-snapshot")
            return _FakeCursor(row=None)
        if "pg_stat_activity" in normalized:
            self.events.append("verify")
            count = (
                self._foreign_counts.pop(0)
                if len(self._foreign_counts) > 1
                else self._foreign_counts[0]
            )
            return _FakeCursor(row={"foreign_connections": count})
        if normalized.startswith("UPDATE maintenance_epochs"):
            self.events.append("release-fence")
            return _FakeCursor(row=None)
        raise AssertionError(normalized)

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


class _MidOpenFailureConnection:
    """Inject a server error after the epoch commit while preserving cleanup access."""

    def __init__(self, connection: psycopg.Connection[dict[str, Any]]) -> None:
        self._connection = connection

    def __enter__(self) -> _MidOpenFailureConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self._connection.close()

    def execute(self, sql: str, params: Any = ()) -> Any:
        if "pg_terminate_backend" in sql:
            return self._connection.execute("SELECT 1 / 0")
        return self._connection.execute(sql, params)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def test_epoch_open_commits_fence_before_terminating_and_verifying_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch_id = uuid.uuid4()
    connection = _FakeEpochConnection(epoch_id)
    monkeypatch.setattr(maintenance, "_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(maintenance, "_new_epoch_id", lambda: epoch_id)

    opened = open_maintenance_epoch(
        "postgresql://example/gobby",
        campaign="purge",
        opened_by="hub-maintenance:purge",
        scope_note="test",
    )

    assert opened.id == epoch_id
    assert connection.events == [
        "insert",
        "commit",
        "terminate",
        "clear-stat-snapshot",
        "verify",
        "close",
    ]


def test_epoch_open_waits_boundedly_for_terminated_backends_to_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pg_terminate_backend is asynchronous; lingering backends must not fail the open."""
    epoch_id = uuid.uuid4()
    connection = _FakeEpochConnection(epoch_id, foreign_counts=[2, 0])
    monkeypatch.setattr(maintenance, "_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(maintenance, "_new_epoch_id", lambda: epoch_id)

    opened = open_maintenance_epoch(
        "postgresql://example/gobby",
        campaign="purge",
        opened_by="hub-maintenance:purge",
        scope_note="test",
        quiescence_deadline_seconds=5.0,
        quiescence_poll_seconds=0.0,
    )

    assert opened.id == epoch_id
    assert connection.events == [
        "insert",
        "commit",
        "terminate",
        "clear-stat-snapshot",
        "verify",
        "terminate",
        "clear-stat-snapshot",
        "verify",
        "close",
    ]


def test_epoch_open_releases_fence_when_quiescence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed open must roll the fence back instead of stranding live clients."""
    epoch_id = uuid.uuid4()
    connection = _FakeEpochConnection(epoch_id, foreign_counts=[3])
    monkeypatch.setattr(maintenance, "_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(maintenance, "_new_epoch_id", lambda: epoch_id)

    with pytest.raises(maintenance.MaintenanceQuiescenceError, match="fence was released"):
        open_maintenance_epoch(
            "postgresql://example/gobby",
            campaign="purge",
            opened_by="hub-maintenance:purge",
            scope_note="test",
            quiescence_deadline_seconds=0.05,
            quiescence_poll_seconds=0.0,
        )

    assert connection.events[-4:] == ["rollback", "release-fence", "commit", "close"]
    assert connection.events.count("terminate") >= 1


def test_epoch_open_terminates_only_gobby_client_backends(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    _connection, scoped_dsn = epoch_admin
    operator = psycopg.connect(
        scoped_dsn,
        autocommit=True,
        application_name="operator-psql",
    )
    gobby_client = psycopg.connect(
        scoped_dsn,
        autocommit=True,
        application_name="gobby-test-client",
    )
    try:
        open_maintenance_epoch(
            scoped_dsn,
            campaign="purge",
            opened_by="hub-maintenance:purge",
            scope_note="test application ownership",
        )

        assert operator.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(psycopg.OperationalError):
            gobby_client.execute("SELECT 1")
    finally:
        operator.close()
        gobby_client.close()


def test_epoch_open_releases_fence_after_mid_open_database_error(
    epoch_admin: tuple[psycopg.Connection[Any], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, scoped_dsn = epoch_admin
    real_connect = maintenance._connect

    def failing_connect(*args: Any, **kwargs: Any) -> _MidOpenFailureConnection:
        return _MidOpenFailureConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(maintenance, "_connect", failing_connect)

    with pytest.raises(psycopg.errors.DivisionByZero):
        open_maintenance_epoch(
            scoped_dsn,
            campaign="purge",
            opened_by="hub-maintenance:purge",
            scope_note="test mid-open failure",
        )

    open_epochs = connection.execute(
        "SELECT COUNT(*) AS count FROM maintenance_epochs WHERE released_at IS NULL"
    ).fetchone()
    assert open_epochs == {"count": 0}
    with psycopg.connect(scoped_dsn, autocommit=True) as subsequent:
        assert subsequent.execute("SELECT 1").fetchone() == (1,)


def test_named_schema_pool_remains_available_across_epoch_release(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    """Named-schema test pools remain usable because login fencing is public-only."""
    connection, scoped_dsn = epoch_admin
    epoch_id = _insert_epoch(connection, campaign="schema-apply")

    pool = ConnectionPool(
        scoped_dsn,
        min_size=0,
        max_size=2,
        open=True,
        timeout=1.0,
    )
    try:
        with pool.connection(timeout=1.0) as pooled:
            assert pooled.execute("SELECT 1").fetchone() is not None

        connection.execute(
            """
            UPDATE maintenance_epochs
            SET released_at = NOW(),
                released_by_command = 'test fence release'
            WHERE id = %s
            """,
            (epoch_id,),
        )

        with pool.connection(timeout=10.0) as pooled:
            assert pooled.execute("SELECT 1").fetchone() is not None
        assert pool.get_stats().get("requests_waiting", 0) == 0
    finally:
        pool.close()


def test_release_requires_owning_orchestrator_and_verified_batch(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    epoch_id = _insert_epoch(connection, campaign="reconcile")
    batch = create_destructive_batch(
        scoped_dsn,
        epoch_id,
        campaign="reconcile",
        intent={"operation": "reconcile"},
    )
    connection.execute(
        """
        UPDATE destructive_batches
        SET status = 'verified', verified_at = NOW()
        WHERE id = %s
        """,
        (batch.id,),
    )

    with pytest.raises(MaintenanceEpochOwnershipError):
        release_maintenance_epoch(
            scoped_dsn,
            epoch_id,
            owner_command="hub-maintenance:wrong",
            released_by_command="hub-maintenance run wrong",
        )

    released = release_maintenance_epoch(
        scoped_dsn,
        epoch_id,
        owner_command="hub-maintenance:reconcile",
        released_by_command="hub-maintenance run reconcile",
    )
    assert released.released_at is not None
    assert released.released_by_command == "hub-maintenance run reconcile"


def test_abort_requires_confirmation_and_records_partial_state_disposition(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    epoch_id = _insert_epoch(connection)
    batch = create_destructive_batch(
        scoped_dsn,
        epoch_id,
        campaign="purge",
        intent={"targets": ["qdrant"]},
    )

    with pytest.raises(ValueError, match="confirmation"):
        abort_maintenance_epoch(
            scoped_dsn,
            epoch_id,
            disposition="Qdrant target verified absent",
            confirmed=False,
        )

    aborted = abort_maintenance_epoch(
        scoped_dsn,
        epoch_id,
        disposition="Qdrant target verified absent",
        confirmed=True,
    )
    stored_batch = get_destructive_batch(scoped_dsn, epoch_id, batch.id)

    assert aborted.released_by_command == "hub-maintenance abort"
    assert stored_batch is not None
    assert stored_batch.status == "aborted"
    assert stored_batch.abort_disposition == "Qdrant target verified absent"


class _InjectedCrash(RuntimeError):
    pass


def test_pending_receipt_resumes_from_external_component_postcondition(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    epoch_id = _insert_epoch(connection, campaign="reconcile")
    batch = create_destructive_batch(
        scoped_dsn,
        epoch_id,
        campaign="reconcile",
        intent={"targets": ["qdrant:orphan"]},
    )
    component = {"exists": True, "apply_calls": 0}

    def delete_then_crash() -> None:
        component["apply_calls"] += 1
        component["exists"] = False
        raise _InjectedCrash

    with pytest.raises(_InjectedCrash):
        run_receipted_component(
            scoped_dsn,
            epoch_id,
            batch.id,
            target="qdrant:orphan",
            apply=delete_then_crash,
            postcondition=lambda: not component["exists"],
        )

    pending = get_destructive_batch(scoped_dsn, epoch_id, batch.id)
    assert pending is not None
    assert pending.target_receipts["qdrant:orphan"]["state"] == "pending"

    run_receipted_component(
        scoped_dsn,
        epoch_id,
        batch.id,
        target="qdrant:orphan",
        apply=lambda: component.update(apply_calls=component["apply_calls"] + 1),
        postcondition=lambda: not component["exists"],
    )
    verified = get_destructive_batch(scoped_dsn, epoch_id, batch.id)

    assert component["apply_calls"] == 1
    assert verified is not None
    assert verified.target_receipts["qdrant:orphan"]["state"] == "verified"


def test_pending_receipt_reruns_component_when_postcondition_does_not_hold(
    epoch_admin: tuple[psycopg.Connection[Any], str],
) -> None:
    connection, scoped_dsn = epoch_admin
    epoch_id = _insert_epoch(connection, campaign="reconcile")
    batch = create_destructive_batch(
        scoped_dsn,
        epoch_id,
        campaign="reconcile",
        intent={"targets": ["falkordb:orphan"]},
    )
    component = {"exists": True, "apply_calls": 0}

    def crash_before_delete() -> None:
        raise _InjectedCrash

    with pytest.raises(_InjectedCrash):
        run_receipted_component(
            scoped_dsn,
            epoch_id,
            batch.id,
            target="falkordb:orphan",
            apply=crash_before_delete,
            postcondition=lambda: not component["exists"],
        )

    def delete_component() -> None:
        component["apply_calls"] += 1
        component["exists"] = False

    run_receipted_component(
        scoped_dsn,
        epoch_id,
        batch.id,
        target="falkordb:orphan",
        apply=delete_component,
        postcondition=lambda: not component["exists"],
    )
    verified = get_destructive_batch(scoped_dsn, epoch_id, batch.id)

    assert component["apply_calls"] == 1
    assert verified is not None
    assert verified.target_receipts["falkordb:orphan"]["state"] == "verified"


def test_admitted_database_url_uses_matching_child_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch_id = uuid.uuid4()
    monkeypatch.setenv("GOBBY_MAINTENANCE_EPOCH", str(epoch_id))
    monkeypatch.setattr(maintenance, "probe_maintenance_admission", lambda dsn: dsn)

    admitted = admitted_database_url("postgresql://example/gobby")

    admitted_options = str(psycopg.conninfo.conninfo_to_dict(admitted).get("options") or "")
    assert str(epoch_id) in admitted_options


def test_campaign_registry_is_admitted_by_every_baseline_campaign_check() -> None:
    """Every runnable campaign must be a value the ledger CHECK constraints admit.

    The CHECK lists may keep retired names (dropping one is a schema delta);
    the Python registry may never name a campaign the ledgers would reject.
    """
    baseline = _BASELINE_SQL.read_text(encoding="utf-8")
    admitted = {
        match.group("name"): set(re.findall(r"'([^']+)'::text", match.group("values")))
        for match in _CAMPAIGN_CHECK_PATTERN.finditer(baseline)
    }

    assert set(admitted) == {
        "destructive_batches_campaign_check",
        "maintenance_epochs_campaign_check",
    }
    for constraint_name, values in admitted.items():
        assert set(CAMPAIGNS) <= values, constraint_name
