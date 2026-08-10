"""Durable embedding projection sequence, generation acknowledgements, and leases."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from gobby.config.embedding_keys import (
    EMBEDDING_SWITCH_COMPLETED_KEY,
    EMBEDDING_SWITCH_JOURNAL_KEY,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase, Transaction

_SOURCE_KINDS = frozenset({"memory", "tool", "github_issue"})
_SOURCE_COLLECTIONS = {
    "memory": "memories",
    "tool": "tool_embeddings",
    "github_issue": "gobby_github_issues",
}


class EmbeddingGenerationError(RuntimeError):
    """Base error for generation coordination failures."""


class EmbeddingGenerationLeaseLost(EmbeddingGenerationError):
    """Raised after a serving lease can no longer be trusted."""


class EmbeddingGenerationNotCaughtUp(EmbeddingGenerationError):
    """Raised when a daemon acknowledges before replay reaches the watermark."""


@dataclass(frozen=True, slots=True)
class ProjectionChange:
    sequence: int
    source_kind: str
    source_id: str
    is_tombstone: bool


class EmbeddingServingLease:
    """Local self-fence backed by a renewable database lease."""

    def __init__(
        self,
        state: EmbeddingGenerationState,
        daemon_instance_id: UUID,
        generation: str,
        revision: int,
        lease_seconds: float,
        *,
        caught_up_watermark: int | None = None,
        required_watermark: int | None = None,
    ) -> None:
        self._state = state
        self._daemon_instance_id = daemon_instance_id
        self._generation = generation
        self._revision = revision
        self._lease_seconds = lease_seconds
        self._caught_up_watermark = caught_up_watermark
        self._required_watermark = required_watermark
        self._deadline = 0.0
        self._activated = False
        self._fenced = False

    def activate(self) -> None:
        if self._activated:
            return
        self._state.acknowledge(
            self._daemon_instance_id,
            self._generation,
            self._revision,
            lease_seconds=self._lease_seconds,
            caught_up_watermark=self._caught_up_watermark,
            required_watermark=self._required_watermark,
        )
        self._deadline = time.monotonic() + self._lease_seconds
        self._activated = True

    def fence(self) -> None:
        self._fenced = True

    def renew(self) -> None:
        self.assert_serving()
        try:
            self._state.renew(
                self._daemon_instance_id,
                self._generation,
                self._revision,
                lease_seconds=self._lease_seconds,
            )
        except Exception as exc:
            self._fenced = True
            raise EmbeddingGenerationLeaseLost("Embedding generation lease renewal failed") from exc
        self._deadline = time.monotonic() + self._lease_seconds

    def assert_serving(self) -> None:
        if self._fenced:
            raise EmbeddingGenerationLeaseLost("Embedding generation serving is fenced")
        if not self._activated:
            raise EmbeddingGenerationLeaseLost(
                "Embedding generation serving lease has not been activated"
            )
        if time.monotonic() >= self._deadline:
            self._fenced = True
            raise EmbeddingGenerationLeaseLost("Embedding generation serving lease expired")


class EmbeddingGenerationState:
    """Single storage owner for generation catch-up and serving eligibility."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def append_change(
        self,
        source_kind: str,
        source_id: str,
        *,
        is_tombstone: bool = False,
        transaction: Transaction | None = None,
    ) -> int:
        if source_kind not in _SOURCE_KINDS:
            raise ValueError(f"Unsupported embedding projection source kind: {source_kind}")
        if not source_id:
            raise ValueError("Embedding projection source ID is required")
        if transaction is not None:
            return self._insert_change(transaction, source_kind, source_id, is_tombstone)
        with self.db.transaction() as owned:
            return self._insert_change(owned, source_kind, source_id, is_tombstone)

    @staticmethod
    def _insert_change(
        transaction: Transaction,
        source_kind: str,
        source_id: str,
        is_tombstone: bool,
    ) -> int:
        row = transaction.execute(
            """
            INSERT INTO embedding_projection_changes (source_kind, source_id, is_tombstone)
            VALUES (%s, %s, %s)
            RETURNING sequence
            """,
            (source_kind, source_id, is_tombstone),
        ).fetchone()
        if row is None:
            raise EmbeddingGenerationError("Embedding projection change was not persisted")
        return int(row["sequence"])

    def watermark(self) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(sequence), 0) AS watermark FROM embedding_projection_changes"
        )
        return 0 if row is None else int(row["watermark"])

    def changes_after(self, sequence: int) -> list[ProjectionChange]:
        rows = self.db.fetchall(
            """
            SELECT sequence, source_kind, source_id, is_tombstone
            FROM embedding_projection_changes
            WHERE sequence > %s
            ORDER BY sequence
            """,
            (sequence,),
        )
        return [
            ProjectionChange(
                sequence=int(row["sequence"]),
                source_kind=str(row["source_kind"]),
                source_id=str(row["source_id"]),
                is_tombstone=bool(row["is_tombstone"]),
            )
            for row in rows
        ]

    def projection_targets(self, source_kind: str, primary: str) -> tuple[str, ...]:
        """Resolve primary plus any durable transition/completed physical target."""
        if source_kind not in _SOURCE_KINDS:
            raise ValueError(f"Unsupported embedding projection source kind: {source_kind}")
        collection_kind = _SOURCE_COLLECTIONS[source_kind]
        config_store = ConfigStore(self.db)
        targets = [primary]
        for key in (EMBEDDING_SWITCH_JOURNAL_KEY, EMBEDDING_SWITCH_COMPLETED_KEY):
            raw = config_store.get_internal_lifecycle(key)
            if not isinstance(raw, dict):
                continue
            physical_names = raw.get("physical_names")
            if not isinstance(physical_names, dict):
                continue
            target = physical_names.get(collection_kind)
            if isinstance(target, str) and target and target not in targets:
                targets.append(target)
        return tuple(targets)

    def replay_after(
        self,
        sequence: int,
        projector: Callable[[ProjectionChange], object],
    ) -> int:
        caught_up = sequence
        for change in self.changes_after(sequence):
            projector(change)
            caught_up = change.sequence
        return caught_up

    def acknowledge(
        self,
        daemon_instance_id: UUID,
        generation: str,
        revision: int,
        *,
        lease_seconds: float,
        acknowledged: bool = True,
        caught_up_watermark: int | None = None,
        required_watermark: int | None = None,
    ) -> None:
        self._validate_lease_seconds(lease_seconds)
        if required_watermark is not None and (
            caught_up_watermark is None or caught_up_watermark < required_watermark
        ):
            raise EmbeddingGenerationNotCaughtUp(
                f"Embedding generation replay reached {caught_up_watermark or 0}; "
                f"required {required_watermark}"
            )
        self.db.execute(
            """
            INSERT INTO embedding_generation_acks (
                daemon_instance_id, generation, committed_revision,
                acknowledged, lease_expires_at, updated_at
            )
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), CURRENT_TIMESTAMP)
            ON CONFLICT (daemon_instance_id) DO UPDATE SET
                generation = EXCLUDED.generation,
                committed_revision = EXCLUDED.committed_revision,
                acknowledged = EXCLUDED.acknowledged,
                lease_expires_at = EXCLUDED.lease_expires_at,
                updated_at = EXCLUDED.updated_at
            """,
            (daemon_instance_id, generation, revision, acknowledged, lease_seconds),
        )

    def renew(
        self,
        daemon_instance_id: UUID,
        generation: str,
        revision: int,
        *,
        lease_seconds: float,
    ) -> None:
        self._validate_lease_seconds(lease_seconds)
        cursor = self.db.execute(
            """
            UPDATE embedding_generation_acks
            SET lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                updated_at = CURRENT_TIMESTAMP
            WHERE daemon_instance_id = %s
              AND generation = %s
              AND committed_revision = %s
              AND acknowledged IS TRUE
            """,
            (lease_seconds, daemon_instance_id, generation, revision),
        )
        if cursor.rowcount != 1:
            raise EmbeddingGenerationLeaseLost("Embedding generation lease no longer matches")

    def serving_lease(
        self,
        daemon_instance_id: UUID,
        generation: str,
        revision: int,
        *,
        lease_seconds: float,
    ) -> EmbeddingServingLease:
        lease = self.prepare_serving_lease(
            daemon_instance_id,
            generation,
            revision,
            lease_seconds=lease_seconds,
        )
        lease.activate()
        return lease

    def prepare_serving_lease(
        self,
        daemon_instance_id: UUID,
        generation: str,
        revision: int,
        *,
        lease_seconds: float,
        caught_up_watermark: int | None = None,
        required_watermark: int | None = None,
    ) -> EmbeddingServingLease:
        self._validate_lease_seconds(lease_seconds)
        return EmbeddingServingLease(
            self,
            daemon_instance_id,
            generation,
            revision,
            lease_seconds,
            caught_up_watermark=caught_up_watermark,
            required_watermark=required_watermark,
        )

    def can_collect(self, generation: str, revision: int, *, drains_complete: bool) -> bool:
        if not drains_complete:
            return False
        row = self.db.fetchone(
            """
            SELECT COUNT(*) AS incompatible
            FROM embedding_generation_acks
            WHERE lease_expires_at > CURRENT_TIMESTAMP
              AND (
                  generation IS DISTINCT FROM %s
                  OR committed_revision IS DISTINCT FROM %s
                  OR acknowledged IS NOT TRUE
              )
            """,
            (generation, revision),
        )
        return row is not None and int(row["incompatible"]) == 0

    @staticmethod
    def _validate_lease_seconds(lease_seconds: float) -> None:
        if lease_seconds <= 0:
            raise ValueError("Embedding generation lease duration must be positive")


__all__ = [
    "EmbeddingGenerationLeaseLost",
    "EmbeddingGenerationNotCaughtUp",
    "EmbeddingGenerationState",
    "EmbeddingServingLease",
    "ProjectionChange",
]
