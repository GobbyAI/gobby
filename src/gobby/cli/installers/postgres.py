"""PostgreSQL service installation and status checks."""

from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import shutil
import subprocess  # nosec B404 # subprocess needed for docker, pg_isready, dpkg
import time
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse

import click
import psycopg

from gobby.cli import postgres_bootstrap as _bootstrap
from gobby.code_index.bm25_health import (
    render_bm25_status,
    unavailable_bm25_status,
    verify_bm25_indexes,
)
from gobby.config.bootstrap import BootstrapConfigError
from gobby.paths import get_gobby_home
from gobby.utils.postgres_extensions import BASELINE_POSTGRES_EXTENSIONS

from .compose_env import ComposeEnvironmentError, ComposeRuntime, resolve_compose_runtime
from .managed_services_lock import ManagedServicesLockError, managed_services_lock

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_POSTGRES_PORT = 60891
DEFAULT_POSTGRES_DB = "gobby"
DEFAULT_POSTGRES_USER = "gobby"


def install_postgres(
    *,
    gobby_home: Path | None = None,
    port: int = DEFAULT_POSTGRES_PORT,
) -> dict[str, Any]:
    """Install or configure PostgreSQL for the Gobby hub."""
    home = gobby_home or get_gobby_home()
    try:
        with managed_services_lock(home, operation="postgres installer refresh"):
            return _install_docker(gobby_home=home, port=port)
    except ManagedServicesLockError as exc:
        return {"success": False, "error": str(exc)}


def _install_docker(*, gobby_home: Path | None, port: int) -> dict[str, Any]:
    home = gobby_home or get_gobby_home()
    if not shutil.which("docker"):
        return {
            "success": False,
            "error": "Docker not found. Install Docker and retry.",
        }

    services_dir = home / "services"
    compose_file = _ensure_unified_compose(services_dir)
    _sync_postgres_pgsearch_assets(gobby_home=home)
    try:
        database_url, runtime = _resolve_postgres_install_database_url(
            gobby_home=home,
            port=port,
        )
        env = runtime.environment
    except (BootstrapConfigError, ComposeEnvironmentError, KeyError, click.ClickException) as exc:
        return {"success": False, "error": str(exc)}

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

    if not _wait_for_pg_isready(
        compose_file=compose_file,
        services_dir=services_dir,
        env=env,
    ):
        return {"success": False, "error": "PostgreSQL did not become ready before timeout"}

    for extension in BASELINE_POSTGRES_EXTENSIONS:
        _probe_create_extension(
            dsn=database_url,
            sql=f"CREATE EXTENSION IF NOT EXISTS {extension}",
        )
    _write_bootstrap_defaults(gobby_home=home, database_url=database_url)

    return {
        "success": True,
        "database_url": database_url,
        "compose_file": str(compose_file),
        "message": "PostgreSQL installed via Docker Compose.",
    }


async def get_postgres_status(
    *,
    gobby_home: Path | None = None,
    dsn: str | None = None,
    readiness_timeout: float = 10.0,
    connect_timeout: int = 5,
) -> dict[str, Any]:
    """Return the stable PostgreSQL status payload used by runbooks."""
    home = gobby_home or get_gobby_home()
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
            "dsn_host": None,
            "dsn_db": None,
            "healthy": False,
            "error": bootstrap_error,
            "extensions": dict.fromkeys(BASELINE_POSTGRES_EXTENSIONS, False),
            "preload_libraries": [],
            "code_index": unavailable_bm25_status(bootstrap_error),
        }
    if database_url is None:
        error = f"{home / 'bootstrap.yaml'} does not define database_url"
        return {
            "available": False,
            "dsn_host": None,
            "dsn_db": None,
            "healthy": False,
            "error": error,
            "extensions": dict.fromkeys(BASELINE_POSTGRES_EXTENSIONS, False),
            "preload_libraries": [],
            "code_index": unavailable_bm25_status(error),
        }

    payload: dict[str, Any] = {
        "dsn_host": _dsn_host(database_url),
        "dsn_db": _dsn_db(database_url),
        "healthy": _pg_isready(
            database_url,
            timeout=readiness_timeout,
            connect_timeout=connect_timeout,
        ),
        "extensions": dict.fromkeys(BASELINE_POSTGRES_EXTENSIONS, False),
        "preload_libraries": [],
        "code_index": unavailable_bm25_status("PostgreSQL status connection unavailable"),
    }
    if bootstrap_error:
        payload["error"] = bootstrap_error

    try:
        with psycopg.connect(database_url, connect_timeout=connect_timeout) as conn:
            payload["extensions"] = {
                extension: _extension_present(conn, extension)
                for extension in BASELINE_POSTGRES_EXTENSIONS
            }
            payload["preload_libraries"] = _preload_libraries(conn)
            payload["code_index"] = verify_bm25_indexes(conn)
    except psycopg.Error as exc:
        logger.debug("PostgreSQL status connection failed: %s", exc)
        payload["code_index"] = unavailable_bm25_status(str(exc))

    return payload


def render_postgres_status(payload: dict[str, Any]) -> str:
    """Render a concise human-readable PostgreSQL status."""
    lines = [
        f"Host:        {payload.get('dsn_host') or 'unknown'}",
        f"Database:    {payload.get('dsn_db') or 'unknown'}",
        f"Healthy:     {'yes' if payload.get('healthy') else 'no'}",
    ]
    extensions = cast(dict[str, bool], payload.get("extensions", {}))
    for extension in BASELINE_POSTGRES_EXTENSIONS:
        lines.append(f"{extension + ':':<13}{'yes' if extensions.get(extension) else 'no'}")
    ownership = payload.get("ownership")
    if isinstance(ownership, dict):
        lines.append(
            f"Ownership:   {'present' if ownership.get('sentinel_present') else 'missing'}"
        )
    code_index = payload.get("code_index")
    if isinstance(code_index, dict):
        lines.extend(render_bm25_status(code_index))
    error = payload.get("error")
    if error:
        lines.append(f"Error:       {error}")
    return "\n".join(lines)


def _ensure_unified_compose(services_dir: Path) -> Path:
    dest = services_dir / "docker-compose.yml"
    services_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_COMPOSE_SRC, dest)
    return dest


def _sync_postgres_pgsearch_assets(*, gobby_home: Path | None = None) -> Path:
    """Copy the bundled postgres-pgsearch asset tree into the user services dir."""
    home = gobby_home or get_gobby_home()
    target_root = home / "services" / "postgres-pgsearch"
    source_ref = resources.files("gobby").joinpath("data/postgres-pgsearch")
    with resources.as_file(source_ref) as source_root:
        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, target_root)
    return target_root


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


def _resolve_postgres_install_database_url(
    *,
    gobby_home: Path,
    port: int,
) -> tuple[str, ComposeRuntime]:
    bootstrap_path = gobby_home / "bootstrap.yaml"
    if bootstrap_path.exists():
        database_url = _read_bootstrap_database_url(gobby_home)
        if not database_url:
            raise click.ClickException(
                f"{bootstrap_path} is missing database_url; repair it before reinstalling PostgreSQL"
            )
        runtime = resolve_compose_runtime(
            gobby_home,
            database_url=database_url,
            profiles=("postgres",),
        )
        return _database_url_from_compose_environment(runtime.environment), runtime

    password = os.environ.get("GOBBY_POSTGRES_PASSWORD") or secrets.token_urlsafe(32)
    runtime = resolve_compose_runtime(
        gobby_home,
        database_url=_docker_database_url(port, password=password),
        profiles=("postgres",),
    )
    return _database_url_from_compose_environment(runtime.environment), runtime


def _write_bootstrap_defaults(
    *,
    gobby_home: Path,
    database_url: str,
) -> None:
    try:
        _bootstrap.write_postgres_defaults(
            gobby_home=gobby_home,
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
    env: dict[str, str],
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
                env=env,
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


def _probe_create_extension(*, dsn: str, sql: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.execute(sql)
            _commit_if_supported(conn)
    except psycopg.Error as exc:
        raise click.ClickException(f"PostgreSQL extension probe failed: {exc}") from exc


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


def _docker_database_url(
    port: int,
    password: str,
) -> str:
    if not password:
        raise click.ClickException(
            "PostgreSQL credentials are unavailable; run `gobby install` to configure them."
        )
    return (
        f"postgresql://{quote(DEFAULT_POSTGRES_USER, safe='')}:{quote(password, safe='')}"
        f"@localhost:{port}/{DEFAULT_POSTGRES_DB}"
    )


def _database_url_from_compose_environment(env: dict[str, str]) -> str:
    user = quote(env["GOBBY_POSTGRES_USER"], safe="")
    password = quote(env["GOBBY_POSTGRES_PASSWORD"], safe="")
    port = env["GOBBY_POSTGRES_PORT"]
    database = quote(env["GOBBY_POSTGRES_DB"], safe="")
    return f"postgresql://{user}:{password}@localhost:{port}/{database}"


def _read_bootstrap_database_url(gobby_home: Path) -> str | None:
    return _bootstrap.read_bootstrap_database_url(gobby_home)


def _dsn_host(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return parsed.hostname


def _dsn_db(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return parsed.path.lstrip("/") or None
