"""`gobby hub-backup`: stop-consistent hub backup with verified restore.

This module owns orchestration only. Artifacts are produced by the drivers in
`_stores`, proven restorable by the scratch restores in `_verify`, and recorded
in the schema-validated manifest from `_manifest`. What lives here is the order
those run in and the guarantees around them: the daemon is down for the whole
backup, the Docker services come back even when archiving fails, and nothing
secret reaches the manifest or stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 # fixed docker inspect argv, never shell=True
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import click

from gobby.cli._daemon_services import ServiceStartResult
from gobby.cli.daemon import _services_start, _services_stop
from gobby.cli.hub_backup._integrity import (
    create_staging_directory,
    publish_staged_backup,
    remove_staging_directory,
    require_absent_output_path,
    verify_artifacts,
)
from gobby.cli.hub_backup._manifest import (
    MANIFEST_NAME,
    ArtifactRecord,
    HubBackupManifest,
    StoreRecord,
    VerificationState,
    load_manifest,
    write_manifest,
)
from gobby.cli.hub_backup._stores import (
    FALKORDB_DUMP_RELPATH,
    GLOBALS_DUMP_RELPATH,
    HUB_VOLUMES,
    POSTGRES_DUMP_RELPATH,
    VOLUME_ARCHIVE_DIR,
    archive_rule_allow_audit_logs,
    collect_postgres_identity,
    collect_row_count_probes,
    collect_schema_object_counts,
    collect_source_roles,
    dump_falkordb,
    dump_postgres,
    reconcile_restored_principals,
    restore_postgres_globals,
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
from gobby.cli.hub_backup.files_home import (
    FILES_ARCHIVE_METHOD,
    FILES_ARCHIVE_RELPATH,
    FilesHomeArchiveError,
    archive_files_home_store,
    check_output_outside_sources,
    maintenance_claim,
    restore_hub_files,
    verify_files_home_archive,
)
from gobby.cli.installers.compose_env import (
    MANAGED_SERVICE_PROFILES,
    ComposeEnvironmentError,
    ComposeRuntime,
    resolve_compose_runtime,
    resolve_predecessor_service_runtime,
)
from gobby.cli.installers.container_restart import (
    FALKORDB_CONTAINER,
    POSTGRES_CONTAINER,
    QDRANT_CONTAINER,
)
from gobby.cli.installers.docker_guard import ensure_docker_allowed
from gobby.cli.postgres_backup import (
    POSTGRES_TEST_CONTAINER,
    _managed_postgres_container,
    _process_output,
    _require_managed_docker_postgres,
    _resolve_database_url,
    restore_postgres_backup,
)
from gobby.cli.runtime import get_cli_runtime
from gobby.cli.utils_shutdown import stop_daemon
from gobby.config import expand_env_vars
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.config.logging import LoggingSettings, resolved_logs_dir
from gobby.config.registry import CONFIG_REGISTRY, UnknownConfigKeyError
from gobby.paths import get_gobby_home
from gobby.storage.config_repository import UnknownStoredConfigKeyError, decode_config_value
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.maintenance_epoch import (
    MAINTENANCE_EPOCH_ENV,
    require_orchestrator_epoch,
)
from gobby.storage.secrets import SecretStore
from gobby.utils.durable_file import durable_replace, exclusive_file_lock
from gobby.utils.version import get_version

REQUIRED_CONTAINERS: tuple[str, ...] = (
    POSTGRES_CONTAINER,
    QDRANT_CONTAINER,
    FALKORDB_CONTAINER,
)
SCRATCH_QDRANT_CONTAINER = "gobby-qdrant-test-1"
SCRATCH_FALKORDB_CONTAINER = "gobby-falkordb-test-1"
SCRATCH_HUB_VOLUMES: tuple[str, ...] = (
    "gobby_test_postgres_data",
    "gobby_test_qdrant_data",
    "gobby_test_falkordb_data",
)

MIN_FREE_BYTES = 5 * 1024**3
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


@dataclass(frozen=True)
class HubBackupTarget:
    """Managed containers and volumes selected by the bootstrap PostgreSQL DSN."""

    containers: tuple[str, ...]
    falkordb_container: str
    volumes: tuple[str, ...]
    qdrant_port: int | None = None


def _configured_files_home(gobby_home: Path) -> Path | None:
    bootstrap = gobby_home / "bootstrap.yaml"
    if not bootstrap.is_file():
        return None
    try:
        config = load_bootstrap(str(bootstrap))
    except BootstrapConfigError:
        return None
    if config.datastore_mode == "remote" or not config.files_home:
        return None
    return Path(config.files_home)


def _hub_backup_target(database_url: str) -> HubBackupTarget:
    postgres_container = _managed_postgres_container(database_url)
    if postgres_container == POSTGRES_CONTAINER:
        return HubBackupTarget(
            containers=REQUIRED_CONTAINERS,
            falkordb_container=FALKORDB_CONTAINER,
            volumes=HUB_VOLUMES,
        )
    if postgres_container == POSTGRES_TEST_CONTAINER:
        return HubBackupTarget(
            containers=(
                POSTGRES_TEST_CONTAINER,
                SCRATCH_QDRANT_CONTAINER,
                SCRATCH_FALKORDB_CONTAINER,
            ),
            falkordb_container=SCRATCH_FALKORDB_CONTAINER,
            volumes=SCRATCH_HUB_VOLUMES,
            qdrant_port=60990,
        )
    raise click.ClickException(f"Unsupported managed PostgreSQL container: {postgres_container}")


@click.group("hub-backup", invoke_without_command=True)
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
    if ctx.invoked_subcommand is not None:
        return

    if epoch is not None:
        child_epoch = os.environ.get(MAINTENANCE_EPOCH_ENV)
        if child_epoch != epoch:
            raise click.ClickException(
                "`hub-backup --epoch` may only run as a child of "
                "`gobby hub-maintenance` for the same epoch"
            )

    gobby_home = get_gobby_home()
    backup_root = _resolve_output_dir(output)
    require_absent_output_path(backup_root)
    database_url = _resolve_database_url(gobby_home)
    target = _hub_backup_target(database_url)
    _preflight(backup_root, containers=target.containers)
    if epoch is not None:
        require_orchestrator_epoch(database_url, epoch)
    _require_managed_docker_postgres(database_url=database_url)
    qdrant_url, qdrant_api_key = _qdrant_settings(ctx, apply_migrations=epoch is None)
    _require_safe_qdrant_target(target, qdrant_url)

    # An open epoch owns the daemon lifecycle, so `--epoch` leaves it stopped.
    restart_daemon = _daemon_is_running() and epoch is None
    staging_root = create_staging_directory(backup_root)
    manifest_path = backup_root / MANIFEST_NAME
    try:
        stop_daemon(quiet=json_output, shutdown_source="cli_hub_backup")
        files_home = _configured_files_home(gobby_home)
        if files_home is not None:
            try:
                check_output_outside_sources(backup_root, files_home)
            except FilesHomeArchiveError as exc:
                raise click.ClickException(str(exc)) from exc
        with maintenance_claim(gobby_home):
            manifest = _run_backup(
                backup_root=staging_root,
                gobby_home=gobby_home,
                logs_dir=_backup_logs_dir(ctx, predecessor=epoch is not None),
                database_url=database_url,
                qdrant_url=qdrant_url,
                qdrant_api_key=qdrant_api_key,
                epoch=epoch,
                target=target,
                files_home=files_home,
            )
            verify_artifacts(staging_root, manifest.artifacts)
            write_manifest(manifest, staging_root / MANIFEST_NAME)
            publish_staged_backup(staging_root, backup_root)
    except FilesHomeArchiveError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        try:
            remove_staging_directory(staging_root)
        finally:
            if restart_daemon:
                _start_daemon()

    _emit_result(manifest, manifest_path, json_output=json_output)


@hub_backup.command("restore")
@click.argument(
    "backup_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--database-url",
    required=True,
    metavar="DSN",
    help="Explicit PostgreSQL DSN for the restored target.",
)
@click.option(
    "--clean",
    is_flag=True,
    help="Drop database objects before restoring the PostgreSQL artifact.",
)
@click.option("--yes", is_flag=True, help="Skip the destructive restore confirmation.")
def restore_hub_backup(
    backup_root: Path,
    database_url: str,
    clean: bool,
    yes: bool,
) -> None:
    """Restore PostgreSQL from a verified hub backup into an explicit target."""
    if _daemon_is_running():
        raise click.ClickException("Stop the daemon first: gobby stop")
    try:
        manifest = load_manifest(backup_root / MANIFEST_NAME)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Invalid hub backup manifest: {exc}") from exc
    verify_artifacts(backup_root, manifest.artifacts)
    postgres_store = manifest.stores.get("postgres")
    if (
        postgres_store is None
        or not postgres_store.archive_verified.verified
        or not postgres_store.restore_verified.verified
    ):
        raise click.ClickException("Hub backup has no verified PostgreSQL restore artifact")
    if not yes and not click.confirm("Restore hub PostgreSQL data into the explicit target?"):
        click.echo("Aborted.")
        return

    files_artifact = next(
        (artifact for artifact in manifest.artifacts if artifact.path == FILES_ARCHIVE_RELPATH),
        None,
    )
    try:
        with maintenance_claim(get_gobby_home()):
            restore_hub_files(
                backup_root,
                expected_sha256=None if files_artifact is None else files_artifact.sha256,
            )
            restore_postgres_globals(database_url, backup_root / GLOBALS_DUMP_RELPATH)
            result = restore_postgres_backup(
                backup_root / Path(POSTGRES_DUMP_RELPATH).parent,
                clean=clean,
                allow_unverified=True,
                gobby_home=get_gobby_home(),
                database_url=database_url,
            )
            reconcile_restored_principals(database_url)
    except FilesHomeArchiveError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Hub PostgreSQL restore completed.")
    if target := result.get("database_url"):
        click.echo(f"  Target: {target}")
    if epoch_id := result.get("released_epoch_id"):
        click.echo(f"  Maintenance epoch released by restore: {epoch_id}")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _preflight(backup_root: Path, *, containers: tuple[str, ...] = REQUIRED_CONTAINERS) -> None:
    """Refuse to start unless Docker, the hub containers, and disk space are there."""
    if shutil.which("docker") is None:
        raise click.ClickException(
            "Docker CLI is unavailable; hub-backup needs Docker to reach the hub datastores"
        )
    for container in containers:
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


def _require_safe_qdrant_target(target: HubBackupTarget, qdrant_url: str | None) -> None:
    """Keep protected scratch backups on the loopback-only scratch Qdrant port."""
    if target.qdrant_port is None:
        return
    parsed = urlparse(qdrant_url or "")
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if host not in {"localhost", "127.0.0.1", "::1"} or port != target.qdrant_port:
        raise click.ClickException(
            f"Protected scratch hub backup requires Qdrant on loopback port {target.qdrant_port}"
        )


def _container_running(container: str) -> bool:
    ensure_docker_allowed("hub backup container inspection", runner=subprocess.run)
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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_backup(
    *,
    backup_root: Path,
    gobby_home: Path,
    logs_dir: Path,
    database_url: str,
    qdrant_url: str | None,
    qdrant_api_key: str | None,
    epoch: str | None,
    target: HubBackupTarget,
    files_home: Path | None = None,
) -> HubBackupManifest:
    """Collect source facts, archive every store, verify each one, and describe it."""
    identity, starting_head = collect_postgres_identity(database_url)
    probes = collect_row_count_probes(database_url)
    schema_objects = collect_schema_object_counts(database_url)
    roles = collect_source_roles(database_url)

    artifacts: list[ArtifactRecord] = []
    postgres_artifacts, postgres_details = dump_postgres(database_url, backup_root)
    postgres_details["schema_object_counts"] = schema_objects
    artifacts.extend(postgres_artifacts)
    qdrant_artifacts, qdrant_details = snapshot_qdrant(qdrant_url, qdrant_api_key, backup_root)
    artifacts.extend(qdrant_artifacts)
    falkordb_artifacts, falkordb_details = dump_falkordb(
        backup_root,
        container=target.falkordb_container,
    )
    artifacts.extend(falkordb_artifacts)
    volume_artifacts, volume_details = _archive_volumes(
        gobby_home,
        backup_root,
        volumes=target.volumes,
    )
    artifacts.extend(volume_artifacts)
    files_artifacts, files_details = archive_files_home_store(backup_root, files_home)
    artifacts.extend(files_artifacts)
    artifacts.extend(archive_rule_allow_audit_logs(logs_dir, backup_root))
    identity_artifact = _archive_machine_identity(gobby_home, backup_root)
    if identity_artifact is not None:
        artifacts.append(identity_artifact)

    stores = _verify_stores(
        backup_root=backup_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        probes=probes,
        schema_objects=schema_objects,
        roles=roles,
        postgres_details=postgres_details,
        qdrant_details=qdrant_details,
        falkordb_details=falkordb_details,
        volume_details=volume_details,
        files_details=files_details,
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


def _archive_machine_identity(gobby_home: Path, backup_root: Path) -> ArtifactRecord | None:
    """Archive the pre-cutover identity inside the verified backup manifest."""
    source = gobby_home / "machine_id"
    if not source.is_file():
        return None
    with exclusive_file_lock(source):
        content = source.read_bytes()
    relative_path = "identity/machine_id"
    destination = backup_root / relative_path
    durable_replace(destination, content)
    return ArtifactRecord(
        name="machine_identity",
        path=relative_path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def _archive_volumes(
    gobby_home: Path,
    backup_root: Path,
    *,
    volumes: tuple[str, ...] = HUB_VOLUMES,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    """Archive the hub volumes cold, and bring the services back either way."""
    if not _services_stop(gobby_home):
        raise click.ClickException(
            "Could not stop the managed Docker services; refusing to archive live volumes"
        )
    try:
        artifacts, details = tar_volumes(backup_root, volumes)
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
        runtime = resolve_predecessor_service_runtime(gobby_home, postgres_runtime)
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
    ensure_docker_allowed("hub backup epoch compose up", runner=subprocess.run)
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
    schema_objects: dict[str, int],
    roles: list[dict[str, object]],
    postgres_details: dict[str, object],
    qdrant_details: dict[str, object],
    falkordb_details: dict[str, object],
    volume_details: dict[str, object],
    files_details: dict[str, object],
) -> dict[str, StoreRecord]:
    """Prove each store restores and fold the proof into its manifest record."""
    postgres_state, postgres_proof = verify_postgres_restore(
        backup_root / POSTGRES_DUMP_RELPATH,
        backup_root / GLOBALS_DUMP_RELPATH,
        expected_probes=probes,
        expected_roles=_role_expectations(roles),
        expected_schema_objects=schema_objects,
    )
    snapshots, expected_points, expected_digests = _qdrant_expectations(qdrant_details, backup_root)
    qdrant_state, qdrant_proof = verify_qdrant_restore(
        qdrant_url,
        qdrant_api_key,
        snapshots,
        expected_points,
        expected_digests,
    )
    falkordb_state, falkordb_proof = verify_falkordb_restore(
        backup_root / FALKORDB_DUMP_RELPATH,
        _expected_graph_inventory(falkordb_details),
    )
    volumes_state, volumes_proof = verify_volume_archives(
        _volume_archives(backup_root, volume_details),
        _expected_volume_inventories(volume_details),
    )
    files_state, files_proof = verify_files_home_archive(backup_root / FILES_ARCHIVE_RELPATH)

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
        "files": _store_record(FILES_ARCHIVE_METHOD, files_state, files_details, files_proof),
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
) -> tuple[dict[str, Path], dict[str, int], dict[str, str]]:
    collections = details.get("collections")
    if not isinstance(collections, dict):
        raise click.ClickException("Qdrant snapshot driver reported no collections to verify")
    snapshots: dict[str, Path] = {}
    expected_points: dict[str, int] = {}
    expected_digests: dict[str, str] = {}
    for name, record in collections.items():
        if (
            not isinstance(record, dict)
            or "snapshot" not in record
            or "points" not in record
            or "content_sha256" not in record
        ):
            raise click.ClickException(f"Qdrant snapshot record for {name} is incomplete")
        snapshots[str(name)] = backup_root / str(record["snapshot"])
        expected_points[str(name)] = int(record["points"])
        expected_digests[str(name)] = str(record["content_sha256"])
    return snapshots, expected_points, expected_digests


def _expected_graph_inventory(details: dict[str, object]) -> dict[str, dict[str, int]]:
    inventory = details.get("graph_inventory")
    if not isinstance(inventory, dict):
        raise click.ClickException("FalkorDB dump driver reported no graph inventory to verify")
    result: dict[str, dict[str, int]] = {}
    for graph, record in inventory.items():
        if not isinstance(record, dict) or "nodes" not in record or "edges" not in record:
            raise click.ClickException(f"FalkorDB graph inventory for {graph} is incomplete")
        result[str(graph)] = {"nodes": int(record["nodes"]), "edges": int(record["edges"])}
    return result


def _expected_volume_inventories(
    details: dict[str, object],
) -> dict[str, dict[str, object]]:
    inventories = details.get("source_inventories")
    if not isinstance(inventories, dict):
        raise click.ClickException("Volume archive driver reported no source inventories")
    result: dict[str, dict[str, object]] = {}
    for volume, record in inventories.items():
        if not isinstance(record, dict):
            raise click.ClickException(f"Volume source inventory for {volume} is incomplete")
        result[str(volume)] = dict(record)
    return result


def _volume_archives(
    backup_root: Path,
    details: dict[str, object],
) -> dict[str, Path]:
    inventories = _expected_volume_inventories(details)
    return {volume: backup_root / VOLUME_ARCHIVE_DIR / f"{volume}.tar.gz" for volume in inventories}


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
    runtime = get_cli_runtime(ctx)
    if not apply_migrations:
        return _predecessor_qdrant_settings(runtime.require_database(apply_migrations=False))
    qdrant = runtime.require_config(apply_migrations=True).databases.qdrant
    return qdrant.url, qdrant.api_key


def _predecessor_qdrant_settings(database: HubDatabase) -> tuple[str | None, str | None]:
    """Read backup settings while the identity predecessor still has retired auth rows."""
    all_keys = [
        str(row["key"]) for row in database.fetchall("SELECT key FROM config_store ORDER BY key")
    ]
    deprecated = {"auth.password_hash", "auth.username"}
    for key in all_keys:
        if key in deprecated:
            continue
        try:
            CONFIG_REGISTRY.resolve(key)
        except UnknownConfigKeyError:
            raise UnknownStoredConfigKeyError(key) from None

    rows = database.fetchall(
        """
        SELECT key, value
        FROM config_store
        WHERE key IN ('databases.qdrant.api_key', 'databases.qdrant.url')
        ORDER BY key
        """
    )
    values = {
        str(row["key"]): decode_config_value(str(row["key"]), str(row["value"])) for row in rows
    }
    url = values.get("databases.qdrant.url")
    if url is not None and not isinstance(url, str):
        raise click.ClickException("databases.qdrant.url must be a string")
    api_key = values.get("databases.qdrant.api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise click.ClickException("databases.qdrant.api_key must be a string")
    if isinstance(api_key, str):
        secret_store = SecretStore(database, gobby_home=get_gobby_home())
        api_key = expand_env_vars(api_key, secret_resolver=secret_store.get)
    return url, api_key


def _backup_logs_dir(ctx: click.Context, *, predecessor: bool) -> Path:
    """Resolve only the logging directory before predecessor config retirement."""
    runtime = get_cli_runtime(ctx)
    if not predecessor:
        return resolved_logs_dir(runtime.require_config().logging)
    database = runtime.require_database(apply_migrations=False)
    row = database.fetchone("SELECT value FROM config_store WHERE key = 'logging.dir'")
    if row is None:
        return resolved_logs_dir(LoggingSettings())
    value = decode_config_value("logging.dir", str(row["value"]))
    if not isinstance(value, str):
        raise click.ClickException("logging.dir must be a string")
    return resolved_logs_dir(LoggingSettings(dir=value))


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
