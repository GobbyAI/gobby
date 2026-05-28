"""Async gateway for gcode-owned code graph operations."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gobby.install.bin_freshness_models import is_at_least_version
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS
from gobby.utils.native_bin import resolve_native_bin

MIN_GCODE_GRAPH_VERSION = MANAGED_BIN_VERSION_PINS["gcode"]
_VERSION_PATTERN = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")


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

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr or "<no stderr>"
        super().__init__(f"gcode exited {returncode}: {detail}")


class GcodeJsonError(GcodeGatewayError):
    """Raised when gcode returns invalid JSON."""


class GcodeGateway:
    """Small async subprocess wrapper for `gcode graph` commands."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 30.0,
        rebuild_timeout_seconds: float = 120.0,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._rebuild_timeout_seconds = rebuild_timeout_seconds
        self._checked_version: str | None = None

    @property
    def checked_version(self) -> str | None:
        return self._checked_version

    async def graph_sync_file(self, project_root: Path, file_path: str) -> dict[str, Any]:
        return await self._run_json(
            [
                "graph",
                "sync-file",
                "--file",
                file_path,
                "--project",
                str(project_root),
            ]
        )

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
        if symbol_id:
            args.extend(["--symbol-id", symbol_id])
        elif file_path:
            args.extend(["--file", file_path])
        else:
            raise ValueError("Provide exactly one of symbol_id or file_path")
        args.extend(["--depth", str(depth), "--limit", str(limit)])
        return await self._run_json(args)

    async def graph_clear(self, project_id: str) -> dict[str, Any]:
        return await self._run_json(["graph", "clear", "--project-id", project_id])

    async def graph_rebuild(self, project_root: Path) -> dict[str, Any]:
        return await self._run_json(
            ["graph", "rebuild", "--project", str(project_root)],
            timeout=self._rebuild_timeout_seconds,
        )

    async def _run_json(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        binary = await self._ensure_version()
        command = [binary, *args, "--format", "json", "--quiet"]
        stdout, _stderr = await self._run_command(command, timeout=timeout)
        text = stdout.decode(errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GcodeJsonError(f"gcode returned invalid JSON: {text[:500]}") from exc
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
    ) -> tuple[bytes, bytes]:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout or self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GcodeUnavailableError(f"gcode binary not found: {command[0]}") from exc
        except TimeoutError as exc:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            raise GcodeTimeoutError(f"gcode timed out: {' '.join(command)}") from exc

        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            if check_version:
                raise GcodeCommandError(command, proc.returncode or 1, stderr_text)
            raise GcodeUnavailableError(stderr_text or f"gcode exited {proc.returncode}")

        return stdout, stderr


__all__ = [
    "GcodeCommandError",
    "GcodeGateway",
    "GcodeGatewayError",
    "GcodeJsonError",
    "GcodeTimeoutError",
    "GcodeUnavailableError",
    "GcodeVersionError",
    "MIN_GCODE_GRAPH_VERSION",
]
