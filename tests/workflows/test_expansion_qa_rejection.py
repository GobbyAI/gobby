from __future__ import annotations

import pytest

from gobby.tasks import expansion_qa_coverage
from tests.workflows.expansion_qa_helpers import call_args, covered_report, make_expansion_qa_case

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_missing_row_triggers_rejection(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    report = {
        "header": {"plan_id": "task-qa-plan"},
        "rows": [
            {
                "section_id": "A1",
                "item_id": "A1.1",
                "status": "invalid",
                "detail": "artifact src/example.py was not referenced",
                "leaves": [{"leaf_task_ref": "#13244"}],
            }
        ],
    }
    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", lambda **_: report)
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    result = await case["registry"].call("run_expansion_qa_coverage", call_args(case))

    action = result["review_action"]
    notes = action["arguments"]["rejection_notes"]
    assert result["passed"] is False
    assert action["server"] == "gobby-tasks-ops"
    assert action["tool"] == "reject_review"
    assert action["arguments"]["stage_name"] == "expansion"
    assert "section_id=A1" in notes
    assert "item_id=A1.1" in notes
    assert "status=invalid" in notes
    assert "detail=artifact src/example.py was not referenced" in notes
    assert "leaves=#13244" in notes


@pytest.mark.asyncio
async def test_zero_missing_invalid_triggers_approval(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", lambda **_: covered_report())
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    result = await case["registry"].call("run_expansion_qa_coverage", call_args(case))

    action = result["review_action"]
    assert result["passed"] is True
    assert action["server"] == "gobby-tasks-ops"
    assert action["tool"] == "approve_review"
    assert action["arguments"]["stage_name"] == "expansion"
    assert result["manifest_path"] in action["arguments"]["approval_notes"]
