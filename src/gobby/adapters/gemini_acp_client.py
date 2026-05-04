"""ACP (Agent Communication Protocol) client for Gemini-compatible CLIs.

Wraps a Gemini-compatible CLI subprocess running ``--acp`` and communicates
over JSON-RPC 2.0 via stdio. Normalizes NDJSON stream events into structured
dicts that web-chat wrappers convert to ChatEvent instances.

Protocol lifecycle:
  1. initialize  →  handshake with protocol version and client info
  2. session/new or session/load  →  obtain a sessionId
  3. session/prompt  →  send user input, receive streaming notifications
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# JSON-RPC request ID counter
_next_id = itertools.count(1)

DEFAULT_ACP_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS = 120.0
ACP_PROMPT_TIMEOUT_ENV = "GOBBY_GEMINI_ACP_PROMPT_TIMEOUT_SECONDS"


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


def _extract_session_id(payload: Any) -> str | None:
    """Extract an ACP session ID from common response/notification shapes."""
    if not isinstance(payload, dict):
        return None

    for key in ("sessionId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    session = payload.get("session")
    if isinstance(session, dict):
        nested = _extract_session_id(session)
        if nested:
            return nested

    result = payload.get("result")
    if isinstance(result, dict):
        nested = _extract_session_id(result)
        if nested:
            return nested

    return None


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
        return f"{compact[: limit - 3]}..."
    return compact


@dataclass
class StreamEvent:
    """A normalized event from the provider ACP stream.

    Attributes:
        event_type: One of "init", "content_delta", "result", "error".
        data: Event-specific payload.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


class GeminiACPClient:
    """Client for a Gemini-compatible CLI's ACP mode.

    Launches ``gemini --acp`` as a subprocess and communicates via JSON-RPC 2.0
    over stdin/stdout. The protocol requires an initialize handshake followed
    by session creation before prompts can be sent.

    Usage::

        client = GeminiACPClient()
        await client.start()
        async for event in client.send("Hello"):
            print(event)
        await client.stop()
    """

    def __init__(
        self,
        cli_path: str | None = None,
        *,
        cwd: str | None = None,
        request_timeout: float = DEFAULT_ACP_REQUEST_TIMEOUT_SECONDS,
        prompt_timeout: float | None = None,
        cli_name: str = "gemini",
        display_name: str = "Gemini",
        prompt_timeout_env: str = ACP_PROMPT_TIMEOUT_ENV,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
        purpose: str = "runtime",
    ) -> None:
        self._cli_path = cli_path
        self._cwd = cwd
        self._request_timeout = request_timeout
        self._cli_name = cli_name
        self._display_name = display_name
        self._purpose = purpose
        self._extra_args = list(extra_args or [])
        self._env_overrides = dict(env_overrides or {})
        self._prompt_timeout = _resolve_timeout(
            prompt_timeout,
            env_name=prompt_timeout_env,
            default=DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._started = False
        self._session_id: str | None = None
        self._session_info: dict[str, Any] = {}
        self._io_lock = asyncio.Lock()

    @property
    def is_started(self) -> bool:
        """Whether the subprocess has been started."""
        return self._started

    @property
    def session_id(self) -> str | None:
        """The ACP session ID obtained from session/new or session/load."""
        return self._session_id

    @property
    def session_info(self) -> dict[str, Any]:
        """The full ACP session/new or session/load result."""
        return dict(self._session_info)

    async def start(
        self,
        session_id: str | None = None,
        model: str | None = None,
        *,
        auto_session: bool = True,
        cwd: str | None = None,
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
            raise RuntimeError(f"{self._display_name}ACPClient already started")

        path = self._cli_path or shutil.which(self._cli_name)
        if not path:
            raise FileNotFoundError(f"{self._display_name} CLI not found in PATH")

        cmd = [path, "--acp"]
        if model:
            cmd.extend(["--model", model])
        if self._extra_args:
            cmd.extend(self._extra_args)

        env = os.environ.copy()
        if self._env_overrides:
            env.update(self._env_overrides)
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
        )
        self._started = True
        logger.debug("%s ACP client started (pid=%s)", self._display_name, self._process.pid)

        # Perform initialize handshake
        init_result = await self._send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {
                    "name": "gobby",
                    "version": "1.0.0",
                },
                "capabilities": {},
            },
        )
        logger.debug(f"ACP initialize response: {init_result}")
        if auto_session:
            if session_id:
                session_result = await self.load_session(session_id, model=model, cwd=cwd)
            else:
                session_result = await self.create_session(model=model, cwd=cwd)
            self._session_info = session_result if isinstance(session_result, dict) else {}
            self._session_id = (
                session_result.get("sessionId")
                if session_result and session_result.get("sessionId")
                else session_id
            )
            logger.debug(f"ACP session ID: {self._session_id}")
        else:
            self._session_id = None
            self._session_info = {}

    async def create_session(
        self,
        *,
        model: str | None = None,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Create a new ACP session on an already-started shared backend."""
        session_params = {
            "cwd": cwd or self._cwd or ".",
            "mcpServers": [],
        }
        if model:
            session_params["model"] = model
        if reasoning_effort:
            session_params["reasoningEffort"] = reasoning_effort
        result = await self._send_request("session/new", session_params)
        self._session_info = result if isinstance(result, dict) else {}
        self._session_id = _extract_session_id(self._session_info)
        return self._session_info

    async def load_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Load an existing ACP session on an already-started shared backend."""
        session_params = {
            "cwd": cwd or self._cwd or ".",
            "mcpServers": [],
            "sessionId": session_id,
        }
        if model:
            session_params["model"] = model
        if reasoning_effort:
            session_params["reasoningEffort"] = reasoning_effort
        result = await self._send_request("session/load", session_params)
        self._session_info = result if isinstance(result, dict) else {}
        self._session_id = _extract_session_id(self._session_info) or session_id
        return self._session_info

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

    async def _send_request_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("GeminiACPClient process not available")

        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": _make_id(),
        }

        request_line = json.dumps(request) + "\n"
        self._process.stdin.write(request_line.encode())
        await self._process.stdin.drain()
        logger.debug(f"Sent ACP request: {method}")
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
                logger.warning(f"Non-JSON line during {method}: {line_str[:200]}")
                continue

            if "id" in data:
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
            logger.debug(f"Skipping notification during {method}: {data.get('method', 'unknown')}")

    async def _read_exit_stderr(self) -> str | None:
        """Read stderr when the subprocess has already exited."""
        if not self._process or not self._process.stderr or self._process.returncode is None:
            return None

        try:
            stderr_data = await asyncio.wait_for(self._process.stderr.read(), timeout=0.1)
        except TimeoutError:
            return None
        except Exception:
            logger.debug("%s ACP stderr read failed", self._display_name, exc_info=True)
            return None

        return _compact_stderr(stderr_data)

    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
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
            raise RuntimeError(f"{self._display_name}ACPClient not started. Call start() first.")

        if self._process.returncode is not None:
            raise RuntimeError(
                f"{self._display_name} ACP process has exited (code={self._process.returncode})"
            )

        assert self._process.stdin is not None
        assert self._process.stdout is not None

        target_session_id = session_id or self._session_id
        if not target_session_id:
            raise RuntimeError(
                f"{self._display_name}ACPClient missing session ID for session/prompt"
            )

        # Acquire manually (not `async with`) because the lock must remain held
        # across the `yield` points of this async generator. Released in finally.
        await self._io_lock.acquire()
        try:
            request: dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": "session/prompt",
                "params": {
                    "sessionId": target_session_id,
                    "prompt": [{"type": "text", "text": message}],
                },
                "id": _make_id(),
            }
            if model:
                request["params"]["model"] = model
            if reasoning_effort:
                request["params"]["reasoningEffort"] = reasoning_effort

            request_line = json.dumps(request) + "\n"
            self._process.stdin.write(request_line.encode())
            await self._process.stdin.drain()
            logger.debug("Sent prompt to %s ACP: %r", self._display_name, message[:80])

            async for event in self._read_stream():
                yield event
        finally:
            self._io_lock.release()

    async def _read_stream(self) -> AsyncIterator[StreamEvent]:
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
                return
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for ACP session/prompt response "
                    f"after {self._prompt_timeout:.1f}s"
                ) from exc

            if not line:
                # EOF -- process may have exited
                logger.debug("%s ACP stdout EOF", self._display_name)
                return

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("Non-JSON line from %s ACP: %s", self._display_name, line_str[:200])
                continue

            # JSON-RPC response (has "id") = end of turn
            if "id" in data:
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

    @staticmethod
    def _normalize_notification(raw: dict[str, Any]) -> StreamEvent:
        """Normalize a JSON-RPC notification to a StreamEvent.

        Gemini-compatible ACP streams send notifications with a "method" field and
        "params" payload.
        Also handles legacy raw-event format (with "type" field) for compatibility.

        Args:
            raw: Parsed JSON dict from the Gemini ACP stream.

        Returns:
            A normalized StreamEvent.
        """
        # JSON-RPC notification format: {method: "...", params: {...}}
        method = raw.get("method", "")
        params = raw.get("params", {})

        if method == "session/init" or method == "init":
            return StreamEvent(
                event_type="init",
                data=params,
            )

        if method == "session/message" or method == "message":
            role = params.get("role", "")
            is_delta = params.get("delta", False)
            content = params.get("content", "")

            if role == "assistant" and is_delta:
                return StreamEvent(
                    event_type="content_delta",
                    data={"content": content, "role": role},
                )

            return StreamEvent(
                event_type="message",
                data=params,
            )

        if method == "session/update":
            update = params.get("update", {})
            if not isinstance(update, dict):
                return StreamEvent(event_type="session/update", data=params or raw)

            update_type = update.get("sessionUpdate", "")
            content = update.get("content")
            text = GeminiACPClient._extract_text_content(content)

            if update_type == "agent_message_chunk":
                return StreamEvent(
                    event_type="content_delta",
                    data={
                        "content": text,
                        "role": "assistant",
                        "message_id": update.get("messageId"),
                    },
                )

            if update_type == "agent_thought_chunk":
                return StreamEvent(
                    event_type="thinking_delta",
                    data={
                        "content": text,
                        "message_id": update.get("messageId"),
                    },
                )

            if update_type == "user_message_chunk":
                return StreamEvent(
                    event_type="message",
                    data={
                        "role": "user",
                        "content": text,
                        "message_id": update.get("messageId"),
                    },
                )

            return StreamEvent(event_type=update_type or method, data=update)

        if method == "session/result" or method == "result":
            return StreamEvent(
                event_type="result",
                data={"stats": params.get("stats", params)},
            )

        if method == "session/error" or method == "error":
            return StreamEvent(
                event_type="error",
                data={
                    "message": params.get("message", "Unknown error"),
                    "code": params.get("code"),
                },
            )

        # Legacy raw-event format (type field instead of method)
        event_type = raw.get("type", "")
        if event_type:
            return GeminiACPClient._normalize_legacy_event(raw)

        # Unknown notification — pass through
        return StreamEvent(event_type=method or "unknown", data=params or raw)

    @staticmethod
    def _normalize_legacy_event(raw: dict[str, Any]) -> StreamEvent:
        """Normalize a legacy raw-event format (type-based) to StreamEvent.

        Kept for backward compatibility with older Gemini-compatible CLI versions.
        """
        event_type = raw.get("type", "unknown")

        if event_type == "init":
            return StreamEvent(
                event_type="init",
                data={k: v for k, v in raw.items() if k != "type"},
            )

        if event_type == "message":
            role = raw.get("role", "")
            is_delta = raw.get("delta", False)
            content = raw.get("content", "")

            if role == "assistant" and is_delta:
                return StreamEvent(
                    event_type="content_delta",
                    data={"content": content, "role": role},
                )
            return StreamEvent(
                event_type="message",
                data={k: v for k, v in raw.items() if k != "type"},
            )

        if event_type == "result":
            return StreamEvent(
                event_type="result",
                data={k: v for k, v in raw.items() if k != "type"},
            )

        if event_type == "error":
            return StreamEvent(
                event_type="error",
                data={
                    "message": raw.get("message", raw.get("error", "Unknown error")),
                    "code": raw.get("code"),
                },
            )

        return StreamEvent(event_type=event_type, data=raw)

    @staticmethod
    def _extract_text_content(content: Any) -> str:
        """Extract text from ACP content payloads."""
        if isinstance(content, str):
            return content

        if isinstance(content, dict):
            if content.get("type") == "text":
                return str(content.get("text", ""))
            if "text" in content:
                return str(content.get("text", ""))
            if "content" in content:
                return str(content.get("content", ""))
            return ""

        if isinstance(content, list):
            parts = [GeminiACPClient._extract_text_content(item) for item in content]
            return "".join(part for part in parts if part)

        return ""

    async def stop(self) -> None:
        """Gracefully stop the subprocess and clean up.

        Safe to call multiple times. If the process has already exited,
        this is a no-op.
        """
        if not self._process:
            self._started = False
            self._session_id = None
            self._session_info = {}
            return

        process = self._process
        try:
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
                        logger.warning(
                            "%s ACP process did not exit after terminate; killing "
                            "provider=%s pid=%s purpose=%s",
                            self._display_name,
                            self._cli_name,
                            getattr(process, "pid", None),
                            self._purpose,
                        )
                        process.kill()
                        await process.wait()
        except ProcessLookupError:
            pass  # Already gone
        except Exception as e:
            logger.debug("%sACPClient stop error (expected): %s", self._display_name, e)
        finally:
            self._process = None
            self._started = False
            self._session_id = None
            self._session_info = {}
            logger.debug("%s ACP client stopped", self._display_name)
