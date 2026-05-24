"""Storage for GitHub issue triage automation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase

DeliveryStatus = Literal["pending", "processing", "processed", "ignored", "duplicate", "error"]
TriageVerdict = Literal["implement", "skip", "escalate", "dedup"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class GitHubTriageConfig:
    """Per-project GitHub issue triage configuration."""

    project_id: str
    enabled: bool = False
    webhook_enabled: bool = False
    repositories: tuple[str, ...] = ()
    reconcile_interval_seconds: int = 3600
    webhook_secret_ref: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GitHubTriageConfig:
        repositories = json.loads(row["repositories_json"] or "[]")
        return cls(
            project_id=row["project_id"],
            enabled=bool(row["enabled"]),
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
            "enabled": self.enabled,
            "webhook_enabled": self.webhook_enabled,
            "repositories": list(self.repositories),
            "reconcile_interval_seconds": self.reconcile_interval_seconds,
            "webhook_secret_ref": self.webhook_secret_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


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
    received_at: str
    processed_at: str | None
    updated_at: str

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
            received_at=row["received_at"],
            processed_at=row["processed_at"],
            updated_at=row["updated_at"],
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
    issue_updated_at: str | None
    content_hash: str
    verdict: TriageVerdict
    decision_json: str
    task_id: str | None
    vector_point_id: str | None
    dedup_issue_key: str | None
    source: str
    last_triaged_at: str
    created_at: str
    updated_at: str

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
            last_triaged_at=row["last_triaged_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class GitHubTriageStore:
    """CRUD wrapper for GitHub triage audit/config tables."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get_config(self, project_id: str, fallback_repo: str | None = None) -> GitHubTriageConfig:
        row = self.db.fetchone(
            "SELECT * FROM project_github_triage_configs WHERE project_id = ?",
            (project_id,),
        )
        if row:
            config = GitHubTriageConfig.from_row(row)
            if not config.repositories and fallback_repo:
                return GitHubTriageConfig(
                    project_id=config.project_id,
                    enabled=config.enabled,
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
        now = _now()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT created_at FROM project_github_triage_configs WHERE project_id = ?",
                (config.project_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO project_github_triage_configs (
                    project_id, enabled, webhook_enabled, repositories_json,
                    reconcile_interval_seconds, webhook_secret_ref, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    webhook_enabled = excluded.webhook_enabled,
                    repositories_json = excluded.repositories_json,
                    reconcile_interval_seconds = excluded.reconcile_interval_seconds,
                    webhook_secret_ref = excluded.webhook_secret_ref,
                    updated_at = excluded.updated_at
                """,
                (
                    config.project_id,
                    bool(config.enabled),
                    bool(config.webhook_enabled),
                    _json_dumps(list(config.repositories)),
                    config.reconcile_interval_seconds,
                    config.webhook_secret_ref,
                    created_at,
                    now,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        except (psycopg.IntegrityError, UniqueViolation):
            inserted = False

        delivery = self.get_delivery(project_id, delivery_id)
        if delivery is None:
            raise RuntimeError(f"Failed to load GitHub triage delivery {delivery_id}")
        return delivery, inserted

    def get_delivery(self, project_id: str, delivery_id: str) -> GitHubTriageDelivery | None:
        row = self.db.fetchone(
            "SELECT * FROM gh_triage_deliveries WHERE project_id = ? AND delivery_id = ?",
            (project_id, delivery_id),
        )
        return GitHubTriageDelivery.from_row(row) if row else None

    def claim_delivery_for_processing(
        self,
        project_id: str,
        delivery_id: str,
    ) -> GitHubTriageDelivery | None:
        """Atomically claim a pending delivery for one processor."""
        now = _now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE gh_triage_deliveries
                   SET status = 'processing',
                       error = NULL,
                       updated_at = ?
                 WHERE project_id = ?
                   AND delivery_id = ?
                   AND status = 'pending'
                """,
                (now, project_id, delivery_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM gh_triage_deliveries WHERE project_id = ? AND delivery_id = ?",
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
    ) -> GitHubTriageDelivery:
        now = _now()
        processed_at = now if processed else None
        self.db.execute(
            """
            UPDATE gh_triage_deliveries
            SET status = ?, error = ?, processed_at = COALESCE(?, processed_at),
                updated_at = ?
            WHERE project_id = ? AND delivery_id = ?
            """,
            (status, error, processed_at, now, project_id, delivery_id),
        )
        delivery = self.get_delivery(project_id, delivery_id)
        if delivery is None:
            raise RuntimeError(f"Failed to update GitHub triage delivery {delivery_id}")
        return delivery

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
    ) -> GitHubIssueTriageRecord:
        row_id = hashlib.sha256(f"{project_id}:{repo}:{issue_number}".encode()).hexdigest()
        now = _now()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT created_at FROM gh_issues_triaged WHERE project_id = ? AND repo = ? "
                "AND issue_number = ?",
                (project_id, repo, issue_number),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO gh_issues_triaged (
                    id, project_id, repo, issue_number, issue_url, issue_state,
                    labels_json, issue_updated_at, content_hash, verdict, decision_json,
                    task_id, vector_point_id, dedup_issue_key, source, last_triaged_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                    created_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM gh_issues_triaged WHERE project_id = ? AND repo = ? "
                "AND issue_number = ?",
                (project_id, repo, issue_number),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to upsert GitHub issue triage row {repo}#{issue_number}")
        return GitHubIssueTriageRecord.from_row(row)

    def get_issue_record(
        self, project_id: str, repo: str, issue_number: int
    ) -> GitHubIssueTriageRecord | None:
        row = self.db.fetchone(
            "SELECT * FROM gh_issues_triaged WHERE project_id = ? AND repo = ? "
            "AND issue_number = ?",
            (project_id, repo, issue_number),
        )
        return GitHubIssueTriageRecord.from_row(row) if row else None
