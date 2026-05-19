"""Inter-agent messaging and command tools for the gobby-agents MCP server.

Provides P2P messaging and command coordination between sessions:
- send_message: P2P messaging with same-project validation
- send_command: Ancestor sends command to descendant
- complete_command: Descendant completes command, clears state, sends result
- deliver_pending_messages: Fetch and mark undelivered messages
- activate_command: Activate a pending command, set session variables
- wait_for_command: Block until a pending command arrives, with optional auto-activate
- get_inter_session_messages: Read-only query of message history
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.inter_session_messages import normalize_message_direction

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.sessions.mailbox import WakeDispatcherProtocol
    from gobby.storage.agent_commands import AgentCommandManager
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.state_manager import SessionVariableManager

# Type alias for the broadcast callback
BroadcastFn = Callable[..., Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)


class CommandLike(Protocol):
    """Protocol for command objects."""

    id: str
    command_text: str
    allowed_tools: str | None
    allowed_mcp_tools: str | None
    exit_condition: str | None


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
    """Return pending-message payload with delivery context included."""
    raw_payload = msg.to_brief()
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
    command_manager: AgentCommandManager,
    session_var_manager: SessionVariableManager,
    db: DatabaseProtocol,
    broadcast_fn: BroadcastFn | None = None,
    wake_dispatcher: WakeDispatcherProtocol | None = None,
) -> None:
    """Add inter-agent messaging and command tools to a registry.

    Args:
        registry: The InternalToolRegistry to add tools to
        message_manager: For persisting inter-session messages
        session_manager: For resolving session relationships
        command_manager: For managing agent commands
        session_var_manager: For setting/clearing session variables
        db: Database for direct queries (agent_runs)
        broadcast_fn: Optional async callback for WebSocket broadcasts.
            Called with (msg_type=, event=, **kwargs) on successful operations.
    """

    def _resolve(ref: str) -> str:
        """Resolve session reference to UUID."""
        from gobby.utils.project_context import get_project_context

        ctx = get_project_context()
        project_id = ctx.get("id") if ctx else None
        return session_manager.resolve_session_reference(ref, project_id)

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
            "Send a P2P message between sessions. Validates both sessions "
            "are in the same project. Messages are automatically injected "
            "into the recipient's context on their next tool call via hook "
            "rules — no polling or mailbox fetch needed. Also auto-writes "
            "to agent_runs.result when sending to parent. Direct message callers "
            "pass to_session and content. Broadcast callers pass send_to_all=true "
            "and omit to_session. Optional fields such as priority, message_type, "
            "metadata, and include_wakeup are keyword-only."
        ),
    )
    async def send_message(
        from_session: str,
        to_session: str | None = None,
        content: str | None = None,
        *,
        priority: str = "normal",
        send_to_all: bool = False,
        include_wakeup: bool = False,
        message_type: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            if content is None:
                return {"success": False, "error": "content is required."}
            content = content.strip()
            if not content:
                return {"success": False, "error": "content is required."}
            if send_to_all and to_session is not None:
                return {
                    "success": False,
                    "error": "to_session cannot be combined with send_to_all=true.",
                }
            if not send_to_all and to_session is None:
                return {
                    "success": False,
                    "error": "Pass to_session unless send_to_all is true.",
                }
            from_id = _resolve(from_session)

            to_id = _resolve(to_session) if to_session is not None else None

            send_result = await mailbox.send(
                from_session_id=from_id,
                to_session_id=to_id,
                send_to_all=send_to_all,
                include_wakeup=include_wakeup,
                content=content,
                priority=priority,
                message_type=message_type,
                metadata=metadata,
            )
            msg = send_result.messages[0] if len(send_result.messages) == 1 else None

            from_sess = session_manager.get(from_id)

            # Auto-write to agent_runs.result when sending to parent
            if from_sess and to_id and from_sess.parent_session_id == to_id:
                try:
                    row = db.fetchone(
                        "SELECT id FROM agent_runs WHERE child_session_id = ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (from_id,),
                    )
                    if row:
                        now = datetime.now(UTC).isoformat()
                        db.execute(
                            "UPDATE agent_runs SET result = ?, updated_at = ? WHERE id = ?",
                            (content, now, row["id"]),
                        )
                except Exception as e:
                    logger.warning(f"Failed to write to agent_runs.result: {e}")

            # Broadcast agent_message event
            failed_broadcasts: list[dict[str, Any]] = []
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
                        failed_broadcasts.append(
                            {
                                "recipient_session_id": recipient_id,
                                "error": str(e),
                            }
                        )
            payload = send_result.to_dict()
            payload["failed_broadcasts"] = failed_broadcasts
            payload["success"] = bool(payload.get("success")) and not failed_broadcasts
            payload["message"] = msg.to_dict() if msg is not None else None
            return payload

        except Exception as e:
            logger.error(f"send_message failed: {e}")
            return {"success": False, "error": str(e)}

    # ── send_command ───────────────────────────────────────────────

    @registry.tool(
        name="send_command",
        description=(
            "Send a command to a session at a higher agent depth within the same project. "
            "Rejects if the target already has an active command."
        ),
    )
    async def send_command(
        from_session: str,
        to_session: str,
        command_text: str,
        allowed_tools: list[str] | None = None,
        allowed_mcp_tools: list[str] | None = None,
        exit_condition: str | None = None,
    ) -> dict[str, Any]:
        try:
            from_id = _resolve(from_session)
            to_id = _resolve(to_session)

            # Validate: sender must be at a lower depth than recipient,
            # OR sender is at depth 0 (interactive/web chat — the operator).
            from_sess = session_manager.get(from_id)
            to_sess = session_manager.get(to_id)
            if from_sess is None:
                return {"success": False, "error": f"Sender session {from_id} not found"}
            if to_sess is None:
                return {"success": False, "error": f"Target session {to_id} not found"}
            from_depth = from_sess.agent_depth or 0
            to_depth = to_sess.agent_depth or 0
            # Depth-0 sessions (web chat, interactive CLI) can command any session.
            # Higher-depth agents can only command sessions at an even higher depth.
            if from_depth > 0 and from_depth >= to_depth:
                return {
                    "success": False,
                    "error": (
                        f"Can only send commands to sessions at a higher agent depth "
                        f"(sender depth={from_depth}, target depth={to_depth})"
                    ),
                }
            if from_sess.project_id != to_sess.project_id:
                return {
                    "success": False,
                    "error": "Sessions must be in the same project",
                }

            # Reject if active command exists
            active = [
                c
                for c in command_manager.list_commands(to_session=to_id)
                if c.status in ("pending", "running")
            ]
            if active:
                return {
                    "success": False,
                    "error": (
                        f"Session {to_id} already has an active command: "
                        f"{active[0].id} (status={active[0].status})"
                    ),
                }

            cmd = command_manager.create_command(
                from_session=from_id,
                to_session=to_id,
                command_text=command_text,
                allowed_tools=allowed_tools,
                allowed_mcp_tools=allowed_mcp_tools,
                exit_condition=exit_condition,
            )

            # Broadcast agent_command event
            if broadcast_fn:
                try:
                    await broadcast_fn(
                        msg_type="agent_command",
                        event="command_sent",
                        from_session=from_id,
                        to_session=to_id,
                        command_id=cmd.id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to broadcast agent_command: {e}")

            return {"success": True, "command": cmd.to_dict()}

        except Exception as e:
            logger.error(f"send_command failed: {e}")
            return {"success": False, "error": str(e)}

    # ── complete_command ───────────────────────────────────────────

    @registry.tool(
        name="complete_command",
        description=(
            "Complete a command: mark it done, clear session variables, "
            "and send the result back to the commanding session."
        ),
    )
    async def complete_command(
        target_session_id: str,
        command_id: str,
        result: str,
    ) -> dict[str, Any]:
        try:
            resolved_id = _resolve(target_session_id)
            cmd = command_manager.get_command(command_id)
            if not cmd:
                return {"success": False, "error": f"Command not found: {command_id}"}

            if cmd.to_session != resolved_id:
                return {
                    "success": False,
                    "error": f"Command not assigned to session {resolved_id}",
                }

            # Mark completed
            command_manager.update_status(command_id, "completed")

            # Clear session variables
            session_var_manager.delete_variables(resolved_id)

            # Send result to commanding session
            message_manager.create_message(
                from_session=resolved_id,
                to_session=cmd.from_session,
                content=result,
                priority="normal",
                message_type="command_result",
            )

            # Broadcast agent_command event
            if broadcast_fn:
                try:
                    await broadcast_fn(
                        msg_type="agent_command",
                        event="command_completed",
                        from_session=resolved_id,
                        to_session=cmd.from_session,
                        command_id=command_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to broadcast agent_command: {e}")

            return {"success": True, "command_id": command_id, "status": "completed"}

        except Exception as e:
            logger.error(f"complete_command failed: {e}")
            return {"success": False, "error": str(e)}

    # ── deliver_pending_messages ───────────────────────────────────

    @registry.tool(
        name="deliver_pending_messages",
        description=(
            "Fetch undelivered messages for a session and mark them as delivered. "
            "Use this to inject pending messages as context."
        ),
    )
    async def deliver_pending_messages(
        target_session_id: str,
    ) -> dict[str, Any]:
        try:
            resolved_id = _resolve(target_session_id)
            undelivered = message_manager.get_undelivered_messages(resolved_id)

            messages = []
            for msg in undelivered:
                message_manager.mark_delivered(msg.id)
                messages.append(_message_delivery_payload(msg))

            return {
                "success": True,
                "messages": messages,
                "count": len(messages),
            }

        except Exception as e:
            logger.error(f"deliver_pending_messages failed: {e}")
            return {"success": False, "error": str(e)}

    # ── get_inter_session_messages ────────────────────────────────

    @registry.tool(
        name="get_inter_session_messages",
        description=(
            "Read-only query of inter-session message history. "
            "Returns sent and/or received messages without marking them "
            "as delivered or read. Use for debugging, audit, and visibility."
        ),
    )
    async def get_inter_session_messages(
        target_session_id: str,
        direction: str = "all",
        unread_only: bool = False,
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
                unread_only=unread_only,
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
            logger.error(f"get_inter_session_messages failed: {e}")
            return {"success": False, "error": str(e)}

    # ── shared activation helper ────────────────────────────────

    def _activate_command_impl(cmd: CommandLike, resolved_session_id: str) -> list[str]:
        """Activate a command: mark running and set session variables.

        Returns the list of variable names that were set.
        """
        import json as _json

        command_manager.update_status(cmd.id, "running")

        variables: dict[str, Any] = {
            "command_id": cmd.id,
            "command_text": cmd.command_text,
        }
        if cmd.allowed_tools:
            try:
                variables["allowed_tools"] = _json.loads(cmd.allowed_tools)
            except (ValueError, TypeError):
                variables["allowed_tools"] = cmd.allowed_tools
        if cmd.allowed_mcp_tools:
            try:
                variables["allowed_mcp_tools"] = _json.loads(cmd.allowed_mcp_tools)
            except (ValueError, TypeError):
                variables["allowed_mcp_tools"] = cmd.allowed_mcp_tools
        if cmd.exit_condition:
            variables["exit_condition"] = cmd.exit_condition

        session_var_manager.merge_variables(resolved_session_id, variables)
        return list(variables.keys())

    # ── activate_command ──────────────────────────────────────────

    @registry.tool(
        name="activate_command",
        description=(
            "Activate a pending command: mark it running and set session "
            "variables (command_id, command_text, allowed_tools, exit_condition)."
        ),
    )
    async def activate_command(
        target_session_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        try:
            resolved_id = _resolve(target_session_id)
            cmd = command_manager.get_command(command_id)
            if not cmd:
                return {"success": False, "error": f"Command not found: {command_id}"}

            if cmd.to_session != resolved_id:
                return {
                    "success": False,
                    "error": f"Command not assigned to session {resolved_id}",
                }

            variables_set = _activate_command_impl(cmd, resolved_id)

            return {
                "success": True,
                "command": cmd.to_dict(),
                "variables_set": variables_set,
            }

        except Exception as e:
            logger.error(f"activate_command failed: {e}")
            return {"success": False, "error": str(e)}

    # ── wait_for_command ──────────────────────────────────────────

    @registry.tool(
        name="wait_for_command",
        description=(
            "Block until a pending command arrives for the session, or timeout. "
            "Polls the database at configurable intervals. By default, auto-activates "
            "the command (marks running, sets session variables). Returns the command "
            "details or a timeout indicator."
        ),
    )
    async def wait_for_command(
        target_session_id: str,
        timeout: int = 600,
        poll_interval: int = 5,
        auto_activate: bool = True,
    ) -> dict[str, Any]:
        """Block until a pending command arrives for the session, or timeout.

        Args:
            session_id: The session to wait for commands on.
            timeout: Maximum wait time in seconds (default: 600).
            poll_interval: Time between status checks in seconds (default: 5).
            auto_activate: If True, auto-activate the command (mark running,
                set session variables). Default: True.

        Returns:
            Dict with:
            - success: Always True (errors are exceptions)
            - command: The command dict, or None if timed out
            - timed_out: Whether the wait timed out
            - wait_time: How long we waited in seconds
        """
        if poll_interval <= 0:
            logger.warning(f"Invalid poll_interval {poll_interval}, using default of 5s")
            poll_interval = 5

        start_time = time.monotonic()

        try:
            resolved_id = _resolve(target_session_id)

            # Check for immediately available command
            pending = command_manager.list_commands(to_session=resolved_id, status="pending")
            if pending:
                cmd = pending[0]
                if auto_activate:
                    _activate_command_impl(cmd, resolved_id)
                return {
                    "success": True,
                    "command": cmd.to_dict(),
                    "timed_out": False,
                    "wait_time": time.monotonic() - start_time,
                }

            # Poll until command arrives or timeout
            while True:
                elapsed = time.monotonic() - start_time

                if elapsed >= timeout:
                    return {
                        "success": True,
                        "command": None,
                        "timed_out": True,
                        "wait_time": elapsed,
                    }

                await asyncio.sleep(poll_interval)

                pending = command_manager.list_commands(to_session=resolved_id, status="pending")
                if pending:
                    cmd = pending[0]
                    if auto_activate:
                        _activate_command_impl(cmd, resolved_id)
                    return {
                        "success": True,
                        "command": cmd.to_dict(),
                        "timed_out": False,
                        "wait_time": time.monotonic() - start_time,
                    }

        except Exception as e:
            logger.error(f"wait_for_command failed: {e}")
            return {"success": False, "error": str(e)}
