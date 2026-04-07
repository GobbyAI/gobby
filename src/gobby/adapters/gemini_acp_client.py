"""Gemini ACP (Agent Communication Protocol) client.

Wraps `gemini --acp` subprocess, communicating over JSON-RPC via stdio.
Normalizes Gemini's NDJSON stream events into structured dicts that
GeminiCLIChatSession converts to ChatEvent instances.

Gemini ACP stream format:
  init -> message(role:assistant, delta:true) -> result{stats}
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# JSON-RPC request ID counter
_next_id = 0


def _make_id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


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

    Launches ``gemini --acp`` as a subprocess and communicates via JSON-RPC
    over stdin/stdout. Each ``send()`` call writes a request and yields
    ``StreamEvent`` objects parsed from the NDJSON response stream.

    Usage::

        client = GeminiACPClient()
        await client.start()
        async for event in client.send("Hello"):
            print(event)
        await client.stop()
    """

    def __init__(self, cli_path: str | None = None) -> None:
        self._cli_path = cli_path
        self._process: asyncio.subprocess.Process | None = None
        self._started = False

    @property
    def is_started(self) -> bool:
        """Whether the subprocess has been started."""
        return self._started

    async def start(self, session_id: str | None = None) -> None:
        """Launch the ``gemini --acp`` subprocess.

        Args:
            session_id: Optional session ID to resume a previous conversation.

        Raises:
            FileNotFoundError: If the Gemini CLI binary cannot be found.
            RuntimeError: If the client is already started.
        """
        if self._started:
            raise RuntimeError("GeminiACPClient already started")

        path = self._cli_path or shutil.which("gemini")
        if not path:
            raise FileNotFoundError("Gemini CLI not found in PATH")

        cmd = [path, "--acp"]
        if session_id:
            cmd.extend(["--resume", session_id])

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._started = True
        logger.debug(f"GeminiACPClient started (pid={self._process.pid})")

    async def send(self, message: str) -> AsyncIterator[StreamEvent]:
        """Send a message and yield normalized stream events.

        Writes a JSON-RPC request to the subprocess stdin and parses
        the NDJSON response lines from stdout.

        Gemini's stream format:
        - ``{"type": "init", ...}`` -- session initialized
        - ``{"type": "message", "role": "assistant", "delta": true, "content": "..."}``
          -- incremental content
        - ``{"type": "result", "stats": {...}}`` -- turn complete

        These are normalized to ``StreamEvent`` instances:
        - init -> StreamEvent(event_type="init", data={...})
        - message with delta -> StreamEvent(event_type="content_delta", data={"content": "..."})
        - result -> StreamEvent(event_type="result", data={"stats": {...}})

        Args:
            message: The user message to send.

        Yields:
            StreamEvent instances for each line in the response stream.

        Raises:
            RuntimeError: If the client is not started or the process has died.
        """
        if not self._started or not self._process:
            raise RuntimeError("GeminiACPClient not started. Call start() first.")

        if self._process.returncode is not None:
            raise RuntimeError(f"Gemini ACP process has exited (code={self._process.returncode})")

        assert self._process.stdin is not None
        assert self._process.stdout is not None

        # Build JSON-RPC request
        request = {
            "jsonrpc": "2.0",
            "method": "send",
            "params": {"message": message},
            "id": _make_id(),
        }

        # Write request as a single line
        request_line = json.dumps(request) + "\n"
        self._process.stdin.write(request_line.encode())
        await self._process.stdin.drain()
        logger.debug(f"Sent message to Gemini ACP: {message[:80]!r}")

        # Read NDJSON response lines until we get a result or error
        async for event in self._read_stream():
            yield event

    async def _read_stream(self) -> AsyncIterator[StreamEvent]:
        """Read and parse NDJSON lines from the subprocess stdout.

        Yields StreamEvent instances. Stops after receiving a "result"
        or "error" event (end-of-turn markers).
        """
        assert self._process is not None
        assert self._process.stdout is not None

        while True:
            try:
                line = await self._process.stdout.readline()
            except asyncio.CancelledError:
                return

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

            event = self._normalize_event(data)
            yield event

            # End-of-turn markers
            if event.event_type in ("result", "error"):
                return

    @staticmethod
    def _normalize_event(raw: dict[str, Any]) -> StreamEvent:
        """Normalize a raw NDJSON object to a StreamEvent.

        Args:
            raw: Parsed JSON dict from the Gemini ACP stream.

        Returns:
            A normalized StreamEvent.
        """
        event_type = raw.get("type", "unknown")

        if event_type == "init":
            return StreamEvent(
                event_type="init",
                data={k: v for k, v in raw.items() if k != "type"},
            )

        if event_type == "message":
            # Gemini sends message events with role and optional delta flag
            role = raw.get("role", "")
            is_delta = raw.get("delta", False)
            content = raw.get("content", "")

            if role == "assistant" and is_delta:
                return StreamEvent(
                    event_type="content_delta",
                    data={"content": content, "role": role},
                )

            # Non-delta messages (full message, or non-assistant role)
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

        # Unknown event type -- pass through
        return StreamEvent(event_type=event_type, data=raw)

    async def stop(self) -> None:
        """Terminate the subprocess and clean up.

        Safe to call multiple times. If the process has already exited,
        this is a no-op.
        """
        if not self._process:
            self._started = False
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
            logger.debug("GeminiACPClient stopped")
