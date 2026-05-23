"""FalkorDB service installation and uninstallation."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess  # nosec B404 # subprocess needed for docker compose management
import time
from pathlib import Path
from typing import Any

from gobby.config.persistence import validate_falkordb_password

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_FALKORDB_HOST = "127.0.0.1"
DEFAULT_FALKORDB_PORT = 16379
DEFAULT_FALKORDB_BROWSER_URL = "http://localhost:13000"

FALKORDB_DATA_VOLUME = "gobby_falkordb_data"


def _resolve_falkordb_password(password: str | None = None) -> str:
    if password:
        return validate_falkordb_password(password)
    env_password = os.environ.get("GOBBY_FALKORDB_PASSWORD")
    if env_password:
        return validate_falkordb_password(env_password)
    return secrets.token_urlsafe(24).replace("-", "_")


def _ensure_unified_compose(services_dir: Path) -> Path:
    dest = services_dir / "docker-compose.yml"
    if not dest.exists():
        services_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_COMPOSE_SRC, dest)
    return dest


def install_falkordb(
    *,
    gobby_home: Path | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Install FalkorDB via Docker Compose."""
    home = gobby_home or Path(os.environ.get("GOBBY_HOME", "~/.gobby")).expanduser()

    if not shutil.which("docker"):
        return {"success": False, "error": "Docker not found. Install Docker to use FalkorDB."}

    try:
        falkordb_password = _resolve_falkordb_password(password)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    services_dir = home / "services"
    compose_file = _ensure_unified_compose(services_dir)
    env = dict(os.environ)
    env["GOBBY_FALKORDB_PASSWORD"] = falkordb_password

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded docker command
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

    if not _wait_for_redis_ping(falkordb_password):
        return {
            "success": False,
            "error": "Health check failed: FalkorDB did not become healthy in time",
        }

    config_ok = _update_config(falkordb_password)
    response: dict[str, Any] = {
        "success": True,
        "host": DEFAULT_FALKORDB_HOST,
        "port": DEFAULT_FALKORDB_PORT,
        "browser_url": DEFAULT_FALKORDB_BROWSER_URL,
        "compose_file": str(compose_file),
        "mode": "local",
    }
    if not config_ok:
        response["warning"] = "FalkorDB is running but failed to persist daemon config"
    return response


def uninstall_falkordb(
    *,
    gobby_home: Path | None = None,
    remove_volumes: bool = False,
) -> dict[str, Any]:
    """Uninstall FalkorDB services."""
    home = gobby_home or Path(os.environ.get("GOBBY_HOME", "~/.gobby")).expanduser()
    services_dir = home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if not compose_file.exists():
        _clear_config()
        return {"success": True, "already_uninstalled": True, "message": "FalkorDB not installed"}

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded docker command
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
            return {
                "success": False,
                "error": f"Docker compose down failed: {result.stderr or result.stdout}",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Docker compose down timed out after 60s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"success": False, "error": f"Docker compose execution failed: {exc}"}

    volumes_removed = False
    if remove_volumes:
        try:
            subprocess.run(  # nosec B603 B607
                ["docker", "volume", "rm", FALKORDB_DATA_VOLUME],
                capture_output=True,
                timeout=30,
            )
            volumes_removed = True
        except (subprocess.TimeoutExpired, OSError):
            volumes_removed = False

    _clear_config()
    return {"success": True, "data_removed": remove_volumes, "volumes_removed": volumes_removed}


def _wait_for_redis_ping(password: str, retries: int = 60, interval: float = 1.0) -> bool:
    for _ in range(retries):
        if _redis_ping(password):
            return True
        time.sleep(interval)
    return False


def _redis_ping(password: str) -> bool:
    try:
        with socket.create_connection(
            (DEFAULT_FALKORDB_HOST, DEFAULT_FALKORDB_PORT), timeout=2
        ) as sock:
            sock.sendall(_resp_command("AUTH", password))
            auth = sock.recv(512)
            if not auth.startswith(b"+OK"):
                return False
            sock.sendall(_resp_command("PING"))
            return sock.recv(512).startswith(b"+PONG")
    except OSError:
        return False


def _resp_command(*parts: str) -> bytes:
    encoded = [part.encode() for part in parts]
    payload = f"*{len(encoded)}\r\n".encode()
    for part in encoded:
        payload += b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
    return payload


def _update_config(password: str) -> bool:
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.hub.runtime import open_runtime_hub_database
        from gobby.storage.secrets import SecretStore

        db = open_runtime_hub_database(apply_migrations=False)
        try:
            store = ConfigStore(db)
            store.set("databases.falkordb.host", DEFAULT_FALKORDB_HOST, source="install")
            store.set("databases.falkordb.port", DEFAULT_FALKORDB_PORT, source="install")
            store.set_secret(
                "databases.falkordb.requirepass",
                password,
                SecretStore(db),
                source="install",
            )
        finally:
            db.close()
        return True
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def _clear_config() -> bool:
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.hub.runtime import open_runtime_hub_database
        from gobby.storage.secrets import SecretStore

        db = open_runtime_hub_database(apply_migrations=False)
        try:
            store = ConfigStore(db)
            secret_store = SecretStore(db)
            store.delete("databases.falkordb.host")
            store.delete("databases.falkordb.port")
            store.clear_secret("databases.falkordb.requirepass", secret_store)
        finally:
            db.close()
        return True
    except (ImportError, OSError, RuntimeError, ValueError):
        return False
