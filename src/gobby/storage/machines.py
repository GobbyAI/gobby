"""Machine registry storage manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import parse_stored_datetime, utc_now
from gobby.utils.machine_id import is_legacy_missing_machine_id

_PLACEHOLDER_MACHINE_IDS = {
    "none",
    "null",
    "unknown",
    "unknown-machine",
    "unknown_machine",
}


def normalize_machine_id(machine_id: str | None) -> str | None:
    """Return a storable machine id or None for missing/placeholder values."""
    normalized = (machine_id or "").strip()
    if not normalized:
        return None
    if is_legacy_missing_machine_id(normalized):
        return None
    if normalized.lower() in _PLACEHOLDER_MACHINE_IDS:
        return None
    return normalized


def _clean_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


@dataclass(frozen=True)
class Machine:
    """Machine registry row."""

    machine_id: str
    hostname: str | None
    os: str | None
    label: str | None
    tailscale_name: str | None
    owner_user_id: str | None
    first_seen: str
    last_seen: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Machine:
        """Create a Machine from a database row."""
        return cls(
            machine_id=row["machine_id"],
            hostname=row.get("hostname"),
            os=row.get("os"),
            label=row.get("label"),
            tailscale_name=row.get("tailscale_name"),
            owner_user_id=row.get("owner_user_id"),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "os": self.os,
            "label": self.label,
            "tailscale_name": self.tailscale_name,
            "owner_user_id": self.owner_user_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


class LocalMachineManager:
    """Manager for machine registry storage."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def upsert_seen(
        self,
        machine_id: str | None,
        *,
        hostname: str | None = None,
        os: str | None = None,
        label: str | None = None,
        tailscale_name: str | None = None,
        owner_user_id: str | None = None,
        seen_at: datetime | str | None = None,
    ) -> Machine | None:
        """Insert or refresh a real machine id, skipping missing placeholders."""
        normalized_machine_id = normalize_machine_id(machine_id)
        if normalized_machine_id is None:
            return None

        now = parse_stored_datetime(seen_at) or utc_now()
        row = self.db.fetchone(
            """
            INSERT INTO machines (
                machine_id, hostname, os, label, tailscale_name, owner_user_id,
                first_seen, last_seen
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(machine_id) DO UPDATE SET
                hostname = COALESCE(EXCLUDED.hostname, machines.hostname),
                os = COALESCE(EXCLUDED.os, machines.os),
                label = COALESCE(EXCLUDED.label, machines.label),
                tailscale_name = COALESCE(EXCLUDED.tailscale_name, machines.tailscale_name),
                owner_user_id = COALESCE(EXCLUDED.owner_user_id, machines.owner_user_id),
                last_seen = EXCLUDED.last_seen
            RETURNING *
            """,
            (
                normalized_machine_id,
                _clean_optional_text(hostname),
                _clean_optional_text(os),
                _clean_optional_text(label),
                _clean_optional_text(tailscale_name),
                _clean_optional_text(owner_user_id),
                now,
                now,
            ),
        )
        return Machine.from_row(row) if row else None

    def get(self, machine_id: str) -> Machine | None:
        """Return a machine by id."""
        normalized_id = normalize_machine_id(machine_id)
        if normalized_id is None:
            return None
        row = self.db.fetchone(
            "SELECT * FROM machines WHERE machine_id = %s",
            (normalized_id,),
        )
        return Machine.from_row(row) if row else None
