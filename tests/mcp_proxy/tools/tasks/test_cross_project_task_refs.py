"""Coordinators can finish a descendant's task across project boundaries."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.config.persistence import MemoryConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.mcp_proxy.tools.tasks._factory import create_task_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.memory.manager import MemoryManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_receipts import INDEPENDENT_REVIEW_APPROVAL, list_close_receipts
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.memory_review_conditions import pending_memory_reviews_complete
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


@dataclass
class CrossProjectWorld:
    db: HubDatabase
    tasks: LocalTaskManager
    sessions: SessionManager
    caller_id: str
    child_id: str
    stranger_id: str
    task: Task
    repo: Path
    base: str
    sha: str

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def ref(self, form: str) -> str:
        if form == "qualified":
            return f"child-project#{self.task.seq_num}"
        if form == "project_uuid":
            return f"{self.task.project_id}#{self.task.seq_num}"
        return self.task.id


@pytest.fixture
def world(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CrossProjectWorld:
    parent = install_isolated_checkout_project(
        temp_db, tmp_path / "parent", name="parent-project", monkeypatch=monkeypatch
    )
    child = install_isolated_checkout_project(
        temp_db, tmp_path / "child", name="child-project", machine_id=parent.machine_id
    )
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        external_id="coordinator",
        source="codex",
        machine_id=parent.machine_id,
        project_id=parent.project.id,
        workspace_path=parent.root_path,
    )
    worker = sessions.register(
        external_id="child",
        source="codex",
        machine_id=parent.machine_id,
        project_id=child.project.id,
        parent_session_id=caller.id,
    )
    stranger = sessions.register(
        external_id="stranger",
        source="codex",
        machine_id=parent.machine_id,
        project_id=parent.project.id,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        child.project.id,
        "Document the child project",
        validation_criteria="The reviewed documentation commit lands in the child repository.",
        created_in_session_id=caller.id,
        claimed_by_session_id=worker.id,
    )
    result = CrossProjectWorld(
        temp_db,
        tasks,
        sessions,
        caller.id,
        worker.id,
        stranger.id,
        task,
        Path(child.root_path),
        "",
        "",
    )
    result.git("init", "--initial-branch=main")
    result.git("config", "user.name", "Cross project test")
    result.git("config", "user.email", "cross-project@example.invalid")
    result.git("config", "core.hooksPath", str(tmp_path / "empty-hooks"))
    (result.repo / "README.md").write_text("base\n")
    result.git("add", "README.md")
    result.git("commit", "-m", "Initial")
    result.base = result.git("rev-parse", "HEAD")
    result.git("checkout", "-b", "candidate")
    (result.repo / "docs").mkdir()
    (result.repo / "docs" / "note.md").write_text("Reviewed documentation.\n")
    result.git("add", "docs/note.md")
    result.git("commit", "-m", "Document child project")
    result.sha = result.git("rev-parse", "HEAD")
    result.git("checkout", "main")
    tasks.link_commit(task.id, result.sha)
    result.task = tasks.close_task(
        task.id, force=True, closed_in_session_id=worker.id, closed_commit_sha=result.sha
    )
    return result


async def approve(world: CrossProjectWorld, caller_id: str, ref: str) -> dict[str, Any]:
    registry = create_commit_registry(
        world.tasks, project_manager=LocalProjectManager(world.db), session_manager=world.sessions
    )
    with session_context_for_test(caller_id):
        return dict(
            await registry.call(
                "record_close_receipt",
                {"task_id": ref, "kind": INDEPENDENT_REVIEW_APPROVAL, "commit_sha": world.sha},
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["qualified", "uuid", "project_uuid"])
async def test_land_commit_resolves_child_task_in_other_project(
    world: CrossProjectWorld, form: str
) -> None:
    receipt = await approve(world, world.caller_id, world.ref(form))
    assert "error" not in receipt, receipt
    assert receipt["task_id"] == world.task.id
    registry = create_task_ops_registry(world.tasks)
    with session_context_for_test(world.caller_id):
        result = await registry.call(
            "land_commit", {"task_id": world.ref(form), "commit_sha": world.sha}
        )
    assert result["landed"] is True
    assert result["landed_tip"] == world.sha
    assert world.git("rev-parse", "HEAD") == world.sha
    assert (world.repo / "docs" / "note.md").read_text() == "Reviewed documentation.\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["qualified", "uuid"])
async def test_review_task_memories_resolves_other_project_task(
    world: CrossProjectWorld, form: str
) -> None:
    variables = SessionVariableManager(world.db)
    assert world.task.closed_at is not None
    closure_id = f"{world.task.id}:{world.task.closed_at.isoformat()}"
    variables.set_variable(
        world.caller_id,
        "_memory_pending_task_reviews",
        [{"closure_id": closure_id, "task_id": world.task.id, "task_ref": world.ref(form)}],
    )
    memories = MemoryManager(world.db, MemoryConfig())
    registry = create_memory_registry(
        lambda: memories, task_manager=world.tasks, session_manager=world.sessions
    )
    with (
        patch.object(memories, "search_memories", AsyncMock(return_value=[])) as search,
        session_context_for_test(world.caller_id),
    ):
        result = await registry.call(
            "review_task_memories",
            {
                "task_id": world.ref(form),
                "changes_summary": "Documented child project.",
                "session_id": world.caller_id,
            },
        )
    assert result["success"] is True
    assert result["task_id"] == world.task.id
    assert result["pending_reviews_complete"] is True
    assert search.await_args is not None
    assert search.await_args.kwargs["project_id"] == world.task.project_id
    stored = variables.get_variable_subset(
        world.caller_id,
        (
            "_memory_pending_task_reviews",
            "_memory_task_review_records",
            "_memory_review_stop_delivered",
        ),
    )
    assert pending_memory_reviews_complete(stored) is True
    assert stored["_memory_review_stop_delivered"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["qualified", "uuid"])
@pytest.mark.parametrize("tool", ["land_commit", "record_close_receipt", "review_task_memories"])
async def test_cross_project_ref_without_authority_is_refused(
    world: CrossProjectWorld, form: str, tool: str
) -> None:
    if tool == "record_close_receipt":
        result = await approve(world, world.stranger_id, world.ref(form))
    elif tool == "land_commit":
        registry = create_task_ops_registry(world.tasks)
        with session_context_for_test(world.stranger_id):
            result = await registry.call(
                tool, {"task_id": world.ref(form), "commit_sha": world.sha}
            )
    else:
        memories = MemoryManager(world.db, MemoryConfig())
        registry = create_memory_registry(
            lambda: memories, task_manager=world.tasks, session_manager=world.sessions
        )
        with session_context_for_test(world.stranger_id):
            result = await registry.call(
                tool,
                {
                    "task_id": world.ref(form),
                    "changes_summary": "Unrelated caller.",
                    "session_id": world.stranger_id,
                },
            )
    assert result["error"] == "cross_project_task_forbidden"
    assert world.git("rev-parse", "HEAD") == world.base
    assert list_close_receipts(world.db, world.task.id) == []


@pytest.mark.asyncio
async def test_cross_project_review_cannot_impersonate_ancestor(world: CrossProjectWorld) -> None:
    memories = MemoryManager(world.db, MemoryConfig())
    registry = create_memory_registry(
        lambda: memories, task_manager=world.tasks, session_manager=world.sessions
    )
    with session_context_for_test(world.stranger_id):
        result = await registry.call(
            "review_task_memories",
            {
                "task_id": world.task.id,
                "changes_summary": "Impersonate parent.",
                "session_id": world.caller_id,
            },
        )
    assert result["error"] == "cross_project_task_forbidden"
    assert (
        SessionVariableManager(world.db)
        .get_variable_subset(world.caller_id, ("_memory_task_review_records",))
        .get("_memory_task_review_records")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assistant", "orchestrator"])
async def test_installed_operator_role_can_approve_other_project(
    world: CrossProjectWorld, role: str
) -> None:
    caller = world.sessions.get(world.stranger_id)
    assert caller is not None
    AgentDefinitionManager(world.db).create(role, {}, project_id=caller.project_id)
    SessionVariableManager(world.db).set_variable(caller.id, "_agent_type", role)
    receipt = await approve(world, caller.id, world.ref("qualified"))
    assert "error" not in receipt, receipt
    assert receipt["task_id"] == world.task.id
    assert receipt["kind"] == INDEPENDENT_REVIEW_APPROVAL


@pytest.mark.asyncio
async def test_cross_project_land_still_needs_review_approval(world: CrossProjectWorld) -> None:
    registry = create_task_ops_registry(world.tasks)
    with session_context_for_test(world.caller_id):
        result = await registry.call(
            "land_commit", {"task_id": world.ref("qualified"), "commit_sha": world.sha}
        )
    assert result["error"] == "review_receipt_missing"
    assert world.git("rev-parse", "HEAD") == world.base


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_role_name_alone_does_not_grant_cross_project_access(
    world: CrossProjectWorld, installed: bool
) -> None:
    caller = world.sessions.get(world.stranger_id)
    assert caller is not None
    if installed:
        AgentDefinitionManager(world.db).create("assistant", {}, project_id=caller.project_id)
        # Installed operator definitions do not authorize a spawned seat.
        world.sessions.register(
            external_id=caller.external_id,
            source=caller.source,
            machine_id=caller.machine_id,
            project_id=caller.project_id,
            parent_session_id=world.caller_id,
        )
    SessionVariableManager(world.db).set_variable(caller.id, "_agent_type", "assistant")
    result = await approve(world, caller.id, world.ref("qualified"))
    assert result["error"] == "cross_project_task_forbidden"
    assert list_close_receipts(world.db, world.task.id) == []


@pytest.mark.asyncio
async def test_create_task_in_other_project_skips_caller_overlay(world: CrossProjectWorld) -> None:
    registry = create_task_registry(world.tasks)
    with session_context_for_test(world.caller_id):
        result = await registry.call(
            "create_task",
            {
                "title": "Follow up documentation",
                "category": "docs",
                "validation_criteria": "Document the child project.",
                "project": "child-project",
                "affected_files": ["README.md"],
                "claim": True,
            },
        )
    assert "id" in result, result
    created = world.tasks.get_task(result["id"])
    assert created.project_id == world.task.project_id
    assert created.claimed_by_session_id is None
    assert not result.get("targets_not_found")
    assert "cannot claim a task in a different project" in result["warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_has_lane", [False, True])
async def test_lane_rule_uses_target_project_lanes(
    world: CrossProjectWorld, target_has_lane: bool
) -> None:
    caller = world.sessions.get(world.caller_id)
    assert caller is not None
    world.tasks.create_task(caller.project_id, "Caller lane", task_type="epic", labels=["lane"])
    if target_has_lane:
        world.tasks.create_task(
            world.task.project_id, "Target lane", task_type="epic", labels=["lane"]
        )
    rule_file = Path(__file__).parents[4] / (
        "src/gobby/install/shared/workflows/rules/task-enforcement/refuse-task-outside-lane.yaml"
    )
    document = yaml.safe_load(rule_file.read_text())
    for name, rule_data in document["rules"].items():
        body, metadata = split_rule_definition_data(rule_data)
        RuleDefinitionManager(world.db).create(
            name=name,
            definition_json=RuleDefinitionBody.model_validate(body).model_dump_json(),
            priority=metadata["priority"],
            enabled=metadata["enabled"],
            tags=document["tags"],
        )
    arguments = {
        "title": "Outer project destination",
        "category": "docs",
        "validation_criteria": "Create work in the target project.",
    }
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=world.caller_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        project_id=caller.project_id,
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "project_id": world.task.project_id,
                "arguments": arguments,
            },
        },
    )
    response = await RuleEngine(world.db, task_manager=world.tasks).evaluate(
        event, session_id=world.caller_id, variables={}
    )
    if target_has_lane:
        assert response.decision == "block"
        assert "Target lane" in (response.reason or "")
        assert "Caller lane" not in (response.reason or "")
        return
    assert response.decision != "block", response.reason
    internal = InternalRegistryManager()
    internal.add_registry(create_task_registry(world.tasks))
    manager = MagicMock()
    manager.session_manager = world.sessions
    manager.project_id = caller.project_id
    service = ToolProxyService(manager, internal_manager=internal)
    with session_context_for_test(world.caller_id):
        result = await service.call_tool(
            "gobby-tasks",
            "create_task",
            arguments,
            session_id=world.caller_id,
            project_id=world.task.project_id,
        )
    assert "id" in result, result
    created = world.tasks.get_task(result["id"])
    assert created.project_id == world.task.project_id
    assert created.created_in_session_id == world.caller_id
