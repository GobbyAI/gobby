"""Inter-agent messaging and command tools for the gobby-agents MCP server.

Provides P2P messaging and command coordination between sessions:
- send_message: target-based messaging; session targets accept UUID, #N, or <project>#N
- get_inter_session_message: Retrieve one complete message for a participant
- get_inter_session_messages: Read-only query of message history
- send_attachment: send_message with one stored document attached
- get_attachment: read a message's attached document, for its sender or recipient only
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any, Literal

from gobby.events.live_wake import NOT_LIVE_WAKE_SKIPS
from gobby.storage.inter_session_attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentDraft,
    AttachmentError,
    InterSessionAttachmentManager,
)
from gobby.storage.inter_session_messages import normalize_message_direction
from gobby.storage.sessions import system_session_id
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.sessions.mailbox_delivery import WakeDispatcherProtocol
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager

# Type alias for the broadcast callback
BroadcastFn = Callable[..., Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)

NOT_LIVE_RECIPIENT_MESSAGE = (
    "The recipient session is not live. The message is stored and waits until that "
    "session resumes; no live wake was delivered."
)


def get_context_project_id(session_manager: SessionManager) -> str | None:
    """Use explicit project context or the caller session's project UUID."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    project_id = ctx.get("id") if ctx else None
    if isinstance(project_id, str):
        return project_id
    from gobby.utils.session_context import get_current_session_id

    caller_id = get_current_session_id()
    caller = session_manager.get(caller_id) if caller_id else None
    return caller.project_id if caller else None


def _message_metadata(msg: Any) -> dict[str, Any]:
    """Parse optional JSON metadata from an inter-session message."""
    raw = getattr(msg, "metadata_json", None)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _message_delivery_payload(msg: Any) -> dict[str, Any]:
    """Return a complete message payload with parsed delivery metadata."""
    raw_payload = msg.to_dict()
    payload: dict[str, Any] = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
    metadata = _message_metadata(msg)
    if metadata:
        payload["metadata"] = metadata
    for key in ("run_id", "task_id", "completion_id", "signoff_message"):
        value = metadata.get(key)
        if value:
            payload[key] = value
    payload["has_signoff"] = bool(metadata.get("signoff_message") or metadata.get("signoff"))
    return payload


def add_messaging_tools(
    registry: InternalToolRegistry,
    message_manager: InterSessionMessageManager,
    session_manager: SessionManager,
    db: HubDatabase,
    broadcast_fn: BroadcastFn | None = None,
    wake_dispatcher: WakeDispatcherProtocol | None = None,
) -> None:
    """Add inter-agent messaging and command tools to a registry.

    Args:
        registry: The InternalToolRegistry to add tools to
        message_manager: For persisting inter-session messages
        session_manager: For resolving session relationships
        db: Database for direct queries (agent_runs)
        broadcast_fn: Optional async callback for WebSocket broadcasts.
            Called with (msg_type=, event=, **kwargs) on successful operations.
    """

    attachment_manager = InterSessionAttachmentManager(db)

    def _resolve(ref: str) -> str:
        """Resolve session reference to UUID."""
        return session_manager.resolve_session_reference(
            ref, get_context_project_id(session_manager)
        )

    from gobby.sessions.mailbox import MailboxService

    mailbox = MailboxService(
        db=db,
        message_manager=message_manager,
        session_manager=session_manager,
        wake_dispatcher=wake_dispatcher,
    )

    # ── send_message ───────────────────────────────────────────────

    @registry.tool(
        name="send_message",
        description=(
            "Send a durable message to global, project, parent, session, or agent. "
            "global reaches every live non-system session owned by this machine; "
            "project reaches that population in the sender's project. Both exclude "
            "the sender and forbid target_id. Session targets accept a full UUID, "
            "#N (caller project), or <project>#N (e.g. gobby#11265) for a "
            "session in another project. Messages are automatically injected "
            "into the recipient's context on their next tool call via hook "
            "rules — no polling or mailbox fetch needed. Also auto-writes "
            "to agent_runs.result when sending to parent. Pass target_id for "
            "target='session' (session ref, forms above) or target='agent' (agent run id). "
            "A non-system project send derives its project from from_session and rejects "
            "project_id; a system-originated project send requires project_id. "
            "from_session defaults to the calling session's id from SessionContext "
            "when omitted. Spawned agents may omit target (defaults to parent). "
            "target='parent' reaches the session that spawned the sender, "
            "forbids target_id, and is available only to spawned agent sessions, which "
            "cannot override from_session. The bundled rule definition-selected messaging policy "
            "limits a spawned agent to the target modes its agent definition lists in "
            "send_message_targets (default: parent only). "
            "Recipient admission follows installed definitions: message_sender_capabilities "
            "requires any listed capability from the sender, with system and operator-terminal "
            "exemptions. Refused direct sends fail with message_recipient_not_allowed and "
            "fanout skips that recipient. "
            "Message content never causes wake behavior. Omitting wake requests immediate "
            "processing for direct session, parent, or agent targets. Fanout (global, and "
            "project with wake=true) applies that same rule to each recipient, with no "
            "claimed-task filter. A wake types '[Gobby] Check messages' only into an idle, "
            "paused, or waiting seat with no open turn whose foreground process is an agent "
            "CLI. No pane text decides a wake: while a viewer holding the seat's terminal "
            "reports focus the wake is held, and it is sent once focus is reported lost "
            "after the viewer's idle window; an unfocused pane takes the wake. An active "
            "turn takes no typed input at any priority; its message arrives through the "
            "hook mailbox on its next tool call. A typed draft, held daemon text, or a "
            "blocked composer, and an interrupted, awaiting-input, awaiting-approval, or "
            "awaiting-handoff session, take no typed input, so the message stays queued "
            "until the seat is safe to wake. The one handoff exception "
            "is the get_handoff continuation of an awaiting-handoff terminal with a "
            "confirmed compact boundary and an empty composer. Otherwise project fanout "
            "stays queued without live wakes, and wake=false queues any target without a "
            "live wake. The daemon sends restart and cutover notices itself. "
            "Wake-result delivery means trigger dispatch, not "
            "mailbox acknowledgement. Optional priority, message_type, metadata, and wake "
            "are keyword-only. For message_type='task_blocker', metadata.task_id must name "
            "the blocked assigned task. Responses are brief by default: target, recipient "
            "count, delivery status, and message IDs. A direct send to a session outside the "
            "live statuses returns delivery_status sent_recipient_not_live with that session's "
            "recipient_status and a message: the durable message is stored and waits until the "
            "session resumes, and no live wake is delivered. A live session whose wake is "
            "declined returns sent_with_declined_wakes. Pass brief=false for the full message, "
            "selector, wake, and broadcast diagnostics."
        ),
    )
    async def send_message(
        target: Literal["global", "project", "parent", "session", "agent"] | None = None,
        content: str = "",
        target_id: str | None = None,
        from_session: str | None = None,
        *,
        project_id: str | None = None,
        priority: str = "normal",
        wake: bool | None = None,
        message_type: str = "message",
        metadata: dict[str, Any] | None = None,
        brief: bool = True,
    ) -> dict[str, Any]:
        return await _deliver(
            target=target,
            content=content,
            target_id=target_id,
            from_session=from_session,
            project_id=project_id,
            priority=priority,
            wake=wake,
            message_type=message_type,
            metadata=metadata,
            brief=brief,
            attachment=None,
        )

    async def _deliver(
        *,
        target: str | None,
        content: str,
        target_id: str | None,
        from_session: str | None,
        project_id: str | None,
        priority: str,
        wake: bool | None,
        message_type: str,
        metadata: dict[str, Any] | None,
        brief: bool,
        attachment: AttachmentDraft | None,
    ) -> dict[str, Any]:
        try:
            from gobby.utils.session_context import get_current_session_id, is_daemon_execution

            ctx_session_id = get_current_session_id()
            if from_session is None:
                if not ctx_session_id:
                    return {
                        "success": False,
                        "error": (
                            "from_session is required and no SessionContext session_id is available"
                        ),
                    }
                from_session = ctx_session_id

            if not content.strip():
                return {"success": False, "error": "content is required."}

            if target is None or not target.strip():
                preview_from_id = _resolve(from_session)
                preview_sess = session_manager.get(preview_from_id)
                preview_caller_id = (
                    _resolve(ctx_session_id)
                    if ctx_session_id and ctx_session_id != from_session
                    else preview_from_id
                )
                preview_caller = (
                    preview_sess
                    if preview_caller_id == preview_from_id
                    else session_manager.get(preview_caller_id)
                )
                if preview_caller is not None and (
                    preview_caller.agent_run_id or preview_caller.agent_depth > 0
                ):
                    target = "parent"
                else:
                    return {
                        "success": False,
                        "error": "target is required unless the caller is a spawned agent.",
                        "error_code": "target_required",
                    }

            normalized_target = target.strip().lower()
            if normalized_target in {"global", "project", "parent"} and target_id is not None:
                return {
                    "success": False,
                    "error": f"target_id is not allowed when target='{normalized_target}'.",
                    "error_code": "target_id_forbidden",
                }
            if normalized_target in {"session", "agent"}:
                if target_id is None or not target_id.strip():
                    return {
                        "success": False,
                        "error": f"target_id is required when target='{normalized_target}'.",
                        "error_code": "target_id_required",
                    }

            from_id = _resolve(from_session)
            # Only daemon-owned execution sends as the system session. A caller with no
            # SessionContext (a session-less HTTP call) gets no exemption.
            if from_id == system_session_id() and not is_daemon_execution():
                return {
                    "success": False,
                    "error": "Only daemon-owned execution can send as the system session.",
                    "error_code": "send_message_sender_mismatch",
                }
            if project_id is None and normalized_target == "agent":
                project_id = get_context_project_id(session_manager)

            from_sess = session_manager.get(from_id)
            if normalized_target == "global" and project_id is not None:
                return {
                    "success": False,
                    "error": "project_id is not allowed when target='global'.",
                    "error_code": "project_scope_forbidden",
                }
            if (
                normalized_target == "project"
                and from_id != system_session_id()
                and project_id is not None
            ):
                return {
                    "success": False,
                    "error": "project_id is not allowed for session-originated project broadcasts.",
                    "error_code": "project_scope_forbidden",
                }
            if (
                normalized_target == "project"
                and from_id == system_session_id()
                and project_id is None
            ):
                return {
                    "success": False,
                    "error": "project_id is required for system-originated project broadcasts.",
                    "error_code": "project_scope_required",
                }

            # Spawned agents send only as themselves. The bundled rule
            # definition-selected messaging policy limits which target modes they use.
            caller_id = (
                _resolve(ctx_session_id)
                if ctx_session_id and ctx_session_id != from_session
                else from_id
            )
            caller = from_sess if caller_id == from_id else session_manager.get(caller_id)
            if caller is not None and (caller.agent_run_id or caller.agent_depth > 0):
                if caller_id != from_id:
                    return {
                        "success": False,
                        "error": "Spawned agents can send messages only as their own session.",
                        "error_code": "send_message_sender_mismatch",
                    }

            resolved_target_id = target_id
            if normalized_target == "session":
                assert target_id is not None
                resolved_target_id = _resolve(target_id)

            # The mailbox resolves an omitted wake per target mode.
            send_result = await mailbox.send(
                from_session_id=from_id,
                target=normalized_target,
                target_id=resolved_target_id,
                wake=wake,
                content=content,
                preserve_content=True,
                priority=priority,
                message_type=message_type,
                metadata=metadata,
                project_id=project_id,
                attachment=attachment,
            )
            msg = send_result.messages[0] if len(send_result.messages) == 1 else None

            # Auto-write to agent_runs.result when sending to parent
            if (
                from_sess
                and len(send_result.recipient_session_ids) == 1
                and (
                    normalized_target == "parent"
                    or from_sess.parent_session_id == send_result.recipient_session_ids[0]
                )
            ):
                try:
                    row = db.fetchone(
                        "SELECT id FROM agent_runs WHERE child_session_id = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (from_id,),
                    )
                    if row:
                        now = utc_now()
                        db.execute(
                            "UPDATE agent_runs SET result = %s, updated_at = %s WHERE id = %s",
                            (content, now, row["id"]),
                        )
                except Exception as e:
                    logger.warning("Failed to write to agent_runs.result: %s", e)

            # Broadcast agent_message event
            failed_ws_broadcasts: list[dict[str, Any]] = []
            if broadcast_fn:
                for recipient_id in send_result.recipient_session_ids:
                    try:
                        await broadcast_fn(
                            msg_type="agent_message",
                            event="message_sent",
                            from_session=from_id,
                            to_session=recipient_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to broadcast agent_message",
                            extra={
                                "from_session": from_id,
                                "to_session": recipient_id,
                                "broadcast_id": send_result.broadcast_id,
                            },
                            exc_info=True,
                        )
                        failed_ws_broadcasts.append(
                            {
                                "recipient_session_id": recipient_id,
                                "error": str(e),
                            }
                        )
            response = send_result.to_dict()
            response["failed_ws_broadcasts"] = failed_ws_broadcasts
            response["success"] = send_result.success
            response["message"] = msg.to_dict() if msg is not None else None
            if attachment is not None:
                response["attachment"] = attachment.summary()
            if not brief or not send_result.success:
                return response

            wake_declines = [
                result
                for result in send_result.wake_results
                if result.get("delivered") is not True and result.get("decline_reason")
            ]
            wake_failures = [
                result
                for result in send_result.wake_results
                if result.get("delivered") is not True and not result.get("decline_reason")
            ]
            not_live_wakes = [
                result
                for result in send_result.wake_results
                if result.get("decline_reason") in NOT_LIVE_WAKE_SKIPS
            ]
            has_failures = bool(
                send_result.failed_broadcasts or failed_ws_broadcasts or wake_failures
            )
            brief_response: dict[str, Any] = {
                "success": True,
                "target": send_result.target,
                "recipient_count": len(send_result.recipient_session_ids),
                "delivery_status": (
                    "sent_with_failures"
                    if has_failures
                    else "sent_recipient_not_live"
                    if not_live_wakes
                    else "sent_with_declined_wakes"
                    if wake_declines
                    else "sent"
                ),
                "message_ids": send_result.message_ids,
            }
            if attachment is not None:
                brief_response["attachment"] = attachment.summary()
            if send_result.target_id is not None:
                brief_response["target_id"] = send_result.target_id
            if send_result.broadcast_id is not None:
                brief_response["broadcast_id"] = send_result.broadcast_id
            if send_result.failed_broadcasts:
                brief_response["failed_broadcasts"] = send_result.failed_broadcasts
            if failed_ws_broadcasts:
                brief_response["failed_ws_broadcasts"] = failed_ws_broadcasts
            if wake_failures:
                brief_response["wake_failures"] = wake_failures
            if not_live_wakes:
                brief_response["recipient_status"] = not_live_wakes[0].get("session_status")
                brief_response["message"] = NOT_LIVE_RECIPIENT_MESSAGE
            return brief_response

        except ValueError as e:
            # Session resolution and MailboxService raise ValueError only for
            # caller input (unknown or out-of-scope targets), which is not a fault.
            return {"success": False, "error": str(e), "error_code": "invalid_message_target"}
        except Exception as e:
            logger.error("send_message failed: %s", e)
            return {"success": False, "error": str(e)}

    # ── get_inter_session_message ──────────────────────────────────

    @registry.tool(
        name="send_attachment",
        description=(
            "Send a message with one document attached. The message follows send_message "
            "for target, wake, and fanout; each recipient of a project or global send gets "
            "its own copy. document is the complete UTF-8 text, filename names it, and "
            "media_type is text/plain or text/markdown. The daemon stores the document in the "
            "database with its byte size and SHA-256 digest. A document over "
            f"{MAX_ATTACHMENT_BYTES} bytes is refused with attachment_too_large and nothing is "
            "stored. Only the sender and that message's recipient can read the document, with "
            "get_attachment. Brief responses report delivery_status as send_message does, "
            "including sent_recipient_not_live with recipient_status for an expired recipient."
        ),
    )
    async def send_attachment(
        target: Literal["global", "project", "parent", "session", "agent"] | None = None,
        content: str = "",
        filename: str = "",
        document: str = "",
        media_type: str = "text/markdown",
        target_id: str | None = None,
        from_session: str | None = None,
        *,
        project_id: str | None = None,
        priority: str = "normal",
        wake: bool | None = None,
        message_type: str = "message",
        metadata: dict[str, Any] | None = None,
        brief: bool = True,
    ) -> dict[str, Any]:
        try:
            attachment = AttachmentDraft.build(
                filename=filename, media_type=media_type, content=document
            )
        except AttachmentError as e:
            return {"success": False, "error": str(e), "error_code": e.code}
        return await _deliver(
            target=target,
            content=content,
            target_id=target_id,
            from_session=from_session,
            project_id=project_id,
            priority=priority,
            wake=wake,
            message_type=message_type,
            metadata=metadata,
            brief=brief,
            attachment=attachment,
        )

    @registry.tool(
        name="get_attachment",
        read_only=True,
        description=(
            "Retrieve the document attached to one inter-session message by message ID. "
            "Only the message's sender or recipient can read it. Anyone else gets "
            "attachment_not_found, the same response as a message with no attachment."
        ),
    )
    def get_attachment(message_id: str) -> dict[str, Any]:
        try:
            from gobby.utils.session_context import get_current_session_id

            caller_session_id = get_current_session_id()
            if not caller_session_id:
                return {
                    "success": False,
                    "error": "No calling session is available for attachment retrieval.",
                    "error_code": "missing_session_context",
                }
            stored = attachment_manager.get_for_caller(message_id, _resolve(caller_session_id))
            if stored is None:
                return {
                    "success": False,
                    "error": "No attachment for that message ID is readable by this session.",
                    "error_code": "attachment_not_found",
                }
            return {"success": True, "attachment": stored.to_dict()}

        except Exception as e:
            logger.error("get_attachment failed: %s", e)
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_inter_session_message",
        read_only=True,
        description=(
            "Retrieve one complete stored inter-session message by ID. "
            "The calling session must be the sender or recipient."
        ),
    )
    def get_inter_session_message(message_id: str) -> dict[str, Any]:
        try:
            from gobby.utils.session_context import get_current_session_id

            caller_session_id = get_current_session_id()
            if not caller_session_id:
                return {
                    "success": False,
                    "error": "No calling session is available for message retrieval.",
                    "error_code": "missing_session_context",
                }
            resolved_caller_id = _resolve(caller_session_id)
            message = message_manager.get_message(message_id)
            if message is None:
                return {
                    "success": False,
                    "error": f"Inter-session message not found: {message_id}",
                    "error_code": "message_not_found",
                }
            if resolved_caller_id not in {message.from_session, message.to_session}:
                return {
                    "success": False,
                    "error": "Only the message sender or recipient may retrieve it.",
                    "error_code": "message_access_denied",
                }
            payload: dict[str, Any] = {
                "success": True,
                "message": _message_delivery_payload(message),
            }
            attachment = attachment_manager.get_summary(message.id)
            if attachment is not None:
                payload["attachment"] = attachment
            return payload

        except Exception as e:
            logger.error("get_inter_session_message failed: %s", e)
            return {"success": False, "error": str(e)}

    # ── get_inter_session_messages ────────────────────────────────

    @registry.tool(
        name="get_inter_session_messages",
        read_only=True,
        description=(
            "Read-only query of inter-session message history. "
            "Returns sent and/or received messages without marking them "
            "as delivered. Use for debugging, audit, and visibility."
        ),
    )
    def get_inter_session_messages(
        target_session_id: str,
        direction: str = "all",
        undelivered_only: bool = False,
        message_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            try:
                normalized_direction = normalize_message_direction(direction)
            except ValueError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "error_code": "invalid_direction",
                }
            resolved_id = _resolve(target_session_id)
            messages = message_manager.list_messages(
                session_id=resolved_id,
                direction=normalized_direction,
                undelivered_only=undelivered_only,
                message_type=message_type,
                limit=limit,
                offset=offset,
            )

            return {
                "success": True,
                "messages": [m.to_brief() for m in messages],
                "count": len(messages),
            }

        except Exception as e:
            logger.error("get_inter_session_messages failed: %s", e)
            return {"success": False, "error": str(e)}
