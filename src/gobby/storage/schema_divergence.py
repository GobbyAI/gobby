"""Read-only comparison of the three schema heads a checkout can observe.

One installed ``~/.gobby/bin/gdaemon`` is shared by every checkout and worktree on
the machine, so the moment any branch adds a migration the other checkouts disagree
with it, and both can disagree with the live hub. Observability commands must report
that divergence instead of failing at the identity gate, which is exactly when the
divergence most needs seeing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.schema_contract import SchemaContractError, expected_schema_identity
from gobby.storage.schema_identity_pin import SchemaIdentityError, validate_identity
from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

_VERSION_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SchemaHeads:
    """The checkout pin, the installed binary's embedded pin, and the live hub head."""

    checkout_version: int | None
    installed_version: int | None
    live_version: int | None

    @property
    def diverged(self) -> bool:
        """True when the heads that could be read do not all agree."""
        observed = {
            version
            for version in (self.checkout_version, self.installed_version, self.live_version)
            if version is not None
        }
        return len(observed) > 1

    def describe(self) -> str:
        """Render one dashboard line naming all three heads."""

        def render(version: int | None) -> str:
            return "unreadable" if version is None else f"v{version}"

        summary = (
            f"checkout pins {render(self.checkout_version)} · "
            f"installed gdaemon {render(self.installed_version)} · "
            f"live hub {render(self.live_version)}"
        )
        if self.diverged:
            return f"{summary} — DIVERGED"
        return summary


def installed_schema_identity() -> dict[str, int | str] | None:
    """Read the installed gdaemon's embedded identity, or None when unreadable.

    ``gdaemon schema version`` deliberately does not enforce the expected identity,
    so this stays readable while the checkout pin and the binary disagree.
    """
    binary = resolve_native_bin("gdaemon")
    if binary is None:
        logger.debug("gdaemon is not installed; cannot read its embedded schema identity")
        return None
    try:
        result = subprocess.run(
            [binary, "schema", "version", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Failed to read installed gdaemon schema identity: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug(
            "gdaemon schema version exited %s: %s",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        return None
    try:
        return validate_identity(json.loads(result.stdout))
    except (json.JSONDecodeError, SchemaIdentityError) as exc:
        logger.debug("gdaemon schema version returned an unusable identity: %s", exc)
        return None


def live_schema_version(database: HubDatabase) -> int | None:
    """Read the live hub's applied schema head, or None when unreadable."""
    try:
        with database.transaction() as conn:
            row = conn.execute("SELECT MAX(version) AS head FROM schema_migrations").fetchone()
    except Exception as exc:
        logger.debug("Failed to read the live schema head: %s", exc)
        return None
    if row is None:
        return None
    value = row["head"]
    return value if isinstance(value, int) else None


def _checkout_version() -> int | None:
    try:
        value = expected_schema_identity()["latest_version"]
    except SchemaContractError as exc:
        logger.debug("Packaged schema identity is unreadable: %s", exc)
        return None
    return value if isinstance(value, int) else None


def collect_schema_heads(database: HubDatabase | None) -> SchemaHeads:
    """Gather every head that can be read without applying or verifying schema."""
    installed = installed_schema_identity()
    installed_version = installed["latest_version"] if installed is not None else None
    return SchemaHeads(
        checkout_version=_checkout_version(),
        installed_version=installed_version if isinstance(installed_version, int) else None,
        live_version=None if database is None else live_schema_version(database),
    )
