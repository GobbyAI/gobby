"""
Qdrant service installation.

Handles Docker-based Qdrant setup for vector search. Qdrant is installed by default
during `gobby install` when Docker is available.
"""

import asyncio
import logging
import shutil
import subprocess  # nosec B404 # subprocess needed for docker compose management
from pathlib import Path
from typing import Any

import httpx

from .compose_env import ComposeEnvironmentError, resolve_compose_runtime

logger = logging.getLogger(__name__)

# Bundled unified compose template
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_COMPOSE_SRC = _DATA_DIR / "docker-compose.services.yml"

DEFAULT_QDRANT_HTTP_URL = "http://localhost:6333"
DEFAULT_QDRANT_PORT = 6333


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

    configured_url = f"http://localhost:{port}"
    try:
        _update_config(qdrant_url=configured_url, qdrant_port=port, gobby_home=home)
        runtime = resolve_compose_runtime(home, profiles=("qdrant",))
    except (ComposeEnvironmentError, ImportError, OSError, RuntimeError, ValueError) as exc:
        return {"success": False, "error": f"Failed to resolve Qdrant config: {exc}"}

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
    except (OSError, subprocess.SubprocessError) as e:
        return {"success": False, "error": f"Docker compose execution failed: {e}"}

    # Wait for health check
    effective_port = int(runtime.environment["GOBBY_QDRANT_HTTP_PORT"])
    url = f"http://localhost:{effective_port}"
    if not _wait_for_health(url):
        return {
            "success": False,
            "error": "Health check failed: Qdrant did not become healthy in time",
        }

    return {
        "success": True,
        "qdrant_url": url,
        "compose_file": str(compose_file),
    }


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
    *,
    gobby_home: Path,
) -> None:
    """Update daemon config with Qdrant settings via ConfigStore."""
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.runtime import open_runtime_hub_database

    db = open_runtime_hub_database(
        str(gobby_home / "bootstrap.yaml"),
        apply_migrations=False,
    )
    try:
        store = ConfigStore(db)
        if qdrant_url:
            store.set("databases.qdrant.url", qdrant_url, source="install")
            if qdrant_port:
                store.set("databases.qdrant.port", qdrant_port, source="install")
        else:
            store.delete("databases.qdrant.url")
            store.delete("databases.qdrant.port")
    finally:
        db.close()
