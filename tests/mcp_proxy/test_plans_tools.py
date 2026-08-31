"""Tests for gobby-plans MCP registry."""

from __future__ import annotations

import asyncio
import hashlib
import textwrap
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.code_index.models import (
    CODE_INDEX_UUID_NAMESPACE,
    IndexedFile,
    IndexedProject,
    IndexWriteMode,
)
from gobby.code_index.storage import CodeIndexStorage
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.tools import plans as plans_tools
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.concurrency import CoverageExecutor
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.project_context import reset_project_context, set_project_context
from tests.fixtures.isolated_checkout import write_project_marker

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


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


def _write_plan(
    root: Path,
    target: str = "docs/demo.md",
    scope_reason: str | None = None,
) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "task-100-demo.md"
    target_file = target.split("::", maxsplit=1)[0]
    target_suffix = f" — scope-reason: {scope_reason}" if scope_reason else ""
    path.write_text(
        textwrap.dedent(
            f"""
            > **Plan ID:** task-100-demo

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work [category: docs]
            `kind: deliverable`

            Target: `{target}`{target_suffix}

            Body.

            **Acceptance:**
            - 1.1.1 — Docs exist. file: `{target_file}`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def _create_project(db: HubDatabase, root: Path, name: str) -> str:
    project_id = str(uuid.uuid4())
    write_project_marker(root, project_id=project_id, name=name)
    return (
        LocalProjectManager(db)
        .create(
            name=name,
            repo_path=str(root),
            project_id=project_id,
        )
        .id
    )


def _create_indexed_project(db: HubDatabase, root: Path) -> str:
    project_id = _create_project(db, root, f"plans-validation-{root.name}")
    indexed_path = root / "docs" / "demo.md"
    indexed_path.parent.mkdir(parents=True, exist_ok=True)
    indexed_path.write_text("Demo.\n", encoding="utf-8")
    content_hash = hashlib.sha256(indexed_path.read_bytes()).hexdigest()

    code_index = CodeIndexStorage(db)
    code_index.upsert_project_stats(
        IndexedProject(
            id=project_id,
            root_path=str(root),
            total_files=1,
            total_symbols=0,
        ),
        mode=IndexWriteMode.OVERLAY,
    )
    code_index.upsert_file(
        IndexedFile(
            id=IndexedFile.make_id(project_id, "docs/demo.md", content_hash),
            project_id=project_id,
            file_path="docs/demo.md",
            language="markdown",
            content_hash=content_hash,
            symbol_count=0,
            byte_size=indexed_path.stat().st_size,
        ),
        root_path=str(root),
        mode=IndexWriteMode.OVERLAY,
    )
    return project_id


def _write_large_plan(root: Path) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "task-100-large.md"
    deliverables = []
    for deliverable_index in range(1, 16):
        criteria = "\n".join(
            f"- 1.{deliverable_index}.{criterion_index} — Criterion {criterion_index}. "
            f"file: `docs/deliverable-{deliverable_index}.md`"
            for criterion_index in range(1, 4)
        )
        deliverables.append(
            textwrap.dedent(
                f"""
                ### 1.{deliverable_index} Work {deliverable_index} [category: docs]
                `kind: deliverable`

                Target: `docs/deliverable-{deliverable_index}.md`

                Body.

                **Acceptance:**
                {criteria}
                """
            ).strip()
        )
    path.write_text(
        "> **Plan ID:** task-100-large\n\n"
        "## P1 Phase\n"
        "`kind: framing`\n\n" + "\n\n".join(deliverables) + "\n",
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
    coverage_executor: CoverageExecutor,
) -> None:
    caller_thread = threading.get_ident()
    db_threads: list[int] = []
    coverage_threads: list[int] = []
    other_worker_threads: list[int] = []
    create_kwargs: list[dict[str, object]] = []
    manifest_path = tmp_path / "plan.coverage.yaml"
    record = SimpleNamespace(
        plan_kind="implementation",
        to_dict=lambda: {"plan_id": "plan"},
    )

    def create_plan_record(self: LocalPlanManager, **kwargs: object) -> SimpleNamespace:
        db_threads.append(threading.get_ident())
        create_kwargs.append(kwargs)
        return record

    def update_plan_hash_record(
        self: LocalPlanManager,
        plan_id: str,
        *,
        project_id: str | None = None,
    ) -> tuple[SimpleNamespace, bool]:
        db_threads.append(threading.get_ident())
        return record, True

    def get_plan(
        self: LocalPlanManager,
        plan_id: str,
        *,
        project_id: str | None = None,
    ) -> SimpleNamespace:
        db_threads.append(threading.get_ident())
        return record

    def generate_coverage_manifest(
        self: LocalPlanManager,
        _record: object,
    ) -> Path:
        coverage_threads.append(threading.get_ident())
        return manifest_path

    def prepare_review_round(
        self: PlanReviewEvidenceService,
        **_kwargs: object,
    ) -> SimpleNamespace:
        other_worker_threads.append(threading.get_ident())
        return SimpleNamespace(to_dict=lambda: {"evidence_id": "evidence"})

    monkeypatch.setattr(LocalPlanManager, "create_plan_record", create_plan_record)
    monkeypatch.setattr(LocalPlanManager, "update_plan_hash_record", update_plan_hash_record)
    monkeypatch.setattr(LocalPlanManager, "get_plan", get_plan)
    monkeypatch.setattr(LocalPlanManager, "generate_coverage_manifest", generate_coverage_manifest)
    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "prepare_plan_review_round",
        prepare_review_round,
    )
    executor = DatabaseExecutor(max_workers=1)
    registry = create_plan_registry(
        temp_db,
        default_project_id="project-1",
        run_db=executor.run,
        coverage_executor=coverage_executor,
    )

    try:
        created = await registry.call(
            "create_plan",
            {
                "plan_id": "plan",
                "plan_path": str(tmp_path / "plan.md"),
                "root_task_ref": "#1",
                "reactivate": True,
            },
        )
        updated = await registry.call("update_plan_hash", {"plan_id": "plan"})
        regenerated = await registry.call("regenerate_coverage_manifest", {"plan_id": "plan"})
        prepared = await registry.call(
            "prepare_plan_review_round",
            {
                "plan_path": str(tmp_path / "plan.md"),
                "round_number": 1,
            },
        )
    finally:
        executor.shutdown(cancel_futures=False)
        await asyncio.to_thread(executor.join)

    assert created == {"ok": True, "plan": {"plan_id": "plan"}}
    assert create_kwargs[0]["reactivate"] is True
    assert updated == {"ok": True, "plan": {"plan_id": "plan"}}
    assert regenerated == {"ok": True, "manifest_path": str(manifest_path)}
    assert prepared == {"ok": True, "evidence_id": "evidence"}
    assert len(db_threads) == 3
    assert len(coverage_threads) == 3
    assert all(thread_id != caller_thread for thread_id in db_threads + coverage_threads)
    assert set(db_threads).isdisjoint(coverage_threads)
    assert all(thread_id != caller_thread for thread_id in other_worker_threads)


async def test_large_plan_coverage_does_not_starve_short_db_job(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coverage_executor: CoverageExecutor,
) -> None:
    project_id = _create_project(temp_db, tmp_path, "large-plan")
    root_task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Large plan root",
        validation_criteria="Large-plan coverage evaluation must leave the DB lane responsive.",
    )
    plan_path = _write_large_plan(tmp_path)
    coverage_started = threading.Event()
    release_coverage = threading.Event()

    def slow_coverage(_self: LocalPlanManager, _record: object) -> Path:
        coverage_started.set()
        if not release_coverage.wait(timeout=3):
            raise TimeoutError("coverage test release was not signaled")
        return tmp_path / "task-100-large.coverage.yaml"

    monkeypatch.setattr(LocalPlanManager, "generate_coverage_manifest", slow_coverage)
    executor = DatabaseExecutor(max_workers=1)
    registry = create_plan_registry(
        temp_db,
        default_project_id=project_id,
        run_db=executor.run,
        coverage_executor=coverage_executor,
    )
    registration = asyncio.create_task(
        registry.call(
            "create_plan",
            {
                "plan_id": "task-100-large",
                "plan_path": str(plan_path),
                "root_task_ref": f"#{root_task.seq_num}",
            },
        )
    )

    try:
        coverage_in_flight = await asyncio.wait_for(
            asyncio.to_thread(coverage_started.wait, 1),
            timeout=1.1,
        )
        assert coverage_in_flight
        started_at = time.monotonic()
        result = await asyncio.wait_for(executor.run(lambda: "responsive"), timeout=0.75)
        elapsed = time.monotonic() - started_at
    finally:
        release_coverage.set()
        response = await registration
        executor.shutdown(cancel_futures=False)
        await asyncio.to_thread(executor.join)

    assert response["ok"] is True
    assert result == "responsive"
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_plan_tool_schemas_and_happy_path(
    temp_db: HubDatabase,
    tmp_path: Path,
    coverage_executor: CoverageExecutor,
) -> None:
    project_id = _create_project(temp_db, tmp_path, "plans")
    root_task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Plan root",
        validation_criteria="Plan tool operations preserve the root task.",
    )
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(
        temp_db,
        default_project_id=project_id,
        coverage_executor=coverage_executor,
    )

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
        "derive_plan_handoff_manifest",
        "apply_plan_handoff_manifest",
        "apply_plan_review_manifest",
        "render_plan_changelog_round",
        "append_plan_changelog_round",
        "finalize_plan_review_evidence",
        "apply_plan_review_repairs",
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
    append_schema = registry.get_schema("append_plan_changelog_round")
    assert append_schema is not None
    assert set(append_schema["inputSchema"]["required"]) == {"evidence_id", "prose"}
    handoff_schema = registry.get_schema("apply_plan_handoff_manifest")
    assert handoff_schema is not None
    assert set(handoff_schema["inputSchema"]["required"]) == {
        "plan_path",
        "routing_decisions",
        "source_plan_hash",
        "rendered_plan_hash",
        "manifest_digest",
    }

    created = await registry.call(
        "create_plan",
        {
            "plan_id": "task-100-demo",
            "plan_path": str(plan_path),
            "root_task_ref": f"#{root_task.seq_num}",
        },
    )
    assert created["ok"] is True

    routing = {
        "1.1": {
            "category": "docs",
            "assigned_agent": "tech-writer",
            "tdd": False,
        }
    }
    handoff = await registry.call(
        "derive_plan_handoff_manifest",
        {"plan_path": str(plan_path), "routing_decisions": routing},
    )
    assert handoff["ok"] is True
    assert "rendered_plan" not in handoff
    applied = await registry.call(
        "apply_plan_handoff_manifest",
        {
            "plan_path": str(plan_path),
            "routing_decisions": routing,
            "source_plan_hash": handoff["source_plan_hash"],
            "rendered_plan_hash": handoff["rendered_plan_hash"],
            "manifest_digest": handoff["manifest_digest"],
        },
    )
    assert applied["ok"] is True
    assert applied["result"]["applied"] is True

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
    project_id = _create_project(temp_db, tmp_path, "review-evidence-tools")
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
    coverage_executor: CoverageExecutor,
) -> None:
    project_id = _create_project(temp_db, tmp_path, "plans")
    root_task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Plan root",
        validation_criteria="Unresolvable project deletion preserves the scoped plan.",
    )
    plan_path = _write_plan(tmp_path)
    registry = create_plan_registry(
        temp_db,
        default_project_id=project_id,
        coverage_executor=coverage_executor,
    )
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
    project_id = _create_project(temp_db, tmp_path, "plans")
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
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = _write_plan(tmp_path)
    project_id = _create_indexed_project(temp_db, tmp_path)
    monkeypatch.setattr(plans_tools, "get_project_context", lambda: None)
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    assert "validate_plan" in {tool["name"] for tool in registry.list_tools()}

    result = await registry.call(
        "validate_plan",
        {"plan_file": str(plan_path.relative_to(tmp_path))},
    )

    assert result["valid"] is True
    assert result["phase_count"] >= 1
    assert result["deliverable_count"] >= 1
    assert result["warnings"] == []
    assert result["symbol_validation"] == {
        "status": "passed",
        "issues": [],
        "checked_targets": ["docs/demo.md"],
        "checked_symbols": [],
    }


@pytest.mark.asyncio
async def test_validate_plan_uses_complete_isolated_context(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    parent_root = tmp_path / "parent"
    worktree_root = tmp_path / "worktree"
    parent_root.mkdir()
    worktree_root.mkdir()
    project_id = _create_indexed_project(temp_db, parent_root)
    plan_path = _write_plan(worktree_root)
    indexed_path = worktree_root / "docs" / "demo.md"
    indexed_path.parent.mkdir(parents=True)
    indexed_path.write_text("Worktree demo.\n", encoding="utf-8")
    content_hash = hashlib.sha256(indexed_path.read_bytes()).hexdigest()
    overlay_id = str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(worktree_root.resolve())))
    code_index = CodeIndexStorage(temp_db)
    code_index.upsert_project_stats(
        IndexedProject(
            id=overlay_id,
            root_path=str(worktree_root),
            total_files=1,
            total_symbols=0,
        ),
        mode=IndexWriteMode.OVERLAY,
    )
    code_index.upsert_file(
        IndexedFile(
            id=IndexedFile.make_id(overlay_id, "docs/demo.md", content_hash),
            project_id=overlay_id,
            file_path="docs/demo.md",
            language="markdown",
            content_hash=content_hash,
            symbol_count=0,
            byte_size=indexed_path.stat().st_size,
        ),
        root_path=str(worktree_root),
        mode=IndexWriteMode.OVERLAY,
    )
    registry = create_plan_registry(temp_db)
    token = set_project_context(
        {
            "id": project_id,
            "project_path": str(worktree_root),
            "parent_project_id": project_id,
            "parent_project_path": str(parent_root),
        }
    )
    try:
        result = await registry.call(
            "validate_plan",
            {"plan_file": str(plan_path.relative_to(worktree_root))},
        )
    finally:
        reset_project_context(token)

    assert result["valid"] is True
    assert result["symbol_validation"]["status"] == "passed"


@pytest.mark.asyncio
async def test_validate_plan_returns_same_payload_as_tasks_ops(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    """gobby-plans:validate_plan must mirror gobby-tasks-ops:validate_plan_file."""
    from gobby.tasks.expansion_service import ExpansionService

    plan_path = _write_plan(tmp_path)
    project_id = _create_indexed_project(temp_db, tmp_path)
    registry = create_plan_registry(temp_db)

    token = set_project_context({"id": project_id, "project_path": str(tmp_path)})
    try:
        plans_result = await registry.call(
            "validate_plan",
            {"plan_file": str(plan_path.relative_to(tmp_path))},
        )
    finally:
        reset_project_context(token)

    service = ExpansionService(task_manager=LocalTaskManager(temp_db), llm_service=MagicMock())
    tasks_ops_result = service.validate_plan_file(
        plan_path,
        project_context={"id": project_id, "project_path": str(tmp_path)},
        code_index=CodeIndexStorage(temp_db),
        require_symbol_validation=True,
    )

    assert plans_result == tasks_ops_result


@pytest.mark.asyncio
async def test_validate_plan_rejects_unindexed_wildcard_target(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    project_id = _create_indexed_project(temp_db, tmp_path)
    unindexed_path = tmp_path / ".codex" / "rules.py"
    unindexed_path.parent.mkdir(parents=True)
    unindexed_path.write_text("RULES = {}\n", encoding="utf-8")
    plan_path = _write_plan(
        tmp_path,
        ".codex/rules.py::*",
        scope_reason="dot-directory configuration",
    )
    registry = create_plan_registry(temp_db)

    token = set_project_context({"id": project_id, "project_path": str(tmp_path)})
    try:
        result = await registry.call(
            "validate_plan",
            {"plan_file": str(plan_path.relative_to(tmp_path))},
        )
    finally:
        reset_project_context(token)

    assert result["valid"] is False
    assert result["symbol_validation"]["status"] == "failed"
    assert result["symbol_validation"]["checked_targets"] == [
        ".codex/rules.py::*",
    ]
    assert {issue["code"] for issue in result["symbol_validation"]["issues"]} == {
        "target_symbol_unresolved",
    }


@pytest.mark.asyncio
async def test_validate_plan_fails_closed_without_project_context(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    monkeypatch.setattr(plans_tools, "get_project_context", lambda: None)
    registry = create_plan_registry(temp_db)

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is False
    assert result["symbol_validation"]["status"] == "failed"
    assert {issue["code"] for issue in result["symbol_validation"]["issues"]} == {
        "symbol_index_unavailable",
    }


@pytest.mark.asyncio
async def test_validate_plan_returns_warnings_for_missing_plan_id(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    project_id = _create_project(temp_db, tmp_path, "missing-plan-id")
    plan_path = _write_plan_without_plan_id(tmp_path)
    registry = create_plan_registry(temp_db, default_project_id=project_id)

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
    project_id = _create_project(temp_db, tmp_path, "old-phase-plan")
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
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is False
    assert any("phase sections" in err for err in result["errors"])


@pytest.mark.asyncio
async def test_validate_plan_returns_semantic_lint_errors(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    project_id = _create_project(temp_db, tmp_path, "semantic-lint-plan")
    plan_path = _write_plan(tmp_path)
    text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(text.replace("Target: `docs/demo.md`\n\n", ""), encoding="utf-8")
    registry = create_plan_registry(temp_db, default_project_id=project_id)

    result = await registry.call("validate_plan", {"plan_file": str(plan_path)})

    assert result["valid"] is False
    assert any("target-coverage" in error for error in result["errors"])
    assert result["semantic_lint"]["valid"] is False


@pytest.mark.asyncio
async def test_prepare_review_round_uses_call_tool_envelope_session(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _create_project(temp_db, tmp_path, "envelope-review-evidence")
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
    project_id = _create_project(temp_db, tmp_path, "staged-review-evidence")
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
    project_id = _create_project(temp_db, tmp_path, "explicit-review-evidence")
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


async def test_apply_plan_review_repairs_registered(
    temp_db: HubDatabase,
    tmp_path: Path,
    coverage_executor: CoverageExecutor,
) -> None:
    project_id = _create_project(temp_db, tmp_path, "plans")
    registry = create_plan_registry(
        temp_db,
        default_project_id=project_id,
        coverage_executor=coverage_executor,
    )

    schema = registry.get_schema("apply_plan_review_repairs")
    assert schema is not None
    input_schema = schema["inputSchema"]
    assert set(input_schema["properties"]) == {"evidence_id", "accepted_finding_ids"}
    assert set(input_schema["required"]) == {"evidence_id", "accepted_finding_ids"}
    assert input_schema["properties"]["accepted_finding_ids"]["items"] == {"type": "string"}

    expected = {"ok": True, "evidence_id": "evidence-1", "changed": False}
    with patch(
        "gobby.plans.review_evidence.PlanReviewEvidenceService.apply_plan_review_repairs",
        return_value=expected,
    ) as apply:
        result = await registry.call(
            "apply_plan_review_repairs",
            {"evidence_id": "evidence-1", "accepted_finding_ids": ["F1", "F2"]},
        )

    assert result == expected
    apply.assert_called_once_with("evidence-1", ["F1", "F2"])
