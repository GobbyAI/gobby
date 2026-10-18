"""Caller scope keeps a request on machines its user owns."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from gobby.hub.caller_scope import bind_caller, current_caller, require_machine_owned
from gobby.hub.node_client import NodeCommandError
from gobby.utils.machine_id import get_machine_id

TEST_USER_ID = "20000000-0000-4000-8000-000000000001"
OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"


class _RecordingDB:
    """In-memory machines table that counts lookups."""

    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners
        self.calls = 0

    def fetchone(self, _sql: str, params: tuple[object, ...]) -> dict[str, object] | None:
        self.calls += 1
        machine_id = str(params[0])
        owner = self.owners.get(machine_id)
        if owner is None:
            return None
        now = datetime.now(UTC)
        return {
            "id": machine_id,
            "owner_user_id": owner,
            "first_seen": now,
            "last_seen": now,
            "ref": 1,
        }


def _hub_id() -> str:
    return str(UUID(get_machine_id()))


def _owned_db() -> tuple[_RecordingDB, str, str]:
    hub = _hub_id()
    remote = str(uuid4())
    return _RecordingDB({hub: TEST_USER_ID, remote: TEST_USER_ID}), hub, remote


def _expect_error(machine_id: str, code: str) -> None:
    with pytest.raises(NodeCommandError) as raised:
        require_machine_owned(machine_id)
    assert raised.value.code == code
    assert raised.value.stage == "none"


def test_require_machine_owned() -> None:
    database, hub, remote = _owned_db()
    unknown = str(uuid4())

    require_machine_owned(hub)
    require_machine_owned(unknown)
    assert database.calls == 0

    missing_user = bind_caller(None, database)
    try:
        calls_before = database.calls
        _expect_error(hub, "machine_not_owned")
        _expect_error(remote, "machine_not_owned")
        assert database.calls == calls_before
    finally:
        missing_user.var.reset(missing_user)

    no_database = bind_caller(TEST_USER_ID, None)
    try:
        _expect_error(hub, "machine_not_found")
        _expect_error(remote, "machine_not_found")
    finally:
        no_database.var.reset(no_database)

    owner = bind_caller(TEST_USER_ID, database)
    try:
        require_machine_owned(hub)
        require_machine_owned(remote)
        _expect_error(unknown, "machine_not_found")
    finally:
        owner.var.reset(owner)

    other = bind_caller(OTHER_USER_ID, database)
    try:
        _expect_error(hub, "machine_not_owned")
        _expect_error(remote, "machine_not_owned")
    finally:
        other.var.reset(other)

    assert current_caller() is None


def test_binding_reaches_child_tasks() -> None:
    database, _hub, _remote = _owned_db()

    async def _seen_user() -> str | None:
        scope = current_caller()
        return None if scope is None else scope.user_id

    def _seen_user_sync() -> str | None:
        scope = current_caller()
        return None if scope is None else scope.user_id

    async def _run() -> tuple[str | None, str | None]:
        token = bind_caller(TEST_USER_ID, database)
        try:
            child = asyncio.create_task(_seen_user())
            child_user = await child
            thread_user = await asyncio.to_thread(_seen_user_sync)
            return child_user, thread_user
        finally:
            token.var.reset(token)

    child_user, thread_user = asyncio.run(_run())
    assert child_user == TEST_USER_ID
    assert thread_user == TEST_USER_ID
    assert current_caller() is None
