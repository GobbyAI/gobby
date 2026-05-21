"""PostgreSQL service installation, uninstallation, and status checks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess  # nosec B404 # subprocess needed for docker, pg_isready, dpkg
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import click
import psycopg

from gobby.cli import postgres_bootstrap as _bootstrap
from gobby.cli.postgres_bootstrap import InstallMode
from gobby.config.bootstrap import BootstrapConfigError, inspect_postgres_keyring
from gobby.utils.version import get_version

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_POSTGRES_PORT = 60891
DEFAULT_POSTGRES_DB = "gobby"
DEFAULT_POSTGRES_USER = "gobby"
DEFAULT_POSTGRES_PASSWORD = "gobby_dev"
POSTGRES_DATA_VOLUME = "gobby_postgres_data"
PGAUDIT_LOG_VOLUME = "gobby_pgaudit_log"


@dataclass(frozen=True)
class PlatformInfo:
    """Normalized platform data for native install dispatch."""

    os: str
    distro: str
    arch: str


def install_postgres(
    *,
    mode: InstallMode = "docker",
    dsn: str | None = None,
    gobby_home: Path | None = None,
    port: int = DEFAULT_POSTGRES_PORT,
) -> dict[str, Any]:
    """Install or configure PostgreSQL for the Gobby hub."""
    if mode == "docker":
        return _install_docker(gobby_home=gobby_home, port=port)
    if mode == "native":
        return _install_native(gobby_home=gobby_home, dsn=dsn)
    if mode == "external":
        if not dsn:
            raise click.ClickException("--mode external requires --dsn")
        return _install_external(gobby_home=gobby_home, dsn=dsn)
    raise click.ClickException(f"Unknown install mode: {mode}")


def uninstall_postgres(
    *,
    mode: InstallMode = "docker",
    gobby_home: Path | None = None,
    remove_data: bool = False,
) -> dict[str, Any]:
    """Uninstall or disconnect PostgreSQL according to the recorded install mode."""
    if mode == "docker":
        return _uninstall_docker(gobby_home=gobby_home, remove_data=remove_data)
    if mode == "native":
        return _uninstall_native(gobby_home=gobby_home, remove_data=remove_data)
    if mode == "external":
        if remove_data:
            raise click.ClickException(
                "External mode never deletes server-side data. Reset the dedicated "
                "database manually with `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` "
                "inside the dedicated database, or drop and recreate the database."
            )
        home = gobby_home or _bootstrap.default_gobby_home()
        _preserve_required_postgres_runtime(home)
        return {
            "success": True,
            "mode": "external",
            "message": (
                "External PostgreSQL service cleanup completed; server left untouched; "
                "runtime bootstrap preserved "
                "because PostgreSQL is the only supported hub backend."
            ),
        }
    raise click.ClickException(f"Unknown install mode: {mode}")


def _install_docker(*, gobby_home: Path | None, port: int) -> dict[str, Any]:
    home = gobby_home or Path("~/.gobby").expanduser()
    if not shutil.which("docker"):
        return {
            "success": False,
            "error": "Docker not found. Use --mode external or install Docker.",
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


def _install_native(*, gobby_home: Path | None, dsn: str | None) -> dict[str, Any]:
    home = gobby_home or Path("~/.gobby").expanduser()
    platform_info = _detect_platform()
    if platform_info.os == "linux" and platform_info.distro in {"debian", "ubuntu"}:
        return _install_native_debian(gobby_home=home, dsn=dsn)
    if platform_info.os == "darwin":
        raise click.ClickException(
            "macOS native pg_search is not supported upstream. "
            "Use `gobby postgres install --mode docker` (recommended), or follow "
            "docs/runbooks/postgres-native-macos.md and re-run with --mode external."
        )
    raise click.ClickException(
        f"Native install on {platform_info.distro or platform_info.os} requires building "
        "pg_search from source. See docs/runbooks/postgres-native-source.md, or use "
        "`gobby postgres install --mode docker` (recommended)."
    )


def _install_native_debian(*, gobby_home: Path, dsn: str | None) -> dict[str, Any]:
    manifest = _read_pgsearch_version_manifest()
    version = manifest["pg_search_version"]
    sha256 = manifest["pg_search_sha256"]
    database_url = dsn or _auto_discover_local_dsn()

    deb_path = _download_pg_search_deb(version=version, sha256=sha256)
    _install_deb_with_sudo(deb_path=deb_path)
    _probe_create_pg_search_extension(dsn=database_url, sql="CREATE EXTENSION pg_search")
    _write_bootstrap_defaults(gobby_home=gobby_home, mode="native", database_url=database_url)

    return {
        "success": True,
        "mode": "native",
        "database_url": database_url,
        "pg_search_version": version,
        "message": "PostgreSQL native pg_search install completed.",
    }


def _install_external(*, gobby_home: Path | None, dsn: str) -> dict[str, Any]:
    home = gobby_home or Path("~/.gobby").expanduser()
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        probe = _run_external_read_only_probes(conn)
        _create_external_ownership_sentinel(conn)
        _commit_if_supported(conn)

    _write_bootstrap_defaults(gobby_home=home, mode="external", database_url=dsn)
    return {
        "success": True,
        "mode": "external",
        "database_url": dsn,
        "pgaudit_available": probe["pgaudit_available"],
        "message": "External PostgreSQL passed ownership and extension probes.",
    }


async def get_postgres_status(
    *,
    gobby_home: Path | None = None,
    mode: InstallMode | None = None,
    dsn: str | None = None,
    readiness_timeout: float = 10.0,
    connect_timeout: int = 5,
) -> dict[str, Any]:
    """Return the stable PostgreSQL status payload used by cutover runbooks."""
    home = gobby_home or Path("~/.gobby").expanduser()
    active_mode = mode or _active_install_mode(gobby_home=home)
    bootstrap_error: str | None = None
    try:
        bootstrap_database_url = _read_bootstrap_database_url(home)
    except BootstrapConfigError as exc:
        bootstrap_database_url = None
        bootstrap_error = str(exc)
    keyring_status = _postgres_keyring_status(home)
    if bootstrap_error:
        keyring_status["error"] = bootstrap_error
        keyring_status["available"] = False
    database_url = dsn or bootstrap_database_url or _docker_database_url(DEFAULT_POSTGRES_PORT)

    payload: dict[str, Any] = {
        "mode": active_mode,
        "dsn_host": _dsn_host(database_url),
        "dsn_db": _dsn_db(database_url),
        "healthy": _pg_isready(
            database_url,
            timeout=readiness_timeout,
            connect_timeout=connect_timeout,
        ),
        "keyring": keyring_status,
        "extensions": {"pg_search": False, "pgaudit": False},
        "preload_libraries": [],
        "migration_complete": {"present": False, "imported_at": None},
    }

    try:
        with psycopg.connect(database_url, connect_timeout=connect_timeout) as conn:
            payload["extensions"] = {
                "pg_search": _extension_present(conn, "pg_search"),
                "pgaudit": _extension_present(conn, "pgaudit"),
            }
            payload["preload_libraries"] = _preload_libraries(conn)
            payload["migration_complete"] = _migration_complete(conn)
            if active_mode == "external":
                payload["ownership"] = _external_ownership_status(conn)
    except psycopg.Error as exc:
        logger.debug("PostgreSQL status connection failed: %s", exc)
        if active_mode == "external":
            payload["ownership"] = {
                "sentinel_present": False,
                "installed_at": None,
                "gobby_version": None,
            }

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
    migration = cast(dict[str, Any], payload.get("migration_complete", {}))
    lines.append(f"Migration:   {'complete' if migration.get('present') else 'not complete'}")
    ownership = payload.get("ownership")
    if isinstance(ownership, dict):
        lines.append(
            f"Ownership:   {'present' if ownership.get('sentinel_present') else 'missing'}"
        )
    keyring_status = payload.get("keyring")
    if isinstance(keyring_status, dict):
        lines.append(f"Keyring:     {_format_keyring_status(keyring_status)}")
        if keyring_status.get("error"):
            lines.append(f"Keyring help: {keyring_status.get('guidance')}")
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


def _uninstall_native(*, gobby_home: Path | None, remove_data: bool) -> dict[str, Any]:
    home = gobby_home or _bootstrap.default_gobby_home()
    _preserve_required_postgres_runtime(home)
    steps = [
        "Remove pg_search manually with your OS package manager if desired.",
        "Gobby does not run apt-get remove for native PostgreSQL installations.",
        "Runtime bootstrap remains configured for PostgreSQL because it is required.",
    ]
    if remove_data:
        steps.append("Remove the native PostgreSQL data directory manually after taking backups.")
    return {
        "success": True,
        "mode": "native",
        "message": (
            "PostgreSQL native cleanup guidance emitted; runtime bootstrap preserved "
            "because PostgreSQL is the only supported hub backend."
        ),
        "manual_steps": steps,
        "data_removed": False,
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


def _write_bootstrap_yaml(path: Path, data: dict[str, Any]) -> None:
    _bootstrap.write_bootstrap_yaml(path, data)


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


def _run_external_read_only_probes(conn: Any) -> dict[str, bool]:
    schemas = [row[0] for row in conn.execute(_EXTERNAL_SCHEMA_SQL).fetchall()]
    if set(schemas) != {"public"}:
        raise click.ClickException(
            "External mode requires a dedicated database with only the public schema. "
            "Create a fresh database for Gobby, or reset it with "
            "`DROP SCHEMA public CASCADE; CREATE SCHEMA public;`."
        )

    object_rows: list[tuple[str, str]] = []
    object_rows.extend(_named_rows(conn.execute(_EXTERNAL_CLASS_SQL).fetchall(), "relation"))
    object_rows.extend(_named_rows(conn.execute(_EXTERNAL_PROC_SQL).fetchall(), "function"))
    object_rows.extend(_named_rows(conn.execute(_EXTERNAL_TYPE_SQL).fetchall(), "type"))
    if object_rows:
        sample = ", ".join(f"{kind}:{name}" for kind, name in object_rows[:5])
        raise click.ClickException(
            "External mode requires an empty dedicated public schema. Existing objects "
            f"found: {sample}. Use a fresh database for Gobby."
        )

    if not conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_search'").fetchone():
        version_row = conn.execute("SELECT version()").fetchone()
        version_text = str(version_row[0]) if version_row else "PostgreSQL"
        raise click.ClickException(
            "pg_search extension is missing. Install it before re-running external mode. "
            f"Suggested upstream command: {_format_pg_search_install_command(version_text)}"
        )

    pgaudit_available = bool(
        conn.execute("SELECT name FROM pg_available_extensions WHERE name = 'pgaudit'").fetchone()
    )
    return {"pgaudit_available": pgaudit_available}


def _named_rows(rows: list[tuple[Any, ...]], kind: str) -> list[tuple[str, str]]:
    return [(kind, str(row[0])) for row in rows]


def _create_external_ownership_sentinel(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE gobby_install_ownership (
            id integer PRIMARY KEY,
            installed_at timestamptz NOT NULL DEFAULT now(),
            gobby_version text NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO gobby_install_ownership (id, installed_at, gobby_version)
        VALUES (1, now(), %s)
        ON CONFLICT (id) DO UPDATE
        SET installed_at = excluded.installed_at,
            gobby_version = excluded.gobby_version
        """,
        (get_version(),),
    )


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


def _migration_complete(conn: Any) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT value FROM gobby_migration_state WHERE key = 'imported_from_sqlite_at'"
        ).fetchone()
    except psycopg.Error:
        row = None
    return {"present": bool(row), "imported_at": str(row[0]) if row else None}


def _external_ownership_status(conn: Any) -> dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT installed_at, gobby_version
            FROM gobby_install_ownership
            WHERE id = 1
            """
        ).fetchone()
    except psycopg.Error:
        row = None
    return {
        "sentinel_present": bool(row),
        "installed_at": str(row[0]) if row else None,
        "gobby_version": str(row[1]) if row else None,
    }


def _commit_if_supported(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _detect_platform() -> PlatformInfo:
    system = platform.system().lower()
    distro = ""
    if system == "linux":
        distro = _linux_distro_id()
    elif system == "darwin":
        distro = "macos"
    return PlatformInfo(os=system, distro=distro, arch=platform.machine())


def _linux_distro_id() -> str:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return "linux"
    for line in os_release.read_text(encoding="utf-8").splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip('"').lower()
    return "linux"


def _download_pg_search_deb(*, version: str, sha256: str) -> Path:
    arch = _debian_arch(platform.machine())
    url = (
        "https://github.com/paradedb/paradedb/releases/download/"
        f"v{version}/postgresql-18-pg-search_{version}-1PARADEDB-trixie_{arch}.deb"
    )
    target = Path(tempfile.gettempdir()) / f"pg_search-{version}-{arch}.deb"
    with urllib.request.urlopen(url, timeout=60) as response:  # nosec B310 # fixed HTTPS URL
        target.write_bytes(response.read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != sha256:
        target.unlink(missing_ok=True)
        raise click.ClickException(
            f"Downloaded pg_search checksum mismatch: expected {sha256}, got {digest}"
        )
    return target


def _install_deb_with_sudo(*, deb_path: Path) -> None:
    result = subprocess.run(  # nosec B603 B607 # sudo dpkg is the native install action
        ["sudo", "dpkg", "-i", str(deb_path)],
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise click.ClickException("sudo dpkg -i failed while installing pg_search")


def _debian_arch(machine: str) -> str:
    normalized = machine.lower()
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized


def _auto_discover_local_dsn() -> str:
    for env_name in ("GOBBY_POSTGRES_DSN", "DATABASE_URL"):
        value = os.environ.get(env_name)
        if value:
            return value
    return "postgresql://localhost:5432/gobby"


def _docker_database_url(port: int) -> str:
    return (
        f"postgresql://{DEFAULT_POSTGRES_USER}:{DEFAULT_POSTGRES_PASSWORD}"
        f"@localhost:{port}/{DEFAULT_POSTGRES_DB}"
    )


def _read_bootstrap_database_url(gobby_home: Path) -> str | None:
    return _bootstrap.read_bootstrap_database_url(gobby_home)


def _postgres_keyring_status(gobby_home: Path) -> dict[str, Any]:
    data = _bootstrap.read_bootstrap_yaml(_bootstrap.bootstrap_path(gobby_home))
    database_url_ref = data.get("database_url_ref")
    return inspect_postgres_keyring(database_url_ref if isinstance(database_url_ref, str) else None)


def _format_keyring_status(status: dict[str, Any]) -> str:
    backend = status.get("backend") or "unknown backend"
    if status.get("error"):
        return f"unavailable ({backend})"
    if not status.get("configured"):
        return f"not configured ({backend})"
    if status.get("credential_present"):
        return f"configured, credential present ({backend})"
    if status.get("readable"):
        return f"configured, credential missing ({backend})"
    return f"configured ({backend})"


def _active_install_mode(*, gobby_home: Path | None = None) -> InstallMode:
    return _bootstrap.active_install_mode(gobby_home=gobby_home)


def _dsn_host(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return parsed.hostname


def _dsn_db(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    return parsed.path.lstrip("/") or None


def _format_pg_search_install_command(version_text: str) -> str:
    manifest = _read_pgsearch_version_manifest()
    version = manifest["pg_search_version"]
    if "debian" in version_text.lower() or "ubuntu" in version_text.lower():
        return (
            "curl -LO https://github.com/paradedb/paradedb/releases/download/"
            f"v{version}/postgresql-18-pg-search_{version}-1PARADEDB-trixie_$(dpkg "
            "--print-architecture).deb && sudo dpkg -i postgresql-18-pg-search_*.deb"
        )
    return f"Install pg_search v{version} from https://github.com/paradedb/paradedb/releases"


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


_EXTERNAL_SCHEMA_SQL = """
SELECT nspname
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname <> 'pg_toast'
  AND nspname NOT LIKE 'pg_temp_%'
  AND nspname NOT LIKE 'pg_toast_temp_%'
"""

_EXTERNAL_CLASS_SQL = """
SELECT c.relname
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.objid = c.oid AND d.deptype = 'e'
  )
"""

_EXTERNAL_PROC_SQL = """
SELECT p.proname
FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.objid = p.oid AND d.deptype = 'e'
  )
"""

_EXTERNAL_TYPE_SQL = """
SELECT t.typname
FROM pg_type t
WHERE t.typnamespace = 'public'::regnamespace
  AND t.typtype IN ('c', 'd', 'e', 'r')
  AND t.typname NOT LIKE '\\_%'
  AND NOT EXISTS (
      SELECT 1 FROM pg_depend d
      WHERE d.objid = t.oid AND d.deptype = 'e'
  )
"""
