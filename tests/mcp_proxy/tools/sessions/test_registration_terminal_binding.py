"""Existing Codex registrations repair verified native panes without SessionStart."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby import terminal_ownership
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.sessions import create_sessions_router
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
    tmp_path: Path,
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
        if missing_identity and not refusal:
            client = _operator_client(manager, tmp_path)
            response = client.post(
                f"/api/sessions/{session.id}/native-terminal-binding",
                json={"terminal_id": terminal_id},
            )
            assert response.status_code == 200, response.text
            result = response.json()
        else:
            result = await register(
                external_id=session.external_id,
                source="codex",
                machine_id=LOCAL_MACHINE_ID,
                project_id=sample_project["id"],
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
        first_hook_binding = terminals.get(terminal_id)
        assert first_hook_binding is not None
        with (
            patch.object(TerminalManager, "bind_session", wraps=terminals.bind_session) as bind,
            patch.object(TerminalManager, "list_live_by_machine") as scan,
            patch(
                "gobby.terminal_ownership.inspect_foreground_ownership",
                wraps=terminal_ownership.inspect_foreground_ownership,
            ) as inspect,
        ):
            lookup.apply_session_mutations(event, session.id)
        bind.assert_not_called()
        scan.assert_not_called()
        assert inspect.call_count == 1
        second_hook_binding = terminals.get(terminal_id)
        assert second_hook_binding is not None
        assert second_hook_binding.updated_at == first_hook_binding.updated_at
    rebound = terminals.get(terminal_id)
    assert rebound is not None and rebound.session_id == session.id


async def test_mcp_without_managed_principal_cannot_select_terminal() -> None:
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
    # The autouse principal returns None, as front-door agent callers do.
    with session_context_for_test("current-session"):
        with pytest.raises(TypeError, match="native_terminal_id"):
            await register(
                external_id="old-thread",
                source="codex",
                machine_id=LOCAL_MACHINE_ID,
                project_id="test-project",
                native_terminal_id=str(uuid.uuid4()),
            )
    manager.register.assert_not_called()


def _operator_client(manager: SessionManager, tmp_path: Path) -> TestClient:
    key_file = tmp_path / "break_glass"
    key_file.touch(mode=0o600)
    key_file.write_text("test-recovery-value")
    server = MagicMock()
    server.session_manager = manager
    server.auth_service = AuthService(lambda: manager.db, break_glass_file=key_file)
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    app = FastAPI()
    app.include_router(create_sessions_router(server))
    return TestClient(
        app,
        client=("127.0.0.1", 50000),
        headers={"X-Gobby-Break-Glass": "test-recovery-value"},
    )


@pytest.mark.parametrize("holder_status", ["active", "awaiting_handoff", "expired"])
def test_manual_binding_respects_identityless_current_holder(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    holder_status: str,
) -> None:
    manager = SessionManager(temp_db)
    terminals = TerminalManager(temp_db)
    holder = manager.register(
        external_id="holder",
        source="codex",
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
        terminal_context={},
    )
    caller = manager.register(
        external_id="caller",
        source="codex",
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
        terminal_context={},
    )
    pane = terminals.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
        backend="native",
        ownership="gobby",
        spawn_key="holder-pane",
    )
    terminals.promote_to_live(
        pane.id,
        locator={"host_terminal_id": "held"},
        locator_key="native:current:held",
        host_epoch="current",
    )
    temp_db.execute(
        "UPDATE terminals SET process=%s::jsonb WHERE id=%s",
        ('{"pgid": 8000, "start_time": 100.0}', pane.id),
    )
    assert terminals.bind_session(pane.id, holder.id, sample_project["id"]) is not None
    manager.update_status(holder.id, holder_status)
    shell = MagicMock(pid=8000)
    shell.create_time.return_value = 100.0
    shell.terminal.return_value = "/dev/ttys-test"
    seat = MagicMock(pid=8001)
    seat.name.return_value = "codex"
    seat.cmdline.return_value = ["codex"]
    seat.create_time.return_value = 110.0
    seat.parents.return_value = [shell]
    seat.terminal.return_value = "/dev/ttys-test"
    seat.environ.return_value = {"GOBBY_TERMINAL_ID": pane.id}
    client = _operator_client(manager, tmp_path)
    with (
        patch("psutil.Process", side_effect=lambda pid: {8000: shell, 8001: seat}[pid]),
        patch.object(os, "getpgid", return_value=8001),
        patch("gobby.terminal_ownership.foreground_process_group", return_value=8001),
    ):
        response = client.post(
            f"/api/sessions/{caller.id}/native-terminal-binding",
            json={"terminal_id": pane.id},
        )
    assert response.status_code == 200, response.text
    expected = "bound" if holder_status == "expired" else "unbound"
    assert response.json()["terminal_binding"]["status"] == expected
    attached = terminals.get(pane.id)
    assert attached is not None
    assert attached.session_id == (caller.id if holder_status == "expired" else holder.id)


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"Cookie": "gobby_session=agent-cookie"},
        {"Authorization": "Bearer agent-token"},
        {"X-Gobby-Break-Glass": "wrong"},
    ],
)
def test_manual_binding_refuses_nonoperator_credentials(
    temp_db: HubDatabase,
    tmp_path: Path,
    credentials: dict[str, str],
) -> None:
    client = _operator_client(SessionManager(temp_db), tmp_path)
    client.headers.clear()
    response = client.post(
        f"/api/sessions/{uuid.uuid4()}/native-terminal-binding",
        json={"terminal_id": str(uuid.uuid4())},
        headers=credentials,
    )
    assert response.status_code == 403
    assert "operator" in response.json()["detail"].lower()
