"""Found-work stop evaluation on a long-lived session transcript (#25143)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.storage.session_models import Session
from gobby.tasks import transcript_evidence
from gobby.tasks.transcript_evidence_transfer import ChunkedPayload
from gobby.workflows.found_work_gate import FoundWorkStopAnalyzer
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _claude_tool_pair,
    _session,
    _write_jsonl,
)

pytestmark = pytest.mark.unit

WARM_RUNS = 500
GREEN_COMMAND = "uv run pytest tests/tasks/test_a.py -q"
RED_COMMAND = "uv run pytest tests/tasks/test_b.py -q"
LATEST_TURN_AT = BASE_TIME + timedelta(seconds=2 * WARM_RUNS + 60)


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.machine_scope.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


@pytest.fixture
def pool_payloads(monkeypatch: pytest.MonkeyPatch) -> list[ChunkedPayload]:
    """Run the real evidence worker off-loop and keep each payload that crosses the pool."""
    payloads: list[ChunkedPayload] = []

    async def run_worker(
        function: Callable[..., ChunkedPayload], /, *args: object
    ) -> ChunkedPayload:
        payload = await asyncio.to_thread(function, *args)
        payloads.append(payload)
        return payload

    monkeypatch.setattr(transcript_evidence, "run_in_transcript_evidence_pool", run_worker)
    return payloads


def _analyzer(session: Session) -> FoundWorkStopAnalyzer:
    return FoundWorkStopAnalyzer(
        llm_service_resolver=lambda: None,
        config_resolver=lambda: None,
        session_manager=SimpleNamespace(get=lambda _session_id: session),
        session_task_manager=SimpleNamespace(get_session_tasks=lambda _session_id: []),
    )


async def _stop_failures(
    analyzer: FoundWorkStopAnalyzer, session: Session, project_path: Path
) -> tuple[str, ...]:
    return await analyzer._terminal_failures(
        session_id=session.id,
        variables={},
        project_path=str(project_path),
        project_id=None,
    )


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(record) for record in records) + "\n")


def _warm_session(tmp_path: Path) -> tuple[Path, Session]:
    """A long session whose every validation run is green, so nothing is unresolved."""
    transcript = tmp_path / "claude.jsonl"
    records = [
        record
        for index in range(WARM_RUNS)
        for record in _claude_tool_pair(
            command=GREEN_COMMAND,
            call_id=f"warm-{index}",
            start=BASE_TIME + timedelta(seconds=2 * index + 1),
            result={"exit_code": 0, "stdout": "4 passed"},
        )
    ]
    _write_jsonl(transcript, records)
    return transcript, _session("claude", transcript)


def _append_latest_turn_red(transcript: Path) -> None:
    _append_jsonl(
        transcript,
        _claude_tool_pair(
            command=RED_COMMAND,
            call_id="latest-red",
            start=LATEST_TURN_AT,
            result={"exit_code": 1, "stdout": "1 failed"},
        ),
    )


async def test_stop_after_large_transcript_reports_the_latest_turn_failure(
    tmp_path: Path, pool_payloads: list[ChunkedPayload]
) -> None:
    transcript, session = _warm_session(tmp_path)
    analyzer = _analyzer(session)
    assert await _stop_failures(analyzer, session, tmp_path) == ()

    _append_latest_turn_red(transcript)

    assert await _stop_failures(analyzer, session, tmp_path) == (RED_COMMAND,)


async def test_stop_after_large_transcript_transfers_only_the_appended_runs(
    tmp_path: Path, pool_payloads: list[ChunkedPayload]
) -> None:
    transcript, session = _warm_session(tmp_path)
    analyzer = _analyzer(session)
    await _stop_failures(analyzer, session, tmp_path)
    pool_payloads.clear()

    _append_latest_turn_red(transcript)
    await _stop_failures(analyzer, session, tmp_path)

    # The warm turns were already evaluated by the first stop. This stop's cost
    # follows the one appended run, so it may hand back at most one record rather
    # than every run in the session window (WARM_RUNS + 1).
    assert all(payload.record_count <= 1 for payload in pool_payloads), [
        payload.record_count for payload in pool_payloads
    ]
