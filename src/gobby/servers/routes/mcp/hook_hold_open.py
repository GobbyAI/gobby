"""Web-chat hold-open handling for synchronous provider hooks."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from gobby.servers.routes.configuration_context import require_config_snapshot
from gobby.servers.tool_approvals import (
    approval_key_for_tool,
    get_global_approval_rules,
    is_tool_auto_allowed,
    load_project_approval_rules,
    normalize_approved_tool_keys,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

MAX_PENDING_PER_SESSION = 3


async def _maybe_hold_open(
    request: Request,
    session_id: str,
    hook_type: str,
    payload: dict[str, Any],
    source: str,
    *,
    server: HTTPServer | None = None,
) -> dict[str, Any] | None:
    """Hold web-chat hook responses open until user interaction resolves."""
    from gobby.storage.sessions import SessionManager

    resolved_server: HTTPServer | None = server or getattr(request.app.state, "server", None)
    if resolved_server is None:
        return None

    db = resolved_server.services.database
    if not db:
        return None
    session_store = SessionManager(db)
    db_session = await resolved_server.run_db(session_store.get, session_id)
    if not db_session:
        try:
            resolved_session_id = await resolved_server.run_db(
                session_store.resolve_session_reference, session_id
            )
        except Exception:
            resolved_session_id = None
        if resolved_session_id:
            db_session = await resolved_server.run_db(session_store.get, resolved_session_id)
    if not db_session:
        db_session = await resolved_server.run_db(
            session_store.find_active_by_external_id, session_id, source
        )

    if not db_session:
        return None

    if getattr(db_session, "session_type", "terminal") != "web_chat":
        return None

    project_path: str | None = None
    if getattr(db_session, "project_id", None):
        try:
            from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
            from gobby.storage.workspace_machine_scope import require_local_machine_id

            machine_id = getattr(db_session, "machine_id", None)
            if machine_id:
                try:
                    resolved_machine = require_local_machine_id(
                        machine_id,
                        resource_kind="project_checkout",
                        resource_id=db_session.project_id,
                    )
                    project_path = await resolved_server.run_db(
                        require_root, db, db_session.project_id, resolved_machine
                    )
                except CheckoutNotFoundError:
                    project_path = None
        except Exception:
            logger.debug("Failed to resolve project_path for approval check", exc_info=True)

    manager = getattr(request.app.state, "pending_interaction_manager", None)
    if manager is None:
        return None

    async def _broadcast_pending_tool(
        interaction_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        ws_server = resolved_server.services.websocket_server or resolved_server.websocket_server
        if not ws_server:
            return

        message = json.dumps(
            {
                "type": "tool_status",
                "conversation_id": db_session.id,
                "message_id": f"pending-interaction-{interaction_id}",
                "tool_call_id": interaction_id,
                "status": "pending_approval",
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        for ws, meta in list(ws_server.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid is not None and cid != db_session.id:
                continue
            try:
                await ws.send(message)
            except Exception:
                logger.debug("Failed to broadcast pending tool interaction", exc_info=True)

    if hook_type == "PreToolUse":
        input_data = payload.get("input_data", {}) or {}
        tool_name = input_data.get("tool_name", "")
        arguments = input_data.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}

        approved_tools_json = getattr(db_session, "approved_tools_json", None)
        try:
            raw_session_rules = json.loads(approved_tools_json) if approved_tools_json else []
        except (TypeError, json.JSONDecodeError):
            raw_session_rules = []
        session_rules = normalize_approved_tool_keys(raw_session_rules)
        project_rules = load_project_approval_rules(project_path)
        config_snapshot = require_config_snapshot(resolved_server)
        global_rules = get_global_approval_rules(config_snapshot)
        if tool_name and is_tool_auto_allowed(
            tool_name,
            arguments,
            session_rules=session_rules,
            project_rules=project_rules,
            global_rules=global_rules,
        ):
            return {"decision": "approve"}

        pending_count = await manager.count_pending(db_session.id)
        if pending_count >= MAX_PENDING_PER_SESSION:
            return {"decision": "deny", "reason": "too_many_pending"}

        interaction_id = await manager.create(
            session_id=db_session.id,
            kind="tool",
            provider=source,
            payload={"tool_name": tool_name, "arguments": arguments},
            tool_name=tool_name,
        )
        await _broadcast_pending_tool(interaction_id, tool_name, arguments)
        result_data = await manager.wait(interaction_id)
        decision = result_data.get("decision", "deny")
        if decision == "approve_always" and tool_name:
            key = approval_key_for_tool(tool_name, arguments)
            updated_rules = {*session_rules, key}
            await resolved_server.run_db(
                session_store.update_approved_tools, db_session.id, updated_rules
            )
            return {"decision": "approve"}
        if decision == "approve":
            return {"decision": "approve"}
        return {"decision": "deny"}

    if hook_type == "AskUserQuestion":
        question = payload.get("input_data", {}).get("question", "")
        interaction_id = await manager.create(
            session_id=db_session.id,
            kind="ask_user",
            provider=source,
            payload={"question": question},
        )
        result_data = await manager.wait(interaction_id)
        response = result_data.get("response", {})
        return {"additionalContext": response.get("answers", {})}

    return None
