"""PostgreSQL service installation, uninstallation, and status checks."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess  # nosec B404 # subprocess needed for docker, pg_isready, dpkg
import time
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import click
import psycopg

from gobby.cli import postgres_bootstrap as _bootstrap
from gobby.cli.postgres_bootstrap import InstallMode
from gobby.config.bootstrap import BootstrapConfigError

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_POSTGRES_PORT = 60891
DEFAULT_POSTGRES_DB = "gobby"
DEFAULT_POSTGRES_USER = "gobby"
DEFAULT_POSTGRES_PASSWORD = "gobby_dev"
POSTGRES_DATA_VOLUME = "gobby_postgres_data"
PGAUDIT_LOG_VOLUME = "gobby_pgaudit_log"


def install_postgres(
    *,
    mode: InstallMode | str = "docker",
    dsn: str | None = None,
    gobby_home: Path | None = None,
    port: int = DEFAULT_POSTGRES_PORT,
) -> dict[str, Any]:
    """Install or configure PostgreSQL for the Gobby hub."""
    if mode != "docker":
        raise click.ClickException(
            f"Unsupported PostgreSQL install mode: {mode}. Docker is the only supported mode."
        )
    return _install_docker(gobby_home=gobby_home, port=port)


def uninstall_postgres(
    *,
    mode: InstallMode | str = "docker",
    gobby_home: Path | None = None,
    remove_data: bool = False,
) -> dict[str, Any]:
    """Uninstall or disconnect PostgreSQL according to the recorded install mode."""
    if mode != "docker":
        raise click.ClickException(
            f"Unsupported PostgreSQL install mode: {mode}. Docker is the only supported mode."
        )
    return _uninstall_docker(gobby_home=gobby_home, remove_data=remove_data)


def _install_docker(*, gobby_home: Path | None, port: int) -> dict[str, Any]:
    home = gobby_home or Path("~/.gobby").expanduser()
    if not shutil.which("docker"):
        return {
            "success": False,
            "error": "Docker not found. Install Docker and retry.",
        }

    services_dir = home / "services"
    compose_file = _ensure_unified_compose(services_dir)
    _sync_postgres_pgsearch_assets(gobby_home=home)
    _write_compose_env(gobby_home=home)

    env = dict(os.environ)
    env["GOBBY_POSTGRES_PORT"] = str(port)
    env.setdefault("GOBBY_POSTGRES_DB", DEFAULT_POSTGRES_DB)
    env.setdefault("GOBBY_POSTGRES_USER", DEFAULT_POSTGRES_USER)
    env.setdefault("GOBBY_POSTGRES_PASSWORD", DEFAULT_POSTGRES_PASSWORD)

    try:
        result = subprocess.run(  # nosec B603 B607 # fixed docker compose command
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--profile",
                "postgres",
                "up",
                "-d",
                "--remove-orphans",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(services_dir),
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Docker compose up timed out after 180s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"success": False, "error": f"Docker compose execution failed: {exc}"}

    if result.returncode != 0:
        return {
            "success": False,
            "error": f"Docker compose up failed: {result.stderr or result.stdout}",
        }

    if not _wait_for_pg_isready(compose_file=compose_file, services_dir=services_dir):
        return {"success": False, "error": "PostgreSQL did not become ready before timeout"}

    database_url = _docker_database_url(port)
    _probe_create_pg_search_extension(
        dsn=database_url,
        sql="CREATE EXTENSION IF NOT EXISTS pg_search",
    )
    _write_bootstrap_defaults(gobby_home=home, mode="docker", database_url=database_url)

    return {
        "success": True,
        "mode": "docker",
        "database_url": database_url,
        "compose_file": str(compose_file),
        "message": "PostgreSQL installed via Docker Compose.",
    }


async def get_postgres_status(
    *,
    gobby_home: Path | None = None,
    mode: InstallMode | None = None,
    dsn: str | None = None,
    readiness_timeout: float = 10.0,
    connect_timeout: int = 5,
) -> dict[str, Any]:
    """Return the stable PostgreSQL status payload used by runbooks."""
    home = gobby_home or Path("~/.gobby").expanduser()
    active_mode = mode or _active_install_mode(gobby_home=home)
    bootstrap_error: str | None = None
    try:
        bootstrap_database_url = _read_bootstrap_database_url(home)
    except BootstrapConfigError as exc:
        bootstrap_database_url = None
        bootstrap_error = str(exc)
    database_url = dsn or bootstrap_database_url
    if bootstrap_error and database_url is None:
        return {
            "available": False,
            "mode": active_mode,
            "dsn_host": None,
            "dsn_db": None,
            "healthy": False,
            "error": bootstrap_error,
            "extensions": {"pg_search": False, "pgaudit": False},
            "preload_libraries": [],
        }
    database_url = database_url or _docker_database_url(DEFAULT_POSTGRES_PORT)

    payload: dict[str, Any] = {
        "mode": active_mode,
        "dsn_host": _dsn_host(database_url),
        "dsn_db": _dsn_db(database_url),
        "healthy": _pg_isready(
            database_url,
            timeout=readiness_timeout,
            connect_timeout=connect_timeout,
        ),
        "extensions": {"pg_search": False, "pgaudit": False},
        "preload_libraries": [],
    }
    if bootstrap_error:
        payload["error"] = bootstrap_error

    try:
        with psycopg.connect(database_url, connect_timeout=connect_timeout) as conn:
            payload["extensions"] = {
                "pg_search": _extension_present(conn, "pg_search"),
                "pgaudit": _extension_present(conn, "pgaudit"),
            }
            payload["preload_libraries"] = _preload_libraries(conn)
    except psycopg.Error as exc:
        logger.debug("PostgreSQL status connection failed: %s", exc)

    return payload


def render_postgres_status(payload: dict[str, Any]) -> str:
    """Render a concise human-readable PostgreSQL status."""
    lines = [
        f"Mode:        {payload.get('mode')}",
        f"Host:        {payload.get('dsn_host') or 'unknown'}",
        f"Database:    {payload.get('dsn_db') or 'unknown'}",
        f"Healthy:     {'yes' if payload.get('healthy') else 'no'}",
    ]
    extensions = cast(dict[str, bool], payload.get("extensions", {}))
    lines.append(f"pg_search:   {'yes' if extensions.get('pg_search') else 'no'}")
    lines.append(f"pgaudit:     {'yes' if extensions.get('pgaudit') else 'no'}")
    ownership = payload.get("ownership")
    if isinstance(ownership, dict):
        lines.append(
            f"Ownership:   {'present' if ownership.get('sentinel_present') else 'missing'}"
        )
    error = payload.get("error")
    if error:
        lines.append(f"Error:       {error}")
    return "\n".join(lines)


def _uninstall_docker(*, gobby_home: Path | None, remove_data: bool) -> dict[str, Any]:
    home = gobby_home or _bootstrap.default_gobby_home()
    _preserve_required_postgres_runtime(home)
    services_dir = home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if compose_file.exists():
        try:
            result = subprocess.run(  # nosec B603 B607 # fixed docker compose command
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "--profile",
                    "postgres",
                    "down",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=str(services_dir),
            )
            if result.returncode != 0:
                logger.warning(
                    "Docker compose postgres down failed: %s", result.stderr or result.stdout
                )
        except subprocess.TimeoutExpired:
            logger.warning("Docker compose postgres down timed out")
        except OSError as exc:
            logger.warning("Docker compose postgres down failed: %s", exc)

    if remove_data:
        _remove_docker_volumes((POSTGRES_DATA_VOLUME, PGAUDIT_LOG_VOLUME))

    return {
        "success": True,
        "mode": "docker",
        "data_removed": remove_data,
        "message": (
            "PostgreSQL Docker service cleanup completed; runtime bootstrap preserved "
            "because PostgreSQL is the only supported hub backend."
        ),
    }


def _ensure_unified_compose(services_dir: Path) -> Path:
    dest = services_dir / "docker-compose.yml"
    services_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_COMPOSE_SRC, dest)
    return dest


def _sync_postgres_pgsearch_assets(*, gobby_home: Path | None = None) -> Path:
    """Copy the bundled postgres-pgsearch asset tree into the user services dir."""
    home = gobby_home or Path("~/.gobby").expanduser()
    target_root = home / "services" / "postgres-pgsearch"
    source_ref = resources.files("gobby").joinpath("data/postgres-pgsearch")
    with resources.as_file(source_ref) as source_root:
        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, target_root)
    return target_root


def _write_compose_env(*, gobby_home: Path | None = None) -> Path:
    """Write .env values needed for deterministic local pg_search image builds."""
    home = gobby_home or Path("~/.gobby").expanduser()
    manifest = _read_pgsearch_version_manifest()
    env_path = home / "services" / ".env"
    updates = {
        "GOBBY_PG_SEARCH_VERSION": manifest["pg_search_version"],
        "GOBBY_PG_SEARCH_SHA256": manifest["pg_search_sha256"],
    }
    _update_env_file(env_path, updates)
    return env_path


def _read_pgsearch_version_manifest() -> dict[str, str]:
    manifest_ref = resources.files("gobby").joinpath("data/postgres-pgsearch/version.json")
    data = json.loads(manifest_ref.read_text(encoding="utf-8"))
    sha_by_arch = data.get("pg_search_sha256_by_arch", {})
    debian_arch = _debian_arch(platform.machine())
    sha256 = (sha_by_arch.get(debian_arch) if isinstance(sha_by_arch, dict) else None) or data[
        "pg_search_sha256"
    ]
    return {
        "pg_search_version": str(data["pg_search_version"]),
        "pg_search_sha256": str(sha256),
        "postgres_major": str(data["postgres_major"]),
    }


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("=", 1)[0] not in updates
        ]
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(f"{key}={value}" for key, value in updates.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_bootstrap_defaults(
    *,
    gobby_home: Path,
    mode: InstallMode,
    database_url: str,
) -> None:
    try:
        _bootstrap.write_postgres_defaults(
            gobby_home=gobby_home,
            mode=mode,
            database_url=database_url,
        )
    except BootstrapConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _preserve_required_postgres_runtime(gobby_home: Path) -> None:
    try:
        _bootstrap.clear_postgres_fields(gobby_home)
    except BootstrapConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _read_bootstrap_yaml(path: Path) -> dict[str, Any]:
    return _bootstrap.read_bootstrap_yaml(path)


def _wait_for_pg_isready(
    *,
    compose_file: Path,
    services_dir: Path,
    retries: int = 30,
    interval: float = 2.0,
) -> bool:
    for _ in range(retries):
        try:
            result = subprocess.run(  # nosec B603 B607 # fixed docker compose command
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "--profile",
                    "postgres",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_isready",
                    "-U",
                    DEFAULT_POSTGRES_USER,
                    "-d",
                    DEFAULT_POSTGRES_DB,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(services_dir),
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass
        time.sleep(interval)
    return False


def _pg_isready(dsn: str, *, timeout: float = 10.0, connect_timeout: int = 5) -> bool:
    try:
        result = subprocess.run(  # nosec B603 B607 # fixed pg_isready command
            ["pg_isready", "-d", dsn],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        try:
            with psycopg.connect(dsn, connect_timeout=connect_timeout):
                return True
        except psycopg.Error:
            return False


def _probe_create_pg_search_extension(*, dsn: str, sql: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.execute(sql)
            _commit_if_supported(conn)
    except psycopg.Error as exc:
        raise click.ClickException(f"pg_search probe failed: {exc}") from exc


def _extension_present(conn: Any, extension: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_extension WHERE extname = %s",
        (extension,),
    ).fetchone()
    return bool(row)


def _preload_libraries(conn: Any) -> list[str]:
    row = conn.execute(
        "SELECT setting FROM pg_settings WHERE name = 'shared_preload_libraries'"
    ).fetchone()
    if not row or not row[0]:
        return []
    return [item.strip() for item in str(row[0]).split(",") if item.strip()]


def _commit_if_supported(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _debian_arch(machine: str) -> str:
    normalized = machine.lower()
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized


def _docker_database_url(port: int) -> str:
    return (
        f"postgresql://{DEFAULT_POSTGRES_USER}:{DEFAULT_POSTGRES_PASSWORD}"
        f"@localhost:{port}/{DEFAULT_POSTGRES_DB}"
    )


def _read_bootstrap_database_url(gobby_home: Path) -> str | None:
    return _bootstrap.read_bootstrap_database_url(gobby_home)


def _active_install_mode(*, gobby_home: Path | None = None) -> InstallMode:
    return _bootstrap.active_install_mode(gobby_home=gobby_home)


def _dsn_host(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return parsed.hostname


def _dsn_db(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return parsed.path.lstrip("/") or None


def _remove_docker_volumes(volume_names: tuple[str, ...]) -> None:
    for volume_name in volume_names:
        try:
            subprocess.run(  # nosec B603 B607 # fixed docker volume command
                ["docker", "volume", "rm", volume_name],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
            logger.warning("Failed to remove Docker volume %s: %s", volume_name, exc)
