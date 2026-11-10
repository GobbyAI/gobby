"""Declared monitor reporting and plan-consensus workflow effects.

Peer messages carry no task authority or automatic claim-transfer effects.
"""

from __future__ import annotations

import re
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.engine.seat_messages import completion_messages
from gobby.workflows.step_instances import AgentStepInstance

# Instance-owned deduplication; never mirror this into session variables.
SEAT_CONSUMED_KEY = "_seat_consumed_messages"


def _report_matches(content: str, protocol: dict[str, Any]) -> bool:
    prefixes = protocol.get("report_prefixes", [])
    return (
        isinstance(prefixes, list)
        and all(isinstance(prefix, str) for prefix in prefixes)
        and isinstance(protocol.get("report_step"), str)
        and content.startswith(tuple(prefixes))
    )


def _monitor_message(
    db: HubDatabase,
    required_recipients: list[str],
    step: str | None,
    state: dict[str, Any],
    recipient: str,
    content: str,
    sent_at: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    report_step = protocol.get("report_step")
    if not _report_matches(content, protocol):
        return {}
    recipient_session = SessionManager(db).get(recipient)
    if recipient_session is None:
        return {}
    definition = session_agent_definition(db, recipient_session)
    if definition is None or definition[0].name not in required_recipients:
        return {}
    target = definition[0].name
    if step == report_step and content != state.get("_window_report"):
        return {}
    reported = set(state.get("_reported_to") or []) if step == report_step else set()
    reported.add(target)
    return {
        "tick_window": state.get("tick_window") if step == report_step else sent_at,
        "_window_report": content,
        "tick_done": not set(required_recipients).issubset(reported),
        "_reported_to": sorted(reported),
    }


def advance_seat(
    db: HubDatabase,
    session_id: str,
    instance: AgentStepInstance,
    mcp_tool: tuple[str, str],
    variables: dict[str, Any],
    tool_input: dict[str, Any],
    tool_output: Any,
    protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute only effects authorized by the caller's enabled definition."""
    session = SessionManager(db).get(session_id)
    if session is None:
        return {}
    definition = session_agent_definition(db, session)
    if definition is None:
        return {}
    body = definition[0]
    protocol = protocol or {}
    step = instance.current_step
    state = {**variables, **instance.variables}
    if mcp_tool[0] != "gobby-agents":
        return {}
    consumed = list(state.get(SEAT_CONSUMED_KEY) or [])
    monitor_updates: dict[str, Any] = {}
    for message in completion_messages(db, session_id, mcp_tool[1], tool_input, tool_output):
        if message.id in consumed:
            continue
        changes: dict[str, Any] = {}
        if (
            body.monitor_report_recipients
            and message.from_session == session_id
            and _report_matches(message.content, protocol)
        ):
            changes = _monitor_message(
                db,
                body.monitor_report_recipients,
                step,
                state,
                message.to_session,
                message.content,
                message.sent_at.isoformat(),
                protocol,
            )
            if changes:
                monitor_updates.update(changes)
                state.update(changes)
                # A broadcast's recipients share one window even before the step transition.
                step = protocol["report_step"]
                consumed.append(message.id)
            continue
        elif body.holds("track_plan_consensus") and message.from_session == session_id:
            pattern = protocol.get("consensus_pattern")
            if not isinstance(pattern, str):
                continue
            match = re.match(pattern, message.content.partition("\n")[0])
            if match and len(match.groups()) < 2:
                raise ValueError("consensus_pattern must capture an event and a commit SHA")
            if (
                match
                and step == protocol.get("consensus_step")
                and match[1] == protocol.get("consensus_event")
            ):
                changes = {"consensus": True, "consensus_sha": match[2], "bounced": False}
            elif (
                match
                and step == protocol.get("withdrawal_step")
                and match[1] == protocol.get("withdrawal_event")
            ):
                if not state.get("consensus_sha") or match[2] == state.get("consensus_sha"):
                    changes = {"bounced": True, "consensus": False}
        if changes:
            return {
                **monitor_updates,
                **changes,
                SEAT_CONSUMED_KEY: (consumed + [message.id])[-64:],
            }
    return {**monitor_updates, SEAT_CONSUMED_KEY: consumed[-64:]} if monitor_updates else {}
