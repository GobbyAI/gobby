"""Evidence collection for the operator status report: hub database, git, agent runs and load."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from gobby.status_report.model import (
    AttentionTask,
    ClosedTask,
    EscalatedTask,
    InFlightTask,
    Landing,
    StalledSeat,
    StatusData,
    WorktreeRow,
)
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._queries import projected_task_state_sql
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.tasks.close_receipts import CLOSE_RECEIPT_AUTHOR_TYPE, LANDING
from gobby.tasks.state_semantics import TASK_HOLD_STATES
from gobby.utils.spawn import run as spawn_run

_GIT_TIMEOUT_SECONDS = 10.0
_NEEDS_DECISION_LABEL = "needs-decision"
_OBJECT_NAME = re.compile(r"[0-9a-f]{4,64}")
_RUN_LIMIT = 500
_ATTENTION_STATES = frozenset({"blocked", "escalated"}) | TASK_HOLD_STATES

_OPEN_TASKS_SQL = f"""
SELECT t.id, t.seq_num, t.title, t.labels, t.claimed_by_session_id,
       t.escalated_at, t.escalation_reason, ({projected_task_state_sql("t")}) AS state
FROM tasks t
WHERE t.project_id = %s AND t.closed_at IS NULL
ORDER BY t.seq_num
"""

_LATEST_ATTESTATIONS_SQL = """
SELECT DISTINCT ON (c.task_id) c.task_id, c.created_at, c.body
FROM task_comments c
JOIN tasks t ON t.id = c.task_id
WHERE t.project_id = %s AND t.closed_at IS NULL AND c.author_type = 'task_attestation'
  AND NOT EXISTS (
    SELECT 1 FROM task_comments correction
    WHERE correction.task_id = c.task_id
      AND correction.author_type = 'task_attestation'
      AND correction.body::jsonb ->> 'supersedes_id' = c.body::jsonb ->> 'id'
  )
ORDER BY c.task_id, (c.body::jsonb ->> 'sequence')::bigint DESC NULLS LAST
"""

_OPEN_CLAIMS_SQL = """
SELECT ci.task_id, min(ci.claimed_at) AS claimed_at
FROM task_claim_intervals ci
JOIN tasks t ON t.id = ci.task_id
WHERE t.project_id = %s AND t.closed_at IS NULL AND ci.released_at IS NULL
GROUP BY ci.task_id
"""

_CLOSED_SQL = """
SELECT t.seq_num, t.title, t.closed_at, min(ci.claimed_at) AS first_claimed_at
FROM tasks t
LEFT JOIN task_claim_intervals ci ON ci.task_id = t.id
WHERE t.project_id = %s AND t.closed_at >= %s
GROUP BY t.id, t.seq_num, t.title, t.closed_at
ORDER BY t.closed_at
"""

_ESCALATED_SQL = """
SELECT seq_num, title, escalated_at, escalation_reason FROM tasks
WHERE project_id = %s AND escalated_at >= %s
ORDER BY escalated_at
"""

_RECEIPTS_SQL = """
SELECT t.seq_num, t.title, c.body, c.created_at
FROM task_comments c
JOIN tasks t ON t.id = c.task_id
WHERE t.project_id = %s AND c.author_type = %s AND c.created_at >= %s
ORDER BY c.created_at
"""

_TASK_REFS_SQL = f"""
SELECT t.id, t.seq_num, t.title, ({projected_task_state_sql("t")}) AS state
FROM tasks t
WHERE t.id = ANY(%s::uuid[])
"""


class _GitUnavailable(Exception):
    """A git command could not produce evidence; the message names why."""


class _TaskRef(NamedTuple):
    seq: int
    title: str
    state: str


def collect_status(
    db: HubDatabase,
    *,
    project_id: str,
    project_path: Path,
    integration_branch: str,
    now: datetime,
    window: timedelta,
    load_threshold: float,
    runs: Sequence[AgentRun],
    worktrees: Sequence[Worktree],
    loadavg: Callable[[], tuple[float, float, float]],
) -> StatusData:
    """Gather every report input. An unreadable receipt becomes a gap; a corrupt attestation store
    fails the run instead of being guessed at."""
    gaps: list[str] = []
    if len(runs) >= _RUN_LIMIT:
        gaps.append(
            f"live runs - listing reached the {_RUN_LIMIT}-run cap; runs beyond it are not "
            "checked for stalled seats"
        )
    since = now - window
    open_rows = db.fetchall(_OPEN_TASKS_SQL, (project_id,))
    latest = _latest_attestations(db, project_id)
    claims = _open_claims(db, project_id)
    refs = _task_refs(db, _referenced_task_ids(runs, worktrees))

    in_flight = tuple(
        _in_flight(row, latest, claims) for row in open_rows if _is_in_flight(row, latest, claims)
    )
    attention = tuple(
        AttentionTask(seq=row["seq_num"], title=row["title"], state=row["state"])
        for row in open_rows
        if row["state"] in _ATTENTION_STATES
    )
    decisions = tuple(
        AttentionTask(seq=row["seq_num"], title=row["title"], state="needs_decision")
        for row in open_rows
        if _NEEDS_DECISION_LABEL in _labels(row)
    )
    return StatusData(
        now=now,
        window_start=since,
        in_flight=in_flight,
        closed=_closed(db, project_id, since),
        escalated=_escalated(db, project_id, since),
        landings=_landings(db, project_id, since, project_path, integration_branch, gaps),
        attention=attention,
        decisions=decisions,
        stalled=_stalled(runs, refs),
        worktrees=_worktrees(worktrees, runs, refs, integration_branch, gaps),
        load_5m=_load_5m(loadavg, gaps),
        load_threshold=load_threshold,
        gaps=tuple(gaps),
    )


def _decode_object(raw: object, gaps: list[str], label: str) -> dict[str, Any] | None:
    """A stored JSON object, or None with a gap naming the unreadable record."""
    try:
        body = json.loads(str(raw))
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    gaps.append(f"{label} - body is not a JSON object; skipped")
    return None


def _labels(row: Mapping[str, Any]) -> list[str]:
    labels = row["labels"]
    if isinstance(labels, str):
        labels = json.loads(labels) if labels else []
    return [label for label in labels if isinstance(label, str)] if isinstance(labels, list) else []


def _latest_attestations(db: HubDatabase, project_id: str) -> dict[str, tuple[str, str, datetime]]:
    latest: dict[str, tuple[str, str, datetime]] = {}
    for row in db.fetchall(_LATEST_ATTESTATIONS_SQL, (project_id,)):
        body = json.loads(str(row["body"]))
        latest[str(row["task_id"])] = (
            str(body.get("kind")),
            str(body.get("result")),
            row["created_at"],
        )
    return latest


def _open_claims(db: HubDatabase, project_id: str) -> dict[str, datetime]:
    return {
        str(row["task_id"]): row["claimed_at"]
        for row in db.fetchall(_OPEN_CLAIMS_SQL, (project_id,))
    }


def _is_in_flight(
    row: Mapping[str, Any],
    latest: Mapping[str, tuple[str, str, datetime]],
    claims: Mapping[str, datetime],
) -> bool:
    task_id = str(row["id"])
    return row["claimed_by_session_id"] is not None or task_id in latest or task_id in claims


def _in_flight(
    row: Mapping[str, Any],
    latest: Mapping[str, tuple[str, str, datetime]],
    claims: Mapping[str, datetime],
) -> InFlightTask:
    task_id = str(row["id"])
    attestation = latest.get(task_id)
    return InFlightTask(
        seq=row["seq_num"],
        title=row["title"],
        stage=None if attestation is None else f"{attestation[0]}/{attestation[1]}",
        evidence_at=None if attestation is None else attestation[2],
        claimed_at=claims.get(task_id),
    )


def _closed(db: HubDatabase, project_id: str, since: datetime) -> tuple[ClosedTask, ...]:
    return tuple(
        ClosedTask(
            seq=row["seq_num"],
            title=row["title"],
            closed_at=row["closed_at"],
            claim_to_close=(
                None
                if row["first_claimed_at"] is None
                else row["closed_at"] - row["first_claimed_at"]
            ),
        )
        for row in db.fetchall(_CLOSED_SQL, (project_id, since))
    )


def _escalated(db: HubDatabase, project_id: str, since: datetime) -> tuple[EscalatedTask, ...]:
    return tuple(
        EscalatedTask(
            seq=row["seq_num"],
            title=row["title"],
            escalated_at=row["escalated_at"],
            reason=row["escalation_reason"],
        )
        for row in db.fetchall(_ESCALATED_SQL, (project_id, since))
    )


def _landings(
    db: HubDatabase,
    project_id: str,
    since: datetime,
    project_path: Path,
    integration_branch: str,
    gaps: list[str],
) -> tuple[Landing, ...]:
    landings: list[Landing] = []
    for row in db.fetchall(_RECEIPTS_SQL, (project_id, CLOSE_RECEIPT_AUTHOR_TYPE, since)):
        body = _decode_object(row["body"], gaps, f"receipt for #{row['seq_num']}")
        if (
            body is None
            or body.get("kind") != LANDING
            or not isinstance(body.get("commit_sha"), str)
        ):
            continue
        sha = str(body["commit_sha"])
        if _OBJECT_NAME.fullmatch(sha) is None:
            gaps.append(
                f"receipt for #{row['seq_num']} - commit_sha is not a hex object name; skipped"
            )
            continue
        verified, error = _verify_landing(project_path, sha, integration_branch)
        if error is not None:
            gaps.append(f"landing {sha[:7]} - integration check unavailable: {error}")
        landings.append(
            Landing(
                seq=row["seq_num"],
                title=row["title"],
                commit_sha=sha,
                recorded_at=row["created_at"],
                verified=verified,
            )
        )
    return tuple(landings)


def _verify_landing(project_path: Path, sha: str, branch: str) -> tuple[bool | None, str | None]:
    try:
        proc = _run_git(project_path, "merge-base", "--is-ancestor", sha, branch)
    except _GitUnavailable as exc:
        return None, str(exc)
    if proc.returncode == 0:
        return True, None
    if proc.returncode == 1:
        return False, None
    return None, proc.stderr.strip() or f"exit {proc.returncode}"


def _stalled(runs: Sequence[AgentRun], refs: Mapping[str, _TaskRef]) -> tuple[StalledSeat, ...]:
    seats: list[StalledSeat] = []
    for run in runs:
        if not run.stall_suspected:
            continue
        ref = refs.get(str(run.task_id)) if run.task_id is not None else None
        seats.append(
            StalledSeat(
                session_ref=str(run.child_session_id or run.id)[:8],
                task_seq=None if ref is None else ref.seq,
                task_title=None if ref is None else ref.title,
            )
        )
    return tuple(seats)


def _worktrees(
    worktrees: Sequence[Worktree],
    runs: Sequence[AgentRun],
    refs: Mapping[str, _TaskRef],
    integration_branch: str,
    gaps: list[str],
) -> tuple[WorktreeRow, ...]:
    live = {str(run.worktree_id) for run in runs if run.worktree_id is not None}
    rows: list[WorktreeRow] = []
    for worktree in worktrees:
        ref = refs.get(str(worktree.task_id)) if worktree.task_id is not None else None
        branch = worktree.branch_name or "(detached)"
        base = worktree.base_branch or integration_branch
        task_seq = None if ref is None else ref.seq
        task_state = None if ref is None else ref.state
        live_seat = str(worktree.id) in live
        try:
            behind, ahead, dirty, last_commit = _worktree_metrics(worktree.worktree_path, base)
        except _GitUnavailable as exc:
            gaps.append(f"worktrees {branch} - git unavailable for {worktree.worktree_path}: {exc}")
            rows.append(
                WorktreeRow(
                    branch=branch,
                    task_seq=task_seq,
                    task_state=task_state,
                    live_seat=live_seat,
                    ahead=None,
                    behind=None,
                    dirty=None,
                    last_commit_at=None,
                    git_error=str(exc),
                )
            )
            continue
        rows.append(
            WorktreeRow(
                branch=branch,
                task_seq=task_seq,
                task_state=task_state,
                live_seat=live_seat,
                ahead=ahead,
                behind=behind,
                dirty=dirty,
                last_commit_at=last_commit,
                git_error=None,
            )
        )
    return tuple(rows)


def _worktree_metrics(path: str, base: str) -> tuple[int, int, int, datetime]:
    counts = _git_text(Path(path), "rev-list", "--left-right", "--count", f"{base}...HEAD").split()
    behind, ahead = int(counts[0]), int(counts[1])
    status = _git_text(Path(path), "status", "--porcelain")
    dirty = sum(1 for line in status.splitlines() if line.strip())
    last_commit = datetime.fromisoformat(_git_text(Path(path), "log", "-1", "--format=%cI"))
    return behind, ahead, dirty, last_commit


def _load_5m(loadavg: Callable[[], tuple[float, float, float]], gaps: list[str]) -> float | None:
    try:
        return loadavg()[1]
    except OSError as exc:
        gaps.append(f"load - sample unavailable: {exc}")
        return None


def _referenced_task_ids(runs: Sequence[AgentRun], worktrees: Sequence[Worktree]) -> list[str]:
    ids = {str(run.task_id) for run in runs if run.task_id is not None}
    ids |= {str(worktree.task_id) for worktree in worktrees if worktree.task_id is not None}
    return sorted(ids)


def _task_refs(db: HubDatabase, task_ids: Sequence[str]) -> dict[str, _TaskRef]:
    if not task_ids:
        return {}
    return {
        str(row["id"]): _TaskRef(seq=row["seq_num"], title=row["title"], state=row["state"])
        for row in db.fetchall(_TASK_REFS_SQL, (list(task_ids),))
    }


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return spawn_run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitUnavailable(str(exc) or type(exc).__name__) from exc


def _git_text(cwd: Path, *args: str) -> str:
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        raise _GitUnavailable(proc.stderr.strip() or f"exit {proc.returncode}")
    return proc.stdout.strip()


def active_worktrees(db: HubDatabase, project_id: str) -> list[Worktree]:
    """Active worktree rows for the project, read through the worktree manager."""
    return LocalWorktreeManager(db).list_worktrees(
        project_id=project_id, status="active", limit=None
    )


def active_runs(db: HubDatabase) -> list[AgentRun]:
    """Live agent runs with their liveness overlay, read through the run manager."""
    return LocalAgentRunManager(db).list_active_global(limit=_RUN_LIMIT)
