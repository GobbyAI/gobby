"""Production writes wait for every named behavior test's own retained RED."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.session_models import Session
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.tdd_write_authorization import (
    TddObligation,
    WriteAuthorization,
    authorize_production_write,
    authorize_session_write,
    task_tdd_obligations,
)
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


def test_claim_switch_during_run_cannot_authorize_write() -> None:
    obligations = task_tdd_obligations(TASK, FEATURE_CRITERION)
    run = replace(_red(2, FEATURE, "test_feature"), completed_at=START + timedelta(seconds=5))
    evidence = TranscriptEvidence(
        edits=(_edit(FEATURE, 1),),
        validation_runs=(run,),
        task_claims=(_claim(TASK, 0), _claim(FOREIGN, 3), _claim(TASK, 4)),
    )

    refused = _authorize(obligations, evidence)

    assert (refused.allowed, refused.missing) == (False, (FEATURE_REF,))


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


@pytest.mark.parametrize("test_writer", ["Write", "apply_patch", "functions.exec"])
def test_compaction_preserves_authenticated_original_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_writer: str
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    original_source = "def test_feature():\n    assert 0\n"
    (repo / FEATURE).write_text(original_source, encoding="utf-8")
    red = _red(2, FEATURE, "test_feature")
    claimed = json.dumps({"success": True, "result": {"success": True, "task_id": TASK}})
    write_arguments = (
        {"file_path": str(repo / FEATURE), "content": original_source}
        if test_writer == "Write"
        else {
            "raw": f"*** Begin Patch\n*** Add File: {repo / FEATURE}\n"
            "+def test_feature():\n+    assert 0\n*** End Patch\n"
        }
    )
    if test_writer == "functions.exec":
        write_arguments = {
            "raw": f"text(await tools.apply_patch({json.dumps(write_arguments['raw'])}));"
        }
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
            test_writer,
            write_arguments,
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
    (repo / FEATURE).write_text("def test_feature():\n    assert 1\n", encoding="utf-8")
    changed = authorize_session_write(
        session, obligations, task_refs=TASK_REFS, repo_path=str(repo), archive_dir=str(archives)
    )
    (repo / FEATURE).unlink()
    missing = authorize_session_write(
        session, obligations, task_refs=TASK_REFS, repo_path=str(repo), archive_dir=str(archives)
    )
    assert (changed.allowed, changed.missing) == (False, (FEATURE_REF,))
    assert (missing.allowed, missing.missing) == (False, (FEATURE_REF,))


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


def test_rust_inline_named_red_authorizes_only_its_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    path = "src/lib.rs"
    source = (
        "#[cfg(test)]\nmod tests {\n    #[test]\n    fn test_feature() { assert_eq!(1, 2); }\n}\n"
    )
    (repo / path).write_text(source, encoding="utf-8")
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8"
    )
    claim = json.dumps({"success": True, "result": {"success": True, "task_id": TASK}})
    output = (
        "running 1 test\ntest tests::test_feature ... FAILED\n\nfailures:\n"
        "---- tests::test_feature stdout ----\n"
        "thread 'tests::test_feature' panicked at src/lib.rs:4:25:\n"
        "assertion `left == right` failed\n  left: 1\n right: 2\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored\n"
    )
    records = [
        *_pair(
            repo,
            0,
            "mcp__gobby__call_tool",
            {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "#24746"},
            },
            [{"type": "text", "text": claim}],
        ),
        *_pair(repo, 1, "Write", {"file_path": str(repo / path), "content": source}, ""),
        *_pair(
            repo,
            2,
            "Bash",
            {"command": "cargo test -p demo --lib tests::test_feature -- --exact"},
            output,
        ),
    ]
    transcript = tmp_path / "rust.jsonl"
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    session = Session(
        id="rust-writer",
        external_id="rust-writer",
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
    obligations = task_tdd_obligations(TASK, f"1. The feature works; test: {path}::test_feature")
    own = authorize_session_write(session, obligations, task_refs=TASK_REFS, repo_path=str(repo))
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    (other / path).write_text(source, encoding="utf-8")
    (other / "Cargo.toml").write_text((repo / "Cargo.toml").read_text(), encoding="utf-8")
    foreign = authorize_session_write(
        session, obligations, task_refs=TASK_REFS, repo_path=str(other)
    )
    assert own.allowed, own.missing
    assert foreign.allowed is False
    assert foreign.missing == (f"{path}::test_feature",)
