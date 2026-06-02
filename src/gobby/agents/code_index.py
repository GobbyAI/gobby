"""Code-index runtime preparation for isolated agent workspaces."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 # fixed git argv for local exclude updates.
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gobby.config.bootstrap import (
    DEFAULT_DAEMON_BIND_HOST,
    DEFAULT_DAEMON_PORT,
)
from gobby.config.bootstrap_io import default_gobby_home, write_bootstrap_yaml
from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

_CONFIG_PROBE_TIMEOUT = 5.0
_SEARCH_SMOKE_TIMEOUT = 10.0
_RUNTIME_DIR_NAME = "gcode-runtime"
_WRAPPER_RELATIVE_PATH = Path(".gobby") / "bin" / "gcode"
_WRAPPER_EXCLUDE_PATTERN = ".gobby/bin/"
_POSTGRES_URL_RE = re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+@", re.IGNORECASE)


@dataclass(frozen=True)
class CodeIndexPreflightResult:
    """Environment additions that make `gcode` usable from an isolated workspace."""

    env: dict[str, str]
    wrapper_path: str | None = None
    runtime_home: str | None = None


async def ensure_isolation_code_index(
    isolated_path: str,
    *,
    timeout: float = 120.0,
    database_url: str | None = None,
    daemon_bind_host: str | None = None,
    daemon_port: int | None = None,
    runtime_root: Path | None = None,
    config_probe_timeout: float = _CONFIG_PROBE_TIMEOUT,
    search_smoke_timeout: float = _SEARCH_SMOKE_TIMEOUT,
) -> CodeIndexPreflightResult:
    """Prepare and verify `gcode` access inside an isolated workspace."""

    workspace = Path(isolated_path)
    if not workspace.is_dir():
        raise RuntimeError(f"gcode_index_workspace_missing:{isolated_path}")

    gcode_bin = resolve_native_bin("gcode")
    if gcode_bin is None:
        raise RuntimeError("gcode_not_installed")

    result = _prepare_gcode_runtime(
        workspace=workspace,
        gcode_bin=Path(gcode_bin),
        database_url=database_url,
        daemon_bind_host=daemon_bind_host,
        daemon_port=daemon_port,
        runtime_root=runtime_root,
    )
    gcode_command = result.wrapper_path or gcode_bin

    await _run_gcode(
        [gcode_command, "projects", "--quiet", "--format", "json"],
        cwd=workspace,
        timeout=config_probe_timeout,
        timeout_code="gcode_index_unavailable_timeout",
        failure_code="gcode_index_unavailable",
    )
    await _run_gcode(
        [gcode_command, "index", "--quiet", "--project", str(workspace)],
        cwd=workspace,
        timeout=timeout,
        timeout_code="gcode_index_timeout",
        failure_code="gcode_index_failed",
    )
    await _run_gcode(
        [
            gcode_command,
            "search-content",
            "__gobby_code_index_smoke__",
            "--limit",
            "1",
            "--quiet",
            "--no-freshness",
            "--project",
            str(workspace),
        ],
        cwd=workspace,
        timeout=search_smoke_timeout,
        timeout_code="gcode_search_content_timeout",
        failure_code="gcode_search_content_failed",
    )
    return result


def _prepare_gcode_runtime(
    *,
    workspace: Path,
    gcode_bin: Path,
    database_url: str | None,
    daemon_bind_host: str | None,
    daemon_port: int | None,
    runtime_root: Path | None,
) -> CodeIndexPreflightResult:
    if not database_url:
        return CodeIndexPreflightResult(env={})

    source_home = default_gobby_home()
    runtime_home = _runtime_home_for_workspace(
        workspace, runtime_root or source_home / _RUNTIME_DIR_NAME
    )
    runtime_home.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(runtime_home.parent)
    runtime_home.mkdir(parents=True, exist_ok=True)
    _chmod_private(runtime_home)
    write_bootstrap_yaml(
        runtime_home / "bootstrap.yaml",
        {
            "hub_backend": "postgres",
            "database_url": database_url,
            "daemon_port": daemon_port or DEFAULT_DAEMON_PORT,
            "bind_host": daemon_bind_host or DEFAULT_DAEMON_BIND_HOST,
        },
    )
    _link_runtime_assets(source_home, runtime_home)

    wrapper_path = workspace / _WRAPPER_RELATIVE_PATH
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    _exclude_generated_wrapper_from_git(workspace)
    wrapper_path.write_text(_gcode_wrapper_script(runtime_home, gcode_bin), encoding="utf-8")
    wrapper_path.chmod(0o755)

    return CodeIndexPreflightResult(
        env={"PATH": _prepend_path(wrapper_path.parent)},
        wrapper_path=str(wrapper_path),
        runtime_home=str(runtime_home),
    )


def _runtime_home_for_workspace(workspace: Path, runtime_root: Path) -> Path:
    try:
        workspace_key = str(workspace.resolve(strict=False))
    except OSError:
        workspace_key = str(workspace)
    digest = hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()[:16]
    return runtime_root / digest


def _gcode_wrapper_script(runtime_home: Path, gcode_bin: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"export GOBBY_HOME={shlex.quote(str(runtime_home))}\n"
        f'exec {shlex.quote(str(gcode_bin))} "$@"\n'
    )


def _exclude_generated_wrapper_from_git(workspace: Path) -> None:
    try:
        result = subprocess.run(  # nosec B603 B607 # fixed git argv on local workspace.
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Skipping gcode wrapper Git exclude after Git failure", exc_info=True)
        return
    if result.returncode != 0:
        logger.debug("Skipping gcode wrapper Git exclude outside repository: %s", workspace)
        return

    exclude_path = workspace / result.stdout.strip()
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        patterns = {line.strip() for line in existing.splitlines()}
        if _WRAPPER_EXCLUDE_PATTERN in patterns:
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(
            f"{existing}{suffix}{_WRAPPER_EXCLUDE_PATTERN}\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug(
            "Failed to update Git exclude for gcode wrapper in %s", workspace, exc_info=True
        )


def _prepend_path(path: Path) -> str:
    path_text = str(path)
    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep) if current_path else []
    if path_text in parts:
        return current_path
    return f"{path_text}{os.pathsep}{current_path}" if current_path else path_text


def _link_runtime_assets(source_home: Path, runtime_home: Path) -> None:
    for name in ("machine_id", ".secret_salt", "models", "services"):
        source = source_home / name
        target = runtime_home / name
        if target.exists() or target.is_symlink() or not source.exists():
            continue
        try:
            target.symlink_to(source, target_is_directory=source.is_dir())
        except OSError:
            if source.is_dir():
                logger.debug("Skipping gcode runtime directory link fallback for %s", source)
                continue
            shutil.copy2(source, target)


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        logger.debug("Failed to apply private permissions to %s", path, exc_info=True)


async def _run_gcode(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    timeout_code: str,
    failure_code: str,
) -> None:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise
    except TimeoutError as exc:
        if proc is not None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except ProcessLookupError:
                pass
            except TimeoutError:
                pass
        raise RuntimeError(f"{timeout_code}:{timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"{failure_code}:{exc}") from exc

    if proc.returncode != 0:
        detail = _process_detail(stdout, stderr)
        raise RuntimeError(f"{failure_code}:{proc.returncode}:{detail}")


def _process_detail(stdout: bytes, stderr: bytes) -> str:
    raw = stderr or stdout
    if not raw:
        return "<no output>"
    detail = raw.decode(errors="replace").strip()
    detail = _POSTGRES_URL_RE.sub(r"\1<redacted>@", detail)
    detail = " ".join(detail.split())
    return detail[:500] or "<empty output>"
