"""Git and checkout helpers for source-control routes."""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 # subprocess needed for git operations
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException

from gobby.servers.routes.projects import HIDDEN_PROJECT_NAMES, _checkout_http_error
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
    require_root,
)
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS, LocalProjectManager
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.utils.daemon_git import GitTimeout, daemon_git

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_GIT_TTL = 10.0
_STATUS_TTL = 30.0
_MAX_CACHE_SIZE = 256


def _get_cached(key: str, ttl: float) -> dict[str, Any] | None:
    """Get a cached value if still valid."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry[0]) < ttl:
            return cast(dict[str, Any], entry[1])
        return None


def _set_cached(key: str, value: Any) -> None:
    """Store a value in cache."""
    with _cache_lock:
        if len(_cache) >= _MAX_CACHE_SIZE:
            oldest = sorted(_cache, key=lambda k: _cache[k][0])[: _MAX_CACHE_SIZE // 4]
            for k in oldest:
                del _cache[k]
        _cache[key] = (time.time(), value)


def _delete_cached(key: str) -> None:
    """Delete cache entry if present."""
    with _cache_lock:
        _cache.pop(key, None)


def _status_cache_key(repo_path: str) -> str:
    """Key source status by canonical repository path, not project alias."""
    return f"status:{os.path.realpath(repo_path)}"


async def _run_git(
    args: list[str], cwd: str, timeout: int = 10
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return result (non-blocking)."""
    result = await daemon_git.run(args, cwd=cwd, timeout=timeout)
    if isinstance(result, GitTimeout):
        raise subprocess.TimeoutExpired(
            result.argv, result.timeout, output=result.stdout, stderr=result.stderr
        )
    if result.returncode is None:
        raise OSError(result.stderr or "Git could not be started")
    return subprocess.CompletedProcess(
        args=result.argv,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _get_project_manager(server: HTTPServer) -> LocalProjectManager:
    """Get a LocalProjectManager from the server."""
    if server.session_manager is None:
        raise HTTPException(503, "Session manager not available")
    return LocalProjectManager(server.session_manager.db)


def _resolve_project(server: HTTPServer, project_id: str | None) -> str | None:
    """Resolve project_id to this machine's checkout root.

    A checkout-free sentinel project resolves to None so callers return an empty
    payload instead of an error. When project_id is None, falls back to the first
    checkout-owning project with a local checkout. A named real project with no
    checkout is HTTP 409, not an empty diff.
    """
    try:
        pm = _get_project_manager(server)
        if project_id:
            project = pm.get(project_id)
            if not project:
                return None
            if project.id in CHECKOUT_FREE_PROJECT_IDS:
                return None
            machine_id = require_local_machine_id(
                None, resource_kind="project_checkout", resource_id=project.id
            )
            try:
                return require_root(pm.db, project.id, machine_id)
            except CheckoutNotFoundError as exc:
                raise _checkout_http_error(exc) from exc
        for project in pm.list():
            if project.name in HIDDEN_PROJECT_NAMES or project.id in CHECKOUT_FREE_PROJECT_IDS:
                continue
            try:
                machine_id = require_local_machine_id(
                    None, resource_kind="project_checkout", resource_id=project.id
                )
                return require_root(pm.db, project.id, machine_id)
            except (CheckoutNotFoundError, CheckoutSentinelRejectedError):
                continue
    except HTTPException as exc:
        if exc.status_code == 409:
            raise
        logger.debug("Failed to resolve project %s: %s", project_id, exc)
    except (
        MissingMachineContextError,
        MachineOwnershipMismatchError,
        CheckoutSentinelRejectedError,
    ) as exc:
        raise _checkout_http_error(exc) from exc
    except (ValueError, OSError) as exc:
        logger.debug("Failed to resolve project %s: %s", project_id, exc)
    return None


def parse_upstream_track(track: str) -> tuple[int, int]:
    """Parse Git's ``%(upstream:track)`` value into ahead and behind counts."""
    ahead = 0
    behind = 0
    if "[ahead " in track:
        try:
            ahead = int(track.split("[ahead ")[1].split("]")[0].split(",")[0])
        except (ValueError, IndexError):
            pass
    if "behind " in track:
        try:
            behind = int(track.split("behind ")[1].split("]")[0])
        except (ValueError, IndexError):
            pass
    return ahead, behind
