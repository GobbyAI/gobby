"""Context seeding for MCP HTTP requests."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from fastapi import HTTPException, Request

from gobby.mcp_proxy.models import ToolProxyErrorCode
from gobby.mcp_proxy.terminal_context import TERMINAL_CONTEXT_KEYS
from gobby.mcp_proxy.wait_tools import MCP_WRAPPER_PROTOCOL_VERSION_HEADER
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.session_resolution import resolve_session_reference
from gobby.utils.session_context import (
    AGENT_RUN_ID_HEADER,
    TERMINAL_CONTEXT_HEADER,
    SeededContextTokens,
    SessionContext,
    reset_seeded_contexts,
    resolve_and_seed_contexts,
    set_current_agent_run_id,
    set_session_context,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger("gobby.servers.routes.mcp.endpoints.execution")

_TERMINAL_CONTEXT_KEYS = frozenset(TERMINAL_CONTEXT_KEYS)


def _raise_session_required(*, terminal_context_seen: bool) -> NoReturn:
    raise HTTPException(
        status_code=409,
        detail={
            "success": False,
            "error_code": ToolProxyErrorCode.SESSION_REQUIRED.value,
            "error": "Wrapper caller session could not be resolved",
            "terminal_context_seen": terminal_context_seen,
        },
    )


def _parse_terminal_context_header(raw_context: str | None, *, seen: bool) -> dict[str, Any]:
    if not seen or not raw_context:
        _raise_session_required(terminal_context_seen=seen)
    try:
        parsed = json.loads(raw_context)
    except (json.JSONDecodeError, TypeError):
        _raise_session_required(terminal_context_seen=True)
    if (
        not isinstance(parsed, dict)
        or not parsed
        or not set(parsed).issubset(_TERMINAL_CONTEXT_KEYS)
    ):
        _raise_session_required(terminal_context_seen=True)
    return parsed


def _header_seen(headers: Mapping[str, str], name: str) -> bool:
    return name in headers or name.lower() in headers


def _get_argument_session_id(arguments: Any) -> str | None:
    """Return a target-tool session_id from the request body when present."""
    if isinstance(arguments, dict):
        session_id = arguments.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _session_ref_seq_num(session_ref: str | None) -> int | None:
    if not session_ref:
        return None
    raw = session_ref[1:] if session_ref.startswith("#") else session_ref
    return int(raw) if raw.isdigit() else None


def _derive_project_from_unique_session_seq(
    server: HTTPServer, session_ref: str | None
) -> str | None:
    """Return a project_id for an unscoped #N session ref when it is unambiguous."""
    seq_num = _session_ref_seq_num(session_ref)
    session_manager = server.session_manager if server.session_manager else None
    db = session_manager.db if session_manager else None
    if seq_num is None or db is None:
        return None

    try:
        rows = db.fetchall(
            """
            SELECT DISTINCT project_id
            FROM sessions
            WHERE seq_num = %s AND project_id IS NOT NULL
            LIMIT 2
            """,
            (seq_num,),
        )
    except Exception as exc:
        logger.debug(
            "Could not derive project from session ref %r: %s",
            session_ref,
            exc,
        )
        return None

    if len(rows) == 1:
        project_id = rows[0]["project_id"]
        return str(project_id) if project_id else None
    if len(rows) > 1:
        logger.debug(
            "Session ref %r is ambiguous across projects; project header is required",
            session_ref,
        )
    return None


async def _set_context_for_request(
    server: HTTPServer, arguments: Any, request: Request | None = None
) -> SeededContextTokens:
    """Set project and session context vars from the best available source.

    Wrapper caller identity:
      1. X-Gobby-Session-Id header
      2. X-Gobby-Terminal-Context resolution
      3. HTTP 409 SESSION_REQUIRED

    Non-wrapper callers retain the body ``session_id`` fallback. For wrappers,
    body ``session_id`` remains a target-tool parameter only.

    The stdio process runs in the CLI's project directory, so its CWD-derived
    project_id is always correct. The daemon's CWD is NOT — it points to the
    gobby project regardless of which project the caller is in.

    Returns seeded tokens; pass them to ``_reset_context`` after the tool call.
    """
    headers = request.headers if request else {}
    header_session_id = headers.get("x-gobby-session-id")
    project_id_header = headers.get("x-gobby-project-id")
    caller_project_id_header = headers.get("x-gobby-caller-project-id")
    wrapper_request = _header_seen(headers, MCP_WRAPPER_PROTOCOL_VERSION_HEADER)
    terminal_context_seen = _header_seen(headers, TERMINAL_CONTEXT_HEADER)
    argument_session_id = _get_argument_session_id(arguments)

    # Header session is wrapper/caller context. Body session_id remains a
    # target-tool parameter and must not make child-session workflow
    # enforcement apply to the caller.
    session_id = header_session_id or (None if wrapper_request else argument_session_id)
    session_ref_origin: Literal["explicit", "ambient"] = "ambient"
    if header_session_id:
        session_ref_origin = "explicit"

    # HTTP-specific bootstrap: old clients send only X-Gobby-Project-Id as a
    # caller-project hint. New clients also send X-Gobby-Caller-Project-Id; when
    # X-Gobby-Project-Id differs, it is an explicit target project override.
    canonical_project_ref = project_id_header or caller_project_id_header
    session_scope_ref = caller_project_id_header
    project_ref_is_fallback = not (
        bool(caller_project_id_header)
        and bool(project_id_header)
        and caller_project_id_header != project_id_header
    )

    if wrapper_request and not header_session_id:
        terminal_context = _parse_terminal_context_header(
            headers.get(TERMINAL_CONTEXT_HEADER),
            seen=terminal_context_seen,
        )
        if not server.session_manager:
            _raise_session_required(terminal_context_seen=terminal_context_seen)
        resolution_project_id = caller_project_id_header or project_id_header
        resolved_session = await server.run_db(
            server.session_manager.resolve_current_terminal_session,
            resolution_project_id,
            terminal_context.get("parent_pid"),
            terminal_context,
        )
        if resolved_session is None:
            _raise_session_required(terminal_context_seen=terminal_context_seen)

        canonical_project_ref = canonical_project_ref or resolved_session.project_id
        db = server.session_manager.db
        tokens = await resolve_and_seed_contexts(
            session_ref=None,
            session_manager=server.session_manager,
            project_ref=canonical_project_ref,
            session_scope_ref=caller_project_id_header,
            session_ref_origin="ambient",
            project_ref_is_fallback=project_ref_is_fallback,
            db=db,
        )
        tokens.session_token = set_session_context(
            SessionContext(
                session_id=resolved_session.id,
                conversation_id=resolved_session.external_id,
            )
        )
        tokens.resolved_session_id = resolved_session.id
        try:
            await _bind_agent_run_context(server, request, tokens, db=db)
        except Exception:
            reset_seeded_contexts(tokens)
            raise
        return tokens

    if not canonical_project_ref and header_session_id:
        canonical_project_ref = await server.run_db(
            _derive_project_from_unique_session_seq, server, header_session_id
        )
    if (
        not canonical_project_ref
        and header_session_id
        and argument_session_id
        and not wrapper_request
        and server.session_manager
        and argument_session_id.lstrip("#").isdigit()
    ):
        try:
            bootstrap_id = await server.run_db(
                resolve_session_reference, server.session_manager.db, header_session_id
            )
            bootstrap_session = await server.run_db(server.session_manager.get, bootstrap_id)
            if bootstrap_session:
                canonical_project_ref = bootstrap_session.project_id
        except Exception as exc:
            logger.debug(
                "HTTP project bootstrap from header session %r failed: %s",
                header_session_id,
                exc,
            )

    db = server.session_manager.db if server.session_manager else None
    tokens = await resolve_and_seed_contexts(
        session_ref=session_id,
        session_manager=server.session_manager if server.session_manager else None,
        project_ref=canonical_project_ref,
        session_scope_ref=session_scope_ref,
        session_ref_origin=session_ref_origin,
        project_ref_is_fallback=project_ref_is_fallback,
        db=db,
    )
    if wrapper_request and tokens.resolved_session_id is None:
        reset_seeded_contexts(tokens)
        _raise_session_required(terminal_context_seen=terminal_context_seen)
    try:
        await _bind_agent_run_context(server, request, tokens, db=db)
    except Exception:
        reset_seeded_contexts(tokens)
        raise
    return tokens


async def _bind_agent_run_context(
    server: HTTPServer,
    request: Request | None,
    tokens: SeededContextTokens,
    *,
    db: HubDatabase | None,
) -> None:
    if request is None or db is None:
        return
    header_run_id = request.headers.get(AGENT_RUN_ID_HEADER)
    manager = LocalAgentRunManager(db)
    if header_run_id:
        run = await server.run_db(manager.get, header_run_id)
        if (
            run is None
            or run.status not in {"pending", "running"}
            or (
                tokens.resolved_session_id is not None
                and run.child_session_id != tokens.resolved_session_id
            )
        ):
            raise HTTPException(status_code=403, detail="Invalid agent run identity")
        tokens.agent_run_token = set_current_agent_run_id(header_run_id)
        return
    if tokens.resolved_session_id is None:
        return
    active_run = await server.run_db(manager.get_by_session, tokens.resolved_session_id)
    if (
        active_run is not None
        and isinstance(active_run.id, str)
        and active_run.child_session_id == tokens.resolved_session_id
    ):
        raise HTTPException(status_code=403, detail="Missing agent run identity")


def _reset_context(tokens: SeededContextTokens) -> None:
    """Reset project and session context vars."""
    reset_seeded_contexts(tokens)
