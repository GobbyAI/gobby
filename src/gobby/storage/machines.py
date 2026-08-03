"""Machine registry storage manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model, parse_stored_datetime, utc_now


def _clean_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


@normalize_datetime_model(
    required=(
        "first_seen",
        "last_seen",
    )
)
@dataclass(frozen=True)
class Machine:
    """Machine registry row."""

    id: str
    hostname: str | None
    os: str | None
    label: str | None
    tailscale_name: str | None
    owner_user_id: str | None
    first_seen: datetime
    last_seen: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Machine:
        """Create a Machine from a database row."""
        return cls(
            id=str(row["id"]),
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
            "id": self.id,
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
        """Insert or refresh a real machine id, skipping missing attribution."""
        if machine_id is None:
            return None
        normalized_machine_id = str(UUID(machine_id.strip()))

        now = parse_stored_datetime(seen_at) or utc_now()
        row = self.db.fetchone(
            """
            WITH changed AS (
                INSERT INTO machines (
                    id, hostname, os, label, tailscale_name, owner_user_id,
                    first_seen, last_seen
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    hostname = COALESCE(EXCLUDED.hostname, machines.hostname),
                    os = COALESCE(EXCLUDED.os, machines.os),
                    label = COALESCE(EXCLUDED.label, machines.label),
                    tailscale_name = COALESCE(EXCLUDED.tailscale_name, machines.tailscale_name),
                    owner_user_id = COALESCE(EXCLUDED.owner_user_id, machines.owner_user_id),
                    last_seen = CASE
                        WHEN EXCLUDED.last_seen >= machines.last_seen + INTERVAL '5 minutes'
                        THEN EXCLUDED.last_seen
                        ELSE machines.last_seen
                    END
                WHERE EXCLUDED.last_seen >= machines.last_seen + INTERVAL '5 minutes'
                   OR EXCLUDED.hostname IS NOT NULL
                      AND EXCLUDED.hostname IS DISTINCT FROM machines.hostname
                   OR EXCLUDED.os IS NOT NULL AND EXCLUDED.os IS DISTINCT FROM machines.os
                   OR EXCLUDED.label IS NOT NULL AND EXCLUDED.label IS DISTINCT FROM machines.label
                   OR EXCLUDED.tailscale_name IS NOT NULL
                      AND EXCLUDED.tailscale_name IS DISTINCT FROM machines.tailscale_name
                   OR EXCLUDED.owner_user_id IS NOT NULL
                      AND EXCLUDED.owner_user_id IS DISTINCT FROM machines.owner_user_id
                RETURNING *
            )
            SELECT * FROM changed
            UNION ALL
            SELECT * FROM machines
            WHERE id = %s AND NOT EXISTS (SELECT 1 FROM changed)
            LIMIT 1
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
                normalized_machine_id,
            ),
        )
        return Machine.from_row(row) if row else None

    def get(self, machine_id: str) -> Machine | None:
        """Return a machine by id."""
        normalized_id = str(UUID(machine_id.strip()))
        row = self.db.fetchone(
            "SELECT * FROM machines WHERE id = %s",
            (normalized_id,),
        )
        return Machine.from_row(row) if row else None
