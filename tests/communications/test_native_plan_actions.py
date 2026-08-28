from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from gobby.communications import native_plan_actions
from gobby.communications.native_plan_actions import NativePlanActionService

pytestmark = pytest.mark.asyncio

SESSION_ID = "11111111-1111-4111-8111-111111111111"
CODEX_MENU = """\
  Implement this plan?

› 1. Yes, implement this plan          Switch to Default and start coding.
  2. Yes, clear context and implement  Fresh thread with this plan.
  3. No, stay in Plan mode             Continue planning with the model.
  Press enter to confirm or esc to go back
"""


class _FakeTmux:
    def __init__(self, pane_text: str) -> None:
        self.pane_text = pane_text
        self.sent: list[tuple[str, str, bool]] = []

    async def snapshot_lines(self, session_name: str, lines: int = 5) -> str | None:
        assert session_name == "%42"
        assert lines == 30
        return self.pane_text

    async def capture_pane(self, session_name: str, lines: int = 5) -> str | None:
        return await self.snapshot_lines(session_name, lines=lines)

    async def send_keys(
        self,
        session_name: str,
        keys: str,
        *,
        literal: bool = True,
    ) -> bool:
        self.sent.append((session_name, keys, literal))
        return True

    async def dispatch_keys(
        self,
        session_name: str,
        keys: str,
        *,
        literal: bool = True,
    ) -> bool:
        return await self.send_keys(session_name, keys, literal=literal)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: str = "codex",
    pane_text: str = CODEX_MENU,
) -> tuple[NativePlanActionService, _FakeTmux]:
    tmux = _FakeTmux(pane_text)
    monkeypatch.setattr(
        native_plan_actions,
        "manager_for_terminal_context",
        lambda _context: tmux,
    )
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id=SESSION_ID,
        status="paused",
        source=source,
        terminal_context={"tmux_pane": "%42"},
    )
    return NativePlanActionService(session_manager, MagicMock()), tmux


async def test_codex_menu_exposes_all_three_exact_live_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(monkeypatch)

    menu = await service.get_menu(SESSION_ID)

    assert menu is not None
    assert [choice.option for choice in menu.choices] == [1, 2, 3]
    assert menu.choices[0].label.startswith("Yes, implement this plan")
    assert menu.choices[1].label.startswith("Yes, clear context and implement")
    assert menu.choices[2].label.startswith("No, stay in Plan mode")
    assert all(len(choice.label) <= 64 for choice in menu.choices)


async def test_dispatch_revalidates_fingerprint_and_uses_native_codex_digit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, tmux = _service(monkeypatch)
    menu = await service.get_menu(SESSION_ID)
    assert menu is not None

    result = await service.dispatch(
        SESSION_ID,
        option=2,
        expected_fingerprint=menu.fingerprint,
    )

    assert result == "sent"
    assert tmux.sent == [("%42", "2", True)]


async def test_dispatch_rejects_changed_prompt_without_sending_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, tmux = _service(monkeypatch)
    menu = await service.get_menu(SESSION_ID)
    assert menu is not None
    tmux.pane_text = CODEX_MENU.replace("Fresh thread with this plan.", "Changed prompt.")

    result = await service.dispatch(
        SESSION_ID,
        option=2,
        expected_fingerprint=menu.fingerprint,
    )

    assert result == "stale"
    assert tmux.sent == []


async def test_plan_actions_use_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
    from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

    terminal = make_memory_terminal(backend="native")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native")
    runtime.snapshot_text = CODEX_MENU
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id=SESSION_ID,
        status="paused",
        source="codex",
        terminal_id=terminal.id,
    )
    service = NativePlanActionService(
        session_manager,
        MagicMock(),
        terminal_manager=store,
        terminal_runtime_registry=registry,
        write_coordinator=coordinator,
    )
    menu = await service.get_menu(SESSION_ID)
    assert menu is not None
    result = await service.dispatch(
        SESSION_ID,
        option=2,
        expected_fingerprint=menu.fingerprint,
    )
    assert result == "sent"
    assert runtime.write_log


async def test_agy_is_excluded_from_native_plan_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(monkeypatch, source="agy")

    assert await service.get_menu(SESSION_ID) is None
