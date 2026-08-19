"""Local-install files_home publication and singleton-held identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from gobby.cli.daemon_singleton import format_singleton_status
from gobby.config.bootstrap import BootstrapConfigError, validate_existing_files_home
from gobby.config.bootstrap_io import (
    bootstrap_path,
    inject_local_files_home,
    read_bootstrap_yaml,
)
from gobby.paths import get_gobby_home
from gobby.runner_pid_file import (
    PidFileClaim,
    ProbeState,
    claim_pid_file,
    probe_daemon_lock,
)
from gobby.storage.projects import ensure_personal_project_identity


def peek_install_bootstrap() -> dict[str, Any]:
    """Raw-read bootstrap without load_bootstrap validation."""
    path = bootstrap_path()
    if not path.exists():
        return {}
    return read_bootstrap_yaml(path)


def resolve_install_files_home(
    files_home: Path | None,
    *,
    datastore_mode: str,
    existing_files_home: str | None,
    no_interactive: bool,
) -> Path | None:
    """Validate or prompt for a local files_home. Remote install takes none."""
    if datastore_mode == "remote":
        if files_home is not None:
            raise click.UsageError("--files-home is not allowed on a remote install")
        return None
    if files_home is not None:
        try:
            return validate_existing_files_home(files_home)
        except BootstrapConfigError as exc:
            raise click.UsageError(str(exc)) from exc
    if existing_files_home:
        return Path(existing_files_home)
    if no_interactive:
        raise click.UsageError(
            "Local install requires --files-home naming an existing absolute directory"
        )
    prompted = click.prompt(
        "Existing files home directory",
        type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    )
    try:
        return validate_existing_files_home(prompted)
    except BootstrapConfigError as exc:
        raise click.UsageError(str(exc)) from exc


def publish_install_files_home(files_home: Path) -> dict[str, Any]:
    """Persist files_home before identity or managed services."""
    from gobby.cli.install_setup import ensure_daemon_config

    path = bootstrap_path()
    if path.exists():
        data = read_bootstrap_yaml(path)
        if not data.get("files_home"):
            inject_local_files_home(path, files_home)
            return {"created": False, "path": str(path), "upgraded": True}
        return {"created": False, "path": str(path)}
    return ensure_daemon_config(files_home=files_home)


def local_install_requires_maintenance(*, datastore_mode: str, full_install: bool) -> bool:
    """Exclusive maintenance is only for full/config-only local install."""
    return datastore_mode == "local" and full_install


def _install_maintenance_block_message(pid_file: Path) -> str:
    probe = probe_daemon_lock(pid_file)
    status = format_singleton_status(probe)
    if probe.state is ProbeState.DAEMON:
        return (
            f"Could not claim the daemon singleton for install; {status}. "
            "Stop the daemon with `gobby stop` before a full install that "
            "publishes files_home or personal identity."
        )
    if probe.state is ProbeState.MAINTENANCE:
        return (
            f"Could not claim the daemon singleton for install; {status}. "
            "A concurrent maintenance campaign is already live."
        )
    if probe.state is ProbeState.LIVE_RESERVATION:
        return (
            f"Could not claim the daemon singleton for install; {status}. "
            "A concurrent start campaign is already live."
        )
    return f"Could not claim the daemon singleton for install; {status}"


def acquire_install_maintenance() -> PidFileClaim:
    """Hold the singleton for bootstrap-plus-identity publication."""
    pid_file = get_gobby_home() / "gobby.pid"
    claim = claim_pid_file(pid_file, role="maintenance")
    if claim is None:
        raise click.ClickException(_install_maintenance_block_message(pid_file))
    return claim


def prepare_local_install_identity(files_home: Path) -> tuple[PidFileClaim, Path]:
    """Claim maintenance, persist files_home, then write the identity marker."""
    claim = acquire_install_maintenance()
    try:
        publish_install_files_home(files_home)
        marker = ensure_personal_project_identity()
    except Exception:
        claim.release()
        raise
    return claim, marker
