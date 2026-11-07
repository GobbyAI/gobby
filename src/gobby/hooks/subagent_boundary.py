"""Recognize context-boundary hooks that an in-process Claude subagent issues.

Claude Code subagents share the parent's session id, so a subagent's own
compaction arrives as the parent's SessionStart, PreCompact or PostCompact.
Process-identity binding cannot tell them apart; the payload can.
"""

from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource

_CONTEXT_BOUNDARY_EVENTS = frozenset(
    {HookEventType.SESSION_START, HookEventType.PRE_COMPACT, HookEventType.POST_COMPACT}
)


def subagent_payload_signal(data: Mapping[str, Any]) -> str | None:
    """Name the payload field proving a hook fired inside a Claude subagent, else ``None``.

    ``agent_id`` marks a hook fired inside a subagent, and a subagent's transcript
    lives under ``<session>/subagents/``; a main conversation has neither.
    """
    if data.get("agent_id"):
        return "agent_id"
    transcript = data.get("transcript_path")
    if isinstance(transcript, str) and "subagents" in PurePath(transcript).parts:
        return "transcript_path"
    return None


def subagent_boundary_signal(event: HookEvent) -> str | None:
    """Name the payload field proving a Claude subagent issued this boundary, else ``None``."""
    if event.source != SessionSource.CLAUDE or event.event_type not in _CONTEXT_BOUNDARY_EVENTS:
        return None
    return subagent_payload_signal(event.data)
