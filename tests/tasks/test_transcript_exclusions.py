"""Pre-link transcript observations remain separate from validation credit."""

import asyncio
import json
import threading
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence,
)
from gobby.tasks import transcript_evidence, transcript_evidence_models, transcript_evidence_pool
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import clear_evidence_snapshots, derive_transcript_evidence
from gobby.tasks.transcript_exclusions import derive_prelink_runs
from gobby.tasks.transcript_outcomes import ValidationCommandEquivalence
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _claude_tool_pair,
    _session,
    _write_jsonl,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_prelink_parse_cannot_credit_old_runs_or_replace_window_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    transcript = tmp_path / "window.jsonl"
    _write_jsonl(
        transcript,
        [
            *_claude_tool_pair(
                command="pytest tests/old.py", call_id="old", start=BASE_TIME, result="passed"
            ),
            *_claude_tool_pair(
                command="ruff check src/",
                call_id="new",
                start=BASE_TIME + timedelta(minutes=2),
                result="passed",
            ),
        ],
    )
    session = _session("claude", transcript)
    config = default_validation_detection_config()
    start = BASE_TIME + timedelta(minutes=1)
    credited = await derive_transcript_evidence(session, start, config, set(), str(tmp_path))
    excluded = await derive_prelink_runs(session, start, config, str(tmp_path))
    assert [run.command for run in excluded] == ["pytest tests/old.py"]
    assert excluded[0].output is None
    assert excluded[0].completed_at < start
    assert [run.command for run in credited.validation_runs] == ["ruff check src/"]
    merged = replace(credited, excluded_runs=excluded)
    gate = evaluate_validation_commands(
        task_category="code", evidence=merged, has_attributed_edits=True
    )
    assert gate.status == "failed"
    assert gate.details["nearest_observed_run"]["reason_code"] == "pre-link"
    again = await derive_transcript_evidence(session, start, config, set(), str(tmp_path))
    assert again == credited


@pytest.mark.asyncio
async def test_no_window_does_not_parse_diagnostic_history(tmp_path: Path) -> None:
    with patch(
        "gobby.tasks.transcript_exclusions.run_in_transcript_evidence_pool", new_callable=AsyncMock
    ) as pool:
        assert (
            await derive_prelink_runs(
                _session("claude", tmp_path / "missing"),
                None,
                default_validation_detection_config(),
                str(tmp_path),
            )
            == ()
        )
    pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_prelink_parse_resumes_from_its_own_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    clear_evidence_snapshots()
    transcript = tmp_path / "resume.jsonl"
    _write_jsonl(
        transcript,
        _claude_tool_pair(
            command="pytest tests/old.py", call_id="old", start=BASE_TIME, result="passed"
        ),
    )
    session = _session("claude", transcript)
    config = default_validation_detection_config()
    start = BASE_TIME + timedelta(minutes=5)
    prelink_key = f"{session.id}:prelink"

    credited = await derive_transcript_evidence(session, start, config, set(), str(tmp_path))
    window_snapshot = transcript_evidence._evidence_snapshots[session.id]
    first = await derive_prelink_runs(session, start, config, str(tmp_path))
    assert [run.command for run in first] == ["pytest tests/old.py"]
    prelink_snapshot = transcript_evidence._evidence_snapshots[prelink_key]
    assert prelink_snapshot.parsed_from_offset == 0
    assert transcript_evidence._evidence_snapshots[session.id] is window_snapshot

    with transcript.open("a") as handle:
        appended = _claude_tool_pair(
            command="ruff check src/",
            call_id="later",
            start=BASE_TIME + timedelta(minutes=2),
            result="passed",
        )
        handle.write("\n".join(json.dumps(record) for record in appended) + "\n")

    second = await derive_prelink_runs(session, start, config, str(tmp_path))
    assert [run.command for run in second] == ["pytest tests/old.py", "ruff check src/"]
    advanced = transcript_evidence._evidence_snapshots[prelink_key]
    assert advanced.parsed_from_offset == prelink_snapshot.watermark
    assert transcript_evidence._evidence_snapshots[session.id] is window_snapshot
    assert (
        await derive_transcript_evidence(session, start, config, set(), str(tmp_path)) == credited
    )


def _no_process_pool() -> ProcessPoolExecutor:
    raise OSError("keep the parse in this process")


@pytest.mark.asyncio
async def test_unrelated_request_is_served_during_large_transcript_close_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #22708 criterion 9: rebuilding a run re-classifies its command, and the close
    # preview rebuilt every pre-link and credited run on the event loop.
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    # The thread fallback keeps the parse in this process, where the probe sees it.
    monkeypatch.setattr(transcript_evidence_pool, "_get_pool", _no_process_pool)
    monkeypatch.setattr(transcript_evidence_pool, "_fallback_warning_logged", True)
    link_time = BASE_TIME + timedelta(seconds=5 * 360 - 2)
    transcript = tmp_path / "long-session.jsonl"
    _write_jsonl(
        transcript,
        [
            record
            for index in range(400)
            for record in _claude_tool_pair(
                command=f"uv run pytest tests/test_{index}.py -q",
                call_id=f"call-{index}",
                start=BASE_TIME + timedelta(seconds=5 * index),
                result="1 passed",
            )
        ],
    )
    session = _session("claude", transcript)
    ctx = MagicMock()
    ctx.config = None
    ctx.session_task_manager.get_task_sessions.return_value = [
        {
            "session_id": session.id,
            "task_id": "task",
            "action": "claimed",
            "created_at": link_time.isoformat(),
        }
    ]
    ctx.session_manager.get.side_effect = {session.id: session}.get
    loop = asyncio.get_running_loop()
    served: list[bool] = []
    classify = transcript_evidence_models.classify_validation_command_equivalence

    def classify_while_a_request_arrives(command: str) -> ValidationCommandEquivalence:
        # Each rebuild sends one unrelated loop callback. On the loop thread the
        # callback cannot run while this call holds the thread, so the wait expires.
        if all(served):
            request = threading.Event()
            loop.call_soon_threadsafe(request.set)
            served.append(request.wait(timeout=2.0))
        return classify(command)

    monkeypatch.setattr(
        transcript_evidence_models,
        "classify_validation_command_equivalence",
        classify_while_a_request_arrives,
    )

    evidence = await derive_close_transcript_evidence(
        ctx,
        task_id="task",
        owner_session_id=session.id,
        closing_session_id=session.id,
        owner_window_start=link_time.isoformat(),
        task_edited_files=set(),
        repo_path=str(tmp_path),
    )

    assert len(evidence.excluded_runs) == 360
    assert len(evidence.validation_runs) + len(evidence.command_runs) == 40
    assert served
    assert all(served)
