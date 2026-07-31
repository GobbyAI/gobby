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
import stat
import subprocess  # nosec B404 # fixed git argv for local exclude updates.
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gobby.config.bootstrap import (
    DEFAULT_DAEMON_BIND_HOST,
    DEFAULT_DAEMON_PORT,
)
from gobby.config.bootstrap_io import write_bootstrap_yaml
from gobby.paths import get_gobby_home
from gobby.storage.secrets import SECRET_MATERIAL_FILENAMES
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV
from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)

_CONFIG_PROBE_TIMEOUT = 5.0
_SEARCH_SMOKE_TIMEOUT = 10.0
_RUNTIME_DIR_NAME = "gcode-runtime"
_RUNTIME_HOME_ENV = "GOBBY_CODE_INDEX_RUNTIME_HOME"
_WRAPPER_RELATIVE_PATH = Path(".gobby") / "bin" / "gcode"
_WRAPPER_EXCLUDE_PATTERN = ".gobby/bin/"
_POSTGRES_URL_RE = re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+@", re.IGNORECASE)


class IndexInventoryError(RuntimeError):
    """Typed failure while producing or verifying a settled index snapshot."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "code": self.code,
            "error": str(self),
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass(frozen=True)
class RepositoryDigest:
    """Digest and exact repository-relative inputs used to produce it."""

    digest: str
    source_files: tuple[str, ...]


def repository_source_digest(
    repository_root: Path,
    *,
    source_files: Sequence[str] | None = None,
) -> RepositoryDigest:
    """Fingerprint Git-visible inputs from paths, symlink targets, and file metadata.

    Regular files use one ``lstat`` each, keeping the cost proportional to the
    inventory without reading every file body. Size, nanosecond mtime/ctime,
    inode, device, and mode identify ordinary worktree changes. This is a
    metadata fingerprint rather than a content hash.
    """
    root = repository_root.resolve(strict=True)
    inputs = (
        tuple(sorted(set(source_files)))
        if source_files is not None
        else _git_visible_source_files(root)
    )
    if not inputs:
        raise IndexInventoryError(
            "inventory_unavailable",
            "repository source inventory is empty",
        )
    digest = hashlib.sha256()
    for relative in inputs:
        path = _repository_input_path(root, relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            digest.update(b"<missing>")
        else:
            if stat.S_ISLNK(path_stat.st_mode):
                digest.update(os.readlink(path).encode("utf-8"))
            elif stat.S_ISREG(path_stat.st_mode):
                identity = (
                    path_stat.st_size,
                    path_stat.st_mtime_ns,
                    path_stat.st_ctime_ns,
                    path_stat.st_ino,
                    path_stat.st_dev,
                    path_stat.st_mode,
                )
                digest.update(":".join(str(value) for value in identity).encode("ascii"))
            else:
                digest.update(b"<missing>")
        digest.update(b"\0")
    return RepositoryDigest(digest=digest.hexdigest(), source_files=inputs)


def settle_indexed_value[T](
    repository_root: Path,
    *,
    index_operation: Callable[[], None],
    read_last_indexed_at: Callable[[], str],
    derive: Callable[[], T],
    source_files: Sequence[str] | None = None,
    max_attempts: int = 3,
    timeout_seconds: float = 120.0,
    backoff_seconds: float = 0.05,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    """Derive a value from an index run bracketed by one stable repository digest.

    The bracket only guarantees the derivation saw a coherent index. Nothing
    downstream pins this state: repository churn after preparation is expected
    during planning, and a change that actually moves the plan surface is a
    finding for the reviewer to report rather than grounds to end a round.
    """
    if max_attempts <= 0 or timeout_seconds <= 0:
        raise ValueError("index settle bounds must be positive")
    deadline = monotonic() + timeout_seconds
    attempts = 0
    while attempts < max_attempts and monotonic() <= deadline:
        attempts += 1
        before = repository_source_digest(repository_root, source_files=source_files)
        try:
            index_operation()
            last_indexed_at = read_last_indexed_at()
        except IndexInventoryError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise IndexInventoryError(
                "inventory_unavailable",
                f"code index operation failed: {exc}",
            ) from exc
        if not isinstance(last_indexed_at, str) or not last_indexed_at:
            raise IndexInventoryError(
                "inventory_unavailable",
                "code index did not report last_indexed_at",
            )
        after = repository_source_digest(
            repository_root,
            source_files=source_files,
        )
        if monotonic() > deadline:
            break
        if before.digest == after.digest and before.source_files == after.source_files:
            try:
                return derive()
            except IndexInventoryError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                raise IndexInventoryError(
                    "inventory_unavailable",
                    f"code index derivation failed: {exc}",
                ) from exc
        remaining = deadline - monotonic()
        if attempts < max_attempts and remaining > 0 and backoff_seconds > 0:
            sleeper(min(backoff_seconds, remaining))
    raise IndexInventoryError(
        "index_unstable",
        f"repository did not settle around index derivation after {attempts} attempts",
        details={"attempts": attempts},
    )


def _git_visible_source_files(repository_root: Path) -> tuple[str, ...]:
    try:
        completed = subprocess.run(  # Fixed local Git argv. # nosec B603 B607
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=repository_root,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IndexInventoryError(
            "inventory_unavailable",
            f"repository source inventory failed: {exc}",
        ) from exc
    files = tuple(sorted(os.fsdecode(raw) for raw in completed.stdout.split(b"\0") if raw))
    return files


def _repository_input_path(repository_root: Path, relative: str) -> Path:
    candidate = repository_root / relative
    try:
        candidate.relative_to(repository_root)
    except ValueError as exc:
        raise IndexInventoryError(
            "invalid_index_token",
            f"index token source path escapes repository: {relative}",
            retryable=False,
        ) from exc
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise IndexInventoryError(
            "invalid_index_token",
            f"index token source path escapes repository: {relative}",
            retryable=False,
        )
    return candidate


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
    api_token: str | None = None,
) -> CodeIndexPreflightResult:
    """Prepare and verify `gcode` access inside an isolated workspace.

    ``api_token`` authenticates only the daemon-owned preflight probes, via
    their subprocess environment. It is never written to the runtime home or
    the wrapper script and never returned in the agent env additions: the
    runtime home deliberately carries no ``local_cli_token`` (#19289), and the
    spawned agent authenticates with its own run-scoped capability instead.
    """

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
    probe_env = {GOBBY_AGENT_API_TOKEN_ENV: api_token} if api_token else None

    await _run_gcode(
        [gcode_command, "projects", "--quiet", "--format", "json"],
        cwd=workspace,
        timeout=config_probe_timeout,
        timeout_code="gcode_index_unavailable_timeout",
        failure_code="gcode_index_unavailable",
        env=probe_env,
    )
    await _run_gcode(
        [gcode_command, "index", "--quiet", "--project", str(workspace)],
        cwd=workspace,
        timeout=timeout,
        timeout_code="gcode_index_timeout",
        failure_code="gcode_index_failed",
        env=probe_env,
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
        env=probe_env,
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

    source_home = get_gobby_home()
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
    (runtime_home / "local_cli_token").unlink(missing_ok=True)
    _link_runtime_assets(source_home, runtime_home)

    wrapper_path = workspace / _WRAPPER_RELATIVE_PATH
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    _exclude_generated_wrapper_from_git(workspace)
    wrapper_path.write_text(_gcode_wrapper_script(runtime_home, gcode_bin), encoding="utf-8")
    wrapper_path.chmod(0o755)

    return CodeIndexPreflightResult(
        env={
            "PATH": _prepend_path(wrapper_path.parent),
            _RUNTIME_HOME_ENV: str(runtime_home),
        },
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
    for name in ("machine_id", *SECRET_MATERIAL_FILENAMES, "models", "services"):
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
    env: Mapping[str, str] | None = None,
) -> None:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env} if env is not None else None,
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
