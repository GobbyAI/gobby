"""Tests for transcript-derived task-close evidence."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import multiprocessing
import os
import pickle
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.storage.session_models import Session
from gobby.tasks import (
    transcript_evidence,
    transcript_evidence_pool,
    transcript_evidence_transfer,
    transcript_outcomes,
)
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.close_test_coverage import uncovered_pytest_paths
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence import (
    WINDOW_LOOKBACK,
    _derive_chunked_transcript_evidence,
    _derive_transcript_evidence_sync,
    _resolve_transcript_path,
    derive_transcript_evidence,
    merge_transcript_evidence,
    select_window_raw_lines,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.tasks.transcript_evidence_snapshots import load_durable_snapshot
from gobby.tasks.transcript_evidence_transfer import CHUNK_RECORDS, ChunkedPayload, decode, encode
from gobby.tasks.transcript_outcomes import EvidenceOutcome
from gobby.tasks.transcript_outcomes import extract_output as _extract_output
from gobby.workflows.found_work_gate import unresolved_validation_failures
from gobby.workflows.validation_cover import cargo_package_selection_failed

BASE_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("receipt", ["valid", "missing", "wrong-job", "wrong-call", "assistant"])
async def test_claude_background_validation_requires_matching_terminal_receipt(
    tmp_path: Path, receipt: str
) -> None:
    transcript = tmp_path / "background.jsonl"
    command = "uv run pytest tests/agents/test_plan_seat_definitions.py -q --tb=line"
    output_path = "/private/tmp/claude-test/tasks/boyb2916b.output"
    records = _claude_tool_pair(
        command=command,
        call_id="toolu-original",
        start=BASE_TIME,
        result=(
            "Command did not complete within its 120s timeout and was moved to the "
            f"background (ID: boyb2916b). Output is being written to: {output_path}. "
            "You will be notified when it completes."
        ),
    )
    failure = (
        "E   AssertionError: plan-writer\n"
        "tests/agents/test_plan_seat_definitions.py:57: AssertionError: plan-writer\n"
        "FAILED tests/agents/test_plan_seat_definitions.py::test_seat_definitions_sync_and_validate\n"
        "============================== 4 failed in 20.35s ==============================\n"
    )
    records += _claude_tool_pair(
        command=f"tail -n 40 {output_path}",
        call_id="read-output",
        start=BASE_TIME + timedelta(seconds=2),
        result=failure,
    )
    if receipt != "missing":
        job_id = "unrelated" if receipt == "wrong-job" else "boyb2916b"
        call_id = "unrelated" if receipt == "wrong-call" else "toolu-original"
        role = "assistant" if receipt == "assistant" else "user"
        records.append(
            {
                "type": role,
                "timestamp": (BASE_TIME + timedelta(seconds=4)).isoformat(),
                "message": {
                    "role": role,
                    "content": (
                        f"<task-notification><task-id>{job_id}</task-id>"
                        f"<tool-use-id>{call_id}</tool-use-id>"
                        f"<output-file>{output_path}</output-file><status>failed</status>"
                        f'<summary>Background command "{command}" failed with exit code 1</summary>'
                        "</task-notification>"
                    ),
                },
            }
        )
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )
    assert len(evidence.validation_runs) == 1
    run = evidence.validation_runs[0]
    assert run.command == command
    assert run.started_at == BASE_TIME
    if receipt == "valid":
        assert (run.outcome, run.exit_code) == ("failure", 1)
        assert run.completed_at == BASE_TIME + timedelta(seconds=4)
        assert run.output == failure.strip()
    else:
        assert run.outcome == "unknown"
        assert run.exit_code is None


LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


class _BrokenExecutor:
    def __init__(self) -> None:
        self.shutdown_args: tuple[bool, bool] | None = None

    def submit(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        raise BrokenProcessPool("worker exited")

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_args = (wait, cancel_futures)


def test_validation_output_is_bounded_with_failure_edges_preserved() -> None:
    output, truncated = _extract_output(
        {"output": "AssertionError: first\n" + ("x" * 20_000) + "\nImportError: last"}
    )

    assert truncated is True
    assert output is not None
    assert output.startswith("AssertionError: first")
    assert output.endswith("ImportError: last")
    assert len(output) <= 16_000


def test_validation_output_uses_normalized_tool_result_once() -> None:
    pytest_output = (
        "E   KeyError: 'missing'\n" + ("captured log\n" * 700) + "FAILED test.py::test_missing"
    )
    output, truncated = _extract_output(
        {
            "tool_result": {"content": pytest_output, "is_error": True},
            "raw_json": {
                "message": {"content": [{"type": "tool_result", "content": pytest_output}]},
                "toolUseResult": pytest_output + "\nE   ValueError: suffix-only failure",
            },
        }
    )

    assert output is not None
    assert output.startswith(pytest_output)
    assert output.count(pytest_output) == 1
    assert "E   ValueError: suffix-only failure" in output
    assert truncated is False


def test_validation_output_keeps_failure_detail_after_shared_prefix() -> None:
    output, truncated = _extract_output(
        {
            "tool_result": {"content": "short summary"},
            "raw_json": {"toolUseResult": "short summary\nE   ValueError: missing"},
        }
    )

    assert output == "short summary\nE   ValueError: missing"
    assert truncated is False


@pytest.mark.parametrize("normalized", ["short summary", ""])
def test_validation_output_keeps_unique_transport_result(normalized: str) -> None:
    output, truncated = _extract_output(
        {
            "tool_result": {"content": normalized},
            "raw_json": {"toolUseResult": "failure detail absent from summary"},
        }
    )

    assert output is not None
    assert "failure detail absent from summary" in output
    assert truncated is False


def _missing_transcript_worker_pid() -> int:
    return os.getpid()


def _raise_missing_transcript() -> None:
    raise TranscriptEvidenceUnavailable(
        "No transcript was found", source="codex", attempted_paths=("/missing/session.jsonl",)
    )


def test_missing_transcript_exception_preserves_process_pool() -> None:
    try:
        pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    except OSError as exc:
        pytest.skip(f"process pool unavailable: {exc}")
    with pool:
        # A module-local callable imports this test module and Gobby in the child.
        # Allow cold spawn/import here; the exception and reuse checks stay bounded.
        worker_pid = pool.submit(_missing_transcript_worker_pid).result(timeout=60)
        with pytest.raises(
            TranscriptEvidenceUnavailable, match="No transcript was found"
        ) as raised:
            pool.submit(_raise_missing_transcript).result(timeout=15)
        assert raised.value.source == "codex"
        assert raised.value.attempted_paths == ("/missing/session.jsonl",)
        assert raised.value.retry_after == 5
        assert pool.submit(os.getpid).result(timeout=15) == worker_pid
        assert worker_pid != os.getpid()


def _concurrent_pool_probe(directory: str) -> int:
    pid = os.getpid()
    root = Path(directory)
    (root / str(pid)).touch()
    deadline = time.monotonic() + 20
    while len(list(root.iterdir())) < 4:
        if time.monotonic() >= deadline:
            return -1
        time.sleep(0.02)
    return pid


@pytest.mark.slow
async def test_prewarmed_pool_runs_four_first_stops_concurrently(tmp_path: Path) -> None:
    try:
        await transcript_evidence_pool.prewarm_transcript_evidence_pool()
        pids = await asyncio.gather(
            *(
                transcript_evidence_pool.run_in_transcript_evidence_pool(
                    _concurrent_pool_probe, str(tmp_path)
                )
                for _ in range(4)
            )
        )
        assert len(set(pids)) == 4
        assert all(pid > 0 for pid in pids)
    finally:
        # A drain that outlives its test can stop the tracker under later tests' pools.
        transcript_evidence_pool.shutdown_transcript_evidence_pool(timeout=60.0)


async def test_process_pool_oserror_falls_back_and_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_to_start() -> ProcessPoolExecutor:
        raise OSError("spawn unavailable")

    monkeypatch.setattr(transcript_evidence_pool, "_get_pool", fail_to_start)
    monkeypatch.setattr(transcript_evidence_pool, "_fallback_warning_logged", False)
    with caplog.at_level(logging.WARNING, logger=transcript_evidence_pool.__name__):
        assert await transcript_evidence_pool.run_in_transcript_evidence_pool(pow, 2, 3) == 8
        assert await transcript_evidence_pool.run_in_transcript_evidence_pool(pow, 3, 2) == 9

    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "OSError: spawn unavailable" in warnings[0]


async def test_broken_process_pool_is_discarded_before_thread_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _BrokenExecutor()
    executor = cast(ProcessPoolExecutor, fake)
    monkeypatch.setattr(transcript_evidence_pool, "_pool", executor)
    monkeypatch.setattr(transcript_evidence_pool, "_get_pool", lambda: executor)
    monkeypatch.setattr(transcript_evidence_pool, "_fallback_warning_logged", False)

    result = await transcript_evidence_pool.run_in_transcript_evidence_pool(pow, 2, 4)

    assert result == 16
    assert transcript_evidence_pool._pool is None
    assert fake.shutdown_args == (False, True)


@pytest.fixture
def _no_pending_pool_exit() -> None:
    """Let an earlier test's pool-exit thread finish before tracker calls are recorded.

    A real worker that outlives the shutdown timeout leaves its exit thread
    running; it would later call the patched tracker stop and record into
    another test's events.
    """
    for thread in threading.enumerate():
        if thread.name == "transcript-evidence-pool-exit":
            thread.join(10)


class _RecordingExecutor:
    def __init__(self, events: list[str], *, block_on_wait: threading.Event | None = None) -> None:
        self._events = events
        self._block_on_wait = block_on_wait

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._events.append(f"shutdown:wait={wait}:cancel={cancel_futures}")
        if wait and self._block_on_wait is not None:
            self._block_on_wait.wait()


@pytest.mark.usefixtures("_no_pending_pool_exit")
def test_shutdown_waits_for_worker_exit_then_stops_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fake = _RecordingExecutor(events)
    monkeypatch.setattr(transcript_evidence_pool, "_pool", cast(ProcessPoolExecutor, fake))
    monkeypatch.setattr(
        transcript_evidence_pool, "_stop_resource_tracker", lambda: events.append("tracker")
    )

    transcript_evidence_pool.shutdown_transcript_evidence_pool()

    assert events == ["shutdown:wait=True:cancel=True", "tracker"]
    assert transcript_evidence_pool._pool is None


@pytest.mark.usefixtures("_no_pending_pool_exit")
def test_shutdown_leaves_hung_worker_to_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    release = threading.Event()
    tracker_stopped = threading.Event()

    def stop_tracker() -> None:
        events.append("tracker")
        tracker_stopped.set()

    fake = _RecordingExecutor(events, block_on_wait=release)
    monkeypatch.setattr(transcript_evidence_pool, "_pool", cast(ProcessPoolExecutor, fake))
    monkeypatch.setattr(transcript_evidence_pool, "_stop_resource_tracker", stop_tracker)

    try:
        started = time.monotonic()
        transcript_evidence_pool.shutdown_transcript_evidence_pool(timeout=0.05)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert events == ["shutdown:wait=True:cancel=True"]
        assert transcript_evidence_pool._pool is None
    finally:
        release.set()
        assert tracker_stopped.wait(5), "pool drain must finish before patches are restored"
    assert events == ["shutdown:wait=True:cancel=True", "tracker"]


def test_shutdown_without_pool_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(transcript_evidence_pool, "_pool", None)
    monkeypatch.setattr(
        transcript_evidence_pool, "_stop_resource_tracker", lambda: events.append("tracker")
    )

    transcript_evidence_pool.shutdown_transcript_evidence_pool()

    assert events == []


def _resource_tracker_pid() -> int | None:
    from multiprocessing import resource_tracker

    return cast(int | None, getattr(resource_tracker._resource_tracker, "_pid", None))


_POOL_OWNER = """
import asyncio, time
from multiprocessing import resource_tracker
from gobby.tasks import transcript_evidence_pool

asyncio.run(transcript_evidence_pool.prewarm_transcript_evidence_pool())
pool = transcript_evidence_pool._get_pool()
pids = [proc.pid for proc in pool._processes.values()]
pids.append(resource_tracker._resource_tracker._pid)
print(" ".join(str(pid) for pid in pids), flush=True)
time.sleep(600)
"""


def test_pool_processes_exit_when_the_daemon_is_killed() -> None:
    # A SIGKILLed daemon runs no shutdown; its workers and tracker must still go.
    owner = subprocess.Popen(
        [sys.executable, "-c", _POOL_OWNER], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL
    )
    pids: list[int] = []
    try:
        assert owner.stdout is not None
        pids = [int(pid) for pid in owner.stdout.readline().split()]
        assert len(pids) == 5
        owner.kill()
        owner.wait(timeout=10)

        assert _survivors(pids, timeout=15.0) == []
    finally:
        owner.kill()
        for pid in _survivors(pids, timeout=0.0):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _survivors(pids: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    while alive := [pid for pid in pids if psutil.pid_exists(pid) and not _zombie(pid)]:
        if time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)
    return []


def _zombie(pid: int) -> bool:
    try:
        return bool(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return True


def test_shutdown_stops_resource_tracker_for_real_pool() -> None:
    try:
        pool = transcript_evidence_pool._get_pool()
    except OSError as exc:
        pytest.skip(f"process pool unavailable: {exc}")
    try:
        assert pool.submit(pow, 2, 5).result(timeout=60) == 32
        assert _resource_tracker_pid() is not None

        transcript_evidence_pool.shutdown_transcript_evidence_pool(timeout=60.0)

        assert _resource_tracker_pid() is None
        assert transcript_evidence_pool._pool is None
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


def _session(source: str, transcript_path: Path | None, *, suffix: str = "1") -> Session:
    return Session(
        id=f"00000000-0000-0000-0000-00000000000{suffix}",
        external_id=f"transcript-evidence-{source}-{suffix}",
        machine_id=LOCAL_MACHINE_ID,
        source=source,
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript_path) if transcript_path else None,
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _claude_tool_pair(
    *,
    command: str,
    call_id: str,
    start: datetime,
    result: Any,
    is_error: bool = False,
) -> list[dict[str, Any]]:
    transport: Any = None
    if isinstance(result, dict) and "exit_code" in result:
        # Render an {exit_code, stdout} shorthand as Claude records Bash: the
        # status lives only in an error content's "Exit code N" header.
        text = result.get("stdout", result.get("output", ""))
        if result["exit_code"]:
            result = f"Exit code {result['exit_code']}\n{text}"
            is_error = True
            transport = f"Error: {result}"
        else:
            result = text
            transport = {"stdout": text, "stderr": "", "interrupted": False, "isImage": False}
    return [
        {
            "type": "assistant",
            "timestamp": start.isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": (start + timedelta(seconds=1)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": result,
                        "is_error": is_error,
                    }
                ],
            },
            **({"toolUseResult": transport} if transport is not None else {}),
        },
    ]


def _claude_edit_pair(
    name: str, arguments: dict[str, Any], call_id: str, seconds: int
) -> list[dict[str, Any]]:
    records = _claude_tool_pair(
        command="", call_id=call_id, start=BASE_TIME + timedelta(seconds=seconds), result="Done"
    )
    block = records[0]["message"]["content"][0]
    block["name"] = name
    block["input"] = arguments
    return records


@pytest.mark.parametrize(
    "original_source,failure_line,expected",
    [
        pytest.param(True, 5, True, id="original-test-body"),
        pytest.param(True, 2, False, id="fixture-body"),
        pytest.param(True, 8, False, id="sibling-body"),
        pytest.param(False, 5, False, id="missing-original-source"),
    ],
)
async def test_tb_line_red_uses_original_transcript_source_not_later_test_lines(
    tmp_path: Path, original_source: bool, failure_line: int, expected: bool
) -> None:
    test_path, product_path = "tests/test_feature.py", "src/feature.py"
    source = (
        "def fixture_helper():\n    assert False\n\n"
        "def test_original():\n    assert feature() == 1\n\n"
        "def test_sibling():\n    assert other() == 1\n"
    )
    command = f"uv run pytest {test_path} -q --tb=line"
    transcript = tmp_path / "source-proof.jsonl"
    records = _claude_edit_pair(
        "Write" if original_source else "Edit",
        {"file_path": str(tmp_path / test_path), "content": source}
        if original_source
        else {"file_path": str(tmp_path / test_path), "old_string": "pass", "new_string": source},
        "original-tests",
        0,
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="red",
            start=BASE_TIME + timedelta(seconds=2),
            result={
                "exit_code": 1,
                "stdout": f"{test_path}:{failure_line}: AssertionError: assert 0 == 1\n1 failed",
            },
            is_error=True,
        )
    )
    records.extend(
        _claude_edit_pair(
            "Edit",
            {
                "file_path": str(tmp_path / product_path),
                "old_string": "return 0",
                "new_string": "return 1",
            },
            "behavior",
            4,
        )
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="green",
            start=BASE_TIME + timedelta(seconds=6),
            result={"exit_code": 0, "stdout": "2 passed"},
        )
    )
    # A post-implementation full Write cannot establish the source at RED.
    records.extend(
        _claude_edit_pair(
            "Write",
            {"file_path": str(tmp_path / test_path), "content": "\n" * 20 + source},
            "later-tests",
            8,
        )
    )
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, product_path},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::test_original",
        path=test_path,
        symbol="test_original",
        body="def test_original():\n    assert feature() == 1\n",
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is expected, result


@pytest.mark.parametrize(
    "case,expected",
    [
        ("ignored-new-keyword", True),
        ("leading-test-context", True),
        ("comment-only-after-stub", True),
        ("other-method-after-stub", True),
        ("partial-method-after-stub", True),
        ("changed-partial-method-after-stub", False),
        ("guarded-helper", True),
        ("unused-guarded-helper", False),
        ("changed-old-body", False),
        ("keyword-already-existed", False),
        ("test-does-not-use-keyword", False),
        ("behavior-after-stub", False),
        ("denied-stub", False),
        ("effectful-default", False),
    ],
)
async def test_python_keyword_stub_red_requires_original_test_and_unchanged_old_body(
    tmp_path: Path, case: str, expected: bool
) -> None:
    test_path, product_path = "tests/test_workspaces.py", "src/gobby/storage/workspaces.py"
    body = (
        "def test_original(workspaces):\n"
        "    with pytest.raises(WorkspaceBusyError):\n"
        "        workspaces.close('workspace', refuse_in_flight=True)\n"
    )
    if case == "test-does-not-use-keyword":
        body = body.replace(", refuse_in_flight=True", "")
    if case in {"guarded-helper", "unused-guarded-helper"}:
        body = (
            "_GUARDED = [lambda workspaces: workspaces.close('workspace', refuse_in_flight=True)]\n\n"
            "def test_original(workspaces):\n"
            "    with pytest.raises(WorkspaceBusyError):\n"
            "        for operation in _GUARDED:\n"
            "            operation(workspaces)\n"
        )
        if case == "unused-guarded-helper":
            body = body.replace(
                "for operation in _GUARDED:\n            operation(workspaces)",
                "workspaces.close('workspace')",
            )
    test_fragment = (
        "    assert previous_test()\n\n\n" + body if case == "leading-test-context" else body
    )
    old = '    def close(self, workspace_id: str) -> Workspace:\n        """Close."""\n        row = self.db.fetchone(\n'
    new = (
        "    def close(self, workspace_id: str, *, refuse_in_flight: bool = False) -> Workspace:\n"
        '        """Close."""\n        del refuse_in_flight\n        row = self.db.fetchone(\n'
    )
    if case == "changed-old-body":
        new = new.replace("row = self.db.fetchone(", "row = self.db.changed(")
    if case == "keyword-already-existed":
        old = old.replace(
            "workspace_id: str)", "workspace_id: str, *, refuse_in_flight: bool = False)"
        )
    if case == "effectful-default":
        new = new.replace("= False)", "= compute_default())")
    records = _claude_edit_pair(
        "Edit",
        {
            "file_path": str(tmp_path / test_path),
            "old_string": "# end",
            "new_string": test_fragment,
        },
        "tests",
        0,
    )
    # An earlier mechanical or unrelated edit cannot implement the newly ignored input.
    records.extend(
        _claude_edit_pair(
            "Edit",
            {
                "file_path": str(tmp_path / product_path),
                "old_string": "class Old: pass",
                "new_string": "class New: pass",
            },
            "earlier-source",
            2,
        )
    )
    stub = _claude_edit_pair(
        "Edit",
        {"file_path": str(tmp_path / product_path), "old_string": old, "new_string": new},
        "api-shape",
        4,
    )
    if case == "denied-stub":
        stub[1]["message"]["content"][0].update(content=_HOOK_BLOCKED, is_error=True)
    records.extend(stub)
    if case in {
        "other-method-after-stub",
        "partial-method-after-stub",
        "changed-partial-method-after-stub",
    }:
        other_old, other_new = (
            old.replace("def close", "def remove"),
            new.replace("def close", "def remove"),
        )
        if case != "other-method-after-stub":
            other_old = '        beside: str | None = None,\n    ) -> Workspace:\n        """Move."""\n        row = self.db.fetchone(\n'
            other_new = '        beside: str | None = None,\n        refuse_in_flight: bool = False,\n    ) -> Workspace:\n        """Move."""\n        del refuse_in_flight\n        row = self.db.fetchone(\n'
        if case == "changed-partial-method-after-stub":
            other_new = other_new.replace("self.db.fetchone", "self.db.changed")
        records.extend(
            _claude_edit_pair(
                "Edit",
                {
                    "file_path": str(tmp_path / product_path),
                    "old_string": other_old,
                    "new_string": other_new,
                },
                "other-api-shape",
                6,
            )
        )
    if case in {"comment-only-after-stub", "behavior-after-stub"}:
        records.extend(
            _claude_edit_pair(
                "Edit",
                {
                    "file_path": str(tmp_path / product_path),
                    "old_string": "from module import name\n# comment\n",
                    "new_string": "from module import name\n",
                }
                if case == "comment-only-after-stub"
                else {
                    "file_path": str(tmp_path / product_path),
                    "old_string": "return row",
                    "new_string": "return changed(row)",
                },
                "after-stub",
                6,
            )
        )
    command = f"uv run pytest {test_path}::test_original -q"
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="red",
            start=BASE_TIME + timedelta(seconds=8),
            result={
                "exit_code": 1,
                "stdout": f"____ test_original ____\n{test_path}:3: in test_original\nE   Failed: DID NOT RAISE <class 'WorkspaceBusyError'>\n1 failed",
            },
            is_error=True,
        )
    )
    records.extend(
        _claude_edit_pair(
            "Edit",
            {
                "file_path": str(tmp_path / product_path),
                "old_string": "del refuse_in_flight",
                "new_string": "if refuse_in_flight: raise WorkspaceBusyError()",
            },
            "behavior",
            10,
        )
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="green",
            start=BASE_TIME + timedelta(seconds=12),
            result={"exit_code": 0, "stdout": "1 passed"},
        )
    )
    transcript = tmp_path / "stub-proof.jsonl"
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, product_path},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::test_original", path=test_path, symbol="test_original", body=body
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is expected, result


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("created-noop", True),
        ("late-test-confirmation", False),
        ("late-stub-confirmation", False),
        ("helper-wiring", True),
        ("constant-api-shape", True),
        ("unrelated-constant", False),
        ("unused-helper-wiring", False),
        ("wrong-wiring-alias", False),
        ("created-noop-ni", True),
        ("overwrite-ni", False),
        ("previous-behavior-ni", False),
        ("behavior-before-red-ni", False),
        ("overwrite", False),
        ("previous-behavior", False),
        ("constructor-side-effect", False),
        ("effectful-default", False),
        ("behavior-before-red", False),
        ("unrelated-import", False),
        ("denied-creation", False),
    ],
)
async def test_python_created_noop_module_red_requires_native_creation_and_original_import(
    tmp_path: Path, case: str, expected: bool
) -> None:
    test_path = "tests/test_upgrade.py"
    product_path = "src/gobby/terminals/host_upgrade.py"
    wiring_path = "src/gobby/terminals/host_manager.py"
    body = (
        "from gobby.terminals.host_upgrade import Coordinator\n\n"
        "async def test_original():\n"
        "    coord = Coordinator(installed='binary')\n"
        "    await coord.observe()\n"
        "    assert coord.is_open\n"
    )
    if case == "unrelated-import":
        body = body.replace("gobby.terminals.host_upgrade", "other.module")
    if case in {"constant-api-shape", "unrelated-constant"}:
        body = (
            "from gobby.terminals.host_upgrade import BUDGET\n\n"
            "async def test_original():\n"
            "    assert feature_ready(BUDGET)\n"
        )
        if case == "unrelated-constant":
            body = body.replace("import BUDGET", "import OTHER_BUDGET as BUDGET")
    if case in {"helper-wiring", "unused-helper-wiring", "wrong-wiring-alias"}:
        body = (
            "from gobby.terminals.host_manager import Manager\n\n"
            "def make_manager():\n"
            "    return Manager()\n\n"
            "async def test_original():\n"
            "    coord = make_manager()\n"
            "    assert coord.api.window is not None\n"
        )
        if case == "unused-helper-wiring":
            body = body.replace("coord = make_manager()", "coord = unrelated()")
    stub_source = (
        "from __future__ import annotations\n\n"
        "class Coordinator:\n"
        "    def __init__(self, *, installed: str) -> None:\n"
        "        self.installed = installed\n"
        "        self.window: str | None = None\n\n"
        "    @property\n"
        "    def is_open(self) -> bool:\n"
        "        return False\n\n"
        "    async def observe(self) -> None:\n"
        "        return None\n"
    )
    if case == "constructor-side-effect":
        stub_source = stub_source.replace("self.installed = installed", "activate(installed)")
    if case == "effectful-default":
        stub_source = stub_source.replace("installed: str", "installed: str = activate()")
    if case == "created-noop-ni":
        stub_source = stub_source.replace("return False", "raise NotImplementedError")
    if case in {"constant-api-shape", "unrelated-constant"}:
        stub_source += "\nBUDGET = 15\n"
    records = _claude_edit_pair(
        "Write", {"file_path": str(tmp_path / test_path), "content": body}, "tests", 0
    )
    if case == "late-test-confirmation":
        records[1]["timestamp"] = (BASE_TIME + timedelta(seconds=9)).isoformat()
    records.extend(
        _claude_edit_pair(
            "Write",
            {"file_path": str(tmp_path / wiring_path), "content": "# API wiring placeholder\n"},
            "earlier-source",
            2,
        )
    )
    if case in {"previous-behavior", "previous-behavior-ni"}:
        records.extend(
            _claude_edit_pair(
                "Write",
                {"file_path": str(tmp_path / product_path), "content": "activate()\n"},
                "previous-behavior",
                2,
            )
        )
    creation = _claude_edit_pair(
        "Write",
        {"file_path": str(tmp_path / product_path), "content": stub_source},
        "api-shape",
        4,
    )
    creation[1]["message"]["content"][0]["content"] = (
        f"The file {tmp_path / product_path} has been updated successfully."
        if case in {"overwrite", "overwrite-ni"}
        else f"File created successfully at: {tmp_path / product_path}"
    )
    if case == "denied-creation":
        creation[1]["message"]["content"][0].update(content=_HOOK_BLOCKED, is_error=True)
    if case == "late-stub-confirmation":
        creation[1]["timestamp"] = (BASE_TIME + timedelta(seconds=9)).isoformat()
    records.extend(creation)
    if case in {"helper-wiring", "unused-helper-wiring", "wrong-wiring-alias"}:
        wiring = (
            "from gobby.terminals.host_upgrade import Coordinator\n\n"
            "class Manager:\n"
            "    def __init__(self):\n"
            "        self.api = Coordinator(installed='binary')\n"
        )
        if case == "wrong-wiring-alias":
            wiring = wiring.replace("import Coordinator", "import Coordinator as API")
        records.extend(
            _claude_edit_pair(
                "Edit",
                {
                    "file_path": str(tmp_path / wiring_path),
                    "old_string": "# API wiring placeholder\n",
                    "new_string": wiring,
                },
                "api-wiring",
                6,
            )
        )
    if case in {"behavior-before-red", "behavior-before-red-ni"}:
        records.extend(
            _claude_edit_pair(
                "Edit",
                {
                    "file_path": str(tmp_path / product_path),
                    "old_string": "return False",
                    "new_string": "return bool(self.window)",
                },
                "behavior-before-red",
                6,
            )
        )
    command = f"uv run pytest {test_path}::test_original -q"
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="red",
            start=BASE_TIME + timedelta(seconds=8),
            result={
                "exit_code": 1,
                "stdout": (
                    f"____ test_original ____\n{test_path}:6: in test_original\n"
                    + (
                        "E   NotImplementedError\n"
                        if case.endswith("-ni")
                        else "E   assert False\n"
                    )
                    + "1 failed"
                ),
            },
            is_error=True,
        )
    )
    records.extend(
        _claude_edit_pair(
            "Write",
            {"file_path": str(tmp_path / product_path), "content": "# implemented\n"},
            "behavior",
            10,
        )
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="green",
            start=BASE_TIME + timedelta(seconds=12),
            result={"exit_code": 0, "stdout": "1 passed"},
        )
    )
    transcript = tmp_path / "module-stub-proof.jsonl"
    records.sort(key=lambda record: record["timestamp"])
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, product_path, wiring_path},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::test_original", path=test_path, symbol="test_original", body=body
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is expected, result


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("added-ni", True),
        ("added-noop-assertion", True),
        ("added-real-body", False),
        ("replaced-anchor", False),
        ("behavior-before-red", False),
        ("unrelated-import", False),
    ],
)
async def test_python_stub_added_to_existing_module_credits_top_level_import_red(
    tmp_path: Path, case: str, expected: bool
) -> None:
    test_path = "tests/test_feature.py"
    product_path = "src/gobby/terminals/feature.py"
    module = "other.module" if case == "unrelated-import" else "gobby.terminals.feature"
    body = (
        f"from {module} import feature_ready\n\ndef test_original():\n    assert feature_ready(3)\n"
    )
    stub_body = {
        "added-noop-assertion": "    return False\n",
        "added-real-body": "    return value > 2\n",
    }.get(case, "    raise NotImplementedError\n")
    anchor = "EXISTING = 1\n"
    stub = f"\n\ndef feature_ready(value: int) -> bool:\n{stub_body}"
    records = _claude_edit_pair(
        "Write", {"file_path": str(tmp_path / test_path), "content": body}, "tests", 0
    )
    records.extend(
        _claude_edit_pair(
            "Edit",
            {
                "file_path": str(tmp_path / product_path),
                "old_string": anchor,
                "new_string": ("EXISTING = 2\n" if case == "replaced-anchor" else anchor) + stub,
            },
            "api-shape",
            2,
        )
    )
    if case == "behavior-before-red":
        records.extend(
            _claude_edit_pair(
                "Edit",
                {
                    "file_path": str(tmp_path / product_path),
                    "old_string": stub_body,
                    "new_string": "    return value > 2\n",
                },
                "behavior-before-red",
                4,
            )
        )
    command = f"uv run pytest {test_path}::test_original -q"
    failure = (
        "E   assert False\n" if case == "added-noop-assertion" else "E   NotImplementedError\n"
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="red",
            start=BASE_TIME + timedelta(seconds=8),
            result={
                "exit_code": 1,
                "stdout": (
                    f"____ test_original ____\n{test_path}:4: in test_original\n{failure}1 failed"
                ),
            },
            is_error=True,
        )
    )
    records.extend(
        _claude_edit_pair(
            "Edit",
            {
                "file_path": str(tmp_path / product_path),
                "old_string": stub_body,
                "new_string": "    return value > 2\n",
            },
            "behavior",
            10,
        )
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="green",
            start=BASE_TIME + timedelta(seconds=12),
            result={"exit_code": 0, "stdout": "1 passed"},
        )
    )
    transcript = tmp_path / "added-stub-proof.jsonl"
    records.sort(key=lambda record: record["timestamp"])
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, product_path},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::test_original", path=test_path, symbol="test_original", body=body
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is expected, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["exception", "active-exception", "unchanged-move", "changed-move"]
)
async def test_python_api_provenance_from_provider_receipts(tmp_path: Path, case: str) -> None:
    test_path = "tests/test_provenance.py"
    product_path = "src/gobby/feature.py"
    records: list[dict[str, Any]] = []
    if case in {"exception", "active-exception"}:
        body = (
            "import pytest\nfrom gobby.feature import CronSessionError, launch\n\n"
            "def test_original():\n    with pytest.raises(CronSessionError):\n        launch()\n"
        )
        anchor = "def launch():\n    return None\n"
        stub = 'class CronSessionError(RuntimeError):\n    """Missing cron session."""\n\n\n'
        shape = {"old_string": anchor, "new_string": anchor + stub}
        failure = "E   Failed: DID NOT RAISE <class 'gobby.feature.CronSessionError'>\n"
        implementation = {"old_string": "return None", "new_string": "raise CronSessionError"}
        records.extend(
            _claude_edit_pair(
                "Write",
                {
                    "file_path": str(tmp_path / product_path),
                    "content": anchor
                    if case == "exception"
                    else "def launch():\n    raise CronSessionError\n\n",
                },
                "existing-behavior",
                0,
            )
        )
        if case == "active-exception":
            shape = {"old_string": "def launch():", "new_string": stub + "def launch():"}
            implementation = {"old_string": "raise CronSessionError", "new_string": "return None"}
    else:
        body = (
            "from gobby.feature import payload\n\ndef test_original():\n"
            "    assert payload({})['seat'] == 'lane-3'\n"
        )
        baseline = "def payload(run):\n    return {'id': run.get('id')}\n"
        repeated = baseline if case == "unchanged-move" else baseline.replace("run.get('id')", "42")
        records.extend(
            _claude_edit_pair(
                "Write", {"file_path": str(tmp_path / product_path), "content": baseline}, "move", 0
            )
        )
        shape = {"content": repeated}
        failure = "E   KeyError: 'seat'\n"
        implementation = {
            "old_string": repeated,
            "new_string": "def payload(run):\n    return {'seat': 'lane-3'}\n",
        }
    records.extend(
        _claude_edit_pair(
            "Write", {"file_path": str(tmp_path / test_path), "content": body}, "tests", 2
        )
    )
    records.extend(
        _claude_edit_pair(
            "Edit" if case in {"exception", "active-exception"} else "Write",
            {"file_path": str(tmp_path / product_path), **shape},
            "shape",
            4,
        )
    )
    if case not in {"exception", "active-exception"}:
        records.extend(
            _claude_edit_pair(
                "Edit",
                {
                    "file_path": str(tmp_path / "src/gobby/query.py"),
                    "old_string": "from collections.abc import Callable, Mapping\n",
                    "new_string": "from collections.abc import Callable\n",
                },
                "cleanup",
                6,
            )
        )
    command = f"uv run pytest {test_path}::test_original -q"
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="red",
            start=BASE_TIME + timedelta(seconds=8),
            result={
                "exit_code": 1,
                "stdout": f"____ test_original ____\n{test_path}:4: in test_original\n{failure}1 failed",
            },
            is_error=True,
        )
    )
    records.extend(
        _claude_edit_pair(
            "Edit", {"file_path": str(tmp_path / product_path), **implementation}, "behavior", 10
        )
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="green",
            start=BASE_TIME + timedelta(seconds=12),
            result={"exit_code": 0, "stdout": "1 passed"},
        )
    )
    records.sort(key=lambda record: record["timestamp"])
    transcript = tmp_path / "provenance.jsonl"
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, product_path, "src/gobby/query.py"},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::test_original", path=test_path, symbol="test_original", body=body
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is (case not in {"changed-move", "active-exception"}), result


@pytest.mark.asyncio
async def test_claude_pairs_shell_results_and_tracks_task_edits(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    records = [
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_example.py",
            call_id="test-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "passed"},
        ),
        {
            "type": "assistant",
            "timestamp": (BASE_TIME + timedelta(seconds=2)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "edit-1",
                        "name": "Edit",
                        "input": {"file_path": str(tmp_path / "src" / "changed.py")},
                    }
                ],
            },
        },
    ]
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code, run.categories) for run in evidence.validation_runs] == [
        ("success", None, ("test",))
    ]
    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [("src/changed.py", "Edit")]
    assert evidence.validation_runs[0].completed_at < evidence.edits[0].timestamp


@pytest.mark.asyncio
async def test_failed_rust_test_run_does_not_overturn_passing_python_lint(tmp_path: Path) -> None:
    """A failing nextest run is test evidence only; it cannot fail lint or type_check (#23382)."""
    transcript = tmp_path / "claude.jsonl"
    lint = "uv run ruff check src/"
    nextest = (
        "cargo nextest run -p gobby-client --status-level pass --stress-count 30 "
        "-E 'binary(loop_liveness)'"
    )
    _write_jsonl(
        transcript,
        [
            *_claude_tool_pair(
                command=lint,
                call_id="lint-1",
                start=BASE_TIME,
                result={"exit_code": 0, "stdout": "All checks passed!"},
            ),
            *_claude_tool_pair(
                command=nextest,
                call_id="rust-1",
                start=BASE_TIME + timedelta(seconds=2),
                result={"exit_code": 101, "stdout": "error: test run failed"},
                is_error=True,
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        changed_paths=("src/example.py",),
    )

    assert gate.details["latest_outcomes"] == {
        "lint": "success",
        "type_check": "success",
        "test": "failure",
    }
    assert gate.status == "failed"
    assert [(run.command, run.outcome, run.categories) for run in evidence.validation_runs] == [
        (lint, "success", ("lint", "type_check")),
        (nextest, "failure", ("test",)),
    ]


_NEXTEST_PACKAGE_ERROR = (
    "Exit code 101\n"
    "error: package ID specification `gterminal` did not match any packages\n\n"
    "help: a package with a similar name exists: `termina`\n"
    "error: command `/Users/josh/.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo test "
    "--no-run --message-format json-render-diagnostics --package gterminal --test build_env` "
    "exited with code 101"
)
_NEXTEST_TEST_FAILURE = (
    "Exit code 100\n"
    "        FAIL [   0.012s] gterminal::build_env resolves_pinned_zig\n"
    "     Summary [   0.050s] 3 tests run: 2 passed, 1 failed, 0 skipped\n"
    "error: test run failed"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "exit_code", "selection_error"),
    [
        pytest.param(_NEXTEST_PACKAGE_ERROR, 101, True, id="package-spec-error"),
        pytest.param(_NEXTEST_TEST_FAILURE, 100, False, id="test-failure"),
    ],
)
async def test_claude_bash_error_header_reaches_the_stop_gate(
    tmp_path: Path, content: str, exit_code: int, selection_error: bool
) -> None:
    """Records shaped like gobby#15391's: Claude's status is only the error text (#23529)."""
    transcript = tmp_path / "claude.jsonl"
    command = "cargo nextest run -p gterminal --test build_env"
    records = _claude_tool_pair(
        command=command,
        call_id="toolu_01MLWZy9sxBbRtUWydVodEFF",
        start=BASE_TIME,
        result=content,
        is_error=True,
    )
    records[1]["toolUseResult"] = f"Error: {content}"
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    [run] = evidence.validation_runs
    assert (run.command, run.outcome, run.exit_code) == (command, "failure", exit_code)
    assert cargo_package_selection_failed(run) is selection_error
    unresolved = unresolved_validation_failures(
        evidence.validation_runs, owner_handoff=False, project_path=str(tmp_path)
    )
    assert unresolved == (() if selection_error else (run,))


_CLAUDE_USER_REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). "
    "STOP what you are doing and wait for the user to tell you how to proceed."
)
_HOOK_BLOCKED = (
    "Rule enforced by Gobby: [step-enforcement:backend-developer/load_required_skills]\n"
    'get_skill_file(name="gobby", path="references/development/obligations.md")'
)
_PERMISSION_DENIED = "Permission to use Bash has been denied."
_UNEXECUTED_VALIDATION_COMMAND = "uv run ruff check src/ tests/sync/test_jsonl_io.py"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "tool_name"),
    [("claude", "Edit"), ("claude", "Write"), ("claude", "Bash"), ("codex", "apply_patch")],
)
@pytest.mark.parametrize(
    "denial",
    [
        pytest.param(_CLAUDE_USER_REJECTED, id="user-rejection"),
        pytest.param(_HOOK_BLOCKED, id="hook-block"),
        pytest.param(_PERMISSION_DENIED, id="permission-denial"),
    ],
)
async def test_unexecuted_edit_is_not_credit_but_later_same_path_edit_is(
    tmp_path: Path, source: str, tool_name: str, denial: str
) -> None:
    transcript = tmp_path / f"{source}.jsonl"
    path = str(tmp_path / "src" / "changed.py")
    arguments: dict[str, Any] = {"file_path": path, "old_string": "old", "new_string": "new"}
    if tool_name == "Write":
        arguments = {"file_path": path, "content": "new"}
    elif tool_name == "Bash":
        arguments = {"command": f"cat > {path} <<'PY'\nnew\nPY"}
    records: list[dict[str, Any]] = []
    for index, result in enumerate([denial, "Successfully edited the file."]):
        call_id = f"edit-{index}"
        start = BASE_TIME + timedelta(seconds=index * 2)
        if source == "claude":
            pair = _claude_tool_pair(
                command="", call_id=call_id, start=start, result=result, is_error=index == 0
            )
            block = pair[0]["message"]["content"][0]
            block.update(name=tool_name, input=arguments)
            records.extend(pair)
        else:
            patch = (
                "*** Begin Patch\n*** Update File: src/changed.py\n@@\n-old\n+new\n*** End Patch\n"
            )
            records.extend(
                [
                    _codex_response_item(
                        {
                            "type": "custom_tool_call",
                            "call_id": call_id,
                            "name": tool_name,
                            "input": patch,
                        },
                        start,
                    ),
                    _codex_response_item(
                        {"type": "custom_tool_call_output", "call_id": call_id, "output": result},
                        start + timedelta(seconds=1),
                    ),
                ]
            )
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session(source, transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name, edit.timestamp) for edit in evidence.edits] == [
        ("src/changed.py", tool_name, BASE_TIME + timedelta(seconds=2))
    ]


@pytest.mark.asyncio
async def test_executed_edit_whose_file_quotes_a_denial_is_credited(tmp_path: Path) -> None:
    """Claude's transport copy of an edited file is not the tool's own verdict (#23409)."""
    transcript = tmp_path / "claude.jsonl"
    path = str(tmp_path / "tests" / "test_denials.py")
    records = _claude_edit_pair(
        "Edit", {"file_path": path, "old_string": "old", "new_string": "new"}, "edit", 0
    )
    records[1]["message"]["content"][0]["content"] = f"The file {path} has been updated."
    records[1]["toolUseResult"] = {
        "filePath": path,
        "originalFile": f"REJECTED = {_CLAUDE_USER_REJECTED!r}\nold\n",
    }
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"tests/test_denials.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("tests/test_denials.py", "Edit")
    ]


@pytest.mark.asyncio
async def test_unflagged_droid_hook_denial_is_not_edit_credit(tmp_path: Path) -> None:
    """Only Claude flags every denial; an unflagged Droid block never ran (#23410)."""
    transcript = tmp_path / "droid.jsonl"
    path = str(tmp_path / "src" / "changed.py")
    result = _droid_tool_result(
        timestamp=BASE_TIME + timedelta(seconds=1), call_id="droid-edit", content=_HOOK_BLOCKED
    )
    del result["message"]["content"][0]["is_error"]
    edit = _droid_tool_record(
        timestamp=BASE_TIME,
        call_id="droid-edit",
        name="Edit",
        tool_input={"file_path": path, "old_string": "old", "new_string": "new"},
    )
    _write_jsonl(transcript, [edit, result])

    evidence = await derive_transcript_evidence(
        _session("droid", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert evidence.edits == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        pytest.param(_CLAUDE_USER_REJECTED, id="rejected"),
        pytest.param(_HOOK_BLOCKED, id="hook-blocked"),
        pytest.param(_PERMISSION_DENIED, id="permission-denied"),
        pytest.param(f"Hook denied: {_HOOK_BLOCKED}", id="hook-denied-prefix"),
    ],
)
async def test_declined_tool_call_is_not_validation_evidence(tmp_path: Path, result: str) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=_UNEXECUTED_VALIDATION_COMMAND,
            call_id="denied-1",
            start=BASE_TIME,
            result=result,
            is_error=True,
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert evidence.validation_runs == ()
    assert evidence.command_runs == ()
    assert evidence.degraded_capabilities == ()


@pytest.mark.asyncio
async def test_executed_tool_failure_without_exit_code_is_still_failure(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command="uv run ruff check src/gobby",
            call_id="fail-1",
            start=BASE_TIME,
            result="ruff check found 3 errors",
            is_error=True,
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code, run.categories) for run in evidence.validation_runs] == [
        ("failure", None, ("lint", "type_check"))
    ]


@pytest.mark.asyncio
async def test_claude_subagent_transcript_satisfies_validation_close_gate(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript-evidence-claude-2.jsonl"
    subagent = transcript.with_suffix("") / "subagents" / "agent-worker.jsonl"
    subagent.parent.mkdir(parents=True)
    _write_jsonl(transcript, [])
    session = _session("claude", transcript, suffix="2")
    window_start = BASE_TIME + timedelta(seconds=10)
    records = [
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_old.py",
            call_id="old-run",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "passed"},
        ),
        {
            "type": "assistant",
            "timestamp": (window_start + timedelta(seconds=1)).isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "subagent-edit",
                        "name": "Edit",
                        "input": {"file_path": str(tmp_path / "src" / "changed.py")},
                    }
                ],
            },
        },
        *_claude_tool_pair(
            command="uv run pytest tests/tasks/test_changed.py",
            call_id="subagent-run",
            start=window_start + timedelta(seconds=2),
            result={"exit_code": 0, "stdout": "1 passed"},
        ),
    ]
    for record in records:
        record.update({"sessionId": session.external_id, "agentId": "worker", "isSidechain": True})
    _write_jsonl(subagent, records)

    evidence = await derive_transcript_evidence(
        session,
        window_start,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert evidence.attempted_paths == (str(transcript), str(subagent))
    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [("src/changed.py", "Edit")]
    assert [(run.command, run.outcome) for run in evidence.validation_runs] == [
        ("uv run pytest tests/tasks/test_changed.py", "success")
    ]
    assert evidence.summary()["validation_run_count"] == 1
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
    )
    assert gate.passed


@pytest.mark.asyncio
async def test_claim_window_excludes_earlier_validation_runs(tmp_path: Path) -> None:
    transcript = tmp_path / "window.jsonl"
    _write_jsonl(
        transcript,
        [
            *_claude_tool_pair(
                command="pytest tests/old.py",
                call_id="old",
                start=BASE_TIME,
                result="passed",
            ),
            *_claude_tool_pair(
                command="pytest tests/new.py",
                call_id="new",
                start=BASE_TIME + timedelta(minutes=2),
                result="passed",
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME + timedelta(minutes=1),
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.command for run in evidence.validation_runs] == ["pytest tests/new.py"]


def _codex_response_item(payload: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    return {"type": "response_item", "timestamp": timestamp.isoformat(), "payload": payload}


def _codex_direct_exec_pair(
    *,
    command: str,
    result: Any,
    call_id: str = "direct-1",
) -> list[dict[str, Any]]:
    return [
        _codex_response_item(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command}),
            },
            BASE_TIME,
        ),
        _codex_response_item(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
            BASE_TIME + timedelta(seconds=1),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["npm ci", "unrecognized-check"])
@pytest.mark.parametrize(
    "source,exit_code,outcome",
    [
        ("claude", 0, "success"),
        ("claude", 1, "failure"),
        ("codex", 0, "success"),
        ("codex", 1, "failure"),
        ("codex", None, "unknown"),
    ],
)
async def test_shell_commands_without_validation_categories_remain_review_evidence(
    tmp_path: Path, source: str, command: str, exit_code: int | None, outcome: str
) -> None:
    result: dict[str, Any] = {"output": "command output"}
    if exit_code is not None:
        result["exit_code"] = exit_code
    records = (
        _codex_direct_exec_pair(command=command, result=result)
        if source == "codex"
        else _claude_tool_pair(command=command, call_id="run", start=BASE_TIME, result=result)
    )
    transcript = tmp_path / f"{source}-commands.jsonl"
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session(source, transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )
    merged = merge_transcript_evidence(evidence)
    # Claude records no exit code for a successful Bash call.
    recorded_exit = None if (source, exit_code) == ("claude", 0) else exit_code
    assert merged.validation_runs == ()
    assert [(run.command, run.outcome, run.exit_code) for run in merged.command_runs] == [
        (command, outcome, recorded_exit)
    ]
    assert bool(merged.degraded_capabilities) is (outcome == "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["claude", "codex-direct", "codex-nested"])
async def test_review_only_commands_do_not_retain_output(tmp_path: Path, shape: str) -> None:
    # Retained output is pickled back from the derivation pool; review-only runs
    # never feed an output-reading gate, so carrying it only stalls the loop.
    def pair(command: str, call_id: str) -> list[dict[str, Any]]:
        result = {"exit_code": 0, "output": f"{call_id} output"}
        if shape == "claude":
            return _claude_tool_pair(
                command=command, call_id=call_id, start=BASE_TIME, result=result
            )
        if shape == "codex-direct":
            return _codex_direct_exec_pair(command=command, result=result, call_id=call_id)
        return _codex_nested_exec_pair(command=command, result=result, call_id=call_id)

    transcript = tmp_path / f"{shape}.jsonl"
    _write_jsonl(
        transcript,
        [*pair("printf filler", "review"), *pair("uv run pytest tests/tasks -q", "validation")],
    )
    evidence = await derive_transcript_evidence(
        _session(shape.split("-")[0], transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )
    merged = merge_transcript_evidence(evidence)
    assert [(run.command, run.output) for run in merged.command_runs] == [("printf filler", None)]
    assert [run.command for run in merged.validation_runs] == ["uv run pytest tests/tasks -q"]
    assert "validation output" in (merged.validation_runs[0].output or "")


@pytest.mark.asyncio
async def test_codex_consumes_nested_exec_outcome_and_apply_patch_edit(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    patch = "*** Begin Patch\n*** Update File: src/changed.py\n@@\n-old\n+new\n*** End Patch\n"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        'const r = await tools.exec_command({cmd:"uv run pytest tests/tasks"}); '
                        "text(r);"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": json.dumps({"exit_code": 0, "output": "passed"}),
                },
                BASE_TIME + timedelta(seconds=1),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "patch-1",
                    "name": "apply_patch",
                    "input": patch,
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        ("uv run pytest tests/tasks", "success", 0)
    ]
    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("src/changed.py", "apply_patch")
    ]


def _pty_chunk(body: str) -> str:
    return f"Chunk ID: chunk-1\nWall time: 0.1 seconds\n{body}"


@pytest.mark.asyncio
async def test_write_stdin_command_reaches_close_evidence(tmp_path: Path) -> None:
    """A PTY opened as zsh must credit the command typed with write_stdin."""
    command = "cargo nextest run -p gobby-core --lib failed_case"
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "function_call",
                    "call_id": "exec-zsh",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "zsh", "tty": True}),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "function_call_output",
                    "call_id": "exec-zsh",
                    "output": _pty_chunk("Process running with session ID 42\n"),
                },
                BASE_TIME + timedelta(seconds=1),
            ),
            _codex_response_item(
                {
                    "type": "function_call",
                    "call_id": "stdin-red",
                    "name": "write_stdin",
                    "arguments": json.dumps({"session_id": 42, "chars": command + "\n"}),
                },
                BASE_TIME + timedelta(seconds=2),
            ),
            _codex_response_item(
                {
                    "type": "function_call_output",
                    "call_id": "stdin-red",
                    "output": _pty_chunk(
                        "Process exited with code 1\nOutput:\nassertion left == right failed\n"
                    ),
                },
                BASE_TIME + timedelta(seconds=3),
            ),
            _codex_response_item(
                {
                    "type": "function_call",
                    "call_id": "exec-zsh-green",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "zsh", "tty": True}),
                },
                BASE_TIME + timedelta(seconds=4),
            ),
            _codex_response_item(
                {
                    "type": "function_call_output",
                    "call_id": "exec-zsh-green",
                    "output": _pty_chunk("Process running with session ID 43\n"),
                },
                BASE_TIME + timedelta(seconds=5),
            ),
            _codex_response_item(
                {
                    "type": "function_call",
                    "call_id": "stdin-green",
                    "name": "write_stdin",
                    "arguments": json.dumps({"session_id": 43, "chars": command + "\n"}),
                },
                BASE_TIME + timedelta(seconds=6),
            ),
            _codex_response_item(
                {
                    "type": "function_call_output",
                    "call_id": "stdin-green",
                    "output": _pty_chunk("Process exited with code 0\nOutput:\n1 passed\n"),
                },
                BASE_TIME + timedelta(seconds=7),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, "failure", 1),
        (command, "success", 0),
    ]


async def test_codex_tracks_apply_patch_inside_functions_exec(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    patch = "*** Begin Patch\n*** Update File: src/changed.py\n@@\n-old\n+new\n*** End Patch\n"
    source = (
        f"const patch = {json.dumps(patch)};\n"
        "const result = await tools.apply_patch(patch); text(result);"
    )
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "wrapped-patch",
                    "name": "exec",
                    "input": source,
                },
                BASE_TIME,
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("src/changed.py", "functions.exec")
    ]


async def test_codex_ingests_unified_exec_failure_event(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    command = "uv run pytest tests/tasks/test_example.py::test_behavior -q"
    _write_jsonl(
        transcript,
        [
            {
                "type": "event_msg",
                "timestamp": BASE_TIME.isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-failed",
                        "command": ["/bin/zsh", "-lc", command],
                        "status": "failed",
                        "exit_code": 1,
                        "aggregated_output": "FAILED test_behavior - AssertionError",
                    },
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, "failure", 1)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["direct", "direct-native", "nested", "command-execution"])
async def test_codex_shell_runs_record_the_tool_workdir(tmp_path: Path, shape: str) -> None:
    # Close coverage resolves pytest and vitest targets from where the run executed (#23653).
    command = "uv run pytest tests/tasks -q"
    workdir = "/repo dir/web"
    arguments = {"cmd": command, "workdir": workdir, "yield_time_ms": 10000}
    records: list[dict[str, Any]]
    if shape == "direct":
        records = _codex_direct_exec_pair(command=command, result={"exit_code": 0, "output": "ok"})
        records[0]["payload"]["arguments"] = json.dumps(arguments)
    elif shape == "direct-native":
        native = "Chunk ID: c1\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\nok\n"
        records = _codex_direct_exec_pair(command=command, result=native)
        records[0]["payload"]["arguments"] = json.dumps(arguments)
    elif shape == "nested":
        records = _codex_nested_exec_pair(command=command, result={"exit_code": 0, "output": "ok"})
        records[0]["payload"]["input"] = (
            f"const r = await tools.exec_command({json.dumps(arguments)}); text(r);"
        )
    else:
        records = [
            {
                "type": "event_msg",
                "timestamp": BASE_TIME.isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-1",
                        "command": ["/bin/zsh", "-lc", command],
                        "cwd": "file:///repo%20dir/web",
                        "exit_code": 0,
                        "aggregated_output": "ok",
                    },
                },
            }
        ]
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.workdir) for run in evidence.validation_runs] == [
        (command, "success", workdir)
    ]


@pytest.mark.asyncio
async def test_claude_shell_runs_record_the_calling_entry_cwd(tmp_path: Path) -> None:
    # A persisted `cd` shows up as the entry cwd, so the run is located there (#23653).
    command = "uv run pytest tests/tasks -q"
    records = _claude_tool_pair(command=command, call_id="toolu_1", start=BASE_TIME, result="ok")
    records[0]["cwd"] = "/repo dir/web"
    records[1]["cwd"] = "/after the run"
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.workdir) for run in evidence.validation_runs] == [
        (command, "success", "/repo dir/web")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tree", "uncovered"), [("export", ()), ("unrelated", ("tests/test_a.py",))]
)
async def test_claude_cd_into_an_identical_export_credits_the_close(
    tmp_path: Path, tree: str, uncovered: tuple[str, ...]
) -> None:
    # A Claude Bash call keeps its leading `cd`, so the close resolves the target there.
    test = "tests/test_a.py"
    for checkout in ("repo", "export", "unrelated"):
        (tmp_path / checkout / "tests").mkdir(parents=True)
    for checkout in ("repo", "export"):
        (tmp_path / checkout / test).write_text("def test_a(): pass\n")
    command = f"cd {tmp_path / tree} && uv run pytest {test} -q"
    records = _claude_tool_pair(
        command=command, call_id="toolu_1", start=BASE_TIME, result="1 passed in 0.01s"
    )
    for record in records:
        record["cwd"] = str(tmp_path / "repo")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, records)

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path / "repo"),
    )

    assert [run.command for run in evidence.validation_runs] == [command]
    assert (
        uncovered_pytest_paths(
            evidence.validation_runs,
            (test,),
            close_root=str(tmp_path / "repo"),
            changed_paths=(test,),
        )
        == uncovered
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,rewritten",
    [
        (
            "uv run pytest tests/tasks/test_example.py::test_behavior -q",
            "uv run rtk pytest tests/tasks/test_example.py::test_behavior -q",
        ),
        ("npm ci", "rtk npm ci"),
    ],
)
async def test_codex_authoritative_exec_supersedes_successful_outer_wrapper(
    tmp_path: Path,
    command: str,
    rewritten: str,
) -> None:
    transcript = tmp_path / "codex-wrapper.jsonl"
    failed_output = "FAILED test_behavior - AssertionError"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME,
            ),
            {
                "type": "event_msg",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "native-exec",
                        "command": ["/bin/zsh", "-lc", rewritten],
                        "status": "failed",
                        "exit_code": 1,
                        "aggregated_output": failed_output,
                    },
                },
            },
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                        },
                        {"type": "input_text", "text": failed_output},
                    ],
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    runs = evidence.command_runs if command == "npm ci" else evidence.validation_runs
    assert [(run.command, run.outcome, run.exit_code) for run in runs] == [
        (rewritten, "failure", 1)
    ]


@pytest.mark.asyncio
async def test_codex_completed_cell_alone_is_not_a_command_pass(tmp_path: Path) -> None:
    """A completed cell that printed only prose never credits its exec (#23724)."""
    command = "uv run pytest tests/tasks/test_example.py -q"
    transcript = tmp_path / "codex-wrapper-only.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                        },
                        {"type": "input_text", "text": "5 passed in 0.4s\n"},
                    ],
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, "unknown", None)
    ]


@pytest.mark.asyncio
async def test_codex_exec_stdout_cannot_forge_its_own_exit_code(tmp_path: Path) -> None:
    """A cell printing the command's stdout never credits a result that stdout spelled (#23724)."""
    command = "uv run pytest tests/tasks/test_example.py -q"
    transcript = tmp_path / "codex-stdout-forged-exit.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                        },
                        {"type": "input_text", "text": '{"exit_code":0}'},
                    ],
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, "unknown", None)
    ]


@pytest.mark.asyncio
async def test_codex_inline_printed_exec_and_poll_results_credit_the_command(
    tmp_path: Path,
) -> None:
    """Cells printing the awaited exec and poll results whole credit the poll's exit (#23724)."""
    command = "uv run gobby test-types audit tests/x.py --baseline b.json --fail-on-new"
    transcript = tmp_path / "codex-inline-printed-chain.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "exec-cell",
                    "name": "exec",
                    "input": (
                        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
                        "yield_time_ms:1000}));\n"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "exec-cell",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 2.5 seconds\nOutput:\n",
                        },
                        {
                            "type": "input_text",
                            "text": json.dumps({"chunk_id": "5ed45b", "session_id": 40084}),
                        },
                    ],
                },
                BASE_TIME + timedelta(seconds=2),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "poll-cell",
                    "name": "exec",
                    "input": (
                        'text(await tools.write_stdin({session_id:40084,chars:"",'
                        "yield_time_ms:30000}));\n"
                    ),
                },
                BASE_TIME + timedelta(seconds=3),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "poll-cell",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 7.4 seconds\nOutput:\n",
                        },
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {"chunk_id": "eea070", "exit_code": 0, "output": "Errors: 0\n"}
                            ),
                        },
                    ],
                },
                BASE_TIME + timedelta(seconds=10),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (command, "success", 0)
    ]


async def test_codex_tdd_gate_accepts_targeted_test_body_exception_before_production_edit(
    tmp_path: Path,
) -> None:
    """RTK's Codex summary names the method frame, while the artifact names its class."""
    transcript = tmp_path / "codex-tdd.jsonl"
    test_path = "tests/hooks/test_session_coordinator.py"
    source_path = "src/gobby/hooks/session_coordinator.py"
    node_id = (
        f"{test_path}::TestAgentRunCompletion::"
        "test_complete_agent_run_uses_transcript_activity_and_persists_counts"
    )
    command = f"uv run pytest {node_id} -q"
    rewritten = f"uv run rtk pytest {node_id} -q"
    red_output = """\
Pytest: 0 passed, 1 failed

Failures:
     tests/hooks/test_session_coordinator.py:606: in test_complete_agent_run_uses_transcript_activity_...
     E   TypeError: SessionCoordinator.__init__() got an unexpected keyword argument 'transcript_reader'
"""
    test_patch = (
        "*** Begin Patch\n"
        f"*** Update File: /deleted/worktree/{test_path}\n"
        "@@\n"
        "-        pass\n"
        "+        assert updated.status == 'success'\n"
        "*** End Patch\n"
    )
    source_patch = (
        "*** Begin Patch\n"
        f"*** Update File: /deleted/worktree/{source_path}\n"
        "@@\n"
        "-        self.session_storage = session_storage\n"
        "+        self.session_storage = session_storage\n"
        "+        self.transcript_reader = transcript_reader\n"
        "*** End Patch\n"
    )
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "test-edit",
                    "name": "exec",
                    "input": (
                        f"const patch = {json.dumps(test_patch)}; await tools.apply_patch(patch);"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "test-edit",
                    "output": "Done!",
                },
                BASE_TIME + timedelta(seconds=1),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "red-run",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME + timedelta(seconds=2),
            ),
            {
                "type": "event_msg",
                "timestamp": (BASE_TIME + timedelta(seconds=3)).isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "red-native",
                        "command": ["/bin/zsh", "-lc", rewritten],
                        "status": "failed",
                        "exit_code": 1,
                        "aggregated_output": red_output,
                    },
                },
            },
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "red-run",
                    "output": red_output,
                },
                BASE_TIME + timedelta(seconds=4),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "source-edit",
                    "name": "exec",
                    "input": (
                        f"const patch = {json.dumps(source_patch)}; await tools.apply_patch(patch);"
                    ),
                },
                BASE_TIME + timedelta(seconds=5),
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "source-edit",
                    "output": "Done!",
                },
                BASE_TIME + timedelta(seconds=6),
            ),
            _codex_response_item(
                {
                    "type": "function_call",
                    "call_id": "green-run",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": command}),
                },
                BASE_TIME + timedelta(seconds=7),
            ),
            _codex_response_item(
                {
                    "type": "function_call_output",
                    "call_id": "green-run",
                    "output": {"exit_code": 0, "output": "Pytest: 1 passed"},
                },
                BASE_TIME + timedelta(seconds=8),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, source_path},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::TestAgentRunCompletion",
        path=test_path,
        symbol="TestAgentRunCompletion",
        body="class TestAgentRunCompletion: ...",
    )
    result = evaluate_tdd_evidence((test,), evidence)
    test_edit = next(edit for edit in evidence.edits if edit.path == test_path)
    source_edit = next(edit for edit in evidence.edits if edit.path == source_path)
    red = next(run for run in evidence.validation_runs if run.outcome == "failure")
    green = next(run for run in evidence.validation_runs if run.outcome == "success")

    assert test_edit.order < red.order < source_edit.order < green.order
    assert result.passed is True, result
    assert result.red_runs == (rewritten,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recall_command", "retrieval", "duplicate_red", "expected"),
    [
        pytest.param("rtk recall d1965a04eb04", "success", False, True, id="original-receipt"),
        pytest.param(
            "rtk recall d1965a04eb04", "indented", False, True, id="indented-native-reference"
        ),
        pytest.param("rtk recall d1965a04eb04", "embedded", False, False, id="embedded-reference"),
        pytest.param("rtk recall d1965a04eb04", "large", False, True, id="bounded-large-recall"),
        pytest.param("rtk recall d1965a04eb04", "oversized", False, False, id="oversized-recall"),
        pytest.param("rtk recall 0123456789ab", "success", False, False, id="wrong-id"),
        pytest.param("rtk recall d1965a04eb04", "failed", False, False, id="failed-retrieval"),
        pytest.param("rtk recall d1965a04eb04", "blocked", False, False, id="unexecuted"),
        pytest.param("rtk recall d1965a04eb04 | cat", "success", False, False, id="wrapped"),
        pytest.param("rtk recall d1965a04eb04", "success", True, False, id="ambiguous-id"),
    ],
)
async def test_rtk_recall_preserves_original_red_chronology_and_exact_reference(
    tmp_path: Path, recall_command: str, retrieval: str, duplicate_red: bool, expected: bool
) -> None:
    path = "tests/hooks/test_session_coordinator.py"
    command = f"uv run pytest {path}::test_original -q --tb=line"
    compacted = "Pytest: 0 passed, 1 failed\n[full output: rtk recall d1965a04eb04]"
    if retrieval == "indented":
        compacted = compacted.replace("\n[full output:", "\n  [full output:")
    elif retrieval == "embedded":
        compacted = compacted.replace("\n[full output:", "\nquoted [full output:")
    raw = f"{path}:3: AssertionError: assert False\n1 failed in 0.01s"
    if retrieval in {"large", "oversized"}:
        raw = "Captured setup diagnostic\n" * (1000 if retrieval == "large" else 3000) + raw
    before = await _derive_claude_tdd_cycle(
        tmp_path, red_command=command, red_output=compacted, green_command=command
    )
    original = next(run for run in before.validation_runs if run.outcome == "failure")
    transcript = tmp_path / "claude-tdd.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    if duplicate_red:
        records.extend(
            _claude_tool_pair(
                command=command,
                call_id="duplicate-red",
                start=BASE_TIME + timedelta(seconds=6),
                result={"exit_code": 1, "stdout": compacted},
                is_error=True,
            )
        )
    # The native tool's is_error=false certifies retrieval success; the recalled
    # original pytest output still reports a failure and has no process-exit envelope.
    records.extend(
        _claude_tool_pair(
            command=recall_command,
            call_id="recall",
            start=BASE_TIME + timedelta(seconds=8),
            result=_HOOK_BLOCKED if retrieval == "blocked" else raw,
            is_error=retrieval not in {"success", "indented", "embedded", "large", "oversized"},
        )
    )
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {path, "src/gobby/hooks/session_coordinator.py"},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{path}::test_original",
        path=path,
        symbol="test_original",
        body="def test_original():\n    assert False\n",
    )
    result = evaluate_tdd_evidence((test,), evidence)
    red = evidence.validation_runs[0]
    source_edit = next(edit for edit in evidence.edits if edit.path.startswith("src/"))

    assert result.passed is expected, result
    assert (red.started_at, red.completed_at, red.order) == (
        original.started_at,
        original.completed_at,
        original.order,
    )
    assert red.order < source_edit.order
    if expected:
        receipt = next(run for run in evidence.command_runs if run.command == recall_command)
        assert red.output == receipt.output
        assert raw in (red.output or "")
        assert red.output_recovered_from == recall_command
        assert red.output_recovered_at == receipt.completed_at
    else:
        assert red.output == original.output


@pytest.mark.asyncio
async def test_recovered_rtk_reference_remains_ambiguous_for_a_later_duplicate(
    tmp_path: Path,
) -> None:
    path = "tests/hooks/test_session_coordinator.py"
    command = f"uv run pytest {path}::test_original -q --tb=line"
    compacted = "Pytest: 0 passed, 1 failed\n[full output: rtk recall d1965a04eb04]"
    raw = f"{path}:3: AssertionError: assert False\n1 failed"
    await _derive_claude_tdd_cycle(
        tmp_path, red_command=command, red_output=compacted, green_command=command
    )
    transcript = tmp_path / "claude-tdd.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    records.extend(
        _claude_tool_pair(
            command="rtk recall d1965a04eb04",
            call_id="first-recall",
            start=BASE_TIME + timedelta(seconds=8),
            result=raw,
        )
    )
    records.extend(
        _claude_tool_pair(
            command=command,
            call_id="later-red",
            start=BASE_TIME + timedelta(seconds=10),
            result={"exit_code": 1, "stdout": compacted},
            is_error=True,
        )
    )
    records.extend(
        _claude_tool_pair(
            command="uv run rtk recall d1965a04eb04",
            call_id="second-recall",
            start=BASE_TIME + timedelta(seconds=12),
            result=raw,
        )
    )
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {path, "src/gobby/hooks/session_coordinator.py"},
        str(tmp_path),
    )
    reds = [run for run in evidence.validation_runs if run.outcome == "failure"]
    assert len(reds) == 2
    assert reds[0].output_recovered_from == "rtk recall d1965a04eb04"
    assert reds[1].output_recovered_from is None
    assert compacted in (reds[1].output or "")


async def test_codex_native_recall_keeps_the_failed_execution_outcome(tmp_path: Path) -> None:
    transcript = tmp_path / "codex-recall.jsonl"
    compacted = "Pytest: 0 passed, 1 failed\n[full output: rtk recall d1965a04eb04]"
    raw = "tests/test_original.py:3: AssertionError: assert False\n1 failed in 0.01s"
    records = _codex_direct_exec_pair(
        command="uv run pytest tests/test_original.py -q",
        call_id="original-red",
        result=_pty_chunk(f"Process exited with code 1\nOutput:\n{compacted}"),
    )
    recalled = _codex_direct_exec_pair(
        command="rtk recall d1965a04eb04",
        call_id="recall",
        result=_pty_chunk(f"Process exited with code 0\nOutput:\n{raw}"),
    )
    recalled[0]["timestamp"] = (BASE_TIME + timedelta(seconds=2)).isoformat()
    recalled[1]["timestamp"] = (BASE_TIME + timedelta(seconds=3)).isoformat()
    _write_jsonl(transcript, [*records, *recalled])
    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    (red,) = evidence.validation_runs
    (receipt,) = evidence.command_runs
    assert (red.outcome, red.exit_code) == ("failure", 1)
    assert (receipt.outcome, receipt.exit_code) == ("success", 0)
    assert red.order < receipt.order
    assert red.output == receipt.output
    assert raw in (red.output or "")
    assert red.output_recovered_from == receipt.command
    assert red.output_recovered_at == receipt.completed_at


_RED4_COMMAND = (
    "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
    "GOBBY_TEST_PROTECT=1 uv run --directory "
    "/Users/josh/.gobby/worktrees/gobby/fix-23194-srt-shared-cargo-home pytest "
    "tests/agents/test_sandbox_policy.py::test_run_paths_refuse_a_sandbox_cache_entry_linked_outside "
    "tests/agents/test_sandbox_policy.py::test_run_paths_refuse_a_sandbox_cache_root_linked_outside "
    "tests/agents/test_srt_runtime.py::test_prepare_srt_launch_refuses_a_cargo_home_swapped_in_at_render "
    "tests/agents/test_cargo_target.py::test_cleanup_keeps_what_a_linked_sandbox_target_points_to "
    "-q -p no:randomly --tb=line"
)
_RED4_WT = "/Users/josh/.gobby/worktrees/gobby/fix-23194-srt-shared-cargo-home"
_RED4_TMP = "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/pytest-of-josh/pytest-3268"
_RED4_PATHLIB = (
    "/Users/josh/.local/share/uv/python/cpython-3.14.3-macos-aarch64-none/lib/python3.14/"
    "pathlib/__init__.py:1011"
)
_RED4_HOME_EXISTS = (
    "FileExistsError: [Errno 17] File exists: "
    f"'{_RED4_TMP}/test_run_paths_refuse_a_sandbo1/gobby-home/cache/sandbox/cargo-home'"
)
_RED4_TARGET_EXISTS = (
    "FileExistsError: [Errno 17] File exists: "
    f"'{_RED4_TMP}/test_run_paths_refuse_a_sandbo3/gobby-home/cache/sandbox/cargo-target/"
    "workspace-44a3a53692b7b56a'"
)
_RED4_DID_NOT_RAISE = "Failed: DID NOT RAISE <class 'PermissionError'>"
_RED4_SWAPPED = "Failed: no policy may be rendered for a swapped cache grant"
_RED4_FAILED = (
    "FAILED tests/agents/test_sandbox_policy.py::"
    "test_run_paths_refuse_a_sandbox_cache_entry_linked_outside[cargo_home-existing]",
    "FAILED tests/agents/test_sandbox_policy.py::"
    "test_run_paths_refuse_a_sandbox_cache_entry_linked_outside[cargo_home-dangling]",
    "FAILED tests/agents/test_sandbox_policy.py::"
    "test_run_paths_refuse_a_sandbox_cache_entry_linked_outside[cargo_target-existing]",
    "FAILED tests/agents/test_sandbox_policy.py::"
    "test_run_paths_refuse_a_sandbox_cache_entry_linked_outside[cargo_target-dangling]",
    "FAILED tests/agents/test_sandbox_policy.py::"
    "test_run_paths_refuse_a_sandbox_cache_root_linked_outside",
    "FAILED tests/agents/test_srt_runtime.py::"
    "test_prepare_srt_launch_refuses_a_cargo_home_swapped_in_at_render[before-grants]",
    "FAILED tests/agents/test_srt_runtime.py::"
    "test_prepare_srt_launch_refuses_a_cargo_home_swapped_in_at_render[after-grants]",
)
_RED4_TOTAL = "=================== 7 failed, 1 passed in 133.49s (0:02:13) ===================="
# #23194's red4 as RTK left it in the transcript: no FAILURES or summary headers,
# both pathlib locations dropped, one path truncated, and blank-line separators.
_RED4_RTK = "\n\n".join(
    (
        "Exit code 1",
        f"E   {_RED4_DID_NOT_RAISE}",
        f"{_RED4_WT}/tests/agents/test_sandbox_policy.py:566: {_RED4_DID_NOT_RAISE}",
        f"E   {_RED4_HOME_EXISTS}",
        f"E   {_RED4_DID_NOT_RAISE}",
        f"{_RED4_WT}/tests/agents/test_sandbox_policy.py:566: {_RED4_DID_NOT_RAISE}",
        f"E   FileExistsError: [Errno 17] File exists: '{_RED4_TMP}/"
        "test_run_paths_refuse_a_sandbo3/gobby-home/cache/sandbox/cargo-targ...",
        f"E   {_RED4_DID_NOT_RAISE}",
        f"{_RED4_WT}/tests/agents/test_sandbox_policy.py:586: {_RED4_DID_NOT_RAISE}",
        f"E   {_RED4_SWAPPED}",
        f"{_RED4_WT}/tests/agents/test_srt_runtime.py:990: {_RED4_SWAPPED}",
        f"E   {_RED4_SWAPPED}",
        f"{_RED4_WT}/tests/agents/test_srt_runtime.py:990: {_RED4_SWAPPED}",
        *_RED4_FAILED,
        _RED4_TOTAL,
        "[full output: rtk recall 50d2f60a82a5]",
    )
)
# `rtk recall 50d2f60a82a5`: the same run's raw pytest output.
_RED4_RAW = "\n".join(
    (
        "============================= test session starts ==============================",
        "platform darwin -- Python 3.14.3, pytest-9.0.3, pluggy-1.6.0",
        f"rootdir: {_RED4_WT}",
        "configfile: pyproject.toml",
        "plugins: mock-3.15.1, xdist-3.8.0, timeout-2.4.0, anyio-4.14.2, asyncio-1.3.0, cov-7.0.0",
        "asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, "
        "asyncio_default_test_loop_scope=function",
        "collected 8 items",
        "",
        "tests/agents/test_sandbox_policy.py FFFFF                                [ 62%]",
        "tests/agents/test_srt_runtime.py FF                                      [ 87%]",
        "tests/agents/test_cargo_target.py .                                      [100%]",
        "",
        "=================================== FAILURES ===================================",
        f"E   {_RED4_DID_NOT_RAISE}",
        f"{_RED4_WT}/tests/agents/test_sandbox_policy.py:566: {_RED4_DID_NOT_RAISE}",
        f"E   {_RED4_HOME_EXISTS}",
        f"{_RED4_PATHLIB}: {_RED4_HOME_EXISTS}",
        f"E   {_RED4_DID_NOT_RAISE}",
        f"{_RED4_WT}/tests/agents/test_sandbox_policy.py:566: {_RED4_DID_NOT_RAISE}",
        f"E   {_RED4_TARGET_EXISTS}",
        f"{_RED4_PATHLIB}: {_RED4_TARGET_EXISTS}",
        f"E   {_RED4_DID_NOT_RAISE}",
        f"{_RED4_WT}/tests/agents/test_sandbox_policy.py:586: {_RED4_DID_NOT_RAISE}",
        f"E   {_RED4_SWAPPED}",
        f"{_RED4_WT}/tests/agents/test_srt_runtime.py:990: {_RED4_SWAPPED}",
        f"E   {_RED4_SWAPPED}",
        f"{_RED4_WT}/tests/agents/test_srt_runtime.py:990: {_RED4_SWAPPED}",
        "=========================== short test summary info ============================",
        *_RED4_FAILED,
        _RED4_TOTAL,
    )
)


@pytest.mark.parametrize(
    "test_path,symbol,recalled,expected",
    [
        pytest.param(
            "tests/agents/test_sandbox_policy.py",
            "test_run_paths_refuse_a_sandbox_cache_entry_linked_outside",
            False,
            "no attributable failure section for "
            "'test_run_paths_refuse_a_sandbox_cache_entry_linked_outside'",
            id="rtk-form-unpaired",
        ),
        pytest.param(
            "tests/agents/test_sandbox_policy.py",
            "test_run_paths_refuse_a_sandbox_cache_entry_linked_outside",
            True,
            None,
            id="recalled-did-not-raise",
        ),
        pytest.param(
            "tests/agents/test_sandbox_policy.py",
            "test_run_paths_refuse_a_sandbox_cache_root_linked_outside",
            True,
            None,
            id="recalled-sole-section",
        ),
        pytest.param(
            "tests/agents/test_srt_runtime.py",
            "test_prepare_srt_launch_refuses_a_cargo_home_swapped_in_at_render",
            True,
            None,
            id="recalled-pytest-fail",
        ),
    ],
)
async def test_red4_23194_tb_line_red_is_credited_from_its_recalled_output(
    tmp_path: Path, test_path: str, symbol: str, recalled: bool, expected: str | None
) -> None:
    source_path = "src/gobby/agents/sandbox_policy.py"
    await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=_RED4_COMMAND,
        red_output=_RED4_RTK,
        green_command=_RED4_COMMAND,
        test_path=test_path,
        source_path=source_path,
    )
    transcript = tmp_path / "claude-tdd.jsonl"
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    if recalled:
        # The recall followed the implementation edit, as #23194's did.
        records.extend(
            _claude_tool_pair(
                command="rtk recall 50d2f60a82a5",
                call_id="recall",
                start=BASE_TIME + timedelta(seconds=8),
                result=_RED4_RAW,
            )
        )
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, source_path},
        str(tmp_path),
    )
    test = AcceptanceTest(
        reference=f"{test_path}::{symbol}",
        path=test_path,
        symbol=symbol,
        body=f"def {symbol}():\n    ...\n",
    )

    result = evaluate_tdd_evidence((test,), evidence)

    red = next(run for run in evidence.validation_runs if run.outcome == "failure")
    source_edit = next(edit for edit in evidence.edits if edit.path == source_path)
    assert red.order < source_edit.order
    if expected is None:
        assert result.passed is True, result
        assert result.red_runs == (_RED4_COMMAND,)
        assert red.output_recovered_from == "rtk recall 50d2f60a82a5"
    else:
        assert result.passed is False
        assert any(expected in finding for finding in result.findings), result.findings


async def _derive_claude_tdd_cycle(
    tmp_path: Path,
    *,
    red_command: str,
    red_output: str,
    green_command: str,
    test_path: str = "tests/hooks/test_session_coordinator.py",
    source_path: str = "src/gobby/hooks/session_coordinator.py",
) -> TranscriptEvidence:
    transcript = tmp_path / "claude-tdd.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "test-edit",
                            "name": "Edit",
                            "input": {"file_path": str(tmp_path / test_path)},
                        }
                    ],
                },
            },
            *_claude_tool_pair(
                command=red_command,
                call_id="red-run",
                start=BASE_TIME + timedelta(seconds=1),
                result={"exit_code": 1, "stdout": red_output},
                is_error=True,
            ),
            {
                "type": "assistant",
                "timestamp": (BASE_TIME + timedelta(seconds=3)).isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "source-edit",
                            "name": "Edit",
                            "input": {"file_path": str(tmp_path / source_path)},
                        }
                    ],
                },
            },
            *_claude_tool_pair(
                command=green_command,
                call_id="green-run",
                start=BASE_TIME + timedelta(seconds=4),
                result={"exit_code": 0, "stdout": "Pytest: 1 passed"},
            ),
        ],
    )
    return await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, source_path},
        str(tmp_path),
    )


@pytest.mark.parametrize(
    "method,red_output",
    [
        pytest.param(
            "test_activity_via_helper",
            """\
________ TestAgentRunCompletion.test_activity_via_helper ________
    def test_activity_via_helper() -> None:
>       _helper()
/deleted/worktree/tests/hooks/test_session_coordinator.py:606:
    def _helper() -> None:
>       raise TypeError("unexpected keyword argument 'transcript_reader'")
E       TypeError: unexpected keyword argument 'transcript_reader'
/deleted/worktree/tests/hooks/test_session_coordinator.py:590: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::TestAgentRunCompletion::test_activity_via_helper
""",
            id="helper-raised-type-error",
        ),
        pytest.param(
            "test_activity_direct",
            """\
__________ TestAgentRunCompletion.test_activity_direct __________
    def test_activity_direct() -> None:
>       raise TypeError("direct")
E       TypeError: direct
/deleted/worktree/tests/hooks/test_session_coordinator.py:620: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::TestAgentRunCompletion::test_activity_direct
""",
            id="direct-type-error",
        ),
    ],
)
async def test_claude_tdd_gate_accepts_default_pytest_test_body_exception(
    tmp_path: Path,
    method: str,
    red_output: str,
) -> None:
    test_path = "tests/hooks/test_session_coordinator.py"
    source_path = "src/gobby/hooks/session_coordinator.py"
    node_id = f"{test_path}::TestAgentRunCompletion::{method}"
    command = f"uv run pytest {node_id} -q"
    evidence = await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=command,
        red_output=red_output,
        green_command=command,
    )
    test = AcceptanceTest(
        reference=f"{test_path}::TestAgentRunCompletion",
        path=test_path,
        symbol="TestAgentRunCompletion",
        body="class TestAgentRunCompletion: ...",
    )
    result = evaluate_tdd_evidence((test,), evidence)
    test_edit = next(edit for edit in evidence.edits if edit.path == test_path)
    source_edit = next(edit for edit in evidence.edits if edit.path == source_path)
    red = next(run for run in evidence.validation_runs if run.outcome == "failure")
    green = next(run for run in evidence.validation_runs if run.outcome == "success")

    assert test_edit.order < red.order < source_edit.order < green.order
    assert result.passed is True, result
    assert result.red_runs == (command,)


async def test_tdd_gate_credits_uv_directory_before_run(tmp_path: Path) -> None:
    test_path = "tests/hooks/test_session_coordinator.py"
    node_id = f"{test_path}::TestAgentRunCompletion::test_activity_direct"
    command = f"uv --directory {tmp_path} run pytest {node_id} -q"
    evidence = await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=command,
        red_output="""\
__________ TestAgentRunCompletion.test_activity_direct __________
    def test_activity_direct() -> None:
>       raise TypeError("direct")
E       TypeError: direct
/deleted/worktree/tests/hooks/test_session_coordinator.py:620: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::TestAgentRunCompletion::test_activity_direct
""",
        green_command=command,
    )
    test = AcceptanceTest(
        reference=f"{test_path}::TestAgentRunCompletion",
        path=test_path,
        symbol="TestAgentRunCompletion",
        body="class TestAgentRunCompletion: ...",
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert evidence.command_runs == ()
    assert [(run.outcome, run.categories) for run in evidence.validation_runs] == [
        ("failure", ("test",)),
        ("success", ("test",)),
    ]
    assert result.passed is True, result
    assert result.red_runs == (command,)


@pytest.mark.parametrize(
    "selection",
    [
        pytest.param("tests/hooks/test_session_coordinator.py", id="file-selection"),
        pytest.param(
            "tests/hooks/test_session_coordinator.py::TestAgentRunCompletion",
            id="class-selection",
        ),
        pytest.param(
            "tests/hooks/test_session_coordinator.py -k test_activity_direct",
            id="keyword-selection",
        ),
    ],
)
async def test_claude_tdd_gate_attributes_default_pytest_header_without_node_selection(
    tmp_path: Path,
    selection: str,
) -> None:
    """The FAILURES header names the test exactly; the command need not select its node."""
    test_path = "tests/hooks/test_session_coordinator.py"
    command = f"uv run pytest {selection} -q"
    red_output = """\
__________ TestAgentRunCompletion.test_activity_direct __________
    def test_activity_direct() -> None:
>       raise TypeError("direct")
E       TypeError: direct
/deleted/worktree/tests/hooks/test_session_coordinator.py:620: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::TestAgentRunCompletion::test_activity_direct
"""
    evidence = await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=command,
        red_output=red_output,
        green_command=command,
    )
    test = AcceptanceTest(
        reference=f"{test_path}::TestAgentRunCompletion.test_activity_direct",
        path=test_path,
        symbol="TestAgentRunCompletion.test_activity_direct",
        body="def test_activity_direct(self) -> None: ...",
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True, result
    assert result.red_runs == (command,)


async def test_tdd_gate_does_not_borrow_default_pytest_exception_from_sibling_test(
    tmp_path: Path,
) -> None:
    test_path = "tests/hooks/test_session_coordinator.py"
    target = f"{test_path}::TestAgentRunCompletion::test_target"
    sibling = f"{test_path}::TestOther::test_sibling"
    command = f"uv run pytest {target} {sibling} -q"
    sibling_output = """\
________________________ TestOther.test_sibling _________________________
    def test_sibling() -> None:
>       raise TypeError("sibling")
E       TypeError: sibling
/deleted/worktree/tests/hooks/test_session_coordinator.py:700: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::TestOther::test_sibling
"""
    test = AcceptanceTest(
        reference=f"{test_path}::TestAgentRunCompletion",
        path=test_path,
        symbol="TestAgentRunCompletion",
        body="class TestAgentRunCompletion: ...",
    )
    evidence = await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=command,
        red_output=sibling_output,
        green_command=f"uv run pytest {target} -q",
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert result.red_runs == ()


async def test_tdd_gate_does_not_borrow_sibling_header_from_file_selection(
    tmp_path: Path,
) -> None:
    test_path = "tests/hooks/test_session_coordinator.py"
    command = f"uv run pytest {test_path} -q"
    sibling_output = """\
________________________ TestOther.test_sibling _________________________
    def test_sibling() -> None:
>       raise TypeError("sibling")
E       TypeError: sibling
/deleted/worktree/tests/hooks/test_session_coordinator.py:700: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::TestOther::test_sibling
"""
    test = AcceptanceTest(
        reference=f"{test_path}::TestAgentRunCompletion",
        path=test_path,
        symbol="TestAgentRunCompletion",
        body="class TestAgentRunCompletion: ...",
    )
    evidence = await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=command,
        red_output=sibling_output,
        green_command=command,
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is False
    assert result.red_runs == ()


async def test_tdd_gate_attributes_unqualified_header_among_multiple_selected_tests(
    tmp_path: Path,
) -> None:
    test_path = "tests/hooks/test_session_coordinator.py"
    target = f"{test_path}::test_target"
    sibling = f"{test_path}::test_sibling"
    command = f"uv run pytest {target} {sibling} -q"
    target_output = """\
______________________________ test_target ______________________________
    def test_target() -> None:
>       raise TypeError("target")
E       TypeError: target
/deleted/worktree/tests/hooks/test_session_coordinator.py:710: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::test_target
"""
    test = AcceptanceTest(
        reference=target,
        path=test_path,
        symbol="test_target",
        body="def test_target(): ...",
    )
    evidence = await _derive_claude_tdd_cycle(
        tmp_path,
        red_command=command,
        red_output=target_output,
        green_command=f"uv run pytest {target} -q",
    )

    result = evaluate_tdd_evidence((test,), evidence)

    assert result.passed is True, result
    assert result.red_runs == (command,)


def _heredoc_write(path: str) -> str:
    """A Bash python heredoc that writes one repository file."""
    return f"python3 - <<'PY'\nfrom pathlib import Path\nPath({path!r}).write_text('x')\nPY"


async def _derive_claude_heredoc_tdd_cycle(
    tmp_path: Path,
    *,
    test_path: str,
    source_path: str,
    command: str,
    red_output: str,
) -> TranscriptEvidence:
    transcript = tmp_path / "claude-heredoc-tdd.jsonl"
    _write_jsonl(
        transcript,
        [
            *_claude_tool_pair(
                command=_heredoc_write(test_path),
                call_id="test-write",
                start=BASE_TIME,
                result={"exit_code": 0, "stdout": ""},
            ),
            *_claude_tool_pair(
                command=command,
                call_id="red-run",
                start=BASE_TIME + timedelta(seconds=2),
                result={"exit_code": 1, "stdout": red_output},
                is_error=True,
            ),
            *_claude_tool_pair(
                command=_heredoc_write(source_path),
                call_id="source-write",
                start=BASE_TIME + timedelta(seconds=4),
                result={"exit_code": 0, "stdout": ""},
            ),
            *_claude_tool_pair(
                command=command,
                call_id="green-run",
                start=BASE_TIME + timedelta(seconds=6),
                result={"exit_code": 0, "stdout": "Pytest: 1 passed"},
            ),
        ],
    )
    return await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {test_path, source_path},
        str(tmp_path),
    )


async def test_tdd_gate_credits_heredoc_written_test_and_production_edits(
    tmp_path: Path,
) -> None:
    test_path = "tests/hooks/test_session_coordinator.py"
    source_path = "src/gobby/hooks/session_coordinator.py"
    command = f"uv run pytest {test_path}::test_target -q"
    red_output = """\
______________________________ test_target ______________________________
    def test_target() -> None:
>       raise TypeError("target")
E       TypeError: target
/deleted/worktree/tests/hooks/test_session_coordinator.py:12: TypeError
=========================== short test summary info ============================
FAILED tests/hooks/test_session_coordinator.py::test_target
"""
    evidence = await _derive_claude_heredoc_tdd_cycle(
        tmp_path,
        test_path=test_path,
        source_path=source_path,
        command=command,
        red_output=red_output,
    )
    test = AcceptanceTest(
        reference=f"{test_path}::test_target",
        path=test_path,
        symbol="test_target",
        body="def test_target(): ...",
    )

    result = evaluate_tdd_evidence((test,), evidence)
    red = next(run for run in evidence.validation_runs if run.outcome == "failure")
    green = next(run for run in evidence.validation_runs if run.outcome == "success")

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        (test_path, "Bash"),
        (source_path, "Bash"),
    ]
    test_edit, source_edit = evidence.edits
    assert test_edit.order < red.order < source_edit.order < green.order
    assert result.passed is True, result
    assert result.red_runs == (command,)


async def test_shell_commands_without_attributed_write_paths_are_not_edits(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "claude-shell.jsonl"
    _write_jsonl(
        transcript,
        [
            *_claude_tool_pair(
                command=_heredoc_write("scratch/notes.py"),
                call_id="unattributed-write",
                start=BASE_TIME,
                result={"exit_code": 0, "stdout": ""},
            ),
            *_claude_tool_pair(
                command="rm src/changed.py",
                call_id="removal",
                start=BASE_TIME + timedelta(seconds=2),
                result={"exit_code": 0, "stdout": ""},
            ),
            *_claude_tool_pair(
                command="cat src/changed.py",
                call_id="read",
                start=BASE_TIME + timedelta(seconds=4),
                result={"exit_code": 0, "stdout": "x"},
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert evidence.edits == ()


async def test_search_replace_target_file_remains_a_transcript_edit(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "claude-search-replace.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "grok-edit",
                            "name": "search_replace",
                            "input": {"target_file": str(tmp_path / "src" / "changed.py")},
                        }
                    ],
                },
            }
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("src/changed.py", "search_replace")
    ]


@pytest.mark.asyncio
async def test_codex_compound_timeout_preserves_completed_segment_outcomes(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "codex-timeout.jsonl"
    command = "uv run ruff check src && uv run pytest tests/unit -q"
    completed = "uv run ruff check src"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); "
                        "text(r.output);"
                    ),
                },
                BASE_TIME,
            ),
            {
                "type": "event_msg",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "completed-segment",
                        "command": ["/bin/zsh", "-lc", completed],
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": "All checks passed!",
                    },
                },
            },
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": "command timed out before pytest completed",
                },
                BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [
        (run.command, run.categories, run.outcome, run.exit_code)
        for run in evidence.validation_runs
    ] == [
        (completed, ("lint", "type_check"), "success", 0),
        (command, ("lint", "type_check", "test"), "unknown", None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_outcome"),
    [(0, "success"), (1, "failure")],
)
@pytest.mark.parametrize(
    "command",
    ["GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q", "npm ci"],
)
async def test_codex_ingests_json_style_functions_exec_evidence(
    tmp_path: Path,
    exit_code: int,
    expected_outcome: str,
    command: str,
) -> None:
    transcript = tmp_path / "codex-functions-exec.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-json-exec",
                    "name": "exec",
                    "input": (
                        f'const r = await tools.exec_command({{"cmd":{json.dumps(command)}}}); '
                        "text(JSON.stringify(r));"
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-json-exec",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                        },
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {
                                    "chunk_id": "focused",
                                    "wall_time_seconds": 0.5,
                                    "exit_code": exit_code,
                                    "output": "focused result",
                                }
                            ),
                        },
                    ],
                },
                BASE_TIME + timedelta(seconds=1),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    runs = evidence.command_runs if command == "npm ci" else evidence.validation_runs
    assert [(run.command, run.outcome, run.exit_code) for run in runs] == [
        (command, expected_outcome, exit_code)
    ]
    if command == "npm ci":
        assert evidence.validation_runs == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "expected_outcome"),
    [(0, "success"), (7, "failure")],
)
@pytest.mark.parametrize(
    "command",
    ["GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q", "npm ci"],
)
async def test_codex_direct_exec_command_accepts_native_terminal_envelope(
    tmp_path: Path,
    exit_code: int,
    expected_outcome: str,
    command: str,
) -> None:
    transcript = tmp_path / "codex-direct-native.jsonl"
    envelope = (
        "Chunk ID: 1d32cc\n"
        "Wall time: 2.9618 seconds\n"
        f"Process exited with code {exit_code}\n"
        "Original token count: 169\n"
        "Output:\n"
        "focused validation output\n"
    )
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(command=command, result=envelope),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    runs = evidence.command_runs if command == "npm ci" else evidence.validation_runs
    assert [(run.outcome, run.exit_code, run.command) for run in runs] == [
        (expected_outcome, exit_code, command)
    ]
    if command == "npm ci":
        assert evidence.validation_runs == ()
    assert not evidence.degraded_capabilities


@pytest.mark.parametrize(
    "command",
    [
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q",
        ("cd /tmp/repo\nGOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q"),
        ("cd /tmp/repo && GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q"),
    ],
)
@pytest.mark.asyncio
async def test_codex_direct_exec_command_recognizes_test_after_directory_change(
    tmp_path: Path,
    command: str,
) -> None:
    transcript = tmp_path / "codex-direct-directory-change.jsonl"
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(
            command=command,
            result={"exit_code": 0, "output": "1 passed"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.command, run.categories, run.outcome) for run in evidence.validation_runs] == [
        (command, ("test",), "success")
    ]
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
    )
    assert gate.passed
    assert gate.details["fresh_run_count"] == 1
    assert gate.details["latest_outcomes"] == {"test": "success"}


@pytest.mark.asyncio
async def test_codex_direct_exec_command_uses_structured_result(tmp_path: Path) -> None:
    transcript = tmp_path / "codex-direct.jsonl"
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(
            command="uv run ruff check src/gobby",
            result={"exit_code": 1, "output": "lint failed"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code, run.categories) for run in evidence.validation_runs] == [
        ("failure", 1, ("lint", "type_check"))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_outcome", "expected_exit"),
    [
        ({"exit_code": 0, "output": ""}, "success", 0),
        ({"exit_code": 1, "output": "workflow.yml: invalid context"}, "failure", 1),
        ({"output": "lint passed"}, "unknown", None),
    ],
)
async def test_codex_actionlint_evidence_preserves_authoritative_outcome(
    tmp_path: Path,
    result: dict[str, object],
    expected_outcome: str,
    expected_exit: int | None,
) -> None:
    transcript = tmp_path / "codex-actionlint.jsonl"
    command = "actionlint .github/workflows/ci.yml .github/workflows/rust-ci.yml"
    _write_jsonl(transcript, _codex_direct_exec_pair(command=command, result=result))

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [
        (run.outcome, run.exit_code, run.categories, run.command)
        for run in evidence.validation_runs
    ] == [(expected_outcome, expected_exit, ("lint",), command)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            (
                "Chunk ID: still-running\n"
                "Wall time: 30.001 seconds\n"
                "Process running with session ID 92\n"
                "Original token count: 5\n"
                "Output:\n"
                "still running\n"
            ),
            id="running",
        ),
        pytest.param(
            (
                "Chunk ID: malformed\n"
                "Wall time: 0.5 seconds\n"
                "Process exited with code 0\n"
                "Original token count: nope\n"
                "Output:\n"
            ),
            id="malformed",
        ),
        pytest.param(
            [
                (
                    "Chunk ID: duplicate-1\n"
                    "Wall time: 0.5 seconds\n"
                    "Process exited with code 0\n"
                    "Output:\n"
                ),
                (
                    "Chunk ID: duplicate-2\n"
                    "Wall time: 0.5 seconds\n"
                    "Process exited with code 0\n"
                    "Output:\n"
                ),
            ],
            id="duplicate",
        ),
        pytest.param(
            (
                "ordinary command output\n"
                "Chunk ID: spoofed\n"
                "Wall time: 0.5 seconds\n"
                "Process exited with code 0\n"
                "Output:\n"
            ),
            id="output-spoofed",
        ),
    ],
)
async def test_codex_direct_exec_command_keeps_non_authoritative_envelopes_unknown(
    tmp_path: Path,
    result: Any,
) -> None:
    transcript = tmp_path / "codex-direct-unknown.jsonl"
    _write_jsonl(
        transcript,
        _codex_direct_exec_pair(command="uv run pytest tests/tasks -q", result=result),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("unknown", None)]
    assert evidence.degraded_capabilities


@pytest.mark.asyncio
async def test_unknown_codex_outcome_is_retained_as_degraded_evidence(tmp_path: Path) -> None:
    transcript = tmp_path / "codex-unknown.jsonl"
    _write_jsonl(
        transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "outer-exec",
                    "name": "exec",
                    "input": (
                        'const r = await tools.exec_command({cmd:"pytest"}); text(r.output);'
                    ),
                },
                BASE_TIME,
            ),
            _codex_response_item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-exec",
                    "output": "passed without structured terminal metadata",
                },
                BASE_TIME + timedelta(seconds=1),
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["unknown"]
    assert evidence.validation_runs[0].unknown_reason
    assert evidence.degraded_capabilities


@pytest.mark.asyncio
async def test_droid_uses_provider_error_status(tmp_path: Path) -> None:
    transcript = tmp_path / "droid.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "message",
                "id": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "droid-1",
                            "name": "Bash",
                            "input": {"command": "pytest tests/droid"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "id": "user",
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "droid-1",
                            "is_error": False,
                            "content": "passed",
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("droid", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", None)]


def _droid_tool_record(
    *, timestamp: datetime, call_id: str, name: str, tool_input: dict[str, str]
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": call_id,
        "timestamp": timestamp.isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
        },
    }


def _droid_tool_result(
    *, timestamp: datetime, call_id: str, content: str, is_error: bool = False
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": f"{call_id}-result",
        "timestamp": timestamp.isoformat(),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "is_error": is_error,
                    "content": content,
                }
            ],
        },
    }


async def test_droid_rotated_epochs_preserve_task_close_gate_evidence(tmp_path: Path) -> None:
    directory = tmp_path / ".factory" / "sessions" / "encoded-cwd"
    directory.mkdir(parents=True)
    oldest = directory / "oldest.jsonl"
    middle = directory / "middle.jsonl"
    current = directory / "current.jsonl"
    sibling = directory / "unrelated.jsonl"
    test_path = "tests/tasks/test_droid_lineage.py"
    source_path = "src/gobby/tasks/droid_lineage.py"
    command = f"GOBBY_TEST_PROTECT=1 uv run pytest {test_path}::test_droid_lineage -q"
    window_start = BASE_TIME + timedelta(seconds=20)

    _write_jsonl(
        oldest,
        [
            {"type": "session_start", "id": "oldest", "timestamp": BASE_TIME.isoformat()},
            _droid_tool_record(
                timestamp=BASE_TIME,
                call_id="before-claim",
                name="Bash",
                tool_input={"command": "uv run pytest tests/tasks/test_before_claim.py -q"},
            ),
            _droid_tool_result(
                timestamp=BASE_TIME + timedelta(seconds=1),
                call_id="before-claim",
                content="1 passed",
            ),
            _droid_tool_record(
                timestamp=window_start + timedelta(seconds=1),
                call_id="test-edit",
                name="Edit",
                tool_input={"file_path": str(tmp_path / test_path)},
            ),
            _droid_tool_record(
                timestamp=window_start + timedelta(seconds=2),
                call_id="red",
                name="Bash",
                tool_input={"command": command},
            ),
            _droid_tool_result(
                timestamp=window_start + timedelta(seconds=3),
                call_id="red",
                content=(
                    f"FAILED {test_path}::test_droid_lineage - AssertionError: assert 0 == 1\n"
                    f"{test_path}:12: in test_droid_lineage\n    assert 0 == 1"
                ),
                is_error=True,
            ),
        ],
    )
    _write_jsonl(
        middle,
        [
            {"type": "session_start", "id": "middle", "parent": "oldest"},
            _droid_tool_record(
                timestamp=window_start + timedelta(seconds=4),
                call_id="source-edit",
                name="Edit",
                tool_input={"file_path": str(tmp_path / source_path)},
            ),
            _droid_tool_record(
                timestamp=window_start + timedelta(seconds=5),
                call_id="green",
                name="Bash",
                tool_input={"command": command},
            ),
            _droid_tool_result(
                timestamp=window_start + timedelta(seconds=6),
                call_id="green",
                content="1 passed",
            ),
        ],
    )
    _write_jsonl(
        current,
        [
            {"type": "session_start", "id": "current", "parent": "middle"},
            _droid_tool_record(
                timestamp=window_start + timedelta(seconds=7),
                call_id="current-run",
                name="Bash",
                tool_input={"command": command},
            ),
            _droid_tool_result(
                timestamp=window_start + timedelta(seconds=8),
                call_id="current-run",
                content="1 passed",
            ),
        ],
    )
    _write_jsonl(
        sibling,
        [
            {"type": "session_start", "id": "unrelated"},
            _droid_tool_record(
                timestamp=window_start + timedelta(seconds=9),
                call_id="sibling-edit",
                name="Edit",
                tool_input={"file_path": str(tmp_path / source_path)},
            ),
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("droid", current),
        window_start,
        default_validation_detection_config(),
        {test_path, source_path},
        str(tmp_path),
    )

    assert evidence.attempted_paths == (str(current), str(oldest), str(middle))
    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        (test_path, "Edit"),
        (source_path, "Edit"),
    ]
    assert [(run.command, run.outcome) for run in evidence.validation_runs] == [
        (command, "failure"),
        (command, "success"),
        (command, "success"),
    ]
    test_edit, source_edit = evidence.edits
    red, green, current_run = evidence.validation_runs
    assert test_edit.order < red.order < source_edit.order < green.order < current_run.order
    assert evaluate_validation_commands(
        task_category="code", evidence=evidence, has_attributed_edits=True
    ).passed
    acceptance = AcceptanceTest(
        reference=f"{test_path}::test_droid_lineage",
        path=test_path,
        symbol="test_droid_lineage",
        body="def test_droid_lineage():\n    assert 0 == 1",
    )
    assert evaluate_tdd_evidence((acceptance,), evidence).passed


@pytest.mark.asyncio
async def test_grok_uses_terminal_tool_status(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": BASE_TIME.isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-1",
                    "title": "run_terminal_command",
                    "rawInput": {"command": "pytest tests/grok"},
                },
            },
            {
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "grok-1",
                    "status": "failed",
                    "content": [{"type": "text", "text": "failed"}],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("grok", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["failure"]


@pytest.mark.asyncio
async def test_grok_search_replace_is_a_tdd_edit(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    named_test = tmp_path / "tests" / "test_named.py"
    other_test = tmp_path / "tests" / "other_named.py"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": BASE_TIME.isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-edit-path",
                    "title": "search_replace",
                    "rawInput": {
                        "file_path": str(named_test),
                        "old_string": "old",
                        "new_string": "new",
                    },
                },
            },
            {
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-edit-target",
                    "title": "search_replace",
                    "rawInput": {
                        "target_file": str(other_test),
                        "old_string": "old",
                        "new_string": "new",
                    },
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("grok", transcript),
        None,
        default_validation_detection_config(),
        {"tests/test_named.py", "tests/other_named.py"},
        str(tmp_path),
    )

    assert [(edit.path, edit.tool_name) for edit in evidence.edits] == [
        ("tests/test_named.py", "search_replace"),
        ("tests/other_named.py", "search_replace"),
    ]


@pytest.mark.asyncio
async def test_grok_completed_status_with_runner_failures_is_failure(tmp_path: Path) -> None:
    transcript = tmp_path / "grok.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "timestamp": BASE_TIME.isoformat(),
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "grok-pytest",
                    "title": "run_terminal_command",
                    "rawInput": {
                        "command": (
                            "uv run pytest tests/storage/"
                            "test_postgres_agent_authorization.py::"
                            "test_project_checkouts_are_machine_isolated_lock_only_and_daemon_writable"
                        )
                    },
                },
            },
            {
                "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "grok-pytest",
                    "status": "completed",
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": (
                                    "Pytest: 1 passed, 1 failed\n\n"
                                    "Failures:\n"
                                    "     tests/storage/test_postgres_agent_authorization.py:614: "
                                    "in test_project_checkouts_are_machine_isolated_lock_only_and_daemon_writable\n"
                                    "     assert locked == rows\n"
                                    "     E   AssertionError: assert [] == [(UUID('4b0d7'))]\n"
                                ),
                            },
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("grok", transcript),
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.outcome for run in evidence.validation_runs] == ["failure"]


@pytest.mark.asyncio
async def test_configured_gzip_archive_is_used(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    session = _session("claude", None)
    archive = archive_dir / f"{session.external_id}.jsonl.gz"
    records = _claude_tool_pair(
        command="pytest tests/archive",
        call_id="archive-1",
        start=BASE_TIME,
        result="passed",
    )
    with gzip.open(archive, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(record) for record in records) + "\n")

    evidence = await derive_transcript_evidence(
        session,
        None,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
        archive_dir=str(archive_dir),
    )

    assert [run.command for run in evidence.validation_runs] == ["pytest tests/archive"]
    assert evidence.attempted_paths[-1] == str(archive)


@pytest.mark.asyncio
async def test_missing_transcript_reports_attempted_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.tasks.transcript_evidence.find_transcript_on_disk",
        lambda *_args, **_kwargs: None,
    )
    missing = tmp_path / "missing.jsonl"
    session = _session("claude", missing)

    with pytest.raises(TranscriptEvidenceUnavailable) as exc_info:
        await derive_transcript_evidence(
            session,
            None,
            default_validation_detection_config(),
            set(),
            str(tmp_path),
            archive_dir=str(tmp_path / "archive"),
        )

    error = exc_info.value
    assert error.retry_after == 5
    assert str(missing) in error.attempted_paths
    assert str(tmp_path / "archive" / f"{session.external_id}.jsonl.gz") in error.attempted_paths


def test_agy_session_resolves_transcript_through_provider_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    external_id = "transcript-evidence-agy-1"
    target = (
        tmp_path
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / external_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    session = _session("agy", None)
    path, attempted = _resolve_transcript_path(session, None)
    assert path == str(target)
    assert str(target) in attempted


@pytest.mark.asyncio
async def test_unreadable_gzip_is_evidence_unavailable(tmp_path: Path) -> None:
    archive = tmp_path / "broken.jsonl.gz"
    archive.write_text("not gzip")

    with pytest.raises(TranscriptEvidenceUnavailable, match="could not be read"):
        await derive_transcript_evidence(
            _session("claude", archive),
            None,
            default_validation_detection_config(),
            set(),
            str(tmp_path),
        )


def test_merge_orders_cross_session_evidence() -> None:
    later_run = TranscriptValidationRun(
        session_id="session-2",
        source="codex",
        command="pytest later",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="success",
        started_at=BASE_TIME + timedelta(seconds=3),
        completed_at=BASE_TIME + timedelta(seconds=4),
        order=1,
    )
    earlier_run = TranscriptValidationRun(
        session_id="session-1",
        source="claude",
        command="pytest earlier",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure",
        started_at=BASE_TIME,
        completed_at=BASE_TIME + timedelta(seconds=1),
        order=2,
    )
    edit = TranscriptEdit(
        session_id="session-1",
        source="claude",
        path="src/changed.py",
        timestamp=BASE_TIME + timedelta(seconds=2),
        order=3,
        tool_name="Edit",
    )

    merged = merge_transcript_evidence(
        TranscriptEvidence(
            validation_runs=(later_run,),
            sessions=("session-2",),
            attempted_paths=("/two",),
        ),
        TranscriptEvidence(
            validation_runs=(earlier_run,),
            edits=(edit,),
            sessions=("session-1",),
            attempted_paths=("/one",),
        ),
    )

    assert [run.command for run in merged.validation_runs] == [
        "pytest earlier",
        "pytest later",
    ]
    assert merged.sessions == ("session-2", "session-1")
    assert merged.attempted_paths == ("/two", "/one")


def _session_run(
    session_id: str,
    command: str,
    completed_at: datetime,
    order: int,
    outcome: EvidenceOutcome = "success",
) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id=session_id,
        source="claude",
        command=command,
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome=outcome,
        started_at=completed_at - timedelta(seconds=1),
        completed_at=completed_at,
        order=order,
    )


@pytest.mark.parametrize(
    "command",
    [
        'cd "/tmp/project root" && cd src && DATABASE_URL="postgres://test db" '
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -q",
        "DATABASE_URL=postgres://test GOBBY_TEST_PROTECT=1 "
        "uv run pytest tests/tasks/test_validation.py -q",
    ],
)
def test_validation_run_core_command_strips_exit_preserving_prefixes(command: str) -> None:
    run = _session_run("session-1", command, BASE_TIME, 1)

    assert run.command == command
    assert run.core_command == "uv run pytest tests/tasks/test_validation.py -q"
    assert run.wrapped is False
    assert run.wrapper_reason is None


@pytest.mark.parametrize(
    "command,wrapper_reason",
    [
        pytest.param("uv run pytest tests/x.py | tail -1", "pipeline", id="pipe"),
        pytest.param("uv run pytest tests/x.py; echo done", "trailing echo", id="semicolon-echo"),
        pytest.param("uv run pytest tests/x.py && echo done", "trailing echo", id="and-echo"),
        pytest.param("uv run pytest tests/x.py || true", "fallback", id="fallback"),
        pytest.param("actionlint .github/workflows/ci.yml || true", "fallback", id="actionlint"),
        pytest.param("uv run pytest tests/x.py &", "backgrounding", id="ampersand"),
        pytest.param("nohup uv run pytest tests/x.py", "nohup wrapper", id="nohup"),
        pytest.param("(uv run pytest tests/x.py)", "subshell wrapper", id="subshell"),
        pytest.param(
            'js_repl("uv run pytest tests/x.py")',
            "js_repl wrapper",
            id="js-repl",
        ),
        pytest.param(
            "node -e \"execSync('uv run pytest tests/x.py')\"",
            "node wrapper",
            id="node",
        ),
    ],
)
def test_validation_run_flags_uncreditable_wrappers(
    command: str,
    wrapper_reason: str,
) -> None:
    run = _session_run("session-1", command, BASE_TIME, 1)

    assert run.command == command
    assert run.core_command is None
    assert run.wrapped is True
    assert run.wrapper_reason == wrapper_reason


def _session_edit(session_id: str, path: str, timestamp: datetime, order: int) -> TranscriptEdit:
    return TranscriptEdit(
        session_id=session_id,
        source="claude",
        path=path,
        timestamp=timestamp,
        order=order,
        tool_name="Edit",
    )


def test_merge_renumbers_order_across_sessions() -> None:
    """A fresh closing session's low local order must not sort before the owner's history."""
    owner = TranscriptEvidence(
        edits=(_session_edit("owner", "tests/test_feature.py", BASE_TIME, 300),),
        validation_runs=(
            _session_run("owner", "pytest red", BASE_TIME + timedelta(seconds=1), 305, "failure"),
        ),
    )
    closer = TranscriptEvidence(
        edits=(_session_edit("closer", "src/feature.py", BASE_TIME + timedelta(seconds=2), 10),),
        validation_runs=(
            _session_run("closer", "pytest green", BASE_TIME + timedelta(seconds=3), 15),
        ),
    )

    merged = merge_transcript_evidence(owner, closer)

    merged_items: list[TranscriptEdit | TranscriptValidationRun] = [
        *merged.edits,
        *merged.validation_runs,
    ]
    positions = sorted(merged_items, key=lambda item: item.order)
    assert [(item.session_id, item.order) for item in positions] == [
        ("owner", 1),
        ("owner", 2),
        ("closer", 3),
        ("closer", 4),
    ]
    assert [run.command for run in merged.validation_runs] == ["pytest red", "pytest green"]


def test_merge_keeps_intra_session_order_under_clock_skew() -> None:
    """Sessions interleave by timestamp, but a session's own transcript order never changes."""
    skewed = TranscriptEvidence(
        edits=(_session_edit("skewed", "src/a.py", BASE_TIME + timedelta(seconds=5), 1),),
        validation_runs=(
            _session_run("skewed", "pytest skewed", BASE_TIME + timedelta(seconds=4), 2),
        ),
    )
    other = TranscriptEvidence(
        edits=(
            _session_edit(
                "other", "src/b.py", BASE_TIME + timedelta(seconds=4, milliseconds=500), 1
            ),
        ),
    )

    merged = merge_transcript_evidence(skewed, other)

    merged_items: list[TranscriptEdit | TranscriptValidationRun] = [
        *merged.edits,
        *merged.validation_runs,
    ]
    positions = sorted(merged_items, key=lambda item: item.order)
    assert [(item.session_id, item.order) for item in positions] == [
        ("other", 1),
        ("skewed", 2),
        ("skewed", 3),
    ]


# ---------------------------------------------------------------------------
# A shell reports the status of the LAST element of a list or pipeline, so
# `pytest ... | tail`, `pytest ... ; echo`, and `pytest ... && other` record a
# zero status for a genuinely failing run. For those compound segments the
# runner's own terminal summary decides the outcome instead.
# ---------------------------------------------------------------------------

# Verbatim tail of a real red pytest run whose exit code a trailing `echo` zeroed.
_RED_PYTEST_OUTPUT = (
    "tests/memory/test_search_ranking.py ................FFFFF                [100%]\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/memory/test_search_ranking.py::test_embed_text_absent_preserves_yake_path\n"
    "========================= 5 failed, 16 passed in 0.30s =========================\n"
    "EXIT=0\n"
)

# Real shape of a PASSING `gobby test-types audit` ratchet over a non-empty
# baseline: the headline "Errors:" counts every baselined finding, and
# "Files scanned: 2\nErrors: 10" is one newline-crossing match away from
# reading as "2 errors" (#20880's misfiled type_check run).
_PASSING_TEST_TYPES_AUDIT_OUTPUT = (
    "Test types audit\n"
    "Files scanned: 2\n"
    "Errors: 10\n"
    "Codes: mypy:arg-type=7, mypy:assignment=3\n"
    "Baseline: loaded (.gobby/test-types-baseline.json)\n"
    "Baseline mode: diff\n"
    "New errors: 0\n"
    "Known baseline errors: 10\n"
    "Failing new errors >= high: 0\n"
    "\n"
    "Ranked files:\n"
    "    7 tests/mcp_proxy/tools/test_task_expansion_new.py [mypy:arg-type]\n"
    "    3 tests/tasks/test_validation.py [mypy:assignment]\n"
)

# The same audit with new errors above the threshold: exit 1, and the ratchet's
# own failing tally line is the runner-summary proof for wrapper-zeroed runs.
_FAILING_TEST_TYPES_AUDIT_OUTPUT = (
    "Test types audit\n"
    "Files scanned: 2\n"
    "Errors: 12\n"
    "Codes: mypy:arg-type=9, mypy:assignment=3\n"
    "Baseline: loaded (.gobby/test-types-baseline.json)\n"
    "Baseline mode: diff\n"
    "New errors: 2\n"
    "Known baseline errors: 10\n"
    "Failing new errors >= high: 2\n"
    "\n"
    "New failing errors:\n"
    '  tests/tasks/test_validation.py:41: error: Argument 1 to "close_task" has'
    ' incompatible type "None"; expected "str" [mypy:arg-type]\n'
)

# `gobby test-quality audit` with a non-empty baseline shares the shape but says
# "Issues"; it must stay clean by contract, not by accident of wording (#20880).
_PASSING_TEST_QUALITY_AUDIT_OUTPUT = (
    "Test quality audit\n"
    "Files scanned: 3\n"
    "Tests scanned: 41\n"
    "Issues: 6\n"
    "Severity: high=2, medium=4\n"
    "Codes: no-assertions=2, mystery-guest=4\n"
    "Baseline: loaded (.gobby/test-quality-baseline.json)\n"
    "Baseline mode: diff\n"
    "New issues: 0\n"
    "Known baseline issues: 6\n"
    "Failing new issues >= high: 0\n"
    "\n"
    "Known baseline issues:\n"
    "  HIGH no-assertions tests/tasks/test_validation.py::test_gate:12 - no assertions\n"
)

_FAILING_TEST_QUALITY_AUDIT_OUTPUT = (
    "Test quality audit\n"
    "Files scanned: 3\n"
    "Tests scanned: 41\n"
    "Issues: 7\n"
    "Severity: high=3, medium=4\n"
    "Codes: no-assertions=3, mystery-guest=4\n"
    "Baseline: loaded (.gobby/test-quality-baseline.json)\n"
    "Baseline mode: diff\n"
    "New issues: 1\n"
    "Known baseline issues: 6\n"
    "Failing new issues >= high: 1\n"
    "\n"
    "Failing new issues:\n"
    "  HIGH no-assertions tests/tasks/test_close.py::test_new:9 - no assertions\n"
)

_RUNNER_FAILURE_OUTPUTS = [
    pytest.param(_RED_PYTEST_OUTPUT, id="pytest-counted-summary"),
    pytest.param("===== 178 errors in 4.90s =====", id="pytest-collection-errors"),
    pytest.param("ERROR tests/x.py::test_y\nFAILED tests/x.py::test_z", id="pytest-summary-lines"),
    pytest.param("test result: FAILED. 1 passed; 1 failed; 0 ignored", id="cargo"),
    pytest.param("--- FAIL: TestThing (0.00s)\nFAIL\tgithub.com/a/b\t0.1s", id="go"),
    pytest.param("Found 2 errors in 1 file (checked 3 source files)", id="mypy"),
    pytest.param("Tests  3 failed | 5 passed (8)", id="vitest"),
    pytest.param(_FAILING_TEST_TYPES_AUDIT_OUTPUT, id="test-types-ratchet-failing"),
    pytest.param(_FAILING_TEST_QUALITY_AUDIT_OUTPUT, id="test-quality-ratchet-failing"),
]

_CLEAN_OUTPUTS = [
    pytest.param("21 passed in 0.13s", id="passing"),
    pytest.param("1208 passed, 64 deselected in 32.47s", id="passing-with-deselect"),
    pytest.param("0 failed, 5 passed in 0.10s", id="zero-failed"),
    pytest.param("3 passed, 1 xfailed, 2 warnings in 0.20s", id="xfailed"),
    pytest.param("Success: no issues found in 1830 source files", id="mypy-clean"),
    pytest.param("New errors: 0\nFailing new errors >= high: 0", id="ratchet-clean"),
    pytest.param("All checks passed!", id="ruff-clean"),
    pytest.param(
        _PASSING_TEST_TYPES_AUDIT_OUTPUT,
        id="test-types-ratchet-passing-with-baselined-errors",
    ),
    pytest.param(
        _PASSING_TEST_QUALITY_AUDIT_OUTPUT,
        id="test-quality-ratchet-passing-with-baselined-issues",
    ),
]


@pytest.mark.parametrize("output", _RUNNER_FAILURE_OUTPUTS)
def test_compound_zero_exit_yields_failure_when_the_runner_reported_failures(
    output: str,
) -> None:
    """A.1: aggregate shell status cannot prove a compound segment passed."""
    outcome, exit_code, unknown_reason = transcript_outcomes.extract_outcome(
        {"exit_code": 0, "stdout": output},
        output,
        aggregate_status_is_trustworthy=False,
    )

    assert outcome == "failure"
    assert exit_code == 0
    assert unknown_reason is None


@pytest.mark.parametrize("output", _RUNNER_FAILURE_OUTPUTS)
def test_compound_provider_success_is_also_overridden(output: str) -> None:
    """A.1: the provider `success`/`is_error` fallbacks lie the same way as `$?`."""
    outcome, _exit_code, _reason = transcript_outcomes.extract_outcome(
        {"success": True, "stdout": output},
        output,
        aggregate_status_is_trustworthy=False,
    )

    assert outcome == "failure"


@pytest.mark.parametrize("output", _CLEAN_OUTPUTS)
def test_clean_run_output_is_not_misread_as_failure(output: str) -> None:
    """A.2: nothing else changes.

    Clean output stays a success even for a compound segment, and a non-compound
    run's recorded exit code stays authoritative whatever the output says.
    """
    compound, _code, _reason = transcript_outcomes.extract_outcome(
        {"exit_code": 0, "stdout": output},
        output,
        aggregate_status_is_trustworthy=False,
    )
    assert compound == "success"

    plain, _code, _reason = transcript_outcomes.extract_outcome(
        {"exit_code": 0, "stdout": _RED_PYTEST_OUTPUT},
        _RED_PYTEST_OUTPUT,
        aggregate_status_is_trustworthy=True,
    )
    assert plain == "success"


def test_nonzero_exit_stays_a_failure_however_clean_the_output() -> None:
    """A.4: the new rule only ever adds failures, never removes one."""
    for trustworthy in (True, False):
        outcome, exit_code, _reason = transcript_outcomes.extract_outcome(
            {"exit_code": 1, "stdout": "21 passed in 0.13s"},
            "21 passed in 0.13s",
            aggregate_status_is_trustworthy=trustworthy,
        )
        assert (outcome, exit_code) == ("failure", 1)


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param(' 2>&1; echo "EXIT=$?"', id="trailing-echo"),
        pytest.param(" 2>&1 | tail -20", id="pipe-to-tail"),
        pytest.param(" && echo done", id="and-then"),
    ],
)
@pytest.mark.asyncio
async def test_wrapper_zeroed_exit_code_still_yields_a_failure_run(
    tmp_path: Path,
    suffix: str,
) -> None:
    """A.3: the end-to-end derivation stops filing wrapper-zeroed reds as passes."""
    transcript = tmp_path / "zeroed.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=f"GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_search_ranking.py -q{suffix}",
            call_id="red-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": _RED_PYTEST_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("failure", None)]
    assert evidence.degraded_capabilities == ()


@pytest.mark.asyncio
async def test_uncompounded_passing_run_is_still_a_success(tmp_path: Path) -> None:
    """The overwhelmingly common shape keeps its byte-for-byte previous behavior."""
    transcript = tmp_path / "plain.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command="GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_search_ranking.py -q",
            call_id="green-1",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": "21 passed in 0.13s"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", None)]


_TEST_TYPES_AUDIT_COMMAND = (
    "uv run gobby test-types audit tests/tasks/test_validation.py "
    "--baseline .gobby/test-types-baseline.json --fail-on-new"
)


def test_failing_audit_nonzero_exit_stays_a_failure_in_both_trust_modes() -> None:
    """#20880: a genuinely failing ratchet keeps its exit-1 failure, bare or compound."""
    for trustworthy in (True, False):
        outcome, exit_code, _reason = transcript_outcomes.extract_outcome(
            {"exit_code": 1, "stdout": _FAILING_TEST_TYPES_AUDIT_OUTPUT},
            _FAILING_TEST_TYPES_AUDIT_OUTPUT,
            aggregate_status_is_trustworthy=trustworthy,
        )
        assert (outcome, exit_code) == ("failure", 1)


@pytest.mark.asyncio
async def test_passing_test_types_audit_is_recorded_as_a_successful_type_check(
    tmp_path: Path,
) -> None:
    """#20880: exit 0 with a passing ratchet is a success even for a compound run.

    The audit's headline counts baselined errors ("Errors: 10"), and the compound
    `cd ... && audit` shape routed that output through the runner-summary rule,
    filing the passing run as a failing type_check.
    """
    transcript = tmp_path / "audit.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=_TEST_TYPES_AUDIT_COMMAND,
            call_id="audit-bare",
            start=BASE_TIME,
            result={"exit_code": 0, "stdout": _PASSING_TEST_TYPES_AUDIT_OUTPUT},
        )
        + _claude_tool_pair(
            command=f"cd /repo && {_TEST_TYPES_AUDIT_COMMAND}",
            call_id="audit-compound",
            start=BASE_TIME + timedelta(minutes=1),
            result={"exit_code": 0, "stdout": _PASSING_TEST_TYPES_AUDIT_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.categories, run.outcome, run.exit_code) for run in evidence.validation_runs] == [
        (("type_check",), "success", None),
        (("type_check",), "success", None),
    ]


def _codex_nested_exec_pair(
    *, command: str, result: dict[str, Any], call_id: str = "outer-exec"
) -> list[dict[str, Any]]:
    """A `tools.exec_command` call nested inside `exec`, which Codex outcomes drive."""
    return [
        _codex_response_item(
            {
                "type": "custom_tool_call",
                "call_id": call_id,
                "name": "exec",
                "input": (
                    f"const r = await tools.exec_command({{cmd:{json.dumps(command)}}}); text(r);"
                ),
            },
            BASE_TIME,
        ),
        _codex_response_item(
            {
                "type": "custom_tool_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            },
            BASE_TIME + timedelta(seconds=1),
        ),
    ]


@pytest.mark.asyncio
async def test_codex_wrapper_zeroed_exit_code_still_yields_a_failure_run(tmp_path: Path) -> None:
    """A.3: Codex records its own outcomes, and the same shell truth applies there.

    `_consume_codex_outcome` classifies the run itself rather than going through
    `_record_validation_run`, so a fix confined to the Claude path would leave every
    Codex session filing wrapper-zeroed reds as passes.
    """
    transcript = tmp_path / "codex-zeroed.jsonl"
    _write_jsonl(
        transcript,
        _codex_nested_exec_pair(
            command="uv run pytest tests/tasks -q 2>&1 | tail -20",
            result={"exit_code": 0, "output": _RED_PYTEST_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("failure", 0)]


@pytest.mark.asyncio
async def test_codex_uncompounded_passing_run_is_still_a_success(tmp_path: Path) -> None:
    """The Codex path keeps its previous behavior for the non-compound shape."""
    transcript = tmp_path / "codex-plain.jsonl"
    _write_jsonl(
        transcript,
        _codex_nested_exec_pair(
            command="uv run pytest tests/tasks -q",
            result={"exit_code": 0, "output": "21 passed in 0.13s"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", 0)]


@pytest.mark.asyncio
async def test_codex_passing_test_types_suppression_ratchet_is_recorded(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "codex-suppressions.jsonl"
    command = (
        "uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json"
    )
    _write_jsonl(
        transcript,
        _codex_nested_exec_pair(
            command=command,
            result={"exit_code": 0, "output": "New: 0\nStale: 0"},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [
        (run.command, run.categories, run.outcome, run.exit_code)
        for run in evidence.validation_runs
    ] == [(command, ("type_check",), "success", 0)]


def _timestamped(stamp: str) -> str:
    return json.dumps({"type": "assistant", "timestamp": stamp, "message": {}})


def test_window_selection_drops_only_lines_older_than_the_lookback() -> None:
    window_start = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    lines = [
        _timestamped("2026-07-20T12:00:00Z"),
        _timestamped((window_start - WINDOW_LOOKBACK - timedelta(minutes=1)).isoformat()),
        _timestamped((window_start - timedelta(minutes=5)).isoformat()),
        _timestamped(window_start.isoformat()),
        _timestamped("2026-07-27T14:00:00Z"),
        json.dumps({"type": "summary", "summary": "no timestamp here"}),
        _timestamped("2026-07-20T12:00:00+02:00"),
    ]

    selected = list(select_window_raw_lines(lines, window_start))

    assert [item.raw_line_no for item in selected] == [2, 3, 4, 5, 6]
    assert [item.text for item in selected] == [lines[index] for index in (2, 3, 4, 5, 6)]


def test_window_selection_keeps_a_line_whose_nested_timestamp_is_older() -> None:
    window_start = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    # A Claude JSONL line carries its own timestamp last; a structured result
    # embedded earlier can carry an older one of its own.
    line = json.dumps(
        {
            "type": "user",
            "toolUseResult": {"timestamp": "2026-07-01T09:00:00Z"},
            "timestamp": window_start.isoformat(),
        }
    )

    selected = list(select_window_raw_lines([line], window_start))

    assert [item.text for item in selected] == [line]


def test_window_selection_keeps_everything_without_a_window() -> None:
    lines = [_timestamped("2026-07-20T12:00:00Z"), _timestamped("2026-07-27T14:00:00Z")]

    selected = list(select_window_raw_lines(lines, None))

    assert [item.text for item in selected] == lines
    assert [item.raw_line_no for item in selected] == [0, 1]


@pytest.mark.asyncio
async def test_pre_window_history_does_not_change_derived_evidence(tmp_path: Path) -> None:
    window_start = BASE_TIME
    transcript = tmp_path / "claude.jsonl"
    in_window = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_example.py",
        call_id="fresh",
        start=window_start + timedelta(minutes=1),
        result={"exit_code": 0, "stdout": "passed"},
    )
    ancient = _claude_tool_pair(
        command="uv run pytest tests/tasks/test_ancient.py",
        call_id="ancient",
        start=window_start - timedelta(days=3),
        result={"exit_code": 1, "stdout": "1 failed"},
    )
    _write_jsonl(transcript, [*ancient, *in_window])
    session = _session("claude", transcript)

    evidence = await derive_transcript_evidence(
        session,
        window_start,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.command for run in evidence.validation_runs] == [
        "uv run pytest tests/tasks/test_example.py"
    ]
    assert [run.outcome for run in evidence.validation_runs] == ["success"]


@pytest.mark.asyncio
async def test_compound_run_records_only_its_validation_segments(tmp_path: Path) -> None:
    """Cover scoping needs the validation argv, not the git/shell segments around it."""
    transcript = tmp_path / "compound.jsonl"
    command = (
        'git stash push -m "tmp" src/gobby/servers/auth.py -q\n'
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_auth.py -q\n"
        "git stash pop -q"
    )
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=command,
            call_id="red-1",
            start=BASE_TIME,
            result={"exit_code": 1, "stdout": _RED_PYTEST_OUTPUT},
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    assert [run.command for run in evidence.validation_runs] == [command]
    assert [run.validation_segments for run in evidence.validation_runs] == [
        (
            TranscriptValidationSegment(
                command="pytest tests/servers/test_auth.py -q", categories=("test",)
            ),
        )
    ]
    assert [
        segment.segment_index for segment in evidence.validation_runs[0].validation_segments
    ] == [1]


async def test_compound_run_records_every_segment_with_its_categories(tmp_path: Path) -> None:
    """A format-then-test compound run belongs to both categories, one segment each."""
    transcript = tmp_path / "compound-green.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command=(
                "uv run ruff format --check src/gobby/x.py && "
                "GOBBY_TEST_PROTECT=1 uv run pytest tests/unit -q"
            ),
            call_id="green-1",
            start=BASE_TIME,
            result={
                "exit_code": 0,
                "stdout": (
                    "1 file already formatted\n"
                    "tests/unit/test_a.py ..                                    [100%]\n"
                    "2 passed in 0.10s\n"
                ),
            },
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    (run,) = evidence.validation_runs
    assert run.categories == ("format", "test")
    assert run.validation_segments == (
        TranscriptValidationSegment(
            command="ruff format --check src/gobby/x.py", categories=("format",)
        ),
        TranscriptValidationSegment(command="pytest tests/unit -q", categories=("test",)),
    )
    assert [segment.segment_index for segment in run.validation_segments] == [0, 1]
    assert [(segment.languages, segment.bounded_inputs) for segment in run.validation_segments] == [
        (("python",), True),
        (("python",), False),
    ]


@pytest.mark.asyncio
async def test_compound_failure_is_attributed_to_reported_runner_segment(tmp_path: Path) -> None:
    transcript = tmp_path / "compound-failure.jsonl"
    command = (
        "uv run ruff format --check src/gobby/x.py && "
        "uv run ruff check src/gobby/x.py && "
        "uv run mypy src/gobby/x.py && "
        "uv run pytest tests/unit/test_x.py -q"
    )
    _write_jsonl(
        transcript,
        _codex_nested_exec_pair(
            command=command,
            result={
                "exit_code": 1,
                "output": (
                    "1 file already formatted\n"
                    "All checks passed!\n"
                    "Success: no issues found in 1 source file\n"
                    "FAILED tests/unit/test_x.py::test_x\n"
                    "1 failed in 0.10s\n"
                ),
            },
        ),
    )

    evidence = await derive_transcript_evidence(
        _session("codex", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
    )

    assert gate.details["latest_outcomes"] == {
        "format": "success",
        "lint": "success",
        "test": "failure",
        "type_check": "success",
    }
    assert gate.details["unresolved_failure_categories"] == ["test"]


async def test_edits_in_another_checkout_match_task_files_by_suffix(tmp_path: Path) -> None:
    """Worktree edits count when the close resolves a different checkout root."""
    repo_path = tmp_path / "main"
    worktree = tmp_path / "worktrees" / "task-1"
    patch = (
        f"*** Begin Patch\n*** Update File: {worktree / 'src' / 'changed.py'}\n"
        "@@\n-old\n+new\n*** End Patch\n"
    )
    codex_transcript = tmp_path / "codex.jsonl"
    _write_jsonl(
        codex_transcript,
        [
            _codex_response_item(
                {
                    "type": "custom_tool_call",
                    "call_id": "wrapped-patch",
                    "name": "exec",
                    "input": (
                        f"const patch = {json.dumps(patch)};\n"
                        "const result = await tools.apply_patch(patch); text(result);"
                    ),
                },
                BASE_TIME,
            ),
        ],
    )
    claude_transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        claude_transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "edit-1",
                            "name": "Edit",
                            "input": {"file_path": str(worktree / "src" / "changed.py")},
                        }
                    ],
                },
            },
        ],
    )

    codex_evidence = await derive_transcript_evidence(
        _session("codex", codex_transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(repo_path),
    )
    claude_evidence = await derive_transcript_evidence(
        _session("claude", claude_transcript, suffix="2"),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(repo_path),
    )

    assert [(edit.path, edit.tool_name) for edit in codex_evidence.edits] == [
        ("src/changed.py", "functions.exec")
    ]
    assert [(edit.path, edit.tool_name) for edit in claude_evidence.edits] == [
        ("src/changed.py", "Edit")
    ]


async def test_task_checkout_paths_reject_foreign_root_and_file(tmp_path: Path) -> None:
    owned_a = tmp_path / "worktrees" / "task-259-runbook"
    owned_b = tmp_path / "worktrees" / "task-259-supply"
    foreign = tmp_path / "worktrees" / "task-260-resources"
    transcript = tmp_path / "claude-checkouts.jsonl"
    records = []
    edits = (
        (owned_a, "docs/replenishment.md"),
        (foreign, "docs/replenishment.md"),
        (owned_b, "docs/replenishment.md"),
        (owned_a, "docs/other.md"),
    )
    for index, (root, path) in enumerate(edits):
        records.append(
            {
                "type": "assistant",
                "timestamp": (BASE_TIME + timedelta(seconds=index)).isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"edit-{index}",
                            "name": "Edit",
                            "input": {"file_path": str(root / path)},
                        }
                    ],
                },
            }
        )
    _write_jsonl(transcript, records)
    # This session works both tasks and currently points at the foreign checkout.
    session = replace(_session("claude", transcript), workspace_path=str(foreign))

    evidence = await derive_transcript_evidence(
        session,
        BASE_TIME,
        default_validation_detection_config(),
        {"docs/replenishment.md", "docs/other.md"},
        str(foreign),
        task_checkout_paths=frozenset(
            {
                (str(owned_a), "docs/replenishment.md"),
                (str(owned_b), "docs/replenishment.md"),
                (str(owned_b), "docs/other.md"),
            }
        ),
    )

    assert [(edit.path, edit.timestamp) for edit in evidence.edits] == [
        ("docs/replenishment.md", BASE_TIME),
        ("docs/replenishment.md", BASE_TIME + timedelta(seconds=2)),
    ]


@pytest.mark.parametrize("tool_name", ["Edit", "apply_patch", "Bash"])
async def test_relative_edits_require_call_time_checkout_proof(
    tmp_path: Path, tool_name: str
) -> None:
    owned_a = tmp_path / "task-259-runbook"
    foreign = tmp_path / "task-260-resources"
    owned_b = tmp_path / "task-259-supply"
    path = "docs/replenishment.md"
    transcript = tmp_path / f"{tool_name}.jsonl"
    records = []
    for index, workdir in enumerate((owned_a, foreign, owned_b, None)):
        if tool_name == "Edit":
            arguments = {"file_path": path}
        elif tool_name == "apply_patch":
            arguments = {"patch": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch"}
        else:
            arguments = {"command": _heredoc_write(path)}
        if workdir is not None:
            arguments["workdir"] = str(workdir)
        records.append(
            {
                "type": "assistant",
                "timestamp": (BASE_TIME + timedelta(seconds=index)).isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"edit-{index}",
                            "name": tool_name,
                            "input": arguments,
                        }
                    ],
                },
            }
        )
    _write_jsonl(transcript, records)
    session = replace(_session("claude", transcript), workspace_path=str(foreign))

    evidence = await derive_transcript_evidence(
        session,
        BASE_TIME,
        default_validation_detection_config(),
        {path},
        str(owned_a),
        task_checkout_paths=frozenset({(str(owned_a), path), (str(owned_b), path)}),
    )

    assert [edit.timestamp for edit in evidence.edits] == [
        BASE_TIME,
        BASE_TIME + timedelta(seconds=2),
    ]


async def test_edit_outside_every_checkout_without_task_suffix_is_ignored(tmp_path: Path) -> None:
    """Escaping paths only count when they end with a task file."""
    repo_path = tmp_path / "main"
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "timestamp": BASE_TIME.isoformat(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "edit-1",
                            "name": "Edit",
                            "input": {"file_path": str(tmp_path / "elsewhere" / "other.py")},
                        }
                    ],
                },
            },
        ],
    )

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.py"},
        str(repo_path),
    )

    assert evidence.edits == ()


async def test_derivation_yields_to_event_loop_between_record_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deterministic guard for the loop-stall fix: no clock and no host load. Records
    # cross the pool boundary only inside bounded chunks, and the loop runs other
    # ready tasks between every chunk it encodes or decodes. A transfer that
    # rebuilds the whole result in one step fails both checks at any size or load.
    transcript = _chunked_transfer_transcript(tmp_path, 3 * CHUNK_RECORDS)
    session = _session("claude", transcript)
    # Workers started before the spies below keep the unpatched codec, so only the
    # daemon-side cooperative steps are observed.
    await transcript_evidence_pool.prewarm_transcript_evidence_pool()
    real_pool = transcript_evidence_pool.run_in_transcript_evidence_pool
    crossed: list[object] = []

    async def recording_pool(function: Any, /, *args: object) -> object:
        assert len(args) == 8, "no outbound resume argument"
        result = await real_pool(function, *args)
        crossed.append(result)
        return result

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", recording_pool)
    turns = 0
    steps: dict[str, list[int]] = {"_encode_steps": [], "_decode_steps": []}
    for name, seen in steps.items():
        real_steps = getattr(transcript_evidence_transfer, name)

        def observed(value: Any, real_steps: Any = real_steps, seen: list[int] = seen) -> Any:
            for step in real_steps(value):
                seen.append(turns)
                yield step

        monkeypatch.setattr(transcript_evidence_transfer, name, observed)

    async def count_turns() -> None:
        nonlocal turns
        while True:
            turns += 1
            await asyncio.sleep(0)

    async def derive() -> TranscriptEvidence:
        return await derive_transcript_evidence(
            session, BASE_TIME, default_validation_detection_config(), set(), str(tmp_path)
        )

    counter = asyncio.create_task(count_turns())
    try:
        await derive()
        # The second derivation resumes entirely within the worker.
        evidence = await derive()
    finally:
        counter.cancel()
        await asyncio.gather(counter, return_exceptions=True)

    # Only the two requested evidence payloads cross back to the daemon.
    assert [type(item) for item in crossed] == [ChunkedPayload] * 2
    for payload in cast(list[ChunkedPayload], crossed):
        assert len(payload.chunks) > 1
        assert all(len(pickle.loads(chunk)[1]) <= CHUNK_RECORDS for chunk in payload.chunks)
    assert steps["_encode_steps"] == [], "no snapshot is encoded on the daemon loop"
    for name, seen in (("_decode_steps", steps["_decode_steps"]),):
        assert len(seen) > 2, name
        assert all(later > earlier for earlier, later in zip(seen, seen[1:], strict=False)), (
            name,
            seen,
        )
    assert "uv run pytest tests/tasks/test_chunked.py -q" in [
        run.command for run in evidence.validation_runs
    ]


def _chunked_transfer_transcript(tmp_path: Path, filler_runs: int) -> Path:
    transcript = tmp_path / "chunked-claude.jsonl"
    records: list[dict[str, Any]] = []
    for index in range(filler_runs):
        records.extend(
            _claude_tool_pair(
                command=f"printf filler-{index}",
                call_id=f"filler-{index}",
                start=BASE_TIME + timedelta(microseconds=index * 2),
                result={"exit_code": 0, "stdout": f"filler {index}"},
            )
        )
    records.extend(
        _claude_tool_pair(
            command="uv run pytest tests/tasks/test_chunked.py -q",
            call_id="validation-final",
            start=BASE_TIME + timedelta(seconds=1),
            result={"exit_code": 0, "stdout": "1 passed"},
        )
    )
    _write_jsonl(transcript, records)
    return transcript


def _derivation_args(
    transcript: Path, tmp_path: Path
) -> tuple[Session, datetime, Any, set[str], str, None, None, str]:
    return (
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
        None,
        None,
        LOCAL_MACHINE_ID,
    )


async def _run_pool_entry_in_process(function: Any, /, *args: object) -> object:
    return function(*args)


def test_chunked_derivation_matches_unchunked_derivation_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both derivations start cold so neither resumes from the other's checkpoint.
    monkeypatch.setattr(transcript_evidence, "load_durable_snapshot", lambda _session_id: None)
    monkeypatch.setattr(transcript_evidence, "store_durable_snapshot", lambda *_args: None)
    args = _derivation_args(_chunked_transfer_transcript(tmp_path, 3 * CHUNK_RECORDS), tmp_path)

    direct = _derive_transcript_evidence_sync(*args, None)[0]
    payload = _derive_chunked_transcript_evidence(*args)

    assert len(payload.chunks) > 1
    assert decode(payload) == direct
    evidence = cast(TranscriptEvidence, decode(payload))
    assert [run.order for run in evidence.command_runs] == [
        run.order for run in direct.command_runs
    ]
    assert [run.command for run in evidence.validation_runs] == [
        "uv run pytest tests/tasks/test_chunked.py -q"
    ]
    assert payload.record_count == len(evidence.validation_runs) + len(evidence.command_runs)


def test_payload_envelope_does_not_grow_with_record_count() -> None:
    # The envelope is pickled before the first yield and unpickled after the last chunk,
    # each in one uninterrupted step. An envelope of fixed size keeps both steps bounded
    # however many records a resumed snapshot or derived result carries.
    def envelope(records: int) -> bytes:
        runs = tuple(_session_run("session-1", f"cmd {i}", BASE_TIME, i) for i in range(records))
        return encode((runs, list(runs))).envelope

    assert len(envelope(CHUNK_RECORDS)) == len(envelope(8 * CHUNK_RECORDS))


def _drop_last_chunk(payload: ChunkedPayload) -> ChunkedPayload:
    return replace(payload, chunks=payload.chunks[:-1])


def _foreign_chunk(payload: ChunkedPayload) -> ChunkedPayload:
    return replace(payload, chunks=(pickle.dumps(["not a record"]), *payload.chunks[1:]))


@pytest.mark.parametrize("corrupt", [_drop_last_chunk, _foreign_chunk])
async def test_chunked_derivation_fails_closed_on_invalid_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: Any
) -> None:
    async def corrupting_pool(function: Any, /, *args: object) -> object:
        return corrupt(function(*args))

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", corrupting_pool)
    session = _session("claude", _chunked_transfer_transcript(tmp_path, 2 * CHUNK_RECORDS))

    with pytest.raises(pickle.UnpicklingError):
        await derive_transcript_evidence(
            session, BASE_TIME, default_validation_detection_config(), set(), str(tmp_path)
        )

    fingerprint = transcript_evidence._derivation_fingerprint(
        session, BASE_TIME, default_validation_detection_config(), set(), str(tmp_path), None
    )
    # Worker persistence completes before daemon-side decoding starts.
    assert load_durable_snapshot(f"{session.id}:{fingerprint}") is not None


async def test_chunked_derivation_cancelled_mid_decode_keeps_worker_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_chunk = asyncio.Event()
    finished = False
    decode_steps = transcript_evidence_transfer._decode_steps

    def observed_decode_steps(payload: ChunkedPayload) -> Any:
        nonlocal finished
        steps = decode_steps(payload)
        yield next(steps)
        first_chunk.set()
        yield from steps
        finished = True

    monkeypatch.setattr(
        transcript_evidence, "run_in_transcript_evidence_pool", _run_pool_entry_in_process
    )
    monkeypatch.setattr(transcript_evidence_transfer, "_decode_steps", observed_decode_steps)
    session = _session("claude", _chunked_transfer_transcript(tmp_path, 4 * CHUNK_RECORDS))

    derivation = asyncio.create_task(
        derive_transcript_evidence(
            session, BASE_TIME, default_validation_detection_config(), set(), str(tmp_path)
        )
    )
    await first_chunk.wait()
    derivation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await derivation

    assert not finished
    fingerprint = transcript_evidence._derivation_fingerprint(
        session, BASE_TIME, default_validation_detection_config(), set(), str(tmp_path), None
    )
    # Worker persistence completes before daemon-side decoding starts.
    assert load_durable_snapshot(f"{session.id}:{fingerprint}") is not None
