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

import psycopg

from gobby.cli.runtime import CliRuntime
from gobby.config.bootstrap import BootstrapConfigError
from gobby.storage.project_checkouts import require_root
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    classify_agent_api_token,
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
_SPAWNED_ENV_NAMES = (
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
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


def agent_activation_refusal(operation: str) -> str | None:
    """Refuse a daemon activation step from a spawned agent unless its verified identity admits it.

    The operator's pane, the web UI and unspawned coordination carry no spawned marker and pass.
    The run id in the environment is only that marker and grants nothing. A spawned caller is
    refused for stop and start, and for restart and cutover unless the managed capability it
    presents verifies and its installed definition holds activate_daemon.
    """
    if not any(os.environ.get(name, "").strip() for name in _SPAWNED_ENV_NAMES):
        return None
    if operation not in _GRANTABLE_OPERATIONS:
        return (
            f"{_REFUSED}: spawned agents may not stop or start the daemon; "
            "activation follows the configured owner"
        )
    return _verified_refusal(operation)


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
