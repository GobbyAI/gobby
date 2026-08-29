"""Durable context delivery for Grok's active hook channels."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from gobby.cli.utils import get_gobby_home
from gobby.hooks.envelope_dedupe import envelope_terminal_response
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.pending_messages import render_pending_messages
from gobby.hooks.session_types import HookSessionManager, has_prior_session_activity
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.storage.inter_session_messages import InterSessionMessageManager

logger = logging.getLogger(__name__)

BRIEFING_VARIABLE = "grok_pending_briefing"
TURN_CONTEXT_VARIABLE = "grok_pending_turn_context"
DELIVERY_VARIABLE = "grok_pending_delivery"

BRIEFING_MAX_COMPONENTS = 64
P2P_MAX_PER_SELECTION = 16
TURN_CONTEXT_MAX_COMPONENTS = 32
TURN_CONTEXT_MAX_TOTAL_BYTES = 16_384
TURN_CONTEXT_MAX_COMPONENT_BYTES = 8_192
GROK_DELIVERY_RETENTION_SECONDS = 3_600.0


class PendingContextComponent(TypedDict):
    """One independently deduplicated pending context fragment."""

    id: str
    text: str
    message_ids: list[str]


class PendingContextHandler(Protocol):
    """Hook-manager surface required by pending-context operations."""

    _session_manager: HookSessionManager
    _inter_session_msg_manager: InterSessionMessageManager | None


class PendingDelivery(TypedDict):
    """Briefing components claimed by one durable hook envelope."""

    envelope_id: str
    components: list[PendingContextComponent]


@dataclass(frozen=True)
class _FlushPlan:
    kind: Literal["pretool_new", "pretool_gate", "stop_briefing", "stop_turn", "drop"]
    briefing: str | None = None
    turn_context: str | None = None


def _platform_session_id(event: HookEvent) -> str | None:
    value = event.metadata.get("_platform_session_id")
    return value if isinstance(value, str) and value else None


def _source_envelope_id(event: HookEvent) -> str | None:
    value = event.data.get("source_event_id")
    return value if isinstance(value, str) and value else None


def _response_text(response: HookResponse) -> str | None:
    parts = [
        value.strip()
        for value in (response.context, response.system_message)
        if isinstance(value, str) and value.strip()
    ]
    return "\n\n".join(parts) or None


def _components(value: object) -> list[PendingContextComponent]:
    if not isinstance(value, list):
        return []
    result: list[PendingContextComponent] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        component_id = item.get("id")
        text = item.get("text")
        raw_message_ids = item.get("message_ids", [])
        if not isinstance(component_id, str) or not isinstance(text, str):
            continue
        if not isinstance(raw_message_ids, list) or not all(
            isinstance(message_id, str) for message_id in raw_message_ids
        ):
            continue
        result.append(
            {
                "id": component_id,
                "text": text,
                "message_ids": list(raw_message_ids),
            }
        )
    return result


def _delivery(value: object) -> PendingDelivery | None:
    if not isinstance(value, dict):
        return None
    envelope_id = value.get("envelope_id")
    components = _components(value.get("components"))
    if not isinstance(envelope_id, str) or not envelope_id or not components:
        return None
    return {"envelope_id": envelope_id, "components": components}


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _next_context_id(components: list[PendingContextComponent], envelope_id: str) -> str:
    prefix = f"ctx:{envelope_id}:"
    indices = [
        int(component["id"][len(prefix) :])
        for component in components
        if component["id"].startswith(prefix) and component["id"][len(prefix) :].isdigit()
    ]
    return f"{prefix}{max(indices, default=0) + 1}"


def _briefing_component_id(event: HookEvent, session_id: str) -> str:
    envelope_id = _source_envelope_id(event) or "direct"
    if event.metadata.get("_session_just_materialized"):
        return f"startup:{session_id}"
    if event.event_type == HookEventType.SESSION_START:
        return f"session_start:{envelope_id}"
    return f"turn:{envelope_id}"


def _component_text(components: Sequence[PendingContextComponent]) -> str | None:
    return "\n\n".join(component["text"] for component in components if component["text"]) or None


def _inbox_path(envelope_id: str) -> Path:
    return get_gobby_home() / "hooks" / "inbox" / f"{envelope_id}.json"


def mark_briefing_turn(handler: PendingContextHandler, event: HookEvent) -> None:
    """Classify first and continuation Grok prompts before rules render context."""
    if event.source != SessionSource.GROK or event.event_type != HookEventType.BEFORE_AGENT:
        return
    session_id = _platform_session_id(event)
    if session_id is None:
        return
    session = handler._session_manager.get(session_id)
    if not has_prior_session_activity(session):
        event.metadata["_grok_briefing_turn"] = True


def clear_queued_context(session_manager: object | None, session_id: str | None) -> None:
    """Drop undelivered Grok briefing, turn-context, and in-flight delivery.

    Compact and session-start start a new context epoch. Stale injects — especially
    strong compact-pressure copy queued before occupancy dropped — must not flush
    as PreToolUse denies on the continuation.
    """
    if session_manager is None or not session_id:
        return
    db = getattr(session_manager, "db", None)
    if db is None:
        return

    def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
        changed = False
        if _components(variables.get(BRIEFING_VARIABLE)):
            variables[BRIEFING_VARIABLE] = []
            changed = True
        if _components(variables.get(TURN_CONTEXT_VARIABLE)):
            variables[TURN_CONTEXT_VARIABLE] = []
            changed = True
        if _delivery(variables.get(DELIVERY_VARIABLE)) is not None:
            variables[DELIVERY_VARIABLE] = None
            changed = True
        return None, changed

    try:
        SessionVariableManager(db)._mutate_variables(session_id, mutate)
    except Exception:
        logger.debug(
            "Skipping queued Grok context clear: session=%s",
            session_id,
            exc_info=True,
        )


def stash_response(
    handler: PendingContextHandler,
    event: HookEvent,
    response: HookResponse,
) -> None:
    """Move passive Grok response text into its durable pending buffers."""
    if event.source != SessionSource.GROK:
        return
    text = _response_text(response)
    session_id = _platform_session_id(event)
    if text is None or session_id is None:
        return

    variable_manager = SessionVariableManager(handler._session_manager.db)
    is_briefing = bool(
        event.event_type == HookEventType.SESSION_START
        or event.metadata.get("_session_just_materialized")
        or event.metadata.get("_grok_briefing_turn")
    )

    def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
        variable = BRIEFING_VARIABLE if is_briefing else TURN_CONTEXT_VARIABLE
        components = _components(variables.get(variable))
        if is_briefing:
            component_id = _briefing_component_id(event, session_id)
        else:
            component_id = _next_context_id(
                components,
                _source_envelope_id(event) or "direct",
            )
        if any(component["id"] == component_id for component in components):
            return None, False

        component: PendingContextComponent = {
            "id": component_id,
            "text": text,
            "message_ids": [],
        }
        if is_briefing:
            if len(components) >= BRIEFING_MAX_COMPONENTS:
                logger.error(
                    "Dropping Grok briefing component after defensive cap: session=%s id=%s",
                    session_id,
                    component_id,
                )
                return None, False
            components.append(component)
        else:
            if _serialized_size(component) > TURN_CONTEXT_MAX_COMPONENT_BYTES:
                logger.debug(
                    "Dropping oversized Grok turn-context component: session=%s id=%s",
                    session_id,
                    component_id,
                )
                return None, False
            components.append(component)
            while (
                len(components) > TURN_CONTEXT_MAX_COMPONENTS
                or _serialized_size(components) > TURN_CONTEXT_MAX_TOTAL_BYTES
            ):
                dropped = components.pop(0)
                logger.debug(
                    "Dropping oldest Grok turn-context component: session=%s id=%s",
                    session_id,
                    dropped["id"],
                )
        variables[variable] = components
        return None, True

    variable_manager._mutate_variables(session_id, mutate)
    response.context = None
    response.system_message = None


def enqueue_pending_messages(
    session_manager: HookSessionManager,
    session_id: str,
    messages: Sequence[Any],
    resolve_sender: Callable[[str | None], str],
) -> None:
    """Queue bounded inter-session messages without acknowledging delivery."""
    # Storage order; the rest waits for the next selection after acknowledgment.
    messages = list(messages)[:P2P_MAX_PER_SELECTION]
    rendered = render_pending_messages(messages, resolve_sender=resolve_sender)
    represented_ids = set(rendered.represented_message_ids)
    if not represented_ids:
        return

    components: list[PendingContextComponent] = []
    for message in messages:
        message_id = str(getattr(message, "id", "") or "")
        if message_id not in represented_ids:
            continue
        single = render_pending_messages([message], resolve_sender=resolve_sender)
        if single.context:
            components.append(
                {
                    "id": f"p2p:{message_id}",
                    "text": single.context,
                    "message_ids": [message_id],
                }
            )
    if not components:
        return

    def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
        queued = _components(variables.get(BRIEFING_VARIABLE))
        claimed = _delivery(variables.get(DELIVERY_VARIABLE))
        known_ids = {component["id"] for component in queued}
        if claimed:
            known_ids.update(component["id"] for component in claimed["components"])
        additions = [component for component in components if component["id"] not in known_ids]
        if not additions:
            return None, False
        available = BRIEFING_MAX_COMPONENTS - len(queued)
        if available <= 0:
            logger.error(
                "Dropping Grok message components after defensive briefing cap: session=%s",
                session_id,
            )
            return None, False
        if len(additions) > available:
            logger.error(
                "Dropping %s Grok message components after defensive briefing cap: session=%s",
                len(additions) - available,
                session_id,
            )
        queued.extend(additions[:available])
        variables[BRIEFING_VARIABLE] = queued
        return None, True

    SessionVariableManager(session_manager.db)._mutate_variables(session_id, mutate)


def settle_delivery(handler: PendingContextHandler, event: HookEvent) -> None:
    """Settle a previous envelope claim before processing the current hook."""
    if event.source != SessionSource.GROK:
        return
    session_id = _platform_session_id(event)
    if session_id is None:
        return
    current_envelope_id = _source_envelope_id(event)

    def mutate(variables: dict[str, Any]) -> tuple[list[str], bool]:
        delivery = _delivery(variables.get(DELIVERY_VARIABLE))
        if delivery is None or delivery["envelope_id"] == current_envelope_id:
            return [], False

        claimed = delivery["components"]
        path = _inbox_path(delivery["envelope_id"])
        try:
            path.unlink()
        except FileNotFoundError:
            variables.pop(DELIVERY_VARIABLE, None)
            message_ids = [
                message_id for component in claimed for message_id in component["message_ids"]
            ]
            return message_ids, True
        except OSError:
            logger.warning(
                "Failed to reclaim undelivered Grok envelope %s",
                path,
                exc_info=True,
            )
            return [], False

        queued = _components(variables.get(BRIEFING_VARIABLE))
        claimed_ids = {component["id"] for component in claimed}
        variables[BRIEFING_VARIABLE] = claimed + [
            component for component in queued if component["id"] not in claimed_ids
        ]
        variables.pop(DELIVERY_VARIABLE, None)
        return [], True

    message_ids = SessionVariableManager(handler._session_manager.db)._mutate_variables(
        session_id,
        mutate,
    )
    if not message_ids or handler._inter_session_msg_manager is None:
        return
    try:
        handler._inter_session_msg_manager.mark_delivered_batch(message_ids, session_id)
    except Exception:
        logger.warning(
            "Failed to acknowledge Grok inter-session messages; they remain selectable",
            exc_info=True,
        )


def _flush_plan(
    variables: dict[str, Any],
    event: HookEvent,
    response: HookResponse,
) -> tuple[_FlushPlan | None, bool]:
    if _delivery(variables.get(DELIVERY_VARIABLE)) is not None:
        return None, False

    briefing = _components(variables.get(BRIEFING_VARIABLE))
    turn_context = _components(variables.get(TURN_CONTEXT_VARIABLE))
    is_pretool = event.event_type == HookEventType.BEFORE_TOOL
    is_stop = event.event_type in {HookEventType.STOP, HookEventType.SUBAGENT_STOP}
    if not is_pretool and not is_stop:
        return None, False

    # Grok reads only the top-level decision; a bare permission_decision is not a gate.
    real_gate = response.decision in {"block", "deny"}
    envelope_id = _source_envelope_id(event)
    marker_absent = bool(envelope_id and envelope_terminal_response(envelope_id) is None)

    if is_pretool:
        if not briefing and not (real_gate and turn_context):
            return None, False
        if not marker_absent or envelope_id is None:
            return None, False
        if briefing:
            variables[DELIVERY_VARIABLE] = {
                "envelope_id": envelope_id,
                "components": briefing,
            }
            variables[BRIEFING_VARIABLE] = []
        if real_gate:
            variables[TURN_CONTEXT_VARIABLE] = []
            return (
                _FlushPlan(
                    "pretool_gate",
                    briefing=_component_text(briefing),
                    turn_context=_component_text(turn_context),
                ),
                True,
            )
        return _FlushPlan("pretool_new", briefing=_component_text(briefing)), True

    if briefing:
        if not marker_absent or envelope_id is None:
            return None, False
        variables[DELIVERY_VARIABLE] = {
            "envelope_id": envelope_id,
            "components": briefing,
        }
        variables[BRIEFING_VARIABLE] = []
        variables[TURN_CONTEXT_VARIABLE] = []
        return (
            _FlushPlan(
                "stop_briefing",
                briefing=_component_text(briefing),
                turn_context=_component_text(turn_context),
            ),
            True,
        )

    if not turn_context:
        return None, False
    variables[TURN_CONTEXT_VARIABLE] = []
    if real_gate:
        return _FlushPlan("stop_turn", turn_context=_component_text(turn_context)), True
    return _FlushPlan("drop"), True


def flush_response(
    handler: PendingContextHandler,
    event: HookEvent,
    response: HookResponse,
) -> None:
    """Deliver pending Grok context through PreToolUse or Stop gates."""
    if event.source != SessionSource.GROK:
        return
    session_id = _platform_session_id(event)
    if session_id is None:
        return
    plan = SessionVariableManager(handler._session_manager.db)._mutate_variables(
        session_id,
        lambda variables: _flush_plan(variables, event, response),
    )
    if plan is None or plan.kind == "drop":
        if plan is not None:
            logger.debug("Dropping Grok turn-context on allowing Stop: session=%s", session_id)
        return

    if plan.kind == "pretool_new":
        response.decision = "deny"
        response.reason = f"{plan.briefing}\n\nRetry the same tool call."
        return
    if plan.kind == "pretool_gate":
        response.reason = "\n\n".join(
            part
            for part in (plan.briefing, plan.turn_context, response.reason)
            if isinstance(part, str) and part
        )
        return
    if plan.kind == "stop_briefing":
        response.context = "\n\n".join(
            part for part in (plan.briefing, plan.turn_context) if isinstance(part, str) and part
        )
        return
    response.context = plan.turn_context


def process_response(
    handler: PendingContextHandler,
    event: HookEvent,
    response: HookResponse,
) -> None:
    """Settle, stash, and flush pending context for one Grok hook response."""
    if event.source != SessionSource.GROK:
        return
    try:
        settle_delivery(handler, event)
        stash_response(handler, event, response)
        flush_response(handler, event, response)
    finally:
        event.metadata.pop("_session_just_materialized", None)
        event.metadata.pop("_grok_briefing_turn", None)


def _envelope_session_id(
    handler: PendingContextHandler,
    envelope: dict[str, Any],
) -> str | None:
    headers = envelope.get("headers")
    if isinstance(headers, dict):
        candidate = headers.get("X-Gobby-Session-Id")
        if isinstance(candidate, str) and candidate and handler._session_manager.get(candidate):
            return candidate

    input_data = envelope.get("input_data")
    external_id = input_data.get("session_id") if isinstance(input_data, dict) else None
    if not isinstance(external_id, str) or not external_id:
        return None
    project_id = headers.get("X-Gobby-Project-Id") if isinstance(headers, dict) else None
    source = envelope.get("source")
    if source != "grok":
        return None
    session = handler._session_manager.find_by_external_id(
        external_id,
        project_id if isinstance(project_id, str) else None,
        source,
    )
    return str(session.id) if session is not None else None


def handle_ack_pending_inbox_envelope(
    handler: PendingContextHandler,
    envelope_id: str,
    envelope: dict[str, Any],
    path: Path,
    *,
    remove_marker: Callable[[str], object],
) -> bool:
    """Retain or expire a daemon-owned envelope awaiting ghook acknowledgment."""
    session_id = _envelope_session_id(handler, envelope)
    if session_id is None:
        return False
    variable_manager = SessionVariableManager(handler._session_manager.db)
    delivery = _delivery(variable_manager.get_variables(session_id).get(DELIVERY_VARIABLE))
    if delivery is None or delivery["envelope_id"] != envelope_id:
        return False

    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
    except FileNotFoundError:
        return True
    if age < GROK_DELIVERY_RETENTION_SECONDS:
        return True

    def mutate(variables: dict[str, Any]) -> tuple[bool, bool]:
        current = _delivery(variables.get(DELIVERY_VARIABLE))
        if current is None or current["envelope_id"] != envelope_id:
            return False, False
        queued = _components(variables.get(BRIEFING_VARIABLE))
        claimed_ids = {component["id"] for component in current["components"]}
        variables[BRIEFING_VARIABLE] = current["components"] + [
            component for component in queued if component["id"] not in claimed_ids
        ]
        variables.pop(DELIVERY_VARIABLE, None)
        return True, True

    if not variable_manager._mutate_variables(session_id, mutate):
        return False
    path.unlink(missing_ok=True)
    remove_marker(envelope_id)
    logger.warning(
        "Requeued expired Grok pending delivery: session=%s envelope=%s",
        session_id,
        envelope_id,
    )
    return True
