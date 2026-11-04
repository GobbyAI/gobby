"""gobby-agents cold_start_admin: apply an enabled definition to the calling session."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import (
    reset_request_principal,
    session_context_for_test,
    set_request_principal,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


@dataclass
class _Stack:
    db: HubDatabase
    sessions: SessionManager
    agents: InternalToolRegistry
    project_id: str


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
async def stack(temp_db: HubDatabase, sample_project: dict[str, Any]) -> AsyncIterator[_Stack]:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    seed_seat(
        temp_db,
        "fixture-coordinator",
        surfaces=["spawn", "persona"],
        prompts={"agent": "Synthetic fixture-coordinator seat.", "persona": "Coordinate."},
        cold_start_commands=["admin"],
    )
    seed_seat(
        temp_db,
        "fixture-helper",
        surfaces=["spawn", "persona"],
        prompts={"agent": "Synthetic fixture-helper seat.", "persona": "Help."},
        cold_start_commands=["helper"],
    )
    seed_seat(
        temp_db,
        "fixture-plain",
        surfaces=["spawn", "persona"],
        prompts={"agent": "Synthetic fixture-plain seat.", "persona": "Plain."},
    )
    sessions = SessionManager(temp_db)
    agents = create_agents_registry(MagicMock(), session_manager=sessions, db=temp_db)
    yield _Stack(temp_db, sessions, agents, str(sample_project["id"]))


@contextmanager
def _principal(principal: AgentApiTokenClaims | None | Literal[False]) -> Iterator[None]:
    async def resolve() -> AgentApiTokenClaims | None | Literal[False]:
        return principal

    token = set_request_principal(resolve)
    try:
        yield
    finally:
        reset_request_principal(token)


def _session(stack: _Stack, external_id: str) -> Session:
    return stack.sessions.register(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=stack.project_id,
    )


async def _cold_start(stack: _Stack, session: Session, role: str) -> dict[str, Any]:
    with _principal(None), session_context_for_test(session.id):
        result = await stack.agents.call("cold_start_admin", {"role": role})
    assert isinstance(result, dict)
    return result


def _agent_type(stack: _Stack, session: Session) -> object:
    return (
        SessionVariableManager(stack.db)
        .get_variable_subset(session.id, ("_agent_type",))
        .get("_agent_type")
    )


def _ref(stack: _Stack, session: Session) -> str:
    stored = stack.sessions.get(session.id)
    assert stored is not None
    return stored.ref


async def test_cold_start_applies_the_definition_to_the_calling_session(stack: _Stack) -> None:
    caller = _session(stack, "caller-session")

    result = await _cold_start(stack, caller, "fixture-coordinator")

    assert result == {
        "success": True,
        "role": "fixture-coordinator",
        "definition": "applied",
        "session": _ref(stack, caller),
    }
    assert _agent_type(stack, caller) == "fixture-coordinator"


async def test_cold_start_is_rerunnable_on_the_same_session(stack: _Stack) -> None:
    caller = _session(stack, "rerun-session")
    first = await _cold_start(stack, caller, "fixture-coordinator")
    assert first["success"] is True, first

    again = await _cold_start(stack, caller, "fixture-coordinator")

    assert (again["success"], again["definition"]) == (True, "unchanged")
    assert _agent_type(stack, caller) == "fixture-coordinator"


async def test_a_role_change_on_a_seated_session_requires_relaunch(stack: _Stack) -> None:
    caller = _session(stack, "switch-session")
    first = await _cold_start(stack, caller, "fixture-coordinator")
    assert first["success"] is True, first

    switched = await _cold_start(stack, caller, "fixture-helper")

    assert (switched["success"], switched["error_code"]) == (
        False,
        "role_change_requires_relaunch",
    )
    assert _agent_type(stack, caller) == "fixture-coordinator"


async def test_a_definition_without_cold_start_commands_is_refused(stack: _Stack) -> None:
    caller = _session(stack, "plain-session")

    result = await _cold_start(stack, caller, "fixture-plain")

    assert (result["success"], result["error_code"]) == (False, "invalid_role")
    assert _agent_type(stack, caller) is None


async def test_a_disabled_definition_is_refused(stack: _Stack) -> None:
    definitions = AgentDefinitionManager(stack.db)
    row = definitions.get_by_name("fixture-coordinator")
    assert row is not None
    definitions.update(row.id, definition_json=dict(row.definition_json), enabled=False)
    caller = _session(stack, "disabled-session")

    result = await _cold_start(stack, caller, "fixture-coordinator")

    assert (result["success"], result["error_code"]) == (False, "invalid_role")
    assert _agent_type(stack, caller) is None


async def test_an_unknown_role_is_refused(stack: _Stack) -> None:
    caller = _session(stack, "unknown-session")

    result = await _cold_start(stack, caller, "fixture-definition-0f916789")

    assert (result["success"], result["error_code"]) == (False, "invalid_role")
    assert _agent_type(stack, caller) is None


async def test_a_call_without_a_session_context_is_refused(stack: _Stack) -> None:
    with _principal(None):
        result = await stack.agents.call("cold_start_admin", {"role": "fixture-coordinator"})

    assert isinstance(result, dict)
    assert (result["success"], result["error_code"]) == (False, "session_unavailable")
