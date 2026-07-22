"""
Internal MCP tools for Gobby Agent System.

This module is a compatibility facade. Focused helper modules own the registry
construction and tool implementations, while this facade keeps historic imports
and test patch points stable.
"""

from __future__ import annotations

import asyncio
import logging
import time

from gobby.agents.kill import kill_agent as _kill_agent_process
from gobby.agents.run_completion import complete_and_notify_agent_run
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.mcp_proxy.tools.agent_cancellation import (
    stop_agent_run,
    terminalize_killed_agent_run,
)
from gobby.mcp_proxy.tools.agent_live_activity import (
    overlay_live_activity,
    overlay_runs_live_activity,
)
from gobby.mcp_proxy.tools.agents_payloads import _agent_result_payload
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.agents_termination import (
    _cleanup_terminal_artifacts,
    _complete_self_terminated_run,
    _fire_synthetic_stop,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import AgentRunStatus, LocalAgentRunManager

logger = logging.getLogger(__name__)

_TERMINAL_AGENT_STATUSES = {"success", "error", "timeout", "cancelled"}

__all__ = [
    "AgentRunStatus",
    "InternalToolRegistry",
    "LocalAgentRunManager",
    "_TERMINAL_AGENT_STATUSES",
    "_agent_result_payload",
    "_cleanup_terminal_artifacts",
    "_complete_self_terminated_run",
    "_fire_synthetic_stop",
    "_kill_agent_process",
    "asyncio",
    "cleanup_agent_runtime_state",
    "complete_and_notify_agent_run",
    "create_agents_registry",
    "logger",
    "overlay_live_activity",
    "overlay_runs_live_activity",
    "stop_agent_run",
    "terminalize_killed_agent_run",
    "time",
]
