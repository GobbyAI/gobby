"""Deterministic markdown rendering for the operator status report."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

_UNRECONCILED = "UNRECONCILED"
_NOMINAL = "Systems nominal"
_GIT_UNAVAILABLE = "git unavailable"


def _breaching_load(data: StatusData) -> float | None:
    """The single sampled 5-minute load when it is above the threshold, else None."""
    if data.load_5m is not None and data.load_5m > data.load_threshold:
        return data.load_5m
    return None


def is_nominal(data: StatusData) -> bool:
    """True only when the evidence shows nothing stuck, alarming, breaching or unmeasured."""
    return not (
        data.attention
        or data.decisions
        or data.stalled
        or data.idle_holders
        or data.escalated
        or data.gaps
        or _breaching_load(data) is not None
    )


def render_markdown(data: StatusData, zone: ZoneInfo) -> str:
    lines = [_header(data, zone), ""]
    lines += _in_flight_section(data, zone)
    lines += _section("Landed", [_landing_line(item) for item in data.landings])
    lines += _section("Closed", [_closed_line(item) for item in data.closed])
    lines += _section("Escalated", [_escalated_line(item, zone) for item in data.escalated])
    lines += _section("Decisions needed", [_decision_line(item) for item in data.decisions])
    lines += _section(
        "Holds, blocked and escalated", [_attention_line(item) for item in data.attention]
    )
    lines += _section("Stalled seats", [_stalled_line(item) for item in data.stalled])
    lines += _worktree_section(data, zone)
    if (load := _breaching_load(data)) is not None:
        lines += _section(
            "Load",
            [f"- 5-minute load {load:.1f} is above {data.load_threshold:g} (one sample)"],
        )
    lines += _section("Source gaps", [f"- GAP: {gap}" for gap in data.gaps])
    lines += _section(
        "Idle holders",
        [
            f"- #{holder.task_seq} {holder.task_title}: {holder.agent or 'unknown agent'} "
            f"({holder.session_id}, {holder.status or 'unknown state'}), "
            + (
                "idle time unknown"
                if holder.idle_seconds is None
                else f"idle {_age(timedelta(seconds=holder.idle_seconds))}"
            )
            for holder in data.idle_holders
        ],
    )
    if is_nominal(data):
        lines.append(_NOMINAL)
    return "\n".join(lines).rstrip() + "\n"


def _header(data: StatusData, zone: ZoneInfo) -> str:
    local_now = data.now.astimezone(zone)
    return (
        f"Status, {local_now:%Y-%m-%d} {_clock(data.window_start, zone)}-"
        f"{_clock(data.now, zone)} CT"
    )


def _in_flight_section(data: StatusData, zone: ZoneInfo) -> list[str]:
    if not data.in_flight:
        return []
    rows = [_in_flight_line(item, data.now, zone) for item in data.in_flight]
    return _section("In flight", rows)


def _section(title: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    return [f"## {title}", *rows, ""]


def _clock(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%H:%M")


def _age(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds())) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def _in_flight_line(task: InFlightTask, now: datetime, zone: ZoneInfo) -> str:
    if task.stage is None:
        evidence = "no attestation"
    elif task.evidence_at is None:
        evidence = f"stage {task.stage}, evidence age unknown"
    else:
        evidence = f"stage {task.stage}, evidence {_age(now - task.evidence_at)} ago"
    if task.claimed_at is None:
        claim = "untimed: no claim interval"
    else:
        claim = f"claimed since {_clock(task.claimed_at, zone)} CT"
    return f"- #{task.seq} {task.title}: {evidence}; {claim}"


def _landing_line(landing: Landing) -> str:
    sha = landing.commit_sha[:7]
    if landing.verified is None:
        status = "unverified (integration branch check unavailable)"
    elif landing.verified:
        status = "verified on the integration branch"
    else:
        status = "not on the integration branch"
    return f"- #{landing.seq} {landing.title}: {sha} {status}"


def _closed_line(task: ClosedTask) -> str:
    if task.claim_to_close is None:
        return f"- #{task.seq} {task.title}: claim-to-close untimed (no claim interval)"
    return f"- #{task.seq} {task.title}: claim-to-close {_age(task.claim_to_close)}"


def _escalated_line(task: EscalatedTask, zone: ZoneInfo) -> str:
    when = "time unknown" if task.escalated_at is None else _clock(task.escalated_at, zone) + " CT"
    reason = task.reason or "no reason recorded"
    return f"- #{task.seq} {task.title}: escalated {when} ({reason})"


def _decision_line(task: AttentionTask) -> str:
    return f"- #{task.seq} {task.title}: awaiting an operator decision"


def _attention_line(task: AttentionTask) -> str:
    return f"- #{task.seq} {task.title}: {task.state}"


def _stalled_line(seat: StalledSeat) -> str:
    if seat.task_seq is None:
        task = "no task"
    else:
        task = f"#{seat.task_seq} {seat.task_title or ''}".rstrip()
    return f"- {seat.session_ref}: stalled on {task}"


def _worktree_section(data: StatusData, zone: ZoneInfo) -> list[str]:
    if not data.worktrees:
        return []
    header = (
        "| Branch | Task | Task state | Ahead / behind base | Dirty files | Live seat "
        "| Last commit | Owner, next action, blocker | Claim holder / state | Claimed since "
        "| Latest attestation | ETA | Landing needs |"
    )
    rule = "|" + "---|" * 13
    rows = [_worktree_row(row, data.now, zone) for row in data.worktrees]
    return ["## Worktrees", header, rule, *rows, ""]


def _owner_cell(row: WorktreeRow) -> str:
    note = row.current_note
    if note is None:
        return _UNRECONCILED
    blocker = note.blocker or "none"
    # A pipe in recorded text would split the table row, so escape it like any table cell.
    text = f"{note.owner} / {note.next_action} / blocker: {blocker}"
    return text.replace("|", "\\|")


def _worktree_row(row: WorktreeRow, now: datetime, zone: ZoneInfo) -> str:
    task = "none" if row.task_seq is None else f"#{row.task_seq}"
    if row.task_title:
        task += " " + row.task_title.replace("|", "\\|")
    state = row.task_state or "none"
    if row.git_error is not None:
        ahead_behind = _GIT_UNAVAILABLE
        dirty = _GIT_UNAVAILABLE
    else:
        ahead_behind = f"{row.ahead} / {row.behind}"
        dirty = str(row.dirty)
    if row.last_commit_at is None:
        last_commit = "unknown"
    else:
        last_commit = f"{_age(now - row.last_commit_at)} ago"
    live = "yes" if row.live_seat else "no"
    holder = (
        "unclaimed"
        if row.holder_session_id is None
        else (
            f"{row.holder_agent or 'unknown agent'} ({row.holder_session_id}) / "
            f"{row.holder_status or 'unknown state'}"
        )
    )
    since = (
        "none"
        if row.claimed_at is None
        else row.claimed_at.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    )
    eta = row.current_note.eta or "unknown" if row.current_note is not None else _UNRECONCILED
    needs = f"target: {row.landing_target or 'unknown'}"
    if row.open_leaf_count is not None:
        needs = (
            f"{row.open_leaf_count} open leaves; repair: {row.integration_leaf or 'unknown'}; "
            + needs
        )
    return (
        f"| {row.branch} | {task} | {state} | {ahead_behind} | {dirty} | {live} "
        f"| {last_commit} | {_owner_cell(row)} | {holder.replace('|', '\\|')} | {since} "
        f"| {row.stage or 'none'} | {eta.replace('|', '\\|')} | {needs.replace('|', '\\|')} |"
    )
