"""Tests for gobby-plans MCP registry."""

from __future__ import annotations

import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.plans.review_evidence import PlanReviewEvidenceService
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


def _create_plan_daemon_tools(
    temp_db: HubDatabase,
    project_id: str,
) -> tuple[GobbyDaemonTools, SessionManager]:
    session_manager = SessionManager(temp_db)
    internal_manager = InternalRegistryManager()
    internal_manager.add_registry(
        create_plan_registry(temp_db, default_project_id=project_id),
    )
    mcp_manager = MagicMock()
    mcp_manager.project_id = project_id
    tools = GobbyDaemonTools(
        mcp_manager=mcp_manager,
        daemon_port=60887,
        websocket_port=60888,
        start_time=0.0,
        internal_manager=internal_manager,
        db=None,
        session_manager=session_manager,
    )
    return tools, session_manager


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

    def prepare_review_round(
        self: PlanReviewEvidenceService,
        **_kwargs: object,
    ) -> SimpleNamespace:
        worker_threads.append(threading.get_ident())
        return SimpleNamespace(to_dict=lambda: {"evidence_id": "evidence"})

    monkeypatch.setattr(LocalPlanManager, "create_plan", create_plan)
    monkeypatch.setattr(LocalPlanManager, "regenerate_coverage_manifest", regenerate_manifest)
    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "prepare_plan_review_round",
        prepare_review_round,
    )
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
    prepared = await registry.call(
        "prepare_plan_review_round",
        {
            "plan_path": str(tmp_path / "plan.md"),
            "round_number": 1,
        },
    )

    assert created == {"ok": True, "plan": {"plan_id": "plan"}}
    assert create_kwargs[0]["reactivate"] is True
    assert regenerated == {"ok": True, "manifest_path": str(manifest_path)}
    assert prepared == {"ok": True, "evidence_id": "evidence"}
    assert len(worker_threads) == 3
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
    snapshot_schema = registry.get_schema("get_plan_review_snapshot")
    assert snapshot_schema is not None
    snapshot_properties = snapshot_schema["inputSchema"]["properties"]
    assert set(snapshot_properties) == {"evidence_id"}
    coverage_schema = registry.get_schema("validate_plan_review_coverage")
    assert coverage_schema is not None
    coverage_properties = coverage_schema["inputSchema"]["properties"]
    assert "shadow_manifest_status" in coverage_properties
    assert "routing_decisions" not in coverage_properties

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


async def test_snapshot_returns_complete_decoded_document(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = (
        LocalProjectManager(temp_db)
        .create(name="review-evidence-tools", repo_path=str(tmp_path))
        .id
    )
    session = SessionManager(temp_db).register(
        external_id="review-evidence-tools",
        machine_id="21000000-0000-4000-8000-000000000002",
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
    snapshot = await registry.call(
        "get_plan_review_snapshot",
        {"evidence_id": prepared["evidence_id"]},
    )

    assert snapshot["ok"] is True
    assert snapshot["evidence_id"] == prepared["evidence_id"]
    assert snapshot["snapshot"] == expected
    assert snapshot["sections"] == prepared["sections"]


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


@pytest.mark.asyncio
async def test_prepare_review_round_uses_call_tool_envelope_session(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = (
        LocalProjectManager(temp_db)
        .create(name="envelope-review-evidence", repo_path=str(tmp_path))
        .id
    )
    tools, session_manager = _create_plan_daemon_tools(temp_db, project_id)
    session = session_manager.register(
        external_id="envelope-review-evidence",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    plan_path = _write_plan(tmp_path)

    result = await tools.call_tool(
        server_name="gobby-plans",
        tool_name="prepare_plan_review_round",
        arguments={
            "plan_path": str(plan_path),
            "round_number": 1,
        },
        session_id=session.id,
    )

    assert result["ok"] is True
    evidence = PlanReviewEvidenceService(temp_db).get_evidence(result["evidence_id"])
    assert evidence.session_id == session.id


@pytest.mark.asyncio
async def test_prepare_review_round_staged_binding_with_ambient_context(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = (
        LocalProjectManager(temp_db)
        .create(name="staged-review-evidence", repo_path=str(tmp_path))
        .id
    )
    tools, session_manager = _create_plan_daemon_tools(temp_db, project_id)
    ambient_session = session_manager.register(
        external_id="staged-review-evidence-ambient",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Review staged evidence",
        validation_criteria="Staged review evidence binds to its task and stage.",
    )
    plan_path = _write_plan(tmp_path)

    result = await tools.call_tool(
        server_name="gobby-plans",
        tool_name="prepare_plan_review_round",
        arguments={
            "plan_path": str(plan_path),
            "round_number": 1,
            "task_id": task.id,
            "stage": "development",
        },
        session_id=ambient_session.id,
    )

    assert result["ok"] is True
    evidence = PlanReviewEvidenceService(temp_db).get_evidence(result["evidence_id"])
    assert evidence.session_id is None
    assert evidence.task_id == task.id
    assert evidence.stage == "development"


@pytest.mark.asyncio
async def test_prepare_review_round_explicit_session_wins_over_ambient_context(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = (
        LocalProjectManager(temp_db)
        .create(name="explicit-review-evidence", repo_path=str(tmp_path))
        .id
    )
    tools, session_manager = _create_plan_daemon_tools(temp_db, project_id)
    ambient_session = session_manager.register(
        external_id="explicit-review-evidence-ambient",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    explicit_session = session_manager.register(
        external_id="explicit-review-evidence-target",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project_id,
    )
    plan_path = _write_plan(tmp_path)

    result = await tools.call_tool(
        server_name="gobby-plans",
        tool_name="prepare_plan_review_round",
        arguments={
            "plan_path": str(plan_path),
            "round_number": 1,
            "session_id": explicit_session.id,
        },
        session_id=ambient_session.id,
    )

    assert result["ok"] is True
    evidence = PlanReviewEvidenceService(temp_db).get_evidence(result["evidence_id"])
    assert evidence.session_id == explicit_session.id
