"""Collector tests: hub evidence, real git checkouts and injected load samples."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from gobby.status_report.collect import collect_status
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)
WINDOW = timedelta(minutes=30)
BRANCH = "0.5.0"


def _iso(value: datetime) -> str:
    return value.isoformat()


def _session(db: HubDatabase, project_id: str) -> str:
    return str(AgentSessions(db, project_id).add("dev", ["*"]))


def _claim(db: HubDatabase, task_id: str, session_id: str, claimed_at: datetime) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_claim_intervals (task_id, session_id, claimed_at) "
            "VALUES (%s, %s, %s)",
            (task_id, session_id, claimed_at),
        )


def _attest(
    db: HubDatabase,
    task_id: str,
    *,
    kind: str,
    result: str,
    at: datetime,
    sequence: int,
) -> None:
    record_id = str(uuid4())
    body = {
        "id": record_id,
        "kind": kind,
        "result": result,
        "summary": "Observed.",
        "sequence": sequence,
        "supersedes_id": None,
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body, created_at, "
            "updated_at) VALUES (%s, %s, 'dev', 'task_attestation', %s, %s, %s)",
            (record_id, task_id, json.dumps(body), at, at),
        )


def _landing_receipt(db: HubDatabase, task_id: str, sha: str, at: datetime) -> None:
    body = {"kind": "landing", "commit_sha": sha, "facts": {}}
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body, created_at, "
            "updated_at) VALUES (%s, %s, 'dev', 'close_receipt', %s, %s, %s)",
            (str(uuid4()), task_id, json.dumps(body), at, at),
        )


def _set_task(db: HubDatabase, task_id: str, column_sql: str, *params: Any) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE tasks SET {column_sql} WHERE id = %s", (*params, task_id))


def _git(cwd: Path, *args: str, when: datetime | None = None) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="status",
        GIT_AUTHOR_EMAIL="status@example.invalid",
        GIT_COMMITTER_NAME="status",
        GIT_COMMITTER_EMAIL="status@example.invalid",
    )
    if when is not None:
        env.update(GIT_AUTHOR_DATE=_iso(when), GIT_COMMITTER_DATE=_iso(when))
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", BRANCH)
    (path / "base.txt").write_text("base\n")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-q", "-m", "base", when=NOW - timedelta(hours=1))
    return path


def _collect(
    db: HubDatabase,
    project_id: str,
    project_path: Path,
    *,
    runs: tuple[Any, ...] = (),
    worktrees: tuple[Any, ...] = (),
    loadavg: tuple[float, float, float] | None = (0.0, 0.0, 0.0),
) -> Any:
    def sample() -> tuple[float, float, float]:
        if loadavg is None:
            raise OSError("loadavg unavailable")
        return loadavg

    return collect_status(
        db,
        project_id=project_id,
        project_path=project_path,
        integration_branch=BRANCH,
        now=NOW,
        window=WINDOW,
        load_threshold=60.0,
        runs=runs,
        worktrees=worktrees,
        loadavg=sample,
    )


def test_claimed_work_reports_latest_attestation_and_claim_time(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Build the report")
    task_id = str(task.id)
    _claim(temp_db, task_id, _session(temp_db, project_id), NOW - timedelta(hours=2, minutes=3))
    _attest(
        temp_db,
        task_id,
        kind="implement",
        result="approved",
        at=NOW - timedelta(minutes=30),
        sequence=1,
    )
    _attest(
        temp_db,
        task_id,
        kind="test-after",
        result="approved",
        at=NOW - timedelta(minutes=4),
        sequence=2,
    )

    data = _collect(temp_db, project_id, tmp_path)

    (row,) = data.in_flight
    assert row.seq == task.seq_num
    assert row.title == "Build the report"
    assert row.stage == "test-after/approved"
    assert row.evidence_at == NOW - timedelta(minutes=4)
    assert row.claimed_at == NOW - timedelta(hours=2, minutes=3)


def test_claimed_task_without_attestation_is_in_flight_without_stage(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Quiet work")
    _claim(temp_db, str(task.id), _session(temp_db, project_id), NOW - timedelta(minutes=9))

    data = _collect(temp_db, project_id, tmp_path)

    (row,) = data.in_flight
    assert row.stage is None
    assert row.evidence_at is None
    assert row.claimed_at == NOW - timedelta(minutes=9)


def test_closed_task_reports_claim_to_close_from_first_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Finished work")
    task_id = str(task.id)
    session_id = _session(temp_db, project_id)
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_claim_intervals (task_id, session_id, claimed_at, released_at) "
            "VALUES (%s, %s, %s, %s)",
            (
                task_id,
                session_id,
                NOW - timedelta(hours=2, minutes=3),
                NOW - timedelta(minutes=5),
            ),
        )
    _set_task(
        temp_db,
        task_id,
        "closed_at = %s, closed_reason = %s, state_bucket = 'closed'",
        NOW - timedelta(minutes=5),
        "completed",
    )

    data = _collect(temp_db, project_id, tmp_path)

    (closed,) = data.closed
    assert closed.seq == task.seq_num
    assert closed.claim_to_close == timedelta(hours=1, minutes=58)
    assert data.in_flight == ()


def test_task_closed_before_window_is_not_reported(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Old work")
    _set_task(
        temp_db,
        str(task.id),
        "closed_at = %s, closed_reason = %s, state_bucket = 'closed'",
        NOW - timedelta(hours=3),
        "completed",
    )

    data = _collect(temp_db, project_id, tmp_path)

    assert data.closed == ()


def test_escalations_and_decision_labels_surface_as_attention(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    escalated = LocalTaskManager(temp_db).create_task(project_id, "Escalated work")
    decided = LocalTaskManager(temp_db).create_task(project_id, "Needs a call")
    _set_task(
        temp_db,
        str(escalated.id),
        "is_escalated = true, escalated_at = %s, escalation_reason = %s",
        NOW - timedelta(minutes=6),
        "reviewer disagreed",
    )
    _set_task(temp_db, str(decided.id), "labels = %s::jsonb", json.dumps(["needs-decision"]))

    data = _collect(temp_db, project_id, tmp_path)

    assert [(item.seq, item.state) for item in data.attention] == [(escalated.seq_num, "escalated")]
    assert [item.seq for item in data.decisions] == [decided.seq_num]
    (event,) = data.escalated
    assert event.seq == escalated.seq_num
    assert event.reason == "reviewer disagreed"


def test_landing_receipts_are_verified_against_integration_branch(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side\n")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-q", "-m", "side", when=NOW - timedelta(minutes=8))
    side_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", BRANCH)
    (repo / "main.txt").write_text("landed\n")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-q", "-m", "land", when=NOW - timedelta(minutes=10))
    landed_sha = _git(repo, "rev-parse", "HEAD")
    landed_task = LocalTaskManager(temp_db).create_task(project_id, "Landed work")
    off_task = LocalTaskManager(temp_db).create_task(project_id, "Unlanded work")
    _landing_receipt(temp_db, str(landed_task.id), landed_sha, NOW - timedelta(minutes=10))
    _landing_receipt(temp_db, str(off_task.id), side_sha, NOW - timedelta(minutes=8))

    data = _collect(temp_db, project_id, repo)

    verdicts = {item.seq: item.verified for item in data.landings}
    assert verdicts == {landed_task.seq_num: True, off_task.seq_num: False}
    assert data.gaps == ()


def test_landing_check_without_repository_is_a_gap_not_a_verdict(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Landed work")
    _landing_receipt(temp_db, str(task.id), "a" * 40, NOW - timedelta(minutes=10))
    missing = tmp_path / "not-a-checkout"
    missing.mkdir()

    data = _collect(temp_db, project_id, missing)

    (landing,) = data.landings
    assert landing.verified is None
    assert any(gap.startswith("landing") for gap in data.gaps)


def test_load_sample_above_threshold_is_carried_into_the_report(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    data = _collect(temp_db, str(sample_project["id"]), tmp_path, loadavg=(9.0, 72.44, 3.0))

    assert data.load_5m == 72.44
    assert data.load_threshold == 60.0
    assert data.gaps == ()


def test_unreadable_load_is_a_gap(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    data = _collect(temp_db, str(sample_project["id"]), tmp_path, loadavg=None)

    assert data.load_5m is None
    assert any(gap.startswith("load") for gap in data.gaps)


def test_stalled_runs_map_to_their_seat_and_task(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Stalled work")
    stalled = SimpleNamespace(
        id="run-1",
        child_session_id="fc94376c-1617-4c8a-ab0b-283937f5d8a8",
        task_id=str(task.id),
        worktree_id=None,
        stall_suspected=True,
        status="running",
    )
    healthy = SimpleNamespace(
        id="run-2",
        child_session_id="069b55f3-906b-4f8c-a75d-a195e273547d",
        task_id=None,
        worktree_id=None,
        stall_suspected=False,
        status="running",
    )

    data = _collect(temp_db, project_id, tmp_path, runs=(stalled, healthy))

    (seat,) = data.stalled
    assert seat.session_ref == "fc94376c"
    assert seat.task_seq == task.seq_num
    assert seat.task_title == "Stalled work"


def test_worktree_rows_report_drift_dirty_files_and_last_commit(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _repo(tmp_path / "wt")
    _git(repo, "checkout", "-q", "-b", "task-7")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feature", when=NOW - timedelta(minutes=12))
    _git(repo, "checkout", "-q", BRANCH)
    (repo / "upstream.txt").write_text("upstream\n")
    _git(repo, "add", "upstream.txt")
    _git(repo, "commit", "-q", "-m", "upstream", when=NOW - timedelta(minutes=20))
    _git(repo, "checkout", "-q", "task-7")
    (repo / "scratch.txt").write_text("untracked\n")
    LocalWorktreeManager(temp_db).create(project_id, "task-7", str(repo), base_branch=BRANCH)
    worktrees = LocalWorktreeManager(temp_db).list_worktrees(project_id=project_id)

    data = _collect(temp_db, project_id, tmp_path, worktrees=tuple(worktrees))

    (row,) = data.worktrees
    assert row.branch == "task-7"
    assert (row.ahead, row.behind) == (1, 1)
    assert row.dirty == 1
    assert row.last_commit_at == NOW - timedelta(minutes=12)
    assert row.git_error is None


def test_unreadable_landing_receipt_is_a_gap_and_the_report_still_builds(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Landed work")
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body, created_at, "
            "updated_at) VALUES (%s, %s, 'dev', 'close_receipt', 'not json', %s, %s)",
            (str(uuid4()), str(task.id), NOW - timedelta(minutes=10), NOW - timedelta(minutes=10)),
        )

    data = _collect(temp_db, project_id, tmp_path)

    assert data.landings == ()
    assert any(f"receipt for #{task.seq_num}" in gap for gap in data.gaps)


def test_worktree_that_is_not_a_checkout_is_a_gap_with_unknown_git_cells(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    gone = tmp_path / "gone"
    gone.mkdir()
    LocalWorktreeManager(temp_db).create(project_id, "task-8", str(gone), base_branch=BRANCH)
    worktrees = LocalWorktreeManager(temp_db).list_worktrees(project_id=project_id)

    data = _collect(temp_db, project_id, tmp_path, worktrees=tuple(worktrees))

    (row,) = data.worktrees
    assert row.git_error is not None
    assert row.ahead is None and row.dirty is None
    assert any(gap.startswith("worktrees") for gap in data.gaps)


def test_landing_receipt_with_a_non_hex_commit_name_is_a_gap_and_never_reaches_git(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    repo = _repo(tmp_path / "repo")
    task = LocalTaskManager(temp_db).create_task(project_id, "Landed work")
    _landing_receipt(temp_db, str(task.id), "--all", NOW - timedelta(minutes=10))

    data = _collect(temp_db, project_id, repo)

    assert data.landings == ()
    assert any(f"receipt for #{task.seq_num}" in gap for gap in data.gaps)


def test_attestation_without_sequence_does_not_shadow_a_sequenced_one(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(project_id, "Build the report")
    task_id = str(task.id)
    _claim(temp_db, task_id, _session(temp_db, project_id), NOW - timedelta(hours=1))
    _attest(
        temp_db,
        task_id,
        kind="test-after",
        result="approved",
        at=NOW - timedelta(minutes=4),
        sequence=2,
    )
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO task_comments (id, task_id, author, author_type, body, created_at, "
            "updated_at) VALUES (%s, %s, 'dev', 'task_attestation', %s, %s, %s)",
            (
                str(uuid4()),
                task_id,
                json.dumps({"kind": "implement", "result": "approved", "supersedes_id": None}),
                NOW - timedelta(minutes=1),
                NOW - timedelta(minutes=1),
            ),
        )

    data = _collect(temp_db, project_id, tmp_path)

    (row,) = data.in_flight
    assert row.stage == "test-after/approved"


def test_run_listing_at_the_cap_is_a_gap_so_stalled_seats_are_not_hidden(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    project_id = str(sample_project["id"])
    runs = tuple(
        SimpleNamespace(
            id=f"run-{number}",
            child_session_id="069b55f3-906b-4f8c-a75d-a195e273547d",
            task_id=None,
            worktree_id=None,
            stall_suspected=False,
            status="running",
        )
        for number in range(500)
    )

    data = _collect(temp_db, project_id, tmp_path, runs=runs)

    assert any(gap.startswith("live runs") and "500" in gap for gap in data.gaps)
