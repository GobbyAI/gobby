"""Close evidence merges every session that worked a handed-off task (#21094)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence,
)
from gobby.storage.session_models import Session
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
)
from gobby.utils.machine_id import get_machine_id
from tests.tasks.test_close_checklist import _run

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
    # ``source`` and ``transcript_path`` carry the provider identity the close path
    # reads before parsing, so every stand-in session must supply them.
    return SimpleNamespace(
        id=session_id,
        created_at=created_at,
        source="claude",
        transcript_path=None,
    )


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
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
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
async def test_close_uses_only_target_task_checkout_paths_for_linked_session() -> None:
    session = _session(IMPLEMENTER, "2026-08-27T00:30:00+00:00")
    session.workspace_path = "/work/task-260"
    ctx = _context(
        [_link(IMPLEMENTER, "claimed", "2026-08-27T01:00:00+00:00")],
        {IMPLEMENTER: session},
    )
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {
            "task": {
                "/work/task-259-runbook": ["docs/replenishment.md"],
                "/work/task-259-supply": ["docs/replenishment.md"],
            },
            "task-260": {"/work/task-260": ["docs/replenishment.md"]},
        }
    }
    seen_paths: list[frozenset[tuple[str, str]]] = []

    async def record(*args: Any, **kwargs: Any) -> TranscriptEvidence:
        assert args[4] == "/work/task-260"
        seen_paths.append(kwargs["task_checkout_paths"])
        return TranscriptEvidence()

    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_transcript_evidence", new=AsyncMock(side_effect=record)),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        merged = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=IMPLEMENTER,
            closing_session_id=IMPLEMENTER,
            owner_window_start="2026-08-27T01:00:00+00:00",
            task_edited_files={"docs/replenishment.md"},
            repo_path="/work/task-260",
        )

    assert merged == TranscriptEvidence()
    assert seen_paths == [
        frozenset(
            {
                ("/work/task-259-runbook", "docs/replenishment.md"),
                ("/work/task-259-supply", "docs/replenishment.md"),
            }
        )
    ]


@pytest.mark.asyncio
async def test_close_commit_fallback_supplies_exact_checkout_paths() -> None:
    session = _session(IMPLEMENTER, "2026-08-27T00:30:00+00:00")
    ctx = _context(
        [_link(IMPLEMENTER, "claimed", "2026-08-27T01:00:00+00:00")],
        {IMPLEMENTER: session},
    )
    ctx.session_var_manager.get_variables.return_value = {}
    seen_paths: list[frozenset[tuple[str, str]]] = []

    async def record(*args: Any, **kwargs: Any) -> TranscriptEvidence:
        seen_paths.append(kwargs["task_checkout_paths"])
        return TranscriptEvidence()

    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_transcript_evidence", new=AsyncMock(side_effect=record)),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        merged = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=IMPLEMENTER,
            closing_session_id=IMPLEMENTER,
            owner_window_start="2026-08-27T01:00:00+00:00",
            task_edited_files={"tests/test_change.py", "src/change.py"},
            repo_path="/work/task-259",
            owner_used_commit_fallback=True,
        )

    assert merged == TranscriptEvidence()
    ctx.session_var_manager.get_variables.assert_called_once_with(IMPLEMENTER)
    assert seen_paths == [
        frozenset(
            {
                ("/work/task-259", "tests/test_change.py"),
                ("/work/task-259", "src/change.py"),
            }
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_task_history", [False, True])
@pytest.mark.parametrize(
    "owner_task_ledger,owner_other_task_ledger,owner_other_task_history,legacy_other_task",
    [
        (True, False, False, "none"),
        (False, False, False, "none"),
        (False, True, False, "none"),
        (True, True, False, "none"),
        (False, False, True, "none"),
        (False, False, False, "open_overlap"),
        (True, False, False, "open_overlap"),
        (True, False, True, "open_overlap"),
        (True, False, False, "closed_overlap"),
        (True, False, False, "closed_disjoint"),
        (True, False, False, "closed_unresolved"),
        (False, False, False, "closed_before_window"),
        (False, False, True, "closed_before_window"),
        (True, False, True, "closed_before_window"),
        (True, True, True, "closed_before_window"),
        (False, False, False, "linked_after_history"),
    ],
)
async def test_close_excludes_other_task_edit_in_same_checkout(
    tmp_path: Path,
    owner_task_history: bool,
    owner_task_ledger: bool,
    owner_other_task_ledger: bool,
    owner_other_task_history: bool,
    legacy_other_task: str,
) -> None:
    start = datetime(2026, 8, 27, 1, tzinfo=UTC)
    relative_path = "src/shared.py"
    absolute_path = tmp_path / relative_path
    machine_id = get_machine_id()
    assert machine_id is not None

    def session_with_edit(session_id: str, offset: int) -> Session:
        transcript = tmp_path / f"{session_id}.jsonl"
        records = [
            {
                "type": "assistant",
                "timestamp": (start + timedelta(seconds=offset)).isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"edit-{session_id}",
                            "name": "Edit",
                            "input": {"file_path": str(absolute_path)},
                        }
                    ],
                },
            }
        ]
        if session_id == IMPLEMENTER:
            records.extend(
                [
                    {
                        "type": "assistant",
                        "timestamp": (start + timedelta(seconds=offset + 1)).isoformat(),
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "validation",
                                    "name": "Bash",
                                    "input": {
                                        "command": "uv run pytest tests/tasks/test_validation.py -q"
                                    },
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "timestamp": (start + timedelta(seconds=offset + 2)).isoformat(),
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "validation",
                                    "content": {"exit_code": 0, "stdout": "1 passed in 0.1s"},
                                }
                            ],
                        },
                    },
                ]
            )
        transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return Session(
            id=session_id,
            external_id=session_id,
            machine_id=machine_id,
            source="claude",
            project_id="project",
            title=None,
            status="active",
            transcript_path=str(transcript),
            summary_path=None,
            summary_markdown=None,
            git_branch="test",
            parent_session_id=None,
            created_at=start,
            updated_at=start,
        )

    ctx = _context(
        [
            _link(IMPLEMENTER, "claimed", start.isoformat()),
            _link(QA, "worked_on", (start + timedelta(seconds=60)).isoformat()),
        ],
        {
            IMPLEMENTER: session_with_edit(IMPLEMENTER, 30),
            QA: session_with_edit(QA, 120),
        },
    )
    if legacy_other_task != "none":
        ctx.session_task_manager.get_session_tasks.return_value = [
            {
                "task": SimpleNamespace(
                    id="other-task",
                    closed_at=(
                        start - timedelta(seconds=1)
                        if legacy_other_task == "closed_before_window"
                        else start + timedelta(seconds=90)
                        if legacy_other_task.startswith("closed_")
                        else None
                    ),
                    commits=["other-commit"],
                ),
                "action": "claimed",
                "link_created_at": start - timedelta(seconds=30),
            }
        ]
    owner_checkouts: dict[str, dict[str, list[str]]] = {}
    if owner_task_ledger:
        owner_checkouts["task"] = {str(tmp_path): [relative_path]}
    if owner_other_task_ledger:
        # The owner may edit the same path for B, with or without an A ledger.
        owner_checkouts["other-task"] = {str(tmp_path): [relative_path]}
    owner_variables: dict[str, Any] = {"task_edited_file_checkouts": owner_checkouts}
    if legacy_other_task == "linked_after_history":
        owner_variables["task_edited_file_checkouts_history_started_at"] = (
            start - timedelta(seconds=60)
        ).timestamp()
    if owner_other_task_history:
        # B's live ledger was released after commit; the durable edit remains.
        owner_variables["task_edited_file_checkouts_history"] = {
            "other-task": {str(tmp_path): [relative_path]}
        }
    if owner_task_history:
        owner_variables.setdefault("task_edited_file_checkouts_history", {})["task"] = {
            str(tmp_path): [relative_path]
        }
    qa_variables = {"task_edited_file_checkouts": {"other-task": {str(tmp_path): [relative_path]}}}
    ctx.session_var_manager.get_variables.side_effect = {
        IMPLEMENTER: owner_variables,
        QA: qa_variables,
    }.get

    with (
        patch(
            f"{_SUPPORT}.resolve_validation_detection_config",
            return_value=default_validation_detection_config(),
        ),
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
        patch(
            f"{_SUPPORT}.collect_commit_paths_async",
            new=AsyncMock(
                side_effect=RuntimeError("Cannot inspect changed paths for commit other-commit.")
                if legacy_other_task == "closed_unresolved"
                else None,
                return_value={
                    "src/unrelated.py" if legacy_other_task == "closed_disjoint" else relative_path
                },
            ),
        ) as commit_paths,
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=IMPLEMENTER,
            closing_session_id=IMPLEMENTER,
            owner_window_start=start.isoformat(),
            task_edited_files={relative_path},
            repo_path=str(tmp_path),
            owner_used_commit_fallback=not owner_task_ledger,
        )

    history_overlaps = owner_other_task_history and legacy_other_task != "closed_before_window"
    expected_owner_edits = (
        []
        if owner_other_task_ledger
        or history_overlaps
        or legacy_other_task in ("closed_overlap", "closed_unresolved")
        else [(relative_path, start + timedelta(seconds=30), IMPLEMENTER)]
    )
    assert [(edit.path, edit.timestamp, edit.session_id) for edit in evidence.edits] == (
        expected_owner_edits
    )
    assert all(edit.session_id != QA for edit in evidence.edits)
    assert [run.command for run in evidence.validation_runs] == [
        "uv run pytest tests/tasks/test_validation.py -q"
    ]
    assert ctx.session_var_manager.get_variables.call_count == 2
    # Only a close that dropped a legacy ledger needs its commits as proof.
    assert commit_paths.await_count == (
        1 if legacy_other_task in ("closed_overlap", "closed_disjoint", "closed_unresolved") else 0
    )


@pytest.mark.asyncio
async def test_prelink_runs_are_attached_without_changing_credited_runs() -> None:
    window = "2026-08-27T02:10:00+00:00"
    ctx = _context(
        [_link(QA, "claimed", window)],
        {QA: _session(QA, "2026-08-27T02:00:00+00:00")},
    )
    credited = _run(2, command="uv run ruff check src/")
    excluded = _run(1, command="uv run pytest tests/ -q")
    original = TranscriptEvidence(validation_runs=(credited,))
    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.derive_transcript_evidence", new=AsyncMock(return_value=original)),
        patch(
            f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=(excluded,))
        ) as prelink,
    ):
        result = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=QA,
            closing_session_id=QA,
            owner_window_start=window,
            task_edited_files=set(),
            repo_path="/repo",
        )
    assert [run.command for run in result.validation_runs] == [credited.command]
    assert result.excluded_runs == (excluded,)
    assert original.excluded_runs == ()
    prelink.assert_awaited_once()
    assert prelink.call_args.args[1] == window


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
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())) as prelink,
        pytest.raises(TranscriptEvidenceUnavailable, match="not found") as error,
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
    assert "vanished-closer" in str(error.value)
    prelink.assert_awaited_once()
    assert prelink.call_args.args[0].id == QA


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_optional_evidence_excludes_unrelated_closing_session(linked: bool) -> None:
    window = "2026-08-27T02:10:00+00:00"
    ctx = _context(
        [_link(IMPLEMENTER, "worked_on", window)] if linked else [],
        {
            IMPLEMENTER: _session(IMPLEMENTER, window),
            REVIEWER: _session(REVIEWER, "2026-08-26T00:00:00+00:00"),
        },
    )
    derive = AsyncMock()
    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.derive_transcript_evidence", derive),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
        patch(f"{_SUPPORT}.merge_transcript_evidence"),
    ):
        await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=REVIEWER,
            closing_session_id=REVIEWER,
            owner_window_start=None,
            task_edited_files=set(),
            repo_path="/repo",
            require_task_link=True,
        )
    assert derive.await_count == int(linked)
    if linked:
        assert derive.await_args is not None
        assert derive.await_args.args[0].id == IMPLEMENTER
        assert derive.await_args.args[1] == window
    else:
        ctx.session_manager.get.assert_not_called()


def _claude_edit_session(transcript: Path, session_id: str, edited: Path, at: datetime) -> Session:
    record = {
        "type": "assistant",
        "timestamp": at.isoformat(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"edit-{session_id}",
                    "name": "Edit",
                    "input": {"file_path": str(edited)},
                }
            ],
        },
    }
    transcript.write_text(json.dumps(record) + "\n")
    machine_id = get_machine_id()
    assert machine_id is not None
    return Session(
        id=session_id,
        external_id=session_id,
        machine_id=machine_id,
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=at,
        updated_at=at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "implementer_ledger",
    ["task_edited_file_checkouts", "task_edited_file_checkouts_history"],
)
async def test_linked_implementer_test_edits_survive_a_partial_owner_ledger(
    tmp_path: Path,
    implementer_ledger: str,
) -> None:
    # #23017: the reclaiming owner's ledger held one later production path, and
    # the implementer's proven test edits were narrowed away by it (#22781).
    start = datetime(2026, 9, 28, 13, tzinfo=UTC)
    test_path = "tests/test_named.py"
    owner_path = "src/owner.py"
    ctx = _context(
        [
            _link(IMPLEMENTER, "claimed", start.isoformat()),
            _link(QA, "claimed", (start + timedelta(hours=8)).isoformat()),
        ],
        {
            IMPLEMENTER: _claude_edit_session(
                tmp_path / "implementer.jsonl",
                IMPLEMENTER,
                tmp_path / test_path,
                start + timedelta(minutes=10),
            ),
            QA: _claude_edit_session(
                tmp_path / "qa.jsonl",
                QA,
                tmp_path / owner_path,
                start + timedelta(hours=9),
            ),
        },
    )
    ctx.session_var_manager.get_variables.side_effect = {
        IMPLEMENTER: {implementer_ledger: {"task": {str(tmp_path): [test_path]}}},
        QA: {"task_edited_file_checkouts": {"task": {str(tmp_path): [owner_path]}}},
    }.get

    with (
        patch(
            f"{_SUPPORT}.resolve_validation_detection_config",
            return_value=default_validation_detection_config(),
        ),
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=QA,
            closing_session_id=QA,
            owner_window_start=(start + timedelta(hours=8)).isoformat(),
            task_edited_files={owner_path},
            repo_path=str(tmp_path),
        )

    assert sorted((edit.session_id, edit.path, edit.timestamp) for edit in evidence.edits) == [
        (IMPLEMENTER, test_path, start + timedelta(minutes=10)),
        (QA, owner_path, start + timedelta(hours=9)),
    ]
    assert set(evidence.sessions) == {IMPLEMENTER, QA}
    assert not evidence.degraded_capabilities


def _session_link(task_id: str, created_at: str) -> dict[str, Any]:
    return {
        "task": SimpleNamespace(id=task_id, closed_at=None),
        "action": "claimed",
        "link_created_at": created_at,
    }


def _run_at(session_id: str, at: str, outcome: str) -> Any:
    started = datetime.fromisoformat(at)
    return replace(
        _run(1, outcome=outcome),
        session_id=session_id,
        started_at=started,
        completed_at=started + timedelta(seconds=5),
    )


@pytest.mark.asyncio
async def test_linked_session_runs_after_it_claims_another_task_are_not_credited() -> None:
    """#22884 found work: another task's failing validation must not gate this close."""
    ctx = _context(
        [
            _link(IMPLEMENTER, "claimed", "2026-09-28T13:55:00+00:00"),
            _link(QA, "claimed", "2026-09-28T20:00:00+00:00"),
        ],
        {
            IMPLEMENTER: _session(IMPLEMENTER, "2026-09-28T13:00:00+00:00"),
            QA: _session(QA, "2026-09-28T19:00:00+00:00"),
        },
    )
    ctx.session_var_manager.get_variables.return_value = {}
    ctx.session_task_manager.get_session_tasks.side_effect = {
        IMPLEMENTER: [
            _session_link("other-task", "2026-09-29T02:14:00+00:00"),
            _session_link("task", "2026-09-28T13:55:00+00:00"),
        ],
        # The owner's own later claim never bounds its window.
        QA: [
            _session_link("owner-next", "2026-09-28T21:00:00+00:00"),
            _session_link("task", "2026-09-28T20:00:00+00:00"),
        ],
    }.__getitem__
    runs = {
        IMPLEMENTER: (
            _run_at(IMPLEMENTER, "2026-09-28T14:30:00+00:00", "success"),
            _run_at(IMPLEMENTER, "2026-09-29T02:40:00+00:00", "failure"),
        ),
        QA: (
            _run_at(QA, "2026-09-28T20:30:00+00:00", "success"),
            _run_at(QA, "2026-09-28T21:30:00+00:00", "success"),
        ),
    }

    async def record(session: Any, *args: Any, **kwargs: Any) -> TranscriptEvidence:
        own = runs[session.id]
        return TranscriptEvidence(validation_runs=own, command_runs=own, sessions=(session.id,))

    with (
        patch(f"{_SUPPORT}.resolve_validation_detection_config"),
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_transcript_evidence", new=AsyncMock(side_effect=record)),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
        patch(f"{_SUPPORT}.merge_transcript_evidence", side_effect=lambda *sets: list(sets)),
    ):
        merged: Any = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=QA,
            closing_session_id=QA,
            owner_window_start="2026-09-28T20:00:00+00:00",
            task_edited_files=set(),
            repo_path="/repo",
        )

    by_session = {evidence.sessions[0]: evidence for evidence in merged}
    assert by_session[IMPLEMENTER].validation_runs == runs[IMPLEMENTER][:1]
    assert by_session[IMPLEMENTER].command_runs == runs[IMPLEMENTER][:1]
    assert by_session[QA].validation_runs == runs[QA]
