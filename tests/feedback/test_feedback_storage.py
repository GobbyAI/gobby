"""FeedbackReviewStore round-trips against the isolated PostgreSQL hub."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.feedback.storage import FeedbackReviewStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000003"

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.fixture
def session_id(temp_db: HubDatabase) -> str:
    project = LocalProjectManager(temp_db).create(name="feedback-test", repo_path="/tmp/test")
    SessionManager(temp_db).register_session(
        external_id="feedback-session",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    row = temp_db.fetchone("SELECT id FROM sessions WHERE external_id = %s", ("feedback-session",))
    assert row is not None
    return str(row["id"])


def _insert_feedback(
    db: HubDatabase,
    session_id: str,
    *,
    kind: str = "friction",
    kind_other_label: str | None = None,
    created_at: datetime = _T0,
    reviewed: bool = False,
) -> str:
    feedback_id = str(uuid4())
    db.execute(
        """
        INSERT INTO session_feedback (
            id, session_id, source, kind, kind_other_label, evidence, impact,
            frequency, suggestion, disposition, reviewed, created_at
        )
        VALUES (%s, %s, 'survey', %s, %s, 'evidence text', 'impact text',
                'once', NULL, NULL, %s, %s)
        """,
        (feedback_id, session_id, kind, kind_other_label, reviewed, created_at),
    )
    return feedback_id


def test_list_unreviewed_returns_oldest_first_within_limit(
    temp_db: HubDatabase, session_id: str
) -> None:
    store = FeedbackReviewStore(temp_db)
    newest = _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(hours=2))
    oldest = _insert_feedback(temp_db, session_id, created_at=_T0)
    _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(hours=1), reviewed=True)

    rows = store.list_unreviewed(limit=10)

    assert [row.id for row in rows] == [oldest, newest]
    assert rows[0].kind == "friction"
    assert rows[0].kind_other_label is None
    assert rows[0].evidence == "evidence text"
    assert store.list_unreviewed(limit=1)[0].id == oldest


def test_run_lifecycle_round_trips_findings_actions_and_digest(
    temp_db: HubDatabase,
) -> None:
    store = FeedbackReviewStore(temp_db)
    run_id = store.create_run(
        dry_run=False,
        window_start=_T0,
        window_end=_T0 + timedelta(hours=2),
        rows_considered=3,
    )

    created = store.get_run(run_id)
    assert created is not None
    assert created.status == "running"
    assert created.dry_run is False
    assert created.rows_considered == 3
    assert created.findings is None
    assert created.completed_at is None

    findings = {"clusters": [{"theme": "close gates", "classification": "defect"}]}
    actions = {"filed": [{"task_id": "t-1", "title": "Fix close gates"}], "deduplicated": 1}
    store.finalize_run(
        run_id,
        status="completed",
        findings=findings,
        actions=actions,
        digest_md="# Digest",
    )

    finalized = store.get_run(run_id)
    assert finalized is not None
    assert finalized.status == "completed"
    assert finalized.findings == findings
    assert finalized.actions == actions
    assert finalized.digest_md == "# Digest"
    assert finalized.error is None
    assert finalized.completed_at is not None


def test_finalize_run_failed_records_error(temp_db: HubDatabase) -> None:
    store = FeedbackReviewStore(temp_db)
    run_id = store.create_run(dry_run=False, window_start=None, window_end=None, rows_considered=0)

    store.finalize_run(run_id, status="failed", error="distill timed out")

    failed = store.get_run(run_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "distill timed out"


def test_mark_reviewed_flips_only_unreviewed_rows_and_links_run(
    temp_db: HubDatabase, session_id: str
) -> None:
    store = FeedbackReviewStore(temp_db)
    first = _insert_feedback(temp_db, session_id)
    second = _insert_feedback(temp_db, session_id, created_at=_T0 + timedelta(minutes=1))
    run_id = store.create_run(dry_run=False, window_start=_T0, window_end=_T0, rows_considered=2)

    assert store.mark_reviewed([first, second], run_id) == 2
    # Already-reviewed rows are not re-counted or re-linked.
    assert store.mark_reviewed([first, second], run_id) == 0
    assert store.mark_reviewed([], run_id) == 0

    rows = temp_db.fetchall(
        "SELECT reviewed, review_run_id FROM session_feedback WHERE id = ANY(%s)",
        ([first, second],),
    )
    assert all(row["reviewed"] for row in rows)
    assert {str(row["review_run_id"]) for row in rows} == {run_id}
    assert store.list_unreviewed(limit=10) == []


def test_latest_run_returns_newest(temp_db: HubDatabase) -> None:
    store = FeedbackReviewStore(temp_db)
    store.create_run(dry_run=False, window_start=None, window_end=None, rows_considered=0)
    newest_id = store.create_run(
        dry_run=True, window_start=None, window_end=None, rows_considered=1
    )

    latest = store.latest_run()
    assert latest is not None
    assert latest.id == newest_id
    assert latest.dry_run is True
