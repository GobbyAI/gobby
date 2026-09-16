"""Typed refusal for durable waits a headless spawned run cannot survive.

``grok --single`` and ``droid exec`` read nothing from their terminal and exit when
the turn yields, so a tool whose contract is "register a durable wait, then end the
turn" kills the run: the process is gone before the wake payload exists, and the
daemon sees a child that ended mid-workflow (#22367). Resuming such a run would mean
respawning the CLI, so these tools refuse the wait instead and tell the worker to
coordinate with a durable message inside the turn it already holds.
"""

from __future__ import annotations

from typing import Any

from gobby.agents.provider_capabilities import provider_capabilities

HEADLESS_WAIT_ERROR_CODE = "headless_agent_run"
HEADLESS_WAIT_GUIDANCE = (
    "Do not call a wait_for_* tool again in this run. Send the other session a durable "
    "message with gobby-agents:send_message, keep working in this turn, and read any reply "
    "from a later tool result."
)


def headless_wait_refusal(
    *,
    agent_run_manager: Any,
    session_id: str | None,
    tool_name: str,
) -> dict[str, Any] | None:
    """Return a typed refusal when this session's run cannot survive a turn yield.

    ``None`` means the caller may register the wait: the session is not a spawned
    run, or its provider keeps reading its terminal across a yielded turn.
    """
    if session_id is None or agent_run_manager is None:
        return None
    try:
        agent_run = agent_run_manager.get_by_session(session_id)
    except Exception:
        # The wait is the risky operation here; an unreadable run is not.
        return None
    provider = getattr(agent_run, "provider", None)
    if not isinstance(provider, str) or not provider_capabilities(provider).headless_spawn:
        return None
    return {
        "success": False,
        "error": (
            f"{tool_name} ends the turn to wait, and {provider} agent runs are headless: "
            "the CLI exits when the turn yields, so the wake payload would have no process "
            "to resume."
        ),
        "error_code": HEADLESS_WAIT_ERROR_CODE,
        "retry_guidance": HEADLESS_WAIT_GUIDANCE,
    }
