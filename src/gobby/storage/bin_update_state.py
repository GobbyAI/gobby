"""Storage helpers for managed native binary update state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import normalize_machine_id
from gobby.utils.datetime import normalize_datetime_model

BinUpdateStatus = Literal[
    "updated",
    "up_to_date",
    "failed",
    "floor_violated",
    "dev",
    "source_unavailable",
]

BIN_UPDATE_STATUS_VALUES = (
    "updated",
    "up_to_date",
    "failed",
    "floor_violated",
    "dev",
    "source_unavailable",
)

_STATUS_SQL = ",".join(f"'{status}'" for status in BIN_UPDATE_STATUS_VALUES)

BIN_UPDATE_STATE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS bin_update_state (
    machine_id UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    installed_version TEXT,
    floor_version TEXT NOT NULL,
    latest_version TEXT,
    binary_path TEXT,
    target TEXT,
    last_status TEXT NOT NULL CHECK (last_status IN ({_STATUS_SQL})),
    last_error TEXT,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    installed_at TEXT,
    source_url TEXT,
    is_dev BOOLEAN NOT NULL DEFAULT FALSE CHECK (is_dev IN (FALSE, TRUE)),
    floor_drift BOOLEAN NOT NULL DEFAULT FALSE CHECK (floor_drift IN (FALSE, TRUE)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (machine_id, tool_name)
);
"""


_RECORD_COLUMNS = """
            machine_id,
            tool_name,
            installed_version,
            floor_version,
            latest_version,
            binary_path,
            target,
            last_status,
            last_error,
            checked_at,
            installed_at,
            source_url,
            is_dev,
            floor_drift"""


def _row_to_record(row: Mapping[str, Any]) -> BinUpdateRecord:
    return BinUpdateRecord(
        machine_id=str(row["machine_id"]),
        tool_name=row["tool_name"],
        installed_version=row["installed_version"],
        floor_version=row["floor_version"],
        latest_version=row["latest_version"],
        binary_path=row["binary_path"],
        target=row["target"],
        last_status=row["last_status"],
        last_error=row["last_error"],
        checked_at=row["checked_at"],
        installed_at=row["installed_at"],
        source_url=row["source_url"],
        is_dev=bool(row["is_dev"]),
        floor_drift=bool(row["floor_drift"]),
    )


@normalize_datetime_model(required=("checked_at",), optional=("installed_at",))
@dataclass(frozen=True)
class BinUpdateRecord:
    """Persisted update state for one managed native binary."""

    machine_id: str
    tool_name: str
    installed_version: str | None
    floor_version: str
    latest_version: str | None
    binary_path: str | None
    target: str | None
    last_status: BinUpdateStatus
    last_error: str | None
    checked_at: datetime
    installed_at: datetime | None
    source_url: str | None
    is_dev: bool
    floor_drift: bool


class BinUpdateStateStore:
    """Read and write rows in ``bin_update_state``."""

    def __init__(self, db: HubDatabase, *, machine_id: str) -> None:
        self.db = db
        normalized = normalize_machine_id(machine_id)
        if normalized is None:
            raise ValueError("machine_id is required for bin update state")
        self.machine_id = normalized

    def upsert(
        self,
        *,
        tool_name: str,
        installed_version: str | None,
        floor_version: str,
        latest_version: str | None,
        binary_path: str | None,
        target: str | None,
        last_status: BinUpdateStatus,
        last_error: str | None,
        installed_at: str | None,
        source_url: str | None,
        is_dev: bool,
        floor_drift: bool,
    ) -> BinUpdateRecord:
        """Insert or replace update state for a managed binary."""
        self.db.execute(
            """
            INSERT INTO bin_update_state (
                machine_id,
                tool_name,
                installed_version,
                floor_version,
                latest_version,
                binary_path,
                target,
                last_status,
                last_error,
                checked_at,
                installed_at,
                source_url,
                is_dev,
                floor_drift,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(machine_id, tool_name) DO UPDATE SET
                installed_version = excluded.installed_version,
                floor_version = excluded.floor_version,
                latest_version = excluded.latest_version,
                binary_path = excluded.binary_path,
                target = excluded.target,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                checked_at = excluded.checked_at,
                installed_at = excluded.installed_at,
                source_url = excluded.source_url,
                is_dev = excluded.is_dev,
                floor_drift = excluded.floor_drift,
                updated_at = excluded.updated_at
            """,
            (
                self.machine_id,
                tool_name,
                installed_version,
                floor_version,
                latest_version,
                binary_path,
                target,
                last_status,
                last_error,
                installed_at,
                source_url,
                bool(is_dev),
                bool(floor_drift),
            ),
        )
        record = self.get(tool_name)
        if record is None:
            raise RuntimeError(f"bin update state row disappeared: {tool_name}")
        return record

    def get(self, tool_name: str) -> BinUpdateRecord | None:
        """Return update state for one managed binary."""
        row = self.db.fetchone(
            f"""
            SELECT {_RECORD_COLUMNS}
             FROM bin_update_state
             WHERE machine_id = %s AND tool_name = %s
            """,
            (self.machine_id, tool_name),
        )
        if row is None:
            return None
        return _row_to_record(row)

    def list(self) -> list[BinUpdateRecord]:
        """Return all managed binary update states."""
        rows = self.db.fetchall(
            f"""
            SELECT {_RECORD_COLUMNS}
              FROM bin_update_state
             WHERE machine_id = %s
             ORDER BY tool_name
            """,
            (self.machine_id,),
        )
        return [_row_to_record(row) for row in rows]


__all__ = [
    "BIN_UPDATE_STATE_SCHEMA",
    "BIN_UPDATE_STATUS_VALUES",
    "BinUpdateRecord",
    "BinUpdateStateStore",
    "BinUpdateStatus",
]
