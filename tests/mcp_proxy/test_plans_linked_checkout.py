"""gobby-plans hash and coverage tools honor a registered linked checkout."""

from __future__ import annotations

import subprocess
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.plans.coverage_manifest import coverage_manifest_path
from gobby.plans.parser import PlanKind, parse_plan
from gobby.storage.concurrency import CoverageExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.isolated_checkout import write_project_marker

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
PLAN_ID = "task-100-demo"
PLAN_RELATIVE = f".gobby/plans/{PLAN_ID}.md"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def coverage_executor() -> Iterator[CoverageExecutor]:
    executor = CoverageExecutor(max_concurrency=1)
    yield executor
    executor.shutdown(cancel_futures=False)
    executor.join()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write_plan(root: Path) -> Path:
    path = root / PLAN_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""
            > **Plan ID:** {PLAN_ID}

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work [category: docs]
            `kind: deliverable`

            Target: `docs/demo.md`

            Body.

            **Acceptance:**
            - 1.1.1 — Docs exist. file: `docs/demo.md`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def _create_project(db: HubDatabase, root: Path, name: str) -> str:
    project_id = str(uuid.uuid4())
    write_project_marker(root, project_id=project_id, name=name)
    return LocalProjectManager(db).create(name=name, repo_path=str(root), project_id=project_id).id


def _linked_checkout(
    db: HubDatabase, tmp_path: Path, name: str
) -> tuple[Path, Path, str, str, str]:
    """Return main, linked, project id, root task ref, and the linked plan hash.

    The main checkout holds the registered plan. The linked worktree carries a
    branch-only edit, so its plan hash differs from the registry's.
    """
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    project_id = _create_project(db, main, name)
    _write_plan(main)
    _git(main, "init", "-q")
    _git(main, "add", ".")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    root_task = LocalTaskManager(db).create_task(
        project_id, "Plan root", validation_criteria="Linked checkouts refresh the plan."
    )
    root_ref = f"#{root_task.seq_num}"
    LocalPlanManager(db).create_plan(
        project_id=project_id,
        plan_id=PLAN_ID,
        plan_path=PLAN_RELATIVE,
        root_task_ref=root_ref,
    )
    linked_plan = linked / PLAN_RELATIVE
    linked_plan.write_text(
        linked_plan.read_text(encoding="utf-8") + "\nBranch-only edit.\n",
        encoding="utf-8",
    )
    branch_hash = parse_plan(
        linked_plan, plan_kind=PlanKind.implementation, parse_mode="draft"
    ).source_hash
    return main, linked, project_id, root_ref, branch_hash


@pytest.mark.asyncio
async def test_update_plan_hash_refreshes_from_registered_linked_worktree(
    temp_db: HubDatabase,
    tmp_path: Path,
    coverage_executor: CoverageExecutor,
) -> None:
    main, linked, project_id, root_ref, branch_hash = _linked_checkout(
        temp_db, tmp_path, "linked-refresh"
    )
    registry_hash_before = LocalPlanManager(temp_db).get_plan(PLAN_ID).plan_hash
    LocalWorktreeManager(temp_db).register_adopted(
        project_id,
        branch_name="task-100",
        worktree_path=str(linked),
        base_branch="main",
    )

    result = await create_plan_registry(temp_db, coverage_executor=coverage_executor).call(
        "update_plan_hash", {"plan_id": PLAN_ID, "project": str(linked)}
    )

    assert result["ok"] is True, result
    assert result["plan"]["plan_hash"] == branch_hash
    assert branch_hash != registry_hash_before
    linked_manifest = coverage_manifest_path(
        linked, project_id=project_id, root_task_ref=root_ref, plan_id=PLAN_ID
    )
    main_manifest = coverage_manifest_path(
        main, project_id=project_id, root_task_ref=root_ref, plan_id=PLAN_ID
    )
    assert branch_hash in linked_manifest.read_text(encoding="utf-8")
    assert branch_hash not in main_manifest.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_update_plan_hash_refuses_unregistered_linked_worktree(
    temp_db: HubDatabase,
    tmp_path: Path,
    coverage_executor: CoverageExecutor,
) -> None:
    _main, linked, project_id, _root_ref, _branch_hash = _linked_checkout(
        temp_db, tmp_path, "linked-unregistered"
    )
    registry_hash_before = LocalPlanManager(temp_db).get_plan(PLAN_ID).plan_hash

    result = await create_plan_registry(temp_db, coverage_executor=coverage_executor).call(
        "update_plan_hash", {"plan_id": PLAN_ID, "project": str(linked)}
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_project"
    assert "not a registered worktree" in result["message"]
    assert LocalPlanManager(temp_db).get_plan(PLAN_ID).plan_hash == registry_hash_before


@pytest.mark.asyncio
async def test_update_plan_hash_refuses_linked_worktree_of_another_project(
    temp_db: HubDatabase,
    tmp_path: Path,
    coverage_executor: CoverageExecutor,
) -> None:
    _main, linked, _project_id, _root_ref, _branch_hash = _linked_checkout(
        temp_db, tmp_path, "linked-foreign"
    )
    other_project = _create_project(temp_db, tmp_path / "other", "other-checkout")
    LocalWorktreeManager(temp_db).register_adopted(
        other_project,
        branch_name="task-900",
        worktree_path=str(linked),
        base_branch="main",
    )

    result = await create_plan_registry(temp_db, coverage_executor=coverage_executor).call(
        "update_plan_hash", {"plan_id": PLAN_ID, "project": str(linked)}
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_project"
    assert "not a registered worktree" in result["message"]


@pytest.mark.asyncio
async def test_regenerate_coverage_manifest_names_the_registered_linked_manifest(
    temp_db: HubDatabase,
    tmp_path: Path,
    coverage_executor: CoverageExecutor,
) -> None:
    _main, linked, project_id, root_ref, branch_hash = _linked_checkout(
        temp_db, tmp_path, "linked-regenerate"
    )
    LocalWorktreeManager(temp_db).register_adopted(
        project_id,
        branch_name="task-100",
        worktree_path=str(linked),
        base_branch="main",
    )
    registry = create_plan_registry(temp_db, coverage_executor=coverage_executor)
    refreshed = await registry.call(
        "update_plan_hash", {"plan_id": PLAN_ID, "project": str(linked)}
    )
    assert refreshed["ok"] is True, refreshed

    result = await registry.call(
        "regenerate_coverage_manifest", {"plan_id": PLAN_ID, "project": str(linked)}
    )

    expected = coverage_manifest_path(
        linked, project_id=project_id, root_task_ref=root_ref, plan_id=PLAN_ID
    )
    assert result == {"ok": True, "manifest_path": str(expected)}
    assert branch_hash in expected.read_text(encoding="utf-8")
