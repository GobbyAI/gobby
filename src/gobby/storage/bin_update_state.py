"""Storage helpers for managed native binary update state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from gobby.storage.database import DatabaseProtocol

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
    tool_name TEXT PRIMARY KEY,
    installed_version TEXT,
    floor_version TEXT NOT NULL,
    latest_version TEXT,
    binary_path TEXT,
    target TEXT,
    last_status TEXT NOT NULL CHECK (last_status IN ({_STATUS_SQL})),
    last_error TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    installed_at TEXT,
    source_url TEXT,
    is_dev INTEGER NOT NULL DEFAULT 0 CHECK (is_dev IN (0, 1)),
    floor_drift INTEGER NOT NULL DEFAULT 0 CHECK (floor_drift IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


_RECORD_COLUMNS = """
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


def _row_to_record(row: sqlite3.Row) -> BinUpdateRecord:
    return BinUpdateRecord(
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


@dataclass(frozen=True)
class BinUpdateRecord:
    """Persisted update state for one managed native binary."""

    tool_name: str
    installed_version: str | None
    floor_version: str
    latest_version: str | None
    binary_path: str | None
    target: str | None
    last_status: BinUpdateStatus
    last_error: str | None
    checked_at: str
    installed_at: str | None
    source_url: str | None
    is_dev: bool
    floor_drift: bool


class BinUpdateStateStore:
    """Read and write rows in ``bin_update_state``."""

    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db

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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(tool_name) DO UPDATE SET
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
                int(is_dev),
                int(floor_drift),
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
             WHERE tool_name = ?
            """,
            (tool_name,),
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
             ORDER BY tool_name
            """
        )
        return [_row_to_record(row) for row in rows]


__all__ = [
    "BIN_UPDATE_STATE_SCHEMA",
    "BIN_UPDATE_STATUS_VALUES",
    "BinUpdateRecord",
    "BinUpdateStateStore",
    "BinUpdateStatus",
]
