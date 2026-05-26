"""Regression tests for memory recall helper cancellation ordering inputs."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

RULES_ROOT = Path("src/gobby/install/shared/workflows/rules/memory-lifecycle")
CANCEL_RULE_NAME = "cancel-stale-memory-recall-helpers"
INCREMENT_RULE_NAME = "increment-parent-turn-seq"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


def _sync_rule_only(db: HubDatabase, tmp_path: Path, rule_name: str) -> None:
    rules_root = tmp_path / "rules"
    target_dir = rules_root / "memory-lifecycle"
    target_dir.mkdir(parents=True)
    rule_source = RULES_ROOT / f"{rule_name}.yaml"
    shutil.copy2(rule_source, target_dir / rule_source.name)
    result = sync_bundled_rules(db, rules_path=rules_root)
    assert result["errors"] == []


def _platform_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="external-X",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "any prompt"},
        metadata={"_platform_session_id": "platform-Y"},
    )


@pytest.mark.asyncio
async def test_cancel_rule_parent_session_id_resolves_to_platform_session_id(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    _sync_rule_only(db, tmp_path, CANCEL_RULE_NAME)
    dispatched: list[tuple[str, str, dict[str, Any]]] = []

    async def dispatcher(server: str, tool: str, args: dict, event: Any) -> dict[str, Any]:
        dispatched.append((server, tool, args))
        return {
            "success": True,
            "inject_result": True,
            "result": {
                "success": True,
                "cancelled": [],
                "errors": [],
                "count": 0,
            },
        }

    engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    event = _platform_event()

    await engine.evaluate(
        event,
        session_id="platform-Y",
        variables={"servers_listed": True},
    )

    assert dispatched == [
        (
            "gobby-agents",
            "cancel_stale_helpers",
            {
                "parent_session_id": "platform-Y",
                "agent_name": "memory-recall-helper",
            },
        )
    ]
    assert dispatched[0][2]["parent_session_id"] != "external-X"

    row = engine.definition_manager.get_by_name(CANCEL_RULE_NAME)
    body = RuleDefinitionBody.model_validate_json(row.definition_json)
    body.effects[0].arguments["parent_session_id"] = "{{ event.session_id }}"
    engine.definition_manager.update(row.id, definition_json=body.model_dump_json())

    patched_dispatches: list[tuple[str, str, dict[str, Any]]] = []

    async def patched_dispatcher(server: str, tool: str, args: dict, event: Any) -> dict[str, Any]:
        patched_dispatches.append((server, tool, args))
        return {
            "success": True,
            "inject_result": True,
            "result": {
                "success": True,
                "cancelled": [],
                "errors": [],
                "count": 0,
            },
        }

    patched_engine = RuleEngine(db, mcp_dispatcher=patched_dispatcher)

    await patched_engine.evaluate(
        _platform_event(),
        session_id="platform-Y",
        variables={"servers_listed": True},
    )

    assert patched_dispatches[0][2]["parent_session_id"] == "external-X"


@pytest.mark.asyncio
async def test_increment_parent_turn_seq_counter_isolated(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    _sync_rule_only(db, tmp_path, INCREMENT_RULE_NAME)
    engine = RuleEngine(db)
    event = _platform_event()

    variables: dict[str, Any] = {"parent_turn_seq": 0}

    await engine.evaluate(event, session_id="platform-Y", variables=variables)
    assert variables["parent_turn_seq"] == 1

    await engine.evaluate(event, session_id="platform-Y", variables=variables)
    assert variables["parent_turn_seq"] == 2

    spawned_variables: dict[str, Any] = {
        "is_spawned_agent": True,
        "parent_turn_seq": 2,
    }
    await engine.evaluate(event, session_id="spawned-Z", variables=spawned_variables)
    assert spawned_variables["parent_turn_seq"] == 2

    unseeded_variables: dict[str, Any] = {}
    await engine.evaluate(event, session_id="unseeded-Z", variables=unseeded_variables)
    assert "parent_turn_seq" not in unseeded_variables
