"""Generated from ``crates/gcore/contracts/tool_loop_limits.v1.json``.

Do not edit by hand; run ``uv run python scripts/generate_tool_loop_limits.py``.
"""

from __future__ import annotations

from typing import TypedDict

TOOL_LOOP_CONTRACT = "gobby.tool_loop_limits"
TOOL_LOOP_CONTRACT_VERSION = 1
TOOL_LOOP_CONFIG_PREFIX = "ai.generation.tool_loop"

DEFAULT_MAX_TURNS: int | None = None
DEFAULT_MAX_TOOL_CALLS = 24
DEFAULT_MAX_BYTES_PER_TOOL_RESULT = 16384
DEFAULT_TOOL_TIMEOUT_SECONDS = 300
DEFAULT_LOOP_TIMEOUT_SECONDS = 1200


class ToolLoopLimitsDict(TypedDict):
    """Complete version-1 wire representation."""

    max_turns: int | None
    max_tool_calls: int
    max_bytes_per_tool_result: int
    tool_timeout_seconds: float
    loop_timeout_seconds: int
