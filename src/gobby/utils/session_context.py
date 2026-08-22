"""Session context utilities for MCP tool calls.

Provides a per-async-task ContextVar that holds the calling session's identity.
Set by dispatch paths (HTTP routes, MCPServer, rule engine, pipeline executor)
before tool execution begins. Tools read via get_current_session_id() instead
of accepting session_id as a parameter.

Mirrors the pattern in project_context.py.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

AGENT_RUN_ID_HEADER = "X-Gobby-Agent-Run-Id"
TERMINAL_CONTEXT_HEADER = "X-Gobby-Terminal-Context"


@dataclass(frozen=True)
class SessionContext:
    """Immutable snapshot of the calling session's identity.

    Intentionally minimal — sessions are mutable, so tools that need the full
    session object should call session_manager.get(session_id) using the UUID
    from this context.
    """

    session_id: str
    """Always a resolved UUID (never #N or seq_num)."""

    conversation_id: str | None = None
    """External/CLI-specific session ID (e.g., Claude Code conversation ID)."""


_current_session_context: contextvars.ContextVar[SessionContext | None] = contextvars.ContextVar(
    "current_session_context", default=None
)
_current_agent_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_agent_run_id", default=None
)


def set_session_context(ctx: SessionContext | None) -> contextvars.Token[SessionContext | None]:
    """Set session context for the current async task.

    Called by dispatch paths before tool execution. Returns a token
    for reset via reset_session_context().
    """
    return _current_session_context.set(ctx)


def get_session_context() -> SessionContext | None:
    """Get the current session context, or None if not set."""
    return _current_session_context.get()


def get_current_session_id() -> str | None:
    """Convenience: get the current session UUID, or None if not set."""
    ctx = _current_session_context.get()
    return ctx.session_id if ctx else None


def reset_session_context(token: contextvars.Token[SessionContext | None]) -> None:
    """Reset session context after tool call completes."""
    _current_session_context.reset(token)


def set_current_agent_run_id(run_id: str) -> contextvars.Token[str | None]:
    """Bind a server-validated agent-run identity to the current request."""
    return _current_agent_run_id.set(run_id)


def get_current_agent_run_id() -> str | None:
    """Return the server-validated agent-run identity for this request."""
    return _current_agent_run_id.get()


def reset_current_agent_run_id(token: contextvars.Token[str | None]) -> None:
    """Reset the request-scoped agent-run identity."""
    _current_agent_run_id.reset(token)


def resolve_session_ref(
    session_manager: Any,
    ref: str,
) -> str:
    """Resolve a session reference (#N, N, UUID, or prefix) to UUID.

    Uses the current project context from ContextVar for scoping.
    Shared utility replacing duplicated closures in cross-session tools.

    Args:
        session_manager: SessionManager instance
        ref: Session reference string

    Returns:
        Resolved UUID string

    Raises:
        ValueError: If session cannot be resolved
    """
    if session_manager is None:
        return ref
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None
    return str(session_manager.resolve_session_reference(ref, project_id))


@contextmanager
def session_context_for_test(
    session_id: str = "test-session-id",
    conversation_id: str | None = None,
) -> Any:
    """Context manager for tests that need session context.

    Usage::

        with session_context_for_test("my-session-uuid"):
            result = await registry.call("create_task", {...})
    """
    ctx = SessionContext(session_id=session_id, conversation_id=conversation_id)
    token = set_session_context(ctx)
    try:
        yield ctx
    finally:
        reset_session_context(token)


@dataclass
class SeededContextTokens:
    """Tokens + resolved refs returned by ``resolve_and_seed_contexts``.

    Dispatchers propagate ``resolved_session_id`` (never the raw input ref) to
    ``tool_proxy.call_tool`` / ``get_tool_schema``. ``ToolProxyService`` prefers
    the explicit session_id arg over the ContextVar, so passing the raw ref
    would re-poison workflow checks and tool filters even after the ContextVar
    is clean.
    """

    session_token: contextvars.Token[SessionContext | None] | None = None
    agent_run_token: contextvars.Token[str | None] | None = None
    project_token: contextvars.Token[dict[str, Any] | None] | None = field(default=None)
    resolved_session_id: str | None = None
    resolved_project_id: str | None = None


def _canonicalize_project_ref(
    project_ref: str | None,
    db: HubDatabase | None,
) -> str | None:
    """Resolve a project UUID-or-name to its canonical UUID.

    When ``db`` is ``None`` we cannot consult ``LocalProjectManager``; accept
    the caller-supplied ref as-is so the minimal-fallback path at the end of
    ``resolve_and_seed_contexts`` can still emit a ``{"id": ref}`` project
    context for HTTP-header bootstrap.

    Raises:
        Any non-``LookupError`` raised by ``LocalProjectManager`` (DB / config
        failures) propagates — callers should not silently downgrade those to
        "not found". Returns ``None`` only when ``resolve_ref`` itself returns
        ``None`` (ref genuinely does not exist).
    """
    if not project_ref:
        return None
    if db is None:
        return project_ref
    from gobby.storage.projects import LocalProjectManager

    pm = LocalProjectManager(db)
    project = pm.resolve_ref(project_ref)
    return project.id if project else None


def _ref_requires_project_scope(session_ref: str) -> bool:
    """``#N`` / numeric refs need a project to resolve; UUIDs and prefixes do not.

    An external_id UUID must resolve authoritatively across projects — the
    caller-supplied or header-derived project_ref is a *context* hint, not a
    constraint on which project the referenced session belongs to. Scoping the
    session lookup by that hint would break cross-project tool calls and HTTP
    header bootstrap for UUID-shaped refs.
    """
    stripped = session_ref[1:] if session_ref.startswith("#") else session_ref
    return stripped.isdigit()


def _resolve_and_seed_contexts_sync(
    session_ref: str | None,
    session_manager: SessionManager | None,
    *,
    project_ref: str | None = None,
    session_scope_ref: str | None = None,
    session_ref_origin: Literal["explicit", "ambient"] = "explicit",
    project_ref_is_fallback: bool = False,
    db: HubDatabase | None = None,
) -> SeededContextTokens:
    """Resolve session and project refs, seed both ContextVars, return tokens.

    Session-scoping rule: ``session_scope_ref`` (or ``project_ref`` when no
    separate scope is supplied) is passed to the resolver only when the
    session_ref is ``#N`` / numeric. For UUID or prefix refs, the resolver is
    called with ``project_id=None`` — an ``external_id`` UUID is authoritative
    across projects and must not be constrained by a project *context* override.

    The mode flag affects project *context* precedence when a session resolves:

    * override mode (default, ``project_ref_is_fallback=False``) — explicit
      caller intent: project_ref > session-derived project. Use when the caller
      passed an explicit ``project_id`` param to override (e.g. server.py's
      cross-project tool calls).
    * fallback mode (``project_ref_is_fallback=True``) — bootstrap hint only:
      session-derived > project_ref. Use when project_ref is a bootstrap hint,
      not an override (e.g. execution.py's ``x-gobby-project-id`` header —
      preserves the current "session's own project wins" contract).

    Both modes fall through to ``project_ref`` when session resolution fails.
    ``session_scope_ref`` can be supplied when wrapper session identity must be
    resolved in the caller project while ``project_ref`` seeds a target project
    context for the downstream tool.
    On ``db is None`` with a ``project_ref`` that cannot be enriched, emit a
    minimal project context of ``{"id": project_ref}``.

    On ``project_ref`` unresolvable: ``resolved_project_id`` is ``None`` —
    callers decide whether that's a hard error.

    On ``session_ref`` unresolvable: ``SessionContext`` is not set; project
    context is set per the precedence rules above.

    Unexpected errors (DB failures, config errors) from project canonicalization
    or session resolution propagate. Only ``ValueError`` from the session
    resolver is logged-and-swallowed as a normal not-found / ambiguous path.
    Explicit session refs log at warning level; ambient wrapper/header refs log
    at debug level because they can be stale during context handoff/startup.
    """
    from gobby.utils.project_context import (
        set_project_context,
        set_project_context_from_ref,
        set_project_context_from_session,
    )

    tokens = SeededContextTokens()
    canonical_project_id = _canonicalize_project_ref(project_ref, db)
    canonical_session_scope_id = (
        _canonicalize_project_ref(session_scope_ref, db)
        if session_scope_ref
        else canonical_project_id
    )
    tokens.resolved_project_id = canonical_project_id

    resolved_session_id: str | None = None
    resolved_session_conversation_id: str | None = None
    effective_session_ref = session_ref.strip() if session_ref else None
    if effective_session_ref is not None and effective_session_ref.casefold() == "current":
        current_ctx = get_session_context()
        if current_ctx is not None:
            resolved_session_id = current_ctx.session_id
            resolved_session_conversation_id = current_ctx.conversation_id
        else:
            logger.debug(
                "resolve_and_seed_contexts: ignoring current session alias without "
                "active session context because get_session_context() returned None"
            )
        effective_session_ref = None

    if effective_session_ref and session_manager is not None:
        # UUID / prefix refs resolve globally; only #N needs project scope.
        session_scope = (
            canonical_session_scope_id
            if _ref_requires_project_scope(effective_session_ref)
            else None
        )
        try:
            resolved_session_id = str(
                session_manager.resolve_session_reference(effective_session_ref, session_scope)
            )
        except ValueError as exc:
            log = logger.debug if session_ref_origin == "ambient" else logger.warning
            log(
                "resolve_and_seed_contexts: could not resolve session ref %r (project_id=%s): %s",
                effective_session_ref,
                session_scope,
                exc,
            )

    if resolved_session_id:
        conversation_id: str | None = resolved_session_conversation_id
        try:
            session = session_manager.get(resolved_session_id) if session_manager else None
            if session is not None:
                conversation_id = session.external_id
        except Exception as exc:
            logger.debug(
                "Failed to load session %s for SessionContext: %s",
                resolved_session_id,
                exc,
            )
        tokens.session_token = set_session_context(
            SessionContext(session_id=resolved_session_id, conversation_id=conversation_id)
        )
        tokens.resolved_session_id = resolved_session_id

    def _minimal_project_token() -> contextvars.Token[dict[str, Any] | None] | None:
        """Last-resort project context when enrichment fails or db is missing."""
        if not canonical_project_id:
            return None
        return set_project_context({"id": canonical_project_id})

    project_token: contextvars.Token[dict[str, Any] | None] | None = None
    if resolved_session_id:
        if project_ref_is_fallback:
            # HTTP header semantics: session-derived wins; header is the fallback.
            if db is not None and session_manager is not None:
                try:
                    project_token = set_project_context_from_session(
                        resolved_session_id, session_manager, db
                    )
                except Exception as exc:
                    logger.debug(
                        "set_project_context_from_session failed for %s: %s",
                        resolved_session_id,
                        exc,
                    )
            if project_token is None and canonical_project_id:
                if db is not None:
                    try:
                        project_token = set_project_context_from_ref(canonical_project_id, db)
                    except Exception as exc:
                        logger.debug(
                            "set_project_context_from_ref failed for %s: %s",
                            canonical_project_id,
                            exc,
                        )
                if project_token is None:
                    project_token = _minimal_project_token()
        else:
            # Override mode: explicit project_ref beats session-derived.
            if canonical_project_id:
                if db is not None:
                    try:
                        project_token = set_project_context_from_ref(canonical_project_id, db)
                    except Exception as exc:
                        logger.debug(
                            "set_project_context_from_ref failed for %s: %s",
                            canonical_project_id,
                            exc,
                        )
                if project_token is None:
                    # Honor the override intent even without db enrichment.
                    project_token = _minimal_project_token()
            elif db is not None and session_manager is not None:
                try:
                    project_token = set_project_context_from_session(
                        resolved_session_id, session_manager, db
                    )
                except Exception as exc:
                    logger.debug(
                        "set_project_context_from_session failed for %s: %s",
                        resolved_session_id,
                        exc,
                    )
    elif canonical_project_id:
        if db is not None:
            try:
                project_token = set_project_context_from_ref(canonical_project_id, db)
            except Exception as exc:
                logger.debug(
                    "set_project_context_from_ref failed for %s: %s",
                    canonical_project_id,
                    exc,
                )
        if project_token is None:
            project_token = _minimal_project_token()

    tokens.project_token = project_token
    return tokens


def _resolve_context_values(
    session_ref: str | None,
    session_manager: SessionManager | None,
    *,
    project_ref: str | None = None,
    session_scope_ref: str | None = None,
    session_ref_origin: Literal["explicit", "ambient"] = "explicit",
    project_ref_is_fallback: bool = False,
    db: HubDatabase | None = None,
) -> tuple[str | None, str | None, SessionContext | None, dict[str, Any] | None]:
    """Resolve DB-backed values in a worker and capture its seeded contexts."""
    from gobby.utils.project_context import get_project_context

    worker_tokens = _resolve_and_seed_contexts_sync(
        session_ref=session_ref,
        session_manager=session_manager,
        project_ref=project_ref,
        session_scope_ref=session_scope_ref,
        session_ref_origin=session_ref_origin,
        project_ref_is_fallback=project_ref_is_fallback,
        db=db,
    )
    try:
        project_context = get_project_context()
        return (
            worker_tokens.resolved_session_id,
            worker_tokens.resolved_project_id,
            get_session_context(),
            dict(project_context) if project_context is not None else None,
        )
    finally:
        reset_seeded_contexts(worker_tokens)


async def resolve_and_seed_contexts(
    session_ref: str | None,
    session_manager: SessionManager | None,
    *,
    project_ref: str | None = None,
    session_scope_ref: str | None = None,
    session_ref_origin: Literal["explicit", "ambient"] = "explicit",
    project_ref_is_fallback: bool = False,
    db: HubDatabase | None = None,
) -> SeededContextTokens:
    """Resolve blocking context data off-loop and seed the calling async task."""
    from gobby.utils.project_context import set_project_context

    (
        resolved_session_id,
        resolved_project_id,
        session_context,
        project_context,
    ) = await asyncio.to_thread(
        _resolve_context_values,
        session_ref,
        session_manager,
        project_ref=project_ref,
        session_scope_ref=session_scope_ref,
        session_ref_origin=session_ref_origin,
        project_ref_is_fallback=project_ref_is_fallback,
        db=db,
    )
    tokens = SeededContextTokens(
        resolved_session_id=resolved_session_id,
        resolved_project_id=resolved_project_id,
    )
    if session_context is not None:
        tokens.session_token = set_session_context(session_context)
    if project_context is not None:
        tokens.project_token = set_project_context(project_context)
    return tokens


def reset_seeded_contexts(tokens: SeededContextTokens) -> None:
    """Reset any ContextVar tokens that were set. Safe on empty/partial tokens."""
    from gobby.utils.project_context import reset_project_context

    if tokens.agent_run_token is not None:
        try:
            reset_current_agent_run_id(tokens.agent_run_token)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.debug("reset_current_agent_run_id failed: %s", exc)
    if tokens.session_token is not None:
        try:
            reset_session_context(tokens.session_token)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.debug("reset_session_context failed: %s", exc)
    if tokens.project_token is not None:
        try:
            reset_project_context(tokens.project_token)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.debug("reset_project_context failed: %s", exc)
