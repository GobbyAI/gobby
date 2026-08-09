"""Integration coverage for bundled Impeccable detector rules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.run_command import RunCommandResult
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROVIDERS = [
    SessionSource.CLAUDE,
    SessionSource.CODEX,
    SessionSource.QWEN,
    SessionSource.DROID,
    SessionSource.GROK,
    SessionSource.AGY,
]


@pytest.fixture
def impeccable_db(temp_db: HubDatabase) -> HubDatabase:
    rules_path = get_bundled_rules_path() / "impeccable"
    result = sync_bundled_rules(temp_db, rules_path)
    assert result["success"] is True
    return temp_db


def _event(source: SessionSource, event_type: HookEventType) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=f"{source.value}-session",
        source=source,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Write",
            "tool_input": {"file_path": "src/Card.tsx"},
        },
        cwd=str(Path.cwd()),
        metadata={},
    )


def test_impeccable_templates_sync_enabled(impeccable_db: HubDatabase) -> None:
    manager = LocalWorkflowDefinitionManager(impeccable_db)

    edit = manager.get_by_name("impeccable-edit-pass")
    deep = manager.get_by_name("impeccable-deep-pass")

    assert edit is not None and edit.enabled is True
    assert deep is not None and deep.enabled is True


@pytest.mark.parametrize("source", PROVIDERS)
async def test_edit_detector_evaluates_for_supported_sources(
    impeccable_db: HubDatabase,
    source: SessionSource,
) -> None:
    engine = RuleEngine(impeccable_db)
    execute = AsyncMock(
        return_value=RunCommandResult(
            status="success",
            context=f"finding from {source.value}",
            duration_ms=1.0,
            exit_code=0,
            stdout_bytes=10,
            stderr_bytes=0,
            timeout_seconds=5.0,
            overflow_stream=None,
            background=False,
        )
    )
    with patch.object(engine, "_execute_run_command", execute):
        response = await engine.evaluate(
            _event(source, HookEventType.AFTER_TOOL),
            session_id=SESSION_ID,
            variables={},
        )

    assert response.decision == "allow"
    assert response.context == f"finding from {source.value}"
    execute.assert_awaited_once()


@pytest.mark.parametrize("source", PROVIDERS)
async def test_deep_detector_schedules_for_supported_sources(
    impeccable_db: HubDatabase,
    source: SessionSource,
) -> None:
    engine = RuleEngine(impeccable_db)
    with patch("gobby.workflows.engine.effects.create_background_task") as create_task:
        response = await engine.evaluate(
            _event(source, HookEventType.STOP),
            session_id=SESSION_ID,
            variables={},
        )

    assert response.decision == "allow"
    create_task.assert_called_once()
    create_task.call_args.args[0].close()
