from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gobby.utils.native_bin import resolve_native_bin


class GwikiGatewayError(RuntimeError):
    """Base error for Gwiki gateway failures."""


class GwikiUnavailableError(GwikiGatewayError):
    """Raised when the gwiki binary cannot be resolved or executed."""


class GwikiJsonError(GwikiGatewayError):
    """Raised when gwiki returns malformed JSON on a successful command."""


class GwikiReadSelectorError(GwikiGatewayError, ValueError):
    """Raised when read receives anything other than one selector."""


class GwikiCommandError(GwikiGatewayError):
    def __init__(
        self,
        *,
        command: str,
        argv: Sequence[str],
        returncode: int,
        stderr: str,
        payload: dict[str, Any] | None,
    ) -> None:
        super().__init__(stderr or f"gwiki {command} exited {returncode}")
        self.command = command
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        self.payload = payload

    def to_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "command": self.command,
            "status": "failed",
            "payload": self.payload,
            "stderr": self.stderr,
            "error": {
                "type": "command",
                "returncode": self.returncode,
                "message": str(self),
            },
        }


class GwikiGateway:
    """Async subprocess wrapper for `gwiki --format json` commands."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        project: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._binary = binary
        self._project = str(project) if project is not None else None
        self._topic = topic
        self._timeout_seconds = timeout_seconds

    async def status(self) -> dict[str, Any]:
        return await self._run_json("status", ["status"])

    async def index(self) -> dict[str, Any]:
        return await self._run_json("index", ["index"])

    async def search(self, query: str, *, limit: int | None = None) -> dict[str, Any]:
        args = ["search", query]
        if limit is not None:
            args.extend(["--limit", str(limit)])
        return await self._run_json("search", args)

    async def read(
        self,
        *,
        path: str | Path | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if (path is None) == (title is None):
            raise GwikiReadSelectorError("Provide exactly one of path or title")
        args = ["read"]
        if path is not None:
            args.extend(["--path", str(path)])
        else:
            assert title is not None
            args.extend(["--title", title])
        return await self._run_json("read", args)

    async def backlinks(self, target: str) -> dict[str, Any]:
        return await self._run_json("backlinks", ["backlinks", target])

    async def ingest_file(self, path: str | Path) -> dict[str, Any]:
        return await self._run_json("ingest_file", ["ingest-file", str(path)])

    async def ingest_url(self, urls: Sequence[str]) -> dict[str, Any]:
        return await self._run_json("ingest_url", ["ingest-url", *urls])

    async def collect(self, query: str | None = None) -> dict[str, Any]:
        args = ["collect"]
        if query is not None:
            args.append(query)
        return await self._run_json("collect", args)

    async def research(self, query: str | None = None) -> dict[str, Any]:
        args = ["research"]
        if query is not None:
            args.append(query)
        return await self._run_json("research", args)

    async def compile(self, output: str | Path | None = None) -> dict[str, Any]:
        args = ["compile"]
        if output is not None:
            args.extend(["--output", str(output)])
        return await self._run_json("compile", args)

    async def audit(self) -> dict[str, Any]:
        return await self._run_json("audit", ["audit"])

    async def health(self) -> dict[str, Any]:
        return await self._run_json("health", ["health"])

    async def sources(self) -> dict[str, Any]:
        return await self._run_json("sources", ["sources"])

    async def remove_source(
        self,
        source_id: str,
        *,
        dry_run: bool,
        yes: bool,
        keep_asset: bool,
    ) -> dict[str, Any]:
        if dry_run == yes:
            raise ValueError("Provide exactly one of dry_run or yes")
        args = ["remove-source", "--id", source_id]
        args.append("--dry-run" if dry_run else "--yes")
        if keep_asset:
            args.append("--keep-asset")
        return await self._run_json("remove_source", args)

    async def refresh(
        self,
        *,
        scope: str | None = None,
        source_ids: Sequence[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        args = ["refresh"]
        if scope is not None:
            args.extend(["--scope", scope])
        for source_id in source_ids or ():
            args.extend(["--id", source_id])
        if dry_run:
            args.append("--dry-run")
        return await self._run_json("refresh", args)

    async def _run_json(self, command_name: str, args: Sequence[str]) -> dict[str, Any]:
        binary = await self._resolve_binary()
        argv = [binary, *args, *self._scope_args(), "--format", "json"]
        outcome = await self._run_command(command_name, argv)
        if isinstance(outcome, dict):
            return outcome

        stdout, stderr = outcome
        payload = self._parse_success_payload(command_name, stdout)
        return self._success_envelope(command_name, payload, stderr)

    async def _resolve_binary(self) -> str:
        if self._binary is not None:
            return self._binary
        binary = await asyncio.to_thread(resolve_native_bin, "gwiki")
        if binary is None:
            raise GwikiUnavailableError("gwiki is not installed")
        self._binary = binary
        return binary

    def _scope_args(self) -> list[str]:
        args: list[str] = []
        if self._project is not None:
            args.extend(["--project", self._project])
        if self._topic is not None:
            args.extend(["--topic", self._topic])
        return args

    async def _run_command(
        self,
        command_name: str,
        argv: Sequence[str],
    ) -> tuple[bytes, str] | dict[str, Any]:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise GwikiUnavailableError(f"gwiki binary not found: {argv[0]}") from exc
        except asyncio.CancelledError:
            if proc is not None:
                await self._kill_process(proc)
            raise
        except TimeoutError:
            if proc is not None:
                await self._kill_process(proc)
            return self._timeout_envelope(command_name)

        stderr_text = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            payload = self._parse_error_payload(stdout, stderr)
            raise GwikiCommandError(
                command=command_name,
                argv=argv,
                returncode=proc.returncode or 1,
                stderr=stderr_text,
                payload=payload,
            )

        return stdout, stderr_text

    async def _kill_process(self, proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

    def _parse_success_payload(self, command_name: str, stdout: bytes) -> dict[str, Any]:
        text = stdout.decode(errors="replace").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GwikiJsonError(
                f"gwiki {command_name} returned invalid JSON: {text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise GwikiJsonError(f"gwiki {command_name} returned JSON that was not an object")
        return payload

    def _parse_error_payload(self, *streams: bytes) -> dict[str, Any] | None:
        for stream in streams:
            text = stream.decode(errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _success_envelope(
        self,
        command_name: str,
        payload: dict[str, Any],
        stderr: str,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "command": command_name,
            "payload": payload,
            "stderr": stderr,
        }

    def _timeout_envelope(self, command_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "command": command_name,
            "status": "degraded",
            "payload": None,
            "stderr": "",
            "error": {
                "type": "timeout",
                "message": "gwiki command timed out",
            },
        }
