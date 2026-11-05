"""Daemon activation guard and the project's primary checkout lookup.

The release window and its lease are retired: nothing acquires one, so landings and new
team admission no longer wait on a window. What remains is the guard that keeps a
spawned agent from stopping, starting, restarting or cutting over the daemon, and the
lookup that resolves a project's primary checkout for landing.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import psycopg

from gobby.cli.runtime import CliRuntime
from gobby.config.bootstrap import BootstrapConfigError
from gobby.storage.project_checkouts import require_root
from gobby.utils.daemon_url import DaemonUrlError, validate_daemon_url
from gobby.utils.local_token import (
    GOBBY_AGENT_API_TOKEN_ENV,
    AgentApiTokenClaims,
    classify_agent_api_token,
    operator_bootstrap_path,
    read_managed_api_token,
    read_managed_signing_key,
)
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_resolver import session_has_capability

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

ACTIVATE_DAEMON = "activate_daemon"
# Stop and start are never admitted for a spawned identity; only these can be granted.
_GRANTABLE_OPERATIONS = frozenset({"restart", "cutover"})
# A marked caller's restart is decided by the daemon route; only cutover, which has no route,
# is verified on the seat itself.
_LOCALLY_VERIFIED_OPERATIONS = frozenset({"cutover"})
# The API token is a run credential that only a spawned seat is given; daemon_auth_headers would
# send it as a bearer, so its presence marks the caller as spawned rather than as the operator.
_SPAWNED_ENV_NAMES = (
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    GOBBY_AGENT_API_TOKEN_ENV,
)
_REFUSED = "spawned_agent_refused"


def claims_may_activate(db: HubDatabase, claims: AgentApiTokenClaims, operation: str) -> bool:
    """Whether a verified managed identity's installed definition admits a daemon operation.

    Only restart and cutover can be admitted, and only for an identity bound to a run or
    managed execution whose session resolves to an enabled installed definition that holds
    activate_daemon. Anything unresolved is refused.
    """
    if operation not in _GRANTABLE_OPERATIONS:
        return False
    if claims.agent_run_id is None and claims.managed_execution_id is None:
        return False
    return session_has_capability(db, claims.session_id, ACTIVATE_DAEMON)


def operator_credential_readable() -> bool:
    """Whether this process can open the operator's bootstrap credential at its canonical path.

    The path comes from the passwd entry only, so no environment variable can point this check at
    a file the caller chose. Readability is the test, not validity: a malformed bootstrap still
    belongs to the operator, and the start path reports its error. The sandbox denies this file to
    spawned seats, so a caller that cleared its markers and cannot open it is refused.
    """
    path = operator_bootstrap_path()
    if path is None:
        return False
    try:
        with path.open("rb"):
            pass
    except OSError:
        return False
    return True


def caller_is_spawned() -> bool:
    """Whether this process carries any spawn marker. The markers are evidence, never a grant."""
    return any(os.environ.get(name, "").strip() for name in _SPAWNED_ENV_NAMES)


def agent_activation_refusal(operation: str) -> str | None:
    """Refuse a daemon activation step from a spawned agent unless its verified identity admits it.

    A caller with no spawn marker passes only when it can read the operator's bootstrap
    credential. A cleared environment is not evidence of the operator, so it is refused when
    that credential is unreadable. The run id in the environment is only a marker and grants
    nothing. A marked caller is refused for stop and start. A marked restart is never admitted
    here: the daemon route decides it with the daemon's own key (spawned_restart_refusal then
    posts it). Cutover has no route, so a marked caller's cutover verifies its managed
    capability locally.
    """
    if not caller_is_spawned():
        if operator_credential_readable():
            return None
        return (
            f"{_REFUSED}: no spawn marker and no readable operator credential; "
            "daemon activation follows the configured owner"
        )
    if operation == "restart":
        return f"{_REFUSED}: a spawned seat's restart is decided by the daemon route, not locally"
    if operation not in _LOCALLY_VERIFIED_OPERATIONS:
        return spawned_whole_daemon_refusal()
    return _verified_refusal(operation)


def spawned_whole_daemon_refusal() -> str:
    """The refusal for a marked caller's stop or start, which no spawned seat may run."""
    return (
        f"{_REFUSED}: spawned agents may not stop or start the daemon; "
        "activation follows the configured owner"
    )


def spawned_restart_refusal() -> str | None:
    """Refuse a marked caller's restart before it posts, without reading the operator bootstrap.

    The seat must present its private managed envelope and the daemon URL it was spawned with.
    Nothing here grants the restart: the route verifies the presented token with the daemon's
    key and the installed definition, and refuses before any stop.
    """
    try:
        token = read_managed_api_token()
    except RuntimeError as exc:
        return f"{_REFUSED}: {exc}"
    if token is None:
        return f"{_REFUSED}: no managed capability is presented for restart"
    try:
        url = validate_daemon_url(os.environ.get("GOBBY_DAEMON_URL", ""), source="GOBBY_DAEMON_URL")
    except DaemonUrlError:
        return f"{_REFUSED}: no daemon URL is presented for restart"
    if urlsplit(url).port is None:
        return f"{_REFUSED}: no daemon URL is presented for restart"
    return None


def _verified_refusal(operation: str) -> str | None:
    """Verify the presented managed capability and its installed definition, failing closed."""
    try:
        token = read_managed_api_token()
    except RuntimeError as exc:
        return f"{_REFUSED}: {exc}"
    if token is None:
        return f"{_REFUSED}: no managed capability is presented for {operation}"
    claims = classify_agent_api_token(token, read_managed_signing_key())
    if not isinstance(claims, AgentApiTokenClaims):
        return f"{_REFUSED}: managed capability is {claims}"
    try:
        admitted = _admitted_by_installed_definition(claims, operation)
    except (BootstrapConfigError, OSError, RuntimeError, psycopg.Error) as exc:
        logger.warning("Activation grant unresolved for %s: %s", operation, exc)
        return f"{_REFUSED}: the verified identity could not be resolved for {operation}"
    if not admitted:
        return f"{_REFUSED}: the verified definition does not hold {ACTIVATE_DAEMON}"
    return None


def _admitted_by_installed_definition(claims: AgentApiTokenClaims, operation: str) -> bool:
    runtime = CliRuntime(config_file=None)
    try:
        return claims_may_activate(
            runtime.require_database(apply_migrations=False), claims, operation
        )
    finally:
        runtime.close()


def project_checkout_root(db: HubDatabase, project_id: str) -> str:
    """The machine's primary checkout root. It blocks on the database: run it on a thread."""
    return require_root(db, project_id, require_machine_id())
