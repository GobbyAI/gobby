"""Close evidence merges every session that worked a handed-off task (#21094)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence,
)
from gobby.tasks.transcript_evidence import TranscriptEvidenceUnavailable

_SUPPORT = "gobby.mcp_proxy.tools.tasks._close_evaluation_support"
QA = "qa-session"
IMPLEMENTER = "implementer-session"
CREATOR = "creator-session"
REVIEWER = "reviewer-session"


def _link(session_id: str, action: str, created_at: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "task_id": "task",
        "action": action,
        "created_at": created_at,
    }


def _context(links: list[dict[str, Any]], sessions: dict[str, Any]) -> MagicMock:
    ctx = MagicMock()
    ctx.config = None
    # Storage returns newest link first, exactly like get_task_sessions.
    ctx.session_task_manager.get_task_sessions.return_value = sorted(
        links, key=lambda row: row["created_at"], reverse=True
    )
    ctx.session_manager.get.side_effect = sessions.get
    return ctx


def _session(session_id: str, created_at: str) -> SimpleNamespace:
    return SimpleNamespace(id=session_id, created_at=created_at)


async def _derive(
    ctx: MagicMock,
    *,
    owner: str,
    closing: str,
    owner_window: str | None,
    unreadable: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, Any]], Any]:
    """Drive the merge with recording fakes; ``unreadable`` sessions raise as missing."""
    calls: list[tuple[str, Any]] = []

    async def record(session: Any, window_start: Any, *args: Any, **kwargs: Any) -> str:
        calls.append((session.id, window_start))
        if session.id in unreadable:
            raise TranscriptEvidenceUnavailable(
                "transcript missing", source="unknown", attempted_paths=("/nope",)
            )
        return f"evidence:{session.id}"

    merged: Any
    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.derive_transcript_evidence", new=AsyncMock(side_effect=record)),
        patch(f"{_SUPPORT}.merge_transcript_evidence", side_effect=lambda *sets: list(sets)),
    ):
        merged = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=owner,
            closing_session_id=closing,
            owner_window_start=owner_window,
            task_edited_files=set(),
            repo_path="/repo",
        )
    return calls, merged


@pytest.mark.asyncio
async def test_handed_off_task_merges_the_implementer_session_within_its_own_window() -> None:
    links = [
        _link(CREATOR, "created", "2026-08-27T00:00:00+00:00"),
        _link(IMPLEMENTER, "worked_on", "2026-08-27T01:00:00+00:00"),
        _link(IMPLEMENTER, "claimed", "2026-08-27T01:05:00+00:00"),
        _link(IMPLEMENTER, "escalated", "2026-08-27T02:00:00+00:00"),
        _link(QA, "claimed", "2026-08-27T02:10:00+00:00"),
    ]
    ctx = _context(
        links,
        {
            CREATOR: _session(CREATOR, "2026-08-26T23:00:00+00:00"),
            IMPLEMENTER: _session(IMPLEMENTER, "2026-08-27T00:30:00+00:00"),
            QA: _session(QA, "2026-08-27T02:00:00+00:00"),
        },
    )

    calls, merged = await _derive(
        ctx, owner=QA, closing=QA, owner_window="2026-08-27T02:10:00+00:00"
    )

    assert calls == [
        (QA, "2026-08-27T02:10:00+00:00"),
        (IMPLEMENTER, "2026-08-27T01:00:00+00:00"),
    ]
    assert merged == [f"evidence:{QA}", f"evidence:{IMPLEMENTER}"]


@pytest.mark.asyncio
async def test_closing_session_keeps_its_link_window_and_creator_is_excluded() -> None:
    links = [
        _link(CREATOR, "created", "2026-08-27T00:00:00+00:00"),
        _link(IMPLEMENTER, "claimed", "2026-08-27T01:00:00+00:00"),
        _link(REVIEWER, "worked_on", "2026-08-27T03:00:00+00:00"),
    ]
    ctx = _context(
        links,
        {
            CREATOR: _session(CREATOR, "2026-08-26T23:00:00+00:00"),
            IMPLEMENTER: _session(IMPLEMENTER, "2026-08-27T00:30:00+00:00"),
            REVIEWER: _session(REVIEWER, "2026-08-27T02:30:00+00:00"),
        },
    )

    calls, _ = await _derive(
        ctx, owner=IMPLEMENTER, closing=REVIEWER, owner_window="2026-08-27T01:00:00+00:00"
    )

    assert calls == [
        (IMPLEMENTER, "2026-08-27T01:00:00+00:00"),
        (REVIEWER, "2026-08-27T03:00:00+00:00"),
    ]


@pytest.mark.asyncio
async def test_single_session_close_derives_once_from_the_owner_window() -> None:
    ctx = _context(
        [_link(QA, "claimed", "2026-08-27T02:10:00+00:00")],
        {QA: _session(QA, "2026-08-27T02:00:00+00:00")},
    )

    calls, merged = await _derive(ctx, owner=QA, closing=QA, owner_window="owner-window")

    assert calls == [(QA, "owner-window")]
    assert merged == [f"evidence:{QA}"]


@pytest.mark.asyncio
async def test_linked_session_that_no_longer_exists_or_has_no_transcript_is_skipped() -> None:
    gone = "deleted-session"
    links = [
        _link(gone, "claimed", "2026-08-27T00:30:00+00:00"),
        _link(IMPLEMENTER, "claimed", "2026-08-27T01:00:00+00:00"),
        _link(QA, "claimed", "2026-08-27T02:10:00+00:00"),
    ]
    ctx = _context(
        links,
        {
            IMPLEMENTER: _session(IMPLEMENTER, "2026-08-27T00:30:00+00:00"),
            QA: _session(QA, "2026-08-27T02:00:00+00:00"),
        },
    )

    calls, merged = await _derive(
        ctx,
        owner=QA,
        closing=QA,
        owner_window="2026-08-27T02:10:00+00:00",
        unreadable=frozenset({IMPLEMENTER}),
    )

    # The deleted session is never parsed; the unreadable one is attempted, then dropped.
    assert [session_id for session_id, _ in calls] == [QA, IMPLEMENTER]
    assert ctx.session_manager.get.call_count == 3
    assert merged == [f"evidence:{QA}"]


@pytest.mark.asyncio
async def test_missing_owner_or_closing_session_still_raises() -> None:
    ctx = _context(
        [_link(QA, "claimed", "2026-08-27T02:10:00+00:00")],
        {QA: _session(QA, "2026-08-27T02:00:00+00:00")},
    )

    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.derive_transcript_evidence", new=AsyncMock()),
        pytest.raises(TranscriptEvidenceUnavailable, match="not found"),
    ):
        await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=QA,
            closing_session_id="vanished-closer",
            owner_window_start=None,
            task_edited_files=set(),
            repo_path="/repo",
        )
