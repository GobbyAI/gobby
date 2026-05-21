"""Best-effort Python environment seeding for isolated agent workspaces."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess  # nosec B404 # fixed local uv argv, no shell.
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

_UV_ENV_TO_CLEAR = (
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
)


@dataclass(frozen=True)
class PythonEnvSeedResult:
    """Result of a Python environment seed attempt."""

    attempted: bool
    success: bool
    skipped_reason: str | None = None
    cache_dir: str | None = None
    error: str | None = None


async def preseed_isolated_python_environment(
    isolated_path: str,
    *,
    timeout: float = 180.0,
) -> PythonEnvSeedResult:
    """Seed an isolated uv project environment from the host uv cache.

    Spawned agents intentionally use a writable per-session ``UV_CACHE_DIR``.
    This pre-spawn step uses the host's default uv cache once, offline, so
    validation can later run from the worktree-local ``.venv`` without network
    access or shared-cache writes.
    """
    workspace = Path(isolated_path)
    skipped_reason = _seed_skip_reason(workspace)
    if skipped_reason is not None:
        return PythonEnvSeedResult(attempted=False, success=False, skipped_reason=skipped_reason)

    uv_bin = resolve_native_bin("uv")
    if uv_bin is None:
        return PythonEnvSeedResult(attempted=False, success=False, skipped_reason="uv_missing")

    seed_env = _seed_process_env(os.environ)
    cache_dir = await asyncio.to_thread(_resolve_default_uv_cache_dir, uv_bin, workspace, seed_env)
    if not Path(cache_dir).is_dir():
        return PythonEnvSeedResult(
            attempted=False,
            success=False,
            skipped_reason=f"host_uv_cache_missing:{cache_dir}",
            cache_dir=cache_dir,
        )

    seed_env["UV_CACHE_DIR"] = cache_dir
    command = [
        uv_bin,
        "sync",
        "--offline",
        "--frozen",
        "--no-progress",
        "--link-mode",
        "copy",
        "--cache-dir",
        cache_dir,
    ]

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace),
            env=seed_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        if proc is not None:
            await _kill_process(proc)
        return PythonEnvSeedResult(
            attempted=True,
            success=False,
            cache_dir=cache_dir,
            error=f"uv_sync_timeout:{timeout:g}s",
        )
    except OSError as exc:
        return PythonEnvSeedResult(
            attempted=True,
            success=False,
            cache_dir=cache_dir,
            error=f"uv_sync_failed:{exc}",
        )

    if proc.returncode != 0:
        detail = _subprocess_detail(stdout, stderr)
        return PythonEnvSeedResult(
            attempted=True,
            success=False,
            cache_dir=cache_dir,
            error=f"uv_sync_failed:{proc.returncode}:{detail}",
        )

    logger.info("Pre-seeded isolated Python environment at %s", workspace)
    return PythonEnvSeedResult(attempted=True, success=True, cache_dir=cache_dir)


def _seed_skip_reason(workspace: Path) -> str | None:
    if not workspace.is_dir():
        return f"workspace_missing:{workspace}"
    if not (workspace / "pyproject.toml").is_file():
        return "pyproject_missing"
    if not (workspace / "uv.lock").is_file():
        return "uv_lock_missing"
    return None


def _seed_process_env(source: Mapping[str, str]) -> dict[str, str]:
    env = {str(key): str(value) for key, value in source.items()}
    for key in _UV_ENV_TO_CLEAR:
        env.pop(key, None)
    env["UV_PYTHON_DOWNLOADS"] = "never"
    return env


def _resolve_default_uv_cache_dir(
    uv_bin: str,
    workspace: Path,
    seed_env: Mapping[str, str],
) -> str:
    try:
        result = subprocess.run(  # nosec B603 # fixed uv argv, no shell.
            [uv_bin, "cache", "dir"],
            cwd=str(workspace),
            env=dict(seed_env),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return str(Path.home() / ".cache" / "uv")
    cache_dir = result.stdout.strip()
    if result.returncode == 0 and cache_dir:
        return cache_dir
    return str(Path.home() / ".cache" / "uv")


def _subprocess_detail(stdout: bytes, stderr: bytes) -> str:
    output = (stderr or stdout).decode(errors="replace").strip()
    if not output:
        return "<no output>"
    return output[-600:]


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
        await proc.wait()
    except ProcessLookupError:
        pass
