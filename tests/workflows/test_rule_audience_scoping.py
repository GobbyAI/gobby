"""Red tests for build-agent rule audience scoping."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


def _make_event(data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="11111111-1111-4111-8111-111111111111",
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


def test_build_rules_sync_with_audience(temp_db: HubDatabase) -> None:
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.workflows.sync_rules import sync_bundled_rules

    sync_bundled_rules(temp_db)
    manager = RuleDefinitionManager(temp_db)

    row = manager.get_by_name("build-agent-block-full-pytest")
    assert row is not None
    assert row.definition_json["audience"] == "autonomous"


@pytest.mark.asyncio
async def test_autonomous_audience_rules_skip_interactive_sessions(
    temp_db: HubDatabase,
) -> None:
    from gobby.storage.definitions.rules import RuleDefinitionManager

    manager = RuleDefinitionManager(temp_db)
    manager.create(
        name="autonomous-only",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            audience="autonomous",
            effects=[RuleEffect(type="block", tools=["Bash"], reason="autonomous only")],
        ).model_dump_json(),
        enabled=True,
        priority=10,
    )

    engine = RuleEngine(temp_db)
    event = _make_event({"tool_name": "Bash"})

    interactive = await engine.evaluate(
        event,
        session_id="11111111-1111-4111-8111-111111111111",
        variables={},
    )
    autonomous = await engine.evaluate(
        event,
        session_id="22222222-2222-4222-8222-222222222222",
        variables={"is_spawned_agent": True},
    )

    assert interactive.decision == "allow"
    assert autonomous.decision == "block"
