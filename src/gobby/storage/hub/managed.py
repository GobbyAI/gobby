"""Grant-scoped hub access for managed executions.

A managed execution (agent run, tool chat, maintenance) receives
``GOBBY_MANAGED_EXECUTION_BOOTSTRAP`` pointing at its signed grant file and never
sees the operator's ``bootstrap.yaml``: the sandbox denies that root of trust
(`sandbox_policy.sensitive_roots`). The Rust binaries resolve their hub access
from the grant; the Python CLI follows the same contract here so ``gobby``
commands run inside a managed execution with the grant's privileges — the
per-execution PostgreSQL role inherits ``gobby_gcode_capability`` and RLS pins it
to the grant's project — and nothing more.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path

from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG, PostgresPoolConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV

MANAGED_GRANT_EXPIRED = (
    "managed execution grant has expired; the daemon issues a fresh grant per run"
)


def managed_grant_path(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the managed grant file when this process is a managed execution."""
    raw = (os.environ if env is None else env).get(MANAGED_EXECUTION_BOOTSTRAP_ENV, "")
    if not raw.strip():
        return None
    return Path(raw).expanduser()


def managed_hub_database(
    grant_path: Path,
    *,
    pool_config: PostgresPoolConfig = DEFAULT_POSTGRES_POOL_CONFIG,
    now: int | None = None,
) -> HubDatabase:
    """Open the hub through a managed grant's direct PostgreSQL capability.

    Migrations and personal-project seeding are daemon-owned and never run here:
    the scoped role cannot perform them, and a managed execution must not try.
    """
    from pydantic import ValidationError

    from gobby.runtime_grants.schema import GrantBundle
    from gobby.storage.hub.postgres import PostgresHubDatabase

    try:
        text = grant_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapConfigError(
            f"cannot read managed execution grant {grant_path}: {exc}"
        ) from exc
    try:
        grant = GrantBundle.model_validate_json(text)
    except ValidationError as exc:
        raise BootstrapConfigError(
            f"managed execution grant {grant_path} is malformed: {exc}"
        ) from exc

    current = int(time.time()) if now is None else now
    if grant.expires_at <= current:
        raise BootstrapConfigError(MANAGED_GRANT_EXPIRED)
    postgres = grant.capabilities.postgres
    if postgres.mode != "direct":
        raise BootstrapConfigError(
            "managed execution grant carries no direct PostgreSQL capability "
            f"(mode={postgres.mode}); hub access is unavailable to this execution"
        )
    if postgres.valid_until <= current:
        raise BootstrapConfigError(MANAGED_GRANT_EXPIRED)
    return PostgresHubDatabase(postgres.dsn, pool_config=pool_config)
