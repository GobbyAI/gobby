from __future__ import annotations

import pytest

from gobby.tasks import expansion_qa_coverage
from tests.workflows.expansion_qa_helpers import call_args, covered_report, make_expansion_qa_case

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_workflow_calls_evaluate_with_full_scope(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    captured: dict[str, object] = {}

    def fake_evaluate(**kwargs):
        captured.update(kwargs)
        return covered_report()

    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", fake_evaluate)
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    args = call_args(case)
    assert args["root_task"].startswith("#"), "the caller spells the ref with its prefix"

    result = await case["registry"].call("run_expansion_qa_coverage", args)

    assert result["ok"] is True
    assert captured["plan_path"] == case["plan_path"].resolve()
    assert captured["plan_id"] == "task-qa-plan"
    assert captured["plan_hash"] == case["plan_hash"]
    # Coverage-manifest identity treats ref spelling as significant, and the
    # gobby-plans registry writes the ref unprefixed, so the prefix is stripped
    # on the way in rather than round-tripped.
    assert captured["root_task_ref"] == str(case["parent"].seq_num)
    assert captured["project_id"] == case["project"].id
    assert captured["task_tree"] == "db"


def test_manifest_path_components_are_capped() -> None:
    sanitized = expansion_qa_coverage._sanitize("x" * 90, kind="plan_id")

    assert len(sanitized.encode("utf-8")) <= 64
    assert sanitized.startswith("x")
    assert "-" in sanitized
