"""One-time destructive hub data purge campaign."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from gobby.cli.hub_maintenance import register_campaign_executor
from gobby.config.app import load_config
from gobby.paths import get_gobby_home
from gobby.storage.maintenance_epoch import (
    DestructiveBatch,
    MaintenanceEpoch,
    bind_maintenance_epoch,
)

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = "30s"
STATEMENT_TIMEOUT = "6h"
SYSTEM_SESSION_ID = "00000000-0000-0000-0000-000000000001"
SESSION_EXPIRY_HOURS = 24

type _Row = dict[str, Any]
type _Connection = psycopg.Connection[_Row]


@dataclass(frozen=True)
class _PurgeTarget:
    name: str
    dml: str
    verification: str


@dataclass(frozen=True)
class _TableLedger:
    pre_count: int
    post_count: int
    pre_size: int
    post_dml_size: int
    post_vacuum_size: int | None = None


PURGE_TARGETS = (
    _PurgeTarget(
        name="metrics_events",
        dml="""
            DELETE FROM metrics_events
            WHERE event_type = 'rule_eval' AND result = 'allow'
        """,
        verification="""
            SELECT count(*) AS count FROM metrics_events
            WHERE event_type = 'rule_eval' AND result = 'allow'
        """,
    ),
    _PurgeTarget(
        name="session_variables",
        dml=f"""
            DELETE FROM session_variables AS sv
            USING sessions AS s
            WHERE sv.session_id = s.id
              AND s.status IN ('expired', 'deleted')
              AND s.id != '{SYSTEM_SESSION_ID}'
              AND s.updated_at < CURRENT_TIMESTAMP
                  - ({SESSION_EXPIRY_HOURS} * INTERVAL '1 hour')
        """,
        verification=f"""
            SELECT count(*) AS count
            FROM session_variables AS sv
            JOIN sessions AS s ON s.id = sv.session_id
            WHERE s.status IN ('expired', 'deleted')
              AND s.id != '{SYSTEM_SESSION_ID}'
              AND s.updated_at < CURRENT_TIMESTAMP
                  - ({SESSION_EXPIRY_HOURS} * INTERVAL '1 hour')
        """,
    ),
    _PurgeTarget(
        name="token_events",
        dml="DELETE FROM token_events",
        verification="SELECT count(*) AS count FROM token_events",
    ),
    _PurgeTarget(
        name="loop_progress",
        dml="DELETE FROM loop_progress",
        verification="SELECT count(*) AS count FROM loop_progress",
    ),
    _PurgeTarget(
        name="step_executions",
        dml="""
            UPDATE step_executions
            SET input_json = NULL, output_json = NULL
            WHERE status = 'completed'
              AND (input_json IS NOT NULL OR output_json IS NOT NULL)
        """,
        verification="""
            SELECT count(*) AS count FROM step_executions
            WHERE status = 'completed'
              AND (input_json IS NOT NULL OR output_json IS NOT NULL)
        """,
    ),
    _PurgeTarget(
        name="spans",
        dml="DELETE FROM spans",
        verification="SELECT count(*) AS count FROM spans",
    ),
)


class _PurgeExecutor:
    """Apply and verify the one-time hub purge inside a maintenance epoch."""

    def __init__(
        self,
        database_url: str | None = None,
        disk_path: Path | None = None,
    ) -> None:
        self._configured_database_url = database_url
        self._disk_path = disk_path or get_gobby_home()
        self._ledger: dict[str, _TableLedger] = {}

    def apply(self, epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
        database_url = self._database_url(epoch)
        with _connect(
            database_url,
            autocommit=True,
            application_name="gobby-hub-purge-preflight",
        ) as connection:
            _configure_session(connection)
            _preflight(connection, self._disk_path)

        self._ledger = {}
        for target in PURGE_TARGETS:
            self._ledger[target.name] = _apply_dml(database_url, target)
        for target in PURGE_TARGETS:
            self._ledger[target.name] = _vacuum(
                database_url,
                target,
                self._ledger[target.name],
            )

    def verify(self, epoch: MaintenanceEpoch, _batch: DestructiveBatch) -> None:
        database_url = self._database_url(epoch)
        with _connect(
            database_url,
            autocommit=True,
            application_name="gobby-hub-purge-verify",
        ) as connection:
            _configure_session(connection)
            for target in PURGE_TARGETS:
                violations = _scalar(connection, target.verification)
                if violations:
                    raise click.ClickException(
                        f"Purge verification failed for {target.name}: "
                        f"{violations} matching rows remain"
                    )
                if target.name not in self._ledger:
                    count, size = _relation_stats(connection, target.name)
                    self._ledger[target.name] = _TableLedger(
                        pre_count=count,
                        post_count=count,
                        pre_size=size,
                        post_dml_size=size,
                        post_vacuum_size=size,
                    )
                    logger.info(
                        "purge verification ledger target=%s count=%d size=%d",
                        target.name,
                        count,
                        size,
                    )
        if set(self._ledger) != {target.name for target in PURGE_TARGETS}:
            raise click.ClickException("Purge verification ledger is incomplete")

    def _database_url(self, epoch: MaintenanceEpoch) -> str:
        database_url = self._configured_database_url
        if database_url is None:
            config = load_config(resolve_database_url=True)
            database_url = config.database_url
        if database_url is None:
            raise click.ClickException("Hub database URL is unavailable")
        return bind_maintenance_epoch(database_url, epoch.id)


def _connect(
    database_url: str,
    *,
    autocommit: bool,
    application_name: str,
) -> _Connection:
    return psycopg.connect(
        database_url,
        autocommit=autocommit,
        application_name=application_name,
        row_factory=dict_row,
    )


def _configure_session(connection: _Connection) -> None:
    connection.execute("SELECT set_config('lock_timeout', %s, FALSE)", (LOCK_TIMEOUT,))
    connection.execute(
        "SELECT set_config('statement_timeout', %s, FALSE)",
        (STATEMENT_TIMEOUT,),
    )


def _preflight(connection: _Connection, disk_path: Path) -> None:
    _assert_quiescent(connection)
    relation_sizes = {
        target.name: _relation_size(connection, target.name) for target in PURGE_TARGETS
    }
    largest_relation = max(relation_sizes.values(), default=0)
    required_free = (largest_relation * 6 + 4) // 5
    free_bytes = shutil.disk_usage(disk_path).free
    if free_bytes < required_free:
        raise click.ClickException(
            f"Insufficient free space for purge: {free_bytes} bytes available, "
            f"{required_free} required"
        )
    logger.info(
        "purge preflight connections=0 free_bytes=%d required_free_bytes=%d "
        "largest_relation_bytes=%d safety_margin=20%%",
        free_bytes,
        required_free,
        largest_relation,
    )


def _assert_quiescent(connection: _Connection) -> None:
    others = _scalar(
        connection,
        """
        SELECT count(*) AS count
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND backend_type = 'client backend'
          AND pid != pg_backend_pid()
        """,
    )
    if others:
        raise click.ClickException(
            f"Purge requires quiescence; found {others} other PostgreSQL connection(s)"
        )


def _apply_dml(database_url: str, target: _PurgeTarget) -> _TableLedger:
    with _connect(
        database_url,
        autocommit=False,
        application_name=f"gobby-hub-purge-dml:{target.name}",
    ) as connection:
        _configure_session(connection)
        pre_count, pre_size = _relation_stats(connection, target.name)
        connection.execute(target.dml)
        post_count, post_size = _relation_stats(connection, target.name)
        connection.commit()
    logger.info(
        "purge DML ledger target=%s pre_count=%d post_count=%d pre_size=%d post_size=%d",
        target.name,
        pre_count,
        post_count,
        pre_size,
        post_size,
    )
    return _TableLedger(pre_count, post_count, pre_size, post_size)


def _vacuum(
    database_url: str,
    target: _PurgeTarget,
    ledger: _TableLedger,
) -> _TableLedger:
    with _connect(
        database_url,
        autocommit=True,
        application_name=f"gobby-hub-purge-vacuum:{target.name}",
    ) as connection:
        _configure_session(connection)
        pre_size = _relation_size(connection, target.name)
        connection.execute(sql.SQL("VACUUM (FULL, ANALYZE) {}").format(sql.Identifier(target.name)))
        post_size = _relation_size(connection, target.name)
    logger.info(
        "purge VACUUM ledger target=%s pre_size=%d post_size=%d reclaimed_bytes=%d",
        target.name,
        pre_size,
        post_size,
        max(0, pre_size - post_size),
    )
    return _TableLedger(
        pre_count=ledger.pre_count,
        post_count=ledger.post_count,
        pre_size=ledger.pre_size,
        post_dml_size=ledger.post_dml_size,
        post_vacuum_size=post_size,
    )


def _relation_stats(connection: _Connection, target: str) -> tuple[int, int]:
    count = _scalar(
        connection,
        sql.SQL("SELECT count(*) AS count FROM {}").format(sql.Identifier(target)),
    )
    return count, _relation_size(connection, target)


def _relation_size(connection: _Connection, target: str) -> int:
    row = connection.execute(
        "SELECT pg_total_relation_size(%s::regclass) AS size",
        (target,),
    ).fetchone()
    if row is None:
        raise click.ClickException(f"Purge target {target!r} does not exist")
    return int(row["size"])


def _scalar(
    connection: _Connection,
    statement: str | bytes | sql.SQL | sql.Composed,
) -> int:
    row = connection.execute(statement).fetchone()
    if row is None:
        raise click.ClickException("Purge database query returned no row")
    return int(row["count"])


register_campaign_executor("purge", _PurgeExecutor())
