from __future__ import annotations

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.plans import LocalPlanManager
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
async def test_manifest_written_in_worktree_artifact_workspace(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    coordinator_root = temp_dir / "coordinator"
    worktree_root = temp_dir / "worktree"
    coordinator_root.mkdir()
    worktree_root.mkdir()
    case = make_expansion_qa_case(temp_db, project_manager, coordinator_root)
    worktree_plan = worktree_root / case["plan_rel"]
    worktree_plan.parent.mkdir(parents=True, exist_ok=True)
    worktree_plan.write_text(case["plan_path"].read_text(encoding="utf-8"), encoding="utf-8")
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        case["parent"].id,
        worktree_path=str(worktree_root),
        worktree_id="a819c2d0-7529-577b-a037-855c1e92ec9d",
        base_commit_sha="abc123",
    )
    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", lambda **_: covered_report())
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)

    result = await case["registry"].call("run_expansion_qa_coverage", call_args(case))

    expected = (
        worktree_root
        / ".gobby/plans/coverage"
        / case["project"].id
        / str(case["parent"].seq_num)
        / "task-qa-plan.coverage.yaml"
    )
    coordinator_manifest = (
        coordinator_root
        / ".gobby/plans/coverage"
        / case["project"].id
        / str(case["parent"].seq_num)
        / "task-qa-plan.coverage.yaml"
    )
    assert result["manifest_path"] == str(expected.relative_to(worktree_root))
    assert result["qa_result"]["scope"]["plan_path"] == str(worktree_plan.resolve())
    assert expected.exists()
    assert not coordinator_manifest.exists()


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


def _register_case_plan(temp_db, case: dict) -> LocalPlanManager:
    plans = LocalPlanManager(temp_db)
    plans.create_plan_record(
        project_id=case["project"].id,
        plan_id="task-qa-plan",
        plan_path=case["plan_rel"],
        plan_kind="implementation",
        root_task_ref=case["root_task"],
    )
    return plans


@pytest.mark.asyncio
async def test_registered_plan_hash_refreshes_stale_artifact_pointer(
    temp_db,
    project_manager,
    temp_dir,
    monkeypatch,
) -> None:
    """update_plan_hash then a QA rerun rewrites the pointer a prior pass wrote."""
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    monkeypatch.setattr(expansion_qa_coverage, "_evaluate_with_a4", lambda **_: covered_report())
    monkeypatch.setattr(expansion_qa_coverage, "_load_a4_manifest_writer", lambda: None)
    plans = _register_case_plan(temp_db, case)
    stale_hash = case["plan_hash"]
    case["plan_path"].write_text(
        case["plan_path"].read_text(encoding="utf-8")
        + "- A1.2 - Ship the thing. file: `src/example_ship.py`.\n",
        encoding="utf-8",
    )
    record, changed = plans.update_plan_hash_record("task-qa-plan", project_id=case["project"].id)
    assert changed is True
    assert record.plan_hash == sha256_file(case["plan_path"]) != stale_hash

    result = await case["registry"].call(
        "run_expansion_qa_coverage",
        call_args(case, plan_hash=record.plan_hash),
    )

    assert result["ok"] is True, result
    artifacts = TaskArtifactManager(temp_db).get_artifacts(case["parent"].id)
    assert artifacts.plan_file_hash == record.plan_hash
    assert artifacts.expansion_run_id == case["run"].id
    persisted = LocalExpansionRunManager(temp_db).get(case["run"].id)
    assert persisted is not None
    assert persisted.status != "failed"


@pytest.mark.asyncio
async def test_registered_plan_hash_outranks_caller_hash(
    temp_db,
    project_manager,
    temp_dir,
) -> None:
    """An edit without update_plan_hash drifts even when the caller hashes the edited file."""
    case = make_expansion_qa_case(temp_db, project_manager, temp_dir)
    _register_case_plan(temp_db, case)
    case["plan_path"].write_text("changed plan content\n", encoding="utf-8")
    new_hash = sha256_file(case["plan_path"])

    result = await case["registry"].call(
        "run_expansion_qa_coverage",
        call_args(case, plan_hash=new_hash),
    )

    assert result["ok"] is False
    assert result["error"] == "plan_hash_drift"
    assert result["expected_plan_hash"] == case["plan_hash"]
    assert result["actual_plan_hash"] == new_hash
    artifacts = TaskArtifactManager(temp_db).get_artifacts(case["parent"].id)
    assert artifacts.plan_file_hash == case["plan_hash"]
