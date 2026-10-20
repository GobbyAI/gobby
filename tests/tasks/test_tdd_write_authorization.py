"""Production writes wait for every named behavior test's own retained RED."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gobby.tasks.tdd_write_authorization import (
    TddObligation,
    WriteAuthorization,
    authorize_production_write,
    authorize_session_write,
    task_tdd_obligations,
)

from gobby.storage.session_models import Session
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptTaskClaim,
    TranscriptValidationRun,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 10, 14, tzinfo=UTC)
TASK = "0b6f2d51-4a4e-4c55-9d1e-246746000002"
FOREIGN = "7df89483-8c49-4277-b491-ffb1a1bf4c55"
TASK_REFS = frozenset({TASK})
FEATURE = "tests/test_feature.py"
OTHER = "tests/test_other.py"
KEPT = "tests/test_kept.py"
FEATURE_REF = f"{FEATURE}::test_feature"
OTHER_REF = f"{OTHER}::test_other"
KEPT_REF = f"{KEPT}::test_kept"
FEATURE_CRITERION = f"1. The feature returns two; test: {FEATURE_REF}"
COLLECTION_ERROR = (
    "==================================== ERRORS ====================================\n"
    "____________________ ERROR collecting tests/test_feature.py ____________________\n"
    "ImportError while importing test module '/repo/tests/test_feature.py'.\n"
    "E   ModuleNotFoundError: No module named 'feature'\n"
    "=========================== short test summary info ============================\n"
    "ERROR tests/test_feature.py\n"
    "1 error in 0.05s"
)


def _claim(task_ref: str, order: int) -> TranscriptTaskClaim:
    return TranscriptTaskClaim(task_ref=task_ref, claimed_at=START + timedelta(seconds=order))


def _edit(path: str, order: int) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="writer",
        source="claude",
        path=path,
        timestamp=START + timedelta(seconds=order),
        order=order,
        tool_name="Write",
    )


def _run(
    order: int,
    output: str,
    *,
    path: str = FEATURE,
    passed: bool = False,
    command: str | None = None,
) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id="writer",
        source="claude",
        command=command or f"uv run pytest {path} -q",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="success" if passed else "failure",
        started_at=START + timedelta(seconds=order),
        completed_at=START + timedelta(seconds=order, milliseconds=500),
        order=order,
        exit_code=0 if passed else 1,
        output=output,
    )


def _red(
    order: int, path: str, symbol: str, *, command: str | None = None
) -> TranscriptValidationRun:
    return _run(
        order,
        f"________________ {symbol} ________________\n"
        "E   assert 1 == 2\n"
        f"{path}:2: AssertionError\n"
        f"FAILED {path}::{symbol} - assert 1 == 2\n"
        "1 failed in 0.02s",
        path=path,
        command=command,
    )


def _authorize(
    obligations: tuple[TddObligation, ...], evidence: TranscriptEvidence
) -> WriteAuthorization:
    return authorize_production_write(obligations, evidence, task_refs=TASK_REFS)


def _pair(
    cwd: Path, step: int, name: str, arguments: dict[str, Any], result: object
) -> list[dict[str, Any]]:
    call = f"call-{step}"
    at = START + timedelta(seconds=step)
    return [
        {
            "type": "assistant",
            "timestamp": at.isoformat(),
            "cwd": str(cwd),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call, "name": name, "input": arguments}],
            },
        },
        {
            "type": "user",
            "timestamp": (at + timedelta(milliseconds=500)).isoformat(),
            "cwd": str(cwd),
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call, "content": result}],
            },
        },
    ]


def test_unwritten_named_behavior_test_blocks_other_criterion() -> None:
    obligations = task_tdd_obligations(
        TASK, f"{FEATURE_CRITERION}\n2. The other path agrees; test: {OTHER_REF}"
    )
    red = _red(2, FEATURE, "test_feature")
    evidence = TranscriptEvidence(
        edits=(_edit(FEATURE, 1),), validation_runs=(red,), task_claims=(_claim(TASK, 0),)
    )

    authorization = _authorize(obligations, evidence)

    assert [obligation.test.reference for obligation in obligations] == [FEATURE_REF, OTHER_REF]
    assert authorization.allowed is False
    assert authorization.missing == (OTHER_REF,)
    assert dict(authorization.red_runs) == {FEATURE_REF: red.command}


def test_foreign_claim_red_cannot_authorize_write() -> None:
    obligations = task_tdd_obligations(TASK, FEATURE_CRITERION)
    under_foreign_claim = TranscriptEvidence(
        edits=(_edit(FEATURE, 1),),
        validation_runs=(_red(3, FEATURE, "test_feature"),),
        task_claims=(_claim(TASK, 0), _claim(FOREIGN, 2)),
    )
    under_own_claim = replace(
        under_foreign_claim, task_claims=(_claim(TASK, 0), _claim(FOREIGN, 4))
    )

    refused = _authorize(obligations, under_foreign_claim)
    allowed = _authorize(obligations, under_own_claim)

    assert (refused.allowed, refused.missing) == (False, (FEATURE_REF,))
    assert (allowed.allowed, allowed.missing) == (True, ())


def test_test_edit_invalidates_named_red() -> None:
    obligations = task_tdd_obligations(TASK, FEATURE_CRITERION)
    first = _red(2, FEATURE, "test_feature")
    rerun = _red(4, FEATURE, "test_feature", command=f"uv run pytest {FEATURE_REF} -q")
    edited_after_red = TranscriptEvidence(
        edits=(_edit(FEATURE, 1), _edit(FEATURE, 3)),
        validation_runs=(first,),
        task_claims=(_claim(TASK, 0),),
    )

    stale = _authorize(obligations, edited_after_red)
    fresh = _authorize(obligations, replace(edited_after_red, validation_runs=(first, rerun)))

    assert (stale.allowed, stale.missing) == (False, (FEATURE_REF,))
    assert fresh.allowed is True
    assert dict(fresh.red_runs) == {FEATURE_REF: rerun.command}


def test_compaction_preserves_authenticated_original_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    red = _red(2, FEATURE, "test_feature")
    claimed = json.dumps({"success": True, "result": {"success": True, "task_id": TASK}})
    before_compaction = [
        *_pair(
            repo,
            0,
            "mcp__gobby__call_tool",
            {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "#24746"},
            },
            [{"type": "text", "text": claimed}],
        ),
        *_pair(
            repo,
            1,
            "Write",
            {"file_path": str(repo / FEATURE), "content": "def test_feature():\n    assert 0\n"},
            "",
        ),
        *_pair(repo, 2, "Bash", {"command": red.command}, red.output),
    ]
    transcript = tmp_path / "current.jsonl"
    current = _pair(repo, 3, "Bash", {"command": "git status --short"}, "")
    transcript.write_text("".join(json.dumps(row) + "\n" for row in current), encoding="utf-8")
    archives = tmp_path / "archives"
    archives.mkdir()
    with gzip.open(archives / "writer.jsonl.gz", "wt", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in before_compaction))
    no_archives = tmp_path / "no-archives"
    no_archives.mkdir()
    session = Session(
        id="writer",
        external_id="writer",
        machine_id="isolated",
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
    obligations = task_tdd_obligations(TASK, FEATURE_CRITERION)

    preserved = authorize_session_write(
        session, obligations, task_refs=TASK_REFS, repo_path=str(repo), archive_dir=str(archives)
    )
    current_only = authorize_session_write(
        session, obligations, task_refs=TASK_REFS, repo_path=str(repo), archive_dir=str(no_archives)
    )

    assert (preserved.allowed, dict(preserved.red_runs)) == (True, {FEATURE_REF: red.command})
    assert (current_only.allowed, current_only.missing) == (False, (FEATURE_REF,))


@pytest.mark.parametrize(
    "output", [COLLECTION_ERROR, "1 failed in 0.02s"], ids=["collection-error", "bare-count"]
)
def test_compile_failure_cannot_authorize_write(output: str) -> None:
    obligations = task_tdd_obligations(TASK, FEATURE_CRITERION)
    evidence = TranscriptEvidence(
        edits=(_edit(FEATURE, 1),),
        validation_runs=(_run(2, output),),
        task_claims=(_claim(TASK, 0),),
    )

    authorization = _authorize(obligations, evidence)

    assert (authorization.allowed, authorization.missing) == (False, (FEATURE_REF,))
    assert dict(authorization.red_runs) == {}


def test_unchanged_explicit_regression_needs_only_green() -> None:
    designated = task_tdd_obligations(
        TASK, f"{FEATURE_CRITERION}\n2. Regression-only: the kept path holds; test: {KEPT_REF}"
    )
    undesignated = task_tdd_obligations(
        TASK, f"{FEATURE_CRITERION}\n2. The kept path holds; test: {KEPT_REF}"
    )
    prepared = TranscriptEvidence(
        edits=(_edit(FEATURE, 1),),
        validation_runs=(_red(2, FEATURE, "test_feature"),),
        task_claims=(_claim(TASK, 0),),
    )
    kept_edited = replace(prepared, edits=(*prepared.edits, _edit(KEPT, 3)))
    closed = replace(
        prepared,
        edits=(*prepared.edits, _edit("src/feature.py", 3)),
        validation_runs=(
            *prepared.validation_runs,
            _run(4, "1 passed in 0.01s", passed=True),
            _run(5, "1 passed in 0.01s", path=KEPT, passed=True),
        ),
    )

    close = evaluate_tdd_evidence(
        tuple(obligation.test for obligation in designated),
        closed,
        implementation_paths=(FEATURE, "src/feature.py"),
        regression_only=tuple(
            obligation.test.reference for obligation in designated if obligation.regression_only
        ),
    )

    assert [obligation.regression_only for obligation in designated] == [False, True]
    assert _authorize(designated, prepared).allowed is True
    assert _authorize(undesignated, prepared).missing == (KEPT_REF,)
    assert _authorize(designated, kept_edited).missing == (KEPT_REF,)
    assert close.passed, close.findings
    assert close.regression_tests == (KEPT_REF,)
