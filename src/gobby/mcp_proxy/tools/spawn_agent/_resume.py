"""Validate an explicit native-thread relaunch before spawn allocations."""

from typing import Any

from gobby.agents.resume_executor import SUPPORTED_RESUME_PROVIDERS
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES


def resolve_resume_target(
    session_manager: SessionManager | None,
    ref: str,
    project_id: str | None,
    provider: str | None,
    run_storage: Any,
) -> Session:
    if session_manager is None or not project_id:
        raise ValueError("Resume requires a session manager and project context")
    session_id = session_manager.resolve_session_reference(ref, project_id)
    session = session_manager.get(session_id)
    if session is None:
        raise ValueError(f"Resume target {ref} does not exist")
    if session.project_id != project_id:
        raise ValueError(f"Resume target {ref} belongs to another project")
    if session.source not in SUPPORTED_RESUME_PROVIDERS:
        raise ValueError(f"Provider {session.source!r} does not support native resume")
    if provider and provider != session.source:
        raise ValueError(f"Resume target uses {session.source}, not requested provider {provider}")
    if session.status in LIVE_SESSION_STATUSES:
        raise ValueError(f"Resume target {ref} is still live ({session.status})")
    if session.agent_run_id:
        run = run_storage.get(session.agent_run_id)
        if run is not None and run.status in ACTIVE_AGENT_RUN_STATUSES:
            raise ValueError(f"Resume target {ref} still has a live agent run ({run.status})")
    if session.status == "deleted":
        raise ValueError(f"Resume target {ref} is deleted")
    thread = session.external_id
    if (
        not thread
        or thread.startswith("agent-")
        or (thread == session.id and session.source != "claude")
    ):
        raise ValueError(f"Resume target {ref} has no recorded provider thread")
    return session
