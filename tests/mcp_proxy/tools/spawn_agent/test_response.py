"""Backend-neutral spawn response and health contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, create_autospec

import pytest

from gobby.mcp_proxy.tools.spawn_agent._health import _terminal_is_live
from gobby.storage.terminals import Terminal
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import TerminalRuntimeRegistry
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_health_uses_runtime_liveness_per_backend() -> None:
    native_row = cast(Terminal, SimpleNamespace(id="terminal-native", backend="native"))
    tmux_row = cast(Terminal, SimpleNamespace(id="terminal-tmux", backend="tmux"))
    native_runtime = create_autospec(NativeTerminalRuntime, instance=True)
    native_runtime.is_live = AsyncMock(return_value=True)
    tmux_runtime = create_autospec(TmuxTerminalRuntime, instance=True)
    tmux_runtime.is_live = AsyncMock(return_value=True)
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.side_effect = [native_runtime, tmux_runtime]

    native_result = await _terminal_is_live(native_row, registry)
    tmux_result = await _terminal_is_live(tmux_row, registry)

    assert native_result == (True, None)
    assert tmux_result == (True, None)
    assert registry.resolve.call_args_list == [call("native"), call("tmux")]
    native_runtime.is_live.assert_awaited_once_with(native_row)
    tmux_runtime.is_live.assert_awaited_once_with(tmux_row)
