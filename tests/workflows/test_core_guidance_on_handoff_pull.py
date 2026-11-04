"""After a compaction, one instruction bundle follows the handoff pull.

The first post-compact turn owes ``get_handoff`` and starts a fresh context
epoch with an empty skill ledger. The bundled rules must ask for every owed
instruction in one ``get_skill(names=[...])`` call delivered with the pull's
result. The batch then pages under the 15K response budget, so the context
costs the pull, that call and its cursor pages, and no skill or reference gate
refuses work once the last page is credited.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.observer_mcp import detect_mcp_call
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
SEAT_PROCEDURE = "gobby:references/agents/messaging.md"
SEAT_REFERENCE = "gobby:references/tasks/overview.md"
OWED = [
    "gobby:references/memory/overview.md",
    "brevity",
    "restraint",
    "gobby:references/development/obligations.md",
    "gobby:references/code-index/overview.md",
    "gobby:references/plan/overview.md",
    SEAT_PROCEDURE,
    SEAT_REFERENCE,
]
CORE = OWED[:3]
# Calls after a compaction, the pull included: the bundle's names call plus its
# cursor pages under the 15K response budget, measured on the bundled bodies
# (core fits one page; the full bundle of about 32K takes three).
CORE_CALL_CEILING = 2
OWED_CALL_CEILING = 4
_BATCH_CALL = re.compile(r'call_tool\("gobby-skills", "get_skill", (\{"names":\[[^\]]*\]\})\)')


def _core_variables() -> dict[str, Any]:
    return {"handoff_pull_pending": True, "loaded_skills": [], "loaded_skill_references": []}


def _post_compact_variables(*, pull_pending: bool) -> dict[str, Any]:
    return {
        "handoff_pull_pending": pull_pending,
        "loaded_skills": [],
        "loaded_skill_references": [],
        "claimed_task_is_source_work": True,
        "code_index_available": True,
        "plan_mode": True,
        "plan_skill_directive_delivered": True,
        "_seat_procedure": SEAT_PROCEDURE,
        "_seat_references": [SEAT_REFERENCE],
    }


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _mcp_event(event_type: HookEventType, server: str, tool: str, **extra: Any) -> HookEvent:
    return _event(
        event_type,
        {
            "tool_name": "mcp__gobby__call_tool",
            "canonical_tool_kind": "mcp",
            "mcp_server": server,
            "mcp_tool": tool,
            "tool_input": {"server_name": server, "tool_name": tool, "arguments": {}},
            **extra,
        },
    )


def _load_calls(text: str | None) -> list[list[str]]:
    return [json.loads(match)["names"] for match in _BATCH_CALL.findall(text or "")]


def _credited(variables: dict[str, Any]) -> list[str]:
    return [*variables["loaded_skills"], *variables["loaded_skill_references"]]


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    sync_bundled_rules(temp_db)
    return RuleEngine(temp_db)


@pytest.fixture
def get_skill(temp_db: HubDatabase) -> Any:
    sync_bundled_skills(temp_db)
    tool = create_skills_registry(temp_db).get_tool("get_skill")
    assert tool is not None
    return tool


async def _deliver(
    engine: RuleEngine, get_skill: Any, names: list[str], variables: dict[str, Any]
) -> int:
    """Run the requested batch to its last page through the observer; return its calls."""
    arguments: dict[str, Any] = {"names": names}
    completed: list[str] = []
    calls = 0
    while True:
        result = await get_skill(**arguments)
        calls += 1
        assert result["success"] is True, result
        event = _mcp_event(
            HookEventType.AFTER_TOOL,
            "gobby-skills",
            "get_skill",
            tool_output={"success": True, "result": result},
        )
        event.data["tool_input"]["arguments"] = arguments
        detect_mcp_call(event, variables, SESSION_ID)
        completed += result["page"]["completed"]
        # An identity is credited only on the page that carries its end.
        assert sorted(_credited(variables)) == sorted(completed)
        partial = {item["identity"] for item in result["instructions"]} - set(completed)
        assert not partial & set(_credited(variables))
        after = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert _load_calls(after.context) == [], "a page must not request another bundle"
        cursor = result["page"]["next_cursor"]
        if cursor is None:
            assert completed == names
            return calls
        arguments = {"cursor": cursor}


def _core_gated_events() -> list[HookEvent]:
    """Work the core bundle alone unlocks: no plan, source or seat gate is owed."""
    return [
        _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Grep",
                "canonical_tool_kind": "search",
                "tool_input": {"pattern": "needle", "path": "src"},
            },
        ),
        _mcp_event(HookEventType.BEFORE_TOOL, "gobby-tasks", "get_task"),
        _event(HookEventType.BEFORE_AGENT, {"prompt": "next"}),
    ]


def _full_gated_events() -> list[HookEvent]:
    """The plan, code-index and seat gates the full bundle must satisfy."""
    plan_write = _event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "Write",
            "canonical_tool_kind": "write",
            "canonical_file_path": "/repo/.gobby/plans/draft.md",
            "tool_input": {"file_path": "/repo/.gobby/plans/draft.md", "content": "x"},
        },
    )
    return [plan_write, *_core_gated_events()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variables_factory", "owed", "ceiling", "gated_factory"),
    [
        (_core_variables, CORE, CORE_CALL_CEILING, _core_gated_events),
        (
            lambda: _post_compact_variables(pull_pending=True),
            OWED,
            OWED_CALL_CEILING,
            _full_gated_events,
        ),
    ],
    ids=["core", "source-work-plan-mode-seat"],
)
async def test_post_compact_bundle_loads_within_its_call_ceiling(
    engine: RuleEngine,
    get_skill: Any,
    variables_factory: Callable[[], dict[str, Any]],
    owed: list[str],
    ceiling: int,
    gated_factory: Callable[[], list[HookEvent]],
) -> None:
    variables = variables_factory()
    first_turn = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "continue"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    assert '"get_skill"' not in (first_turn.context or "")

    variables["handoff_pull_pending"] = False
    pulled = await engine.evaluate(
        _mcp_event(
            HookEventType.AFTER_TOOL,
            "gobby-sessions",
            "get_handoff",
            tool_output={"success": True, "result": {"found": True, "handoff": "state"}},
        ),
        session_id=SESSION_ID,
        variables=variables,
    )
    assert _load_calls(pulled.context) == [owed]
    assert (pulled.context or "").count("call_tool(") == 1

    calls = 1 + await _deliver(engine, get_skill, owed, variables)

    assert calls <= ceiling
    for event in gated_factory():
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert "get_skill" not in (response.reason or ""), event.data
        assert "get_skill" not in (response.context or ""), event.data


@pytest.mark.asyncio
async def test_cold_start_names_every_owed_instruction_in_one_call(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "start"}),
        session_id=SESSION_ID,
        variables=_post_compact_variables(pull_pending=False),
    )

    assert _load_calls(response.context) == [OWED]


@pytest.mark.asyncio
async def test_unavailable_instruction_is_not_requested_again(engine: RuleEngine) -> None:
    variables = {
        **_post_compact_variables(pull_pending=False),
        "unavailable_instructions": [SEAT_REFERENCE],
    }

    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "start"}),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert _load_calls(response.context) == [[name for name in OWED if name != SEAT_REFERENCE]]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_skill", "get_skill_file", "list_skills", "search_skills"])
async def test_bootstrap_tools_pass_every_gate_before_any_load(
    engine: RuleEngine, tool: str
) -> None:
    """No later skill or reference gate refuses the calls that satisfy it (#25580 item 9)."""
    variables = _post_compact_variables(pull_pending=False)
    event = _mcp_event(HookEventType.BEFORE_TOOL, "gobby-skills", tool)
    event.data["tool_input"]["arguments"] = {"names": OWED}

    response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
async def test_the_pull_itself_passes_while_it_is_owed(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _mcp_event(HookEventType.BEFORE_TOOL, "gobby-sessions", "get_handoff"),
        session_id=SESSION_ID,
        variables=_post_compact_variables(pull_pending=True),
    )

    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
async def test_skill_loads_still_wait_for_the_pull(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _mcp_event(HookEventType.BEFORE_TOOL, "gobby-skills", "get_skill"),
        session_id=SESSION_ID,
        variables=_post_compact_variables(pull_pending=True),
    )

    assert response.decision == "block"
    assert "get_handoff" in (response.reason or "")
