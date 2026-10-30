"""Coordinators can finish a descendant's task across project boundaries."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.tasks._factory import create_task_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.memory.manager import MemoryManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import list_task_step_records
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.memory_review_conditions import pending_memory_reviews_complete
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.fixtures.landing_case import grant_orchestrator
from tests.fixtures.review_approvals import grant_review_writes

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

    @property
    def caller_project_id(self) -> str:
        caller = self.sessions.get(self.caller_id)
        assert caller is not None and caller.project_id is not None
        return caller.project_id

    def close(self) -> None:
        self.task = self.tasks.close_task(
            self.task.id, force=True, closed_in_session_id=self.child_id, closed_commit_sha=self.sha
        )

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
    result.task = tasks.get_task(task.id)
    return result


async def approve(world: CrossProjectWorld, caller_id: str, ref: str) -> dict[str, Any]:
    registry = create_task_registry(world.tasks)
    review = {
        "task_id": ref,
        "status": "approved",
        "epoch": 1,
        "round_number": 1,
        "feedback": "LAND: the documentation commit is reviewed.",
        "commit_sha": world.sha,
    }
    with session_context_for_test(caller_id):
        return dict(await registry.call("record_review_step", review))


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["qualified", "uuid", "project_uuid"])
async def test_land_commit_resolves_child_task_in_other_project(
    world: CrossProjectWorld, form: str
) -> None:
    grant_review_writes(world.db, world.caller_id, world.caller_project_id)
    approval = await approve(world, world.caller_id, world.ref(form))
    assert "error" not in approval, approval
    assert approval["record"]["task_id"] == world.task.id
    grant_orchestrator(world.db, world.stranger_id, world.caller_project_id)
    registry = create_task_ops_registry(world.tasks)
    with session_context_for_test(world.stranger_id):
        result = await registry.call(
            "land_commit", {"task_id": world.ref(form), "commit_sha": world.sha}
        )
    assert result["landed"] is True, result
    assert result["landed_by_session_id"] == world.stranger_id
    assert result["landed_tip"] == world.sha
    assert world.git("rev-parse", "HEAD") == world.sha
    assert (world.repo / "docs" / "note.md").read_text() == "Reviewed documentation.\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["qualified", "uuid"])
async def test_review_task_memories_resolves_other_project_task(
    world: CrossProjectWorld, form: str
) -> None:
    variables = SessionVariableManager(world.db)
    world.close()
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
@pytest.mark.parametrize("tool", ["land_commit", "record_review_step", "review_task_memories"])
async def test_cross_project_ref_without_authority_is_refused(
    world: CrossProjectWorld, form: str, tool: str
) -> None:
    if tool == "record_review_step":
        grant_review_writes(world.db, world.stranger_id, world.caller_project_id)
        result = await approve(world, world.stranger_id, world.ref(form))
    elif tool == "land_commit":
        registry = create_task_ops_registry(world.tasks)
        with session_context_for_test(world.stranger_id):
            result = await registry.call(
                tool, {"task_id": world.ref(form), "commit_sha": world.sha}
            )
    else:
        world.close()
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
    assert result["error"] == "task_not_found"
    assert world.git("rev-parse", "HEAD") == world.base
    assert list_task_step_records(world.db, world.task.id) == []


@pytest.mark.asyncio
async def test_cross_project_review_cannot_impersonate_ancestor(world: CrossProjectWorld) -> None:
    world.close()
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
    assert result["error"] == "task_not_found"
    assert (
        SessionVariableManager(world.db)
        .get_variable_subset(world.caller_id, ("_memory_task_review_records",))
        .get("_memory_task_review_records")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assistant", "orchestrator", "custom-coordinator"])
async def test_declared_coordinator_can_approve_other_project(
    world: CrossProjectWorld, role: str
) -> None:
    caller = world.sessions.get(world.stranger_id)
    assert caller is not None
    AgentDefinitionManager(world.db).create(
        role,
        {
            "prompts": {"agent": "Coordinate the fleet."},
            "capabilities": ["coordinate_tasks"],
            "task_step_writes": ["review"],
            "workflows": {"rule_selectors": {"include": []}},
        },
        project_id=caller.project_id,
    )
    SessionVariableManager(world.db).set_variable(caller.id, "_agent_type", role)
    approval = await approve(world, caller.id, world.ref("qualified"))
    assert "error" not in approval, approval
    assert approval["record"]["task_id"] == world.task.id
    assert (approval["record"]["step"], approval["record"]["status"]) == ("review", "approved")
    assert approval["record"]["agent_definition"] == role


@pytest.mark.asyncio
async def test_cross_project_land_still_needs_review_approval(world: CrossProjectWorld) -> None:
    grant_orchestrator(world.db, world.stranger_id, world.caller_project_id)
    registry = create_task_ops_registry(world.tasks)
    with session_context_for_test(world.stranger_id):
        result = await registry.call(
            "land_commit", {"task_id": world.ref("qualified"), "commit_sha": world.sha}
        )
    assert result["blockers"] == ["review_approval_missing"]
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
    assert result["error"] == "task_not_found"
    assert list_task_step_records(world.db, world.task.id) == []


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
