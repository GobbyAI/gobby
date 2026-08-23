"""Tests for the MCP-layer claim-authority guard (#20821)."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry
from gobby.mcp_proxy.tools.tasks._artifacts import create_ops_artifact_registry
from gobby.mcp_proxy.tools.tasks._authorization import (
    has_delegated_agent_run,
    require_claim_authority,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_delete import register_delete_task
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifacts
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

OWNER_SESSION = "11111111-1111-4111-8111-aaaaaaaaaaaa"
CALLER_SESSION = "11111111-1111-4111-8111-bbbbbbbbbbbb"
TASK_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _task(claimed_by: str | None, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": TASK_UUID,
        "project_id": "11111111-1111-4111-8111-111111110001",
        "title": "Guarded Task",
        "priority": 2,
        "task_type": "task",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2024, 1, 1, tzinfo=UTC),
        "claimed_by_session_id": claimed_by,
        "validation_criteria": "Focused tests pass.",
    }
    fields.update(overrides)
    return Task(**fields)


@pytest.fixture
def task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    manager.db.fetchone.return_value = None
    return manager


class TestRequireClaimAuthority:
    def test_unclaimed_allowed_without_session_context(self, task_manager: MagicMock) -> None:
        assert require_claim_authority(task_manager, _task(None), "update_task") is None
        task_manager.db.fetchone.assert_not_called()

    def test_owner_allowed(self, task_manager: MagicMock) -> None:
        with session_context_for_test(OWNER_SESSION):
            denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is None
        task_manager.db.fetchone.assert_not_called()

    def test_foreign_session_refused(self, task_manager: MagicMock) -> None:
        with session_context_for_test(CALLER_SESSION):
            denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is not None
        assert denied["error_code"] == "TASK_CLAIM_CONFLICT"
        assert denied["claimed_by"] == OWNER_SESSION
        assert "gobby-agents:send_message" in denied["message"]
        assert "claim_task" in denied["message"]

    def test_no_session_on_claimed_refused(self, task_manager: MagicMock) -> None:
        denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is not None
        assert denied["error_code"] == "SESSION_REQUIRED"
        assert denied["claimed_by"] == OWNER_SESSION

    def test_delegated_caller_allowed(self, task_manager: MagicMock) -> None:
        task_manager.db.fetchone.return_value = {"id": "run-1"}
        with session_context_for_test(CALLER_SESSION):
            denied = require_claim_authority(task_manager, _task(OWNER_SESSION), "update_task")
        assert denied is None


class TestHasDelegatedAgentRun:
    def test_queries_lineage_in_both_directions(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = {"id": "run-1"}
        assert has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )
        params = db.fetchone.call_args.args[1]
        assert params == (
            TASK_UUID,
            CALLER_SESSION,
            OWNER_SESSION,
            OWNER_SESSION,
            CALLER_SESSION,
        )

    def test_no_owner_short_circuits(self) -> None:
        db = MagicMock()
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=None,
        )
        db.fetchone.assert_not_called()

    def test_missing_row_refused(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = None
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )

    def test_non_string_run_id_refused(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = {"id": 42}
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )

    def test_lookup_failure_fails_closed(self) -> None:
        db = MagicMock()
        db.fetchone.side_effect = RuntimeError("db down")
        assert not has_delegated_agent_run(
            db,
            caller_session_id=CALLER_SESSION,
            task_id=TASK_UUID,
            owner_session_id=OWNER_SESSION,
        )


def _delete_registry(task_manager: MagicMock) -> InternalToolRegistry:
    ctx = RegistryContext(task_manager=task_manager)
    registry = InternalToolRegistry(name="test-delete", description="test")
    register_delete_task(registry, ctx)
    return registry


class TestDeleteTaskGuard:
    def test_cascade_defaults_false_in_schema(self, task_manager: MagicMock) -> None:
        registry = _delete_registry(task_manager)
        schema = registry.get_schema("delete_task")
        assert schema is not None
        assert schema["inputSchema"]["properties"]["cascade"]["default"] is False

    def test_delete_unclaimed_defaults_to_no_cascade(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(None)
        task_manager.delete_task.return_value = True
        tool = _delete_registry(task_manager).get_tool("delete_task")
        assert tool is not None

        result = tool(task_id=TASK_UUID)

        assert result["success"] is True
        task_manager.delete_task.assert_called_once_with(TASK_UUID, cascade=False, unlink=False)

    def test_delete_foreign_claimed_refused(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(OWNER_SESSION)
        tool = _delete_registry(task_manager).get_tool("delete_task")
        assert tool is not None

        with session_context_for_test(CALLER_SESSION):
            result = tool(task_id=TASK_UUID)

        assert result["error_code"] == "TASK_CLAIM_CONFLICT"
        task_manager.delete_task.assert_not_called()


class TestLinkTaskToSessionGuard:
    def test_link_explicit_session_without_context_refused(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(None)
        ctx = RegistryContext(task_manager=task_manager)
        tool = create_session_registry(ctx).get_tool("link_task_to_session")
        assert tool is not None

        result = tool(task_id=TASK_UUID, session_id="#42")

        assert result["error_code"] == "SESSION_REQUIRED"

    def test_link_foreign_claimed_task_refused(self, task_manager: MagicMock) -> None:
        task_manager.get_task.return_value = _task(OWNER_SESSION)
        ctx = RegistryContext(task_manager=task_manager)
        tool = create_session_registry(ctx).get_tool("link_task_to_session")
        assert tool is not None

        with session_context_for_test(CALLER_SESSION):
            result = tool(task_id=TASK_UUID)

        assert result["error_code"] == "TASK_CLAIM_CONFLICT"
        assert result["claimed_by"] == OWNER_SESSION


# ---------------------------------------------------------------------------
# Handler-level authority matrix: every guarded tool under foreign, owner,
# unclaimed, and delegated session contexts.
# ---------------------------------------------------------------------------

_CONTEXT_PATCHES = (
    "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager",
    "gobby.mcp_proxy.tools.tasks._context.SessionManager",
    "gobby.mcp_proxy.tools.tasks._context.SessionVariableManager",
)

BuildResult = tuple[Callable[..., dict[str, Any]], MagicMock]


@dataclass
class ToolSpec:
    """One guarded MCP tool: how to build it, call it, and observe storage."""

    name: str
    build: Callable[[MagicMock, ExitStack], BuildResult]
    kwargs: dict[str, Any]
    task_kwargs: dict[str, Any] = field(default_factory=dict)
    # Session context for the unclaimed case (tools that require a session).
    unclaimed_context: str | None = None


def _prime_storage(manager: MagicMock, task: Task) -> None:
    manager.update_task.return_value = task
    manager.delete_task.return_value = True
    manager.escalate_task.return_value = task
    manager.de_escalate_task.return_value = task
    manager.reopen_task.return_value = task
    manager.add_label.return_value = task
    manager.remove_label.return_value = task
    manager.link_commit.return_value = task
    manager.unlink_commit.return_value = task


def _build_task_registry_tool(
    tool_name: str, mutation_attr: str, extra_patches: tuple[str, ...] = ()
) -> Callable[[MagicMock, ExitStack], BuildResult]:
    def build(manager: MagicMock, stack: ExitStack) -> BuildResult:
        for target in (*extra_patches, *_CONTEXT_PATCHES):
            stack.enter_context(patch(target))
        registry = create_task_registry(manager)
        tool = registry.get_tool(tool_name)
        assert tool is not None
        return tool, getattr(manager, mutation_attr)

    return build


def _build_link_task_to_session(manager: MagicMock, stack: ExitStack) -> BuildResult:
    stm_cls = stack.enter_context(patch(_CONTEXT_PATCHES[0]))
    sm_cls = stack.enter_context(patch(_CONTEXT_PATCHES[1]))
    stack.enter_context(patch(_CONTEXT_PATCHES[2]))
    sm_cls.return_value.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
    registry = create_task_registry(manager)
    tool = registry.get_tool("link_task_to_session")
    assert tool is not None
    return tool, stm_cls.return_value.link_task


def _build_commit_tool(tool_name: str) -> Callable[[MagicMock, ExitStack], BuildResult]:
    def build(manager: MagicMock, stack: ExitStack) -> BuildResult:
        from gobby.mcp_proxy.tools.task_commits import create_commit_registry

        registry = create_commit_registry(task_manager=manager)
        tool = registry.get_tool(tool_name)
        assert tool is not None
        return tool, getattr(manager, tool_name)

    return build


def _build_affected_files_tool(manager: MagicMock, stack: ExitStack) -> BuildResult:
    af_cls = stack.enter_context(
        patch("gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager")
    )
    af_cls.return_value.set_files.return_value = []
    registry = create_ops_affected_files_registry(RegistryContext(task_manager=manager))
    tool = registry.get_tool("set_affected_files")
    assert tool is not None
    return tool, af_cls.return_value.set_files


def _build_artifact_tool(
    tool_name: str, mutation_method: str
) -> Callable[[MagicMock, ExitStack], BuildResult]:
    def build(manager: MagicMock, stack: ExitStack) -> BuildResult:
        am_cls = stack.enter_context(
            patch("gobby.mcp_proxy.tools.tasks._artifacts.TaskArtifactManager")
        )
        instance = am_cls.return_value
        artifacts = TaskArtifacts(task_id=TASK_UUID)
        instance.set_artifact.return_value = artifacts
        instance.set_artifacts_atomic.return_value = artifacts
        instance.clear_isolation_pair.return_value = artifacts
        registry = create_ops_artifact_registry(RegistryContext(task_manager=manager))
        tool = registry.get_tool(tool_name)
        assert tool is not None
        return tool, getattr(instance, mutation_method)

    return build


def _build_append_description_section(manager: MagicMock, stack: ExitStack) -> BuildResult:
    conn = manager.db.transaction_immediate.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = {"description": ""}
    registry = create_ops_artifact_registry(RegistryContext(task_manager=manager))
    tool = registry.get_tool("append_description_section")
    assert tool is not None
    return tool, manager.db.transaction_immediate


TOOL_SPECS = [
    ToolSpec(
        name="update_task",
        build=_build_task_registry_tool("update_task", "update_task"),
        kwargs={"task_id": TASK_UUID, "title": "Retitled"},
    ),
    ToolSpec(
        name="delete_task",
        build=_build_task_registry_tool("delete_task", "delete_task"),
        kwargs={"task_id": TASK_UUID},
    ),
    ToolSpec(
        name="escalate_task",
        build=_build_task_registry_tool(
            "escalate_task",
            "escalate_task",
            ("gobby.mcp_proxy.tools.tasks._lifecycle_status.coordinate_task_escalation",),
        ),
        kwargs={"task_id": TASK_UUID, "reason": "blocked"},
        unclaimed_context=CALLER_SESSION,
    ),
    ToolSpec(
        name="de_escalate_task",
        build=_build_task_registry_tool(
            "de_escalate_task",
            "de_escalate_task",
            ("gobby.mcp_proxy.tools.tasks._lifecycle_status.notify_parent_on_task_state_change",),
        ),
        kwargs={"task_id": TASK_UUID, "reason": "resolved"},
        task_kwargs={"is_escalated": True},
        unclaimed_context=CALLER_SESSION,
    ),
    ToolSpec(
        name="reopen_task",
        build=_build_task_registry_tool(
            "reopen_task",
            "reopen_task",
            ("gobby.mcp_proxy.tools.tasks._lifecycle_status.clear_prior_claim_session_variables",),
        ),
        kwargs={"task_id": TASK_UUID},
        unclaimed_context=CALLER_SESSION,
    ),
    ToolSpec(
        name="add_label",
        build=_build_task_registry_tool("add_label", "add_label"),
        kwargs={"task_id": TASK_UUID, "label": "needs-decision"},
    ),
    ToolSpec(
        name="remove_label",
        build=_build_task_registry_tool("remove_label", "remove_label"),
        kwargs={"task_id": TASK_UUID, "label": "needs-decision"},
    ),
    ToolSpec(
        name="link_commit",
        build=_build_commit_tool("link_commit"),
        kwargs={"task_id": TASK_UUID, "commit_sha": "abc123"},
    ),
    ToolSpec(
        name="unlink_commit",
        build=_build_commit_tool("unlink_commit"),
        kwargs={"task_id": TASK_UUID, "commit_sha": "abc123"},
    ),
    ToolSpec(
        name="set_affected_files",
        build=_build_affected_files_tool,
        kwargs={"task_id": TASK_UUID, "files": ["a.py"], "source": "manual"},
    ),
    ToolSpec(
        name="set_artifact",
        build=_build_artifact_tool("set_artifact", "set_artifact"),
        kwargs={"task_id": TASK_UUID, "field": "base_commit_sha", "value": "abc"},
    ),
    ToolSpec(
        name="set_artifacts_atomic",
        build=_build_artifact_tool("set_artifacts_atomic", "set_artifacts_atomic"),
        kwargs={"task_id": TASK_UUID, "fields": {"base_commit_sha": "abc"}},
    ),
    ToolSpec(
        name="clear_isolation_pair",
        build=_build_artifact_tool("clear_isolation_pair", "clear_isolation_pair"),
        kwargs={"task_id": TASK_UUID, "family": "worktree"},
    ),
    ToolSpec(
        name="append_description_section",
        build=_build_append_description_section,
        kwargs={"task_id": TASK_UUID, "heading": "Audit", "body": "text"},
    ),
    ToolSpec(
        name="link_task_to_session",
        build=_build_link_task_to_session,
        kwargs={"task_id": TASK_UUID},
        unclaimed_context=CALLER_SESSION,
    ),
]

_SPEC_IDS = [spec.name for spec in TOOL_SPECS]


def _invoke(
    spec: ToolSpec,
    *,
    claimed_by: str | None,
    context_session: str | None,
    delegated: bool = False,
) -> tuple[dict[str, Any], MagicMock]:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    manager.db.fetchone.return_value = {"id": "run-1"} if delegated else None
    task = _task(claimed_by, **spec.task_kwargs)
    manager.get_task.return_value = task
    _prime_storage(manager, task)

    with ExitStack() as stack:
        tool, mutation = spec.build(manager, stack)
        if context_session is not None:
            with session_context_for_test(context_session):
                result = tool(**spec.kwargs)
        else:
            result = tool(**spec.kwargs)
    return result, mutation


class TestGuardedToolMatrix:
    @pytest.mark.parametrize("spec", TOOL_SPECS, ids=_SPEC_IDS)
    def test_foreign_session_refused(self, spec: ToolSpec) -> None:
        result, mutation = _invoke(spec, claimed_by=OWNER_SESSION, context_session=CALLER_SESSION)
        assert result["error_code"] == "TASK_CLAIM_CONFLICT"
        assert result["claimed_by"] == OWNER_SESSION
        mutation.assert_not_called()

    @pytest.mark.parametrize("spec", TOOL_SPECS, ids=_SPEC_IDS)
    def test_owner_allowed(self, spec: ToolSpec) -> None:
        result, mutation = _invoke(spec, claimed_by=OWNER_SESSION, context_session=OWNER_SESSION)
        assert result.get("error_code") is None
        assert mutation.called

    @pytest.mark.parametrize("spec", TOOL_SPECS, ids=_SPEC_IDS)
    def test_unclaimed_allowed(self, spec: ToolSpec) -> None:
        result, mutation = _invoke(spec, claimed_by=None, context_session=spec.unclaimed_context)
        assert result.get("error_code") is None
        assert mutation.called

    @pytest.mark.parametrize("spec", TOOL_SPECS, ids=_SPEC_IDS)
    def test_delegated_allowed(self, spec: ToolSpec) -> None:
        result, mutation = _invoke(
            spec,
            claimed_by=OWNER_SESSION,
            context_session=CALLER_SESSION,
            delegated=True,
        )
        assert result.get("error_code") is None
        assert mutation.called
