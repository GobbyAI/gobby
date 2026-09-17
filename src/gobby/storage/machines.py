"""Machine registry storage manager."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from gobby.storage.hub.protocol import HubDatabase, Row, Transaction
from gobby.utils.datetime import normalize_datetime_model, parse_stored_datetime, utc_now


class MachineOwnershipConflictError(RuntimeError):
    """Raised when a machine is claimed by a different canonical user."""

    def __init__(self, machine_id: str, owner_user_id: str, requested_user_id: str) -> None:
        self.machine_id = machine_id
        self.owner_user_id = owner_user_id
        self.requested_user_id = requested_user_id
        super().__init__(
            f"Machine {machine_id} is owned by user {owner_user_id}; "
            f"user {requested_user_id} cannot claim it"
        )


class MachineNotRegisteredError(RuntimeError):
    """Raised when untrusted ingress references an unknown machine."""


_NODE_REF_RE = re.compile(r"n([1-9][0-9]*)")


def _clean_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def parse_node_ref(reference: str) -> int | None:
    """Return the number of an ``n#`` node ref, or None for any other text."""
    match = _NODE_REF_RE.fullmatch(reference.strip())
    return int(match.group(1)) if match else None


def lowest_free_ref(taken: Iterable[int]) -> int:
    """Return the lowest positive integer absent from ``taken``, so released refs are reused."""
    used = set(taken)
    ref = 1
    while ref in used:
        ref += 1
    return ref


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
    owner_user_id: str
    first_seen: datetime
    last_seen: datetime
    ref: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Machine:
        """Create a Machine from a database row."""
        owner_user_id = row["owner_user_id"]
        if owner_user_id is None:
            raise ValueError("Machine row is missing owner_user_id")
        return cls(
            id=str(row["id"]),
            hostname=row.get("hostname"),
            os=row.get("os"),
            label=row.get("label"),
            tailscale_name=row.get("tailscale_name"),
            owner_user_id=str(owner_user_id),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            ref=row["ref"],
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
            "ref": self.ref,
        }


class LocalMachineManager:
    """Manager for machine registry storage."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def upsert_seen(
        self,
        machine_id: str,
        owner_user_id: str,
        *,
        hostname: str | None = None,
        os: str | None = None,
        label: str | None = None,
        tailscale_name: str | None = None,
        seen_at: datetime | str | None = None,
    ) -> Machine:
        """Register a machine or refresh it when the canonical owner matches.

        A machine without a node ref gets the lowest ref free for its owner.
        """
        normalized_machine_id = str(UUID(machine_id.strip()))
        normalized_owner_id = str(UUID(owner_user_id.strip()))

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
                    last_seen = CASE
                        WHEN EXCLUDED.last_seen >= machines.last_seen + INTERVAL '5 minutes'
                        THEN EXCLUDED.last_seen
                        ELSE machines.last_seen
                    END
                WHERE machines.owner_user_id = EXCLUDED.owner_user_id
                  AND (EXCLUDED.last_seen >= machines.last_seen + INTERVAL '5 minutes'
                   OR EXCLUDED.hostname IS NOT NULL
                      AND EXCLUDED.hostname IS DISTINCT FROM machines.hostname
                   OR EXCLUDED.os IS NOT NULL AND EXCLUDED.os IS DISTINCT FROM machines.os
                   OR EXCLUDED.label IS NOT NULL AND EXCLUDED.label IS DISTINCT FROM machines.label
                   OR EXCLUDED.tailscale_name IS NOT NULL
                      AND EXCLUDED.tailscale_name IS DISTINCT FROM machines.tailscale_name
                  )
                RETURNING *
            )
            SELECT * FROM changed
            UNION ALL
            SELECT * FROM machines
            WHERE id = %s
              AND owner_user_id = %s
              AND NOT EXISTS (SELECT 1 FROM changed)
            LIMIT 1
            """,
            (
                normalized_machine_id,
                _clean_optional_text(hostname),
                _clean_optional_text(os),
                _clean_optional_text(label),
                _clean_optional_text(tailscale_name),
                normalized_owner_id,
                now,
                now,
                normalized_machine_id,
                normalized_owner_id,
            ),
        )
        if row is not None and row["ref"] is None:
            with self.db.transaction() as conn:
                row = _allocate_node_ref(conn, normalized_machine_id, normalized_owner_id)
        if row is None:
            existing = self.get(normalized_machine_id)
            if existing is None:
                raise RuntimeError(f"Machine upsert returned no row: {normalized_machine_id}")
            raise MachineOwnershipConflictError(
                normalized_machine_id,
                existing.owner_user_id,
                normalized_owner_id,
            )
        return Machine.from_row(row)

    def refresh_seen(
        self,
        machine_id: str,
        *,
        hostname: str | None = None,
        os: str | None = None,
        label: str | None = None,
        tailscale_name: str | None = None,
        seen_at: datetime | str | None = None,
    ) -> Machine | None:
        """Refresh known-machine metadata without establishing ownership."""
        normalized_machine_id = str(UUID(machine_id.strip()))
        now = parse_stored_datetime(seen_at) or utc_now()
        row = self.db.fetchone(
            """
            WITH changed AS (
                UPDATE machines
                SET hostname = COALESCE(%s, hostname),
                    os = COALESCE(%s, os),
                    label = COALESCE(%s, label),
                    tailscale_name = COALESCE(%s, tailscale_name),
                    last_seen = CASE
                        WHEN %s >= last_seen + INTERVAL '5 minutes' THEN %s
                        ELSE last_seen
                    END
                WHERE id = %s
                  AND (%s >= last_seen + INTERVAL '5 minutes'
                    OR COALESCE(%s, hostname) IS DISTINCT FROM hostname
                    OR COALESCE(%s, os) IS DISTINCT FROM os
                    OR COALESCE(%s, label) IS DISTINCT FROM label
                    OR COALESCE(%s, tailscale_name) IS DISTINCT FROM tailscale_name)
                RETURNING *
            )
            SELECT * FROM changed
            UNION ALL
            SELECT * FROM machines
            WHERE id = %s AND NOT EXISTS (SELECT 1 FROM changed)
            LIMIT 1
            """,
            (
                _clean_optional_text(hostname),
                _clean_optional_text(os),
                _clean_optional_text(label),
                _clean_optional_text(tailscale_name),
                now,
                now,
                normalized_machine_id,
                now,
                _clean_optional_text(hostname),
                _clean_optional_text(os),
                _clean_optional_text(label),
                _clean_optional_text(tailscale_name),
                normalized_machine_id,
            ),
        )
        return Machine.from_row(row) if row else None

    def get(self, machine_id: str, *, owner_user_id: str | None = None) -> Machine | None:
        """Return a machine by id, or by ``n#`` node ref within one owner's machines."""
        node_ref = parse_node_ref(machine_id)
        if node_ref is None:
            row = self.db.fetchone(
                "SELECT * FROM machines WHERE id = %s",
                (str(UUID(machine_id.strip())),),
            )
        elif owner_user_id is None:
            raise ValueError(f"Node ref {machine_id.strip()!r} needs an owner to resolve")
        else:
            row = self.db.fetchone(
                "SELECT * FROM machines WHERE owner_user_id = %s AND ref = %s",
                (str(UUID(owner_user_id.strip())), node_ref),
            )
        return Machine.from_row(row) if row else None

    def list_for_user(self, user_id: str) -> list[Machine]:
        """Return every machine owned by one canonical user."""
        normalized_user_id = str(UUID(user_id.strip()))
        rows = self.db.fetchall(
            "SELECT * FROM machines WHERE owner_user_id = %s ORDER BY first_seen, id",
            (normalized_user_id,),
        )
        return [Machine.from_row(row) for row in rows]


def _allocate_node_ref(conn: Transaction, machine_id: str, owner_user_id: str) -> Row | None:
    """Assign the owner's lowest free node ref while holding the owner row lock."""
    conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (owner_user_id,))
    taken = conn.execute(
        "SELECT ref FROM machines WHERE owner_user_id = %s AND ref IS NOT NULL",
        (owner_user_id,),
    ).fetchall()
    row = conn.execute(
        "UPDATE machines SET ref = %s WHERE id = %s AND ref IS NULL RETURNING *",
        (lowest_free_ref(int(item["ref"]) for item in taken), machine_id),
    ).fetchone()
    if row is not None:
        return row
    # A concurrent upsert assigned the ref first; its commit is visible now.
    return conn.execute("SELECT * FROM machines WHERE id = %s", (machine_id,)).fetchone()
