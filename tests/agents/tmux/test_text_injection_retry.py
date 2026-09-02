"""Regressions for retrying a timed-out trailing tmux Enter."""

import logging
from unittest.mock import AsyncMock, call

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.text_injection import TmuxTextInjectionTimeout
from gobby.terminals.pane_io import TmuxPaneIO


def _enter_timeout() -> TmuxTextInjectionTimeout:
    return TmuxTextInjectionTimeout(
        command=("tmux", "send-keys", "-t", "%12", "Enter"),
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_enter_timeout_retries_once_without_repasting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paste = AsyncMock()
    enter = AsyncMock(side_effect=[_enter_timeout(), None])
    sleep = AsyncMock()
    monkeypatch.setattr(
        "gobby.agents.tmux.text_injection.paste_literal_text_to_tmux_target",
        paste,
    )
    monkeypatch.setattr(
        "gobby.agents.tmux.text_injection.send_enter_key_to_tmux_target",
        enter,
    )
    monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

    manager = TmuxSessionManager()
    assert await manager.send_keys("%12", "/compact\n") is True

    paste.assert_awaited_once()
    assert enter.await_count == 2
    assert sleep.await_args_list == [call(1.0), call(0.25)]


@pytest.mark.asyncio
async def test_double_enter_timeout_returns_false_with_caller_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paste = AsyncMock()
    enter = AsyncMock(side_effect=[_enter_timeout(), _enter_timeout()])
    sleep = AsyncMock()
    monkeypatch.setattr(
        "gobby.agents.tmux.text_injection.paste_literal_text_to_tmux_target",
        paste,
    )
    monkeypatch.setattr(
        "gobby.agents.tmux.text_injection.send_enter_key_to_tmux_target",
        enter,
    )
    monkeypatch.setattr("gobby.agents.tmux.text_injection.asyncio.sleep", sleep)

    with caplog.at_level(logging.WARNING):
        ok, reason = await TmuxPaneIO(TmuxSessionManager(), "%12").type_text("/compact\n")

    assert ok is False
    assert reason == "tmux send-keys returned false while typing text to %12"
    paste.assert_awaited_once()
    assert enter.await_count == 2
    assert "%12" in caplog.text
