"""Gate 12 credits an authentic retained RED output bound to its original job."""

import gzip
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.integrations.rtk import platform_paths
from gobby.storage.session_models import Session
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import TddEvidenceResult, evaluate_tdd_evidence
from gobby.tasks.transcript_evidence import _derive_transcript_path_evidence
from gobby.tasks.transcript_evidence_models import TranscriptEvidence

pytestmark = pytest.mark.unit

START = datetime(2026, 10, 14, tzinfo=UTC)
RUST_TEST = "crates/feature/tests/golden.rs"
RUST_CODE = "crates/feature/src/lib.rs"
NEXTEST = "cargo nextest run -p feature --test golden"
PY_TEST = "tests/test_feature.py"
PY_CODE = "src/feature.py"
PYTEST = f"uv run pytest {PY_TEST} -v"
RECALL = "0123456789ab"
NOTICE = 3.9


def _block(name: str, index: int, panic: str | None) -> str:
    stderr = (
        f"    thread '{name}' ({index}) panicked at {RUST_TEST}:{index}:5:\n    {panic}\n"
        if panic
        else ""
    )
    return (
        f"        FAIL [   0.01{index}s] ({index}/3) feature::golden {name}\n"
        "  stdout ───\n\n    running 1 test\n"
        f"    test {name} ... FAILED\n\n"
        "    test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 2 filtered out\n\n"
        f"  stderr ───\n\n{stderr}\n"
    )


def _nextest_file(target_panic: str | None, *, exit_line: str = "[exited with code 100]") -> str:
    names = ("sibling_before", "replays_corpus", "sibling_after")
    panics = ("sibling detail", target_panic, "assertion `left == right` failed")
    body = "".join(
        _block(name, index + 1, panic)
        for index, (name, panic) in enumerate(zip(names, panics, strict=True))
    )
    summary = "".join(
        f"        FAIL [   0.01{index + 1}s] ({index + 1}/3) feature::golden {name}\n"
        for index, name in enumerate(names)
    )
    return (
        "   Compiling feature v0.1.0\n    Starting 3 tests across 1 binary\n"
        f"{body}────\n     Summary [   0.013s] 3 tests run: 0 passed, 3 failed\n"
        f"{summary}error: test run failed\n\n{exit_line}\n"
    )


def _monitor_lines(text: str) -> str:
    keep = ("Starting", "FAIL [", "Summary")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip().startswith(keep))


def _pytest_raw(*, failed: int = 1) -> str:
    return (
        "============================= test session starts ==============================\n"
        f"collecting ... collected {failed + 1} items\n\n"
        f"{PY_TEST}::test_feature FAILED [ 50%]\n{PY_TEST}::test_other PASSED [100%]\n\n"
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_feature _________________________________\n\n"
        "    def test_feature() -> None:\n>       assert feature() == 2\nE       assert 1 == 2\n\n"
        f"{PY_TEST}:4: AssertionError\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {PY_TEST}::test_feature - assert 1 == 2\n"
        f"========================= {failed} failed, 1 passed in 0.50s =========================\n"
    )


def _compacted(*, failed: int = 1, recall: str = RECALL) -> str:
    return (
        f"Exit code 1\n{PY_TEST}::test_feature FAILED [ 50%]\n\n"
        f"========================= {failed} failed, 1 passed in 0.50s =========================\n"
        f"[full output: rtk recall {recall}]"
    )


def _pair(
    at: float, name: str, arguments: dict[str, Any], result: str, *, error: bool = False
) -> list[dict[str, Any]]:
    call = f"call-{at:g}"
    return [
        {
            "type": "assistant",
            "timestamp": (START + timedelta(seconds=at)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call, "name": name, "input": arguments}],
            },
        },
        {
            "type": "user",
            "timestamp": (START + timedelta(seconds=at + 0.5)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call,
                        "content": result,
                        "is_error": error,
                    }
                ],
            },
        },
    ]


def _write(tmp_path: Path, at: float, path: str) -> list[dict[str, Any]]:
    return _pair(at, "Write", {"file_path": str(tmp_path / path), "content": "x = 1\n"}, "")


def _queued(at: float, prompt: str) -> dict[str, Any]:
    return {
        "type": "attachment",
        "timestamp": (START + timedelta(seconds=at)).isoformat(),
        "attachment": {"type": "queued_command", "prompt": prompt},
    }


def _job(
    output_path: Path, *, monitor: bool = True, monitor_at: float = 3.2, events: str | None = None
) -> list[dict[str, Any]]:
    records = _pair(
        2,
        "Bash",
        {"command": NEXTEST, "run_in_background": True, "description": "RED tests"},
        "Command did not complete within its 600s timeout and was moved to the "
        f"background (ID: job). Output is being written to: {output_path}. "
        "You will be notified when it completes.",
    )
    # Claude records the job id beside the receipt, so the delivered output repeats it.
    records[1]["toolUseResult"] = {"stdout": "", "stderr": "", "backgroundTaskId": "job"}
    if monitor:
        records += _pair(
            monitor_at - 0.6,
            "Monitor",
            {
                "command": f"tail -n +1 -f {output_path} | "
                'grep -E --line-buffered "Starting|FAIL|Summary"'
            },
            "Monitor started (task monitor, expires in 30m unless the source ends first).",
        )
    records.append(
        {
            "type": "user",
            "timestamp": (START + timedelta(seconds=NOTICE)).isoformat(),
            "message": {
                "role": "user",
                "content": "<task-notification><task-id>job</task-id>"
                f"<tool-use-id>call-2</tool-use-id><output-file>{output_path}</output-file>"
                "<status>failed</status>"
                '<summary>Background command "RED tests" failed with exit code 100</summary>'
                "</task-notification>",
            },
        }
    )
    if monitor:
        lines = events or _monitor_lines(_nextest_file("x"))
        records.append(
            _queued(
                monitor_at,
                "<task-notification><task-id>monitor</task-id>"
                f'<summary>Monitor event: "tests"</summary><event>{lines}</event>'
                "</task-notification>",
            )
        )
    return sorted(records, key=lambda record: str(record["timestamp"]))


def _session(transcript: Path) -> Session:
    return Session(
        id="earlier",
        external_id="fixture",
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


def _evidence(tmp_path: Path, records: list[dict[str, Any]], files: set[str]) -> TranscriptEvidence:
    transcript = tmp_path / "claude.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    evidence, _ = _derive_transcript_path_evidence(
        _session(transcript),
        str(transcript),
        START,
        default_validation_detection_config(),
        files,
        str(tmp_path),
        frozenset((str(tmp_path), path) for path in files),
        [str(transcript)],
        resume=None,
    )
    return evidence


def _retain(path: Path, text: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = (START + timedelta(seconds=mtime)).timestamp()
    os.utime(path, (stamp, stamp))


def _recall_store(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str | None,
    *,
    created: float = 2.5,
    recall: str = RECALL,
) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if text is None:
        return
    database = platform_paths().data_dir / "recall.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE recall (hash TEXT PRIMARY KEY, command TEXT NOT NULL, cwd TEXT,"
            " exit_code INTEGER, created_at INTEGER NOT NULL, total_lines INTEGER NOT NULL,"
            " shown_upto INTEGER NOT NULL, byte_size INTEGER NOT NULL, truncated INTEGER NOT NULL,"
            " codec TEXT NOT NULL, blob BLOB NOT NULL, recalled INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO recall VALUES (?, 'uv', NULL, 1, ?, ?, 1, ?, 0, 'gzip', ?, 0)",
            (
                recall,
                int((START + timedelta(seconds=created)).timestamp()),
                text.count("\n"),
                len(text),
                gzip.compress(text.encode()),
            ),
        )


def _rust_result(tmp_path: Path, records: list[dict[str, Any]]) -> TddEvidenceResult:
    evidence = _evidence(tmp_path, records, {RUST_TEST, RUST_CODE})
    return evaluate_tdd_evidence(
        (AcceptanceTest(f"{RUST_TEST}::replays_corpus", RUST_TEST, "replays_corpus", ""),),
        evidence,
    )


def _rust_cycle(
    tmp_path: Path,
    *,
    production_at: float = 10,
    job_file: str = "tasks/job.output",
    stub_at: float | None = None,
) -> list[dict[str, Any]]:
    records = _write(tmp_path, 1, RUST_TEST)
    if stub_at is not None:
        records += _write(tmp_path, stub_at, RUST_CODE)
    records += _job(tmp_path / job_file)
    records += _write(tmp_path, production_at, RUST_CODE)
    records += _pair(
        12,
        "Bash",
        {"command": NEXTEST},
        "PASS [ 0.001s] (1/1) feature::golden replays_corpus\nSummary 1 tests run: 1 passed",
    )
    return sorted(records, key=lambda record: str(record["timestamp"]))


def _python_result(tmp_path: Path, *, production_at: float = 10) -> TddEvidenceResult:
    records = _write(tmp_path, 1, PY_TEST)
    records += _pair(2, "Bash", {"command": PYTEST}, _compacted(), error=True)
    records += _write(tmp_path, production_at, PY_CODE)
    records += _pair(12, "Bash", {"command": PYTEST}, "2 passed in 0.40s")
    evidence = _evidence(tmp_path, records, {PY_TEST, PY_CODE})
    return evaluate_tdd_evidence(
        (AcceptanceTest(f"{PY_TEST}::test_feature", PY_TEST, "test_feature", ""),), evidence
    )


def _assert_uncredited(result: TddEvidenceResult) -> None:
    assert not result.passed
    assert result.red_runs == ()
    assert any("no attributable failure section" in finding for finding in result.findings), (
        result.findings
    )


@pytest.mark.parametrize("form", ["job_output_file", "rtk_recall_artifact"])
def test_retained_output_credits_node_with_own_failure_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    if form == "job_output_file":
        _retain(tmp_path / "tasks/job.output", _nextest_file("expected 2"), NOTICE + 0.3)
        result = _rust_result(tmp_path, _rust_cycle(tmp_path))
        command = NEXTEST
    else:
        _recall_store(tmp_path / "home", monkeypatch, _pytest_raw())
        result = _python_result(tmp_path)
        command = PYTEST
    assert result.passed, result.findings
    assert result.red_runs == (command,)


@pytest.mark.parametrize(
    "case", ["missing", "modified_late", "after_production", "unnamed_path", "content_mismatch"]
)
def test_retained_output_rejected_when_provenance_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    job_text = _nextest_file("expected 2")
    if case == "content_mismatch":
        job_text = job_text.replace("Starting 3 tests", "Starting 4 tests")
    job_name = "tasks/other.output" if case == "unnamed_path" else "tasks/job.output"
    mtime = {"modified_late": NOTICE + 1.5, "after_production": NOTICE + 0.3}.get(case, NOTICE)
    if case != "missing":
        _retain(tmp_path / job_name, job_text, mtime)
    production_at = NOTICE + 0.2 if case == "after_production" else 10
    _assert_uncredited(_rust_result(tmp_path, _rust_cycle(tmp_path, production_at=production_at)))

    _recall_store(
        tmp_path / "home",
        monkeypatch,
        None if case == "missing" else _pytest_raw(failed=2 if case == "content_mismatch" else 1),
        created={"modified_late": 4.0, "after_production": 3.0}.get(case, 2.5),
        recall="ba9876543210" if case == "unnamed_path" else RECALL,
    )
    _assert_uncredited(
        _python_result(tmp_path / "py", production_at=3.0 if case == "after_production" else 10)
    )


@pytest.mark.parametrize(("production_at", "credited"), [(10, True), (NOTICE + 0.2, False)])
def test_stub_first_retained_output_is_bounded_by_its_window(
    tmp_path: Path, production_at: float, credited: bool
) -> None:
    # A compile stub precedes the RED job, so the edit that closes its window bounds the file.
    _retain(tmp_path / "tasks/job.output", _nextest_file("expected 2"), NOTICE + 0.3)
    records = _rust_cycle(tmp_path, production_at=production_at, stub_at=1.5)
    assert _rust_result(tmp_path, records).passed is credited


def test_red_after_the_closing_edit_earns_no_credit(tmp_path: Path) -> None:
    # The only production edit precedes the job, so no window closes after this RED.
    _retain(tmp_path / "tasks/job.output", _nextest_file("expected 2"), NOTICE + 0.3)
    result = _rust_result(tmp_path, _rust_cycle(tmp_path, production_at=1.5))
    assert not result.passed
    assert result.red_runs == ()


@pytest.mark.parametrize("job_file", ["notes/job.output", "tasks/other.output"])
def test_job_output_must_be_the_jobs_own_tasks_file(tmp_path: Path, job_file: str) -> None:
    # The receipt names this path, but only tasks/<job_id>.output is the job's own file.
    _retain(tmp_path / job_file, _nextest_file("expected 2"), NOTICE + 0.3)
    _assert_uncredited(_rust_result(tmp_path, _rust_cycle(tmp_path, job_file=job_file)))


@pytest.mark.parametrize("retained", ["summary_only", "sibling_body"])
def test_retained_output_rejects_summary_only_and_sibling_body(
    tmp_path: Path, retained: str
) -> None:
    text = _nextest_file(None)
    if retained == "summary_only":
        kept = ("Starting", "FAIL [", "Summary", "[exited")
        text = "\n".join(line for line in text.splitlines() if line.strip().startswith(kept))
    _retain(tmp_path / "tasks/job.output", text + "\n", NOTICE + 0.3)
    _assert_uncredited(_rust_result(tmp_path, _rust_cycle(tmp_path)))


@pytest.mark.parametrize("later", ["read", "tail", "monitor", "rerun"])
def test_later_read_or_rerun_is_not_red(tmp_path: Path, later: str) -> None:
    output_path = tmp_path / "tasks/job.output"
    full = _nextest_file("expected 2")
    _retain(output_path, full, NOTICE + 0.3)
    records = _write(tmp_path, 1, RUST_TEST)
    events = "\n".join(line.strip() for line in full.splitlines() if line.strip())
    records += _job(output_path, monitor=later == "monitor", monitor_at=11.5, events=events)
    records += _write(tmp_path, 10, RUST_CODE)
    if later == "read":
        records += _pair(11, "Read", {"file_path": str(output_path)}, full)
    elif later == "tail":
        records += _pair(11, "Bash", {"command": f"tail -n 40 {output_path}"}, full)
    elif later == "rerun":
        records += _pair(11, "Bash", {"command": NEXTEST}, "Exit code 100\n" + full, error=True)
    records += _pair(
        14,
        "Bash",
        {"command": NEXTEST},
        "PASS [ 0.001s] (1/1) feature::golden replays_corpus\nSummary 1 tests run: 1 passed",
    )
    records.sort(key=lambda record: str(record["timestamp"]))
    result = _rust_result(tmp_path, records)
    assert not result.passed
    assert result.red_runs == ()


@pytest.mark.parametrize(("mtime", "credited"), [(NOTICE + 0.9, True), (NOTICE + 1.1, False)])
def test_job_output_flush_grace_is_one_second(tmp_path: Path, mtime: float, credited: bool) -> None:
    _retain(tmp_path / "tasks/job.output", _nextest_file("expected 2"), mtime)
    assert _rust_result(tmp_path, _rust_cycle(tmp_path)).passed is credited


@pytest.mark.parametrize("trailer", ["[exited with code 1]", "[killed]", ""])
def test_job_output_requires_its_exit_trailer(tmp_path: Path, trailer: str) -> None:
    _retain(
        tmp_path / "tasks/job.output",
        _nextest_file("expected 2", exit_line=trailer),
        NOTICE + 0.3,
    )
    _assert_uncredited(_rust_result(tmp_path, _rust_cycle(tmp_path)))


@pytest.mark.timeout(5)
def test_job_output_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    # A FIFO has no writer here, so a blocking open would hang the close evaluation.
    output_path = tmp_path / "tasks/job.output"
    output_path.parent.mkdir(parents=True)
    os.mkfifo(output_path)
    _assert_uncredited(_rust_result(tmp_path, _rust_cycle(tmp_path)))


@pytest.mark.parametrize("restamp", ["late", "original"])
def test_job_output_mutated_between_stat_and_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restamp: str
) -> None:
    output_path = tmp_path / "tasks/job.output"
    full = _nextest_file("expected 2")
    _retain(output_path, full, NOTICE + 0.3)
    inode = output_path.stat().st_ino
    real_fstat = os.fstat
    rewritten: list[bool] = []

    def fstat_then_rewrite(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        if info.st_ino == inode and not rewritten:
            # A writer lands after the first snapshot and before the read.
            rewritten.append(True)
            _retain(output_path, full, 20 if restamp == "late" else NOTICE + 0.3)
        return info

    monkeypatch.setattr(os, "fstat", fstat_then_rewrite)
    _assert_uncredited(_rust_result(tmp_path, _rust_cycle(tmp_path)))
    assert rewritten
