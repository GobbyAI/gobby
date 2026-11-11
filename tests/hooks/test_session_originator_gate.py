"""A session may originate Gobby MCP calls only under its own session_ref."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit

# manager_with_mocks comes from tests/hooks/conftest.py.

OWN_SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


def _mock(value: object) -> MagicMock:
    assert isinstance(value, MagicMock)
    return value


@dataclass
class Gate:
    """The hook manager under test, its event handler and its session-ref resolver."""

    manager: HookManager
    handler: MagicMock
    resolve: MagicMock

    def knows(self, refs: dict[str, str]) -> None:
        def resolve(ref: str, project_id: str | None = None) -> str:
            if ref in refs:
                return refs[ref]
            raise ValueError(f"Session {ref} not found")

        self.resolve.side_effect = resolve

    def run(
        self, tool_input: dict[str, Any], tool_name: str = "mcp__gobby__call_tool"
    ) -> HookResponse:
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="codex-thread",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": tool_name, "tool_input": tool_input},
            machine_id="21000000-0000-4000-8000-000000000002",
        )
        event.project_id = "proj-1"
        event.metadata["_platform_session_id"] = OWN_SESSION
        return self.manager._handle_internal(event)


@pytest.fixture
def gate(manager_with_mocks: HookManager, monkeypatch: pytest.MonkeyPatch) -> Gate:
    handler = MagicMock(return_value=HookResponse(decision="allow"))
    _mock(manager_with_mocks._event_handlers.get_handler).return_value = handler
    _mock(manager_with_mocks._session_lookup.resolve).return_value = None
    _mock(manager_with_mocks._workflow_handler.handle).return_value = HookResponse(decision="allow")
    monkeypatch.setattr(manager_with_mocks._enricher, "enrich", MagicMock())
    return Gate(
        manager=manager_with_mocks,
        handler=handler,
        resolve=_mock(manager_with_mocks._session_manager.resolve_session_reference),
    )


def _call(session_id: str | None) -> dict[str, Any]:
    tool_input: dict[str, Any] = {
        "server_name": "gobby-tasks",
        "tool_name": "get_task",
        "arguments": {},
    }
    if session_id is not None:
        tool_input["session_id"] = session_id
    return tool_input


@pytest.mark.parametrize("ref", ["#7", "7", "gobby#7", OWN_SESSION, "codex-thread"])
def test_a_call_naming_the_callers_own_session_is_allowed(gate: Gate, ref: str) -> None:
    gate.knows(
        {
            "#7": OWN_SESSION,
            "7": OWN_SESSION,
            "gobby#7": OWN_SESSION,
            OWN_SESSION: OWN_SESSION,
            "codex-thread": OWN_SESSION,
        }
    )

    response = gate.run(_call(ref))

    assert response.decision == "allow", response.reason
    gate.handler.assert_called_once()


@pytest.mark.parametrize("ref", ["#8", OTHER_SESSION])
def test_a_call_naming_another_session_as_originator_is_blocked(gate: Gate, ref: str) -> None:
    gate.knows({"#8": OTHER_SESSION, OTHER_SESSION: OTHER_SESSION})

    response = gate.run(_call(ref))

    assert response.decision == "block"
    assert response.reason is not None
    assert OWN_SESSION in response.reason
    gate.handler.assert_not_called()


def test_a_call_naming_an_unresolvable_session_is_blocked(gate: Gate) -> None:
    gate.knows({})

    response = gate.run(_call("#999999"))

    assert response.decision == "block"
    assert response.reason is not None
    assert "#999999" in response.reason


@pytest.mark.parametrize("tool_name", ["mcp__gobby__list_tools", "mcp__gobby__get_tool_schema"])
def test_every_gobby_proxy_tool_is_gated(gate: Gate, tool_name: str) -> None:
    gate.knows({"#8": OTHER_SESSION})

    response = gate.run({"server_name": "gobby-tasks", "session_id": "#8"}, tool_name=tool_name)

    assert response.decision == "block"


@pytest.mark.parametrize("tool_name", ["mcp__gobby__set_variable", "mcp__gobby__get_variable"])
def test_variable_tools_name_their_target_and_are_not_gated(gate: Gate, tool_name: str) -> None:
    gate.knows({"#8": OTHER_SESSION})

    response = gate.run({"name": "flag", "value": True, "session_id": "#8"}, tool_name=tool_name)

    assert response.decision == "allow", response.reason


def test_a_target_session_inside_arguments_is_not_the_originator(gate: Gate) -> None:
    gate.knows({"#8": OTHER_SESSION})
    tool_input = _call(None)
    tool_input["arguments"] = {"session_id": "#8"}

    response = gate.run(tool_input)

    assert response.decision == "allow", response.reason


def test_a_call_without_a_session_id_is_not_gated(gate: Gate) -> None:
    response = gate.run(_call(None))

    assert response.decision == "allow", response.reason


def test_another_servers_tool_is_not_gated(gate: Gate) -> None:
    gate.knows({})

    response = gate.run({"session_id": "#8"}, tool_name="mcp__other__tool")

    assert response.decision == "allow", response.reason
