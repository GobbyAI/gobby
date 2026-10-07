"""Plan CLI executions retain criterion credit and normal edit freshness."""

from pathlib import Path

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.tasks.criterion_commands import criterion_command_records
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.validation_diagnostics import excluded_validation_records
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _claude_edit_pair,
    _claude_tool_pair,
    _session,
    _write_jsonl,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("later_edit", [False, True])
@pytest.mark.parametrize("mode", ["standard", "expansion"])
async def test_plan_validation_credit_observes_source_edit_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later_edit: bool, mode: str
) -> None:
    monkeypatch.setattr("gobby.sessions.machine_scope.get_machine_id", lambda: LOCAL_MACHINE_ID)
    command = "uv run gobby plans validate .gobby/plans/demo.md -p /project"
    if mode == "expansion":
        command += " --mode expansion"
    transcript = tmp_path / "plan-validation.jsonl"
    records = _claude_tool_pair(
        command=command,
        call_id="validate",
        start=BASE_TIME,
        result="Plan: /project/.gobby/plans/demo.md\nPhases: 4\n",
    )
    if later_edit:
        records.extend(
            _claude_edit_pair(
                name="Edit",
                arguments={"file_path": str(tmp_path / "src/changed.rs")},
                call_id="edit",
                seconds=2,
            )
        )
    _write_jsonl(transcript, records)
    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        {"src/changed.rs"},
        str(tmp_path),
    )
    assert len(evidence.validation_runs) == 1
    assert evidence.validation_runs[0].categories == ("lint",)
    assert evidence.validation_runs[0].outcome == "success"
    assert evidence.validation_runs[0].exit_code is None
    record = criterion_command_records(f"`{command}` exits 0.", evidence)[0]
    assert record["status"] == ("stale" if later_edit else "satisfied")
    exclusions = excluded_validation_records(evidence, lambda _run: ())
    assert [item["reason_code"] for item in exclusions] == (["stale"] if later_edit else [])
