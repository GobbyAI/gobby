"""
Qdrant service installation and uninstallation.

Handles Docker-based Qdrant setup for vector search. Qdrant is installed
by default during `gobby install` when Docker is available.
"""

import asyncio
import logging
import shutil
import subprocess  # nosec B404 # subprocess needed for docker compose management
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Bundled unified compose template
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_QDRANT_HTTP_URL = "http://localhost:6333"
DEFAULT_QDRANT_PORT = 6333
QDRANT_VOLUME_NAME = "gobby_qdrant_data"


def _ensure_unified_compose(services_dir: Path) -> Path:
    """Ensure the unified Docker Compose file exists, copying from template if needed.

    Returns the path to the compose file.
    """
    dest = services_dir / "docker-compose.yml"
    if not dest.exists():
        services_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_COMPOSE_SRC, dest)
    return dest


def install_qdrant(
    *,
    gobby_home: Path | None = None,
    port: int = DEFAULT_QDRANT_PORT,
) -> dict[str, Any]:
    """Install Qdrant via Docker Compose.

    Uses the unified services compose file with Docker Compose profiles.

    Args:
        gobby_home: Gobby home directory (default: ~/.gobby)
        port: HTTP port for Qdrant (default: 6333)

    Returns:
        Dict with 'success' and details
    """
    home = gobby_home or Path("~/.gobby").expanduser()

    if not shutil.which("docker"):
        return {"success": False, "error": "Docker not found. Install Docker to use Qdrant."}

    services_dir = home / "services"
    compose_file = _ensure_unified_compose(services_dir)

    # Run docker compose up with qdrant profile
    try:
        result = subprocess.run(  # nosec B603 B607
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--profile",
                "qdrant",
                "up",
                "-d",
                "--remove-orphans",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(services_dir),
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": f"Docker compose up failed: {result.stderr or result.stdout}",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Docker compose up timed out after 120s"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"success": False, "error": f"Docker compose execution failed: {e}"}

    # Wait for health check
    url = f"http://localhost:{port}"
    if not _wait_for_health(url):
        return {
            "success": False,
            "error": "Health check failed: Qdrant did not become healthy in time",
        }

    # Update daemon config
    _update_config(qdrant_url=url, qdrant_port=port)

    return {
        "success": True,
        "qdrant_url": url,
        "compose_file": str(compose_file),
    }


def uninstall_qdrant(
    *,
    gobby_home: Path | None = None,
    remove_data: bool = False,
) -> dict[str, Any]:
    """Uninstall Qdrant service.

    Args:
        gobby_home: Gobby home directory (default: ~/.gobby)
        remove_data: Also remove Qdrant storage data

    Returns:
        Dict with 'success' and details
    """
    home = gobby_home or Path("~/.gobby").expanduser()
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
                    "qdrant",
                    "down",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(services_dir),
            )
            if result.returncode != 0:
                logger.warning(f"Docker compose down failed: {result.stderr or result.stdout}")
        except subprocess.TimeoutExpired:
            logger.warning("Docker compose down timed out")

    if remove_data:
        try:
            subprocess.run(  # nosec B603 B607
                ["docker", "volume", "rm", QDRANT_VOLUME_NAME],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"Failed to remove Docker volume {QDRANT_VOLUME_NAME}: {exc}")

    _update_config(qdrant_url=None)

    return {"success": True, "data_removed": remove_data}


def _wait_for_health(url: str, retries: int = 30, interval: float = 2.0) -> bool:
    """Synchronous wrapper for health check."""
    return asyncio.run(_wait_for_health_async(url, retries, interval))


async def _wait_for_health_async(url: str, retries: int = 30, interval: float = 2.0) -> bool:
    """Wait for Qdrant to become healthy via GET /healthz."""
    healthz_url = f"{url.rstrip('/')}/healthz"
    async with httpx.AsyncClient() as client:
        for _ in range(retries):
            try:
                resp = await client.get(healthz_url, timeout=5)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(interval)
    return False


def _update_config(
    qdrant_url: str | None = None,
    qdrant_port: int | None = None,
) -> None:
    """Update daemon config with Qdrant settings via ConfigStore."""
    try:
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.hub.runtime import open_runtime_hub_database

        db = open_runtime_hub_database(apply_migrations=False)
        try:
            store = ConfigStore(db)
            if qdrant_url:
                store.set("databases.qdrant.url", qdrant_url, source="install")
                if qdrant_port:
                    store.set("databases.qdrant.port", str(qdrant_port), source="install")
            else:
                store.delete("databases.qdrant.url")
                store.delete("databases.qdrant.port")
        finally:
            db.close()
    except (ImportError, OSError, RuntimeError, ValueError) as e:
        logger.warning(f"Failed to update config: {e}")
