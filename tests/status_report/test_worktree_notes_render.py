"""Worktree rows show a reconcile note only while it is newer than the tree's last change."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gobby.status_report.model import IdleHolder, StatusData, WorktreeRow
from gobby.status_report.render import render_markdown
from gobby.storage.worktree_notes import WorktreeNote

pytestmark = pytest.mark.unit

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)
LAST_COMMIT = NOW - timedelta(minutes=12)


def test_holder_columns_and_idle_callouts_use_operator_time_and_escape_cells() -> None:
    row = replace(
        _row(note=WorktreeNote("fixture", "land", "await build", NOW, eta="14:45 CT")),
        task_title="Build | synthetic widget",
        holder_session_id="fixture-session",
        holder_agent="fixture-builder",
        holder_status="paused",
        claimed_at=NOW - timedelta(hours=2),
        stage="test-after/approved",
        task_type="epic",
        open_leaf_count=2,
        landing_target="fixture-integration",
    )
    data = replace(
        _data(row),
        idle_holders=(
            IdleHolder(
                7,
                "Build | synthetic widget",
                "fixture-session",
                "fixture-builder",
                "paused",
                2700,
            ),
        ),
    )

    rendered = render_markdown(data, CT)

    assert "#7 Build \\| synthetic widget" in rendered
    assert "fixture-builder" in rendered
    assert "paused" in rendered
    assert "12:30 CST" in rendered
    assert "test-after/approved" in rendered
    assert "14:45 CT" in rendered
    assert "2 open leaves" in rendered
    assert "fixture-integration" in rendered
    assert "## Idle holders" in rendered
    assert "45m" in rendered.split("## Idle holders", 1)[1]


def _data(*rows: WorktreeRow) -> StatusData:
    return StatusData(
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        in_flight=(),
        closed=(),
        escalated=(),
        landings=(),
        attention=(),
        decisions=(),
        stalled=(),
        worktrees=rows,
        load_5m=None,
        load_threshold=60.0,
        gaps=(),
    )


def _row(
    *,
    note: WorktreeNote | None = None,
    change_at: datetime | None = LAST_COMMIT,
    git_error: str | None = None,
) -> WorktreeRow:
    return WorktreeRow(
        branch="task-7-build",
        task_seq=7,
        task_state="in_progress",
        ahead=2,
        behind=0,
        dirty=1,
        live_seat=True,
        last_commit_at=LAST_COMMIT,
        git_error=git_error,
        note=note,
        change_at=change_at,
    )


def _note(
    *,
    updated_at: datetime,
    owner: str = "dev-1",
    next_action: str = "rerun the focused tests",
    blocker: str = "",
) -> WorktreeNote:
    return WorktreeNote(
        owner=owner, next_action=next_action, blocker=blocker, updated_at=updated_at
    )


def test_note_newer_than_the_last_change_renders_owner_next_action_and_blocker() -> None:
    row = _row(note=_note(updated_at=NOW - timedelta(minutes=2)))

    text = render_markdown(_data(row), CT)

    assert (
        "| task-7-build | #7 | in_progress | 2 / 0 | 1 | yes | 12m ago "
        "| dev-1 / rerun the focused tests / blocker: none |"
    ) in text


def test_recorded_blocker_is_rendered_in_place_of_none() -> None:
    row = _row(
        note=_note(updated_at=NOW - timedelta(minutes=2), blocker="waiting on review"),
    )

    text = render_markdown(_data(row), CT)

    assert "/ blocker: waiting on review |" in text


def test_note_older_than_the_last_change_is_unreconciled() -> None:
    row = _row(note=_note(updated_at=NOW - timedelta(minutes=20)))

    text = render_markdown(_data(row), CT)

    assert "| 12m ago | UNRECONCILED |" in text
    assert "dev-1" not in text


def test_unknown_change_time_stays_unreconciled_even_with_a_note() -> None:
    row = _row(note=_note(updated_at=NOW - timedelta(minutes=2)), change_at=None)

    text = render_markdown(_data(row), CT)

    assert "| 12m ago | UNRECONCILED |" in text


def test_git_failure_stays_unreconciled_even_with_a_note() -> None:
    row = WorktreeRow(
        branch="task-7-build",
        task_seq=7,
        task_state="in_progress",
        ahead=None,
        behind=None,
        dirty=None,
        live_seat=False,
        last_commit_at=None,
        git_error="git timed out",
        note=_note(updated_at=NOW - timedelta(minutes=2)),
        change_at=None,
    )

    text = render_markdown(_data(row), CT)

    assert text.count("UNRECONCILED") >= 1
    assert "dev-1" not in text


def test_pipe_in_note_text_does_not_split_the_table_row() -> None:
    row = _row(note=_note(updated_at=NOW - timedelta(minutes=2), next_action="fix a|b"))

    text = render_markdown(_data(row), CT)

    assert "| dev-1 / fix a\\|b / blocker: none |" in text
