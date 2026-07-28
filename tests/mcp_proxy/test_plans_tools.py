"""Tests for gobby-plans MCP registry."""

from __future__ import annotations

import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _write_plan(root: Path) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "task-100-demo.md"
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** task-100-demo

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


def _write_plan_without_plan_id(root: Path) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "missing-id.md"
    path.write_text(
        textwrap.dedent(
            """
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


@pytest.mark.asyncio
async def test_plan_storage_tools_dispatch_off_event_loop(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []
    create_kwargs: list[dict[str, object]] = []
    manifest_path = tmp_path / "plan.coverage.yaml"

    def create_plan(self: LocalPlanManager, **kwargs: object) -> SimpleNamespace:
        worker_threads.append(threading.get_ident())
        create_kwargs.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"plan_id": "plan"})

    def regenerate_manifest(
        self: LocalPlanManager,
        plan_id: str,
        *,
        project_id: str | None = None,
    ) -> Path:
        worker_threads.append(threading.get_ident())
        return manifest_path

    monkeypatch.setattr(LocalPlanManager, "create_plan", create_plan)
    monkeypatch.setattr(LocalPlanManager, "regenerate_coverage_manifest", regenerate_manifest)
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    created = await registry.call(
        "create_plan",
        {
            "plan_id": "plan",
            "plan_path": str(tmp_path / "plan.md"),
            "root_task_ref": "#1",
            "reactivate": True,
        },
    )
    regenerated = await registry.call("regenerate_coverage_manifest", {"plan_id": "plan"})

    assert created == {"ok": True, "plan": {"plan_id": "plan"}}
    assert create_kwargs[0]["reactivate"] is True
    assert regenerated == {"ok": True, "manifest_path": str(manifest_path)}
    assert len(worker_threads) == 2
    assert all(thread_id != caller_thread for thread_id in worker_threads)


@pytest.mark.asyncio
async def test_plan_tool_schemas_and_happy_path(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(tmp_path)).id
    root_task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Plan root",
        validation_criteria="Plan tool operations preserve the root task.",
    )
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    names = {tool["name"] for tool in registry.list_tools()}
    assert {
        "create_plan",
        "get_plan",
        "list_plans",
        "archive_plan",
        "update_plan_hash",
        "regenerate_coverage_manifest",
        "delete_plan",
        "prepare_plan_review_round",
        "get_plan_review_snapshot",
        "bind_evidence_run",
        "expire_plan_review_evidence",
        "verify_plan_unchanged",
        "apply_plan_review_manifest",
        "render_v1_round_checkpoint",
        "finalize_plan_review_evidence",
        "checkpoint_plan_review_lesson_mint",
    } <= names
    assert registry.get_schema("create_plan") is not None

    created = await registry.call(
        "create_plan",
        {
            "plan_id": "task-100-demo",
            "plan_path": str(plan_path),
            "root_task_ref": f"#{root_task.seq_num}",
        },
    )
    assert created["ok"] is True

    listed = await registry.call("list_plans", {"state": "active"})
    assert listed["count"] == 1
    assert listed["plans"][0]["plan_id"] == "task-100-demo"

    archived = await registry.call("archive_plan", {"plan_id": "task-100-demo"})
    assert archived["ok"] is True
    assert archived["plan"]["state"] == "archived"


async def test_review_evidence_tools_prepare_and_return_pinned_snapshot(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = (
        LocalProjectManager(temp_db)
        .create(
            name="review-evidence-tools",
            repo_path=str(tmp_path),
        )
        .id
    )
    session = SessionManager(temp_db).register(
        external_id="review-evidence-tools",
        machine_id="test-machine",
        source="codex",
        project_id=project_id,
    )
    plan_path = _write_plan(tmp_path)
    expected = plan_path.read_text()
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    prepared = await registry.call(
        "prepare_plan_review_round",
        {
            "plan_path": str(plan_path),
            "round_number": 1,
            "session_id": session.id,
        },
    )
    assert prepared["ok"] is True
    assert prepared["sections"][0]["section_id"] == "__preamble__"

    snapshot = await registry.call(
        "get_plan_review_snapshot",
        {"evidence_id": prepared["evidence_id"]},
    )
    assert snapshot["ok"] is True
    assert snapshot["snapshot"] == expected


@pytest.mark.asyncio
async def test_plan_tools_return_invalid_ref_for_blank_plan_ref(
    temp_db: HubDatabase,
) -> None:
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    result = await registry.call("get_plan", {"plan_id_or_ref": "   "})

    assert result["ok"] is False
    assert result["error"] == "invalid_ref"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_plan", {"plan_id_or_ref": "task-100-demo"}),
        ("list_plans", {}),
        ("archive_plan", {"plan_id": "task-100-demo"}),
        ("update_plan_hash", {"plan_id": "task-100-demo"}),
        ("regenerate_coverage_manifest", {"plan_id": "task-100-demo"}),
        ("delete_plan", {"plan_id": "task-100-demo"}),
    ],
)
async def test_plan_tools_reject_unresolvable_project_ref(
    temp_db: HubDatabase,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    registry = create_plan_registry(temp_db)

    result = await registry.call(tool_name, {**arguments, "project": "missing-project"})

    assert result["ok"] is False
    assert result["error"] == "invalid_project"


@pytest.mark.asyncio
async def test_delete_plan_with_unresolvable_project_does_not_delete_unscoped_plan(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(tmp_path)).id
    root_task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Plan root",
        validation_criteria="Unresolvable project deletion preserves the scoped plan.",
    )
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id=project_id)
    created = await registry.call(
        "create_plan",
        {
            "plan_id": "task-100-demo",
            "plan_path": str(plan_path),
            "root_task_ref": f"#{root_task.seq_num}",
        },
    )
    assert created["ok"] is True

    result = await registry.call(
        "delete_plan",
        {"plan_id": "task-100-demo", "project": "missing-project"},
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_project"
    preserved = LocalPlanManager(temp_db).get_plan("task-100-demo", project_id=project_id)
    assert preserved.plan_id == "task-100-demo"


@pytest.mark.asyncio
async def test_create_plan_rejects_invalid_plan_kind(temp_db: HubDatabase, tmp_path: Path) -> None:
    project_id = LocalProjectManager(temp_db).create(name="plans", repo_path=str(tmp_path)).id
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    result = await registry.call(
        "create_plan",
        {
            "plan_id": "task-100-demo",
            "plan_path": str(plan_path),
            "plan_kind": "speculative",
            "root_task_ref": "#100",
        },
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_plan_kind"
    assert "implementation" in result["message"]
    assert "strategy" in result["message"]


@pytest.mark.asyncio
async def test_validate_plan_returns_valid_for_canonical_plan(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    assert "validate_plan" in {tool["name"] for tool in registry.list_tools()}

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is True
    assert result["phase_count"] >= 1
    assert result["deliverable_count"] >= 1
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_validate_plan_returns_same_payload_as_tasks_ops(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    """gobby-plans:validate_plan must mirror gobby-tasks-ops:validate_plan_file."""
    from unittest.mock import MagicMock

    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.expansion_service import ExpansionService

    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    plans_result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    service = ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())
    tasks_ops_result = service.validate_plan_file(plan_path)

    assert plans_result == tasks_ops_result


@pytest.mark.asyncio
async def test_validate_plan_returns_warnings_for_missing_plan_id(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    plan_path = _write_plan_without_plan_id(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is False
    assert result["errors"] == result["warnings"]
    assert len(result["warnings"]) == 1
    assert "real **Plan ID:**" in result["warnings"][0]
    assert "covers:unknown:*" in result["warnings"][0]


@pytest.mark.asyncio
async def test_validate_plan_rejects_plan_with_old_phase_form(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    plan_dir = tmp_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "broken.md"
    plan_path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** broken

            ## Phase 1: Setup
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
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is False
    assert any("phase sections" in err for err in result["errors"])


@pytest.mark.asyncio
async def test_validate_plan_returns_semantic_lint_errors(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    plan_path = _write_plan(tmp_path)
    text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(text.replace("Target: `docs/demo.md`\n\n", ""), encoding="utf-8")
    registry = create_plan_registry(temp_db, default_project_id="project-1")

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is False
    assert any("target-coverage" in error for error in result["errors"])
    assert result["semantic_lint"]["valid"] is False
