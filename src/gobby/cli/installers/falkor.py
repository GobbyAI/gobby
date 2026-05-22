"""FalkorDB service installation and uninstallation."""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess  # nosec B404 - docker compose management
import time
from pathlib import Path
from typing import Any, Literal, NamedTuple

from gobby.cli.utils import get_gobby_home
from gobby.config.persistence import validate_falkordb_password
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.secrets import SECRET_REF_PATTERN, SecretStore

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_FALKORDB_HOST = "127.0.0.1"
DEFAULT_FALKORDB_PORT = 16379
DEFAULT_FALKORDB_BROWSER_URL = "http://localhost:13000"

PasswordSource = Literal["generated", "provided", "reused"]


class ResolvedFalkorPassword(NamedTuple):
    """Resolved FalkorDB password and its disclosure source."""

    value: str
    source: PasswordSource


def install_falkordb(
    *,
    password: str | None,
    gobby_home: Path | None = None,
) -> dict[str, Any]:
    """Install FalkorDB via the unified Docker Compose profile."""
    home: Path = gobby_home if gobby_home is not None else get_gobby_home()
    resolved = _resolve_falkordb_password(password, gobby_home=home)

    if not shutil.which("docker"):
        return _install_result(
            success=False,
            password_source=resolved.source,
            error="Docker not found. Install Docker to use FalkorDB.",
        )

    services_dir = home / "services"
    try:
        compose_file = _refresh_unified_compose(services_dir)
    except OSError as exc:
        return _install_result(
            success=False,
            password_source=resolved.source,
            error=f"Failed to refresh Docker Compose file: {exc}",
        )

    env = dict(os.environ)
    env["GOBBY_FALKORDB_PASSWORD"] = resolved.value

    try:
        subprocess.run(  # nosec B603 B607 - hardcoded docker command
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
            cwd=str(services_dir),
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        return _install_result(
            success=False,
            password_source=resolved.source,
            error=f"Docker compose up failed: {exc.stderr or exc.stdout}",
        )
    except subprocess.TimeoutExpired:
        return _install_result(
            success=False,
            password_source=resolved.source,
            error="Docker compose up timed out after 120s",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _install_result(
            success=False,
            password_source=resolved.source,
            error=f"Docker compose execution failed: {exc}",
        )

    if not _wait_for_health(
        services_dir=services_dir,
        compose_file=compose_file,
        password=resolved.value,
    ):
        return _install_result(
            success=False,
            password_source=resolved.source,
            error="Health check failed: FalkorDB did not return PONG in time",
        )

    try:
        _update_config(
            host=DEFAULT_FALKORDB_HOST,
            port=DEFAULT_FALKORDB_PORT,
            password=resolved.value,
            gobby_home=home,
        )
    except Exception as exc:
        return _install_result(
            success=False,
            password_source=resolved.source,
            error=(
                "Failed to persist FalkorDB credentials to config_store; run "
                "`gobby uninstall --falkordb` to clean up the running container, "
                f"then retry. Details: {exc}"
            ),
        )

    if not _write_bootstrap_password(resolved.value, home):
        # Keep this loud instead of returning a warning: config_store and bootstrap.yaml
        # must agree or gobby start can inject a stale Docker password.
        return _install_result(
            success=False,
            password_source=resolved.source,
            error=(
                "FalkorDB is running and credentials are persisted to config_store, "
                "but the bootstrap.yaml write failed. Run `gobby uninstall --falkordb` "
                "to roll back the container + config_store, then retry."
            ),
            compose_running=True,
        )

    return _install_result(
        success=True,
        password_source=resolved.source,
        password=resolved.value if resolved.source == "generated" else None,
    )


def uninstall_falkordb(
    *,
    gobby_home: Path | None = None,
    purge: bool = False,
) -> dict[str, Any]:
    """Uninstall the FalkorDB Docker profile and clear connection credentials."""
    home: Path = gobby_home if gobby_home is not None else get_gobby_home()
    services_dir = home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if compose_file.exists():
        args = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "--profile",
            "falkordb",
            "down",
        ]
        if purge:
            args.append("-v")
        try:
            subprocess.run(  # nosec B603 B607 - hardcoded docker command
                args,
                cwd=str(services_dir),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Docker compose down timed out for FalkorDB")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Docker compose down failed for FalkorDB: %s", exc)

    _clear_config(gobby_home=home)
    _clear_bootstrap_password(home)
    return {"success": True, "data_removed": purge}


def _resolve_falkordb_password(
    password: str | None,
    *,
    gobby_home: Path | None = None,
) -> ResolvedFalkorPassword:
    """Resolve password by explicit value, existing secret, then generation."""
    home: Path = gobby_home if gobby_home is not None else get_gobby_home()

    if password is not None:
        return ResolvedFalkorPassword(validate_falkordb_password(password), "provided")

    reused = _read_existing_password(home)
    if reused is not None:
        return ResolvedFalkorPassword(validate_falkordb_password(reused), "reused")

    generated = secrets.token_urlsafe(24)
    return ResolvedFalkorPassword(validate_falkordb_password(generated), "generated")


def _resolve_falkordb_db_path(home: Path) -> Path:
    """Resolve FalkorDB config DB path without falling through to production defaults."""
    bootstrap_file = home / "bootstrap.yaml"
    if bootstrap_file.exists():
        from gobby.config.bootstrap import load_bootstrap

        bootstrap = load_bootstrap(str(bootstrap_file))
        return Path(bootstrap.database_path).expanduser()
    return home / "gobby-hub.db"


def _refresh_unified_compose(services_dir: Path) -> Path:
    """Overwrite the unified compose file and stop the legacy Neo4j profile first."""
    services_dir.mkdir(parents=True, exist_ok=True)
    dest = services_dir / "docker-compose.yml"
    if dest.exists():
        try:
            subprocess.run(  # nosec B603 B607 - best-effort legacy cleanup
                ["docker", "compose", "-f", str(dest), "--profile", "neo4j", "down"],
                cwd=str(services_dir),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Failed to stop legacy Neo4j profile before compose refresh: %s", exc)
    shutil.copy2(_COMPOSE_SRC, dest)
    return dest


def _wait_for_health(
    *,
    services_dir: Path,
    compose_file: Path,
    password: str,
    retries: int = 30,
    interval: float = 2.0,
) -> bool:
    """Poll docker compose exec until FalkorDB answers PONG."""
    cmd = [
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
    ]
    for _ in range(retries):
        try:
            result = subprocess.run(  # nosec B603 B607 - hardcoded docker command
                cmd,
                cwd=str(services_dir),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            result = None
        except (OSError, subprocess.SubprocessError):
            result = None

        if result is not None:
            if result.stdout.strip() == "PONG":
                return True
            combined = f"{result.stdout}\n{result.stderr}"
            if "NOAUTH" in combined or "WRONGPASS" in combined:
                return False

        time.sleep(interval)
    return False


def _update_config(*, host: str, port: int, password: str, gobby_home: Path) -> None:
    """Persist FalkorDB host, port, and requirepass atomically."""
    db_path = _resolve_falkordb_db_path(gobby_home)
    db = _open_config_db(db_path)
    try:
        store = ConfigStore(db)
        secret_store = SecretStore(db)
        with db.transaction():
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


def is_falkordb_installed(
    *,
    db: LocalDatabase | None = None,
    gobby_home: Path | None = None,
) -> bool:
    """Return whether FalkorDB connection keys are present in config_store."""
    home: Path = gobby_home if gobby_home is not None else get_gobby_home()
    owns_db = db is None
    if db is None:
        db_path = _resolve_falkordb_db_path(home)
        if not db_path.exists():
            return False
        db = _open_config_db(db_path)

    try:
        store = ConfigStore(db)
        return (
            store.get("databases.falkordb.host") is not None
            and store.get("databases.falkordb.port") is not None
        )
    finally:
        if owns_db:
            db.close()


def _write_bootstrap_password(password: str, gobby_home: Path) -> bool:
    """Write falkordb_password after config_store persistence succeeds."""
    bootstrap_path = gobby_home / "bootstrap.yaml"
    try:
        import yaml

        bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if bootstrap_path.exists():
            with open(bootstrap_path, encoding="utf-8") as file:
                data = yaml.safe_load(file) or {}
                if not isinstance(data, dict):
                    data = {}
        data.pop("neo4j_password", None)
        data["falkordb_password"] = password
        with open(bootstrap_path, "w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, default_flow_style=False)
        bootstrap_path.chmod(0o600)
        return True
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to write falkordb_password to bootstrap.yaml: %s", exc)
        return False


def _clear_config(*, gobby_home: Path) -> None:
    db_path = _resolve_falkordb_db_path(gobby_home)
    if not db_path.exists():
        return

    db = _open_config_db(db_path)
    try:
        store = ConfigStore(db)
        secret_store = SecretStore(db)
        with db.transaction():
            store.clear_secret("databases.falkordb.requirepass", secret_store)
            db.execute(
                "DELETE FROM config_store WHERE key IN (?, ?)",
                ("databases.falkordb.host", "databases.falkordb.port"),
            )
    finally:
        db.close()


def _clear_bootstrap_password(gobby_home: Path) -> bool:
    bootstrap_path = gobby_home / "bootstrap.yaml"
    if not bootstrap_path.exists():
        return True

    try:
        import yaml

        with open(bootstrap_path, encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
            if not isinstance(data, dict):
                data = {}
        data.pop("falkordb_password", None)
        with open(bootstrap_path, "w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, default_flow_style=False)
        bootstrap_path.chmod(0o600)
        return True
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to clear falkordb_password from bootstrap.yaml: %s", exc)
        return False


def _read_existing_password(home: Path) -> str | None:
    db_path = _resolve_falkordb_db_path(home)
    if not db_path.exists():
        return None

    db = _open_config_db(db_path)
    try:
        store = ConfigStore(db)
        secret_ref = store.get("databases.falkordb.requirepass")
        if not isinstance(secret_ref, str):
            return None
        match = SECRET_REF_PATTERN.fullmatch(secret_ref)
        if match is None:
            return secret_ref
        return SecretStore(db).get(match.group(1))
    finally:
        db.close()


def _open_config_db(db_path: Path) -> LocalDatabase:
    db = LocalDatabase(db_path)
    db.apply_migrations()
    return db


def _install_result(
    *,
    success: bool,
    password_source: PasswordSource,
    password: str | None = None,
    error: str | None = None,
    compose_running: bool = False,
) -> dict[str, Any]:
    return {
        "success": success,
        "password_source": password_source,
        "password": password,
        "browser_url": DEFAULT_FALKORDB_BROWSER_URL,
        "error": error,
        "compose_running": compose_running,
    }
