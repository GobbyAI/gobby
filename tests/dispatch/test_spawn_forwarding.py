"""Plan 4.2.7: terminal_backend survives dispatch and scheduler forwarding."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.dispatch._planning_enhancement import _spawn_plan_enhancer
from gobby.dispatch._rule_actions import _spawn_stage_agent
from gobby.dispatch.actions import SpawnAgentAction
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron_models import CronJob
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SPAWN_IMPL_SCAN_PATHS = (
    ROOT / "src/gobby/dispatch/spawn.py",
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_factory.py",
    ROOT / "src/gobby/mcp_proxy/tools/spawn_agent/_execution.py",
    ROOT / "src/gobby/servers/routes/agent_spawn.py",
    ROOT / "src/gobby/scheduler/executor.py",
    ROOT / "tests/build/test_dispatcher_stage_wake.py",
    ROOT / "tests/build_pipeline/test_build_pipeline_service.py",
    ROOT / "tests/dispatch/test_dispatcher.py",
    ROOT / "tests/e2e/test_build_dispatcher_autonomy.py",
    ROOT / "tests/mcp_proxy/tools/spawn_agent/test_error_handling.py",
    ROOT / "tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py",
    ROOT / "tests/mcp_proxy/tools/test_spawn_agent_speed.py",
    ROOT / "tests/storage/test_stage_review_findings.py",
    ROOT / "tests/tasks/test_plan_gate.py",
)


def _is_spawn_agent_impl_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "spawn_agent_impl":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "spawn_agent_impl"


def spawn_impl_calls_missing_backend(path: Path) -> list[int]:
    if not path.exists():
        return [0]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_spawn_agent_impl_call(node):
            continue
        if node.args and not node.keywords:
            continue
        names = {kw.arg for kw in node.keywords if kw.arg is not None}
        if any(kw.arg is None for kw in node.keywords):
            continue
        if "terminal_backend" not in names:
            missing.append(getattr(node, "lineno", 0))
    return missing


def test_spawn_agent_impl_call_sites_forward_terminal_backend() -> None:
    for path in SPAWN_IMPL_SCAN_PATHS:
        missing = spawn_impl_calls_missing_backend(path)
        assert missing == [], f"{path} spawn_agent_impl calls omit terminal_backend: {missing}"


@pytest.mark.asyncio
async def test_terminal_backend_reaches_the_effect(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.sessions import SessionManager
    from gobby.storage.terminals import TerminalManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Native dispatch spawn",
        task_type="task",
        category="code",
        allow_automation=True,
        isolation="none",
        validation_criteria="Spawn forwarding is observable.",
    )
    captured: dict[str, object] = {}
    created: list[str] = []

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        backend = str(kwargs.get("terminal_backend") or "tmux")
        manager = TerminalManager(temp_db)
        terminal_id = str(uuid4())
        manager.create_pending(
            terminal_id,
            sample_project["id"],
            backend,
            "gobby",
            terminal_id if backend == "native" else f"gobby-{terminal_id}",
        )
        created.append(terminal_id)
        return {"success": True, "run_id": str(uuid4())}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(
        "gobby.dispatch.spawn.inspect_skill_composition",
        lambda *_args, **_kwargs: SimpleNamespace(failure_reason=None, allowed_tools=()),
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )
    stage = SimpleNamespace(name="planning", stage_name="planning", state="ready", position=0)
    context = SimpleNamespace(prompt_context={})
    rule_action = _spawn_stage_agent(
        SimpleNamespace(id=task.id, ref=f"#{task.seq_num}", additional_skills=()),
        stage,
        context,
        "backend-developer",
    )
    native_action = SpawnAgentAction(
        task_id=rule_action.task_id,
        task_ref=rule_action.task_ref,
        agent_slug=rule_action.agent_slug,
        prompt=rule_action.prompt,
        initial_variables=rule_action.initial_variables,
        additional_skills=rule_action.additional_skills,
        terminal_backend="native",
    )
    run_id = await spawn_agent(native_action, db=temp_db, services=services)
    assert run_id
    assert captured["terminal_backend"] == "native"
    row = TerminalManager(temp_db).get(created[-1])
    assert row is not None
    assert row.backend == "native"

    captured.clear()
    enhancement = _spawn_plan_enhancer(
        SimpleNamespace(id=task.id, ref=f"#{task.seq_num}", additional_skills=()),
        stage,
        context,
        round_number=1,
        max_rounds=2,
    )
    enhanced = SpawnAgentAction(
        task_id=enhancement.task_id,
        task_ref=enhancement.task_ref,
        agent_slug=enhancement.agent_slug,
        prompt=enhancement.prompt,
        initial_variables=enhancement.initial_variables,
        additional_skills=enhancement.additional_skills,
        terminal_backend="tmux",
    )
    await spawn_agent(enhanced, db=temp_db, services=services)
    assert captured["terminal_backend"] == "tmux"

    scheduled_captured: dict[str, object] = {}

    async def fake_scheduled(**kwargs: object) -> dict[str, object]:
        scheduled_captured.update(kwargs)
        return {"success": True, "run_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_scheduled,
    )
    storage = MagicMock()
    storage.active_children_for_job.return_value = []
    executor = CronExecutor(
        storage=storage,
        agent_runner=SimpleNamespace(child_session_manager=None),
        services=SimpleNamespace(),
        run_db=None,
    )
    job = CronJob(
        id="job",
        project_id=sample_project["id"],
        name="spawn",
        schedule_type="interval",
        action_type="agent_spawn",
        action_config={"prompt": "cron work", "provider": "claude"},
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
    with patch(
        "gobby.agents.readiness.spawn_readiness_blocker",
        return_value=None,
    ):
        outcome = await executor._execute_agent_spawn(job)
    assert outcome.status == "dispatched"
    assert scheduled_captured["terminal_backend"] in {"tmux", "native"}
    assert scheduled_captured["terminal_backend"] is not None
