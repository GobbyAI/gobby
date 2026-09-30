"""The hold/release tools admit only root terminal callers and same-machine seats."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._claim_hold import register_claim_hold_tools
from gobby.sessions.operator_claim_hold import (
    OPERATOR_CLAIM_HOLD_VARIABLE,
    operator_claim_hold_payload,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.sessions._contested_expiry import read_session_variables
from gobby.utils.datetime import utc_now
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import insert_isolated_machine
from tests.storage.tasks.test_sweep_stale_claims import _make_session

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    def get_tool(self, name: str) -> Callable[..., Any]:
        return self._tools[name].func


def _registry(temp_db: HubDatabase) -> _TestRegistry:
    registry = _TestRegistry(name="gobby-sessions", description="test")
    register_claim_hold_tools(registry, SessionManager(temp_db), temp_db)
    return registry


def _seat(temp_db: HubDatabase, project: dict[str, Any], status: str = "paused") -> str:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, project, session_id, status)
    return session_id


def _held(temp_db: HubDatabase, session_id: str) -> bool:
    return OPERATOR_CLAIM_HOLD_VARIABLE in (read_session_variables(temp_db, session_id) or {})


def test_root_terminal_caller_holds_and_releases_a_seat(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    caller, target = _seat(temp_db, sample_project, "active"), _seat(temp_db, sample_project)
    registry = _registry(temp_db)

    with session_context_for_test(caller):
        held = asyncio.run(
            registry.get_tool("hold_session_claims")(session_id=target, reason="CLI update")
        )
        stored = (read_session_variables(temp_db, target) or {})[OPERATOR_CLAIM_HOLD_VARIABLE]
        released = asyncio.run(registry.get_tool("release_session_claims_hold")(session_id=target))

    held_at = datetime.fromisoformat(held["held_at"])
    assert (held["success"], held["session_id"], held["actor_session_id"]) == (True, target, caller)
    assert datetime.fromisoformat(held["expires_at"]) - held_at == timedelta(
        hours=SESSION_REVIVAL_HORIZON_HOURS
    )
    assert (stored["actor_session_id"], stored["reason"]) == (caller, "CLI update")
    assert (released["released"], _held(temp_db, target)) == (True, False)


@pytest.mark.parametrize(
    "demote_caller",
    [
        pytest.param("UPDATE sessions SET agent_depth = 1 WHERE id = %s", id="spawned"),
        pytest.param(
            "UPDATE sessions SET session_type = 'autonomous' WHERE id = %s", id="non_terminal"
        ),
    ],
)
def test_non_root_callers_are_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any], demote_caller: str
) -> None:
    caller, target = _seat(temp_db, sample_project, "active"), _seat(temp_db, sample_project)
    temp_db.execute(demote_caller, (caller,))

    with session_context_for_test(caller):
        result = asyncio.run(
            _registry(temp_db).get_tool("hold_session_claims")(session_id=target, reason="update")
        )

    assert (result["error_code"], _held(temp_db, target)) == ("claim_hold_caller_forbidden", False)


def test_a_seat_on_another_machine_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    caller, target = _seat(temp_db, sample_project, "active"), _seat(temp_db, sample_project)
    foreign_machine = insert_isolated_machine(temp_db)
    temp_db.execute("UPDATE sessions SET machine_id = %s WHERE id = %s", (foreign_machine, target))

    with session_context_for_test(caller):
        result = asyncio.run(
            _registry(temp_db).get_tool("hold_session_claims")(session_id=target, reason="update")
        )

    assert (result["error_code"], _held(temp_db, target)) == ("claim_hold_target_forbidden", False)


def test_a_hold_without_a_reason_is_refused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    caller, target = _seat(temp_db, sample_project, "active"), _seat(temp_db, sample_project)

    with session_context_for_test(caller):
        result = asyncio.run(
            _registry(temp_db).get_tool("hold_session_claims")(session_id=target, reason="  ")
        )

    assert (result["error_code"], _held(temp_db, target)) == ("claim_hold_reason_required", False)


def test_only_the_issuer_renews_or_releases_a_live_hold(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    issuer, other = (
        _seat(temp_db, sample_project, "active"),
        _seat(temp_db, sample_project, "active"),
    )
    target = _seat(temp_db, sample_project)
    registry = _registry(temp_db)
    with session_context_for_test(issuer):
        asyncio.run(registry.get_tool("hold_session_claims")(session_id=target, reason="update"))

    with session_context_for_test(other):
        renewed = asyncio.run(
            registry.get_tool("hold_session_claims")(session_id=target, reason="takeover")
        )
        released = asyncio.run(registry.get_tool("release_session_claims_hold")(session_id=target))
    stored = (read_session_variables(temp_db, target) or {})[OPERATOR_CLAIM_HOLD_VARIABLE]

    for refused in (renewed, released):
        assert (refused["success"], refused["error_code"], refused["actor_session_id"]) == (
            False,
            "claim_hold_held_by_other",
            issuer,
        )
    assert (stored["actor_session_id"], stored["reason"]) == (issuer, "update")


def test_a_lapsed_hold_no_longer_binds_to_its_issuer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    issuer, other = (
        _seat(temp_db, sample_project, "active"),
        _seat(temp_db, sample_project, "active"),
    )
    target = _seat(temp_db, sample_project)
    registry = _registry(temp_db)
    lapsed_at = utc_now() - timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS, minutes=1)
    with session_context_for_test(issuer):
        asyncio.run(registry.get_tool("hold_session_claims")(session_id=target, reason="update"))
    temp_db.execute(
        "UPDATE session_variables SET variables = jsonb_set(variables, %s, %s::jsonb) "
        "WHERE session_id = %s",
        (
            [OPERATOR_CLAIM_HOLD_VARIABLE],
            json.dumps(operator_claim_hold_payload(issuer, "update", lapsed_at)),
            target,
        ),
    )

    with session_context_for_test(other):
        renewed = asyncio.run(
            registry.get_tool("hold_session_claims")(session_id=target, reason="second update")
        )

    assert (renewed["success"], renewed["actor_session_id"]) == (True, other)
