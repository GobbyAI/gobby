from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from gobby.runner_pid_file import held_singleton_claim
from gobby.runtime_grants.launch import ManagedLaunch, merge_child_env
from gobby.runtime_output import (
    forward_subprocess_stderr,
    is_daemon_effective_config_transport_error,
)
from gobby.utils.native_bin import resolve_native_bin
from gobby.utils.wiki_vault import existing_vault_dir, is_vault, resolve_vault_dir

COMPILE_KINDS = frozenset({"source", "concept", "topic"})
MAX_URL_AGE_HOURS = 8760
PAGE_WRITE_MODES = frozenset({"upsert", "create"})

# Gateway command names (the ``command_name`` passed to ``_run_json``) whose
# subprocesses mutate the wiki vault. Every gateway in this process — watcher
# coordinator, cron handlers, MCP tools, HTTP routes — serializes these per
# vault so concurrent runs don't collide on gwiki's internal file locks and
# degrade with lock timeouts.
SERIALIZED_WRITE_COMMANDS = frozenset(
    {
        "index",
        "ingest_file",
        "ingest_url",
        "collect",
        "compile",
        "remove_source",
        "refresh",
        "sync_sessions",
        "upkeep",
        "librarian",
        "recap",
        "write_page",
        "delete_page",
        "export_pages",
        "graph_artifacts",
    }
)

# Locks are scoped per event loop: the daemon has one loop, while tests spin
# up a fresh loop per test and an asyncio.Lock cannot be reused across loops.
_vault_write_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


def _subprocess_env_and_pass_fds(
    child_env: Mapping[str, str] | None,
) -> tuple[dict[str, str] | None, tuple[int, ...]]:
    """Inherit the process-local singleton lock into a gwiki child."""
    claim = held_singleton_claim()
    if child_env is None and claim is None:
        return None, ()
    env = dict(child_env) if child_env is not None else dict(os.environ)
    if claim is None:
        return env, ()
    env.update(claim.inherit_environment())
    return env, (claim.fileno(),)


def _vault_write_lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _vault_write_locks.setdefault(loop, {})
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


# Subprocess kill guards for gwiki calls. Generation-backed commands
# (compile, AI-routed ask) run LLM synthesis that scales with vault size and
# cannot fit the interactive bound; they get the generation guard. The
# generation guard must stay below the MCP wrapper's 300s extended HTTP
# timeout (MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS) so gwiki's structured
# timeout envelope reaches the caller before the transport gives up.
INTERACTIVE_GWIKI_TIMEOUT_SECONDS = 30.0
INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS = 25.0
GENERATION_GWIKI_TIMEOUT_SECONDS = 270.0


def normalize_kind(value: str | None) -> str | None:
    if value is None:
        return None
    kind = value.strip().lower()
    if kind not in COMPILE_KINDS:
        allowed = ", ".join(sorted(COMPILE_KINDS))
        raise ValueError(f"kind must be one of {allowed}")
    return kind


def normalize_page_write_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in PAGE_WRITE_MODES:
        allowed = ", ".join(sorted(PAGE_WRITE_MODES))
        raise ValueError(f"mode must be one of {allowed}")
    return mode


class GwikiGatewayError(RuntimeError):
    """Base error for Gwiki gateway failures."""


class GwikiUnavailableError(GwikiGatewayError):
    """Raised when the gwiki binary cannot be resolved or executed."""


class GwikiDaemonConfigUnavailableError(GwikiUnavailableError):
    """Raised when gwiki cannot fetch daemon-served effective configuration."""

    def __init__(
        self,
        *,
        command: str,
        argv: Sequence[str],
        returncode: int,
        stderr: str,
    ) -> None:
        super().__init__(stderr or f"gwiki {command} exited {returncode}")
        self.command = command
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr


class GwikiJsonError(GwikiGatewayError):
    """Raised when gwiki returns malformed JSON on a successful command."""


class GwikiReadSelectorError(GwikiGatewayError, ValueError):
    """Raised when read receives anything other than one selector."""


@dataclass(frozen=True)
class GwikiCommandResult:
    """Captured gwiki maintenance command outcome."""

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
        timeout_seconds: float = INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
        managed_launch: ManagedLaunch | None = None,
    ) -> None:
        self._binary = binary
        self._binary_lock = asyncio.Lock()
        self._project_root = str(project_root) if project_root is not None else None
        self._topic = topic
        self._timeout_seconds = timeout_seconds
        self._child_env: Mapping[str, str] | None = (
            merge_child_env(managed_launch.env) if managed_launch is not None else None
        )

    async def status(self) -> dict[str, Any]:
        return await self._run_json("status", ["status"])

    async def index(self) -> dict[str, Any]:
        return await self._run_json("index", ["index"])

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        args = ["search", query]
        if limit is not None:
            args.extend(["--limit", str(limit)])
        if token_budget is not None:
            args.extend(["--token-budget", str(token_budget)])
        return await self._run_json("search", args)

    async def read(
        self,
        *,
        path: str | Path | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        args = ["read", *self._read_selector_args(path=path, title=title)]
        return await self._run_json("read", args)

    async def graph(self, *, include: str = "all") -> dict[str, Any]:
        return await self._run_json("graph", ["graph", "--stdout", "--include", include])

    async def export_pages(self) -> dict[str, Any]:
        return await self._run_json("export_pages", ["export", "pages"])

    async def graph_artifacts(self) -> dict[str, Any]:
        return await self._run_json("graph_artifacts", ["graph"])

    async def pages(self, *, prefix: str | None = None) -> dict[str, Any]:
        args = ["pages"]
        if prefix is not None:
            args.extend(["--prefix", prefix])
        return await self._run_json("pages", args)

    async def backlinks(self, target: str) -> dict[str, Any]:
        return await self._run_json("backlinks", ["backlinks", target])

    async def write_page(
        self,
        *,
        path: str,
        content: str,
        mode: str = "upsert",
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        mode_value = normalize_page_write_mode(mode)
        args = ["page", "write", "--path", path, "--mode", mode_value]
        if expected_hash is not None:
            # The precondition is never droppable at this boundary: whenever a
            # caller supplies expected_hash it must reach the gwiki argv.
            args.extend(["--expected-hash", expected_hash])
        return await self._run_json("write_page", args, stdin_data=content.encode())

    async def delete_page(self, *, path: str) -> dict[str, Any]:
        return await self._run_json("delete_page", ["page", "delete", "--path", path])

    async def ingest_file(self, path: str | Path) -> dict[str, Any]:
        return await self._run_json("ingest_file", ["ingest-file", str(path)])

    async def ingest_url(
        self,
        urls: Sequence[str],
        *,
        max_age_hours: int | None = None,
    ) -> dict[str, Any]:
        args = ["ingest-url", *urls]
        if max_age_hours is not None:
            if not 0 <= max_age_hours <= MAX_URL_AGE_HOURS:
                raise ValueError(f"max_age_hours must be between 0 and {MAX_URL_AGE_HOURS}")
            args.extend(["--max-age-hours", str(max_age_hours)])
        return await self._run_json("ingest_url", args)

    async def collect(self, query: str | None = None) -> dict[str, Any]:
        args = ["collect"]
        if query is not None:
            args.append(query)
        return await self._run_json("collect", args)

    async def compile(
        self,
        topic: str | None = None,
        *,
        kind: str | None = None,
        sources: Sequence[str] | None = None,
        outline: Sequence[str] | None = None,
        target: str | Path | None = None,
        write_intent: bool = False,
        ai: str | None = None,
    ) -> dict[str, Any]:
        kind_value = normalize_kind(kind)
        args = ["compile"]
        if topic is not None:
            args.append(topic)
        if kind_value is not None:
            args.extend(["--kind", kind_value])
        for source in sources or ():
            args.extend(["--source", source])
        for heading in outline or ():
            args.extend(["--outline", heading])
        if target is not None:
            args.extend(["--target", str(target)])
        if write_intent:
            args.append("--write-intent")
        if ai is not None:
            args.extend(["--ai", ai])
        return await self._run_json("compile", args)

    async def audit(self) -> dict[str, Any]:
        return await self._run_json("audit", ["audit"])

    async def trust(self) -> dict[str, Any]:
        return await self._run_json("trust", ["trust"])

    async def health(self) -> dict[str, Any]:
        result = await self._run_json("health", ["health"])
        await self._normalize_health_report_heading(result)
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

    async def sync_sessions(
        self,
        *,
        archive_dir: str | Path | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        args = ["sync-sessions"]
        if archive_dir is not None:
            args.extend(["--archive-dir", str(archive_dir)])
        if limit is not None:
            args.extend(["--limit", str(limit)])
        return await self._run_json("sync_sessions", args)

    async def upkeep(
        self,
        *,
        dry_run: bool = False,
        ai: str | None = None,
        max_pages: int | None = None,
        time_budget_seconds: int | None = None,
    ) -> dict[str, Any]:
        args = ["upkeep"]
        if dry_run:
            args.append("--dry-run")
        if ai is not None:
            args.extend(["--ai", ai])
        if max_pages is not None:
            args.extend(["--max-pages", str(max_pages)])
        if time_budget_seconds is not None:
            args.extend(["--time-budget-seconds", str(time_budget_seconds)])
        return await self._run_json("upkeep", args)

    async def librarian(self) -> dict[str, Any]:
        return await self._run_json("librarian", ["librarian"])

    async def recap(self, *, date: str | None = None) -> dict[str, Any]:
        args = ["recap"]
        if date is not None:
            args.extend(["--date", date])
        return await self._run_json("recap", args)

    async def prune_all_scopes(
        self,
        *,
        timeout: float | None = None,
    ) -> GwikiCommandResult:
        binary = await self._resolve_binary()
        return await self._run_command_result(
            [binary, "prune", "--force"],
            timeout=timeout,
        )

    async def purge_project_scope(
        self,
        project_id: str,
        *,
        timeout: float | None = None,
    ) -> GwikiCommandResult:
        binary = await self._resolve_binary()
        return await self._run_command_result(
            [binary, "purge", "--project-id", project_id, "--yes"],
            timeout=timeout,
        )

    async def _run_json(
        self,
        command_name: str,
        args: Sequence[str],
        *,
        include_scope: bool = True,
        stdin_data: bytes | None = None,
    ) -> dict[str, Any]:
        binary = await self._resolve_binary()
        scope_args = self._scope_args() if include_scope else []
        argv = [binary, *args, *scope_args, "--format", "json"]
        if command_name in SERIALIZED_WRITE_COMMANDS:
            async with _vault_write_lock(await self._vault_lock_key()):
                outcome = await self._run_command(command_name, argv, stdin_data=stdin_data)
        else:
            outcome = await self._run_command(command_name, argv, stdin_data=stdin_data)
        if isinstance(outcome, dict):
            return outcome

        stdout, stderr = outcome
        payload = self._parse_success_payload(command_name, stdout)
        return self._success_envelope(command_name, payload, stderr)

    async def _vault_lock_key(self) -> str:
        """Identity of the vault this gateway mutates, shared across callers.

        Callers name the same vault differently: cron and MCP/HTTP gateways
        pass the project repo root, the watcher passes the vault directory
        itself, and topic gateways pass only the topic name. Normalizing to
        the resolved vault directory (mirroring gwiki's own resolution) makes
        watcher- and cron-triggered runs share one lock.
        """
        if self._topic is not None:
            return f"topic:{self._topic}"
        return await asyncio.to_thread(self._vault_lock_key_sync)

    def _vault_lock_key_sync(self) -> str:
        root = Path(self._project_root).expanduser() if self._project_root else Path.cwd()
        try:
            root = root.resolve()
        except OSError:
            pass
        if is_vault(root):
            return f"vault:{root}"
        vault = existing_vault_dir(root) or resolve_vault_dir(root)
        return f"vault:{vault if vault is not None else root}"

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
        if title_value:
            return ["--title", title_value]
        raise GwikiReadSelectorError("Provide exactly one non-empty path or title")

    async def _run_command(
        self,
        command_name: str,
        argv: Sequence[str],
        *,
        stdin_data: bytes | None = None,
    ) -> tuple[bytes, str] | dict[str, Any]:
        proc: asyncio.subprocess.Process | None = None
        env, pass_fds = _subprocess_env_and_pass_fds(self._child_env)
        try:
            if pass_fds:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    pass_fds=pass_fds,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            stdout_pipe = getattr(proc, "stdout", None)
            stderr_pipe = getattr(proc, "stderr", None)
            # stdin-fed runs must use communicate() so the input is written and
            # the pipe closed without deadlocking against full output buffers;
            # they trade away partial-output collection on timeout.
            if (
                stdin_data is None
                and stdout_pipe is not None
                and stderr_pipe is not None
                and hasattr(stdout_pipe, "read")
                and hasattr(stderr_pipe, "read")
            ):
                stdout_task = asyncio.create_task(stdout_pipe.read())
                stderr_task = asyncio.create_task(stderr_pipe.read())
                process_wait = asyncio.create_task(proc.wait())
                started_at = time.monotonic()
                try:
                    done, _ = await asyncio.wait((process_wait,), timeout=self._timeout_seconds)
                    if process_wait not in done:
                        elapsed_seconds = time.monotonic() - started_at
                        await self._kill_process(proc, process_wait)
                        stdout, stderr = await self._collect_streams(stdout_task, stderr_task)
                        return self._timeout_envelope(
                            command_name,
                            stdout=stdout,
                            stderr=stderr,
                            elapsed_seconds=elapsed_seconds,
                        )
                    await process_wait
                except asyncio.CancelledError:
                    await self._kill_process(proc, process_wait)
                    await self._cancel_streams(stdout_task, stderr_task)
                    raise
                stdout, stderr = await self._collect_streams(stdout_task, stderr_task)
            else:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_data),
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
            if is_daemon_effective_config_transport_error(stderr_text):
                raise GwikiDaemonConfigUnavailableError(
                    command=command_name,
                    argv=argv,
                    returncode=proc.returncode or 1,
                    stderr=stderr_text,
                )
            payload = self._parse_error_payload(stdout, stderr)
            raise GwikiCommandError(
                command=command_name,
                argv=argv,
                returncode=proc.returncode or 1,
                stderr=stderr_text,
                payload=payload,
            )

        forward_subprocess_stderr(stderr)
        return stdout, stderr_text

    async def _run_command_result(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> GwikiCommandResult:
        proc: asyncio.subprocess.Process | None = None
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        timeout_seconds = self._timeout_seconds if timeout is None else timeout
        env, pass_fds = _subprocess_env_and_pass_fds(self._child_env)
        try:
            if pass_fds:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    pass_fds=pass_fds,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
            returncode = proc.returncode
            timed_out = False
        except FileNotFoundError as exc:
            raise GwikiUnavailableError(f"gwiki binary not found: {command[0]}") from exc
        except asyncio.CancelledError:
            if proc is not None:
                await self._kill_process(proc)
            raise
        except TimeoutError:
            if proc is not None:
                await self._kill_process(proc)
            stdout = b""
            stderr = f"gwiki timed out after {timeout_seconds}s".encode()
            returncode = None
            timed_out = True

        stderr_text = stderr.decode(errors="replace").strip()
        if (
            returncode is not None
            and returncode != 0
            and is_daemon_effective_config_transport_error(stderr_text)
        ):
            raise GwikiDaemonConfigUnavailableError(
                command=command[1] if len(command) > 1 else command[0],
                argv=command,
                returncode=returncode,
                stderr=stderr_text,
            )
        if returncode == 0:
            forward_subprocess_stderr(stderr)
        return GwikiCommandResult(
            command=tuple(command),
            returncode=returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            started_at=started_at,
            completed_at=datetime.now(UTC).isoformat(),
            duration_seconds=time.perf_counter() - started,
            timeout_seconds=timeout_seconds,
            timed_out=timed_out,
        )

    async def _collect_streams(
        self,
        stdout_task: asyncio.Task[bytes],
        stderr_task: asyncio.Task[bytes],
    ) -> tuple[bytes, bytes]:
        stdout_result, stderr_result = await asyncio.gather(
            stdout_task,
            stderr_task,
            return_exceptions=True,
        )
        stdout = stdout_result if isinstance(stdout_result, bytes) else b""
        stderr = stderr_result if isinstance(stderr_result, bytes) else b""
        return stdout, stderr

    async def _cancel_streams(
        self,
        stdout_task: asyncio.Task[bytes],
        stderr_task: asyncio.Task[bytes],
    ) -> None:
        for task in (stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    async def _kill_process(
        self,
        proc: asyncio.subprocess.Process,
        process_wait: asyncio.Task[int] | None = None,
    ) -> None:
        wait_task = process_wait or asyncio.create_task(proc.wait())
        try:
            proc.terminate()
        except ProcessLookupError:
            await asyncio.gather(wait_task, return_exceptions=True)
            return

        done, _ = await asyncio.wait((wait_task,), timeout=1.0)
        if wait_task in done:
            try:
                await wait_task
            except TimeoutError:
                wait_task = asyncio.create_task(proc.wait())
            else:
                return

        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.gather(wait_task, return_exceptions=True)

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

    def _timeout_envelope(
        self,
        command_name: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        elapsed_seconds: float | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": "timeout",
            "message": "gwiki command timed out",
            "timeout_seconds": self._timeout_seconds,
        }
        if elapsed_seconds is not None:
            error["elapsed_seconds"] = elapsed_seconds
        return {
            "ok": False,
            "command": command_name,
            "status": "degraded",
            "payload": None,
            "stdout": stdout.decode(errors="replace").strip(),
            "stderr": stderr.decode(errors="replace").strip(),
            "scope": self._scope_envelope(),
            "error": error,
        }

    def _scope_envelope(self) -> dict[str, str | None]:
        if self._project_root is None and self._topic is None:
            return {"cwd": str(Path.cwd())}
        return {
            "project_root": self._project_root,
            "topic": self._topic,
        }

    async def _normalize_health_report_heading(self, result: dict[str, Any]) -> None:
        payload = result.get("payload")
        if not isinstance(payload, dict):
            return
        root = payload.get("root")
        text_path = payload.get("text_path")
        if not isinstance(root, str) or not isinstance(text_path, str):
            return
        report_path = Path(root) / text_path
        try:
            await asyncio.to_thread(self._normalize_health_report_file, report_path)
        except (OSError, ValueError):
            return

    @staticmethod
    def _normalize_health_report_file(report_path: Path) -> None:
        text = report_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        if not lines:
            return
        first_line = lines[0].rstrip("\r\n")
        if first_line == "# Wiki health report":
            return
        if first_line == "Wiki health report":
            newline = lines[0][len(first_line) :]
            lines[0] = f"# Wiki health report{newline}"
            report_path.write_text("".join(lines), encoding="utf-8")
