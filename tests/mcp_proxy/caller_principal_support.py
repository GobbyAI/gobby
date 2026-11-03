"""Bind the /mcp request caller in tests the way the route does.

/mcp proxy tools refuse a session_id the bound caller cannot speak for, and an
unbound caller speaks for no session. Tests that call those tools directly bind
the caller first.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import (
    RequestPrincipal,
    SessionContext,
    reset_request_principal,
    set_request_principal,
)

FIXTURE_PROJECT_ID = "11111111-1111-1111-1111-111111111111"


@contextmanager
def bound_principal(principal: RequestPrincipal) -> Iterator[None]:
    """Bind ``principal`` as the authenticated /mcp caller."""

    async def resolve() -> RequestPrincipal:
        return principal

    token = set_request_principal(resolve)
    try:
        yield
    finally:
        reset_request_principal(token)


@contextmanager
def operator_caller() -> Iterator[None]:
    """Bind the operator, who may name any session."""
    with bound_principal(None):
        yield


@contextmanager
def session_ref_resolves_to(
    session_id: str, *, project_id: str = FIXTURE_PROJECT_ID
) -> Iterator[None]:
    """Make every caller session ref resolve to ``session_id`` in ``project_id``."""
    resolved = (session_id, project_id, SessionContext(session_id=session_id), {"id": project_id})
    with patch("gobby.utils.session_context._resolve_context_values", return_value=resolved):
        yield


def session_claims(
    session_id: str,
    *,
    project_id: str = "",
    machine_id: str = "local-machine",
    agent_run_id: str | None = None,
) -> AgentApiTokenClaims:
    """Managed-agent claims for the caller that owns ``session_id``."""
    return AgentApiTokenClaims(
        session_id=session_id,
        project_id=project_id,
        machine_id=machine_id,
        iat=1_700_000_000,
        exp=1_700_003_600,
        agent_run_id=agent_run_id,
    )
