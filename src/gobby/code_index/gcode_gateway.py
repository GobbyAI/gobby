"""Async gateway for gcode-owned code-index projection operations."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PureWindowsPath
from time import perf_counter
from typing import Any

from gobby.install.bin_freshness_models import is_at_least_version
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.runtime_grants.launch import ManagedLaunch, merge_child_env
from gobby.runtime_output import (
    forward_subprocess_stderr,
    is_daemon_effective_config_transport_error,
)
from gobby.utils.native_bin import resolve_native_bin

MIN_GCODE_GRAPH_VERSION = MANAGED_BIN_VERSION_PINS["gcode"]
GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION = "0.9.5"
_VERSION_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")
_PROJECT_NOT_FOUND_PATTERN = re.compile(r"Project '([^']+)' not found")
_NO_GCODE_PROJECT_FOUND = "No gcode project found. Run `gcode init`"
# Stderr signatures for embedding-endpoint transport failures (incident #18196
# logged: "embedding response was invalid: AI transport failed: error sending
# request for url (http://localhost:1234/v1/embeddings)").
_EMBEDDING_TRANSPORT_SIGNATURES = (
    "ai transport failed",
    "embedding response was invalid",
)
_INDEXED_FILE_NOT_FOUND_PATTERN = re.compile(
    r"indexed file `([^`]+)` was not found for project (\S+)"
)


class GcodeGatewayError(RuntimeError):
    """Base error for gcode graph gateway failures."""


class GcodeUnavailableError(GcodeGatewayError):
    """Raised when the gcode binary cannot be executed."""


class GcodeVersionError(GcodeGatewayError):
    """Raised when gcode is too old for daemon graph cutover."""


class GcodeTimeoutError(GcodeGatewayError):
    """Raised when a gcode subprocess exceeds its timeout."""


class GcodeCommandError(GcodeGatewayError):
    """Raised when gcode exits non-zero."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr: str,
        *,
        stdout: str = "",
    ) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = stderr or "<no stderr>"
        super().__init__(f"gcode exited {returncode}: {detail}")


class GcodeDaemonConfigUnavailableError(GcodeCommandError):
    """Raised when gcode cannot fetch daemon-served effective configuration."""


@dataclass(frozen=True)
class GcodeCommandResult:
    """Captured gcode command outcome for maintenance logging."""

    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    started_at: str
    completed_at: str
    duration_seconds: float
    timeout_seconds: float | None
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return not self.timed_out and self.returncode == 0


class GcodeProjectNotFoundError(GcodeCommandError):
    """Raised when gcode no longer has a project for the requested root."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr: str,
        project_path: str,
        *,
        stdout: str = "",
    ) -> None:
        self.project_path = project_path
        super().__init__(command, returncode, stderr, stdout=stdout)


class GcodeIndexedFileNotFoundError(GcodeCommandError):
    """Raised when an indexed file is not found in the gcode index."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr: str,
        file_path: str,
        project_id: str,
        *,
        stdout: str = "",
    ) -> None:
        self.file_path = file_path
        self.project_id = project_id
        super().__init__(command, returncode, stderr, stdout=stdout)


class GcodeEmbeddingTransportError(GcodeCommandError):
    """Raised when gcode fails because the embedding endpoint is unreachable.

    Distinct from generic ``GcodeCommandError`` so the vector-sync circuit
    breaker trips only on embedding-transport failures, never on unrelated
    per-file command errors.
    """


class GcodeJsonError(GcodeGatewayError):
    """Raised when gcode returns invalid JSON."""


class GcodeInputValidationError(GcodeGatewayError):
    """Raised when user-controlled gcode argv values fail daemon validation."""

    def __init__(self, parameter: str, value: str, reason: str) -> None:
        self.parameter = parameter
        self.value = value
        self.reason = reason
        super().__init__(f"Invalid {parameter}: {reason}")


def _validate_user_gcode_value(parameter: str, value: str) -> str:
    if value.startswith("-"):
        raise GcodeInputValidationError(parameter, value, "value must not start with '-'")
    if PurePath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise GcodeInputValidationError(parameter, value, "value must not be an absolute path")
    if ".." in PurePath(value).parts or ".." in PureWindowsPath(value).parts:
        raise GcodeInputValidationError(parameter, value, "value must not contain '..' segments")
    return value


def _command_project_path(command: Sequence[str]) -> str | None:
    try:
        project_arg_index = command.index("--project")
    except ValueError:
        return None
    project_path_index = project_arg_index + 1
    if project_path_index >= len(command):
        return None
    return command[project_path_index]


def _classify_gcode_command_error(
    command: Sequence[str],
    returncode: int,
    stderr_text: str,
    stdout_text: str = "",
) -> GcodeCommandError:
    if is_daemon_effective_config_transport_error(stderr_text):
        return GcodeDaemonConfigUnavailableError(
            command,
            returncode,
            stderr_text,
            stdout=stdout_text,
        )
    if match := _INDEXED_FILE_NOT_FOUND_PATTERN.search(stderr_text):
        return GcodeIndexedFileNotFoundError(
            command,
            returncode,
            stderr_text,
            match.group(1),
            match.group(2),
            stdout=stdout_text,
        )
    if match := _PROJECT_NOT_FOUND_PATTERN.search(stderr_text):
        return GcodeProjectNotFoundError(
            command,
            returncode,
            stderr_text,
            match.group(1),
            stdout=stdout_text,
        )
    if _NO_GCODE_PROJECT_FOUND in stderr_text:
        project_path = _command_project_path(command)
        if project_path is not None:
            return GcodeProjectNotFoundError(
                command,
                returncode,
                stderr_text,
                project_path,
                stdout=stdout_text,
            )
    lowered = stderr_text.lower()
    if any(signature in lowered for signature in _EMBEDDING_TRANSPORT_SIGNATURES):
        return GcodeEmbeddingTransportError(command, returncode, stderr_text, stdout=stdout_text)
    return GcodeCommandError(command, returncode, stderr_text, stdout=stdout_text)


class GcodeGateway:
    """Small async subprocess wrapper for gcode projection commands."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 30.0,
        rebuild_timeout_seconds: float = 120.0,
        managed_launch: ManagedLaunch | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._rebuild_timeout_seconds = rebuild_timeout_seconds
        self._checked_version: str | None = None
        self._child_env: Mapping[str, str] | None = (
            merge_child_env(managed_launch.env) if managed_launch is not None else None
        )

    @property
    def checked_version(self) -> str | None:
        return self._checked_version

    async def graph_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        file_path = _validate_user_gcode_value("file_path", file_path)
        await self._ensure_version()
        args = [
            "graph",
            "sync-file",
            "--file",
            file_path,
            "--project",
            str(project_root),
        ]
        assert self._checked_version is not None
        if is_at_least_version(
            self._checked_version,
            GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION,
        ):
            args.append("--allow-missing-indexed-file")
        return await self._run_json(args, timeout=timeout)

    async def graph_overview(self, project_root: Path, *, limit: int = 100) -> dict[str, Any]:
        return await self._run_json(
            [
                "graph",
                "overview",
                "--project",
                str(project_root),
                "--limit",
                str(limit),
            ]
        )

    async def graph_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        file_path = _validate_user_gcode_value("file_path", file_path)
        return await self._run_json(
            [
                "graph",
                "file",
                "--file",
                file_path,
                "--project",
                str(project_root),
            ]
        )

    async def graph_neighbors(
        self,
        project_root: Path,
        symbol_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        symbol_id = _validate_user_gcode_value("symbol_id", symbol_id)
        return await self._run_json(
            [
                "graph",
                "neighbors",
                "--symbol-id",
                symbol_id,
                "--project",
                str(project_root),
                "--limit",
                str(limit),
            ]
        )

    async def graph_blast_radius(
        self,
        project_root: Path,
        *,
        symbol_id: str | None = None,
        file_path: str | None = None,
        depth: int = 3,
        limit: int = 100,
    ) -> dict[str, Any]:
        args = ["graph", "blast-radius", "--project", str(project_root)]
        if (symbol_id is None) == (file_path is None):
            raise ValueError("Provide exactly one of symbol_id or file_path")
        if symbol_id is not None:
            symbol_id = _validate_user_gcode_value("symbol_id", symbol_id)
            args.extend(["--symbol-id", symbol_id])
        else:
            if file_path is None:
                raise ValueError("file_path must be provided when symbol_id is None")
            file_path = _validate_user_gcode_value("file_path", file_path)
            args.extend(["--file", file_path])
        args.extend(["--depth", str(depth), "--limit", str(limit)])
        return await self._run_json(args)

    async def symbol_path(
        self,
        project_root: Path,
        symbol_a: str,
        symbol_b: str,
        max_depth: int,
    ) -> dict[str, Any]:
        symbol_a = _validate_user_gcode_value("symbol_a", symbol_a)
        symbol_b = _validate_user_gcode_value("symbol_b", symbol_b)
        if max_depth < 1:
            raise ValueError("max_depth must be greater than or equal to 1")
        return await self._run_json(
            [
                "path",
                symbol_a,
                symbol_b,
                "--project",
                str(project_root),
                "--max-depth",
                str(max_depth),
            ]
        )

    async def graph_clear(
        self, project_id: str, *, env: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        project_id = _validate_user_gcode_value("project_id", project_id)
        return await self._run_json(["graph", "clear", "--project-id", project_id], env=env)

    async def graph_rebuild(self, project_root: Path) -> dict[str, Any]:
        return await self._run_json(
            ["graph", "rebuild", "--project", str(project_root)],
            timeout=self._rebuild_timeout_seconds,
        )

    async def vector_sync_file(
        self,
        project_root: Path,
        file_path: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        file_path = _validate_user_gcode_value("file_path", file_path)
        await self._ensure_version()
        args = [
            "vector",
            "sync-file",
            "--file",
            file_path,
            "--project",
            str(project_root),
        ]
        assert self._checked_version is not None
        if is_at_least_version(
            self._checked_version,
            GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION,
        ):
            args.append("--allow-missing-indexed-file")
        return await self._run_json(args, timeout=timeout)

    async def vector_clear(
        self,
        project_root: Path | None = None,
        *,
        project_id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if project_id is not None:
            project_id = _validate_user_gcode_value("project_id", project_id)
            args = ["vector", "clear", "--project-id", project_id]
        elif project_root is not None:
            args = ["vector", "clear", "--project", str(project_root)]
        else:
            raise GcodeInputValidationError("project", "", "project root or project_id is required")
        return await self._run_json(
            args,
            timeout=self._rebuild_timeout_seconds,
            env=env,
        )

    async def vector_rebuild(self, project_root: Path) -> dict[str, Any]:
        return await self._run_json(
            ["vector", "rebuild", "--project", str(project_root)],
            timeout=self._rebuild_timeout_seconds,
        )

    async def prune(self, project_root: Path) -> dict[str, Any]:
        return await self._run_json_or_text(
            ["prune", "--force", "--project", str(project_root)],
            timeout=self._rebuild_timeout_seconds,
        )

    async def maintenance_index(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        binary = await self._ensure_version()
        return await self._run_command_result(
            [binary, "index", "--project", str(project_root), "--skip-if-locked"],
            timeout=timeout,
            env=env,
        )

    async def incremental_index(
        self,
        project_root: Path,
        files: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        binary = await self._ensure_version()
        return await self._run_command_result(
            [
                binary,
                "index",
                "--project",
                str(project_root),
                "--files",
                *files,
                "--quiet",
                "--skip-if-locked",
            ],
            timeout=timeout,
            env=env,
        )

    async def nightly_full_reindex(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        binary = await self._ensure_version()
        return await self._run_command_result(
            [
                binary,
                "index",
                "--full",
                "--sync-projections",
                "--project",
                str(project_root),
                "--format",
                "json",
            ],
            timeout=timeout,
            env=env,
        )

    async def prune_all_projects(
        self,
        *,
        retention_days: int,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        binary = await self._ensure_version()
        return await self._run_command_result(
            [binary, "prune", "--force", "--retention-days", str(retention_days)],
            timeout=timeout,
            env=env,
        )

    async def prune_project_for_maintenance(
        self,
        project_root: Path,
        *,
        retention_days: int,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        binary = await self._ensure_version()
        return await self._run_command_result(
            [
                binary,
                "prune",
                "--force",
                "--project",
                str(project_root),
                "--retention-days",
                str(retention_days),
            ],
            timeout=timeout,
            env=env,
        )

    async def invalidate_project_by_id(
        self,
        project_id: str,
        *,
        timeout: float | None = None,
    ) -> GcodeCommandResult:
        """Invalidate one indexed project without requiring its former repo root."""
        project_id = _validate_user_gcode_value("project_id", project_id)
        binary = await self._ensure_version()
        return await self._run_command_result(
            [binary, "invalidate", "--project-id", project_id, "--force"],
            timeout=timeout,
        )

    async def _run_json(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        binary = await self._ensure_version()
        command = [binary, *args, "--format", "json"]
        stdout, _stderr = await self._run_command(command, timeout=timeout, env=env)
        text = stdout.decode(errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GcodeJsonError(f"gcode returned invalid JSON: {text[:500]}") from exc
        if not isinstance(parsed, dict):
            raise GcodeJsonError("gcode returned JSON that was not an object")
        return parsed

    async def _run_json_or_text(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        binary = await self._ensure_version()
        command = [binary, *args, "--format", "json"]
        stdout, _stderr = await self._run_command(command, timeout=timeout)
        text = stdout.decode(errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"success": True, "output": text}
        if not isinstance(parsed, dict):
            raise GcodeJsonError("gcode returned JSON that was not an object")
        return parsed

    async def _ensure_version(self) -> str:
        if self._checked_version is not None:
            assert self._binary is not None
            return self._binary

        binary = self._binary or await asyncio.to_thread(resolve_native_bin, "gcode")
        if binary is None:
            raise GcodeUnavailableError("gcode is not installed")

        stdout, _stderr = await self._run_command(
            [binary, "--version"],
            timeout=min(self._timeout_seconds, 10.0),
            check_version=False,
        )
        version_text = stdout.decode(errors="replace").strip()
        match = _VERSION_PATTERN.search(version_text)
        if match is None:
            raise GcodeVersionError(f"unable to parse gcode version from: {version_text}")

        version = match.group(1)
        if not is_at_least_version(version, MIN_GCODE_GRAPH_VERSION):
            raise GcodeVersionError(f"gcode >= {MIN_GCODE_GRAPH_VERSION} required; found {version}")

        self._binary = binary
        self._checked_version = version
        return binary

    async def _run_command(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
        check_version: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> tuple[bytes, bytes]:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env if env is not None else self._child_env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout or self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GcodeUnavailableError(f"gcode binary not found: {command[0]}") from exc
        except asyncio.CancelledError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            raise
        except TimeoutError as exc:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            raise GcodeTimeoutError(f"gcode timed out: {' '.join(command)}") from exc

        stderr_text = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            stdout_text = stdout.decode(errors="replace").strip()
            if check_version:
                raise _classify_gcode_command_error(
                    command,
                    proc.returncode or 1,
                    stderr_text,
                    stdout_text,
                )
            raise GcodeUnavailableError(stderr_text or f"gcode exited {proc.returncode}")

        forward_subprocess_stderr(stderr)
        return stdout, stderr

    async def _run_command_result(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        proc: asyncio.subprocess.Process | None = None
        started = datetime.now(UTC)
        started_at = started.isoformat()
        start = perf_counter()
        timeout_seconds = timeout or self._timeout_seconds
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env if env is not None else self._child_env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
            returncode = proc.returncode
            timed_out = False
        except FileNotFoundError as exc:
            raise GcodeUnavailableError(f"gcode binary not found: {command[0]}") from exc
        except asyncio.CancelledError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            raise
        except TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            stdout = b""
            stderr = f"gcode timed out after {timeout_seconds}s".encode()
            returncode = None
            timed_out = True

        stderr_text = stderr.decode(errors="replace").strip()
        if (
            returncode is not None
            and returncode != 0
            and is_daemon_effective_config_transport_error(stderr_text)
        ):
            raise GcodeDaemonConfigUnavailableError(
                command,
                returncode,
                stderr_text,
                stdout=stdout.decode(errors="replace").strip(),
            )
        if returncode == 0:
            forward_subprocess_stderr(stderr)
        completed_at = datetime.now(UTC).isoformat()
        return GcodeCommandResult(
            command=tuple(command),
            returncode=returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=perf_counter() - start,
            timeout_seconds=timeout_seconds,
            timed_out=timed_out,
        )


__all__ = [
    "GcodeCommandError",
    "GcodeCommandResult",
    "GcodeEmbeddingTransportError",
    "GcodeGateway",
    "GcodeGatewayError",
    "GcodeIndexedFileNotFoundError",
    "GcodeInputValidationError",
    "GcodeJsonError",
    "GcodeProjectNotFoundError",
    "GcodeTimeoutError",
    "GcodeUnavailableError",
    "GcodeVersionError",
    "MIN_GCODE_GRAPH_VERSION",
]
