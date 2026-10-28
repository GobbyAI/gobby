"""gobby-agents release-window tools: the Release Manager's lease on landings and new teams."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.releases.store import HubReleaseLease
from gobby.releases.window import (
    RELEASE_ROLES,
    ReleaseWindow,
    ReleaseWindowRefused,
    acquire_window,
    release_window,
    renew_window,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.tasks.lifecycle_access import session_agent_definition


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


def _is_run_id(value: object) -> bool:
    """An agent run id is a UUID string; any other value is refused before storage sees it."""
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def register_release_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    """Register acquire, renew and end for the release window, and the batch's fenced reads.

    The batch's reads carry the Release Manager's token and generation and answer only
    ids, the checkout root and the candidate, or a refusal code. Credentials never cross.
    """

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

    def batch_lease(token: str, generation: int) -> HubReleaseLease:
        """The caller's lease for a batch read; every read fences on token and generation."""
        db, session_id, project_id = release_caller()
        return HubReleaseLease(
            db,
            project_id=project_id,
            owner_session_id=session_id,
            token=token,
            generation=generation,
        )

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

    @registry.tool(
        name="release_batch_checkout",
        description=(
            "Release Manager only. The release batch's checkout: the main checkout root and "
            "the candidate the live window pins. token and generation must match the stored "
            "lease. Refuses release_window_stale on any mismatch or after expiry."
        ),
    )
    async def release_batch_checkout(token: str, generation: int) -> dict[str, Any]:
        try:
            found = await batch_lease(token, generation).checkout()
        except _Forbidden as exc:
            return _refusal("release_window_forbidden", str(exc))
        except ReleaseWindowRefused as exc:
            return _refusal(exc.code, str(exc))
        return {"success": True, "root": str(found.root), "candidate_sha": found.candidate_sha}

    @registry.tool(
        name="release_batch_active_runs",
        description=(
            "Release Manager only. The machine's active agent run ids that are not already "
            "reconciliation-fenced, captured before the batch activates the daemon. token and "
            "generation must match the stored lease."
        ),
    )
    async def release_batch_active_runs(token: str, generation: int) -> dict[str, Any]:
        try:
            lease = batch_lease(token, generation)
            await lease.require_live()
            run_ids = await lease.active_runs()
        except _Forbidden as exc:
            return _refusal("release_window_forbidden", str(exc))
        except ReleaseWindowRefused as exc:
            return _refusal(exc.code, str(exc))
        return {"success": True, "run_ids": sorted(run_ids)}

    @registry.tool(
        name="release_batch_fenced_runs",
        description=(
            "Release Manager only. The subset of run_ids that reconciliation has fenced since "
            "the batch captured them. token and generation must match the stored lease."
        ),
    )
    async def release_batch_fenced_runs(
        token: str, generation: int, run_ids: list[str]
    ) -> dict[str, Any]:
        try:
            lease = batch_lease(token, generation)
            await lease.require_live()
            if not all(_is_run_id(run_id) for run_id in run_ids):
                return _refusal("release_run_ids_invalid", "run_ids must be agent run UUIDs")
            fenced = await lease.fenced_runs(frozenset(run_ids))
        except _Forbidden as exc:
            return _refusal("release_window_forbidden", str(exc))
        except ReleaseWindowRefused as exc:
            return _refusal(exc.code, str(exc))
        return {"success": True, "run_ids": sorted(fenced)}
