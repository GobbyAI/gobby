"""Storage for GitHub issue triage automation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg.errors import UniqueViolation

from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model, utc_now

DeliveryStatus = Literal["pending", "processing", "processed", "ignored", "duplicate", "error"]
TriageVerdict = Literal["implement", "skip", "escalate", "dedup"]


def _now() -> datetime:
    return utc_now()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@normalize_datetime_model(
    optional=(
        "created_at",
        "updated_at",
    )
)
@dataclass(frozen=True)
class GitHubTriageConfig:
    """Per-project GitHub issue triage configuration."""

    project_id: str
    sync_enabled: bool = False
    triage_enabled: bool = False
    webhook_enabled: bool = False
    repositories: tuple[str, ...] = ()
    reconcile_interval_seconds: int = 3600
    webhook_secret_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GitHubTriageConfig:
        repositories = json.loads(row["repositories_json"] or "[]")
        return cls(
            project_id=row["project_id"],
            sync_enabled=bool(row["sync_enabled"]),
            triage_enabled=bool(row["triage_enabled"]),
            webhook_enabled=bool(row["webhook_enabled"]),
            repositories=tuple(str(repo) for repo in repositories),
            reconcile_interval_seconds=int(row["reconcile_interval_seconds"]),
            webhook_secret_ref=row["webhook_secret_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @classmethod
    def default(cls, project_id: str, fallback_repo: str | None = None) -> GitHubTriageConfig:
        repositories = (fallback_repo,) if fallback_repo else ()
        return cls(project_id=project_id, repositories=repositories)

    def repositories_with_fallback(self, fallback_repo: str | None = None) -> tuple[str, ...]:
        if self.repositories:
            return self.repositories
        return (fallback_repo,) if fallback_repo else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "sync_enabled": self.sync_enabled,
            "triage_enabled": self.triage_enabled,
            "webhook_enabled": self.webhook_enabled,
            "repositories": list(self.repositories),
            "reconcile_interval_seconds": self.reconcile_interval_seconds,
            "webhook_secret_configured": self.webhook_secret_ref is not None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@normalize_datetime_model(
    required=(
        "received_at",
        "updated_at",
    ),
    optional=("processed_at",),
)
@dataclass(frozen=True)
class GitHubTriageDelivery:
    """Persisted GitHub webhook delivery."""

    id: str
    project_id: str
    delivery_id: str
    event: str
    action: str | None
    repository: str | None
    issue_number: int | None
    status: DeliveryStatus
    payload_hash: str
    headers_json: str
    raw_body: str
    error: str | None
    attempt_count: int
    next_attempt_at: datetime | None
    received_at: datetime
    processed_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GitHubTriageDelivery:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            delivery_id=row["delivery_id"],
            event=row["event"],
            action=row["action"],
            repository=row["repository"],
            issue_number=row["issue_number"],
            status=row["status"],
            payload_hash=row["payload_hash"],
            headers_json=row["headers_json"],
            raw_body=row["raw_body"],
            error=row["error"],
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=row["next_attempt_at"],
            received_at=row["received_at"],
            processed_at=row["processed_at"],
            updated_at=row["updated_at"],
        )


@normalize_datetime_model(
    required=(
        "last_triaged_at",
        "created_at",
        "updated_at",
    ),
    optional=("issue_updated_at",),
)
@dataclass(frozen=True)
class GitHubIssueTriageRecord:
    """Audit row for the latest triage of one GitHub issue."""

    id: str
    project_id: str
    repo: str
    issue_number: int
    issue_url: str | None
    issue_state: str | None
    labels: tuple[str, ...]
    issue_updated_at: datetime | None
    content_hash: str
    verdict: TriageVerdict
    decision_json: str
    task_id: str | None
    vector_point_id: str | None
    dedup_issue_key: str | None
    source: str
    source_text: str | None
    last_triaged_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GitHubIssueTriageRecord:
        labels = json.loads(row["labels_json"] or "[]")
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            repo=row["repo"],
            issue_number=row["issue_number"],
            issue_url=row["issue_url"],
            issue_state=row["issue_state"],
            labels=tuple(str(label) for label in labels),
            issue_updated_at=row["issue_updated_at"],
            content_hash=row["content_hash"],
            verdict=row["verdict"],
            decision_json=row["decision_json"],
            task_id=row["task_id"],
            vector_point_id=row["vector_point_id"],
            dedup_issue_key=row["dedup_issue_key"],
            source=row["source"],
            source_text=row.get("source_text"),
            last_triaged_at=row["last_triaged_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class GitHubTriageStore:
    """CRUD wrapper for GitHub triage audit/config tables."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self.embedding_generation_state = EmbeddingGenerationState(db)

    def get_config(self, project_id: str, fallback_repo: str | None = None) -> GitHubTriageConfig:
        row = self.db.fetchone(
            "SELECT * FROM project_github_triage_configs WHERE project_id = %s",
            (project_id,),
        )
        if row:
            config = GitHubTriageConfig.from_row(row)
            if not config.repositories and fallback_repo:
                return GitHubTriageConfig(
                    project_id=config.project_id,
                    sync_enabled=config.sync_enabled,
                    triage_enabled=config.triage_enabled,
                    webhook_enabled=config.webhook_enabled,
                    repositories=(fallback_repo,),
                    reconcile_interval_seconds=config.reconcile_interval_seconds,
                    webhook_secret_ref=config.webhook_secret_ref,
                    created_at=config.created_at,
                    updated_at=config.updated_at,
                )
            return config
        return GitHubTriageConfig.default(project_id, fallback_repo)

    def upsert_config(self, config: GitHubTriageConfig) -> GitHubTriageConfig:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO project_github_triage_configs (
                    project_id, sync_enabled, triage_enabled, webhook_enabled, repositories_json,
                    reconcile_interval_seconds, webhook_secret_ref
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(project_id) DO UPDATE SET
                    sync_enabled = excluded.sync_enabled,
                    triage_enabled = excluded.triage_enabled,
                    webhook_enabled = excluded.webhook_enabled,
                    repositories_json = excluded.repositories_json,
                    reconcile_interval_seconds = excluded.reconcile_interval_seconds,
                    webhook_secret_ref = excluded.webhook_secret_ref,
                    updated_at = excluded.updated_at
                """,
                (
                    config.project_id,
                    bool(config.sync_enabled),
                    bool(config.triage_enabled),
                    bool(config.webhook_enabled),
                    _json_dumps(list(config.repositories)),
                    config.reconcile_interval_seconds,
                    config.webhook_secret_ref,
                ),
            )
        return self.get_config(config.project_id)

    def record_delivery(
        self,
        *,
        project_id: str,
        delivery_id: str,
        event: str,
        action: str | None,
        repository: str | None,
        issue_number: int | None,
        headers: dict[str, str],
        raw_body: bytes,
        status: DeliveryStatus = "pending",
    ) -> tuple[GitHubTriageDelivery, bool]:
        payload_hash = hashlib.sha256(raw_body).hexdigest()
        row_id = hashlib.sha256(f"{project_id}:{delivery_id}".encode()).hexdigest()
        now = _now()
        try:
            self.db.execute(
                """
                INSERT INTO gh_triage_deliveries (
                    id, project_id, delivery_id, event, action, repository,
                    issue_number, status, payload_hash, headers_json, raw_body,
                    received_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row_id,
                    project_id,
                    delivery_id,
                    event,
                    action,
                    repository,
                    issue_number,
                    status,
                    payload_hash,
                    _json_dumps(headers),
                    raw_body.decode("utf-8", errors="replace"),
                    now,
                    now,
                ),
            )
            inserted = True
        except UniqueViolation:
            inserted = False

        delivery = self.get_delivery(project_id, delivery_id)
        if delivery is None:
            raise RuntimeError(f"Failed to load GitHub triage delivery {delivery_id}")
        return delivery, inserted

    def get_delivery(self, project_id: str, delivery_id: str) -> GitHubTriageDelivery | None:
        row = self.db.fetchone(
            "SELECT * FROM gh_triage_deliveries WHERE project_id = %s AND delivery_id = %s",
            (project_id, delivery_id),
        )
        return GitHubTriageDelivery.from_row(row) if row else None

    def claim_delivery_for_processing(
        self,
        project_id: str,
        delivery_id: str,
        *,
        lease_timeout_seconds: int = 900,
        max_attempts: int = 3,
    ) -> GitHubTriageDelivery | None:
        """Claim due pending work or recover a stale processing lease."""
        now = _now()
        stale_before = now - timedelta(seconds=lease_timeout_seconds)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE gh_triage_deliveries
                   SET status = 'processing',
                       attempt_count = attempt_count + 1,
                       next_attempt_at = NULL,
                       error = NULL,
                       updated_at = %s
                 WHERE project_id = %s
                   AND delivery_id = %s
                   AND attempt_count < %s
                   AND (
                       (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= %s))
                       OR (status = 'processing' AND updated_at <= %s)
                   )
                """,
                (now, project_id, delivery_id, max_attempts, now, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM gh_triage_deliveries WHERE project_id = %s AND delivery_id = %s",
                (project_id, delivery_id),
            ).fetchone()
        return GitHubTriageDelivery.from_row(row) if row else None

    def update_delivery_status(
        self,
        project_id: str,
        delivery_id: str,
        status: DeliveryStatus,
        *,
        error: str | None = None,
        processed: bool = False,
        retry_after_seconds: float | None = None,
    ) -> GitHubTriageDelivery:
        now = _now()
        processed_at = now if processed else None
        next_attempt_at = (
            now + timedelta(seconds=retry_after_seconds)
            if retry_after_seconds is not None
            else None
        )
        self.db.execute(
            """
            UPDATE gh_triage_deliveries
            SET status = %s, error = %s, processed_at = COALESCE(%s, processed_at),
                next_attempt_at = %s, updated_at = %s
            WHERE project_id = %s AND delivery_id = %s
            """,
            (status, error, processed_at, next_attempt_at, now, project_id, delivery_id),
        )
        delivery = self.get_delivery(project_id, delivery_id)
        if delivery is None:
            raise RuntimeError(f"Failed to update GitHub triage delivery {delivery_id}")
        return delivery

    def list_recoverable_delivery_ids(
        self,
        project_id: str,
        *,
        lease_timeout_seconds: int = 900,
        max_attempts: int = 3,
        limit: int = 100,
    ) -> list[str]:
        """Expire exhausted leases and list due pending or stale processing work."""
        now = _now()
        stale_before = now - timedelta(seconds=lease_timeout_seconds)
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE gh_triage_deliveries
                   SET status = 'error', error = 'retry attempts exhausted',
                       processed_at = %s, updated_at = %s
                 WHERE project_id = %s AND status = 'processing'
                   AND attempt_count >= %s AND updated_at <= %s
                """,
                (now, now, project_id, max_attempts, stale_before),
            )
            rows = conn.execute(
                """
                SELECT delivery_id FROM gh_triage_deliveries
                 WHERE project_id = %s AND attempt_count < %s
                   AND (
                       (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= %s))
                       OR (status = 'processing' AND updated_at <= %s)
                   )
                 ORDER BY received_at, delivery_id
                 LIMIT %s
                """,
                (project_id, max_attempts, now, stale_before, limit),
            ).fetchall()
        return [str(row["delivery_id"]) for row in rows]

    def upsert_issue_record(
        self,
        *,
        project_id: str,
        repo: str,
        issue_number: int,
        issue_url: str | None,
        issue_state: str | None,
        labels: list[str],
        issue_updated_at: str | None,
        content_hash: str,
        verdict: TriageVerdict,
        decision: dict[str, Any],
        task_id: str | None,
        vector_point_id: str | None,
        dedup_issue_key: str | None,
        source: str,
        source_text: str | None = None,
    ) -> GitHubIssueTriageRecord:
        row_id = hashlib.sha256(f"{project_id}:{repo}:{issue_number}".encode()).hexdigest()
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO gh_issues_triaged (
                    id, project_id, repo, issue_number, issue_url, issue_state,
                    labels_json, issue_updated_at, content_hash, verdict, decision_json,
                    task_id, vector_point_id, dedup_issue_key, source, source_text,
                    last_triaged_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(project_id, repo, issue_number) DO UPDATE SET
                    issue_url = excluded.issue_url,
                    issue_state = excluded.issue_state,
                    labels_json = excluded.labels_json,
                    issue_updated_at = excluded.issue_updated_at,
                    content_hash = excluded.content_hash,
                    verdict = excluded.verdict,
                    decision_json = excluded.decision_json,
                    task_id = COALESCE(excluded.task_id, gh_issues_triaged.task_id),
                    vector_point_id = COALESCE(
                        excluded.vector_point_id,
                        gh_issues_triaged.vector_point_id
                    ),
                    dedup_issue_key = excluded.dedup_issue_key,
                    source = excluded.source,
                    source_text = COALESCE(
                        excluded.source_text,
                        gh_issues_triaged.source_text
                    ),
                    last_triaged_at = excluded.last_triaged_at,
                    updated_at = excluded.updated_at
                """,
                (
                    row_id,
                    project_id,
                    repo,
                    issue_number,
                    issue_url,
                    issue_state,
                    _json_dumps(labels),
                    issue_updated_at,
                    content_hash,
                    verdict,
                    _json_dumps(decision),
                    task_id,
                    vector_point_id,
                    dedup_issue_key,
                    source,
                    source_text,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM gh_issues_triaged WHERE project_id = %s AND repo = %s "
                "AND issue_number = %s",
                (project_id, repo, issue_number),
            ).fetchone()
            self.embedding_generation_state.append_change(
                "github_issue",
                f"{project_id}:{repo}:{issue_number}",
                transaction=conn,
            )
        if row is None:
            raise RuntimeError(f"Failed to upsert GitHub issue triage row {repo}#{issue_number}")
        return GitHubIssueTriageRecord.from_row(row)

    def list_issue_records(
        self,
        *,
        project_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[GitHubIssueTriageRecord]:
        """List durable GitHub issue triage records for rebuild jobs."""
        if limit <= 0:
            return []

        if project_id is None:
            rows = self.db.fetchall(
                "SELECT * FROM gh_issues_triaged ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM gh_issues_triaged WHERE project_id = %s "
                "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (project_id, limit, offset),
            )
        return [GitHubIssueTriageRecord.from_row(row) for row in rows]

    def get_issue_record(
        self, project_id: str, repo: str, issue_number: int
    ) -> GitHubIssueTriageRecord | None:
        row = self.db.fetchone(
            "SELECT * FROM gh_issues_triaged WHERE project_id = %s AND repo = %s "
            "AND issue_number = %s",
            (project_id, repo, issue_number),
        )
        return GitHubIssueTriageRecord.from_row(row) if row else None

    def has_build_dispatch(self, project_id: str, repo: str, issue_number: int) -> bool:
        row = self.db.fetchone(
            "SELECT 1 FROM gh_triage_build_dispatches "
            "WHERE project_id = %s AND repo = %s AND issue_number = %s",
            (project_id, repo, issue_number),
        )
        return row is not None

    def record_build_dispatch(
        self, project_id: str, repo: str, issue_number: int, task_id: str
    ) -> None:
        self.db.execute(
            """
            INSERT INTO gh_triage_build_dispatches (
                project_id, repo, issue_number, task_id, dispatched_at
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(project_id, repo, issue_number) DO UPDATE SET
                task_id = excluded.task_id,
                dispatched_at = excluded.dispatched_at
            """,
            (project_id, repo, issue_number, task_id, _now()),
        )

    def rollback_issue_record(
        self,
        project_id: str,
        repo: str,
        issue_number: int,
        *,
        content_hash: str,
        previous: GitHubIssueTriageRecord | None,
    ) -> None:
        """Remove or restore an exact provisional audit row after comment failure."""
        source_id = f"{project_id}:{repo}:{issue_number}"
        with self.db.transaction() as conn:
            if previous is None:
                cursor = conn.execute(
                    "DELETE FROM gh_issues_triaged WHERE project_id = %s AND repo = %s "
                    "AND issue_number = %s AND content_hash = %s",
                    (project_id, repo, issue_number, content_hash),
                )
                if cursor.rowcount:
                    self.embedding_generation_state.append_change(
                        "github_issue", source_id, is_tombstone=True, transaction=conn
                    )
                return
            cursor = conn.execute(
                """
                UPDATE gh_issues_triaged
                   SET issue_url = %s, issue_state = %s, labels_json = %s,
                       issue_updated_at = %s, content_hash = %s, verdict = %s,
                       decision_json = %s, task_id = %s, vector_point_id = %s,
                       dedup_issue_key = %s, source = %s, source_text = %s,
                       last_triaged_at = %s, created_at = %s, updated_at = %s
                 WHERE project_id = %s AND repo = %s AND issue_number = %s
                   AND content_hash = %s
                """,
                (
                    previous.issue_url,
                    previous.issue_state,
                    _json_dumps(list(previous.labels)),
                    previous.issue_updated_at,
                    previous.content_hash,
                    previous.verdict,
                    previous.decision_json,
                    previous.task_id,
                    previous.vector_point_id,
                    previous.dedup_issue_key,
                    previous.source,
                    previous.source_text,
                    previous.last_triaged_at,
                    previous.created_at,
                    previous.updated_at,
                    project_id,
                    repo,
                    issue_number,
                    content_hash,
                ),
            )
            if cursor.rowcount:
                self.embedding_generation_state.append_change(
                    "github_issue", source_id, transaction=conn
                )
