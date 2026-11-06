"""Pure renderer tests for the daemon-built operator status report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

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
from gobby.status_report.render import is_nominal, render_markdown

pytestmark = pytest.mark.unit

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)  # 14:30 CST
WINDOW_START = NOW - timedelta(minutes=30)


def _data(
    *,
    in_flight: tuple[InFlightTask, ...] = (),
    closed: tuple[ClosedTask, ...] = (),
    escalated: tuple[EscalatedTask, ...] = (),
    landings: tuple[Landing, ...] = (),
    attention: tuple[AttentionTask, ...] = (),
    decisions: tuple[AttentionTask, ...] = (),
    stalled: tuple[StalledSeat, ...] = (),
    worktrees: tuple[WorktreeRow, ...] = (),
    load_5m: float | None = None,
    load_threshold: float = 60.0,
    gaps: tuple[str, ...] = (),
) -> StatusData:
    return StatusData(
        now=NOW,
        window_start=WINDOW_START,
        in_flight=in_flight,
        closed=closed,
        escalated=escalated,
        landings=landings,
        attention=attention,
        decisions=decisions,
        stalled=stalled,
        worktrees=worktrees,
        load_5m=load_5m,
        load_threshold=load_threshold,
        gaps=gaps,
    )


def _flying(seq: int = 7, stage: str | None = "implement/approved") -> InFlightTask:
    return InFlightTask(
        seq=seq,
        title="Build the report",
        stage=stage,
        evidence_at=NOW - timedelta(minutes=4),
        claimed_at=NOW - timedelta(hours=2, minutes=3),
    )


def test_quiet_tick_with_work_in_flight_is_nominal() -> None:
    data = _data(in_flight=(_flying(),))

    text = render_markdown(data, CT)

    assert is_nominal(data)
    assert text.splitlines()[-1] == "Systems nominal"
    assert "## Holds" not in text
    assert "## Stalled seats" not in text


def test_header_reads_window_in_central_time() -> None:
    text = render_markdown(_data(), CT)

    assert text.splitlines()[0] == "Status, 2026-11-05 14:00-14:30 CT"


def test_empty_in_flight_section_is_omitted_and_stays_nominal() -> None:
    data = _data()

    text = render_markdown(data, CT)

    assert "In flight" not in text
    assert "No task in flight." not in text
    assert text.splitlines()[-1] == "Systems nominal"
    assert is_nominal(data)


def test_in_flight_rows_name_stage_age_and_claim_time() -> None:
    text = render_markdown(_data(in_flight=(_flying(),)), CT)

    assert (
        "- #7 Build the report: stage implement/approved, evidence 4m ago; claimed since 12:27 CT"
        in text
    )


def test_missing_attestation_is_stated_not_inferred() -> None:
    text = render_markdown(_data(in_flight=(_flying(stage=None),)), CT)

    assert "no attestation" in text
    assert "implement/approved" not in text


def test_stalled_seat_withholds_nominal_and_is_named() -> None:
    seat = StalledSeat(session_ref="fc94376c", task_seq=7, task_title="Build the report")
    data = _data(stalled=(seat,))

    text = render_markdown(data, CT)

    assert not is_nominal(data)
    assert "Systems nominal" not in text
    assert "## Stalled seats" in text
    assert "fc94376c" in text
    assert "#7 Build the report" in text


@pytest.mark.parametrize("state", ["escalated", "blocked", "needs_human"])
def test_open_attention_state_withholds_nominal(state: str) -> None:
    data = _data(attention=(AttentionTask(seq=9, title="Stuck work", state=state),))

    text = render_markdown(data, CT)

    assert not is_nominal(data)
    assert "## Holds, blocked and escalated" in text
    assert f"- #9 Stuck work: {state}" in text
    assert "Systems nominal" not in text


def test_escalation_inside_window_withholds_nominal() -> None:
    event = EscalatedTask(seq=11, title="Escalated", escalated_at=NOW, reason="needs review")
    data = _data(escalated=(event,))

    assert not is_nominal(data)
    assert "## Escalated" in render_markdown(data, CT)


def test_load_above_threshold_is_reported_as_single_sample() -> None:
    data = _data(load_5m=72.44, load_threshold=60.0)

    text = render_markdown(data, CT)

    assert not is_nominal(data)
    assert "- 5-minute load 72.4 is above 60 (one sample)" in text


def test_load_at_or_below_threshold_is_not_rendered() -> None:
    data = _data(load_5m=60.0, load_threshold=60.0)

    text = render_markdown(data, CT)

    assert is_nominal(data)
    assert "## Load" not in text
    assert "load" not in text.lower()


def test_gap_withholds_nominal_and_is_listed() -> None:
    data = _data(gaps=("worktrees - git unavailable for /tmp/wt",))

    text = render_markdown(data, CT)

    assert not is_nominal(data)
    assert "## Source gaps" in text
    assert "- GAP: worktrees - git unavailable for /tmp/wt" in text
    assert "Systems nominal" not in text


def test_empty_sections_are_omitted() -> None:
    text = render_markdown(_data(in_flight=(_flying(),)), CT)

    for heading in (
        "## Landed",
        "## Closed",
        "## Escalated",
        "## Decisions needed",
        "## Holds, blocked and escalated",
        "## Stalled seats",
        "## Worktrees",
        "## Load",
        "## Source gaps",
    ):
        assert heading not in text


def test_landings_and_closures_render_their_evidence() -> None:
    landing = Landing(
        seq=3,
        title="Ship it",
        commit_sha="abcdef1234567890",
        recorded_at=NOW,
        verified=True,
    )
    closed = ClosedTask(
        seq=4,
        title="Done it",
        closed_at=NOW,
        claim_to_close=timedelta(hours=2, minutes=3),
    )

    text = render_markdown(_data(landings=(landing,), closed=(closed,)), CT)

    assert "## Landed" in text
    assert "- #3 Ship it: abcdef1 verified on the integration branch" in text
    assert "## Closed" in text
    assert "- #4 Done it: claim-to-close 2h03m" in text


def test_unverified_landing_is_labelled_not_claimed() -> None:
    landing = Landing(
        seq=3,
        title="Ship it",
        commit_sha="abcdef1234567890",
        recorded_at=NOW,
        verified=None,
    )

    text = render_markdown(_data(landings=(landing,)), CT)

    assert "unverified" in text
    assert "verified on the integration branch" not in text


def test_worktree_rows_show_owner_fields_unreconciled() -> None:
    row = WorktreeRow(
        branch="task-7-build",
        task_seq=7,
        task_state="in_progress",
        ahead=2,
        behind=0,
        dirty=1,
        live_seat=True,
        last_commit_at=NOW - timedelta(minutes=12),
        git_error=None,
    )

    text = render_markdown(_data(worktrees=(row,)), CT)

    assert "## Worktrees" in text
    assert "| task-7-build | #7 | in_progress | 2 / 0 | 1 | yes | 12m ago | UNRECONCILED |" in text


def test_worktree_git_failure_renders_unavailable_cell() -> None:
    row = WorktreeRow(
        branch="task-8",
        task_seq=8,
        task_state=None,
        ahead=None,
        behind=None,
        dirty=None,
        live_seat=False,
        last_commit_at=None,
        git_error="timeout",
    )

    text = render_markdown(_data(worktrees=(row,)), CT)

    assert (
        "| task-8 | #8 | none | git unavailable | git unavailable | no | unknown | UNRECONCILED |"
        in text
    )


def test_decisions_needed_are_listed_and_withhold_nominal() -> None:
    data = _data(decisions=(AttentionTask(seq=5, title="Pick one", state="needs_human"),))

    text = render_markdown(data, CT)

    assert not is_nominal(data)
    assert "## Decisions needed" in text
    assert "- #5 Pick one" in text
