"""Restore an archived bootstrap.yaml during `gobby unpack`.

The destination owns its PostgreSQL credentials and its machine-bound API key.
The archived key pair survives only under --restore-identity, together with the
archived machine_id it is bound to.
"""

from __future__ import annotations

import tarfile
import uuid
from collections.abc import Iterable
from pathlib import Path

import click

from gobby.cli.hub_backup.files_home import (
    archived_bootstrap,
    merge_bootstrap_preserving_files_home,
)
from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.bootstrap_io import read_bootstrap_yaml, validated_bootstrap_payload


def preview_archived_bootstrap(
    target: Path, content: bytes, dest_files_home: Path, *, restore_identity: bool
) -> bool:
    """Validate the bootstrap unpack would publish; return whether it names an API key."""
    try:
        destination = read_bootstrap_yaml(target)
        restored = validated_bootstrap_payload(
            destination,
            archived_bootstrap(
                destination, content, dest_files_home, restore_identity=restore_identity
            ),
        )
    except BootstrapConfigError as exc:
        raise click.ClickException(f"Cannot restore bootstrap.yaml: {exc}") from exc
    return bool(restored.get("api_key"))


def require_archived_machine_id(
    tar: tarfile.TarFile, restores: Iterable[tuple[tarfile.TarInfo, Path, str]]
) -> None:
    """Refuse a restored API key unless the archive carries the machine_id it is bound to."""
    member = next((member for member, _target, label in restores if label == "machine_id"), None)
    archived = tar.extractfile(member) if member else None
    try:
        uuid.UUID((archived.read() if archived else b"").decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise click.ClickException(
            "Restoring the archived api_key requires the archived machine_id "
            "(gobby/machine_id) to be present and a UUID"
        ) from exc


def restore_archived_bootstrap(
    target: Path, content: bytes, dest_files_home: Path, *, restore_identity: bool
) -> None:
    """Publish the archived bootstrap; tell an unenrolled remote node to log in again."""
    merge_bootstrap_preserving_files_home(
        target, content, dest_files_home, restore_identity=restore_identity
    )
    restored = read_bootstrap_yaml(target)
    if restored.get("datastore_mode") == "remote" and not restored.get("api_key"):
        click.echo("  This machine holds no hub API key; run `gobby auth login` to enroll it.")
