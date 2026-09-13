"""Pre-link transcript observations remain separate from validation credit."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.transcript_exclusions import derive_prelink_runs
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
