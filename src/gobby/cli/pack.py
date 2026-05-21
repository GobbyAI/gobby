"""
CLI commands for portable export/import of Gobby data.

`gobby pack` creates a tarball of all Gobby state for machine migration.
`gobby unpack` restores from a pack tarball on a new machine.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import click

from gobby.cli.installers.git_hooks import install_git_hooks
from gobby.cli.postgres_backup import (
    POSTGRES_BACKUP_ARCHIVE_PREFIX,
    backup_payload_paths,
    create_postgres_backup,
    postgres_backup_configured,
    restore_postgres_backup,
)
from gobby.cli.utils import get_gobby_home, stop_daemon

DB_NAME = "gobby-hub.db"


# Directories to include in pack (relative to ~/.gobby/)
PACK_DIRS = [
    "session_transcripts",
    "session_summaries",
    "services",
    "hooks",
    "certs",
    "canvas",
    "scripts",
]

# Files to include (relative to ~/.gobby/)
PACK_FILES = [
    DB_NAME,
    "bootstrap.yaml",
    "machine_id",
    ".secret_salt",
]

# Docker volumes to export
DOCKER_VOLUMES = [
    "gobby_neo4j_data",
    "gobby_qdrant_data",
]


def _docker_available() -> bool:
    """Check if Docker CLI is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _volume_exists(volume_name: str) -> bool:
    """Check if a Docker volume exists."""
    try:
        result = subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _export_docker_volume(volume_name: str, output_path: Path) -> bool:
    """Export a Docker volume to a tar.gz file."""
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume_name}:/source:ro",
                "-v",
                f"{output_path.parent}:/backup",
                "alpine",
                "tar",
                "czf",
                f"/backup/{output_path.name}",
                "-C",
                "/source",
                ".",
            ],
            capture_output=True,
            timeout=120,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _import_docker_volume(volume_name: str, archive_path: Path) -> bool:
    """Import a tar.gz file into a Docker volume."""
    try:
        # Create volume if it doesn't exist
        subprocess.run(
            ["docker", "volume", "create", volume_name],
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume_name}:/target",
                "-v",
                f"{archive_path.parent}:/backup:ro",
                "alpine",
                "sh",
                "-c",
                f"rm -rf /target/* && tar xzf /backup/{archive_path.name} -C /target",
            ],
            capture_output=True,
            timeout=120,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _daemon_is_running() -> bool:
    """Check if the Gobby daemon is currently running."""
    pid_file = get_gobby_home() / "gobby.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _stop_services() -> bool:
    """Stop Docker services (Qdrant, Neo4j) for consistent snapshots."""
    services_dir = get_gobby_home() / "services"
    compose_file = services_dir / "docker-compose.yml"

    # Fall back to legacy Neo4j compose
    if not compose_file.exists():
        compose_file = services_dir / "neo4j" / "docker-compose.yml"
        if not compose_file.exists():
            return False

    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--profile",
                "all",
                "down",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(services_dir),
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _start_services() -> None:
    """Start Docker services via daemon's lifecycle management."""
    from gobby.cli.daemon import _services_start

    _services_start(get_gobby_home())


# Aliases — _stop_services/_start_services handle all Docker services (Qdrant, Neo4j)
_stop_docker_services = _stop_services
_start_docker_services = _start_services


def _start_daemon() -> None:
    """Start the Gobby daemon via the service manager."""
    from gobby.cli.installers.service import get_service_status, service_start

    svc = get_service_status()
    if svc.get("installed"):
        service_start()
    else:
        # Fallback: direct process start
        try:
            subprocess.Popen(
                [sys.executable, "-m", "gobby.runner"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            pass


def _get_pack_size_estimate() -> int:
    """Estimate total size of data to pack."""
    total = 0
    for f in PACK_FILES:
        path = get_gobby_home() / f
        if path.exists():
            total += path.stat().st_size
    for d in PACK_DIRS:
        path = get_gobby_home() / d
        if path.is_dir():
            for root, _, files in os.walk(path):
                for name in files:
                    total += (Path(root) / name).stat().st_size
    return total


@click.command("pack")
@click.argument("output", required=False, type=click.Path())
@click.option("--no-docker", is_flag=True, help="Skip Docker volume export (Neo4j + Qdrant)")
@click.option("--no-transcripts", is_flag=True, help="Skip session transcript archives")
@click.option("--dry-run", is_flag=True, help="Show what would be packed without creating archive")
def pack(output: str | None, no_docker: bool, no_transcripts: bool, dry_run: bool) -> None:
    """Pack all Gobby data into a portable archive for machine migration.

    Creates a tarball containing local configs, session transcripts, vector
    store data, Docker volume data (Neo4j + Qdrant), a logical PostgreSQL dump
    when configured, and the legacy SQLite hub file when present.

    \b
    Usage:
        gobby pack                          # Auto-named archive
        gobby pack ~/backup/gobby.tar.gz    # Custom path
        gobby pack --no-docker              # Skip Docker volume export
        gobby pack --dry-run                # Preview what would be packed
    """
    if not get_gobby_home().exists():
        click.echo("No ~/.gobby directory found. Nothing to pack.", err=True)
        sys.exit(1)

    if output is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        output = f"gobby-pack-{timestamp}.tar.gz"

    output_path = Path(output).resolve()

    # Collect items to pack
    items: list[tuple[str, Path]] = []
    missing: list[str] = []

    for f in PACK_FILES:
        path = get_gobby_home() / f
        if path.exists():
            items.append((f"gobby/{f}", path))
        else:
            missing.append(f)

    pack_dirs = list(PACK_DIRS)
    if no_transcripts:
        pack_dirs = [d for d in pack_dirs if d != "session_transcripts"]

    for d in pack_dirs:
        path = get_gobby_home() / d
        if path.is_dir():
            items.append((f"gobby/{d}", path))
        else:
            missing.append(f"{d}/")

    # Project-level .gobby directories
    # Pack the current project's .gobby/ if it exists
    cwd_gobby = Path.cwd() / ".gobby"
    if cwd_gobby.is_dir():
        items.append(("project-gobby", cwd_gobby))

    # Docker volumes
    docker_volumes_to_export: list[str] = []
    if not no_docker and _docker_available():
        for vol in DOCKER_VOLUMES:
            if _volume_exists(vol):
                docker_volumes_to_export.append(vol)
    include_postgres = postgres_backup_configured(gobby_home=get_gobby_home())

    if dry_run:
        click.echo("Pack contents (dry run):\n")
        total_size = 0
        for archive_name, path in items:
            if path.is_file():
                size = path.stat().st_size
                total_size += size
                click.echo(f"  {archive_name} ({_human_size(size)})")
            elif path.is_dir():
                dir_size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                total_size += dir_size
                file_count = sum(1 for f in path.rglob("*") if f.is_file())
                click.echo(f"  {archive_name}/ ({_human_size(dir_size)}, {file_count} files)")
        for vol in docker_volumes_to_export:
            click.echo(f"  docker-volumes/{vol}.tar.gz (size unknown)")
        if include_postgres:
            for archive_name, _path in backup_payload_paths(Path()):
                click.echo(f"  {archive_name} (created during pack)")
        if missing:
            click.echo(f"\nSkipped (not found): {', '.join(missing)}")
        click.echo(f"\nEstimated size: {_human_size(total_size)} (before compression)")
        return

    click.echo(f"Packing Gobby data to {output_path}...")

    # Stop daemon for consistent DB snapshot
    daemon_was_running = _daemon_is_running()
    if daemon_was_running:
        click.echo("  Stopping daemon for consistent snapshot...")
        stop_daemon(quiet=True)

    # Stop Docker services for consistent volume export
    services_were_running = False
    if docker_volumes_to_export:
        services_were_running = _stop_docker_services()
        if services_were_running:
            click.echo("  Stopped Docker services")

    try:
        _do_pack(output_path, items, docker_volumes_to_export, missing, include_postgres)
    finally:
        # Restart services that were running
        if services_were_running:
            click.echo("  Restarting Docker services...")
            _start_docker_services()
        if daemon_was_running:
            click.echo("  Restarting daemon...")
            _start_daemon()


def _do_pack(
    output_path: Path,
    items: list[tuple[str, Path]],
    docker_volumes_to_export: list[str],
    missing: list[str],
    include_postgres: bool,
) -> None:
    """Inner pack logic, separated for try/finally lifecycle management."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Export Docker volumes to temp dir
        for vol in docker_volumes_to_export:
            click.echo(f"  Exporting Docker volume: {vol}...")
            vol_archive = tmp / f"{vol}.tar.gz"
            if _export_docker_volume(vol, vol_archive):
                items.append((f"gobby/docker-volumes/{vol}.tar.gz", vol_archive))
                click.echo(f"    Done ({_human_size(vol_archive.stat().st_size)})")
            else:
                click.echo(f"    Warning: Failed to export {vol}", err=True)

        if include_postgres:
            click.echo("  Creating PostgreSQL logical backup...")
            backup_dir = tmp / "postgres"
            backup = create_postgres_backup(output_dir=backup_dir, gobby_home=get_gobby_home())
            for archive_name, path in backup_payload_paths(backup_dir):
                items.append((archive_name, path))
            click.echo(f"    Done ({_human_size(Path(backup['dump_path']).stat().st_size)})")

        # Write manifest
        manifest = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "hostname": os.uname().nodename,
            "items": [name for name, _ in items],
            "docker_volumes": docker_volumes_to_export,
            "postgres_backup": include_postgres,
        }
        manifest_path = tmp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        items.insert(0, ("gobby/manifest.json", manifest_path))

        # Create tarball
        with tarfile.open(output_path, "w:gz") as tar:
            for archive_name, path in items:
                click.echo(f"  Adding: {archive_name}")
                tar.add(str(path), arcname=archive_name)

    final_size = output_path.stat().st_size
    click.echo(f"\nPacked: {output_path} ({_human_size(final_size)})")
    if missing:
        click.echo(f"Skipped (not found): {', '.join(missing)}")


@click.command("unpack")
@click.argument("archive", type=click.Path(exists=True))
@click.option("--no-docker", is_flag=True, help="Skip Docker volume import (Neo4j + Qdrant)")
@click.option("--no-postgres", is_flag=True, help="Skip PostgreSQL logical dump restore")
@click.option("--dry-run", is_flag=True, help="Show what would be unpacked without extracting")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing data without prompting",
)
def unpack(
    archive: str,
    no_docker: bool,
    no_postgres: bool,
    dry_run: bool,
    force: bool,
) -> None:
    """Unpack a Gobby archive to restore data on a new machine.

    Restores local configs, session transcripts, vector store data, Docker
    volume data (Neo4j + Qdrant), PostgreSQL logical dump data when present,
    and the legacy SQLite hub file when present.

    \b
    Usage:
        gobby unpack gobby-pack-20260316.tar.gz
        gobby unpack backup.tar.gz --no-docker
        gobby unpack backup.tar.gz --no-postgres
        gobby unpack backup.tar.gz --dry-run
    """
    archive_path = Path(archive).resolve()
    services_started = False

    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()

        # Read manifest if present
        manifest = None
        try:
            manifest_member = tar.getmember("gobby/manifest.json")
            f = tar.extractfile(manifest_member)
            if f:
                manifest = json.loads(f.read())
        except KeyError:
            pass

        if dry_run:
            click.echo(f"Archive: {archive_path} ({_human_size(archive_path.stat().st_size)})\n")
            if manifest:
                click.echo(f"Created: {manifest.get('created_at', 'unknown')}")
                click.echo(f"Source host: {manifest.get('hostname', 'unknown')}")
                if manifest.get("docker_volumes"):
                    click.echo(f"Docker volumes: {', '.join(manifest['docker_volumes'])}")
                click.echo()
            click.echo("Contents:")
            for member in members:
                if member.isfile():
                    click.echo(f"  {member.name} ({_human_size(member.size)})")
                elif member.isdir():
                    click.echo(f"  {member.name}/")
            return

        # Safety check
        if get_gobby_home().exists() and not force:
            existing_db = get_gobby_home() / DB_NAME
            if existing_db.exists():
                if not click.confirm(
                    f"Warning: {existing_db} already exists. "
                    "This will overwrite your existing Gobby data. Continue?"
                ):
                    click.echo("Aborted.")
                    sys.exit(0)

        click.echo(f"Unpacking {archive_path}...")
        if manifest:
            click.echo(f"  Source: {manifest.get('hostname', 'unknown')}")
            click.echo(f"  Created: {manifest.get('created_at', 'unknown')}")

        # Stop services before overwriting data
        daemon_was_running = _daemon_is_running()
        if daemon_was_running:
            click.echo("  Stopping daemon...")
            stop_daemon(quiet=True)

        services_were_running = _stop_docker_services()
        if services_were_running:
            click.echo("  Stopped Docker services")

        # Backup existing DB if present
        existing_db = get_gobby_home() / DB_NAME
        if existing_db.exists():
            backup_name = f"{DB_NAME}.pre-unpack-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
            backup_path = get_gobby_home() / backup_name
            shutil.copy2(existing_db, backup_path)
            click.echo(f"  Backed up existing DB to {backup_name}")

        # Extract gobby/ contents to ~/.gobby/
        get_gobby_home().mkdir(parents=True, exist_ok=True)
        docker_archives: list[tarfile.TarInfo] = []
        postgres_members: list[tarfile.TarInfo] = []

        for member in members:
            if member.name == "gobby/manifest.json":
                # Save manifest but don't need to extract to ~/.gobby
                continue

            if member.name.startswith("gobby/docker-volumes/"):
                docker_archives.append(member)
                continue

            if member.name.startswith(f"{POSTGRES_BACKUP_ARCHIVE_PREFIX}/"):
                postgres_members.append(member)
                continue

            if member.name.startswith("project-gobby"):
                # Project-level .gobby — extract to cwd
                rel = member.name.removeprefix("project-gobby")
                if rel.startswith("/"):
                    rel = rel[1:]
                target = Path.cwd() / ".gobby"
                if rel:
                    target = target / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(member)
                    if f:
                        target.write_bytes(f.read())
                click.echo(f"  Restored: .gobby/{rel}")
                continue

            if member.name.startswith("gobby/"):
                rel = member.name.removeprefix("gobby/")
                target = get_gobby_home() / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(member)
                    if f:
                        target.write_bytes(f.read())
                click.echo(f"  Restored: {rel}")

        # Import Docker volumes
        if not no_docker and docker_archives:
            if not _docker_available():
                click.echo(
                    "\n  Warning: Docker not available, skipping volume import.",
                    err=True,
                )
            else:
                with tempfile.TemporaryDirectory() as tmpdir:
                    for member in docker_archives:
                        vol_filename = Path(member.name).name
                        vol_name = vol_filename.removesuffix(".tar.gz")
                        click.echo(f"  Importing Docker volume: {vol_name}...")

                        f = tar.extractfile(member)
                        if f:
                            tmp_archive = Path(tmpdir) / vol_filename
                            tmp_archive.write_bytes(f.read())
                            if _import_docker_volume(vol_name, tmp_archive):
                                click.echo("    Done")
                            else:
                                click.echo(
                                    f"    Warning: Failed to import {vol_name}",
                                    err=True,
                                )

        if postgres_members and no_postgres:
            click.echo("  Skipped PostgreSQL restore")
        elif postgres_members:
            if services_were_running or (not no_docker and docker_archives):
                click.echo("  Starting Docker services before PostgreSQL restore...")
                _start_docker_services()
                services_started = True
            with tempfile.TemporaryDirectory() as tmpdir:
                postgres_dir = Path(tmpdir) / "postgres"
                postgres_dir.mkdir()
                for member in postgres_members:
                    rel = member.name.removeprefix(f"{POSTGRES_BACKUP_ARCHIVE_PREFIX}/")
                    target = postgres_dir / rel
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(member)
                    if f:
                        target.write_bytes(f.read())
                restore_postgres_backup(postgres_dir, gobby_home=get_gobby_home())
                click.echo("  Restored PostgreSQL logical dump")

    # Reinstall git hooks from templates (ensures they match current version)
    if (Path.cwd() / ".git").exists():
        click.echo("  Installing git hooks...")
        hook_result = install_git_hooks(Path.cwd(), force=True, setup_precommit=False)
        if hook_result["success"]:
            click.echo(f"    Installed: {', '.join(hook_result['installed'])}")
        else:
            click.echo(f"    Warning: {hook_result['error']}", err=True)

    # Restart services
    if not services_started and (services_were_running or (not no_docker and docker_archives)):
        click.echo("  Starting Docker services...")
        _start_docker_services()
    if daemon_was_running:
        click.echo("  Restarting daemon...")
        _start_daemon()

    click.echo("\nUnpack complete.")


def _human_size(size: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size //= 1024
    return f"{size:.1f}TB"
