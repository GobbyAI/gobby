"""Tests for the bundled `roles` rule group (shared seat guidance)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "22222222-2222-4222-8222-222222222222"
ROLES_DIR = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules/roles"
GUIDANCE_HEADING = "## Seat Guidance"


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    manager = RuleDefinitionManager(temp_db)
    for rule_file in sorted(ROLES_DIR.glob("*.yaml")):
        document = yaml.safe_load(rule_file.read_text())
        for name, rule_data in document["rules"].items():
            body_data, metadata = split_rule_definition_data(rule_data)
            manager.create(
                name=name,
                definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
                priority=metadata["priority"],
                enabled=metadata["enabled"],
                tags=document["tags"],
            )
    return RuleEngine(temp_db)


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


async def _turn_context(engine: RuleEngine, variables: dict[str, Any]) -> str:
    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "hello"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    return response.context or ""


@pytest.mark.asyncio
async def test_seat_common_injected_once_per_epoch(engine: RuleEngine) -> None:
    variables: dict[str, Any] = {"_persona_name": "plan-writer"}

    first = await _turn_context(engine, variables)
    second = await _turn_context(engine, variables)

    assert GUIDANCE_HEADING in first
    assert "only when Josh asks" in first
    assert "Game Goblins jobs are never rerun" in first
    assert "Give no load numbers unless load is breaching" in first
    assert GUIDANCE_HEADING not in second


@pytest.mark.asyncio
async def test_seat_common_matches_spawned_and_skips_non_seats(engine: RuleEngine) -> None:
    spawned = await _turn_context(engine, {"_agent_type": "developer"})
    orchestrator = await _turn_context(engine, {"_persona_name": "orchestrator"})
    plain = await _turn_context(engine, {})
    off_catalogue = await _turn_context(engine, {"_persona_name": "design-lead"})

    assert GUIDANCE_HEADING in spawned
    assert GUIDANCE_HEADING in orchestrator
    assert GUIDANCE_HEADING not in plain
    assert GUIDANCE_HEADING not in off_catalogue


@pytest.mark.asyncio
async def test_seat_common_rearms_after_compact(engine: RuleEngine) -> None:
    variables: dict[str, Any] = {"_persona_name": "plan-writer"}
    assert GUIDANCE_HEADING in await _turn_context(engine, variables)

    await engine.evaluate(
        _event(HookEventType.SESSION_START, {"source": "compact"}),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert GUIDANCE_HEADING in await _turn_context(engine, variables)
