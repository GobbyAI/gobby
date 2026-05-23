"""FalkorDB service installation and uninstallation."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import string
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gobby.config.persistence import validate_falkordb_password

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_FALKORDB_HOST = "127.0.0.1"
DEFAULT_FALKORDB_PORT = 16379
DEFAULT_FALKORDB_BROWSER_URL = "http://localhost:13000"
DEFAULT_FALKORDB_PASSWORD = "gobbyfalkor"
FALKORDB_DATA_VOLUME = "gobby_falkordb_data"
_LEGACY_NEO4J_CONFIG_KEYS = (
    "databases.neo4j.url",
    "databases.neo4j.auth",
    "databases.neo4j.database",
    "databases.neo4j.password",
)


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


def _resolve_falkordb_db_path(gobby_home: Path) -> Path:
    """Resolve the local config DB path for a Gobby home."""
    bootstrap_file = gobby_home / "bootstrap.yaml"
    if bootstrap_file.exists():
        from gobby.config.bootstrap import load_bootstrap

        return Path(load_bootstrap(str(bootstrap_file)).database_path).expanduser()
    return gobby_home / "gobby-hub.db"


def _open_local_config_db(gobby_home: Path) -> Any:
    from gobby.storage.database import LocalDatabase

    db = LocalDatabase(_resolve_falkordb_db_path(gobby_home))
    db.apply_migrations()
    return db


def _generate_falkordb_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    password = "".join(secrets.choice(alphabet) for _ in range(32))
    return validate_falkordb_password(password)


def _resolve_falkordb_password(
    password: str | None = None,
    *,
    gobby_home: Path | None = None,
) -> ResolvedFalkorPassword:
    home = _normalize_home(gobby_home)
    if password:
        return ResolvedFalkorPassword(
            value=validate_falkordb_password(password),
            source="provided",
            expose_value=False,
        )

    db = _open_local_config_db(home)
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.secrets import SecretStore

        configured = ConfigStore(db).get("databases.falkordb.requirepass")
        if isinstance(configured, str) and configured:
            if configured.startswith("$secret:"):
                secret = SecretStore(db).get(configured.removeprefix("$secret:"))
                if secret:
                    return ResolvedFalkorPassword(
                        value=validate_falkordb_password(secret),
                        source="reused",
                        expose_value=False,
                    )
            return ResolvedFalkorPassword(
                value=validate_falkordb_password(configured),
                source="reused",
                expose_value=False,
            )
    finally:
        db.close()

    return ResolvedFalkorPassword(
        value=_generate_falkordb_password(),
        source="generated",
        expose_value=True,
    )


def _refresh_unified_compose(services_dir: Path) -> Path:
    """Overwrite the services compose file with the current FalkorDB template."""
    services_dir.mkdir(parents=True, exist_ok=True)
    compose_file = services_dir / "docker-compose.yml"
    if compose_file.exists():
        legacy_profile = "".join(("neo", "4j"))
        try:
            subprocess.run(  # nosec B603 B607
                ["docker", "compose", "-f", str(compose_file), "--profile", legacy_profile, "down"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(services_dir),
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    shutil.copy2(_COMPOSE_SRC, compose_file)
    return compose_file


def install_falkordb(
    *,
    gobby_home: Path | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Install FalkorDB via Docker Compose."""
    home = _normalize_home(gobby_home)
    if not shutil.which("docker"):
        return {"success": False, "error": "Docker not found. Install Docker to use FalkorDB."}

    resolved = _resolve_falkordb_password(password, gobby_home=home)

    services_dir = home / "services"
    compose_file = _refresh_unified_compose(services_dir)
    env = dict(os.environ)
    env["GOBBY_FALKORDB_PASSWORD"] = resolved.value

    try:
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
            env=env,
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

    if not _wait_for_health(compose_file, services_dir, resolved.value):
        return {
            "success": False,
            "error": "Health check failed: FalkorDB did not become healthy in time",
        }

    try:
        _update_config(
            host=DEFAULT_FALKORDB_HOST,
            port=DEFAULT_FALKORDB_PORT,
            password=resolved.value,
            gobby_home=home,
        )
    except Exception as exc:
        logger.warning("Failed to persist FalkorDB config: %s", exc)
        return {
            "success": False,
            "error": (
                "Failed to persist FalkorDB credentials to config_store; run "
                "'gobby uninstall --falkordb' to clean up the running container, then retry."
            ),
            "compose_running": True,
        }

    if not _write_bootstrap_password(resolved.value, home):
        return {
            "success": False,
            "error": (
                "FalkorDB is running and credentials are persisted to config_store, but the "
                "bootstrap.yaml write failed. Run 'gobby uninstall --falkordb' to roll back "
                "the container and config_store, then retry."
            ),
            "compose_running": True,
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
    return response


def uninstall_falkordb(
    *,
    gobby_home: Path | None = None,
    purge: bool = False,
) -> dict[str, Any]:
    """Uninstall FalkorDB service and clear connection credentials."""
    home = _normalize_home(gobby_home)
    services_dir = home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if compose_file.exists():
        try:
            result = subprocess.run(  # nosec B603 B607
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "--profile",
                    "falkordb",
                    "down",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(services_dir),
            )
            if result.returncode != 0:
                logger.warning("Docker compose down failed: %s", result.stderr or result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("Docker compose down timed out")
        except OSError as exc:
            logger.warning("Docker compose down failed: %s", exc)

    if purge:
        try:
            subprocess.run(  # nosec B603 B607
                ["docker", "volume", "rm", FALKORDB_DATA_VOLUME],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Failed to remove Docker volume %s: %s", FALKORDB_DATA_VOLUME, exc)

    _clear_config(gobby_home=home)
    _clear_bootstrap_password(home)

    return {"success": True, "data_removed": purge}


def _wait_for_health(
    compose_file: Path,
    services_dir: Path,
    password: str,
    retries: int = 30,
    interval: float = 2.0,
) -> bool:
    return asyncio.run(
        _wait_for_health_async(compose_file, services_dir, password, retries, interval)
    )


async def _wait_for_health_async(
    compose_file: Path,
    services_dir: Path,
    password: str,
    retries: int = 30,
    interval: float = 2.0,
) -> bool:
    for _ in range(retries):
        try:
            result = subprocess.run(  # nosec B603 B607
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "exec",
                    "-T",
                    "falkordb",
                    "redis-cli",
                    "-a",
                    password,
                    "PING",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(services_dir),
            )
            if result.returncode == 0 and "PONG" in result.stdout:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        await asyncio.sleep(interval)
    return False


def _update_config(*, host: str, port: int, password: str, gobby_home: Path) -> None:
    db = _open_local_config_db(gobby_home)
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.secrets import SecretStore

        store = ConfigStore(db)
        secret_store = SecretStore(db)
        with db.transaction():
            _clear_legacy_neo4j_config(store)
            store.set("databases.falkordb.host", host, source="install")
            store.set("databases.falkordb.port", port, source="install")
            store.set_secret(
                "databases.falkordb.requirepass",
                password,
                secret_store,
                source="install",
            )
    finally:
        db.close()


def _clear_config(*, gobby_home: Path) -> None:
    db = _open_local_config_db(gobby_home)
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.secrets import SecretStore

        store = ConfigStore(db)
        secret_store = SecretStore(db)
        store.clear_secret("databases.falkordb.requirepass", secret_store)
        _clear_legacy_neo4j_config(store)
        for key in ("databases.falkordb.host", "databases.falkordb.port"):
            store.delete(key)
    except Exception as exc:
        logger.warning("Failed to clear FalkorDB config: %s", exc)
    finally:
        db.close()


def _clear_legacy_neo4j_config(store: Any) -> None:
    """Drop legacy config_store keys owned by the old Neo4j backend."""
    for key in _LEGACY_NEO4J_CONFIG_KEYS:
        store.delete(key)


def _write_bootstrap_password(password: str, gobby_home: Path) -> bool:
    bootstrap_path = gobby_home / "bootstrap.yaml"
    try:
        data: dict[str, Any] = {}
        if bootstrap_path.exists():
            with open(bootstrap_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        data["falkordb_password"] = password
        gobby_home.mkdir(parents=True, exist_ok=True)
        with open(bootstrap_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False)
        bootstrap_path.chmod(0o600)
        return True
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to write FalkorDB password to bootstrap.yaml: %s", exc)
        return False


def _clear_bootstrap_password(gobby_home: Path) -> None:
    bootstrap_path = gobby_home / "bootstrap.yaml"
    if not bootstrap_path.exists():
        return
    try:
        with open(bootstrap_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            data.pop("falkordb_password", None)
            with open(bootstrap_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, default_flow_style=False)
            bootstrap_path.chmod(0o600)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to clear FalkorDB password from bootstrap.yaml: %s", exc)
