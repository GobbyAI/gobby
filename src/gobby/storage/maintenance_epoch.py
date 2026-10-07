"""Release a maintenance epoch that a restored hub backup carries.

Hub maintenance campaigns are retired. A backup taken during one can still restore
an open epoch together with the LOGIN guard that enforces it, and that guard refuses
every login without the epoch token. Restore releases the epoch. When the restored
hub has no maintenance_epochs table there is nothing to release.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row


def release_restored_maintenance_epoch(database_url: str) -> uuid.UUID | None:
    """Release an open epoch copied into a disaster-recovery target; return its id."""
    epoch_id = _discover_open_epoch(database_url)
    if epoch_id is None:
        return None

    epoch_url = _with_option(database_url, f"-c gobby.maintenance_epoch={epoch_id}")
    with _connect(epoch_url, application_name="gobby-hub-backup-restore") as connection:
        row = connection.execute(
            """
            UPDATE maintenance_epochs
            SET released_at = NOW(),
                released_by_command = 'restore'
            WHERE id = %s
              AND released_at IS NULL
            RETURNING id
            """,
            (epoch_id,),
        ).fetchone()
    return cast(uuid.UUID, row["id"]) if row is not None else None


def _discover_open_epoch(database_url: str) -> uuid.UUID | None:
    """Read the open epoch, through PostgreSQL's superuser-only bypass if the guard refuses."""
    try:
        return _read_open_epoch(database_url)
    except psycopg.Error as exc:
        try:
            return _read_open_epoch(_with_option(database_url, "-c event_triggers=off"))
        except psycopg.Error:
            raise exc from None


def _read_open_epoch(database_url: str) -> uuid.UUID | None:
    with _connect(database_url, application_name="gobby-maintenance-discover") as connection:
        relation = connection.execute(
            "SELECT pg_catalog.to_regclass('maintenance_epochs') AS relation"
        ).fetchone()
        if relation is None or relation["relation"] is None:
            return None
        row = connection.execute(
            "SELECT id FROM maintenance_epochs WHERE released_at IS NULL"
        ).fetchone()
    return cast(uuid.UUID, row["id"]) if row is not None else None


def _with_option(database_url: str, option: str) -> str:
    fields = conninfo_to_dict(database_url)
    existing = fields.get("options")
    fields["options"] = f"{existing if existing is not None else ''} {option}".strip()
    return make_conninfo(
        "", **{key: str(value) for key, value in fields.items() if value is not None}
    )


def _connect(database_url: str, *, application_name: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        database_url,
        autocommit=True,
        application_name=application_name,
        connect_timeout=5,
        row_factory=dict_row,
    )
