"""gobby-agents release-window tools: the Release Manager's lease on landings and new teams."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.releases.window import (
    ReleaseWindow,
    ReleaseWindowRefused,
    acquire_window,
    release_window,
    renew_window,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.tasks.lifecycle_access import session_agent_definition

RELEASE_ROLES = frozenset({"release-manager"})


class _Forbidden(Exception):
    """The caller cannot hold a release window."""


def _window_payload(window: ReleaseWindow) -> dict[str, Any]:
    return {
        "owner_session_id": window.owner_session_id,
        "token": window.token,
        "candidate_sha": window.candidate_sha,
        "branch": window.branch,
        "started_at": window.started_at.isoformat(),
        "expires_at": window.expires_at.isoformat(),
        "generation": window.generation,
    }


def _refusal(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": code}


def register_release_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    """Register acquire, renew and end for the caller project's release window."""

    def release_caller() -> tuple[HubDatabase, str, str]:
        """Return the hub, the calling Release Manager's session and its project."""
        db = ctx.db
        caller_ref = ctx.get_current_session_id()
        if db is None or not caller_ref:
            raise _Forbidden("release window tools need a database and a caller session")
        try:
            session_id = ctx.resolve_session_id(caller_ref)
        except ValueError as exc:
            raise _Forbidden(str(exc)) from exc
        session = SessionManager(db).get(session_id)
        if session is None or session.project_id is None:
            raise _Forbidden("the calling session has no project")
        definition = session_agent_definition(db, session)
        if definition is None or definition[0].name not in RELEASE_ROLES:
            raise _Forbidden("only the Release Manager holds the release window")
        return db, session.id, session.project_id

    @registry.tool(
        name="acquire_release_window",
        description=(
            "Release Manager only. Take the project's release window: landings and new-team "
            "admissions wait while it is live. Pins candidate_sha to the integration tip read "
            "under the landing lock. The lease expires 10 minutes after acquire or the last "
            "renew; renew at least every 60 seconds. Returns the window with its token and "
            "generation. Refuses release_window_active while another lease is live and "
            "release_window_unreadable for malformed state (an operator deletes "
            "<git common dir>/gobby/release-window.json to recover)."
        ),
    )
    async def acquire_release_window() -> dict[str, Any]:
        try:
            db, session_id, project_id = release_caller()
            window = await acquire_window(
                db, project_id=project_id, owner_session_id=session_id, now=datetime.now(UTC)
            )
        except _Forbidden as exc:
            return _refusal("release_window_forbidden", str(exc))
        except ReleaseWindowRefused as exc:
            return _refusal(exc.code, str(exc))
        return {"success": True, "window": _window_payload(window)}

    @registry.tool(
        name="renew_release_window",
        description=(
            "Release Manager only. Extend the caller's live release window by 10 minutes. "
            "token and generation must match the stored lease; each renew returns the next "
            "generation, which the following renew or end must use. Refuses "
            "release_window_stale on any mismatch or after expiry and release_window_missing "
            "when no lease is held."
        ),
    )
    async def renew_release_window(token: str, generation: int) -> dict[str, Any]:
        try:
            db, session_id, project_id = release_caller()
            window = await renew_window(
                db,
                project_id=project_id,
                owner_session_id=session_id,
                token=token,
                generation=generation,
                now=datetime.now(UTC),
            )
        except _Forbidden as exc:
            return _refusal("release_window_forbidden", str(exc))
        except ReleaseWindowRefused as exc:
            return _refusal(exc.code, str(exc))
        return {"success": True, "window": _window_payload(window)}

    @registry.tool(
        name="end_release_window",
        description=(
            "Release Manager only. Remove the caller's live release window so landings and "
            "new-team admissions resume. token and generation must match the stored lease; "
            "a stale owner never clears a successor's window."
        ),
    )
    async def end_release_window(token: str, generation: int) -> dict[str, Any]:
        try:
            db, session_id, project_id = release_caller()
            await release_window(
                db,
                project_id=project_id,
                owner_session_id=session_id,
                token=token,
                generation=generation,
                now=datetime.now(UTC),
            )
        except _Forbidden as exc:
            return _refusal("release_window_forbidden", str(exc))
        except ReleaseWindowRefused as exc:
            return _refusal(exc.code, str(exc))
        return {"success": True}
