"""Context seeding for MCP HTTP requests."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Request

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.session_resolution import resolve_session_reference
from gobby.utils.session_context import (
    AGENT_RUN_ID_HEADER,
    SeededContextTokens,
    reset_seeded_contexts,
    resolve_and_seed_contexts,
    set_current_agent_run_id,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger("gobby.servers.routes.mcp.endpoints.execution")


def _get_requested_session_id(arguments: Any, request: Request | None = None) -> str | None:
    """Return the raw session reference from tool arguments or HTTP headers.

    Discovery routes need the caller-supplied value for audit/proxy events even
    when context seeding cannot resolve it to a platform UUID.
    """
    if isinstance(arguments, dict):
        session_id = arguments.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id

    if request is None:
        return None

    header_session_id = request.headers.get("x-gobby-session-id")
    return header_session_id or None


def _get_argument_session_id(arguments: Any) -> str | None:
    """Return a target-tool session_id from the request body when present."""
    if isinstance(arguments, dict):
        session_id = arguments.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _get_discovery_session_id(arguments: Any, request: Request | None = None) -> str | None:
    """Return the session ref that should own HTTP discovery side effects.

    For HTTP callers, the session header identifies the requesting CLI session.
    Body/session arguments may target some other session for tool semantics, so
    discovery tracking prefers the header and only falls back to arguments.
    """
    if request is not None:
        header_session_id = request.headers.get("x-gobby-session-id")
        if header_session_id:
            return header_session_id

    return _get_requested_session_id(arguments, request)


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

    Priority:
      1. X-Gobby-Session-Id header (the caller/workflow context)
      2. session_id from tool arguments (the target tool parameter)
      3. X-Gobby-Caller-Project-Id header for wrapper session scope
      4. X-Gobby-Project-Id header for target project context

    The stdio process runs in the CLI's project directory, so its CWD-derived
    project_id is always correct. The daemon's CWD is NOT — it points to the
    gobby project regardless of which project the caller is in.

    Returns seeded tokens; pass them to ``_reset_context`` after the tool call.
    """
    header_session_id = request.headers.get("x-gobby-session-id") if request else None
    project_id_header = request.headers.get("x-gobby-project-id") if request else None
    caller_project_id_header = request.headers.get("x-gobby-caller-project-id") if request else None
    argument_session_id = _get_argument_session_id(arguments)

    # Header session is wrapper/caller context. Body session_id remains a
    # target-tool parameter and must not make child-session workflow
    # enforcement apply to the caller.
    session_id = header_session_id or argument_session_id
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
    if not canonical_project_ref and header_session_id:
        canonical_project_ref = await server.run_db(
            _derive_project_from_unique_session_seq, server, header_session_id
        )
    if (
        not canonical_project_ref
        and header_session_id
        and argument_session_id
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
