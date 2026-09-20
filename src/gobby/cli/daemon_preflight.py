"""Prove the start half of a restart before anything is stopped or promoted.

``gobby restart`` and ``gobby cutover`` are stop-then-start with no way back:
``gobby stop`` runs ``launchctl bootout``, so launchd cannot revive the daemon, and
after a cutover the checkout pin matches the new binaries, so rolling the binaries
back is not available either. Both commands therefore run this preflight first and
leave the running daemon alone on refusal.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.install.bin_set_coherence import BinarySetCoherenceError, probe_set_member_identity
from gobby.paths import get_install_dir
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.schema_contract import SchemaContractError, expected_schema_identity, plan_schema
from gobby.sync.integrity import dirty_bundled_content_refusal
from gobby.utils.dev import worktree_daemon_refusal

logger = logging.getLogger(__name__)


def restart_start_refusal(
    ctx: click.Context,
    gdaemon: Path | None = None,
    *,
    expected_identity: dict[str, int | str] | None = None,
) -> str | None:
    """Name the reason the start half would fail, or None.

    ``gdaemon=None`` proves the installed set, which is what ``restart`` will start.
    A candidate path proves an unpromoted build, which is what ``cutover`` is about to
    promote. An unreadable hub URL skips the schema plan and leaves the start unproven,
    the same "unreadable is not a refusal" convention ``schema_apply_refusal`` follows.
    The candidate identity check is the one place that fails closed, because an
    unverifiable candidate must never be promoted.
    """
    from gobby.storage.schema_divergence import binary_set_apply_refusal, schema_apply_refusal

    if refusal := worktree_daemon_refusal():
        return refusal

    database: HubDatabase | None = None
    if gdaemon is None:
        if refusal := binary_set_apply_refusal():
            return refusal
        database = _open_hub(ctx)
        refusal = (
            schema_apply_refusal(database)
            if expected_identity is None
            else schema_apply_refusal(database, expected_identity=expected_identity)
        )
        if refusal:
            return refusal
    elif refusal := _candidate_identity_refusal(gdaemon, expected_identity=expected_identity):
        return refusal

    if refusal := dirty_bundled_content_refusal(get_install_dir(), database=database):
        return refusal

    url = _hub_url(database)
    if url is None:
        logger.debug("Restart preflight skipped the schema plan: no readable hub URL")
        return None
    try:
        plan_schema(url, gdaemon=gdaemon)
    except SchemaContractError as exc:
        return str(exc)
    return None


def _candidate_identity_refusal(
    gdaemon: Path,
    *,
    expected_identity: dict[str, int | str] | None = None,
) -> str | None:
    """Fail closed: anything the probe or expected pin cannot read is a refusal."""
    try:
        candidate = probe_set_member_identity(gdaemon, "gdaemon")
        expected = (
            expected_identity if expected_identity is not None else expected_schema_identity()
        )
    except (BinarySetCoherenceError, SchemaContractError) as exc:
        return f"candidate gdaemon cannot be verified: {exc}"
    if candidate != expected:
        return (
            f"candidate gdaemon identity {_render_identity(candidate)} does not match "
            f"the checkout pin {_render_identity(expected)}"
        )
    return None


def _render_identity(identity: dict[str, int | str]) -> str:
    return f"v{identity['latest_version']} ({str(identity['latest_checksum'])[:12]})"


def _open_hub(ctx: click.Context) -> HubDatabase | None:
    from gobby.cli.runtime import require_cli_database

    try:
        return require_cli_database(ctx, apply_migrations=False)
    except Exception:
        logger.debug("Restart preflight could not open the hub", exc_info=True)
        return None


def _hub_url(database: HubDatabase | None) -> str | None:
    """Resolve the URL the started daemon will use, or None when none is readable.

    The opened hub's ``conninfo`` is the string the daemon itself uses. Candidate mode
    never opens the hub, so it resolves the bootstrap ``database_url`` instead, which
    is the URL gdaemon's own ``resolve_database_url`` picks for the daemon to come.
    """
    if database is not None:
        conninfo = getattr(database, "conninfo", None)
        if isinstance(conninfo, str) and conninfo.strip():
            return conninfo
    try:
        return load_bootstrap().database_url or None
    except (BootstrapConfigError, OSError) as exc:
        logger.debug("Restart preflight could not read the bootstrap database_url: %s", exc)
        return None
