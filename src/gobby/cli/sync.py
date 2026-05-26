"""CLI command for syncing bundled content to the database.

Provides ``gobby sync`` with options for integrity verification,
selective syncing, and force mode.
"""

import logging
import sys
from pathlib import Path

import click

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


@click.command("sync")
@click.option("--force", is_flag=True, help="Skip integrity check even in production mode.")
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
def sync(
    force: bool,
    verify_only: bool,
    fail_on_verify: bool,
    types: tuple[str, ...],
    verbose: bool,
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
    if types:
        from gobby.sync.integrity import BUNDLED_SYNC_CONTENT_TYPES

        requested = _sync_targets_for_cli_types(set(types))
        if skip_types:
            skip_types = (BUNDLED_SYNC_CONTENT_TYPES - requested) | (skip_types & requested)
        else:
            skip_types = BUNDLED_SYNC_CONTENT_TYPES - requested

    # --- Initialize DB and sync ---
    from gobby.cli.installers.shared import sync_bundled_content_to_db
    from gobby.storage.hub.runtime import open_runtime_hub_database

    try:
        db = open_runtime_hub_database(apply_migrations=False)
    except RuntimeError as exc:
        click.echo(f"Database unavailable: {exc}", err=True)
        sys.exit(1)

    click.echo("Syncing bundled content to database...")
    sync_result = sync_bundled_content_to_db(db, skip_types=skip_types)

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
        for err in errors:
            click.echo(f"  Warning: {err}", err=True)
        sys.exit(1)


def _sync_targets_for_cli_types(types: set[str]) -> set[str]:
    targets: set[str] = set()
    for content_type in types:
        targets.update(CONTENT_TYPE_SYNC_TARGETS.get(content_type, []))
    return targets
