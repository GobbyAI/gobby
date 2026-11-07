"""MCP send_keys and capture_output are backend-neutral (plan 2.4.6)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.team_communications import RECIPIENT_REFUSED
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import _terminal_send_keys
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.mcp_proxy.tools.sessions._terminal_send_keys_gate import SendKeysRefusal
from gobby.sessions.mailbox import MailboxService
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import Delivered, IndeterminateWrite
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _target_gate_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover backend-neutral delivery; the target gate has its own tests."""

    async def admit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(_terminal_send_keys, "send_keys_target_refusal", admit)


CALLER_REF = "caller-session"
TARGET_REF = "target-session"


@dataclass(frozen=True)
class _Seat:
    """A synthetic seat: its definition's role and admission lists. No role is the operator."""

    role: str | None = None
    capabilities: tuple[str, ...] = ()
    required_senders: tuple[str, ...] = ()


OPERATOR = _Seat()
_HOLDER = _Seat(role="custom-holder", capabilities=("send_reports",))
_FENCED = _Seat(role="fenced-seat", required_senders=("send_fenced",))


def _seat_session(sessions: SessionManager, project_id: str, seat: _Seat) -> str:
    """Register a session seated under a synthetic definition and return its id."""
    name = seat.role or "operator"
    if seat.role is not None:
        definitions = AgentDefinitionManager(sessions.db)
        if definitions.get_by_name(seat.role, project_id) is None:
            definitions.create(
                seat.role,
                {
                    "name": seat.role,
                    "prompts": {"agent": "Hold the assigned seat."},
                    "workflows": {"rule_selectors": {"include": []}},
                    "capabilities": list(seat.capabilities),
                    "message_sender_capabilities": list(seat.required_senders),
                },
                project_id=project_id,
            )
    session = sessions.register(
        external_id=f"{name}-{uuid4()}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project_id,
        title=name,
    )
    if seat.role is not None:
        SessionVariableManager(sessions.db).set_variable(session.id, "_agent_type", seat.role)
    return session.id


def _registered_send_keys(
    temp_db: HubDatabase,
    project_id: str,
    *,
    caller: _Seat = OPERATOR,
    target: _Seat = OPERATOR,
    self_target: bool = False,
    context_only: bool = False,
    backend: Literal["tmux", "native"] = "native",
    tool_name: str = "send_keys",
) -> tuple[Callable[..., Any], FakeRuntime, MemoryTerminalStore, WriteCoordinator, str]:
    sessions = SessionManager(temp_db)
    caller_id = _seat_session(sessions, project_id, caller)
    target_id = caller_id if self_target else _seat_session(sessions, project_id, target)
    terminal = make_memory_terminal(backend=backend)
    terminal.session_id = None if context_only else target_id
    if backend == "tmux":
        terminal.ownership = "external"
    if context_only:
        terminal.project_id = project_id
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend=backend)
    other = FakeRuntime(backend="native" if backend == "tmux" else "tmux")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime, other),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    # Refs resolve to the seated rows, so admission reads each seat's real definition.
    refs = {CALLER_REF: caller_id, TARGET_REF: target_id}
    sessions_by_id = {
        session_id: MagicMock(
            id=session_id,
            project_id=project_id,
            agent_run_id=None,
            session_type="terminal",
            terminal_context={"gobby_terminal_id": terminal.id} if context_only else None,
        )
        for session_id in (caller_id, target_id)
    }
    session_manager = MagicMock()
    session_manager.db = temp_db
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: refs.get(
        ref, ref
    )
    session_manager.get.side_effect = sessions_by_id.get
    tools = InternalToolRegistry(name="gobby-sessions", description="sessions")
    register_terminal_tools(
        tools,
        session_manager,
        temp_db,
        terminal_manager=store,
        write_coordinator=coordinator,
    )
    send = tools.get_tool(tool_name)
    assert send is not None
    return send, runtime, store, coordinator, target_id


@pytest.mark.asyncio
async def test_send_keys_uses_unbound_native_context_id(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send, runtime, _store, _coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"], context_only=True
    )

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        result = await send(session_id="target-session", keys="hello\n", literal=True)

    assert result["success"] is True
    assert runtime.write_log == [("text", "hello\n")]


@pytest.mark.asyncio
async def test_send_and_capture_are_backend_neutral(temp_db: HubDatabase) -> None:
    terminal = make_memory_terminal(backend="native")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native")
    runtime.snapshot_text = "pane"
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    tools = InternalToolRegistry(name="gobby-sessions", description="sessions")
    session_manager = MagicMock()
    register_terminal_tools(
        tools,
        session_manager,
        temp_db,
        terminal_manager=store,
        terminal_runtime_registry=registry,
        write_coordinator=coordinator,
    )
    send = tools.get_tool("send_keys")
    capture = tools.get_tool("capture_output")
    assert send is not None
    assert capture is not None
    assert runtime.backend == "native"


@pytest.mark.asyncio
async def test_send_keys_uses_daemon_origin_and_idempotency(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send, runtime, store, coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"]
    )
    terminal = next(iter(store.rows.values()))
    await coordinator.lease_registry.attach(terminal.id, attachment_id="operator")
    await coordinator.lease_registry.take_control(terminal.id, "operator")
    coordinator_write = AsyncMock(wraps=coordinator.write)

    with (
        patch.object(coordinator, "write", coordinator_write),
        patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value="caller-session",
        ),
    ):
        result = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="request-1",
        )
        replay = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="request-1",
        )

    assert result == {"success": True, "idempotency_key": "request-1"}
    assert replay == result
    assert coordinator_write.await_args is not None
    assert coordinator_write.await_args.args[0].origin == "daemon"
    assert runtime.write_log == [("text", "hello"), ("text", "hello")]


@pytest.mark.asyncio
async def test_send_keys_reaches_an_external_tmux_pane(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send, runtime, store, coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"], backend="tmux"
    )
    terminal = next(iter(store.rows.values()))
    native = coordinator.runtime_for(make_memory_terminal(backend="native"))
    await coordinator.lease_registry.attach(terminal.id, attachment_id="operator")
    await coordinator.lease_registry.take_control(terminal.id, "operator")

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        result = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="external-1",
        )

    assert result == {"success": True, "idempotency_key": "external-1"}
    assert runtime.write_log == [("text", "hello")]
    assert cast(FakeRuntime, native).write_log == []


@pytest.mark.asyncio
async def test_send_keys_idempotency_conflict(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send, runtime, store, _coordinator, target_id = _registered_send_keys(
        temp_db, sample_project["id"]
    )
    runtime.outcome = IndeterminateWrite(detail="reply lost")

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        first = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="request-1",
        )
        conflict = await send(
            session_id="target-session",
            keys="different",
            idempotency_key="request-1",
        )

    assert first["indeterminate"] is True
    assert conflict == {
        "success": False,
        "error": "idempotency key was already used with a different payload",
        "error_code": "idempotency_conflict",
        "idempotency_key": "request-1",
    }
    assert runtime.write_log == [("text", "hello")]
    terminal = next(iter(store.rows.values()))
    entry = terminal.unresolved_writes[f"mcp-send-keys:{target_id}:request-1"]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["payload_fingerprint"])


@pytest.mark.asyncio
async def test_send_keys_idempotency_key_contract(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send, runtime, _store, _coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"]
    )

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        runtime.outcome = IndeterminateWrite(detail="reply lost")
        first = await send(
            session_id="target-session",
            keys="maybe",
            idempotency_key="explicit",
        )
        replay = await send(
            session_id="target-session",
            keys="maybe",
            idempotency_key="explicit",
        )
        assert len(runtime.write_log) == 1

        runtime.outcome = Delivered()
        delivered_once = await send(
            session_id="target-session",
            keys="delivered",
            idempotency_key="reusable",
        )
        delivered_twice = await send(
            session_id="target-session",
            keys="delivered",
            idempotency_key="reusable",
        )
        malformed = await send(
            session_id="target-session",
            keys="blocked",
            idempotency_key="contains whitespace",
        )

        runtime.outcome = IndeterminateWrite(detail="reply lost")
        minted = await send(session_id="target-session", keys="minted")
        minted_replay = await send(
            session_id="target-session",
            keys="minted",
            idempotency_key=minted["idempotency_key"],
        )

    assert first["indeterminate"] is True
    assert replay["indeterminate"] is True
    assert first["idempotency_key"] == replay["idempotency_key"] == "explicit"
    assert delivered_once == {"success": True, "idempotency_key": "reusable"}
    assert delivered_twice == delivered_once
    assert malformed["error_code"] == "invalid_idempotency_key"
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", minted["idempotency_key"])
    assert minted_replay["indeterminate"] is True
    assert minted_replay["idempotency_key"] == minted["idempotency_key"]
    assert runtime.write_log == [
        ("text", "maybe"),
        ("text", "delivered"),
        ("text", "delivered"),
        ("text", "minted"),
    ]


WAKE_LINE = "continue: check your queued Gobby messages and proceed"


@pytest.mark.asyncio
async def test_send_wake_takes_no_text_and_types_the_fixed_line(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send_wake, runtime, _store, _coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"], tool_name="send_wake"
    )

    assert list(inspect.signature(send_wake).parameters) == ["session_id"]
    with patch("gobby.utils.session_context.get_current_session_id", return_value=CALLER_REF):
        with pytest.raises(TypeError):
            await send_wake(session_id=TARGET_REF, keys="rm -rf ~")
        result = await send_wake(session_id=TARGET_REF)

    assert result["success"] is True
    assert runtime.write_log == [("text", f"{WAKE_LINE}\n")]


@pytest.mark.asyncio
async def test_send_wake_refuses_unadmitted_holder_before_any_keys(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send_wake, runtime, store, _coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"], caller=_HOLDER, target=_FENCED, tool_name="send_wake"
    )

    with patch("gobby.utils.session_context.get_current_session_id", return_value=CALLER_REF):
        result = await send_wake(session_id=TARGET_REF)

    assert result["success"] is False
    assert result["error_code"] == RECIPIENT_REFUSED
    assert runtime.write_log == []
    terminal = next(iter(store.rows.values()))
    assert terminal.unresolved_writes == {}


@pytest.mark.asyncio
async def test_send_wake_delivers_to_a_holder_with_the_listed_sender(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    send_wake, runtime, _store, _coordinator, _target = _registered_send_keys(
        temp_db,
        sample_project["id"],
        caller=_Seat(role="custom-holder", capabilities=("send_fenced",)),
        target=_FENCED,
        tool_name="send_wake",
    )

    with patch("gobby.utils.session_context.get_current_session_id", return_value=CALLER_REF):
        result = await send_wake(session_id=TARGET_REF)

    assert result["success"] is True
    assert runtime.write_log == [("text", f"{WAKE_LINE}\n")]


@pytest.mark.asyncio
async def test_send_wake_target_gate_refusal_types_nothing(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    send_wake, runtime, _store, _coordinator, _target = _registered_send_keys(
        temp_db, sample_project["id"], tool_name="send_wake"
    )
    refusal = SendKeysRefusal(
        code="synthetic_target_refusal", message="target refused", recovery="try later"
    )

    async def refuse(*_args: object, **_kwargs: object) -> SendKeysRefusal:
        return refusal

    monkeypatch.setattr(_terminal_send_keys, "send_keys_target_refusal", refuse)
    with patch("gobby.utils.session_context.get_current_session_id", return_value=CALLER_REF):
        result = await send_wake(session_id=TARGET_REF)

    assert result["success"] is False
    assert result["error_code"] == "synthetic_target_refusal"
    assert result["recovery"] == "try later"
    assert runtime.write_log == []


_ADMISSION_CASES = [
    pytest.param(_HOLDER, _FENCED, False, id="holder-lacks-listed-sender"),
    pytest.param(
        _Seat(role="custom-holder", capabilities=("send_fenced",)),
        _FENCED,
        True,
        id="holder-with-listed-sender",
    ),
    pytest.param(_HOLDER, _Seat(role="open-seat"), True, id="target-lists-no-senders"),
    pytest.param(OPERATOR, _FENCED, True, id="operator-terminal-caller"),
    pytest.param(_HOLDER, OPERATOR, True, id="operator-terminal-target"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("caller", "target", "admitted"), _ADMISSION_CASES)
async def test_send_wake_admission_matches_send_message(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caller: _Seat,
    target: _Seat,
    admitted: bool,
) -> None:
    sessions = SessionManager(temp_db)
    project_id = sample_project["id"]
    caller_id = _seat_session(sessions, project_id, caller)
    target_id = _seat_session(sessions, project_id, target)
    mailbox = MailboxService(
        db=temp_db,
        message_manager=InterSessionMessageManager(temp_db),
        session_manager=sessions,
        wake_dispatcher=MagicMock(),
    )

    sent = await mailbox.send(
        from_session_id=caller_id,
        target="session",
        target_id=target_id,
        content="drive",
        wake=False,
    )
    refusal = _terminal_send_keys._wake_admission_refusal(caller_id, target_id, temp_db)

    refused_by_send_message = sent.error_code == RECIPIENT_REFUSED
    refused_by_send_wake = refusal is not None and refusal["error_code"] == RECIPIENT_REFUSED
    assert refused_by_send_message == refused_by_send_wake == (not admitted)
