"""Crash-safe app-level cutover from legacy machine keys to UUID identity.

Rollback is restore-based: restore the database and the identity anchor from
the same verified pre-cutover backup manifest.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gobby.utils.durable_file import durable_replace_text, exclusive_file_lock

FaultInjector = Callable[[str, str], None]

_MIGRATION_LOCK_SQL = "hashtext('postgres_migrations_apply'), hashtext(current_schema())"


class IdentityCutoverError(RuntimeError):
    """Raised when the identity cutover's safety gates fail."""


@dataclass(frozen=True)
class CutoverReport:
    """Completed cutover facts used by CLI verification and tests."""

    rotated_id: str
    completed_identities: int
    retired_identities: int


def run_identity_cutover(
    database_url: str,
    identity_file: Path,
    *,
    fault_injector: FaultInjector | None = None,
) -> CutoverReport:
    """Rotate the local identity and retire every other inventoried identity."""
    fault = fault_injector or _no_fault
    with exclusive_file_lock(identity_file):
        current_file_id = _read_identity_file(identity_file)
        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
            application_name="gobby-identity-cutover",
        ) as connection:
            connection.execute(f"SELECT pg_advisory_lock({_MIGRATION_LOCK_SQL})")
            try:
                _require_precursor(connection)
                activated, activation_inventory = _activate_fence_and_inventory(
                    connection,
                    current_file_id,
                )
                if activated:
                    fault("after_activation", current_file_id)
                _assert_no_foreign_application_connections(connection)
                should_reinventory = activated or _journal_is_unstarted(connection)
                expected_inventory = (
                    activation_inventory if activated else _journal_inventory(connection)
                )
                if should_reinventory and _inventory(connection) != expected_inventory:
                    raise IdentityCutoverError(
                        "machine identity inventory changed after fence activation"
                    )
                _process_journal(connection, identity_file, fault)
                _assert_zero_unmapped(connection)
                return _report(connection)
            finally:
                connection.execute(f"SELECT pg_advisory_unlock({_MIGRATION_LOCK_SQL})")


def verify_identity_cutover(database_url: str, identity_file: Path) -> None:
    """Verify journal completion, DB/file agreement, and the zero-unmapped gate."""
    file_id = _canonical_uuid(_read_identity_file(identity_file))
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        incomplete = connection.execute(
            "SELECT COUNT(*) AS count FROM identity_cutover_journal WHERE phase <> 'file_committed'"
        ).fetchone()
        if incomplete is None or int(incomplete["count"]) != 0:
            raise IdentityCutoverError("identity cutover journal is incomplete")
        rotated = connection.execute(
            """
            SELECT new_id::TEXT AS new_id
            FROM identity_cutover_journal
            WHERE disposition = 'rotated'
            """
        ).fetchall()
        if len(rotated) != 1 or rotated[0]["new_id"] != file_id:
            raise IdentityCutoverError("identity file does not match the rotated journal row")
        machine_column = _machine_identity_column(connection)
        row = connection.execute(
            f"SELECT {machine_column}::TEXT AS id FROM machines"  # nosec B608 - catalog choice
        ).fetchall()
        if [machine["id"] for machine in row] != [file_id]:
            raise IdentityCutoverError("machines registry does not match the identity file")
        _assert_zero_unmapped(connection)


def _require_precursor(connection: psycopg.Connection[Any]) -> None:
    row = connection.execute("SELECT to_regclass('identity_cutover_journal') AS journal").fetchone()
    if row is None or row["journal"] is None:
        raise IdentityCutoverError("migration 364 identity cutover precursor is not applied")


def _activate_fence_and_inventory(
    connection: psycopg.Connection[Any],
    local_old_id: str,
) -> tuple[bool, tuple[tuple[str, bool, int], ...]]:
    existing = connection.execute(
        "SELECT COUNT(*) AS count FROM identity_cutover_journal"
    ).fetchone()
    if existing is not None and int(existing["count"]) > 0:
        return False, ()

    with connection.transaction():
        connection.execute("LOCK TABLE machines, sessions IN EXCLUSIVE MODE")
        rows = _inventory_rows(connection, local_old_id)
        for row in rows:
            old_id = str(row["old_id"])
            rotated = old_id == local_old_id
            connection.execute(
                """
                INSERT INTO identity_cutover_journal(
                    old_id, new_id, disposition, phase, token,
                    had_machine, session_count, machine_snapshot
                )
                VALUES (%s, %s, %s, 'started', %s, %s, %s, %s)
                """,
                (
                    old_id,
                    uuid.uuid4() if rotated else None,
                    "rotated" if rotated else "retired",
                    uuid.uuid4(),
                    bool(row["had_machine"]),
                    int(row["session_count"]),
                    Jsonb(row["machine_snapshot"]),
                ),
            )
        inventory = tuple(
            (str(row["old_id"]), bool(row["had_machine"]), int(row["session_count"]))
            for row in rows
        )
    return True, inventory


def _inventory_rows(
    connection: psycopg.Connection[Any],
    local_old_id: str,
) -> list[Mapping[str, Any]]:
    return list(
        connection.execute(
            """
            WITH identities AS (
                SELECT machine_id AS old_id FROM machines
                UNION
                SELECT machine_id FROM sessions WHERE machine_id IS NOT NULL
                UNION
                SELECT %s
            )
            SELECT identities.old_id,
                   machine.machine_id IS NOT NULL AS had_machine,
                   COUNT(session.id) AS session_count,
                   to_jsonb(machine) AS machine_snapshot
            FROM identities
            LEFT JOIN machines AS machine ON machine.machine_id = identities.old_id
            LEFT JOIN sessions AS session ON session.machine_id = identities.old_id
            GROUP BY identities.old_id, machine.machine_id
            ORDER BY identities.old_id
            """,
            (local_old_id,),
        ).fetchall()
    )


def _inventory(connection: psycopg.Connection[Any]) -> tuple[tuple[str, bool, int], ...]:
    rotated = connection.execute(
        "SELECT old_id FROM identity_cutover_journal WHERE disposition = 'rotated'"
    ).fetchone()
    if rotated is None:
        raise IdentityCutoverError("identity cutover journal has no rotated identity")
    return tuple(
        (str(row["old_id"]), bool(row["had_machine"]), int(row["session_count"]))
        for row in _inventory_rows(connection, str(rotated["old_id"]))
    )


def _journal_inventory(
    connection: psycopg.Connection[Any],
) -> tuple[tuple[str, bool, int], ...]:
    rows = connection.execute(
        """
        SELECT old_id, had_machine, session_count
        FROM identity_cutover_journal
        ORDER BY old_id
        """
    ).fetchall()
    return tuple(
        (str(row["old_id"]), bool(row["had_machine"]), int(row["session_count"])) for row in rows
    )


def _journal_is_unstarted(connection: psycopg.Connection[Any]) -> bool:
    row = connection.execute(
        "SELECT COALESCE(BOOL_AND(phase = 'started'), FALSE) AS unstarted "
        "FROM identity_cutover_journal"
    ).fetchone()
    return bool(row and row["unstarted"])


def _assert_no_foreign_application_connections(connection: psycopg.Connection[Any]) -> None:
    rows = connection.execute(
        """
        SELECT pid, application_name
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND backend_type = 'client backend'
        ORDER BY pid
        """
    ).fetchall()
    if rows:
        names = ", ".join(str(row["application_name"] or row["pid"]) for row in rows)
        raise IdentityCutoverError(f"foreign application connection blocks cutover: {names}")


def _process_journal(
    connection: psycopg.Connection[Any],
    identity_file: Path,
    fault: FaultInjector,
) -> None:
    rows = connection.execute(
        """
        SELECT * FROM identity_cutover_journal
        ORDER BY (disposition = 'rotated'), old_id
        """
    ).fetchall()
    for row in rows:
        old_id = str(row["old_id"])
        phase = str(row["phase"])
        if phase == "started":
            _commit_database_identity(connection, row)
            phase = "db_committed"
            fault("after_db_commit", old_id)
        if phase == "db_committed":
            if row["disposition"] == "rotated":
                new_id = _canonical_uuid(str(row["new_id"]))
                durable_replace_text(identity_file, new_id)
                fault("after_file_replace", old_id)
            connection.execute(
                """
                UPDATE identity_cutover_journal
                SET phase = 'file_committed', file_committed_at = NOW()
                WHERE old_id = %s AND phase = 'db_committed'
                """,
                (old_id,),
            )
            fault("after_file_commit", old_id)


def _commit_database_identity(
    connection: psycopg.Connection[Any],
    row: Mapping[str, Any],
) -> None:
    old_id = str(row["old_id"])
    token = str(row["token"])
    with connection.transaction():
        connection.execute("SELECT set_config('gobby.identity_cutover', %s, TRUE)", (token,))
        if row["disposition"] == "rotated":
            snapshot = row["machine_snapshot"] or {}
            connection.execute(
                """
                INSERT INTO machines(
                    machine_id, hostname, os, label, tailscale_name, owner_user_id,
                    first_seen, last_seen
                ) VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), COALESCE(%s, NOW()))
                ON CONFLICT(machine_id) DO NOTHING
                """,
                (
                    row["new_id"],
                    snapshot.get("hostname"),
                    snapshot.get("os"),
                    snapshot.get("label"),
                    snapshot.get("tailscale_name"),
                    snapshot.get("owner_user_id"),
                    snapshot.get("first_seen"),
                    snapshot.get("last_seen"),
                ),
            )
            connection.execute(
                "UPDATE sessions SET machine_id = %s WHERE machine_id = %s",
                (row["new_id"], old_id),
            )
        else:
            connection.execute(
                "UPDATE sessions SET machine_id = NULL WHERE machine_id = %s", (old_id,)
            )
            connection.execute(
                """
                INSERT INTO retired_machine_identities(old_id, disposition)
                VALUES (%s, 'identity-cutover-retired')
                ON CONFLICT(old_id) DO NOTHING
                """,
                (old_id,),
            )
        connection.execute("DELETE FROM machines WHERE machine_id = %s", (old_id,))
        connection.execute(
            """
            UPDATE identity_cutover_journal
            SET phase = 'db_committed', db_committed_at = NOW()
            WHERE old_id = %s AND phase = 'started'
            """,
            (old_id,),
        )


def _assert_zero_unmapped(connection: psycopg.Connection[Any]) -> None:
    machine_column = _machine_identity_column(connection)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM sessions AS session
        LEFT JOIN machines AS machine
          ON machine.{machine_column}::TEXT = session.machine_id
        WHERE session.machine_id IS NOT NULL
          AND machine.{machine_column} IS NULL
        """  # nosec B608 - catalog-selected identifier
    ).fetchone()
    if row is None or int(row["count"]) != 0:
        count = "unknown" if row is None else row["count"]
        raise IdentityCutoverError(f"zero-unmapped gate found {count} session identities")


def _machine_identity_column(connection: psycopg.Connection[Any]) -> str:
    row = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'machines'
          AND column_name IN ('id', 'machine_id')
        ORDER BY CASE column_name WHEN 'id' THEN 0 ELSE 1 END
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise IdentityCutoverError("machines identity column is missing")
    column = str(row["column_name"])
    if column not in {"id", "machine_id"}:
        raise IdentityCutoverError("machines identity column is invalid")
    return column


def _report(connection: psycopg.Connection[Any]) -> CutoverReport:
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS completed,
            COUNT(*) FILTER (WHERE disposition = 'retired') AS retired,
            MAX(new_id::TEXT) FILTER (WHERE disposition = 'rotated') AS rotated_id
        FROM identity_cutover_journal
        WHERE phase = 'file_committed'
        """
    ).fetchone()
    if row is None or row["rotated_id"] is None:
        raise IdentityCutoverError("identity cutover has no completed rotated identity")
    return CutoverReport(
        rotated_id=str(row["rotated_id"]),
        completed_identities=int(row["completed"]),
        retired_identities=int(row["retired"]),
    )


def _read_identity_file(path: Path) -> str:
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise IdentityCutoverError(f"cannot read local identity file {path}: {exc}") from exc
    if not value:
        raise IdentityCutoverError(f"local identity file is empty: {path}")
    return value


def _canonical_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise IdentityCutoverError(f"identity is not a UUID: {value}") from exc


def _no_fault(_point: str, _old_id: str) -> None:
    return None


__all__ = [
    "CutoverReport",
    "FaultInjector",
    "IdentityCutoverError",
    "run_identity_cutover",
    "verify_identity_cutover",
]
