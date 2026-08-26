"""FalkorDB service installation."""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import string
import subprocess  # nosec B404
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.config.persistence import validate_falkordb_password

from .compose_env import ComposeEnvironmentError, resolve_compose_runtime
from .docker_guard import ensure_docker_allowed
from .managed_services_lock import ManagedServicesLockError, managed_services_lock
from .postgres import compose_restart_required_notice, reconcile_unified_compose

logger = logging.getLogger(__name__)

_COMPOSE_SRC = Path(__file__).resolve().parents[2] / "data" / "docker-compose.services.yml"

DEFAULT_FALKORDB_HOST = "127.0.0.1"
DEFAULT_FALKORDB_PORT = 16379
DEFAULT_FALKORDB_BROWSER_URL = "http://localhost:13000"


@dataclass(frozen=True)
class ResolvedFalkorPassword:
    value: str
    source: str
    expose_value: bool


def _normalize_home(gobby_home: Path | None = None) -> Path:
    if gobby_home is not None:
        return gobby_home
    from gobby.cli.utils import get_gobby_home

    return get_gobby_home()


def _config_db(gobby_home: Path, *, apply_migrations: bool = True) -> Any:
    """Build a bounded PostgreSQL hub context for a Gobby home."""
    from gobby.storage.hub.runtime import runtime_hub_database

    return runtime_hub_database(
        str(gobby_home / "bootstrap.yaml"),
        apply_migrations=apply_migrations,
    )


def _generate_falkordb_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    password = "".join(secrets.choice(alphabet) for _ in range(32))
    return validate_falkordb_password(password)


def _resolve_falkordb_password(*, gobby_home: Path | None = None) -> ResolvedFalkorPassword:
    """Reuse the stored ``falkordb_password`` secret, else generate a fresh one."""
    home = _normalize_home(gobby_home)
    database_stack = ExitStack()
    db = database_stack.enter_context(_config_db(home))
    try:
        from gobby.storage.config_repository import ConfigRepository
        from gobby.storage.secrets import SecretStore

        secret_store = SecretStore(db, gobby_home=home)
        snapshot = ConfigRepository(db, secret_store=secret_store).read(resolve_secrets=False)
        config_values = snapshot.overrides
        required = {
            "databases.falkordb.host",
            "databases.falkordb.port",
            "databases.falkordb.password",
        }
        present = set(config_values) & required
        if present:
            if "databases.falkordb.password" not in present:
                raise ValueError("stored FalkorDB config is incomplete; password must be set")
            configured = config_values.get("databases.falkordb.password")
            if not isinstance(configured, str) or not configured.startswith("$secret:"):
                raise ValueError("stored FalkorDB password must be a SecretStore reference")
            secret_name = configured.removeprefix("$secret:")
            if not secret_name or not secret_store.exists(secret_name):
                raise ValueError(f"stored FalkorDB secret {secret_name or '<empty>'!r} is missing")
            secret = secret_store.get(secret_name)
            if not secret:
                raise ValueError(f"stored FalkorDB secret {secret_name!r} is empty")
            return ResolvedFalkorPassword(
                value=validate_falkordb_password(secret),
                source="reused",
                expose_value=False,
            )
    finally:
        database_stack.close()

    return ResolvedFalkorPassword(
        value=_generate_falkordb_password(),
        source="generated",
        expose_value=True,
    )


def install_falkordb(*, gobby_home: Path | None = None) -> dict[str, Any]:
    """Install FalkorDB via Docker Compose."""
    home = _normalize_home(gobby_home)
    try:
        with managed_services_lock(home, operation="falkordb installer refresh"):
            return _install_falkordb_locked(gobby_home=home)
    except ManagedServicesLockError as exc:
        return {"success": False, "error": str(exc)}


def _install_falkordb_locked(*, gobby_home: Path) -> dict[str, Any]:
    home = gobby_home
    if not shutil.which("docker"):
        return {"success": False, "error": "Docker not found. Install Docker to use FalkorDB."}

    try:
        resolved = _resolve_falkordb_password(gobby_home=home)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to read FalkorDB config: {exc}"}

    try:
        _update_config(
            port=DEFAULT_FALKORDB_PORT,
            password=resolved.value,
            gobby_home=home,
        )
    except Exception as exc:
        logger.warning("Failed to persist FalkorDB config: %s", exc)
        return {"success": False, "error": f"Failed to persist FalkorDB config: {exc}"}
    try:
        runtime = resolve_compose_runtime(home, profiles=("falkordb",))
    except ComposeEnvironmentError as exc:
        return {"success": False, "error": f"Failed to resolve FalkorDB config: {exc}"}

    services_dir = home / "services"
    reconciliation = reconcile_unified_compose(services_dir)
    compose_file = reconciliation.compose_file

    try:
        ensure_docker_allowed("falkordb install compose up", runner=subprocess.run)
        result = subprocess.run(  # nosec B603 B607
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--profile",
                "falkordb",
                "up",
                "-d",
                "--remove-orphans",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=runtime.environment,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"Docker compose up failed: {result.stderr or result.stdout}",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Docker compose up timed out after 120s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"success": False, "error": f"Docker compose execution failed: {exc}"}

    if not _wait_for_health(compose_file, services_dir, runtime.environment):
        return {
            "success": False,
            "error": "Health check failed: FalkorDB did not become healthy in time",
        }

    response: dict[str, Any] = {
        "success": True,
        "host": DEFAULT_FALKORDB_HOST,
        "port": DEFAULT_FALKORDB_PORT,
        "url": f"redis://{DEFAULT_FALKORDB_HOST}:{DEFAULT_FALKORDB_PORT}",
        "browser_url": DEFAULT_FALKORDB_BROWSER_URL,
        "compose_file": str(compose_file),
        "mode": "docker",
        "password_source": resolved.source,
        "password": resolved.value if resolved.expose_value else None,
    }
    restart_notice = compose_restart_required_notice(
        reconciliation,
        started_services=frozenset({"falkordb"}),
    )
    if restart_notice:
        response["restart_required"] = restart_notice
    return response


def _wait_for_health(
    compose_file: Path,
    services_dir: Path,
    env: dict[str, str],
    retries: int = 30,
    interval: float = 2.0,
) -> bool:
    return asyncio.run(_wait_for_health_async(compose_file, services_dir, env, retries, interval))


async def _wait_for_health_async(
    compose_file: Path,
    services_dir: Path,
    env: dict[str, str],
    retries: int = 30,
    interval: float = 2.0,
) -> bool:
    for _ in range(retries):
        try:
            ensure_docker_allowed("falkordb health-check compose exec", runner=subprocess.run)
            result = subprocess.run(  # nosec B603 B607
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "exec",
                    "-T",
                    "falkordb",
                    "sh",
                    "-c",
                    'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" PING',
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
                cwd=str(services_dir),
            )
            if result.returncode == 0 and "PONG" in result.stdout:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug(
                "FalkorDB health check attempt failed (%s): %s",
                type(exc).__name__,
                exc,
            )
        await asyncio.sleep(interval)
    return False


def _update_config(*, port: int, password: str, gobby_home: Path) -> None:
    database_stack = ExitStack()
    db = database_stack.enter_context(_config_db(gobby_home))
    try:
        from gobby.cli.config_writes import apply_cas_config_patch
        from gobby.storage.config_mutations import ConfigPatch, SecretUpdate
        from gobby.storage.config_repository import ConfigReadSnapshot
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.secrets import SecretStore

        def build_patch(snapshot: ConfigReadSnapshot) -> ConfigPatch:
            configured_host = snapshot.values.get("databases.published_host")
            host = (
                configured_host.strip()
                if isinstance(configured_host, str) and configured_host.strip()
                else DEFAULT_FALKORDB_HOST
            )
            return ConfigPatch(
                values={
                    "databases.falkordb.host": host,
                    "databases.falkordb.port": port,
                },
                secrets={"databases.falkordb.password": SecretUpdate(password)},
            )

        secret_store = SecretStore(db, gobby_home=gobby_home)
        store = ConfigStore(db, secret_store=secret_store)
        apply_cas_config_patch(
            read_snapshot=store.read_snapshot,
            build_patch=build_patch,
            patch=store.patch,
        )
    finally:
        database_stack.close()
