from __future__ import annotations

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import TaskArtifactManager
from gobby.tasks import expansion_qa_coverage
from tests.workflows.expansion_qa_helpers import (
    call_args,
    covered_report,
    make_expansion_qa_case,
    sha256_file,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_manifest_written_at_canonical_path(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", lambda **_: covered_report())
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    result = await case["registry"].call("run_expansion_qa_coverage", call_args(case))

    expected = (
        temp_dir
        / ".gobby/plans/coverage"
        / case["project"].id
        / str(case["parent"].seq_num)
        / "task-qa-plan.coverage.yaml"
    )
    assert result["manifest_path"] == str(expected.relative_to(temp_dir))
    assert expected.exists()


@pytest.mark.asyncio
async def test_artifact_pointer_written(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", lambda **_: covered_report())
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    await case["registry"].call("run_expansion_qa_coverage", call_args(case))

    artifacts = TaskArtifactManager(temp_db).get_artifacts(case["parent"].id)
    assert artifacts.plan_file_path == str(case["plan_rel"])
    assert artifacts.plan_file_hash == case["plan_hash"]
    assert artifacts.expansion_run_id == case["run"].id


@pytest.mark.asyncio
async def test_plan_hash_drift_fails(
    temp_db,
    project_manager,
    temp_dir,
) -> None:
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    old_hash = case["plan_hash"]
    case["plan_path"].write_text("changed plan content\n", encoding="utf-8")
    new_hash = sha256_file(case["plan_path"])

    result = await case["registry"].call(
        "run_expansion_qa_coverage",
        call_args(case, plan_hash=old_hash),
    )

    persisted = LocalExpansionRunManager(temp_db).get(case["run"].id)
    assert result["ok"] is False
    assert result["error"] == "plan_hash_drift"
    assert result["actual_plan_hash"] == new_hash
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.qa_result is not None
    assert persisted.qa_result["reason"] == "plan_hash_drift"
    assert "plan_hash_drift" in (persisted.error or "")
