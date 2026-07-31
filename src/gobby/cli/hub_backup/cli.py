"""`gobby hub-backup`: stop-consistent hub backup with verified restore.

This module owns orchestration only. Artifacts are produced by the drivers in
`_stores`, proven restorable by the scratch restores in `_verify`, and recorded
in the schema-validated manifest from `_manifest`. What lives here is the order
those run in and the guarantees around them: the daemon is down for the whole
backup, the Docker services come back even when archiving fails, and nothing
secret reaches the manifest or stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 # fixed docker inspect argv, never shell=True
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from gobby.cli.daemon import ServiceStartResult, _services_start, _services_stop
from gobby.cli.hub_backup._manifest import (
    MANIFEST_NAME,
    ArtifactRecord,
    HubBackupManifest,
    StoreRecord,
    VerificationState,
    write_manifest,
)
from gobby.cli.hub_backup._stores import (
    FALKORDB_DUMP_RELPATH,
    GLOBALS_DUMP_RELPATH,
    HUB_VOLUMES,
    POSTGRES_DUMP_RELPATH,
    VOLUME_ARCHIVE_DIR,
    collect_postgres_identity,
    collect_row_count_probes,
    collect_source_roles,
    dump_falkordb,
    dump_postgres,
    snapshot_qdrant,
    tar_volumes,
)
from gobby.cli.hub_backup._verify import (
    RoleExpectation,
    verify_falkordb_restore,
    verify_postgres_restore,
    verify_qdrant_restore,
    verify_volume_archives,
)
from gobby.cli.installers.compose_env import (
    MANAGED_SERVICE_PROFILES,
    ComposeEnvironmentError,
    ComposeRuntime,
    resolve_compose_runtime,
)
from gobby.cli.installers.container_restart import (
    FALKORDB_CONTAINER,
    POSTGRES_CONTAINER,
    QDRANT_CONTAINER,
)
from gobby.cli.postgres_backup import (
    _process_output,
    _require_managed_docker_postgres,
    _resolve_database_url,
)
from gobby.cli.runtime import get_cli_runtime
from gobby.cli.utils_shutdown import stop_daemon
from gobby.paths import get_gobby_home
from gobby.storage.maintenance_epoch import (
    MAINTENANCE_EPOCH_ENV,
    require_orchestrator_epoch,
)
from gobby.utils.version import get_version

# `gobby-postgres-test-1` is deliberately absent: the test cluster is scratch
# state and is never part of a hub backup.
REQUIRED_CONTAINERS: tuple[str, ...] = (
    POSTGRES_CONTAINER,
    QDRANT_CONTAINER,
    FALKORDB_CONTAINER,
)

MIN_FREE_BYTES = 5 * 1024**3
OUTPUT_DIR_MODE = 0o700
DOCKER_INSPECT_TIMEOUT_SECONDS = 30
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
COMPOSE_START_TIMEOUT_SECONDS = 120

_EPOCH_COMPOSE_OVERRIDE = """\
services:
  postgres:
    environment:
      PGOPTIONS: "${PGOPTIONS:?PGOPTIONS must carry the maintenance epoch}"
"""

POSTGRES_ARCHIVE_METHOD = "pg-restore-list+sha256"
QDRANT_ARCHIVE_METHOD = "snapshot-download+sha256"
FALKORDB_ARCHIVE_METHOD = "bgsave-rdb-copy+sha256"
VOLUMES_ARCHIVE_METHOD = "tar-archive+sha256"


@click.command("hub-backup")
@click.option(
    "--output",
    "output",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Backup directory (default: ~/.gobby/backups/hub/<UTC timestamp>).",
)
@click.option(
    "--epoch",
    "epoch",
    type=str,
    default=None,
    help="Maintenance epoch id to record; also leaves the daemon stopped afterwards.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print the manifest path and a store summary as JSON.",
)
@click.pass_context
def hub_backup(
    ctx: click.Context,
    output: Path | None,
    epoch: str | None,
    json_output: bool,
) -> None:
    """Back up every hub datastore and prove each artifact restores."""
    if epoch is not None:
        child_epoch = os.environ.get(MAINTENANCE_EPOCH_ENV)
        if child_epoch != epoch:
            raise click.ClickException(
                "`hub-backup --epoch` may only run as a child of "
                "`gobby hub-maintenance` for the same epoch"
            )

    gobby_home = get_gobby_home()
    backup_root = _resolve_output_dir(output)
    _preflight(backup_root)
    _create_output_dir(backup_root)

    database_url = _resolve_database_url(gobby_home)
    if epoch is not None:
        require_orchestrator_epoch(database_url, epoch)
    _require_managed_docker_postgres(database_url=database_url)
    qdrant_url, qdrant_api_key = _qdrant_settings(ctx, apply_migrations=epoch is None)

    # An open epoch owns the daemon lifecycle, so `--epoch` leaves it stopped.
    restart_daemon = _daemon_is_running() and epoch is None
    manifest_path = backup_root / MANIFEST_NAME
    try:
        stop_daemon(shutdown_source="cli_hub_backup")
        manifest = _run_backup(
            backup_root=backup_root,
            gobby_home=gobby_home,
            database_url=database_url,
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            epoch=epoch,
        )
        write_manifest(manifest, manifest_path)
    finally:
        if restart_daemon:
            _start_daemon()

    _emit_result(manifest, manifest_path, json_output=json_output)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _preflight(backup_root: Path) -> None:
    """Refuse to start unless Docker, the hub containers, and disk space are there."""
    if shutil.which("docker") is None:
        raise click.ClickException(
            "Docker CLI is unavailable; hub-backup needs Docker to reach the hub datastores"
        )
    for container in REQUIRED_CONTAINERS:
        if not _container_running(container):
            raise click.ClickException(
                f"Required container {container} is not running; "
                "start the hub with `gobby start` before backing it up"
            )
    free = _free_bytes(backup_root)
    if free < MIN_FREE_BYTES:
        raise click.ClickException(
            f"Insufficient free space for {backup_root}: {free} bytes available, "
            f"{MIN_FREE_BYTES} required"
        )


def _container_running(container: str) -> bool:
    try:
        result = subprocess.run(  # nosec B603 B607 # fixed docker CLI argv
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            timeout=DOCKER_INSPECT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return _process_output(result.stdout) == "true"


def _free_bytes(backup_root: Path) -> int:
    """Free bytes on the filesystem that will hold the backup, before it exists."""
    probe = backup_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------


def _resolve_output_dir(output: Path | None) -> Path:
    if output is not None:
        return output.expanduser()
    stamp = datetime.now(UTC).strftime(TIMESTAMP_FORMAT)
    return get_gobby_home() / "backups" / "hub" / stamp


def _create_output_dir(backup_root: Path) -> None:
    """Create the backup root owner-only, including every parent we create."""
    created = [path for path in (backup_root, *backup_root.parents) if not path.exists()]
    backup_root.mkdir(parents=True, exist_ok=True)
    for path in created:
        path.chmod(OUTPUT_DIR_MODE)
    backup_root.chmod(OUTPUT_DIR_MODE)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_backup(
    *,
    backup_root: Path,
    gobby_home: Path,
    database_url: str,
    qdrant_url: str | None,
    qdrant_api_key: str | None,
    epoch: str | None,
) -> HubBackupManifest:
    """Collect source facts, archive every store, verify each one, and describe it."""
    identity, starting_head = collect_postgres_identity(database_url)
    probes = collect_row_count_probes(database_url)
    roles = collect_source_roles(database_url)

    artifacts: list[ArtifactRecord] = []
    postgres_artifacts, postgres_details = dump_postgres(database_url, backup_root)
    artifacts.extend(postgres_artifacts)
    qdrant_artifacts, qdrant_details = snapshot_qdrant(qdrant_url, qdrant_api_key, backup_root)
    artifacts.extend(qdrant_artifacts)
    falkordb_artifacts, falkordb_details = dump_falkordb(backup_root)
    artifacts.extend(falkordb_artifacts)
    volume_artifacts, volume_details = _archive_volumes(gobby_home, backup_root)
    artifacts.extend(volume_artifacts)

    stores = _verify_stores(
        backup_root=backup_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        probes=probes,
        roles=roles,
        postgres_details=postgres_details,
        qdrant_details=qdrant_details,
        falkordb_details=falkordb_details,
        volume_details=volume_details,
    )
    return HubBackupManifest(
        created_at=datetime.now(UTC).isoformat(),
        gobby_version=get_version(),
        epoch_id=epoch,
        source_identity=identity,
        backup_starting_head=starting_head,
        row_count_probes=probes,
        artifacts=artifacts,
        stores=stores,
    )


def _archive_volumes(
    gobby_home: Path,
    backup_root: Path,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Archive the hub volumes cold, and bring the services back either way."""
    if not _services_stop(gobby_home):
        raise click.ClickException(
            "Could not stop the managed Docker services; refusing to archive live volumes"
        )
    try:
        artifacts, details = tar_volumes(backup_root)
    finally:
        restart = (
            _start_epoch_services(gobby_home)
            if os.environ.get(MAINTENANCE_EPOCH_ENV)
            else _services_start(gobby_home)
        )
    if restart.outcome != "success":
        raise click.ClickException(
            f"Docker services did not restart after archiving volumes: {restart.detail}"
        )
    return artifacts, details


def _start_epoch_services(gobby_home: Path) -> ServiceStartResult:
    """Start managed services with the epoch token available to Postgres healthchecks."""
    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"
    if not compose_file.exists():
        return ServiceStartResult("failed", f"Compose file is missing: {compose_file}")

    try:
        postgres_runtime = resolve_compose_runtime(gobby_home, profiles=("postgres",))
    except ComposeEnvironmentError as exc:
        return ServiceStartResult("failed", f"Could not resolve Docker service config: {exc}")
    postgres_result = _run_epoch_compose_up(compose_file, services_dir, postgres_runtime)
    if postgres_result.outcome != "success":
        return postgres_result

    try:
        runtime = resolve_compose_runtime(gobby_home)
    except ComposeEnvironmentError as exc:
        return ServiceStartResult("failed", f"Could not resolve Docker service config: {exc}")
    if runtime.profiles != MANAGED_SERVICE_PROFILES:
        return ServiceStartResult(
            "failed",
            "Docker service config must enable postgres, qdrant, and falkordb profiles",
        )
    return _run_epoch_compose_up(compose_file, services_dir, runtime)


def _run_epoch_compose_up(
    compose_file: Path,
    services_dir: Path,
    runtime: ComposeRuntime,
) -> ServiceStartResult:
    epoch = os.environ.get(MAINTENANCE_EPOCH_ENV, "")
    if epoch not in runtime.environment.get("PGOPTIONS", ""):
        return ServiceStartResult("failed", "PGOPTIONS does not carry the maintenance epoch")

    command = ["docker", "compose", "-f", str(compose_file), "-f", "-"]
    for profile in runtime.profiles:
        command.extend(["--profile", profile])
    command.extend(["up", "-d", "--remove-orphans", "--wait"])
    try:
        result = subprocess.run(  # nosec B603 - fixed Docker Compose arguments
            command,
            input=_EPOCH_COMPOSE_OVERRIDE,
            capture_output=True,
            text=True,
            timeout=COMPOSE_START_TIMEOUT_SECONDS,
            env=runtime.environment,
            cwd=str(services_dir),
        )
    except subprocess.TimeoutExpired:
        return ServiceStartResult(
            "failed",
            f"Docker compose up timed out after {COMPOSE_START_TIMEOUT_SECONDS}s",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ServiceStartResult("failed", f"Docker compose execution failed: {exc}")
    if result.returncode != 0:
        return ServiceStartResult(
            "failed",
            f"Docker compose up failed: {result.stderr or result.stdout}",
        )
    return ServiceStartResult("success", "Docker services started")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verify_stores(
    *,
    backup_root: Path,
    qdrant_url: str | None,
    qdrant_api_key: str | None,
    probes: dict[str, int],
    roles: list[dict[str, object]],
    postgres_details: dict[str, object],
    qdrant_details: dict[str, object],
    falkordb_details: dict[str, object],
    volume_details: dict[str, object],
) -> dict[str, StoreRecord]:
    """Prove each store restores and fold the proof into its manifest record."""
    postgres_state, postgres_proof = verify_postgres_restore(
        backup_root / POSTGRES_DUMP_RELPATH,
        backup_root / GLOBALS_DUMP_RELPATH,
        expected_probes=probes,
        expected_roles=_role_expectations(roles),
    )
    snapshots, expected_points = _qdrant_expectations(qdrant_details, backup_root)
    qdrant_state, qdrant_proof = verify_qdrant_restore(
        qdrant_url,
        qdrant_api_key,
        snapshots,
        expected_points,
    )
    falkordb_state, falkordb_proof = verify_falkordb_restore(
        backup_root / FALKORDB_DUMP_RELPATH,
        _expected_graphs(falkordb_details),
    )
    volumes_state, volumes_proof = verify_volume_archives(_volume_archives(backup_root))

    return {
        "postgres": _store_record(
            POSTGRES_ARCHIVE_METHOD, postgres_state, postgres_details, postgres_proof
        ),
        "qdrant": _store_record(QDRANT_ARCHIVE_METHOD, qdrant_state, qdrant_details, qdrant_proof),
        "falkordb": _store_record(
            FALKORDB_ARCHIVE_METHOD, falkordb_state, falkordb_details, falkordb_proof
        ),
        "volumes": _store_record(
            VOLUMES_ARCHIVE_METHOD, volumes_state, volume_details, volumes_proof
        ),
    }


def _store_record(
    archive_method: str,
    restore_verified: VerificationState,
    driver_details: dict[str, object],
    restore_details: dict[str, object],
) -> StoreRecord:
    return StoreRecord(
        archive_verified=_archive_verified(archive_method),
        restore_verified=restore_verified,
        details={**driver_details, **restore_details},
    )


def _archive_verified(method: str) -> VerificationState:
    """Every driver hashes what it wrote, so the archive is proven on arrival."""
    return VerificationState(
        verified=True,
        method=method,
        timestamp=datetime.now(UTC).isoformat(),
    )


def _role_expectations(roles: list[dict[str, object]]) -> list[RoleExpectation]:
    return [
        RoleExpectation(
            rolname=str(role["rolname"]),
            rolsuper=bool(role["rolsuper"]),
            rolcanlogin=bool(role["rolcanlogin"]),
        )
        for role in roles
    ]


def _qdrant_expectations(
    details: dict[str, object],
    backup_root: Path,
) -> tuple[dict[str, Path], dict[str, int]]:
    collections = details.get("collections")
    if not isinstance(collections, dict):
        raise click.ClickException("Qdrant snapshot driver reported no collections to verify")
    snapshots: dict[str, Path] = {}
    expected_points: dict[str, int] = {}
    for name, record in collections.items():
        if not isinstance(record, dict) or "snapshot" not in record or "points" not in record:
            raise click.ClickException(f"Qdrant snapshot record for {name} is incomplete")
        snapshots[str(name)] = backup_root / str(record["snapshot"])
        expected_points[str(name)] = int(record["points"])
    return snapshots, expected_points


def _expected_graphs(details: dict[str, object]) -> list[str]:
    graphs = details.get("graphs")
    if not isinstance(graphs, list):
        raise click.ClickException("FalkorDB dump driver reported no graph list to verify")
    return [str(graph) for graph in graphs]


def _volume_archives(backup_root: Path) -> dict[str, Path]:
    return {volume: backup_root / VOLUME_ARCHIVE_DIR / f"{volume}.tar.gz" for volume in HUB_VOLUMES}


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


def _daemon_is_running() -> bool:
    """Check the daemon PID file the same way `gobby pack` does."""
    pid_file = get_gobby_home() / "gobby.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _start_daemon() -> None:
    """Start the daemon via the service manager, falling back to a direct spawn."""
    from gobby.cli.installers.service import get_service_status, service_start

    if get_service_status().get("installed"):
        service_start()
        return
    try:
        subprocess.Popen(  # nosec B603 # sys.executable launches a fixed module
            [sys.executable, "-m", "gobby.runner"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        click.echo("Warning: could not restart the Gobby daemon", err=True)


# ---------------------------------------------------------------------------
# Configuration and reporting
# ---------------------------------------------------------------------------


def _qdrant_settings(
    ctx: click.Context,
    *,
    apply_migrations: bool = True,
) -> tuple[str | None, str | None]:
    qdrant = get_cli_runtime(ctx).require_config(apply_migrations=apply_migrations).databases.qdrant
    return qdrant.url, qdrant.api_key


def _emit_result(
    manifest: HubBackupManifest,
    manifest_path: Path,
    *,
    json_output: bool,
) -> None:
    """Report the backup. The DSN and Qdrant key never appear here."""
    if json_output:
        payload = {
            "manifest": str(manifest_path),
            "backup_root": str(manifest_path.parent),
            "created_at": manifest.created_at,
            "epoch_id": manifest.epoch_id,
            "artifacts": len(manifest.artifacts),
            "stores": {
                key: {
                    "archive_verified": store.archive_verified.verified,
                    "restore_verified": store.restore_verified.verified,
                }
                for key, store in manifest.stores.items()
            },
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    click.echo(f"Hub backup complete: {manifest_path.parent}")
    click.echo(f"Manifest:  {manifest_path}")
    click.echo(f"Artifacts: {len(manifest.artifacts)}")
    for key in sorted(manifest.stores):
        store = manifest.stores[key]
        click.echo(
            f"  {key}: archive={store.archive_verified.method} "
            f"restore={store.restore_verified.method}"
        )
