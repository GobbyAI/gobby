"""Existing Codex registrations repair verified native panes without SessionStart."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.storage.attention import AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.utils.session_context import (
    reset_request_principal,
    session_context_for_test,
    set_request_principal,
)
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _local_machine() -> Iterator[None]:
    async def operator() -> None:
        return None

    token = set_request_principal(operator)
    try:
        with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
            yield
    finally:
        reset_request_principal(token)


@pytest.mark.parametrize(
    "missing_identity,refusal",
    [
        (False, None),
        (True, None),
        (False, "recycled_cli"),
        (False, "recycled_shell"),
        (False, "background"),
        (False, "wrong_pane"),
        (False, "shared_host"),
        (False, "stale_epoch"),
        (False, "competing_thread"),
        (False, "live_holder"),
        (False, "waiting_holder"),
        (False, "nonfinite"),
        (True, "missing_proof"),
    ],
)
async def test_register_repairs_existing_unbound_native_pane(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    missing_identity: bool,
    refusal: str | None,
) -> None:
    manager = SessionManager(temp_db)
    terminals = TerminalManager(temp_db)
    terminal_id = str(uuid.uuid4())
    pane = terminals.create_pending(
        terminal_id=terminal_id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
    )
    terminals.promote_to_live(
        pane.id,
        locator={"host_terminal_id": "ht-test"},
        locator_key="native:current:ht-test",
        host_epoch="current",
    )
    temp_db.execute(
        "UPDATE terminals SET process=%s::jsonb WHERE id=%s",
        ('{"pgid": 8000, "start_time": 100.0}', pane.id),
    )
    session = manager.register(
        external_id="01a15107-eb82-73b2-aea2-ab4077886b1d",
        source="codex",
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
        terminal_context={}
        if missing_identity
        else {"parent_pid": 8001, "parent_create_time": 110.0},
        title="Orchestrator",
    )
    shell = MagicMock(pid=8000)
    shell.create_time.return_value = 100.0
    shell.terminal.return_value = "/dev/ttys-test"
    seat = MagicMock(pid=8001)
    seat.create_time.return_value = 110.0
    seat.name.return_value = "codex"
    seat.cmdline.return_value = ["codex", "--yolo"]
    seat.parents.return_value = [shell]
    seat.terminal.return_value = "/dev/ttys-test"
    seat.environ.return_value = {"GOBBY_TERMINAL_ID": terminal_id, "GOBBY_PANE_REF": "0:0:0:0"}
    seat.cwd.return_value = "/project"
    shell.children.return_value = [seat]
    host = MagicMock(pid=8002)
    host.name.return_value = "codex"
    host.cmdline.return_value = ["codex", "app-server", "--managed-daemon"]
    host.create_time.return_value = 120.0
    holder = MagicMock(pid=8003)
    holder.create_time.return_value = 130.0
    if refusal == "recycled_cli":
        seat.create_time.return_value = 210.0
    elif refusal == "recycled_shell":
        shell.create_time.return_value = 200.0
    elif refusal == "wrong_pane":
        seat.environ.return_value = {"GOBBY_TERMINAL_ID": str(uuid.uuid4())}
    elif refusal == "nonfinite":
        manager.backfill_terminal_context(session.id, {"parent_create_time": "nan"})
    elif refusal in ("shared_host", "stale_epoch"):
        manager.backfill_terminal_context(
            session.id,
            {
                "parent_pid": 8002 if refusal == "shared_host" else 8001,
                "parent_create_time": 120.0 if refusal == "shared_host" else 110.0,
                "gobby_host_epoch": "old" if refusal == "stale_epoch" else "current",
            },
        )
    elif refusal in ("competing_thread", "live_holder", "waiting_holder"):
        other = manager.register(
            external_id="other-thread",
            source="codex",
            machine_id=LOCAL_MACHINE_ID,
            project_id=sample_project["id"],
            terminal_context={
                "parent_pid": 8001 if refusal == "competing_thread" else 8003,
                "parent_create_time": 110.25 if refusal == "competing_thread" else 130.0,
            },
        )
        if refusal in ("live_holder", "waiting_holder"):
            assert terminals.bind_session(terminal_id, other.id, sample_project["id"]) is not None
            if refusal == "waiting_holder":
                manager.update_status(other.id, "awaiting_handoff")
    register = create_session_messages_registry(session_manager=manager).get_tool(
        "register_session"
    )
    assert register is not None

    with (
        session_context_for_test(session.id),
        patch(
            "psutil.Process",
            side_effect=lambda pid: {8000: shell, 8001: seat, 8002: host, 8003: holder}[pid],
        ),
        patch.object(os, "getpgid", return_value=8004 if refusal == "background" else 8001),
        patch("gobby.terminal_ownership.foreground_process_group", return_value=8001),
    ):
        result = await register(
            external_id=session.external_id,
            source="codex",
            machine_id=LOCAL_MACHINE_ID,
            project_id=sample_project["id"],
            native_terminal_id=terminal_id if missing_identity and not refusal else None,
        )
        if refusal:
            assert result["terminal_binding"]["status"] == "unbound"
            assert result["terminal_binding"]["reason"]
            unbound = terminals.get(terminal_id)
            assert unbound is not None and unbound.session_id != session.id
            return
        assert result["terminal_binding"]["status"] == "bound"
        repeated = await register(
            external_id=session.external_id,
            source="codex",
            machine_id=LOCAL_MACHINE_ID,
            project_id=sample_project["id"],
        )
    assert repeated["session_id"] == session.id
    attached = terminals.get(terminal_id)
    assert attached is not None and attached.session_id == session.id
    refreshed = manager.get(session.id)
    assert refreshed is not None and refreshed.title == "Orchestrator"
    assert refreshed.terminal_context is not None
    assert refreshed.terminal_context["gobby_terminal_id"] == terminal_id
    roster = AttentionStateManager(temp_db).load_roster_rows(live_session_statuses=["active"])
    row = next(row for row in roster if row.session_id == session.id)
    assert row.terminal is not None and row.terminal.id == terminal_id

    # In-app /resume never adds the old thread to argv. Replaying a shared-host hook
    # must revalidate the saved seat and rebind the released pane, without SessionStart.
    terminals.release_session(terminal_id, session.id)
    lookup = SessionLookupService(
        cast(HookSessionManager, manager),
        MagicMock(),
        MagicMock(),
        lambda *_: sample_project["id"],
        logging.getLogger(__name__),
    )
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=session.external_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
        cwd="/project",
        data={"terminal_context": {"parent_pid": 8002}},
    )
    with (
        patch("psutil.Process", side_effect=lambda pid: {8000: shell, 8001: seat, 8002: host}[pid]),
        patch("psutil.process_iter", return_value=[]),
        patch.object(os, "getpgid", return_value=8001),
        patch("gobby.terminal_ownership.foreground_process_group", return_value=8001),
    ):
        lookup.apply_session_mutations(event, session.id)
    rebound = terminals.get(terminal_id)
    assert rebound is not None and rebound.session_id == session.id


async def test_terminal_selector_requires_operator_authentication() -> None:
    manager = MagicMock()
    manager.resolve_session_reference.return_value = "current-session"
    manager.get.return_value = MagicMock(
        id="current-session",
        source="codex",
        external_id="old-thread",
        machine_id=LOCAL_MACHINE_ID,
        project_id="test-project",
    )
    register = create_session_messages_registry(session_manager=manager).get_tool(
        "register_session"
    )
    assert register is not None

    async def unauthenticated() -> Literal[False]:
        return False

    token = set_request_principal(unauthenticated)
    try:
        with session_context_for_test("current-session"):
            result = await register(
                external_id="old-thread",
                source="codex",
                machine_id=LOCAL_MACHINE_ID,
                project_id="test-project",
                native_terminal_id=str(uuid.uuid4()),
            )
        assert result["error_code"] == "operator_auth_required"
        manager.register.assert_not_called()
    finally:
        reset_request_principal(token)


@pytest.mark.parametrize(
    "source,external_id", [("grok", "old-thread"), ("codex", "another-thread")]
)
async def test_operator_terminal_selector_cannot_change_caller_identity(
    source: str,
    external_id: str,
) -> None:
    manager = MagicMock()
    manager.resolve_session_reference.return_value = "current-session"
    manager.get.return_value = MagicMock(
        id="current-session",
        source="codex",
        external_id="old-thread",
        machine_id=LOCAL_MACHINE_ID,
        project_id="test-project",
    )
    register = create_session_messages_registry(session_manager=manager).get_tool(
        "register_session"
    )
    assert register is not None
    with session_context_for_test("current-session"):
        result = await register(
            external_id=external_id,
            source=source,
            machine_id=LOCAL_MACHINE_ID,
            project_id="test-project",
            native_terminal_id=str(uuid.uuid4()),
        )
    assert result["error_code"] == "identity_mismatch"
    manager.register.assert_not_called()
