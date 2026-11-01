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


def _monitor_message(
    db: HubDatabase,
    required_recipients: list[str],
    step: str | None,
    state: dict[str, Any],
    recipient: str,
    content: str,
    sent_at: str,
) -> dict[str, Any]:
    if not (content.startswith("Systems nominal") or content.startswith("EVENT=ALARM ")):
        return {}
    recipient_session = SessionManager(db).get(recipient)
    if recipient_session is None:
        return {}
    definition = session_agent_definition(db, recipient_session)
    if definition is None or definition[0].name not in required_recipients:
        return {}
    target = definition[0].name
    if step == "report" and content != state.get("_window_report"):
        return {}
    reported = set(state.get("_reported_to") or []) if step == "report" else set()
    reported.add(target)
    return {
        "tick_window": state.get("tick_window") if step == "report" else sent_at,
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
) -> dict[str, Any]:
    """Compute only effects authorized by the caller's enabled definition."""
    session = SessionManager(db).get(session_id)
    if session is None:
        return {}
    definition = session_agent_definition(db, session)
    if definition is None:
        return {}
    body = definition[0]
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
            and message.content.startswith(("Systems nominal", "EVENT=ALARM "))
        ):
            changes = _monitor_message(
                db,
                body.monitor_report_recipients,
                step,
                state,
                message.to_session,
                message.content,
                message.sent_at.isoformat(),
            )
            if changes:
                monitor_updates.update(changes)
                state.update(changes)
                # A broadcast's recipients share one window even before the step transition.
                step = "report"
                consumed.append(message.id)
            continue
        elif "track_plan_consensus" in body.capabilities and message.from_session == session_id:
            match = re.match(
                r"EVENT=(CONSENSUS|CONSENSUS_WITHDRAWN) SHA=([0-9a-f]{40})(?:\s|$)",
                message.content.partition("\n")[0],
            )
            if match and step == "review" and match[1] == "CONSENSUS":
                changes = {"consensus": True, "consensus_sha": match[2], "bounced": False}
            elif match and step == "stamp" and match[1] == "CONSENSUS_WITHDRAWN":
                if not state.get("consensus_sha") or match[2] == state.get("consensus_sha"):
                    changes = {"bounced": True, "consensus": False}
        if changes:
            return {
                **monitor_updates,
                **changes,
                SEAT_CONSUMED_KEY: (consumed + [message.id])[-64:],
            }
    return {**monitor_updates, SEAT_CONSUMED_KEY: consumed[-64:]} if monitor_updates else {}
