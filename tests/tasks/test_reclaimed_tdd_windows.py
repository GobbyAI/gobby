"""Reclaim attribution through real provider parsing and Gate 12."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import derive_close_transcript_evidence
from gobby.storage.session_models import Session
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.transcript_evidence_models import TranscriptEdit, TranscriptEvidence
from gobby.utils.machine_id import require_machine_id
from tests.tasks.test_tdd_cycle_continuity import (
    START,
    SUPPORT,
    TEST_PATH,
    _edit,
    _pair,
    _run,
    _test,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["same-task", "escalated", "other-task-before", "other-task-between"]
)
async def test_reclaim_credits_only_edits_selected_for_same_task(tmp_path: Path, case: str) -> None:
    credited = case in {"same-task", "escalated"}
    root = str(tmp_path.resolve())
    source = "src/feature.py"
    paths = {TEST_PATH, source}
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    records = [
        *_pair(2, "Write", {"file_path": str(tmp_path / TEST_PATH), "content": "test"}, "ok"),
        *_pair(
            3,
            "Bash",
            {"command": f"uv run pytest {TEST_PATH} -q", "cwd": root},
            "________________ test_feature ________________\nE   assert 1 == 2\n"
            "tests/test_feature.py:4: AssertionError\n"
            "FAILED tests/test_feature.py::test_feature - assert 1 == 2\n",
        ),
        *_pair(4, "Write", {"file_path": str(tmp_path / source), "content": "code"}, "ok"),
        *_pair(
            12,
            "Bash",
            {"command": f"uv run pytest {TEST_PATH} -q", "cwd": root},
            "1 passed in 0.01s",
        ),
    ]
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    session = Session(
        id="owner",
        external_id="reclaim-owner",
        machine_id=require_machine_id(),
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )
    # Legacy checkout history survives, but its per-path times have been discarded.
    variables: dict[str, Any] = {
        "task_edited_file_checkouts_history": {
            "task": {root: sorted(paths), str(tmp_path / "foreign"): sorted(paths)}
        },
        "task_edited_file_checkouts_history_started_at": START.timestamp(),
        "task_selection_history": [
            {
                "epoch": START.isoformat(),
                "task_id": "other" if case == "other-task-before" else "task",
            },
            {
                "epoch": (
                    START + timedelta(seconds=1 if case == "other-task-between" else 6)
                ).isoformat(),
                "task_id": None if case == "escalated" else "other",
            },
            {"epoch": (START + timedelta(seconds=10)).isoformat(), "task_id": "task"},
        ],
    }
    if not credited:
        # RED and implementation belong to A: rejecting this cycle must depend
        # on the earlier test edit belonging to B, rather than an off-task RED.
        variables["task_selection_history"].insert(
            1 if case == "other-task-before" else 2,
            {"epoch": (START + timedelta(seconds=2.5)).isoformat(), "task_id": "task"},
        )
    ctx = MagicMock()
    ctx.config = None
    ctx.session_manager.get.return_value = session
    ctx.session_var_manager.get_variables.return_value = variables
    ctx.session_task_manager.get_task_sessions.return_value = []
    ctx.session_task_manager.get_session_tasks.return_value = [
        {
            "task": SimpleNamespace(id="task", closed_at=None),
            "action": "claimed",
            "link_created_at": (START + timedelta(seconds=10)).isoformat(),
        }
    ]
    if case == "escalated":
        ctx.session_task_manager.get_session_tasks.return_value.insert(
            0,
            {
                "task": SimpleNamespace(id="task", closed_at=None),
                "action": "escalated",
                "link_created_at": (START + timedelta(seconds=6)).isoformat(),
            },
        )
    with (
        patch(f"{SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
        patch(f"{SUPPORT}.derive_transcript_evidence", wraps=derive_transcript_evidence) as parse,
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="owner",
            closing_session_id="owner",
            owner_window_start=(START + timedelta(seconds=10)).isoformat(),
            task_edited_files=set(),
            repo_path=root,
        )
    result = evaluate_tdd_evidence((_test(),), evidence)
    assert result.passed is credited, result.findings
    if credited:
        assert result.red_runs == (f"uv run pytest {TEST_PATH} -q",)
        assert result.green_runs == (f"uv run pytest {TEST_PATH} -q",)
        assert {edit.path for edit in evidence.edits} == paths
    else:
        assert result.red_runs == ()
        if case == "other-task-between":
            assert any(
                edit.path == TEST_PATH and not edit.task_attributed for edit in evidence.edits
            )
    assert parse.await_count == 1


@pytest.mark.parametrize("away_edit", ["implementation-before-test", "placeholder-before-red"])
def test_off_task_edits_remain_visible_to_red_safety_checks(away_edit: str) -> None:
    source = "from feature import run\n\ndef test_feature():\n    assert run() == 2\n"
    test = replace(_test(), body=source)
    test_edit = replace(
        _edit(TEST_PATH, 2),
        source_after=source,
        source_confirmed=True,
        source_confirmed_at=START + timedelta(seconds=2, milliseconds=100),
    )
    production = _edit("src/feature.py", 6)
    red = _run(5, red=True)
    green = replace(_run(9, red=False), session_id=red.session_id)
    physical_edits: tuple[TranscriptEdit, ...]
    if away_edit == "implementation-before-test":
        physical_edits = (
            replace(_edit("src/feature.py", 1), task_attributed=False),
            test_edit,
            production,
        )
        rejection = "post-production red has no attributable NotImplementedError or proven API stub"
    else:
        placeholder = "import pytest\n\ndef test_feature():\n    pytest.fail('forced red')\n"
        physical_edits = (
            test_edit,
            replace(
                test_edit,
                order=4,
                timestamp=START + timedelta(seconds=4),
                source_after=placeholder,
                source_confirmed_at=START + timedelta(seconds=4, milliseconds=100),
                task_attributed=False,
            ),
            production,
            replace(
                test_edit,
                order=7,
                timestamp=START + timedelta(seconds=7),
                source_confirmed_at=START + timedelta(seconds=7, milliseconds=100),
                task_attributed=False,
            ),
        )
        red = replace(
            red,
            output="________________ test_feature ________________\n"
            "E   Failed: forced red\n"
            "tests/test_feature.py:4: Failed\n"
            "FAILED tests/test_feature.py::test_feature - Failed: forced red\n",
        )
        rejection = "test body is an unconditional pytest.fail placeholder"
    evidence = TranscriptEvidence(edits=physical_edits, validation_runs=(red, green))

    result = evaluate_tdd_evidence((test,), evidence)

    assert not result.passed, result
    assert result.red_runs == ()
    assert any(rejection in finding for finding in result.findings), result.findings
    # The same physical history must be rejected when all its edits are on-task.
    all_on_task = replace(
        evidence, edits=tuple(replace(edit, task_attributed=True) for edit in physical_edits)
    )
    assert not evaluate_tdd_evidence((test,), all_on_task).passed
