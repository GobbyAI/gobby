"""Red tests for build-agent rule audience scoping."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


def _make_event(data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def test_build_rules_autonomous_only() -> None:
    rule_dir = Path("src/gobby/install/shared/rules/build")
    rule_files = [path for path in rule_dir.glob("*.yaml") if path.is_file()]

    assert rule_files, "build rule YAML files must be bundled"
    for path in rule_files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["audience"] == "autonomous", path


def test_build_rules_sync_with_audience(temp_db: LocalDatabase) -> None:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.sync_rules import sync_bundled_rules

    sync_bundled_rules(temp_db)
    manager = LocalWorkflowDefinitionManager(temp_db)

    row = manager.get_by_name("build-agent-block-full-pytest")
    assert row is not None
    body = yaml.safe_load(row.definition_json)
    assert body["audience"] == "autonomous"


@pytest.mark.asyncio
async def test_autonomous_audience_rules_skip_interactive_sessions(
    temp_db: LocalDatabase,
) -> None:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    manager = LocalWorkflowDefinitionManager(temp_db)
    manager.create(
        name="autonomous-only",
        definition_json=RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            audience="autonomous",
            effects=[RuleEffect(type="block", tools=["Bash"], reason="autonomous only")],
        ).model_dump_json(),
        workflow_type="rule",
        enabled=True,
        priority=10,
    )

    engine = RuleEngine(temp_db)
    event = _make_event({"tool_name": "Bash"})

    interactive = await engine.evaluate(event, session_id="sess-1", variables={})
    autonomous = await engine.evaluate(
        event,
        session_id="sess-2",
        variables={"is_spawned_agent": True},
    )

    assert interactive.decision == "allow"
    assert autonomous.decision == "block"
