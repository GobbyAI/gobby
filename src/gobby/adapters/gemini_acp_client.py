"""Gemini ACP (Agent Communication Protocol) client.

Wraps `gemini --acp` subprocess, communicating over JSON-RPC 2.0 via stdio.
Normalizes Gemini's NDJSON stream events into structured dicts that
GeminiCLIChatSession converts to ChatEvent instances.

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


def _make_id() -> int:
    return next(_next_id)


@dataclass
class StreamEvent:
    """A normalized event from the Gemini ACP stream.

    Attributes:
        event_type: One of "init", "content_delta", "result", "error".
        data: Event-specific payload.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


class GeminiACPClient:
    """Client for the Gemini CLI's ACP (Agent Communication Protocol) mode.

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
        request_timeout: float = 30.0,
    ) -> None:
        self._cli_path = cli_path
        self._cwd = cwd
        self._request_timeout = request_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._started = False
        self._session_id: str | None = None

    @property
    def is_started(self) -> bool:
        """Whether the subprocess has been started."""
        return self._started

    @property
    def session_id(self) -> str | None:
        """The ACP session ID obtained from session/new or session/load."""
        return self._session_id

    async def start(
        self,
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Launch ``gemini --acp``, perform initialize handshake, and create/resume session.

        Args:
            session_id: Optional session ID to resume a previous conversation.
            model: Optional model override to apply to the Gemini CLI subprocess.

        Raises:
            FileNotFoundError: If the Gemini CLI binary cannot be found.
            RuntimeError: If the client is already started or handshake fails.
        """
        if self._started:
            raise RuntimeError("GeminiACPClient already started")

        path = self._cli_path or shutil.which("gemini")
        if not path:
            raise FileNotFoundError("Gemini CLI not found in PATH")

        cmd = [path, "--acp"]
        if model:
            cmd.extend(["--model", model])

        env = os.environ.copy()
        # Prevent inherited Gemini hooks from registering nested daemon sessions.
        env["GOBBY_HOOKS_DISABLED"] = "1"

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
        )
        self._started = True
        logger.debug(f"GeminiACPClient started (pid={self._process.pid})")

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

        session_params = {
            "cwd": self._cwd or ".",
            "mcpServers": [],
        }

        # Create or resume session
        if session_id:
            session_result = await self._send_request(
                "session/load",
                {
                    **session_params,
                    "sessionId": session_id,
                },
            )
        else:
            session_result = await self._send_request("session/new", session_params)

        self._session_id = session_result.get("sessionId") if session_result else None
        logger.debug(f"ACP session ID: {self._session_id}")

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

        # Read lines until we get a JSON-RPC response (has "id" field)
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
                raise RuntimeError(f"EOF while waiting for {method} response")

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning(f"Non-JSON line during {method}: {line_str[:200]}")
                continue

            # JSON-RPC response has "id" field
            if "id" in data:
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"ACP {method} error: {err.get('message', err)}")
                result: dict[str, Any] = data.get("result", {})
                return result

            # Skip notifications during handshake
            logger.debug(f"Skipping notification during {method}: {data.get('method', 'unknown')}")

    async def send(self, message: str) -> AsyncIterator[StreamEvent]:
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
            raise RuntimeError("GeminiACPClient not started. Call start() first.")

        if self._process.returncode is not None:
            raise RuntimeError(f"Gemini ACP process has exited (code={self._process.returncode})")

        assert self._process.stdin is not None
        assert self._process.stdout is not None

        # Build JSON-RPC request using session/prompt method
        request = {
            "jsonrpc": "2.0",
            "method": "session/prompt",
            "params": {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": message}],
            },
            "id": _make_id(),
        }

        # Write request as a single line
        request_line = json.dumps(request) + "\n"
        self._process.stdin.write(request_line.encode())
        await self._process.stdin.drain()
        logger.debug(f"Sent prompt to Gemini ACP: {message[:80]!r}")

        # Read NDJSON response lines until we get the final JSON-RPC response
        async for event in self._read_stream():
            yield event

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
                    timeout=self._request_timeout,
                )
            except asyncio.CancelledError:
                return
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for ACP session/prompt response "
                    f"after {self._request_timeout:.1f}s"
                ) from exc

            if not line:
                # EOF -- process may have exited
                logger.debug("Gemini ACP stdout EOF")
                return

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning(f"Non-JSON line from Gemini ACP: {line_str[:200]}")
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

        Gemini ACP sends notifications with a "method" field and "params" payload.
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

        Kept for backward compatibility with older Gemini CLI versions.
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

    async def stop(self) -> None:
        """Terminate the subprocess and clean up.

        Safe to call multiple times. If the process has already exited,
        this is a no-op.
        """
        if not self._process:
            self._started = False
            self._session_id = None
            return

        try:
            if self._process.returncode is None:
                # Close stdin to signal EOF
                if self._process.stdin:
                    try:
                        self._process.stdin.close()
                    except Exception:
                        pass

                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except TimeoutError:
                    logger.warning("Gemini ACP process did not exit after terminate, killing")
                    self._process.kill()
                    await self._process.wait()
        except ProcessLookupError:
            pass  # Already gone
        except Exception as e:
            logger.debug(f"GeminiACPClient stop error (expected): {e}")
        finally:
            self._process = None
            self._started = False
            self._session_id = None
            logger.debug("GeminiACPClient stopped")
