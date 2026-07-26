"""Restart-policy management for Gobby's Docker service containers."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 # fixed docker update command
from pathlib import Path
from typing import Final

from gobby.paths import get_gobby_home

DEFAULT_RESTART_POLICY: Final = "unless-stopped"
DISABLED_RESTART_POLICY: Final = "no"
POSTGRES_CONTAINER: Final = "gobby-postgres"
FALKORDB_CONTAINER: Final = "services-falkordb-1"
QDRANT_CONTAINER: Final = "services-qdrant-1"
MANAGED_SERVICE_CONTAINERS: Final = (
    POSTGRES_CONTAINER,
    FALKORDB_CONTAINER,
    QDRANT_CONTAINER,
)

_COMPOSE_TEMPLATE = Path(__file__).parent.parent.parent / "data" / "docker-compose.services.yml"
_DEFAULT_RESTART_LINE = f"restart: {DEFAULT_RESTART_POLICY}"
_MANAGED_SERVICE_COUNT = len(MANAGED_SERVICE_CONTAINERS)


def apply_managed_service_restart_policy(
    *,
    enabled: bool,
    containers: tuple[str, ...] = MANAGED_SERVICE_CONTAINERS,
    gobby_home: Path | None = None,
) -> dict[str, object]:
    """Persist and apply one restart policy to managed service containers."""
    policy = DEFAULT_RESTART_POLICY if enabled else DISABLED_RESTART_POLICY
    home = gobby_home or get_gobby_home()
    compose_file = _write_managed_compose(home / "services", policy=policy)

    docker_path = shutil.which("docker")
    if not docker_path:
        return {
            "success": False,
            "error": "Docker not found. Restart policy was saved but could not be applied.",
            "compose_file": str(compose_file),
        }

    try:
        result = subprocess.run(  # nosec B603 # fixed docker update command
            [docker_path, "update", "--restart", policy, *containers],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Docker restart-policy update timed out after 60s",
            "compose_file": str(compose_file),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "success": False,
            "error": f"Docker restart-policy update failed: {exc}",
            "compose_file": str(compose_file),
        }

    if result.returncode != 0:
        return {
            "success": False,
            "error": f"Docker restart-policy update failed: {result.stderr or result.stdout}",
            "compose_file": str(compose_file),
        }

    return {
        "success": True,
        "policy": policy,
        "containers": list(containers),
        "compose_file": str(compose_file),
    }


def _write_managed_compose(services_dir: Path, *, policy: str) -> Path:
    """Install the bundled Compose template with a concrete restart policy."""
    if policy not in {DEFAULT_RESTART_POLICY, DISABLED_RESTART_POLICY}:
        raise ValueError(f"Unsupported container restart policy: {policy}")

    content = _COMPOSE_TEMPLATE.read_text()
    if content.count(_DEFAULT_RESTART_LINE) != _MANAGED_SERVICE_COUNT:
        raise RuntimeError(
            "Managed service Compose template must define one default restart policy "
            "per managed container"
        )
    if policy == DISABLED_RESTART_POLICY:
        content = content.replace(_DEFAULT_RESTART_LINE, f'restart: "{policy}"')

    services_dir.mkdir(parents=True, exist_ok=True)
    compose_file = services_dir / "docker-compose.yml"
    compose_file.write_text(content)
    return compose_file
