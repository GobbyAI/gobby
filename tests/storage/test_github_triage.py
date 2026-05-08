from __future__ import annotations

import json

import pytest

from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_config_uses_legacy_github_repo_fallback(temp_db, sample_project) -> None:
    store = GitHubTriageStore(temp_db)

    config = store.get_config(sample_project["id"], fallback_repo="owner/repo")

    assert config.enabled is False
    assert config.repositories == ("owner/repo",)


def test_upsert_config_persists_repositories_and_secret_ref(temp_db, sample_project) -> None:
    store = GitHubTriageStore(temp_db)

    saved = store.upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            enabled=True,
            webhook_enabled=True,
            repositories=("owner/repo", "owner/other"),
            reconcile_interval_seconds=900,
            webhook_secret_ref="$secret:github_triage_webhook",
        )
    )

    assert saved.enabled is True
    assert saved.webhook_enabled is True
    assert saved.repositories == ("owner/repo", "owner/other")
    assert saved.reconcile_interval_seconds == 900
    assert saved.webhook_secret_ref == "$secret:github_triage_webhook"


def test_record_delivery_is_idempotent_by_project_and_delivery(temp_db, sample_project) -> None:
    store = GitHubTriageStore(temp_db)
    raw_body = b'{"action":"opened"}'

    first, inserted_first = store.record_delivery(
        project_id=sample_project["id"],
        delivery_id="delivery-1",
        event="issues",
        action="opened",
        repository="owner/repo",
        issue_number=42,
        headers={"x-github-event": "issues"},
        raw_body=raw_body,
    )
    second, inserted_second = store.record_delivery(
        project_id=sample_project["id"],
        delivery_id="delivery-1",
        event="issues",
        action="opened",
        repository="owner/repo",
        issue_number=42,
        headers={"x-github-event": "issues"},
        raw_body=raw_body,
    )

    assert inserted_first is True
    assert inserted_second is False
    assert first.id == second.id
    assert first.payload_hash == second.payload_hash


def test_claim_delivery_for_processing_is_single_winner(temp_db, sample_project) -> None:
    store = GitHubTriageStore(temp_db)
    store.record_delivery(
        project_id=sample_project["id"],
        delivery_id="delivery-claim",
        event="issues",
        action="opened",
        repository="owner/repo",
        issue_number=42,
        headers={"x-github-event": "issues"},
        raw_body=b'{"action":"opened"}',
    )

    claimed = store.claim_delivery_for_processing(sample_project["id"], "delivery-claim")
    second_claim = store.claim_delivery_for_processing(sample_project["id"], "delivery-claim")

    assert claimed is not None
    assert claimed.status == "processing"
    assert second_claim is None


def test_issue_record_upsert_preserves_task_link_and_latest_decision(
    temp_db,
    sample_project,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Implement issue",
    )
    store = GitHubTriageStore(temp_db)

    first = store.upsert_issue_record(
        project_id=sample_project["id"],
        repo="owner/repo",
        issue_number=7,
        issue_url="https://github.com/owner/repo/issues/7",
        issue_state="open",
        labels=["bug"],
        issue_updated_at="2026-05-03T00:00:00Z",
        content_hash="hash-1",
        verdict="implement",
        decision={"verdict": "implement"},
        task_id=task.id,
        vector_point_id="point-1",
        dedup_issue_key=None,
        source="webhook",
    )
    second = store.upsert_issue_record(
        project_id=sample_project["id"],
        repo="owner/repo",
        issue_number=7,
        issue_url="https://github.com/owner/repo/issues/7",
        issue_state="open",
        labels=["bug", "triaged"],
        issue_updated_at="2026-05-03T00:01:00Z",
        content_hash="hash-2",
        verdict="escalate",
        decision={"verdict": "escalate", "reason": "needs owner"},
        task_id=task.id,
        vector_point_id="point-2",
        dedup_issue_key=None,
        source="reconcile",
    )

    assert second.id == first.id
    assert second.content_hash == "hash-2"
    assert second.labels == ("bug", "triaged")
    assert second.task_id == task.id
    assert second.vector_point_id == "point-2"
    assert json.loads(second.decision_json)["reason"] == "needs owner"
