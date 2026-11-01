"""Seat close authority and Consolidator fold protections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._crud import create_crud_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import write_project_marker

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class Closing:
    db: HubDatabase
    project_id: str
    manager: LocalTaskManager
    context: RegistryContext
    task: Task


def _seat(
    db: HubDatabase,
    project_id: str,
    agent_type: str,
    *,
    capabilities: tuple[str, ...] = (),
    definition_fields: dict[str, Any] | None = None,
) -> str:
    session = SessionManager(db).register(
        external_id=f"close-authority-{agent_type}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project_id,
    )
    definitions = AgentDefinitionManager(db)
    if definitions.get_by_name(agent_type, project_id) is None:
        definitions.create(
            agent_type,
            {
                "name": agent_type,
                "prompts": {"agent": "Apply the declared task close policy."},
                "workflows": {"rule_selectors": {"include": []}},
                "capabilities": list(capabilities),
                **(definition_fields or {}),
            },
            project_id=project_id,
        )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", agent_type)
    return session.id


def _protected_seat(closing: Closing, name: str) -> str:
    return _seat(
        closing.db,
        closing.project_id,
        name,
        capabilities=("close_without_work",),
        definition_fields={
            "respect_task_protections": True,
        },
    )


def _protection_policy(closing: Closing, policy: dict[str, object]) -> None:
    checkout = LocalProjectCheckoutManager(closing.db).get(require_machine_id(), closing.project_id)
    assert checkout is not None
    marker = Path(checkout.root_path) / ".gobby" / "project.json"
    data = json.loads(marker.read_text())
    marker.write_text(json.dumps({**data, "task_protection_policy": policy}))


@pytest.mark.parametrize("name", ["consolidator", "fixture-backlog"])
@pytest.mark.parametrize("operation", ["update_task", "close_task"])
@pytest.mark.asyncio
async def test_task_protection_is_declared_in_definition_and_project(
    closing: Closing, name: str, operation: str
) -> None:
    closing.manager.release_task_claim(closing.task.id)
    closing.manager.update_task(closing.task.id, labels=["fixture-held"])
    _protection_policy(closing, {"labels": ["fixture-held"]})
    seat = _protected_seat(closing, name)
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(task_id=closing.task.id, title="Should not change")
            assert result.get("error"), result
        else:
            evaluation = await _evaluate(closing, seat, "moot")
            assert not evaluation.ready
    assert closing.manager.get_task(closing.task.id).title == closing.task.title


@pytest.mark.asyncio
async def test_consolidator_name_does_not_impose_undeclared_protection(closing: Closing) -> None:
    closing.manager.release_task_claim(closing.task.id)
    closing.manager.update_task(closing.task.id, labels=["deferred-by-josh"])
    seat = _seat(
        closing.db, closing.project_id, "consolidator", capabilities=("close_without_work",)
    )
    evaluation = await _evaluate(closing, seat, "moot")
    assert evaluation.ready


@pytest.mark.parametrize("root_identity", ["number", "uuid", "uppercase_uuid"])
@pytest.mark.asyncio
async def test_project_protection_selects_custom_subtree_roots(
    closing: Closing, root_identity: str
) -> None:
    closing.manager.release_task_claim(closing.task.id)
    root = closing.manager.create_task(
        closing.project_id,
        "Custom protected root",
        task_type="epic",
        category="code",
        validation_criteria="Protect descendants.",
    )
    closing.manager.update_task(closing.task.id, parent_task_id=root.id)
    reference = f"#{root.seq_num}" if root_identity == "number" else root.id
    if root_identity == "uppercase_uuid":
        reference = reference.upper()
    _protection_policy(closing, {"subtree_roots": [reference]})
    seat = _protected_seat(closing, "fixture-root-policy")
    evaluation = await _evaluate(closing, seat, "moot")
    assert evaluation.error == "task_protected"
    assert evaluation.message is not None
    assert "ancestor task" in evaluation.message
    _protection_policy(closing, {"subtree_roots": []})
    unprotected = await _evaluate(closing, seat, "moot")
    assert unprotected.ready


@pytest.mark.parametrize("foreign_claims", [True, False])
def test_project_controls_foreign_claim_protection(closing: Closing, foreign_claims: bool) -> None:
    _protection_policy(closing, {"foreign_claims": foreign_claims})
    seat = _protected_seat(closing, "fixture-claim-policy")
    update = create_crud_registry(closing.context).get_tool("update_task")
    assert update is not None
    with session_context_for_test(seat):
        result = update(task_id=closing.task.id, title="Policy-approved edit")
    assert bool(result.get("error")) is foreign_claims
    expected_title = closing.task.title if foreign_claims else "Policy-approved edit"
    assert closing.manager.get_task(closing.task.id).title == expected_title


@pytest.mark.parametrize(
    "policy",
    [{"labels": "held"}, {"subtree_roots": ["invalid"]}, {"foreign_claims": "false"}],
)
@pytest.mark.parametrize("operation", ["update_task", "close_task"])
@pytest.mark.asyncio
async def test_invalid_project_protection_refuses_mutation(
    closing: Closing, policy: dict[str, object], operation: str
) -> None:
    closing.manager.release_task_claim(closing.task.id)
    _protection_policy(closing, policy)
    seat = _protected_seat(closing, "fixture-invalid-policy")
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(task_id=closing.task.id, title="Should not change")
            assert "policy unavailable" in result["error"]
        else:
            evaluation = await _evaluate(closing, seat, "moot")
            assert evaluation.error == "task_protection_policy_unavailable"
    assert closing.manager.get_task(closing.task.id).title == closing.task.title


def test_disabled_definition_does_not_bypass_mutation_policy(closing: Closing) -> None:
    seat = _protected_seat(closing, "fixture-disabled-policy")
    definitions = AgentDefinitionManager(closing.db)
    definition = definitions.get_by_name("fixture-disabled-policy", closing.project_id)
    assert definition is not None
    definitions.update(definition.id, enabled=False)
    update = create_crud_registry(closing.context).get_tool("update_task")
    assert update is not None
    with session_context_for_test(seat):
        result = update(task_id=closing.task.id, title="Should not change")
    assert "enabled, valid active definition" in result["error"]
    assert closing.manager.get_task(closing.task.id).title == closing.task.title


@pytest.mark.parametrize("operation", ["update_task", "close_task"])
@pytest.mark.asyncio
async def test_ancestor_uses_its_own_project_protection_policy(
    closing: Closing, tmp_path: Path, operation: str
) -> None:
    other = LocalProjectManager(closing.db).create("ancestor-project")
    root = tmp_path / "ancestor-project"
    write_project_marker(root, project_id=other.id, name=other.name)
    LocalProjectCheckoutManager(closing.db).register(require_machine_id(), other.id, str(root))
    ancestor = closing.manager.create_task(
        other.id,
        "Protected foreign ancestor",
        task_type="epic",
        category="code",
        labels=["other-project-held"],
        validation_criteria="Respect ancestor project policy.",
    )
    other_closing = Closing(closing.db, other.id, closing.manager, closing.context, ancestor)
    _protection_policy(other_closing, {"labels": ["other-project-held"]})
    _protection_policy(closing, {"labels": []})
    closing.manager.release_task_claim(closing.task.id)
    closing.manager.update_task(closing.task.id, parent_task_id=ancestor.id)
    seat = _protected_seat(closing, "fixture-cross-project")
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(task_id=closing.task.id, title="Should not change")
            assert "other-project-held" in result.get("error", "")
        else:
            evaluation = await _evaluate(closing, seat, "moot")
            assert evaluation.error == "task_protected"
    assert closing.manager.get_task(ancestor.id).closed_at is None
    assert closing.manager.get_task(closing.task.id).title == closing.task.title


@pytest.fixture
def closing(temp_db: HubDatabase, sample_git_project: dict[str, Any]) -> Closing:
    project_id = sample_git_project["id"]
    LocalProjectCheckoutManager(temp_db).register(
        require_machine_id(), project_id, str(sample_git_project["repo_path"])
    )
    manager = LocalTaskManager(temp_db)
    developer = _seat(temp_db, project_id, "developer")
    created = manager.create_task(
        project_id,
        "No-work candidate",
        category="code",
        task_type="task",
        implementation_domain="backend",
        validation_criteria="Close authority.",
    )
    task = manager.claim_task(created.id, developer)
    result = Closing(temp_db, project_id, manager, RegistryContext(task_manager=manager), task)
    _protection_policy(
        result,
        {"labels": ["deferred-by-josh"], "subtree_roots": ["#22978", "#22225"]},
    )
    return result


async def _evaluate(closing: Closing, seat: str, reason: str) -> CloseEvaluation:
    with session_context_for_test(seat):
        return await lifecycle._evaluate_close(
            closing.context,
            task_id=closing.task.id,
            reason=reason,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            superseded_by="#25223",
        )


@pytest.mark.parametrize("protection", ("foreign_claim", "deferred-by-josh", "22978", "22225"))
@pytest.mark.parametrize("operation", ("update_task", "close_task"))
@pytest.mark.parametrize("policy_source", ("fixture", "repository"))
@pytest.mark.asyncio
async def test_consolidator_cannot_mutate_protected_tasks(
    closing: Closing, protection: str, operation: str, policy_source: str
) -> None:
    if policy_source == "repository":
        marker = Path(__file__).resolve().parents[4] / ".gobby" / "project.json"
        policy = json.loads(marker.read_text()).get("task_protection_policy", {})
        _protection_policy(closing, policy)
    if protection != "foreign_claim":
        closing.manager.release_task_claim(closing.task.id)
    if protection == "deferred-by-josh":
        closing.manager.update_task(closing.task.id, labels=["deferred-by-josh"])
    elif protection in ("22978", "22225"):
        root = closing.manager.create_task(
            closing.project_id,
            "Protected root",
            task_type="epic",
            category="code",
            validation_criteria="Protected operator subtree.",
        )
        # Set the operator root identity only in this isolated test database.
        with closing.db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET seq_num = %s, path_cache = %s WHERE id = %s",
                (int(protection), protection, root.id),
            )
        parent = closing.manager.create_task(
            closing.project_id,
            "Intermediate epic",
            task_type="epic",
            category="code",
            parent_task_id=root.id,
            validation_criteria="Nested protection.",
        )
        closing.manager.update_task(closing.task.id, parent_task_id=parent.id)
    before = closing.manager.get_task(closing.task.id)
    seat = _protected_seat(closing, "fixture-backlog")
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(
                task_id=closing.task.id, validation_criteria="Unauthorized replacement."
            )
            assert result.get("error"), result
        else:
            evaluation = await _evaluate(closing, seat, "moot")
            assert evaluation.error is not None
            assert not evaluation.ready
    after = closing.manager.get_task(closing.task.id)
    assert after.validation_criteria == before.validation_criteria
    assert after.closed_at is None
    assert after.claimed_by_session_id == before.claimed_by_session_id


def test_consolidator_updates_unprotected_survivor(closing: Closing) -> None:
    closing.manager.release_task_claim(closing.task.id)
    seat = _protected_seat(closing, "fixture-backlog")
    update = create_crud_registry(closing.context).get_tool("update_task")
    assert update is not None
    with session_context_for_test(seat):
        result = update(
            task_id=closing.task.id, validation_criteria="Original and folded criteria."
        )
    assert not result.get("error"), result
    assert closing.manager.get_task(closing.task.id).validation_criteria == (
        "Original and folded criteria."
    )


def _protected_epic(closing: Closing, protection: str, developer: str) -> str:
    """An epic guarded by a deferred-by-josh label or a claim by the developer."""
    epic = closing.manager.create_task(
        closing.project_id,
        "Protected epic",
        task_type="epic",
        category="code",
        validation_criteria="Protected ancestor.",
    )
    if protection == "deferred-by-josh":
        closing.manager.update_task(epic.id, labels=["deferred-by-josh"])
    else:
        closing.manager.claim_task(epic.id, developer)
    return epic.id


@pytest.mark.parametrize("protection", ("deferred-by-josh", "foreign_claim"))
@pytest.mark.parametrize("operation", ("update_task", "close_task"))
@pytest.mark.asyncio
async def test_consolidator_cannot_mutate_under_protected_ancestor(
    closing: Closing, protection: str, operation: str
) -> None:
    developer = closing.task.claimed_by_session_id
    assert developer is not None
    closing.manager.release_task_claim(closing.task.id)
    epic_id = _protected_epic(closing, protection, developer)
    closing.manager.update_task(closing.task.id, parent_task_id=epic_id)
    before = closing.manager.get_task(closing.task.id)
    seat = _protected_seat(closing, "fixture-backlog")
    with session_context_for_test(seat):
        if operation == "update_task":
            update = create_crud_registry(closing.context).get_tool("update_task")
            assert update is not None
            result = update(
                task_id=closing.task.id, validation_criteria="Unauthorized replacement."
            )
            assert result.get("error"), result
        else:
            close = create_lifecycle_registry(closing.context).get_tool("close_task")
            assert close is not None
            result = await close(
                task_id=closing.task.id,
                reason="moot",
                changes_summary="Orchestrator directed approved proposal 3.",
            )
            assert not result.get("success"), result
    after = closing.manager.get_task(closing.task.id)
    assert after.validation_criteria == before.validation_criteria
    assert after.closed_at is None
    assert closing.manager.get_task(epic_id).closed_at is None


@pytest.mark.parametrize("protection", ("deferred-by-josh", "foreign_claim"))
@pytest.mark.parametrize("direction", ("into", "out_of"))
@pytest.mark.asyncio
async def test_consolidator_cannot_reparent_across_protected_parent(
    closing: Closing, protection: str, direction: str
) -> None:
    developer = closing.task.claimed_by_session_id
    assert developer is not None
    closing.manager.release_task_claim(closing.task.id)
    protected_id = _protected_epic(closing, protection, developer)
    if direction == "into":
        destination_id = protected_id
    else:
        closing.manager.update_task(closing.task.id, parent_task_id=protected_id)
        destination_id = closing.manager.create_task(
            closing.project_id,
            "Free epic",
            task_type="epic",
            category="code",
            validation_criteria="Unprotected parent.",
        ).id
    before = closing.manager.get_task(closing.task.id)
    seat = _protected_seat(closing, "fixture-backlog")
    update = create_crud_registry(closing.context).get_tool("update_task")
    assert update is not None
    with session_context_for_test(seat):
        result = update(task_id=closing.task.id, parent_task_id=destination_id)
    assert result.get("error"), result
    assert closing.manager.get_task(closing.task.id).parent_task_id == before.parent_task_id
