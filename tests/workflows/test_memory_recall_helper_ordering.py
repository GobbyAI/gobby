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

RULE_NAME = "cancel-stale-memory-recall-helpers"
RULE_SOURCE = (
    Path("src/gobby/install/shared/workflows/rules/memory-lifecycle") / f"{RULE_NAME}.yaml"
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


def _sync_cancel_rule_only(db: HubDatabase, tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    target_dir = rules_root / "memory-lifecycle"
    target_dir.mkdir(parents=True)
    shutil.copy2(RULE_SOURCE, target_dir / RULE_SOURCE.name)
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
    _sync_cancel_rule_only(db, tmp_path)
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

    row = engine.definition_manager.get_by_name(RULE_NAME)
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
