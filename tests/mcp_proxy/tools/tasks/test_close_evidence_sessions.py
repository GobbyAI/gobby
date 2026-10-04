"""Close evidence merges every session that worked a handed-off task (#21094)."""

from __future__ import annotations

import json
import subprocess
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
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptTaskClaim,
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


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "partial-owner",
        "ambiguous-root",
        "root-mismatch",
        "machine-mismatch",
        "project-mismatch",
        "before-window",
        "after-window",
        "wrong-task",
        "no-terminal",
        "late-valid",
        "late-ambiguous-root",
    ],
)
async def test_legacy_linked_edits_require_registered_task_checkout_proof(
    tmp_path: Path, case: str
) -> None:
    root = tmp_path / "task-checkout"
    root.mkdir()
    test_path, production_path = "tests/test_named.py", "src/named.py"
    for path, source in (
        (test_path, "def test_named():\n    assert named()\n"),
        (production_path, "def named():\n    return True\n"),
    ):
        target = root / path
        target.parent.mkdir()
        target.write_text(source)
    for args in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.email=t@t", "-c", "user.name=Test", "commit", "-qm", "task work"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    start = datetime(2026, 9, 30, 2, tzinfo=UTC)
    transcript = tmp_path / "legacy.jsonl"
    implementer = _claude_edit_session(
        transcript, IMPLEMENTER, root / test_path, start + timedelta(seconds=10)
    )
    first = json.loads(transcript.read_text())
    records = [first]
    command = f"cd {root} && uv run pytest {test_path} -q"
    records += _claude_shell_records(
        start,
        20,
        "red",
        command,
        f"E   AssertionError\n{test_path}:2: AssertionError\nFAILED {test_path}::test_named",
        1,
    )
    if case == "no-terminal":
        records.pop()
    for offset, relative in ((30, production_path), (70, test_path)):
        record = json.loads(json.dumps(first))
        record["timestamp"] = (start + timedelta(seconds=offset)).isoformat()
        record["message"]["content"][0]["id"] = f"edit-{offset}"
        record["message"]["content"][0]["input"]["file_path"] = str(root / relative)
        records.append(record)
    records += _claude_shell_records(start, 40, "green", command, "1 passed in 0.1s", 0)
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    qa = _claude_edit_session(
        tmp_path / "qa.jsonl", QA, tmp_path / "foreign.py", start + timedelta(hours=1)
    )
    ctx = _context(
        [
            _link(IMPLEMENTER, "claimed", start.isoformat()),
            _link(QA, "claimed", (start + timedelta(hours=1)).isoformat()),
        ],
        {IMPLEMENTER: implementer, QA: qa},
    )
    ctx.session_var_manager.get_variables.return_value = {}
    ctx.task_manager.get_task.return_value = SimpleNamespace(id="task", commits=[commit])
    ctx.session_task_manager.get_session_tasks.return_value = [
        _session_link("task", start.isoformat()),
        _session_link("other", (start + timedelta(seconds=60)).isoformat()),
    ]
    created = start + timedelta(seconds=5)
    if case == "before-window":
        created = start - timedelta(seconds=1)
    elif case == "after-window":
        created = start + timedelta(seconds=61)
    worktree = SimpleNamespace(
        task_id="other" if case == "wrong-task" else "task",
        project_id="other" if case == "project-mismatch" else implementer.project_id,
        machine_id="other" if case == "machine-mismatch" else implementer.machine_id,
        worktree_path=str(tmp_path / "wrong") if case == "root-mismatch" else str(root),
        created_at=created,
    )
    worktrees = [worktree]
    if case == "ambiguous-root":
        worktrees.append(
            SimpleNamespace(**{**vars(worktree), "worktree_path": str(tmp_path / "other")})
        )
    if case == "late-valid":
        outside = SimpleNamespace(**{**vars(worktree), "created_at": start + timedelta(seconds=61)})
        worktrees = [outside] * 50 + [worktree]
    elif case == "late-ambiguous-root":
        other = SimpleNamespace(**{**vars(worktree), "worktree_path": str(tmp_path / "other")})
        worktrees = [worktree] * 50 + [other]

    def list_task_worktrees(*, task_id: str, limit: int | None = 50) -> list[SimpleNamespace]:
        assert task_id == "task"
        return worktrees[:limit]

    ctx.worktree_manager.list_worktrees.side_effect = list_task_worktrees
    with (
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id=QA,
            closing_session_id=QA,
            owner_window_start=(start + timedelta(hours=1)).isoformat(),
            task_edited_files={production_path}
            if case == "partial-owner"
            else {test_path, production_path},
            repo_path=str(root),
        )
    tests = (
        AcceptanceTest(
            reference=f"{test_path}::test_named",
            path=test_path,
            symbol="test_named",
            body="def test_named():\n    assert named()\n",
        ),
    )
    result = evaluate_tdd_evidence(tests, evidence)
    assert result.passed is (case in {"valid", "partial-owner", "late-valid"}), result.findings
    if case in {"valid", "partial-owner", "no-terminal", "late-valid"}:
        assert [(edit.path, edit.timestamp) for edit in evidence.edits] == [
            (test_path, start + timedelta(seconds=10)),
            (production_path, start + timedelta(seconds=30)),
        ]
    else:
        assert not evidence.edits


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

    async def record(
        session: Any, window_start: Any, *args: Any, **kwargs: Any
    ) -> TranscriptEvidence:
        calls.append((session.id, window_start))
        if session.id in unreadable:
            raise TranscriptEvidenceUnavailable(
                "transcript missing", source="unknown", attempted_paths=("/nope",)
            )
        return TranscriptEvidence(sessions=(session.id,))

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
    assert merged == [
        TranscriptEvidence(sessions=(QA,)),
        TranscriptEvidence(sessions=(IMPLEMENTER,)),
    ]


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
    assert merged == [TranscriptEvidence(sessions=(QA,))]


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
    assert merged == [TranscriptEvidence(sessions=(QA,))]


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
async def test_owner_commit_fallback_credits_test_edits_despite_ignored_checkout_ledger(
    tmp_path: Path,
) -> None:
    test_path = "tests/test_named.py"
    named_test = tmp_path / test_path
    named_test.parent.mkdir()
    named_test.write_text("def test_named():\n    assert True\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".gobby/evidence/\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", ".gitignore", test_path),
        ("-c", "user.email=t@t", "-c", "user.name=Test", "commit", "-qm", "task work"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    ctx = _context(
        [_link(IMPLEMENTER, "claimed", start.isoformat())],
        {
            IMPLEMENTER: _claude_edit_session(
                tmp_path / "owner.jsonl",
                IMPLEMENTER,
                named_test,
                start + timedelta(minutes=10),
            )
        },
    )
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {"task": {str(tmp_path): [".gobby/evidence/scratch.md"]}}
    }
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
            owner_session_id=IMPLEMENTER,
            closing_session_id=IMPLEMENTER,
            owner_window_start=start.isoformat(),
            task_edited_files={test_path},
            repo_path=str(tmp_path),
            owner_used_commit_fallback=True,
        )

    assert [(edit.session_id, edit.path) for edit in evidence.edits] == [(IMPLEMENTER, test_path)]
    assert not evidence.degraded_capabilities


def _claude_shell_records(
    start: datetime, offset: int, tool_id: str, command: str, output: str, exit_code: int
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant",
            "timestamp": (start + timedelta(seconds=offset)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": (start + timedelta(seconds=offset + 1)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": {"exit_code": exit_code, "stdout": output},
                    }
                ],
            },
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_task_overlaps", [False, True])
async def test_transferred_task_credits_released_test_history_without_foreign_task_edits(
    tmp_path: Path, other_task_overlaps: bool
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    closing_checkout = tmp_path / "closing"
    test_path = "crates/gclient/tests/host_upgrade_recovery.rs"
    production_path = "crates/gclient/src/recovery.rs"
    foreign_path = "crates/gclient/src/foreign.rs"
    scratch_path = ".gobby/evidence/scratch.md"
    python_tests = ("tests/cli/test_cli_daemon.py", "tests/e2e/test_terminal_client_stack.py")
    names = (
        "pane_reconnects_to_host_without_daemon",
        "host_local_failure_falls_back_to_daemon_attach",
        "host_local_recovery_survives_daemon_attempts",
    )
    tests = tuple(
        AcceptanceTest(
            reference=f"{test_path}::{name}",
            path=test_path,
            symbol=name,
            body=f"#[test]\nfn {name}() {{ assert!(recovered()); }}",
        )
        for name in names
    )
    for relative, content in (
        (test_path, "\n".join(test.body for test in tests)),
        (production_path, "pub fn recovered() -> bool { true }\n"),
        (foreign_path, "pub fn unrelated() {}\n"),
        (".gitignore", ".gobby/evidence/\n"),
        *((path, "def test_example():\n    assert True\n") for path in python_tests),
    ):
        path = primary / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "crates", "tests", ".gitignore"),
        ("-c", "user.email=t@t", "-c", "user.name=Test", "commit", "-qm", "task work"),
        ("worktree", "add", "--detach", str(closing_checkout), "HEAD"),
    ):
        subprocess.run(["git", *args], cwd=primary, check=True, capture_output=True)
    scratch = primary / scratch_path
    scratch.parent.mkdir(parents=True)
    scratch.write_text("temporary evidence\n", encoding="utf-8")

    start = datetime(2026, 9, 30, 2, tzinfo=UTC)
    transcript = tmp_path / "implementer.jsonl"
    implementer = _claude_edit_session(
        transcript, IMPLEMENTER, primary / test_path, start + timedelta(seconds=10)
    )
    records = [json.loads(transcript.read_text(encoding="utf-8"))]
    command = f"cd {primary} && cargo nextest run -p gobby-client --test host_upgrade_recovery"
    records += _claude_shell_records(
        start,
        20,
        "red",
        command,
        f"FAIL [0.01s] gobby-client::host_upgrade_recovery {names[0]}\n"
        f"thread '{names[0]}' panicked at {test_path}:3: assertion failed: recovered()",
        100,
    )
    audit_command = (
        f"cd {primary} && uv run gobby test-types audit {' '.join(python_tests)} "
        "--baseline .gobby/test-types-baseline.json --fail-on-new"
    )
    for offset, relative in ((30, production_path), (50, scratch_path), (60, foreign_path)):
        record = json.loads(json.dumps(records[0]))
        record["timestamp"] = (start + timedelta(seconds=offset)).isoformat()
        record["message"]["content"][0]["id"] = f"edit-{offset}"
        record["message"]["content"][0]["input"]["file_path"] = str(primary / relative)
        records.append(record)
        if offset == 30:
            records += _claude_shell_records(
                start,
                40,
                "green",
                command,
                "\n".join(
                    f"PASS [0.01s] gobby-client::host_upgrade_recovery {name}" for name in names
                ),
                0,
            )
            records += _claude_shell_records(
                start, 44, "audit", audit_command, "Files scanned: 2\nErrors: 0\nNew errors: 0", 0
            )
            records += _claude_shell_records(
                start,
                47,
                "pytest",
                f"cd {primary} && uv run pytest {' '.join(python_tests)} -q",
                "2 passed in 0.1s",
                0,
            )
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    transfer_at = start + timedelta(hours=1)
    qa = _claude_edit_session(
        tmp_path / "qa.jsonl", QA, primary / test_path, transfer_at + timedelta(seconds=10)
    )
    ctx = _context(
        [
            _link(IMPLEMENTER, "claimed", start.isoformat()),
            _link(QA, "claimed", transfer_at.isoformat()),
        ],
        {IMPLEMENTER: implementer, QA: qa},
    )
    ctx.session_var_manager.get_variables.side_effect = {
        IMPLEMENTER: {
            "task_edited_file_checkouts": {
                "task": {str(closing_checkout): [".gobby/evidence/scratch.md"]}
            },
            "task_edited_file_checkouts_history": {
                "task": {str(primary): [test_path, production_path, scratch_path, *python_tests]},
                "other-task": {
                    str(primary): [foreign_path, *([test_path] if other_task_overlaps else [])]
                },
            },
        },
        QA: {"task_edited_file_checkouts": {"other-task": {str(primary): [test_path]}}},
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
            owner_window_start=transfer_at.isoformat(),
            task_edited_files={test_path, production_path, *python_tests},
            repo_path=str(closing_checkout),
            owner_used_commit_fallback=True,
        )

    result = evaluate_tdd_evidence(tests, evidence)
    validation = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        changed_paths=[test_path, production_path, *python_tests],
    )
    assert validation.passed, validation.message
    assert validation.details["latest_test_types_audit"]["command"] == audit_command
    assert result.passed is not other_task_overlaps, result.findings
    assert all(
        edit.session_id == IMPLEMENTER and edit.path != foreign_path for edit in evidence.edits
    )
    assert {edit.path for edit in evidence.edits} == (
        {production_path} if other_task_overlaps else {test_path, production_path}
    )
    if not other_task_overlaps:
        assert result.red_runs == (command,)
        assert result.green_runs == (command,) * len(names)
    else:
        assert result.findings == tuple(
            f"{test.reference}: transcript has no edit of the named test" for test in tests
        )
    assert not evidence.degraded_capabilities


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


async def _implementer_runs_after_claims(
    db_claims: list[dict[str, Any]],
    transcript_claims: tuple[TranscriptTaskClaim, ...],
    runs: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Derive close evidence where QA owns the task and IMPLEMENTER is only linked."""
    ctx = _context(
        [
            _link(IMPLEMENTER, "claimed", "2026-09-28T13:55:00+00:00"),
            _link(QA, "claimed", "2026-09-29T20:00:00+00:00"),
        ],
        {
            IMPLEMENTER: _session(IMPLEMENTER, "2026-09-28T13:00:00+00:00"),
            QA: _session(QA, "2026-09-29T19:00:00+00:00"),
        },
    )
    ctx.session_var_manager.get_variables.return_value = {}
    ctx.session_task_manager.get_session_tasks.side_effect = {
        IMPLEMENTER: db_claims,
        QA: [_session_link("task", "2026-09-29T20:00:00+00:00")],
    }.__getitem__

    async def record(session: Any, *args: Any, **kwargs: Any) -> TranscriptEvidence:
        if session.id == QA:
            return TranscriptEvidence(sessions=(QA,))
        return TranscriptEvidence(
            validation_runs=runs,
            command_runs=runs,
            task_claims=transcript_claims,
            sessions=(IMPLEMENTER,),
        )

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
            owner_window_start="2026-09-29T20:00:00+00:00",
            task_edited_files=set(),
            repo_path="/repo",
        )
    by_session = {evidence.sessions[0]: evidence for evidence in merged}
    assert by_session[IMPLEMENTER].command_runs == by_session[IMPLEMENTER].validation_runs
    return tuple(by_session[IMPLEMENTER].validation_runs)


def _claim(task_ref: str, at: str) -> TranscriptTaskClaim:
    return TranscriptTaskClaim(task_ref=task_ref, claimed_at=datetime.fromisoformat(at))


@pytest.mark.asyncio
async def test_linked_session_runs_after_returning_to_the_task_are_credited() -> None:
    """#23385: a reclaim writes no new session_tasks row, so only the transcript shows it."""
    before = _run_at(IMPLEMENTER, "2026-09-28T14:30:00+00:00", "success")
    away = _run_at(IMPLEMENTER, "2026-09-29T03:00:00+00:00", "failure")
    red = _run_at(IMPLEMENTER, "2026-09-29T06:00:00+00:00", "failure")
    green = _run_at(IMPLEMENTER, "2026-09-29T06:30:00+00:00", "success")

    credited = await _implementer_runs_after_claims(
        [
            _session_link("other-task", "2026-09-29T02:14:00+00:00"),
            _session_link("task", "2026-09-28T13:55:00+00:00"),
        ],
        (
            _claim("other-task", "2026-09-29T02:14:00+00:00"),
            _claim("task", "2026-09-29T05:00:00+00:00"),
        ),
        (before, away, red, green),
    )

    assert credited == (before, red, green)


@pytest.mark.asyncio
async def test_linked_session_runs_after_returning_to_an_earlier_task_are_not_credited() -> None:
    """#23385: the other task's first claim predates this one, so its row shows no departure."""
    before = _run_at(IMPLEMENTER, "2026-09-28T14:30:00+00:00", "success")
    away = _run_at(IMPLEMENTER, "2026-09-29T03:00:00+00:00", "failure")

    credited = await _implementer_runs_after_claims(
        [
            _session_link("task", "2026-09-28T13:55:00+00:00"),
            _session_link("other-task", "2026-09-28T12:00:00+00:00"),
        ],
        (_claim("other-task", "2026-09-29T02:14:00+00:00"),),
        (before, away),
    )

    assert credited == (before,)
