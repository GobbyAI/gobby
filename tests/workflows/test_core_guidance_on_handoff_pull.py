"""Core guidance is requested on the turn that carries a pending handoff pull.

After a compaction the first turn both owes ``get_handoff`` and starts a fresh
context epoch with an empty skill ledger. The bundled turn-start guidance must
fire on that turn, while the pull gate still sequences the loads after the pull.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _turn_start_skill_targets() -> set[str]:
    targets: set[str] = set()
    for path in sorted(get_bundled_rules_path().rglob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict):
            continue
        for rule in (document.get("rules") or {}).values():
            if not isinstance(rule, dict) or rule.get("event") != "turn_start":
                continue
            for effect in rule.get("effects") or []:
                if isinstance(effect, dict) and effect.get("type") == "load_skill":
                    targets.add(str(effect["skill"]))
    return targets


def _fresh_epoch_variables() -> dict[str, Any]:
    return {
        "handoff_pull_pending": True,
        "loaded_skills": [],
        "loaded_skill_references": [],
    }


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    sync_bundled_rules(temp_db)
    return RuleEngine(temp_db)


@pytest.mark.asyncio
async def test_pending_pull_turn_requests_core_guidance(engine: RuleEngine) -> None:
    targets = _turn_start_skill_targets()
    assert targets

    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "continue"}),
        session_id=SESSION_ID,
        variables=_fresh_epoch_variables(),
    )

    context = response.context or ""
    requested = {target for target in targets if skill_fetch_directive(target) in context}
    assert requested, "no turn-start guidance was requested while the pull was pending"


@pytest.mark.asyncio
async def test_guidance_loads_still_wait_for_the_pull(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "mcp_server": "gobby-skills",
                "mcp_tool": "get_skill_file",
                "tool_input": {"server_name": "gobby-skills", "tool_name": "get_skill_file"},
            },
        ),
        session_id=SESSION_ID,
        variables=_fresh_epoch_variables(),
    )

    assert response.decision == "block"
    assert "get_handoff" in (response.reason or "")
