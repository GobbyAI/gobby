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
        project_root: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._binary = binary
        self._binary_lock = asyncio.Lock()
        self._project_root = str(project_root) if project_root is not None else None
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

    async def ask(
        self,
        query: str,
        *,
        llm: bool = False,
        ai: str | None = None,
        require_ai: bool = False,
    ) -> dict[str, Any]:
        if not llm and (ai is not None or require_ai):
            names = []
            if ai is not None:
                names.append("ai")
            if require_ai:
                names.append("require_ai")
            raise ValueError(f"{' and '.join(names)} require llm=True")
        args = ["ask", query]
        if llm:
            args.append("--llm")
            if ai is not None:
                args.extend(["--ai", ai])
            if require_ai:
                args.append("--require-ai")
        return await self._run_json("ask", args)

    async def read(
        self,
        *,
        path: str | Path | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        args = ["read", *self._read_selector_args(path=path, title=title)]
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

    async def research(
        self,
        query: str | None = None,
        *,
        audit: bool = False,
        source_constraints: Sequence[str] | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_sources: int | None = None,
        ai: str | None = None,
        require_ai: bool = False,
    ) -> dict[str, Any]:
        """Run `gwiki research` with explicit CLI argv construction.

        `query` remains positional for compatibility and is appended last when
        provided. Optional values use daemon defaults of omission: `audit=False`,
        no `source_constraints`, no `max_steps`, no `max_tokens`, no
        `max_sources`, `ai=None`, and `require_ai=False`. Provided options map
        directly to repeated `--source-constraint`, `--max-steps`,
        `--max-tokens`, `--max-sources`, `--ai`, and `--require-ai` flags.
        """
        args = ["research"]
        if audit:
            args.append("--audit")
        for source_constraint in source_constraints or ():
            args.extend(["--source-constraint", source_constraint])
        if max_steps is not None:
            args.extend(["--max-steps", str(max_steps)])
        if max_tokens is not None:
            args.extend(["--max-tokens", str(max_tokens)])
        if max_sources is not None:
            args.extend(["--max-sources", str(max_sources)])
        if ai is not None:
            args.extend(["--ai", ai])
        if require_ai:
            args.append("--require-ai")
        if query is not None:
            args.append(query)
        return await self._run_json("research", args)

    async def compile(self, output: str | Path | None = None) -> dict[str, Any]:
        args = ["compile"]
        if output is not None:
            args.extend(["--target", str(output)])
        return await self._run_json("compile", args)

    async def audit(self) -> dict[str, Any]:
        return await self._run_json("audit", ["audit"])

    async def trust(self) -> dict[str, Any]:
        return await self._run_json("trust", ["trust"])

    async def health(self) -> dict[str, Any]:
        result = await self._run_json("health", ["health"], include_scope=False)
        self._normalize_health_report_heading(result)
        return result

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
        if not (dry_run ^ yes):
            raise ValueError("Provide exactly one of dry_run or yes")
        args = ["remove-source", "--id", source_id]
        args.append("--dry-run" if dry_run else "--yes")
        if keep_asset:
            args.append("--keep-asset")
        return await self._run_json("remove_source", args)

    async def refresh(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        args = ["refresh"]
        for source_id in source_ids or ():
            args.extend(["--id", source_id])
        if dry_run:
            args.append("--dry-run")
        return await self._run_json("refresh", args)

    async def _run_json(
        self,
        command_name: str,
        args: Sequence[str],
        *,
        include_scope: bool = True,
    ) -> dict[str, Any]:
        binary = await self._resolve_binary()
        scope_args = self._scope_args() if include_scope else []
        argv = [binary, *args, *scope_args, "--format", "json"]
        outcome = await self._run_command(command_name, argv)
        if isinstance(outcome, dict):
            return outcome

        stdout, stderr = outcome
        payload = self._parse_success_payload(command_name, stdout)
        return self._success_envelope(command_name, payload, stderr)

    async def _resolve_binary(self) -> str:
        if self._binary is not None:
            return self._binary
        async with self._binary_lock:
            if self._binary is not None:
                return self._binary
            binary = await asyncio.to_thread(resolve_native_bin, "gwiki")
            if binary is None:
                raise GwikiUnavailableError("gwiki is not installed")
            self._binary = binary
            return binary

    def _scope_args(self) -> list[str]:
        args: list[str] = []
        if self._project_root is not None:
            args.extend(["--project", self._project_root])
        if self._topic is not None:
            args.extend(["--topic", self._topic])
        return args

    def _read_selector_args(
        self,
        *,
        path: str | Path | None,
        title: str | None,
    ) -> list[str]:
        path_value = str(path).strip() if path is not None else None
        title_value = title.strip() if title is not None else None
        if bool(path_value) == bool(title_value):
            raise GwikiReadSelectorError("Provide exactly one non-empty path or title")
        if path_value:
            return ["--path", path_value]
        assert title_value is not None
        return ["--title", title_value]

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
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
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

    def _normalize_health_report_heading(self, result: dict[str, Any]) -> None:
        payload = result.get("payload")
        if not isinstance(payload, dict):
            return
        root = payload.get("root")
        text_path = payload.get("text_path")
        if not isinstance(root, str) or not isinstance(text_path, str):
            return
        report_path = Path(root) / text_path
        try:
            text = report_path.read_text()
        except OSError:
            return
        lines = text.splitlines(keepends=True)
        if not lines:
            return
        first_line = lines[0].rstrip("\r\n")
        if first_line == "# Wiki health report":
            return
        if first_line == "Wiki health report":
            newline = lines[0][len(first_line) :]
            lines[0] = f"# Wiki health report{newline}"
            report_path.write_text("".join(lines))
