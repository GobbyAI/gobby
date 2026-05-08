"""Regression test for #13048: bundled strip-skip-validation rule fires on the
proxy before-tool event shape, and ``apply_before_tool_enforcement`` propagates
the rewrite into the dispatched arguments.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.mcp_proxy.services.result_handling import (
    apply_before_tool_enforcement,
    build_before_tool_event,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit


def _load_strip_skip_validation_rule(db: LocalDatabase) -> None:
    """Insert only the strip-skip-validation rule so other bundled rules don't interfere."""
    yaml_path = (
        get_bundled_rules_path() / "task-enforcement" / "strip-skip-validation-with-commit.yaml"
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    rule_data = data["rules"]["strip-skip-validation-with-commit"]
    body = RuleDefinitionBody.model_validate(rule_data)
    LocalWorkflowDefinitionManager(db).create(
        name="strip-skip-validation-with-commit",
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=rule_data.get("priority", 33),
        enabled=rule_data.get("enabled", True),
    )


class _StubService:
    """Minimal service surface required by ``apply_before_tool_enforcement``."""

    def __init__(self, engine: RuleEngine, session_id: str, variables: dict[str, Any]) -> None:
        self._engine = engine
        self._session_id = session_id
        self._variables = variables

        workflow_handler = MagicMock()
        workflow_handler.evaluate.side_effect = self._evaluate
        hook_manager = MagicMock()
        hook_manager._workflow_handler = workflow_handler
        self._hook_manager = hook_manager

    def _evaluate(self, event: HookEvent) -> HookResponse:
        # Production calls workflow_handler.evaluate via asyncio.to_thread,
        # so the stub is sync and can safely use asyncio.run for the async engine.
        return asyncio.run(
            self._engine.evaluate(
                event=event,
                session_id=self._session_id,
                variables=self._variables,
            )
        )

    def _get_effective_session_id(self, session_id: str | None) -> str | None:
        return session_id or self._session_id

    def _resolve_hook_manager(self) -> MagicMock:
        return self._hook_manager

    def _resolve_tool_event_context(
        self, effective_session_id: str
    ) -> tuple[None, None, None, SessionSource, dict[str, Any], None, None]:
        return (None, None, None, SessionSource.CLAUDE, {}, None, None)

    def _build_before_tool_event(
        self,
        *,
        effective_session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> HookEvent:
        event: HookEvent = build_before_tool_event(
            self,
            effective_session_id=effective_session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        return event

    def _prepare_arguments(
        self, arguments: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if arguments is None:
            return {}, None
        if isinstance(arguments, dict):
            return arguments, None
        return None, {"success": False, "error": "invalid arguments"}


@pytest.mark.asyncio
async def test_apply_before_tool_enforcement_strips_skip_validation(tmp_path: Path) -> None:
    """End-to-end regression for #13048.

    Builds the same before_tool event the proxy emits, runs it through the real
    RuleEngine with the bundled YAML rule loaded, then calls the production
    ``apply_before_tool_enforcement`` helper. The dispatched arguments must
    have ``skip_validation: False`` after the rewrite is applied.
    """
    db = LocalDatabase(tmp_path / "test.db")
    run_migrations(db)
    _load_strip_skip_validation_rule(db)

    engine = RuleEngine(db)
    service = _StubService(
        engine=engine,
        session_id="sess-1",
        variables={"task_has_commits": True},
    )

    server_name, tool_name, arguments, error = await apply_before_tool_enforcement(
        service,
        "gobby-tasks",
        "close_task",
        arguments={"task_id": "t-1", "skip_validation": True},
        session_id="sess-1",
    )

    assert error is None
    assert server_name == "gobby-tasks"
    assert tool_name == "close_task"
    assert arguments == {"task_id": "t-1", "skip_validation": False}


@pytest.mark.asyncio
async def test_apply_before_tool_enforcement_passthrough_without_commits(
    tmp_path: Path,
) -> None:
    """Without commits, the rule does not fire and arguments pass through."""
    db = LocalDatabase(tmp_path / "test.db")
    run_migrations(db)
    _load_strip_skip_validation_rule(db)

    engine = RuleEngine(db)
    service = _StubService(
        engine=engine,
        session_id="sess-1",
        variables={"task_has_commits": False},
    )

    server_name, tool_name, arguments, error = await apply_before_tool_enforcement(
        service,
        "gobby-tasks",
        "close_task",
        arguments={"task_id": "t-1", "skip_validation": True},
        session_id="sess-1",
    )

    assert error is None
    assert server_name == "gobby-tasks"
    assert tool_name == "close_task"
    assert arguments == {"task_id": "t-1", "skip_validation": True}


def test_proxy_before_tool_event_shape_unchanged() -> None:
    """If ``build_before_tool_event`` ever stops emitting ``mcp__gobby__call_tool``
    as the wrapper tool_name, the rule's ``when:`` clause needs to be revisited.
    """

    class _CtxStub:
        def _resolve_tool_event_context(self, _sid: str) -> Any:
            return (None, None, None, SessionSource.CLAUDE, {}, None, None)

    event = build_before_tool_event(
        _CtxStub(),
        effective_session_id="sess-1",
        server_name="gobby-tasks",
        tool_name="close_task",
        arguments={"task_id": "t-1", "skip_validation": True},
    )
    assert event.event_type == HookEventType.BEFORE_TOOL
    assert event.data["tool_name"] == "mcp__gobby__call_tool"
    assert event.data["tool_input"]["server_name"] == "gobby-tasks"
    assert event.data["tool_input"]["tool_name"] == "close_task"
    assert event.data["tool_input"]["arguments"]["skip_validation"] is True
