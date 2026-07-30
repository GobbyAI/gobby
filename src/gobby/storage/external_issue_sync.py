"""Durable per-project external issue synchronization status."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

ExternalIssueProvider = Literal["linear", "github"]
ExternalIssueSyncState = Literal[
    "disabled",
    "pending",
    "running",
    "healthy",
    "degraded",
    "rate_limited",
    "unready",
]


@dataclass(frozen=True)
class ExternalIssueSyncStatus:
    """Last known reconciliation health for one project/provider pair."""

    project_id: str
    provider: ExternalIssueProvider
    state: ExternalIssueSyncState = "disabled"
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_outbound_success_at: datetime | None = None
    linked_count: int = 0
    pending_count: int = 0
    consecutive_failures: int = 0
    retry_at: datetime | None = None
    last_statistics: dict[str, Any] | None = None
    last_error: str | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ExternalIssueSyncStatus:
        raw_statistics = row["last_statistics"] or {}
        if isinstance(raw_statistics, str):
            try:
                raw_statistics = json.loads(raw_statistics)
            except json.JSONDecodeError:
                logger.warning(
                    "Ignoring malformed external issue sync statistics for project %s",
                    row["project_id"],
                )
                raw_statistics = {}
        return cls(
            project_id=str(row["project_id"]),
            provider=row["provider"],
            state=row["state"],
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            last_outbound_success_at=row["last_outbound_success_at"],
            linked_count=int(row["linked_count"]),
            pending_count=int(row["pending_count"]),
            consecutive_failures=int(row["consecutive_failures"]),
            retry_at=row["retry_at"],
            last_statistics=dict(raw_statistics),
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "provider": self.provider,
            "state": self.state,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_outbound_success_at": self.last_outbound_success_at,
            "linked_count": self.linked_count,
            "pending_count": self.pending_count,
            "consecutive_failures": self.consecutive_failures,
            "retry_at": self.retry_at,
            "last_statistics": self.last_statistics or {},
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


class ExternalIssueSyncStatusStore:
    """Read and update external issue synchronization health."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get(
        self, project_id: str, provider: ExternalIssueProvider
    ) -> ExternalIssueSyncStatus | None:
        row = self.db.fetchone(
            "SELECT * FROM external_issue_sync_status WHERE project_id = %s AND provider = %s",
            (project_id, provider),
        )
        return ExternalIssueSyncStatus.from_row(row) if row else None

    def list_for_project(self, project_id: str) -> list[ExternalIssueSyncStatus]:
        rows = self.db.fetchall(
            "SELECT * FROM external_issue_sync_status WHERE project_id = %s ORDER BY provider",
            (project_id,),
        )
        return [ExternalIssueSyncStatus.from_row(row) for row in rows]

    def upsert(
        self,
        *,
        project_id: str,
        provider: ExternalIssueProvider,
        state: ExternalIssueSyncState,
        linked_count: int,
        pending_count: int,
        last_attempt_at: datetime | None = None,
        last_success_at: datetime | None = None,
        last_outbound_success_at: datetime | None = None,
        consecutive_failures: int = 0,
        retry_at: datetime | None = None,
        last_statistics: Mapping[str, Any] | None = None,
        last_error: str | None = None,
    ) -> ExternalIssueSyncStatus:
        now = utc_now()
        row = self.db.fetchone(
            """
            INSERT INTO external_issue_sync_status (
                project_id, provider, state, last_attempt_at, last_success_at,
                last_outbound_success_at, linked_count, pending_count,
                consecutive_failures, retry_at, last_statistics, last_error, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT(project_id, provider) DO UPDATE SET
                state = excluded.state,
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                last_outbound_success_at = excluded.last_outbound_success_at,
                linked_count = excluded.linked_count,
                pending_count = excluded.pending_count,
                consecutive_failures = excluded.consecutive_failures,
                retry_at = excluded.retry_at,
                last_statistics = excluded.last_statistics,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            RETURNING *
            """,
            (
                project_id,
                provider,
                state,
                last_attempt_at,
                last_success_at,
                last_outbound_success_at,
                linked_count,
                pending_count,
                consecutive_failures,
                retry_at,
                json.dumps(dict(last_statistics or {}), sort_keys=True),
                last_error,
                now,
            ),
        )
        if row is None:
            raise RuntimeError("External issue sync status upsert returned no row")
        return ExternalIssueSyncStatus.from_row(row)

    def counts(self, project_id: str, provider: ExternalIssueProvider) -> tuple[int, int]:
        if provider == "linear":
            row = self.db.fetchone(
                "SELECT "
                "COUNT(*) FILTER (WHERE linear_issue_id IS NOT NULL) AS linked, "
                "COUNT(*) FILTER (WHERE closed_at IS NULL AND linear_issue_id IS NULL) AS pending "
                "FROM tasks WHERE project_id = %s",
                (project_id,),
            )
        else:
            row = self.db.fetchone(
                "SELECT "
                "COUNT(*) FILTER (WHERE github_repo IS NOT NULL "
                "AND github_issue_number IS NOT NULL) AS linked, "
                "(SELECT COUNT(*) FROM gh_triage_deliveries "
                "WHERE project_id = %s AND status IN ('pending', 'processing')) AS pending "
                "FROM tasks WHERE project_id = %s",
                (project_id, project_id),
            )
        if not row:
            return (0, 0)
        return (int(row["linked"] or 0), int(row["pending"] or 0))
