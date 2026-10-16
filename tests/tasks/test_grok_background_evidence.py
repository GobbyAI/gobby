"""Grok's captured background pytest lifecycle must reach Gate 10 (#24501)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.storage.session_models import Session
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.transcript_evidence_models import TranscriptEvidence

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"
FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/transcripts/grok-background-pytest-24501.jsonl"
)


@pytest.fixture(autouse=True)
def local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.sessions.machine_scope.get_machine_id", lambda: LOCAL_MACHINE_ID)


def records() -> list[dict[str, Any]]:
    # Raw records supplied by Grok developer 16717; only machine paths are sanitized.
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


async def evidence_for(tmp_path: Path, events: list[dict[str, Any]]) -> TranscriptEvidence:
    path = tmp_path / "updates.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    timestamp = datetime(2026, 10, 15, 11, 50, tzinfo=UTC)
    session = Session(
        id="00000000-0000-0000-0000-000000024501",
        external_id="01a13618-5f32-76b0-9a4f-b50a804e8f71",
        machine_id=LOCAL_MACHINE_ID,
        source="grok",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(path),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return await derive_transcript_evidence(
        session, None, default_validation_detection_config(), set(), str(tmp_path)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("foreground", [False, True], ids=["background", "foreground"])
@pytest.mark.parametrize("rewritten_start", [False, True], ids=["native-start", "rtk-start"])
async def test_captured_grok_pytest_credits_all_targets(
    tmp_path: Path, foreground: bool, rewritten_start: bool
) -> None:
    events = records()
    events = events[5:] if foreground else events[:5]
    original = events[0]["params"]["update"]["rawInput"]["command"]
    rewritten = events[1]["params"]["update"]["rawInput"]["command"]
    assert rewritten == original.replace("uv run pytest", "uv run rtk pytest")
    if rewritten_start:
        events[0]["params"]["update"]["rawInput"]["command"] = rewritten
    evidence = await evidence_for(tmp_path, events)

    assert len(evidence.validation_runs) == 1
    run = evidence.validation_runs[0]
    assert (run.command, run.outcome, run.exit_code) == (
        rewritten if rewritten_start else original,
        "success",
        0,
    )
    assert "103 passed" in (run.output or "")
    assert run.completed_at == datetime.fromtimestamp(
        events[-2 if not foreground else -1]["timestamp"], UTC
    )
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        validation_criteria=f"Run `{original}`.",
    )
    assert gate.status == "passed", gate.message
    assert not gate.details.get("criterion_command_gaps")
    # The repeated background_tasks inventory includes an unrelated older gcode call.
    assert not evidence.command_runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exit_code", 7),
        ("exit_code", None),
        ("exit_code", False),
        ("completed", False),
        ("completed", "true"),
        ("task_id", "unrelated-call"),
    ],
)
async def test_background_nonpassing_snapshot_is_not_credited(
    tmp_path: Path, field: str, value: object
) -> None:
    events = records()[:5]
    events[3]["params"]["update"]["task_snapshot"][field] = value
    evidence = await evidence_for(tmp_path, events)
    assert not any(run.outcome == "success" for run in evidence.validation_runs)
    gate = evaluate_validation_commands(
        task_category="code", evidence=evidence, has_attributed_edits=True
    )
    assert gate.status == "failed"


@pytest.mark.asyncio
async def test_background_completion_without_original_call_is_not_credited(tmp_path: Path) -> None:
    evidence = await evidence_for(tmp_path, records()[2:5])
    assert not evidence.validation_runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "exit_code"), [("in_progress", 0), ("pending", 0), ("failed", 7), ("completed", 7)]
)
async def test_foreground_nonpassing_result_is_not_credited(
    tmp_path: Path, status: str, exit_code: int
) -> None:
    events = records()[5:]
    events[-1]["params"]["update"]["status"] = status
    events[-1]["params"]["update"]["rawOutput"]["exit_code"] = exit_code
    evidence = await evidence_for(tmp_path, events)
    assert not any(run.outcome == "success" for run in evidence.validation_runs)


@pytest.mark.asyncio
async def test_duplicate_background_completion_is_consumed_once(tmp_path: Path) -> None:
    events = records()[:5]
    events.append(events[3])
    evidence = await evidence_for(tmp_path, events)
    assert [(run.outcome, run.exit_code) for run in evidence.validation_runs] == [("success", 0)]
