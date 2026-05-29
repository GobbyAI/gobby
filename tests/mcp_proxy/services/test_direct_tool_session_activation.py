"""Regression coverage for direct MCP tool enforcement session activation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, SessionSource
from gobby.hooks.session_activation import (
    MARKER_COMPLETED,
    MARKER_HASH,
    MARKER_VERSION,
    SESSION_ACTIVATION_CONTRACT_HASH,
    SESSION_ACTIVATION_CONTRACT_VERSION,
)
from gobby.mcp_proxy.services.result_handling import (
    apply_before_tool_enforcement,
    build_before_tool_event,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit


def _load_interactive_review_block_rule(db: HubDatabase) -> None:
    yaml_path = (
        get_bundled_rules_path() / "task-enforcement" / "block-needs-review-interactive.yaml"
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    rule_data = data["rules"]["block-needs-review-interactive"]
    body = RuleDefinitionBody.model_validate(rule_data)
    LocalWorkflowDefinitionManager(db).create(
        name="block-needs-review-interactive",
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=rule_data.get("priority", 32),
        enabled=rule_data.get("enabled", True),
    )


class _DirectToolService:
    def __init__(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        project_id: str,
        project_path: Path,
    ) -> None:
        workflow_handler = WorkflowHookHandler(rule_engine=RuleEngine(db), enabled=True)
        event_handlers = EventHandlers(session_manager=session_manager)  # type: ignore[arg-type]
        self._hook_manager = SimpleNamespace(
            _workflow_handler=workflow_handler,
            _session_manager=session_manager,
            _event_handlers=event_handlers,
            _database=db,
        )
        self._session_manager = session_manager
        self._project_id = project_id
        self._project_path = project_path

    def _get_effective_session_id(self, session_id: str | None) -> str | None:
        return session_id

    def _resolve_hook_manager(self) -> Any:
        return self._hook_manager

    def _resolve_tool_event_context(
        self, effective_session_id: str
    ) -> tuple[Any, SessionManager, Any, SessionSource, dict[str, Any], str, str]:
        session = self._session_manager.get(effective_session_id)
        return (
            self._hook_manager,
            self._session_manager,
            session,
            SessionSource.CLAUDE,
            {"_platform_session_id": effective_session_id},
            str(self._project_path),
            self._project_id,
        )

    def _build_before_tool_event(
        self,
        *,
        effective_session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> HookEvent:
        return build_before_tool_event(
            self,
            effective_session_id=effective_session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )

    def _prepare_arguments(
        self,
        arguments: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return (arguments if isinstance(arguments, dict) else {}, None)


def _session(
    db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
    *,
    agent_depth: int,
) -> str:
    project = LocalProjectManager(db).create(
        name=f"direct-tool-{agent_depth}",
        repo_path=str(tmp_path),
    )
    return session_manager.register_session(
        external_id=f"external-{agent_depth}",
        machine_id="machine-1",
        source="claude",
        project_id=project.id,
        project_path=str(tmp_path),
        agent_depth=agent_depth,
    )


def _seed_agent_variables(db: HubDatabase, session_id: str, *, spawned: bool) -> None:
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "qa-reviewer" if spawned else "default",
            "_active_rule_names": None,
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": spawned,
            "loaded_skills": ["task-transitions"],
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )


@pytest.mark.asyncio
async def test_direct_mcp_review_tool_reconciles_spawned_session_before_rules(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Direct MCP calls repair stale spawned status before review-gate rules run."""
    _load_interactive_review_block_rule(temp_db)
    session_manager = SessionManager(temp_db)
    child_session_id = _session(temp_db, session_manager, tmp_path, agent_depth=1)
    _seed_agent_variables(temp_db, child_session_id, spawned=False)
    session = session_manager.get(child_session_id)
    assert session is not None
    service = _DirectToolService(temp_db, session_manager, session.project_id, tmp_path)

    _, _, _, error = await apply_before_tool_enforcement(
        service,
        "gobby-tasks-ops",
        "approve_review",
        arguments={"task_id": "#1", "stage_name": "development"},
        session_id=child_session_id,
    )

    variables = SessionVariableManager(temp_db).get_variables(child_session_id)
    assert error is None
    assert variables["is_spawned_agent"] is True


@pytest.mark.asyncio
async def test_direct_mcp_review_tool_still_blocks_interactive_session(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """The interactive protection still applies to non-spawned sessions."""
    _load_interactive_review_block_rule(temp_db)
    session_manager = SessionManager(temp_db)
    session_id = _session(temp_db, session_manager, tmp_path, agent_depth=0)
    _seed_agent_variables(temp_db, session_id, spawned=False)
    session = session_manager.get(session_id)
    assert session is not None
    service = _DirectToolService(temp_db, session_manager, session.project_id, tmp_path)

    _, _, _, error = await apply_before_tool_enforcement(
        service,
        "gobby-tasks-ops",
        "approve_review",
        arguments={"task_id": "#1", "stage_name": "development"},
        session_id=session_id,
    )

    assert error is not None
    assert "block-needs-review-interactive" in error["error"]
