"""Managed Docker service lifecycle for daemon commands."""

import contextlib
import logging
import os
import shutil
import signal
import subprocess  # nosec B404 # subprocess needed for managed Docker services
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import yaml

from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

from .installers.compose_env import (
    MANAGED_SERVICE_PROFILES,
    ComposeEnvironmentError,
    ComposeRuntime,
)
from .installers.docker_guard import (
    DockerTestProtectError,
    ensure_docker_allowed,
    resolves_to_real_run,
)
from .installers.managed_services_lock import (
    ManagedServicesLockError,
    managed_services_lock,
)
from .installers.postgres import reconcile_unified_compose

logger = logging.getLogger(__name__)

_COMPOSE_TERMINATION_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class ServiceStartResult:
    outcome: Literal["success", "skipped", "failed"]
    detail: str


class ComposeRuntimeResolver(Protocol):
    def __call__(
        self,
        gobby_home: Path,
        *,
        database_url: str | None = None,
        profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
        overrides: dict[str, str] | None = None,
    ) -> ComposeRuntime: ...


def start_managed_services(
    gobby_home: Path,
    *,
    resolve_runtime: ComposeRuntimeResolver,
) -> ServiceStartResult:
    """Start the required managed Docker stack and wait for container health."""
    try:
        with managed_services_lock(gobby_home, operation="services start"):
            return _start_managed_services_locked(gobby_home, resolve_runtime=resolve_runtime)
    except ManagedServicesLockError as exc:
        return ServiceStartResult("failed", str(exc))


def _start_managed_services_locked(
    gobby_home: Path,
    *,
    resolve_runtime: ComposeRuntimeResolver,
) -> ServiceStartResult:
    try:
        bootstrap = load_bootstrap(str(gobby_home / "bootstrap.yaml"))
    except BootstrapConfigError as exc:
        return ServiceStartResult("failed", f"Invalid bootstrap.yaml: {exc}")
    if bootstrap.datastore_mode == "remote":
        target_host = urlparse(bootstrap.database_url or "").hostname or "configured hub"
        return ServiceStartResult(
            "skipped",
            f"Remote datastore mode: using shared services at {target_host}",
        )

    if not shutil.which("docker"):
        return ServiceStartResult(
            "failed", "Docker executable is unavailable; install Docker and retry"
        )

    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if not compose_file.exists():
        return ServiceStartResult(
            "failed", f"Compose file is missing: {compose_file}; run `gobby install`"
        )

    try:
        compose_file = reconcile_unified_compose(services_dir).compose_file
    except (OSError, RuntimeError) as exc:
        return ServiceStartResult("failed", f"Could not refresh managed Compose file: {exc}")

    compose_error = _validate_managed_compose_profiles(compose_file)
    if compose_error:
        return ServiceStartResult("failed", compose_error)

    try:
        postgres_runtime = resolve_runtime(gobby_home, profiles=("postgres",))
    except ComposeEnvironmentError as exc:
        return ServiceStartResult("failed", f"Could not resolve Docker service config: {exc}")

    postgres_result = _run_compose_up(compose_file, services_dir, postgres_runtime)
    if postgres_result.outcome != "success":
        return postgres_result

    # The full-profile resolve below reads config_store and SecretStore with
    # current-contract queries; a schema-advancing upgrade reaches this point
    # with an older hub, so apply the contract before reading it.
    try:
        from gobby.cli.datastores import apply_hub_schema_contract

        apply_hub_schema_contract(gobby_home)
    except Exception as exc:
        return ServiceStartResult("failed", f"Could not apply the hub schema contract: {exc}")

    try:
        runtime = resolve_runtime(gobby_home)
    except ComposeEnvironmentError as exc:
        return ServiceStartResult("failed", f"Could not resolve Docker service config: {exc}")

    if runtime.profiles != MANAGED_SERVICE_PROFILES:
        return ServiceStartResult(
            "failed",
            "Docker service config must enable postgres, qdrant, and falkordb profiles",
        )

    return _run_compose_up(compose_file, services_dir, runtime)


def _terminate_compose_process(
    process: subprocess.Popen[str],
    *,
    graceful: bool,
) -> None:
    """Terminate the compose process tree and reap its root process."""
    interrupted = False
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # nosec B603 B607 # Windows process-tree cleanup
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
            )
    elif graceful:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=_COMPOSE_TERMINATION_GRACE_SECONDS)
            return
        except KeyboardInterrupt:
            interrupted = True
        except (OSError, subprocess.SubprocessError):
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    try:
        process.wait(timeout=_COMPOSE_TERMINATION_GRACE_SECONDS)
    except KeyboardInterrupt:
        interrupted = True
        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.SubprocessError, KeyboardInterrupt):
            process.wait(timeout=_COMPOSE_TERMINATION_GRACE_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    if interrupted:
        raise KeyboardInterrupt


def _run_compose_command(
    cmd: list[str],
    *,
    timeout: int,
    env: Mapping[str, str],
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Run compose in its own process group so a timeout kills the plugin too.

    subprocess.run's timeout kills only the docker CLI wrapper; the
    docker-compose plugin child survives as an orphan that keeps mutating the
    project, and concurrent orphans recreate each other's containers so
    ``up --wait`` never converges again until they are killed by hand.
    """
    if not resolves_to_real_run(subprocess.run):
        # The docker-guard contract is that unit tests stub subprocess.run;
        # honor the stub so no real process is ever spawned under test.
        return subprocess.run(  # nosec B603 # test stub per docker-guard contract
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env),
            cwd=cwd,
        )
    with subprocess.Popen(  # nosec B603 # hardcoded docker command
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env),
        cwd=cwd,
        start_new_session=sys.platform != "win32",
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_compose_process(process, graceful=False)
            raise
        except KeyboardInterrupt:
            _terminate_compose_process(process, graceful=True)
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _run_compose_up(
    compose_file: Path,
    services_dir: Path,
    runtime: ComposeRuntime,
) -> ServiceStartResult:
    cmd = ["docker", "compose", "-f", str(compose_file)]
    for profile in runtime.profiles:
        cmd.extend(["--profile", profile])
    cmd.extend(["up", "-d", "--remove-orphans", "--wait"])

    try:
        ensure_docker_allowed("managed-services compose up", runner=subprocess.run)
        result = _run_compose_command(
            cmd,
            timeout=120,
            env=runtime.environment,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            return ServiceStartResult(
                "failed",
                f"Docker compose up failed: {result.stderr or result.stdout}",
            )
    except subprocess.TimeoutExpired:
        return ServiceStartResult("failed", "Docker compose up timed out after 120s")
    except (OSError, subprocess.SubprocessError) as exc:
        return ServiceStartResult("failed", f"Docker compose execution failed: {exc}")
    return ServiceStartResult("success", "Docker services started")


def _validate_managed_compose_profiles(compose_file: Path) -> str | None:
    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return f"Compose file is invalid: {exc}"
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, dict):
        return "Compose file must define a services mapping"
    configured_profiles: set[str] = set()
    for service in services.values():
        service_profiles = service.get("profiles") if isinstance(service, dict) else None
        if isinstance(service_profiles, list):
            configured_profiles.update(
                profile for profile in service_profiles if isinstance(profile, str)
            )
    missing = [
        profile for profile in MANAGED_SERVICE_PROFILES if profile not in configured_profiles
    ]
    if missing:
        return f"Compose file is missing required profiles: {', '.join(missing)}"
    return None


def stop_managed_services(
    gobby_home: Path,
    *,
    resolve_runtime: ComposeRuntimeResolver,
) -> bool:
    """Stop all Docker services via the unified compose file."""
    try:
        with managed_services_lock(gobby_home, operation="services stop"):
            return _stop_managed_services_locked(gobby_home, resolve_runtime=resolve_runtime)
    except ManagedServicesLockError as exc:
        logger.warning("Could not acquire managed-services lock: %s", exc)
        return False


def _stop_managed_services_locked(
    gobby_home: Path,
    *,
    resolve_runtime: ComposeRuntimeResolver,
) -> bool:
    if not shutil.which("docker"):
        return False

    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if not compose_file.exists():
        return False

    try:
        # Docker shutdown must still work when PostgreSQL is already unhealthy.
        runtime = resolve_runtime(gobby_home, profiles=("postgres",))
        command = ["docker", "compose", "-f", str(compose_file)]
        for profile in MANAGED_SERVICE_PROFILES:
            command.extend(["--profile", profile])
        # `stop`, never `down`: containers keep their identity, volumes, and
        # unless-stopped policy so `gobby start` brings them back with `up -d`.
        command.append("stop")
        ensure_docker_allowed("managed-services compose stop", runner=subprocess.run)
        result = _run_compose_command(
            command,
            timeout=60,
            env=runtime.environment,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            logger.warning("Failed to stop services: %s", result.stderr or result.stdout)
            return False
        return True
    except ComposeEnvironmentError as exc:
        logger.warning("Could not resolve config for services; skipping Docker shutdown: %s", exc)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out stopping Docker services")
    except DockerTestProtectError:
        raise
    except Exception as exc:
        logger.warning("Failed to stop Docker services: %s", exc)
    return False
