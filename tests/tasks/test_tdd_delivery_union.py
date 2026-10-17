"""Gate 12 proves RED against each recognized background-output delivery, not only the last.

Deliveries are proved one at a time, never joined. A split RED credits only through the
job's retained full output, and every delivery before the cutoff must validate against it.
"""

import re
from pathlib import Path

import pytest

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import (
    TddEvidenceResult,
    _has_named_red_failure,
    evaluate_tdd_evidence,
)
from tests.tasks.test_tdd_retained_output import (
    NOTICE,
    _evidence,
    _job,
    _pair,
    _retain,
    _write,
)

pytestmark = pytest.mark.unit

TEST_FILE = "crates/gcode/src/db/queries_cas_tests.rs"
CODE_FILE = "crates/gcode/src/db/queries.rs"
MODULE = ("db", "queries", "queries_cas_tests")
TARGET = "mark_vectors_synced_cas_rejects_same_hash_stale_attempt"
QUALIFIED = f"db::queries::queries_cas_tests::{TARGET}"
# The exact RED command from #23575 (LM1 message c49983fe).
REPLAY = (
    "CARGO_BUILD_JOBS=3 CARGO_TARGET_DIR=/Users/josh/.gobby/cache/sandbox/cargo-target/"
    "lane-4-rust-4-13da843fd891a9af GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:"
    "gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E "
    "'test(mark_vectors_synced) | test(mark_vector_sync) | test(mark_graph_synced) | "
    "test(serial_db) | test(api_tests)'"
)
GREEN = f"PASS [   0.010s] (  1/1) gobby-code {QUALIFIED}\nSummary [   0.010s] 1 test run: 1 passed"
# The FAIL blocks, summary and exit trailer of #23575's real RED output (PASS rows dropped).
RED = (Path(__file__).parent / "fixtures" / "nextest_red_23575.txt").read_text(encoding="utf-8")


def _grep(output: str, pattern: str) -> str:
    """The lines ``grep -E`` prints for ``pattern`` over ``output``."""
    return "\n".join(line for line in output.splitlines() if re.search(pattern, line))


def _read(job: Path, pattern: str) -> str:
    return f"grep -E '{pattern}' {job}"


def _red_result(
    tmp_path: Path, reads: list[tuple[float, str, str]], *, retained: str | None
) -> TddEvidenceResult:
    job = tmp_path / "tasks/job.output"
    if retained is not None:
        _retain(job, retained, NOTICE + 0.3)
    records = _write(tmp_path, 1, TEST_FILE)
    records += _job(job, monitor=False, command=REPLAY)
    for at, command, text in reads:
        records += _pair(at, "Bash", {"command": command}, text)
    records += _write(tmp_path, 10, CODE_FILE)
    records += _pair(12, "Bash", {"command": REPLAY}, GREEN)
    records.sort(key=lambda record: str(record["timestamp"]))
    test = AcceptanceTest(f"{TEST_FILE}::{TARGET}", TEST_FILE, TARGET, "", rust_module=MODULE)
    return evaluate_tdd_evidence((test,), _evidence(tmp_path, records, {TEST_FILE, CODE_FILE}))


def test_earlier_self_contained_read_credits_red(tmp_path: Path) -> None:
    # No retained file: the earlier read alone must carry the RED, and the later read must not
    # displace it.
    job = tmp_path / "tasks/job.output"
    earlier = "FAIL|panicked at|assertion failed"
    result = _red_result(
        tmp_path,
        [
            (5, _read(job, earlier), _grep(RED, earlier)),
            (7, _read(job, "left:|right:"), _grep(RED, "left:|right:")),
        ],
        retained=None,
    )
    assert result.passed, result.findings
    assert result.red_runs == (REPLAY,)


def test_split_reads_credit_red_only_from_authenticated_full_output(tmp_path: Path) -> None:
    # Neither read alone names the target's failure body; together they do in the original.
    job = tmp_path / "tasks/job.output"
    detail = "assertion failed|left:|right:"
    result = _red_result(
        tmp_path,
        [
            (5, _read(job, "panicked at"), _grep(RED, "panicked at")),
            (7, _read(job, detail), _grep(RED, detail)),
        ],
        retained=RED,
    )
    assert result.passed, result.findings
    assert result.red_runs == (REPLAY,)


def test_nextest_replay_credits_full_module_path_failures(tmp_path: Path) -> None:
    job = tmp_path / "tasks/job.output"
    pattern = "panicked at|assertion|left:|right:"
    result = _red_result(tmp_path, [(5, _read(job, pattern), _grep(RED, pattern))], retained=RED)
    assert result.passed, result.findings
    assert result.red_runs == (REPLAY,)


def test_named_red_failure_accepts_full_module_path_thread() -> None:
    test = AcceptanceTest(f"{TEST_FILE}::{TARGET}", TEST_FILE, TARGET, "", rust_module=MODULE)
    matched, _ = _has_named_red_failure(REPLAY, RED, test)
    assert matched


@pytest.mark.parametrize(
    "case",
    [
        "split_without_original",
        "summary_only_original",
        "post_cutoff_detail",
        "sibling_detail_without_target",
    ],
)
def test_split_or_post_cutoff_reads_never_credit_red(tmp_path: Path, case: str) -> None:
    job = tmp_path / "tasks/job.output"
    thread = _grep(RED, "panicked at")
    detail = _grep(RED, "assertion failed|left:|right:")
    reads: list[tuple[float, str, str]] = [
        (5, _read(job, "panicked at"), thread),
        (7, _read(job, "assertion failed|left:|right:"), detail),
    ]
    retained: str | None = None
    if case == "summary_only_original":
        retained = _grep(RED, "FAIL|Summary|exited")
    elif case == "post_cutoff_detail":
        reads[1] = (11, reads[1][1], detail)
    elif case == "sibling_detail_without_target":
        # The original's target block lost its own panic and detail; the sibling's identical
        # assertion line remains. The target's panic thread alone would otherwise credit RED.
        lines = RED.splitlines()
        index = next(i for i, line in enumerate(lines) if f"thread '{QUALIFIED}'" in line)
        retained = "\n".join(lines[:index] + lines[index + 2 :])
    result = _red_result(tmp_path, reads, retained=retained)
    assert not result.passed
    assert result.red_runs == ()


def test_gate10_output_keeps_only_the_final_delivery(tmp_path: Path) -> None:
    job = tmp_path / "tasks/job.output"
    earlier = "FAIL|panicked at"
    later = _grep(RED, "left:|right:")
    records = _write(tmp_path, 1, TEST_FILE)
    records += _job(job, monitor=False, command=REPLAY)
    records += _pair(5, "Bash", {"command": _read(job, earlier)}, _grep(RED, earlier))
    records += _pair(7, "Bash", {"command": _read(job, "left:|right:")}, later)
    records.sort(key=lambda record: str(record["timestamp"]))
    evidence = _evidence(tmp_path, records, {TEST_FILE, CODE_FILE})
    [run] = [run for run in evidence.validation_runs if run.command == REPLAY]
    # Extraction strips the read's whole text, so the delivered output is the stripped read.
    assert run.output == later.strip()


def test_red_rejection_names_the_delivered_read(tmp_path: Path) -> None:
    job = tmp_path / "tasks/job.output"
    read = _read(job, "left:|right:")
    result = _red_result(tmp_path, [(5, read, _grep(RED, "left:|right:"))], retained=None)
    assert not result.passed
    reason = f"its reads before the implementation edit ({read}) name no runner row for {QUALIFIED}"
    assert any(reason in finding for finding in result.findings), result.findings
