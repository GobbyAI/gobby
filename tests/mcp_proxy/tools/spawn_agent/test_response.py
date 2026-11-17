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

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_health_uses_native_runtime_liveness() -> None:
    native_row = cast(Terminal, SimpleNamespace(id="terminal-native", backend="native"))
    native_runtime = create_autospec(NativeTerminalRuntime, instance=True)
    native_runtime.is_live = AsyncMock(return_value=True)
    registry = MagicMock(spec=TerminalRuntimeRegistry)
    registry.resolve.return_value = native_runtime

    native_result = await _terminal_is_live(native_row, registry)

    assert native_result == (True, None)
    assert registry.resolve.call_args_list == [call("native")]
    native_runtime.is_live.assert_awaited_once_with(native_row)
