"""Agent run result payloads for get_agent_result and wait_for_agent."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.agent_live_output import live_output_reference
from gobby.mcp_proxy.tools.agents_payloads import _agent_result_payload
from gobby.sessions.handoff_records import get_agent_end_handoff

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

DIRTY_PATHS_UNSET = object()


async def agent_result_payload(
    db: HubDatabase | None,
    run: AgentRun,
    *,
    include_prompt: bool = False,
    dirty_paths: list[str] | None | object = DIRTY_PATHS_UNSET,
) -> dict[str, Any]:
    """Build a run's result payload on a worker thread.

    The final-handoff read and the sandbox violation count both block (#23279).
    """
    return await asyncio.to_thread(
        _build_result_payload, db, run, include_prompt=include_prompt, dirty_paths=dirty_paths
    )


def _build_result_payload(
    db: HubDatabase | None,
    run: AgentRun,
    *,
    include_prompt: bool,
    dirty_paths: list[str] | None | object,
) -> dict[str, Any]:
    try:
        handoff = get_agent_end_handoff(db, run.id) if db is not None else None
    except Exception:
        logger.warning("Failed to read final handoff for agent run %s", run.id, exc_info=True)
        handoff = None
    kwargs: dict[str, Any] = {
        "include_prompt": include_prompt,
        "authoritative_result": handoff.payload.rendered_markdown if handoff else None,
    }
    if dirty_paths is not DIRTY_PATHS_UNSET:
        kwargs["dirty_paths"] = dirty_paths
    payload = _agent_result_payload(run, **kwargs)
    payload["live_output"] = live_output_reference(run)
    return payload
