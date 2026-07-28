from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

AsyncTool = Callable[..., Awaitable[Any]]


def async_tool_capture_mock() -> tuple[MagicMock, dict[str, AsyncTool]]:
    captured: dict[str, AsyncTool] = {}
    mcp = MagicMock()

    def tool(
        name: str | None = None,
        **_kwargs: Any,
    ) -> Callable[[AsyncTool], AsyncTool]:
        def register(func: AsyncTool) -> AsyncTool:
            captured[name or func.__name__] = func
            return func

        return register

    mcp.tool.side_effect = tool
    return mcp, captured
