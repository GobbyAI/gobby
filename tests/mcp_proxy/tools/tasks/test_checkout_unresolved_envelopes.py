"""Task MCP tools return the typed checkout_unresolved envelope instead of raising."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import create_expansion_registry
from gobby.mcp_proxy.tools.tasks._lifecycle_paths import _lifecycle_checkout_root
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _NoCheckout:
    """An isolated machine whose project has no registered checkout."""

    machine_id: str
    project_id: str
    task_manager: LocalTaskManager
    session_id: str
    task: Any


@pytest.fixture
def no_checkout(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> _NoCheckout:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(
        name="no-checkout", github_url="https://github.com/test/no-checkout"
    )
    session = SessionManager(temp_db).register(
        external_id="no-checkout-session",
        machine_id=machine_id,
        source="codex",
        project_id=project.id,
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="Task whose project has no checkout",
        task_type="task",
        category="code",
        implementation_domain="backend",
        validation_criteria="Checkout failures return typed envelopes.",
        claimed_by_session_id=session.id,
    )
    return _NoCheckout(
        machine_id=machine_id,
        project_id=project.id,
        task_manager=task_manager,
        session_id=session.id,
        task=task,
    )


def _assert_checkout_unresolved(result: dict[str, Any]) -> None:
    assert result["success"] is False
    assert result["error_type"] == "checkout_unresolved"
    assert "no checkout for machine" in result["error"]


@pytest.mark.asyncio
async def test_start_expansion_run_with_plan_file_returns_checkout_unresolved(
    no_checkout: _NoCheckout,
) -> None:
    registry = create_expansion_registry(RegistryContext(task_manager=no_checkout.task_manager))

    with session_context_for_test(no_checkout.session_id):
        result = await registry.call(
            "start_expansion_run",
            {"task_id": no_checkout.task.id, "plan_file": "docs/plans/plan.md"},
        )

    _assert_checkout_unresolved(result)


@pytest.mark.asyncio
async def test_run_expansion_qa_coverage_returns_checkout_unresolved(
    no_checkout: _NoCheckout,
) -> None:
    registry = create_expansion_registry(RegistryContext(task_manager=no_checkout.task_manager))
    run = LocalExpansionRunManager(no_checkout.task_manager.db).create(
        parent_task_id=no_checkout.task.id,
        project_id=no_checkout.project_id,
        triggering_session_id=None,
        input_source="task",
    )

    result = await registry.call(
        "run_expansion_qa_coverage",
        {
            "run_id": run.id,
            "plan_path": "docs/plans/plan.md",
            "plan_id": "plan-1",
            "plan_hash": "0" * 64,
            "root_task": f"#{no_checkout.task.seq_num}",
            "project_id": no_checkout.project_id,
        },
    )

    assert result["ok"] is False
    _assert_checkout_unresolved(result)


@pytest.mark.asyncio
async def test_validate_plan_file_returns_checkout_unresolved(
    no_checkout: _NoCheckout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = create_expansion_registry(RegistryContext(task_manager=no_checkout.task_manager))
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._expansion_registry._build_expansion_service",
        lambda _ctx: object(),
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._expansion_registry.get_project_context", lambda: None
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._context.get_project_context",
        lambda: {"id": no_checkout.project_id},
    )

    result = await registry.call("validate_plan_file", {"plan_file": "docs/plans/plan.md"})

    _assert_checkout_unresolved(result)


@pytest.mark.asyncio
async def test_open_delivery_pr_returns_checkout_unresolved(no_checkout: _NoCheckout) -> None:
    registry = create_task_ops_registry(no_checkout.task_manager)

    result = await registry.call(
        "open_delivery_pr", {"task_id": no_checkout.task.id, "target_branch": "main"}
    )

    assert result["ok"] is False
    assert result["task_id"] == no_checkout.task.id
    _assert_checkout_unresolved(result)


@pytest.mark.asyncio
async def test_submit_for_review_returns_checkout_unresolved(no_checkout: _NoCheckout) -> None:
    registry = create_task_ops_registry(no_checkout.task_manager)

    with session_context_for_test(no_checkout.session_id):
        result = await registry.call(
            "submit_for_review",
            {"task_id": no_checkout.task.id, "stage_name": "development"},
        )

    _assert_checkout_unresolved(result)


@pytest.mark.asyncio
async def test_inspect_task_path_ownership_returns_checkout_unresolved(
    no_checkout: _NoCheckout,
) -> None:
    registry = create_task_registry(no_checkout.task_manager)

    with session_context_for_test(no_checkout.session_id):
        result = await registry.call("inspect_task_path_ownership", {})

    _assert_checkout_unresolved(result)


def test_lifecycle_checkout_root_loads_the_session_once(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    session = SessionManager(temp_db).register(
        external_id="checkout-root-session",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=isolated.project.id,
    )
    ctx = RegistryContext(task_manager=LocalTaskManager(temp_db))
    lookups: list[str] = []
    real_get = ctx.session_manager.get

    def counting_get(session_id: str) -> Session | None:
        lookups.append(session_id)
        return real_get(session_id)

    monkeypatch.setattr(ctx.session_manager, "get", counting_get)

    root = _lifecycle_checkout_root(
        ctx,
        session_id=session.id,
        project_id=isolated.project.id,
        overlay_path=None,
    )

    assert root == isolated.root_path
    assert lookups == [session.id]
