"""ACP (Agent Communication Protocol) client base class.

Wraps an ACP-speaking CLI subprocess running ``--acp`` and communicates over
JSON-RPC 2.0 via stdio. Normalizes NDJSON stream events into structured
``StreamEvent`` payloads that web-chat wrappers convert to ChatEvent instances.

Protocol lifecycle:
  1. initialize  →  handshake with protocol version and client info
  2. session/new or session/load  →  obtain a sessionId
  3. session/prompt  →  send user input, receive streaming notifications

Per-CLI concretes (``GrokACPClient``, ``QwenACPClient``) override the class
attributes ``cli_name``, ``display_name``, ``prompt_timeout_env``,
``protocol_version`` and may override ``_normalize_notification`` or
``_extract_text_content`` to handle protocol-specific seams.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from gobby.adapters.acp_session_state import (
    ACPSessionState,
    copy_default_acp_client_capabilities,
)
from gobby.adapters.acp_session_state import (
    extract_session_id as _extract_session_id,
)
from gobby.adapters.acp_stream import (
    StreamEvent,
)
from gobby.adapters.acp_stream import (
    extract_text as _extract_text_content,
)
from gobby.adapters.acp_stream import (
    normalize_notification as _normalize_notification,
)
from gobby.adapters.acp_terminal import ACPTerminalManager
from gobby.adapters.subprocess_stderr import SubprocessStderrDrain

logger = logging.getLogger(__name__)

# JSON-RPC request ID counter
_next_id = itertools.count(1)

DEFAULT_ACP_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS = 120.0
ACP_PROMPT_TIMEOUT_ENV_QWEN = "GOBBY_QWEN_ACP_PROMPT_TIMEOUT_SECONDS"
ACP_PROMPT_TIMEOUT_ENV_GROK = "GOBBY_GROK_ACP_PROMPT_TIMEOUT_SECONDS"

__all__ = [
    "ACPClient",
    "ACP_PROMPT_TIMEOUT_ENV_GROK",
    "ACP_PROMPT_TIMEOUT_ENV_QWEN",
    "StreamEvent",
    "UnsupportedACPMethodError",
]


class UnsupportedACPMethodError(RuntimeError):
    """Raised when an ACP lifecycle method is invoked without the matching capability.

    The ACP agent advertises session lifecycle support through
    ``agentCapabilities.sessionCapabilities`` (presence-not-null semantics). A
    method whose capability is absent must not be sent on the wire; callers
    receive this error so the REST layer can map it to a ``409``.
    """

    def __init__(self, method: str) -> None:
        self.method = method
        super().__init__(f"ACP agent does not support {method}")


# asyncio subprocess pipes default to a 64 KiB StreamReader limit. A single
# stdout JSON-RPC line larger than that raises LimitOverrunError from
# readline(), which kills the ACP session. ACP agents routinely emit larger
# frames, so widen the process pipe readers. 16 MiB covers realistic frames.
ACP_STREAM_READER_LIMIT_BYTES = 16 * 1024 * 1024


def _make_id() -> int:
    return next(_next_id)


def _resolve_timeout(value: float | None, *, env_name: str, default: float) -> float:
    """Resolve a timeout from an explicit value or environment override."""
    if value is not None:
        return value

    raw = os.getenv(env_name)
    if not raw:
        return default

    try:
        parsed = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %.1fs", env_name, raw, default)
        return default

    if parsed <= 0:
        logger.warning("Ignoring non-positive %s=%r; using %.1fs", env_name, raw, default)
        return default

    return parsed


def _compact_stderr(data: bytes | str | None, *, limit: int = 300) -> str | None:
    """Return a compact single-line stderr snippet for startup diagnostics."""
    if data is None:
        return None
    if isinstance(data, bytes):
        text = data.decode(errors="replace")
    else:
        text = data
    compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not compact:
        return None
    if len(compact) > limit:
        return f"...{compact[-limit:]}"
    return compact


class ACPClient:
    """Base client for an ACP-speaking CLI's ``--acp`` mode.

    Concrete subclasses (``GrokACPClient``, ``QwenACPClient``) set the four
    class attributes below; everything else — protocol mechanics, lifecycle,
    notification dispatch — is inherited.

    Override seams:
        cli_name: PATH binary name used for ``shutil.which`` lookup.
        display_name: Human-readable label used in errors and logs.
        prompt_timeout_env: Environment variable name that, when set, overrides
            the default ``session/prompt`` timeout.
        protocol_version: The integer ``protocolVersion`` advertised on the
            initialize handshake.
        default_prompt_timeout_seconds: Fallback prompt timeout when the env
            variable is unset.
        _normalize_notification / _extract_text_content: Classmethods that may
            be overridden for CLI-specific notification shapes.

    Usage::

        client = QwenACPClient()
        await client.start()
        async for event in client.send("Hello"):
            print(event)
        await client.stop()
    """

    cli_name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    prompt_timeout_env: ClassVar[str] = ""
    protocol_version: ClassVar[int] = 1
    default_prompt_timeout_seconds: ClassVar[float] = DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS
    required_env: ClassVar[Mapping[str, str]] = MappingProxyType({})
    supports_cached_auth: ClassVar[bool] = False

    def __init__(
        self,
        cli_path: str | None = None,
        *,
        cwd: str | None = None,
        request_timeout: float = DEFAULT_ACP_REQUEST_TIMEOUT_SECONDS,
        prompt_timeout: float | None = None,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
        purpose: str = "runtime",
    ) -> None:
        if not self.cli_name or not self.display_name or not self.prompt_timeout_env:
            raise TypeError(
                f"{type(self).__name__} must set cli_name, display_name, and "
                "prompt_timeout_env class attributes"
            )

        self._cli_path = cli_path
        self._cwd = cwd
        self._request_timeout = request_timeout
        self._purpose = purpose
        self._extra_args = list(extra_args or [])
        self._env_overrides = dict(env_overrides or {})
        self._prompt_timeout = _resolve_timeout(
            prompt_timeout,
            env_name=self.prompt_timeout_env,
            default=self.default_prompt_timeout_seconds,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._started = False
        self._session_state = ACPSessionState()
        self._io_lock = asyncio.Lock()
        self._stdin_write_lock = asyncio.Lock()
        self._active_operations = 0
        self._active_prompt_session_id: str | None = None
        self._active_prompt_request_id: int | None = None
        self._request_ids = itertools.count(1)
        self._stderr_drain = SubprocessStderrDrain(f"{self.display_name} ACP", logger=logger)
        self._terminal_manager = ACPTerminalManager()

    @property
    def is_started(self) -> bool:
        """Whether the subprocess has been started."""
        return self._started

    @property
    def session_id(self) -> str | None:
        """The ACP session ID obtained from session/new or session/load."""
        return self._session_state.session_id

    @property
    def session_info(self) -> dict[str, Any]:
        """The full ACP session/new or session/load result."""
        return self._session_state.session_info

    @property
    def agent_capabilities(self) -> dict[str, Any]:
        """The capabilities advertised by the ACP agent during initialize."""
        return self._session_state.agent_capabilities

    @property
    def session_capabilities(self) -> dict[str, bool]:
        """Session lifecycle capabilities (list/resume/close/delete/additional_directories)."""
        return self._session_state.session_capabilities

    async def start(
        self,
        session_id: str | None = None,
        model: str | None = None,
        *,
        auto_session: bool = True,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Launch ``<cli> --acp``, perform initialize handshake, and create/resume session.

        Args:
            session_id: Optional session ID to resume a previous conversation.
            model: Optional model override to apply to the CLI subprocess.

        Raises:
            FileNotFoundError: If the CLI binary cannot be found.
            RuntimeError: If the client is already started or handshake fails.
        """
        if self._started:
            raise RuntimeError(f"{type(self).__name__} already started")

        path = self._cli_path or shutil.which(self.cli_name)
        if not path:
            raise FileNotFoundError(f"{self.display_name} CLI not found in PATH")

        cmd = self._build_launch_command(path, model=model, reasoning_effort=reasoning_effort)

        env = os.environ.copy()
        if self._env_overrides:
            env.update(self._env_overrides)
        if self.required_env:
            env.update(self.required_env)
        # Primary guard: ghook short-circuits when this is set, so the ACP
        # child's inherited SessionStart hook never reaches the daemon.
        env["GOBBY_HOOKS_DISABLED"] = "1"
        # Defense in depth: if an outdated ghook binary ignores the flag above,
        # ghook still carries this marker into the hook envelope's
        # terminal_context, and _session_start refuses to register on it.
        env["GOBBY_ACP_CHILD"] = "1"

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
            limit=ACP_STREAM_READER_LIMIT_BYTES,
        )
        self._started = True
        self._stderr_drain.start_async(self._process.stderr)
        logger.debug("%s ACP client started (pid=%s)", self.display_name, self._process.pid)

        try:
            # Perform initialize handshake
            init_result = await self._send_request(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "clientInfo": {
                        "name": "gobby",
                        "version": "1.0.0",
                    },
                    "clientCapabilities": copy_default_acp_client_capabilities(),
                },
            )
            logger.debug(
                "ACP initialize response",
                extra={
                    "provider": self.cli_name,
                    "provider_display": self.display_name,
                    "purpose": self._purpose,
                    "payload": init_result,
                },
            )
            negotiated_version = init_result.get("protocolVersion")
            if negotiated_version != self.protocol_version:
                raise RuntimeError(
                    f"ACP protocol version mismatch: requested {self.protocol_version}, "
                    f"agent selected {negotiated_version!r}"
                )
            self._session_state.update_agent_capabilities(init_result.get("agentCapabilities"))
            await self._maybe_authenticate(init_result)
            if auto_session:
                if session_id and self._session_state.supports_session_load():
                    session_result = await self.load_session(
                        session_id,
                        model=model,
                        cwd=cwd,
                        reasoning_effort=reasoning_effort,
                    )
                else:
                    if session_id:
                        logger.debug(
                            "%s ACP agent does not support session/load; creating new session",
                            self.display_name,
                        )
                    session_result = await self.create_session(
                        model=model,
                        cwd=cwd,
                        reasoning_effort=reasoning_effort,
                    )
                self._session_state.update_session_info(
                    session_result,
                    fallback_session_id=session_id,
                )
                self._track_additional_directories()
                logger.debug("ACP session ID: %s", self._session_state.session_id)
            else:
                self._session_state.clear_session()
        except BaseException:
            await self.stop()
            raise

    def _build_launch_command(
        self,
        path: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        """Build the provider subprocess command."""
        cmd = [path, "--acp"]
        if model:
            cmd.extend(["--model", model])
        if self._extra_args:
            cmd.extend(self._extra_args)
        return cmd

    async def _maybe_authenticate(self, init_result: dict[str, Any]) -> None:
        """Authenticate with cached credentials when a provider advertises them."""
        if not self.supports_cached_auth:
            return
        auth_methods = init_result.get("authMethods")
        if not isinstance(auth_methods, list):
            return
        method_id = None
        for method in auth_methods:
            if isinstance(method, dict) and method.get("id") == "cached_token":
                method_id = "cached_token"
                break
        if method_id is None:
            return
        await self._send_request("authenticate", {"methodId": method_id})

    def _include_additional_directories(
        self,
        params: dict[str, Any],
        additional_directories: list[str] | None,
    ) -> tuple[str, ...]:
        """Add ``additionalDirectories`` to session params when supported and non-empty.

        ACP spec: omitted == empty, so the camelCase key is included only when the
        agent advertises ``sessionCapabilities.additionalDirectories`` and the caller
        passes a non-empty list. Returns the cleaned directories that were included so
        the caller can track them as session roots.
        """
        if not additional_directories:
            return ()
        if not self._session_state.supports_session_additional_directories:
            return ()
        cleaned = tuple(
            directory
            for directory in additional_directories
            if isinstance(directory, str) and directory
        )
        if not cleaned:
            return ()
        params["additionalDirectories"] = list(cleaned)
        return cleaned

    def _track_additional_directories(self) -> None:
        """Merge agent-accepted additional directories into the tracked session roots."""
        session_info = self._session_state.session_info
        value = session_info.get("additionalDirectories")
        if not isinstance(value, list):
            return
        accepted = tuple(
            directory for directory in value if isinstance(directory, str) and directory
        )
        if not accepted:
            return
        merged = list(dict.fromkeys((*self._session_state.root_uris, *accepted)))
        self._session_state.set_roots(merged)

    async def create_session(
        self,
        *,
        model: str | None = None,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
        additional_directories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new ACP session on an already-started shared backend."""
        resolved_cwd = cwd or self._cwd or "."
        session_params: dict[str, Any] = {
            "cwd": resolved_cwd,
            "mcpServers": [],
        }
        self._include_additional_directories(session_params, additional_directories)
        if model:
            session_params["model"] = model
        if reasoning_effort and reasoning_effort != "auto":
            session_params["reasoningEffort"] = reasoning_effort
        result = await self._send_request("session/new", session_params)
        info = self._session_state.update_session_info(
            result,
            fallback_roots=(resolved_cwd,),
        )
        self._track_additional_directories()
        return info

    async def load_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
        additional_directories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load an existing ACP session on an already-started shared backend."""
        if not self._session_state.supports_session_load():
            return await self.create_session(
                model=model,
                cwd=cwd,
                reasoning_effort=reasoning_effort,
                additional_directories=additional_directories,
            )
        resolved_cwd = cwd or self._cwd or "."
        session_params: dict[str, Any] = {
            "cwd": resolved_cwd,
            "mcpServers": [],
            "sessionId": session_id,
        }
        self._include_additional_directories(session_params, additional_directories)
        if model:
            session_params["model"] = model
        if reasoning_effort and reasoning_effort != "auto":
            session_params["reasoningEffort"] = reasoning_effort
        result = await self._send_request("session/load", session_params)
        info = self._session_state.update_session_info(
            result,
            fallback_session_id=session_id,
            fallback_roots=(resolved_cwd,),
        )
        self._track_additional_directories()
        return info

    async def list_sessions(
        self,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Enumerate agent-side sessions via ``session/list`` (``{sessions, nextCursor}``).

        Gated by ``sessionCapabilities.list``. ``cursor`` carries an opaque
        pagination token returned as ``nextCursor`` by a prior page.
        """
        if not self._session_state.supports_session_list:
            raise UnsupportedACPMethodError("session/list")
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = cwd
        if cursor is not None:
            params["cursor"] = cursor
        return await self._send_request("session/list", params)

    async def resume_session(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        additional_directories: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Resume an existing session via ``session/resume`` (gated by ``resume``).

        Distinct from ``session/load``: ``resume`` does not replay transcript
        history, which is the exact semantic Gobby wants since it re-renders the
        transcript from its own DB.
        """
        if not self._session_state.supports_session_resume:
            raise UnsupportedACPMethodError("session/resume")
        resolved_cwd = cwd or self._cwd or "."
        session_params: dict[str, Any] = {
            "cwd": resolved_cwd,
            "mcpServers": [],
            "sessionId": session_id,
        }
        self._include_additional_directories(session_params, additional_directories)
        if model:
            session_params["model"] = model
        if reasoning_effort and reasoning_effort != "auto":
            session_params["reasoningEffort"] = reasoning_effort
        result = await self._send_request("session/resume", session_params)
        info = self._session_state.update_session_info(
            result,
            fallback_session_id=session_id,
            fallback_roots=(resolved_cwd,),
        )
        self._track_additional_directories()
        return info

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """End a session's lifecycle via ``session/close`` (gated by ``close``).

        Distinct from the ``session/cancel`` turn-interrupt notification: this is a
        request that retires the session rather than interrupting the current turn.
        """
        if not self._session_state.supports_session_close:
            raise UnsupportedACPMethodError("session/close")
        return await self._send_request("session/close", {"sessionId": session_id})

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        """Hard-delete a session via ``session/delete`` (gated by ``delete``)."""
        if not self._session_state.supports_session_delete:
            raise UnsupportedACPMethodError("session/delete")
        return await self._send_request("session/delete", {"sessionId": session_id})

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response.

        Args:
            method: The JSON-RPC method name.
            params: The request parameters.

        Returns:
            The result dict from the JSON-RPC response.

        Raises:
            RuntimeError: If the process is not running or returns an error.
        """
        async with self._io_lock:
            return await self._send_request_locked(method, params)

    async def cancel_session(self, session_id: str | None = None) -> None:
        """Send an out-of-band ACP session/cancel notification."""
        if not self._started or not self._process or not self._process.stdin:
            return
        target_session_id = session_id or self._active_prompt_session_id or self.session_id
        if not target_session_id:
            return
        await self._write_json_rpc_message(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": target_session_id},
            }
        )

    async def _write_json_rpc_message(self, message: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError(f"{type(self).__name__} process not available")
        async with self._stdin_write_lock:
            self._process.stdin.write((json.dumps(message) + "\n").encode())
            await self._process.stdin.drain()

    async def _send_request_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._active_operations += 1
        try:
            if not self._process or not self._process.stdin or not self._process.stdout:
                raise RuntimeError(f"{type(self).__name__} process not available")

            request_id = next(self._request_ids)
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": request_id,
            }

            await self._write_json_rpc_message(request)
            logger.debug("Sent ACP request: %s", method)
            pending_session_id: str | None = None

            while True:
                try:
                    line = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=self._request_timeout,
                    )
                except TimeoutError as exc:
                    raise TimeoutError(
                        f"Timed out waiting for ACP {method} response "
                        f"after {self._request_timeout:.1f}s"
                    ) from exc
                if not line:
                    message = f"EOF while waiting for {method} response"
                    stderr_text = await self._read_exit_stderr()
                    if stderr_text:
                        message = f"{message}; stderr: {stderr_text}"
                    raise RuntimeError(message)

                line_str = line.decode().strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line during %s: %s", method, line_str[:200])
                    continue

                if "id" in data and data.get("method"):
                    from gobby.adapters.acp_client_requests import handle_client_request

                    async for _ in handle_client_request(self, data):
                        pass
                    continue

                if "id" in data:
                    if data.get("id") != request_id:
                        logger.debug(
                            "Ignoring stale ACP %s response id=%r while waiting for id=%r",
                            method,
                            data.get("id"),
                            request_id,
                        )
                        continue
                    if "error" in data:
                        err = data["error"]
                        raise RuntimeError(f"ACP {method} error: {err.get('message', err)}")
                    result = data.get("result", {})
                    if not isinstance(result, dict):
                        result = {}
                    if pending_session_id and not _extract_session_id(result):
                        result = {**result, "sessionId": pending_session_id}
                    return result

                if not pending_session_id:
                    normalized = self._normalize_notification(data)
                    if normalized.event_type == "init":
                        pending_session_id = _extract_session_id(normalized.data)
                logger.debug(
                    "Skipping notification during %s: %s", method, data.get("method", "unknown")
                )
        finally:
            self._active_operations = max(0, self._active_operations - 1)

    async def _read_exit_stderr(self) -> str | None:
        """Read stderr when the subprocess has already exited."""
        if not self._process:
            return None
        if self._process.returncode is not None:
            await self._stderr_drain.wait_finished(timeout=0.1)
        return _compact_stderr(self._stderr_drain.snapshot())

    async def send(
        self,
        message: str | list[dict[str, Any]],
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        pre_tool_callback: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Send a prompt and yield normalized stream events.

        Uses the ``session/prompt`` method with the session ID obtained
        during start(). The prompt is sent as an array of content blocks.

        Args:
            message: The user message to send.

        Yields:
            StreamEvent instances for each notification in the response stream.

        Raises:
            RuntimeError: If the client is not started or the process has died.
        """
        if not self._started or not self._process:
            raise RuntimeError(f"{type(self).__name__} not started. Call start() first.")

        if self._process.returncode is not None:
            raise RuntimeError(
                f"{self.display_name} ACP process has exited (code={self._process.returncode})"
            )

        assert self._process.stdin is not None
        assert self._process.stdout is not None

        target_session_id = session_id or self.session_id
        if not target_session_id:
            raise RuntimeError(f"{type(self).__name__} missing session ID for session/prompt")

        # Acquire manually (not `async with`) because the lock must remain held
        # across the `yield` points of this async generator. Released in finally.
        self._active_operations += 1
        lock_acquired = False
        request_id: int | None = None
        try:
            await self._io_lock.acquire()
            lock_acquired = True
            request_id = next(self._request_ids)
            prompt = message if isinstance(message, list) else [{"type": "text", "text": message}]
            request: dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": "session/prompt",
                "params": {
                    "sessionId": target_session_id,
                    "prompt": prompt,
                },
                "id": request_id,
            }
            if model:
                request["params"]["model"] = model
            if reasoning_effort and reasoning_effort != "auto":
                request["params"]["reasoningEffort"] = reasoning_effort

            self._active_prompt_session_id = target_session_id
            self._active_prompt_request_id = request_id
            await self._write_json_rpc_message(request)
            logger.debug(
                "Sent prompt to %s ACP: %r",
                self.display_name,
                self._extract_text_content(prompt)[:80],
            )

            async for event in self._read_stream(
                expected_response_id=request_id,
                pre_tool_callback=pre_tool_callback,
            ):
                yield event
        except asyncio.CancelledError:
            try:
                await self.cancel_session(target_session_id)
            except Exception:
                logger.debug(
                    "%s ACP session/cancel failed during prompt cancellation",
                    self.display_name,
                    extra={
                        "provider": self.cli_name,
                        "purpose": self._purpose,
                        "session_id": target_session_id,
                    },
                    exc_info=True,
                )
            raise
        finally:
            if self._active_prompt_request_id == request_id:
                self._active_prompt_session_id = None
                self._active_prompt_request_id = None
            if lock_acquired:
                self._io_lock.release()
            self._active_operations = max(0, self._active_operations - 1)

    async def _read_stream(
        self,
        *,
        expected_response_id: int,
        pre_tool_callback: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
        | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Read and parse NDJSON lines from the subprocess stdout.

        Handles two types of messages:
        - JSON-RPC notifications (no "id"): streaming content, converted to StreamEvent
        - JSON-RPC response (has "id"): end-of-turn marker

        Yields StreamEvent instances. Stops after receiving the final response.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        while True:
            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=self._prompt_timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for ACP session/prompt response "
                    f"after {self._prompt_timeout:.1f}s"
                ) from exc

            if not line:
                # EOF -- process may have exited
                logger.debug("%s ACP stdout EOF", self.display_name)
                return

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("Non-JSON line from %s ACP: %s", self.display_name, line_str[:200])
                continue

            # JSON-RPC request from the provider to the client.
            if "id" in data and data.get("method"):
                from gobby.adapters.acp_client_requests import handle_client_request

                async for event in handle_client_request(
                    self, data, pre_tool_callback=pre_tool_callback
                ):
                    yield event
                continue

            # JSON-RPC response (has "id") = end of turn
            if "id" in data:
                if data.get("id") != expected_response_id:
                    logger.debug(
                        "Ignoring stale ACP session/prompt response id=%r while waiting for id=%r",
                        data.get("id"),
                        expected_response_id,
                    )
                    continue
                if "error" in data:
                    err = data["error"]
                    yield StreamEvent(
                        event_type="error",
                        data={
                            "message": err.get("message", str(err)),
                            "code": err.get("code"),
                        },
                    )
                else:
                    # Final response — extract stats if present
                    result = data.get("result", {})
                    if not isinstance(result, dict):
                        result = {}
                    yield StreamEvent(
                        event_type="result",
                        data={"stats": result.get("stats", result)},
                    )
                return

            # JSON-RPC notification (no "id") — normalize to StreamEvent
            event = self._normalize_notification(data)
            yield event

    @classmethod
    def _normalize_notification(cls, raw: dict[str, Any]) -> StreamEvent:
        """Normalize a JSON-RPC notification to a StreamEvent.

        ACP streams send notifications with a "method" field and "params"
        payload. Subclasses may override for CLI-specific shapes.

        Args:
            raw: Parsed JSON dict from the ACP stream.

        Returns:
            A normalized StreamEvent.
        """
        return _normalize_notification(raw, extract_text_content=cls._extract_text_content)

    @classmethod
    def _extract_text_content(cls, content: Any) -> str:
        """Extract text from ACP content payloads."""
        return _extract_text_content(content)

    async def stop(self) -> None:
        """Gracefully stop the subprocess and clean up.

        Safe to call multiple times. If the process has already exited,
        this is a no-op.
        """
        if not self._process:
            await self._stderr_drain.stop()
            await self._terminal_manager.release_all()
            self._started = False
            self._session_state.reset()
            self._active_prompt_session_id = None
            self._active_prompt_request_id = None
            return

        process = self._process
        try:
            await self._terminal_manager.release_all()
            if process.returncode is None:
                if process.stdin:
                    try:
                        process.stdin.close()
                    except Exception:
                        pass

                exited_after_eof = False
                try:
                    await asyncio.wait_for(process.wait(), timeout=15.0)
                    exited_after_eof = True
                except TimeoutError:
                    pass

                if not exited_after_eof and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=15.0)
                    except TimeoutError:
                        log_forced_cleanup = (
                            logger.warning if self._active_operations > 0 else logger.debug
                        )
                        log_forced_cleanup(
                            "%s ACP process did not exit after terminate; killing "
                            "provider=%s pid=%s purpose=%s",
                            self.display_name,
                            self.cli_name,
                            getattr(process, "pid", None),
                            self._purpose,
                        )
                        process.kill()
                        await process.wait()
        except ProcessLookupError:
            pass  # Already gone
        except Exception as e:
            logger.debug("%s stop error (expected): %s", type(self).__name__, e)
        finally:
            await self._stderr_drain.stop()
            self._process = None
            self._started = False
            self._session_state.reset()
            self._active_prompt_session_id = None
            self._active_prompt_request_id = None
            self._active_operations = 0
            logger.debug("%s ACP client stopped", self.display_name)
