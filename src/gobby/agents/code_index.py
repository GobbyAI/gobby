"""Code-index runtime preparation for isolated agent workspaces."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404 # local command execution only.
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from psycopg.conninfo import conninfo_to_dict

from gobby.agents.spawn_timing import finish_spawn_phase, start_spawn_phase
from gobby.config.bootstrap_io import read_bootstrap_yaml
from gobby.paths import get_gobby_home
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import (
    AIDaemonCapability,
    AIUnavailableCapability,
    GrantBundle,
    GrantCapabilities,
    GrantDeployment,
    GrantPrincipal,
    PostgresDirect,
    SchemaIdentity,
    UnavailableCapability,
)
from gobby.runtime_grants.service import DeploymentGrantContext
from gobby.runtime_grants.signing import sign_grant
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV
from gobby.storage.schema_contract import installed_schema_identity
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV, read_local_api_token
from gobby.utils.native_bin import resolve_native_bin

if TYPE_CHECKING:
    from gobby.storage.managed_credentials import ManagedCredential

logger = logging.getLogger(__name__)

_CONFIG_PROBE_TIMEOUT = 5.0
_SEARCH_SMOKE_TIMEOUT = 10.0
_RUNTIME_DIR_NAME = "gcode-runtime"
_RUNTIME_HOME_ENV = "GOBBY_CODE_INDEX_RUNTIME_HOME"
_WRAPPER_RELATIVE_PATH = Path(".gobby") / "bin" / "gcode"
_WRAPPER_EXCLUDE_PATTERN = ".gobby/bin/"
_POSTGRES_URL_RE = re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+@", re.IGNORECASE)
_SCOPED_ROLE_RE = re.compile(r"gobby_agent_[0-9a-f]{32}_[1-9][0-9]*")


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


async def repository_source_digest(
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
        else await _git_visible_source_files(root)
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


async def settle_indexed_value[T](
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
        before = await repository_source_digest(repository_root, source_files=source_files)
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
        after = await repository_source_digest(
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


async def _git_visible_source_files(repository_root: Path) -> tuple[str, ...]:
    result = await daemon_git.run(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repository_root,
        timeout=30,
    )
    if not isinstance(result, GitOk):
        detail = result.stderr.strip() or result.stdout.strip() or result.status
        raise IndexInventoryError(
            "inventory_unavailable",
            f"repository source inventory failed: {detail}",
        )
    files = tuple(sorted(raw for raw in result.stdout.split("\0") if raw))
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
    # Managed capability for probe and snapshot processes. It stays out of `env`,
    # which spawn hands to the agent alongside the run token it installs itself.
    api_token: str | None = field(default=None, repr=False)


async def ensure_isolation_code_index(
    isolated_path: str,
    *,
    timeout: float = 120.0,
    gcode_bin: Path | None = None,
    credential: ManagedCredential | None = None,
    principal_kind: Literal["agent_run", "tool_chat"] = "agent_run",
    runtime_root: Path | None = None,
    config_probe_timeout: float = _CONFIG_PROBE_TIMEOUT,
    search_smoke_timeout: float = _SEARCH_SMOKE_TIMEOUT,
    api_token: str | None = None,
    identity_env: Mapping[str, str] | None = None,
    phase_timings_ms: MutableMapping[str, float] | None = None,
) -> CodeIndexPreflightResult:
    """Prepare and verify `gcode` access inside an isolated workspace.

    ``api_token`` authenticates only the daemon-owned preflight probes, via
    their subprocess environment. It is never written to the runtime home or
    the wrapper script and never returned in the agent env additions: the
    runtime home deliberately carries no ``local_cli_token`` (#19289), and the
    spawned agent authenticates with its own run-scoped capability instead.

    ``identity_env`` carries the spawned run's managed-execution identity
    (GOBBY_AGENT_RUN_ID, GOBBY_PROJECT_ID, GOBBY_SESSION_ID) for the probes:
    gcore's effective-config resolution requires exactly one execution owner
    variable, which the daemon process env does not provide.
    """

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("gcode index timeout must be finite and positive")
    cutoff = time.monotonic() + timeout

    def remaining(*, cap: float | None = None) -> float:
        value = cutoff - time.monotonic()
        if value <= 0:
            raise RuntimeError("gcode_index_timeout:deadline_exceeded")
        return value if cap is None else min(value, cap)

    workspace = Path(isolated_path)
    if not workspace.is_dir():
        raise RuntimeError(f"gcode_index_workspace_missing:{isolated_path}")

    resolved_gcode = gcode_bin or resolve_native_bin("gcode")
    if resolved_gcode is None:
        raise RuntimeError("gcode_not_installed")
    resolved_gcode = Path(resolved_gcode)

    identity = dict(identity_env or {})
    remaining()
    git_exclude_path = (
        await _resolve_git_exclude_path(workspace, timeout=remaining(cap=10.0))
        if credential is not None
        else None
    )
    remaining()
    result = await asyncio.to_thread(
        _prepare_gcode_runtime,
        workspace=workspace,
        gcode_bin=resolved_gcode,
        credential=credential,
        runtime_root=runtime_root,
        machine_id=identity.get("GOBBY_MACHINE_ID"),
        project_id=identity.get("GOBBY_PROJECT_ID"),
        session_id=identity.get("GOBBY_SESSION_ID"),
        git_exclude_path=git_exclude_path,
        principal_kind=principal_kind,
    )
    remaining()
    gcode_command = str(resolved_gcode)
    merged_probe_env = {**result.env, **dict(identity_env or {})}
    probe_token = api_token or result.api_token
    if probe_token:
        merged_probe_env[GOBBY_AGENT_API_TOKEN_ENV] = probe_token
    probe_env = merged_probe_env or None

    # `status` proves the path the spawned agent will actually use: grant
    # acquisition, /api/runtime/config (an agent-capability route), and a
    # scoped-role read. `projects` lists every indexed project through an
    # operator-only route, which the run-scoped token cannot call.
    status_started = start_spawn_phase()
    try:
        await _run_gcode(
            [
                gcode_command,
                "status",
                "--quiet",
                "--format",
                "json",
                "--allow-stale",
                "--project",
                str(workspace),
            ],
            cwd=workspace,
            timeout=remaining(cap=config_probe_timeout),
            timeout_code="gcode_index_unavailable_timeout",
            failure_code="gcode_index_unavailable",
            env=probe_env,
        )
    finally:
        finish_spawn_phase(phase_timings_ms, "code_index_status", status_started)

    index_started = start_spawn_phase()
    index_command = [gcode_command, "index", "--quiet", "--project", str(workspace)]
    try:
        await _run_gcode(
            index_command,
            cwd=workspace,
            timeout=remaining(),
            timeout_code="gcode_index_timeout",
            failure_code="gcode_index_failed",
            env=probe_env,
        )
    finally:
        finish_spawn_phase(phase_timings_ms, "code_index_index", index_started)

    search_started = start_spawn_phase()
    try:
        await _run_gcode(
            [
                gcode_command,
                "search-content",
                "__gobby_code_index_smoke__",
                "--limit",
                "1",
                "--quiet",
                "--allow-stale",
                "--project",
                str(workspace),
            ],
            cwd=workspace,
            timeout=remaining(cap=search_smoke_timeout),
            timeout_code="gcode_search_content_timeout",
            failure_code="gcode_search_content_failed",
            env=probe_env,
        )
    finally:
        finish_spawn_phase(phase_timings_ms, "code_index_search_content", search_started)
    return result


def _prepare_gcode_runtime(
    *,
    workspace: Path,
    gcode_bin: Path,
    credential: ManagedCredential | None,
    runtime_root: Path | None,
    machine_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    git_exclude_path: Path | None = None,
    principal_kind: Literal["agent_run", "tool_chat"] = "agent_run",
) -> CodeIndexPreflightResult:
    if credential is None:
        return CodeIndexPreflightResult(env={})

    operator_token = read_local_api_token()
    if not operator_token:
        raise IndexInventoryError(
            "operator_token_unavailable",
            "operator token unavailable",
            retryable=False,
        )
    if not project_id:
        raise IndexInventoryError(
            "project_required",
            "isolation grant requires project_id",
            retryable=False,
        )
    context = _active_deployment_grant_context()
    source_home = get_gobby_home()
    runtime_home = _runtime_home_for_workspace(
        workspace, runtime_root or source_home / _RUNTIME_DIR_NAME
    )
    runtime_home.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(runtime_home.parent)
    runtime_home.mkdir(parents=True, exist_ok=True)
    _chmod_private(runtime_home)
    grant = _signed_grant_from_credential(
        credential,
        machine_id=machine_id,
        project_id=project_id,
        session_id=session_id,
        context=context,
        principal_kind=principal_kind,
    )
    remaining_seconds = (credential.expires_at - datetime.now(UTC)).total_seconds()
    launch = materialize_managed_launch(
        grant,
        dest_dir=runtime_home,
        operator_token=operator_token,
        deadline_seconds=max(1.0, remaining_seconds),
    )
    _link_runtime_assets(source_home, runtime_home)
    # Runs last so it is the final word on #19289: no writer above it can leave
    # a credential behind. Sweeping the whole root also reaps pre-#19289 residue.
    _reap_stale_gcode_runtime_tokens(runtime_home.parent)

    wrapper_path = workspace / _WRAPPER_RELATIVE_PATH
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    _exclude_generated_wrapper_from_git(git_exclude_path)
    wrapper_path.write_text(
        _gcode_wrapper_script(runtime_home, gcode_bin, launch.grant_path),
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)

    bootstrap_path = launch.env.get(MANAGED_EXECUTION_BOOTSTRAP_ENV)
    if not bootstrap_path:
        raise RuntimeError("gcode_managed_execution_bootstrap_missing")
    api_token = launch.env.get(GOBBY_AGENT_API_TOKEN_ENV)
    if not api_token:
        raise RuntimeError("gcode_managed_capability_missing")

    return CodeIndexPreflightResult(
        env={
            "PATH": _prepend_path(wrapper_path.parent),
            _RUNTIME_HOME_ENV: str(runtime_home),
            MANAGED_EXECUTION_BOOTSTRAP_ENV: bootstrap_path,
        },
        wrapper_path=str(wrapper_path),
        runtime_home=str(runtime_home),
        api_token=api_token,
    )


def _active_deployment_grant_context() -> DeploymentGrantContext:
    """Read token, epoch, and signing secret from the live daemon lease."""
    from gobby.daemon_lease import current_lease

    lease = current_lease()
    token = getattr(lease, "deployment_token", None)
    epoch = getattr(lease, "fencing_epoch", None)
    secret = getattr(lease, "grant_signing_secret", None)
    if lease is None or not token or epoch is None or not secret:
        raise IndexInventoryError(
            "lease_unavailable",
            "active-daemon lease has no grant signing context",
            retryable=False,
        )
    return DeploymentGrantContext(
        token=str(token),
        fencing_epoch=int(epoch),
        signing_secret=str(secret),
    )


def _signed_grant_from_credential(
    credential: ManagedCredential,
    *,
    machine_id: str | None,
    project_id: str,
    session_id: str | None,
    context: DeploymentGrantContext,
    principal_kind: Literal["agent_run", "tool_chat"] = "agent_run",
) -> GrantBundle:
    postgres = PostgresDirect(
        dsn=_scoped_database_url(credential),
        role_name=credential.role_name,
        credential_generation=credential.credential_generation,
        valid_until=int(credential.expires_at.timestamp()),
    )
    unsigned = GrantBundle(
        config_revision=0,
        deployment=GrantDeployment(token=context.token, fencing_epoch=context.fencing_epoch),
        schema_identity=SchemaIdentity.model_validate(installed_schema_identity()),
        principal=GrantPrincipal(
            kind=principal_kind,
            machine_id=machine_id or "00000000-0000-4000-8000-000000000000",
            project_id=project_id,
            execution_id=str(credential.managed_execution_id),
            session_id=session_id or str(uuid4()),
        ),
        capabilities=GrantCapabilities(
            postgres=postgres,
            falkordb=UnavailableCapability(),
            qdrant=UnavailableCapability(),
            embed=AIUnavailableCapability(),
            text_generate=AIDaemonCapability(),
            tool_chat=AIDaemonCapability(),
            vision_extract=AIUnavailableCapability(),
            audio_transcribe=AIUnavailableCapability(),
            broker_operations=(),
        ),
        issued_at=int(credential.issued_at.timestamp()),
        expires_at=int(credential.expires_at.timestamp()),
    )
    return sign_grant(unsigned, context.signing_secret)


def _scoped_database_url(credential: ManagedCredential) -> str:
    try:
        payload = json.loads(credential.bootstrap_path.read_text(encoding="utf-8"))
        execution_id = payload["managed_execution_id"]
        database_url = payload["database_url"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("managed credential bootstrap is invalid") from exc
    if execution_id != str(credential.managed_execution_id):
        raise RuntimeError("managed credential bootstrap execution mismatch")
    if not isinstance(database_url, str) or not database_url.strip():
        raise RuntimeError("managed credential bootstrap database_url is invalid")
    return database_url


def _runtime_home_for_workspace(workspace: Path, runtime_root: Path) -> Path:
    try:
        workspace_key = str(workspace.resolve(strict=False))
    except OSError:
        workspace_key = str(workspace)
    digest = hashlib.sha256(workspace_key.encode("utf-8")).hexdigest()[:16]
    return runtime_root / digest


def _reap_stale_gcode_runtime_tokens(runtime_root: Path) -> None:
    """Drop legacy shared credentials from every runtime home under `runtime_root`.

    Best effort: this sweeps homes owned by other sessions, so an unreadable or
    concurrently torn-down home must never abort the caller's preflight.
    """
    try:
        runtime_homes = list(runtime_root.iterdir())
    except OSError:
        logger.debug("Failed to list gcode runtime root %s", runtime_root, exc_info=True)
        return
    for runtime_home in runtime_homes:
        if runtime_home.is_symlink() or not runtime_home.is_dir():
            continue
        for name in ("local_cli_token", ".secret_kek"):
            try:
                (runtime_home / name).unlink(missing_ok=True)
            except OSError:
                logger.debug(
                    "Failed to reap gcode runtime credential %s in %s",
                    name,
                    runtime_home,
                    exc_info=True,
                )
        bootstrap = runtime_home / "bootstrap.yaml"
        if _has_scoped_runtime_bootstrap(bootstrap):
            continue
        try:
            bootstrap.unlink(missing_ok=True)
        except OSError:
            logger.debug(
                "Failed to reap legacy gcode runtime bootstrap in %s",
                runtime_home,
                exc_info=True,
            )


def _has_scoped_runtime_bootstrap(path: Path) -> bool:
    """Return whether a runtime bootstrap uses the reserved scoped-role shape."""
    try:
        database_url = read_bootstrap_yaml(path).get("database_url")
        if not isinstance(database_url, str):
            return False
        user = conninfo_to_dict(database_url).get("user")
    except Exception:
        logger.debug("Invalid gcode runtime bootstrap %s", path, exc_info=True)
        return False
    return isinstance(user, str) and _SCOPED_ROLE_RE.fullmatch(user) is not None


def _gcode_wrapper_script(
    runtime_home: Path,
    gcode_bin: Path,
    credential_bootstrap: Path,
) -> str:
    return (
        "#!/bin/sh\n"
        f"export GOBBY_HOME={shlex.quote(str(runtime_home))}\n"
        f"export {MANAGED_EXECUTION_BOOTSTRAP_ENV}="
        f"{shlex.quote(str(credential_bootstrap))}\n"
        f'exec {shlex.quote(str(gcode_bin))} "$@"\n'
    )


async def _resolve_git_exclude_path(workspace: Path, *, timeout: float = 10.0) -> Path | None:
    result = await daemon_git.run(
        ["rev-parse", "--git-path", "info/exclude"],
        cwd=workspace,
        timeout=timeout,
    )
    if not isinstance(result, GitOk):
        logger.debug(
            "Skipping gcode wrapper Git exclude after Git failure in %s: %s",
            workspace,
            result.stderr.strip() or result.stdout.strip() or result.status,
        )
        return None
    raw_path = result.stdout.strip()
    if not raw_path:
        return None
    exclude_path = Path(raw_path)
    return exclude_path if exclude_path.is_absolute() else workspace / exclude_path


def _exclude_generated_wrapper_from_git(exclude_path: Path | None) -> None:
    if exclude_path is None:
        return
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
            "Failed to update Git exclude for gcode wrapper at %s", exclude_path, exc_info=True
        )


def _prepend_path(path: Path) -> str:
    path_text = str(path)
    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep) if current_path else []
    if path_text in parts:
        return current_path
    return f"{path_text}{os.pathsep}{current_path}" if current_path else path_text


def _link_runtime_assets(source_home: Path, runtime_home: Path) -> None:
    for name in ("machine_id", "models", "services"):
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
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = await asyncio.to_thread(
            subprocess.Popen,  # nosec B603 # fixed gcode binary with controlled arguments.
            args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **env} if env is not None else None,
        )
        stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=timeout)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.to_thread(proc.wait)
        raise
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.to_thread(proc.wait)
        raise RuntimeError(f"{timeout_code}:{timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"{failure_code}:{exc}") from exc

    if proc.returncode != 0:
        detail = _process_detail(stdout, stderr)
        preview = _preview_process_detail(detail)
        raise GcodeCommandError(
            f"{failure_code}:{proc.returncode}:{preview}",
            output=detail,
        )


class GcodeCommandError(RuntimeError):
    """gcode subprocess failed; ``output`` is the full redacted detail."""

    def __init__(self, message: str, *, output: str) -> None:
        super().__init__(message)
        self.output = output


def _process_detail(stdout: bytes, stderr: bytes) -> str:
    raw = stderr or stdout
    if not raw:
        return "<no output>"
    detail = raw.decode(errors="replace").strip()
    detail = _POSTGRES_URL_RE.sub(r"\1<redacted>@", detail)
    detail = " ".join(detail.split())
    return detail or "<empty output>"


def _preview_process_detail(detail: str, *, max_chars: int = 500) -> str:
    if len(detail) <= max_chars:
        return detail
    return f"[truncated]\n{detail[-max_chars:]}"
