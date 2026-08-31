"""CLI command for syncing bundled content to the database.

Provides ``gobby sync`` with options for integrity verification,
selective syncing, and force mode.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Protocol

import click
import httpx

from gobby.storage.hub.protocol import HubDatabase

from .utils import get_install_dir

logger = logging.getLogger(__name__)

VALID_CONTENT_TYPES = {"skills", "prompts", "rules", "agents", "workflows"}
CONTENT_TYPE_SYNC_TARGETS: dict[str, set[str]] = {
    "skills": {"skills"},
    "prompts": {"prompts"},
    "rules": {"rules"},
    "agents": {"agents"},
    "workflows": {"pipelines", "variables", "build_profiles"},
}
REINSTALL_KINDS = ("rules", "agents", "pipelines", "variables")
REINSTALL_TARGETS: dict[str, set[str]] = {
    "rules": {"rules"},
    "agents": {"agents"},
    "pipelines": {"pipelines"},
    "variables": {"variables"},
    "all": set(REINSTALL_KINDS),
}


def _running_daemon_install_dir() -> Path | None:
    """Return the bundled-content directory of the running daemon, or None when none answers."""
    from gobby.cli.utils_config import get_daemon_url

    try:
        response = httpx.get(f"{get_daemon_url()}/api/health", timeout=2.0)
        payload = response.json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        logger.debug("No running daemon answered the bundled-source check: %s", exc)
        return None
    if response.status_code != 200 or not isinstance(payload, dict):
        return None
    install_dir = payload.get("install_dir")
    if not isinstance(install_dir, str) or not install_dir:
        return None
    return Path(install_dir)


def _check_bundled_source_checkout(install_dir: Path, *, force: bool) -> None:
    """Refuse to overwrite the shared bundled rows from a checkout the daemon does not serve.

    Every session reads the installed rows the running daemon serves, and the daemon
    keeps running its own checkout's code against them. Syncing from another checkout
    (a worktree on a branch) swaps the content without swapping that code, so it is
    refused unless ``--force`` makes the overwrite deliberate. No reachable daemon means
    nothing to conflict with.
    """
    daemon_dir = _running_daemon_install_dir()
    if daemon_dir is None or daemon_dir.resolve() == install_dir.resolve():
        return
    if not force:
        click.echo(
            "The running daemon serves bundled content from\n"
            f"  {daemon_dir}\n"
            "and this checkout would sync from\n"
            f"  {install_dir}\n"
            "Every session reads the shared rows the daemon serves, and the daemon keeps "
            "running its own code against them. Run `gobby sync` from the daemon's checkout, "
            "or pass --force to overwrite the shared rows anyway.",
            err=True,
        )
        sys.exit(1)
    click.echo(
        "WARNING: --force is overwriting the shared bundled rows from\n"
        f"  {install_dir}\n"
        "while the running daemon serves\n"
        f"  {daemon_dir}\n"
        "Every active session now reads this checkout's content; the daemon's next restart "
        "or reload_cache reverts it.",
        err=True,
    )


@click.command("sync")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Skip integrity check even in production mode, and overwrite the shared bundled "
        "rows from a checkout the running daemon does not serve."
    ),
)
@click.option("--verify-only", is_flag=True, help="Only run integrity check, don't sync.")
@click.option(
    "--fail-on-verify",
    is_flag=True,
    help="With --verify-only, exit non-zero when integrity verification fails.",
)
@click.option(
    "--type",
    "types",
    multiple=True,
    type=click.Choice(sorted(VALID_CONTENT_TYPES), case_sensitive=False),
    help="Sync only specific content types (repeatable).",
)
@click.option("--verbose", is_flag=True, help="Show per-type details.")
@click.option(
    "--reinstall",
    type=click.Choice([*REINSTALL_KINDS, "all"], case_sensitive=False),
    default=None,
    help="Hard-delete bundled installed definitions and re-sync that domain.",
)
def sync(
    force: bool,
    verify_only: bool,
    fail_on_verify: bool,
    types: tuple[str, ...],
    verbose: bool,
    reinstall: str | None,
) -> None:
    """Sync bundled content (skills, prompts, rules, agents, workflows) to the database.

    In dev mode, syncs freely without integrity checks.
    In production mode, verifies git integrity first and blocks tampered types.
    """
    from gobby.utils.dev import is_dev_mode

    dev_mode = is_dev_mode(Path.cwd())
    install_dir = get_install_dir()
    skip_types: set[str] | None = None
    tampered_types: set[str] = set()

    # --- Integrity check ---
    if not dev_mode and not force:
        from gobby.sync.integrity import (
            BUNDLED_SYNC_CONTENT_TYPES,
            get_dirty_content_types,
            verify_bundled_integrity,
        )

        click.echo("Verifying bundled content integrity...")
        result = verify_bundled_integrity(install_dir)

        if not result.checked:
            click.echo("  Integrity verification unavailable; blocking bundled content sync")
            for err in result.errors:
                click.echo(f"  {err}", err=True)
            skip_types = set(BUNDLED_SYNC_CONTENT_TYPES)
            tampered_types = set(BUNDLED_SYNC_CONTENT_TYPES)
        else:
            if not result.git_available and result.source == "manifest" and verbose:
                click.echo("  Git not available; verified packaged manifest")
            if result.all_clean:
                click.echo("  All bundled content is clean")
            else:
                if result.dirty_files:
                    click.echo(f"  Modified files ({len(result.dirty_files)}):")
                    for f in result.dirty_files:
                        click.echo(f"    {f}")
                if result.untracked_files:
                    click.echo(f"  Untracked files ({len(result.untracked_files)}):")
                    for f in result.untracked_files:
                        click.echo(f"    {f}")

                tampered = get_dirty_content_types(
                    result.dirty_files + result.untracked_files, install_dir
                )
                if tampered:
                    skip_types = tampered
                    tampered_types = set(skip_types)
                    click.echo(f"  Blocking tampered content types: {', '.join(sorted(tampered))}")

        if verify_only:
            sys.exit(1 if fail_on_verify and not (result.checked and result.all_clean) else 0)
    elif dev_mode and not force:
        if verbose:
            click.echo("Dev mode: skipping integrity check")
    elif force:
        if verbose:
            click.echo("Force mode: skipping integrity check")

    if verify_only:
        # In dev mode or force mode with --verify-only, nothing to report
        click.echo("No integrity check performed (dev mode or --force)")
        sys.exit(0)

    # --- Filter to requested types ---
    only: set[str] | None = None
    if reinstall:
        only = set(REINSTALL_TARGETS[reinstall])
    elif types:
        from gobby.sync.integrity import BUNDLED_SYNC_CONTENT_TYPES

        requested = _sync_targets_for_cli_types(set(types))
        if skip_types:
            skip_types = (BUNDLED_SYNC_CONTENT_TYPES - requested) | (skip_types & requested)
        else:
            skip_types = BUNDLED_SYNC_CONTENT_TYPES - requested

    _check_bundled_source_checkout(install_dir, force=force)

    # --- Initialize DB and sync ---
    from gobby.cli.installers.shared import sync_bundled_content_to_db
    from gobby.cli.runtime import require_cli_database

    try:
        db = require_cli_database()
    except RuntimeError as exc:
        click.echo(f"Database unavailable: {exc}", err=True)
        sys.exit(1)

    if reinstall:
        type_label = reinstall
        selected = only or set()
        blocked = set() if force else selected & (skip_types or set())
        if blocked:
            click.echo(
                "Cannot reinstall types blocked by integrity check: "
                f"{', '.join(sorted(blocked))}. Use --force to override.",
                err=True,
            )
            sys.exit(1)
        if not force:
            click.confirm(
                f"This will delete and reinstall only bundled {type_label} definitions. "
                "User and project definitions will be preserved. Continue?",
                abort=True,
            )
        click.echo("Syncing bundled content to database...")
        deleted, sync_result = _reinstall_bundled_definitions(db, selected, skip_types=skip_types)
        click.echo(f"Deleted {deleted} existing definitions")
    else:
        click.echo("Syncing bundled content to database...")
        sync_result = sync_bundled_content_to_db(db, only=only, skip_types=skip_types)

    total = sync_result["total_synced"]
    errors = sync_result["errors"]

    if total > 0:
        click.echo(f"Synced {total} bundled items to database")
    else:
        click.echo("No changes to sync")

    if verbose and sync_result.get("details"):
        for content_type, detail in sync_result["details"].items():
            synced = detail.get("synced", 0) + detail.get("updated", 0)
            if synced > 0:
                click.echo(f"  {content_type}: {synced} items")

    if tampered_types:
        skipped = tampered_types
        if types:
            skipped = skipped & _sync_targets_for_cli_types(set(types))
        if skipped:
            click.echo(f"Skipped tampered types: {', '.join(sorted(skipped))}")

    if errors:
        disposition_errors = [str(err) for err in errors if "delivery disposition:" in str(err)]
        if disposition_errors:
            raise click.ClickException("\n".join(str(err) for err in errors))
        for err in errors:
            click.echo(f"  Warning: {err}", err=True)
        sys.exit(1)


def _sync_targets_for_cli_types(types: set[str]) -> set[str]:
    targets: set[str] = set()
    for content_type in types:
        targets.update(CONTENT_TYPE_SYNC_TARGETS.get(content_type, []))
    return targets


class _InstalledDefinitionManager(Protocol):
    def list_all(self) -> list[Any]: ...
    def hard_delete(self, definition_id: str) -> bool: ...


def _reinstall_bundled_definitions(
    db: HubDatabase,
    kinds: set[str],
    *,
    skip_types: set[str] | None,
) -> tuple[int, dict[str, Any]]:
    """Delete and replace one selected domain per transaction.

    A failed replacement rolls back that domain so prior bundled rows remain.
    """
    from gobby.sync_registry import sync_bundled_content_to_db

    deleted = 0
    merged: dict[str, Any] = {"total_synced": 0, "errors": [], "details": {}}
    for kind in sorted(kinds):
        try:
            with db.transaction():
                kind_deleted = _delete_installed_definitions(db, {kind})
                result = sync_bundled_content_to_db(db, only={kind}, skip_types=skip_types)
                errors = result.get("errors") or []
                if errors:
                    raise RuntimeError("; ".join(str(item) for item in errors))
                deleted += kind_deleted
                merged["total_synced"] += int(result.get("total_synced") or 0)
                merged["details"].update(result.get("details") or {})
        except Exception as exc:
            merged["errors"].append(f"Failed to reinstall bundled {kind}: {exc}")
    return deleted, merged


def _delete_installed_definitions(db: HubDatabase, kinds: set[str]) -> int:
    """Hard-delete bundled installed rows for the requested domain kinds."""
    from gobby.storage.definitions.agents import AgentDefinitionManager
    from gobby.storage.definitions.pipelines import PipelineDefinitionManager
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.storage.definitions.variables import SessionVariableDefaultManager

    deleted = 0
    if "rules" in kinds:
        deleted += _hard_delete_installed(RuleDefinitionManager(db))
    if "agents" in kinds:
        deleted += _hard_delete_installed(AgentDefinitionManager(db))
    if "pipelines" in kinds:
        deleted += _hard_delete_installed(PipelineDefinitionManager(db))
    if "variables" in kinds:
        deleted += _hard_delete_installed(SessionVariableDefaultManager(db))
    return deleted


def _hard_delete_installed(manager: _InstalledDefinitionManager) -> int:
    deleted = 0
    for row in manager.list_all():
        if row.source != "installed":
            continue
        if manager.hard_delete(str(row.id)):
            deleted += 1
    return deleted
