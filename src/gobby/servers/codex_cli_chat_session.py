"""Codex CLI-backed web chat session via app-server protocol.

Spawns ``codex app-server`` and communicates via JSON-RPC 2.0 over stdio.
The protocol requires an initialize handshake, thread creation via
thread/start, and turn execution via turn/start. Streaming content arrives
as agent/messageDelta notifications.

Protocol lifecycle:
  1. initialize  →  handshake with client info
  2. thread/start  →  create a conversation thread
  3. turn/start  →  send user input, receive streaming notifications
  4. agent/messageDelta  →  streaming text content
  5. turn/completed  →  turn finished with usage data
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
)
from gobby.sessions.transcripts.codex import CodexTranscriptParser

logger = logging.getLogger(__name__)


def _extract_text(content: str | list[dict[str, Any]]) -> str:
    """Extract text from content (string or multimodal blocks)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


class CodexCLIChatSession:
    """Codex CLI-backed web chat session implementing ChatSessionProtocol.

    Spawns ``codex app-server`` and communicates via JSON-RPC 2.0 over stdio.
    Lifecycle hooks and approvals are handled via PendingInteractionManager,
    not in this class.
    """

    provider: str = "codex"

    def __init__(
        self,
        conversation_id: str,
        model: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        # Identity / protocol fields
        self.conversation_id = conversation_id
        self.db_session_id: str | None = None
        self.seq_num: int | None = None
        self.project_id: str | None = None
        self.project_path: str | None = None
        self.message_index: int = 0
        self.chat_mode: str = "code"
        self.system_prompt_override: str | None = None
        self.resume_session_id: str | None = None
        self.sdk_session_id: str | None = None
        self.last_activity: datetime = datetime.now(UTC)

        # Lifecycle callbacks (protocol conformance)
        self._on_before_agent: (
            Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
        ) = None
        self._on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = (
            None
        )
        self._on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = (
            None
        )
        self._on_pre_compact: (
            Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
        ) = None
        self._on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
        self._on_mode_changed: Callable[[str, str], Awaitable[None]] | None = None
        self._on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None = None

        # Optional attrs set dynamically by WebSocket session control
        self._tool_approval_config: Any = None
        self._tool_approval_callback: Callable[..., Any] | None = None
        self._session_manager_ref: Any = None
        self._on_mode_persist: Callable[[str], None] | None = None
        self._on_approved_tools_persist: Callable[[set[str]], None] | None = None
        self._approved_tools: set[str] = set()
        self._plan_file_path: str | None = None
        self._pending_agent_name: str | None = None
        self._plan_approval_completed: bool = False
        self._context_window_overrides: dict[str, int] = {}
        self._accumulated_output_tokens: int = 0
        self._message_manager_source_session_id: str | None = None
        self._needs_history_injection: bool = False
        self._message_manager: Any = None
        self._config: Any = None

        # Codex internals
        self._model = model
        self._thread_id = thread_id
        self._turn_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False
        self._request_id = itertools.count(1)
        self._read_timeout = 30.0
        self._transcript_path: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str | None:
        return self._model

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response.

        Skips any notifications (no "id" field) received before the response.

        Args:
            method: The JSON-RPC method name.
            params: The request parameters.

        Returns:
            The result dict from the JSON-RPC response.

        Raises:
            RuntimeError: If the process is not running or returns an error.
        """
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("CodexCLIChatSession process not available")

        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._request_id),
        }

        request_line = json.dumps(request) + "\n"
        self._process.stdin.write(request_line.encode())
        await self._process.stdin.drain()
        logger.debug(f"Sent Codex request: {method}")

        # Read lines until we get a JSON-RPC response (has "id" field)
        while True:
            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=self._read_timeout,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Timed out waiting for Codex {method} response after {self._read_timeout:.1f}s"
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
                    raise RuntimeError(f"Codex {method} error: {err.get('message', err)}")
                result: dict[str, Any] = data.get("result", {})
                return result

            # Skip notifications during handshake
            logger.debug(f"Skipping notification during {method}: {data.get('method', 'unknown')}")

    async def start(self, model: str | None = None) -> None:
        """Spawn ``codex app-server``, perform initialize handshake, and create thread.

        Args:
            model: Optional model override.

        Raises:
            FileNotFoundError: If Codex CLI is not found.
            RuntimeError: If handshake or thread creation fails.
        """
        path = shutil.which("codex")
        if not path:
            raise FileNotFoundError("Codex CLI not found")

        if model:
            self._model = model

        cmd = [path, "app-server"]
        env = os.environ.copy()
        env["GOBBY_HOOKS_DISABLED"] = "1"

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.debug(f"Codex app-server started (pid={self._process.pid})")

        # Initialize handshake
        init_result = await self._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "gobby",
                    "version": "1.0.0",
                },
            },
        )
        logger.debug(f"Codex initialize response: {init_result}")

        # Create or resume thread
        if self._thread_id:
            # Resume existing thread
            resume_result = await self._send_request(
                "thread/resume",
                {"threadId": self._thread_id},
            )
            thread_data = resume_result.get("thread", {})
            self._transcript_path = thread_data.get("path") or self._transcript_path
        else:
            thread_params: dict[str, Any] = {
                "cwd": self.project_path or ".",
            }
            if self._model:
                thread_params["model"] = self._model

            thread_result = await self._send_request("thread/start", thread_params)
            thread_data = thread_result.get("thread", {})
            self._thread_id = thread_data.get("id") or thread_result.get("threadId")
            self._transcript_path = thread_data.get("path")
            logger.debug(f"Codex thread ID: {self._thread_id}")

        self.sdk_session_id = self._thread_id
        self._connected = True

    async def _get_transcript_offset(self) -> int:
        """Return the current transcript file size before a turn starts."""
        if not self._transcript_path:
            return 0

        def _stat_size() -> int:
            try:
                return os.path.getsize(self._transcript_path or "")
            except OSError:
                return 0

        return await asyncio.to_thread(_stat_size)

    async def _get_transcript_assistant_text_since(self, offset: int) -> str:
        """Extract assistant text written after ``offset`` from the Codex transcript."""
        if not self._transcript_path:
            return ""

        def _read_assistant_text() -> str:
            try:
                with open(self._transcript_path or "", encoding="utf-8") as handle:
                    handle.seek(offset)
                    parser = CodexTranscriptParser(session_id=self._thread_id)
                    parsed = parser.parse_lines(handle.readlines())
            except OSError:
                return ""

            assistant_chunks = [
                message.content.strip()
                for message in parsed
                if message.role == "assistant" and message.content.strip()
            ]
            return "\n\n".join(assistant_chunks)

        # Codex can flush transcript lines slightly after the turn-completed
        # notification arrives, so poll briefly before giving up.
        for _ in range(5):
            assistant_text = await asyncio.to_thread(_read_assistant_text)
            if assistant_text:
                return assistant_text
            await asyncio.sleep(0.1)
        return ""

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        """Send a message via turn/start and stream responses.

        Sends a turn/start JSON-RPC request, then reads notifications:
        - agent/messageDelta: streaming text → TextChunk
        - turn/completed: turn finished → DoneEvent

        Args:
            content: Plain text or content block list.

        Yields:
            ChatEvent instances (TextChunk, DoneEvent).

        Raises:
            RuntimeError: If session is not started.
        """
        text = _extract_text(content)
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("CodexCLIChatSession not started")

        # Send turn/start request
        turn_params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if self._model:
            turn_params["model"] = self._model

        request = {
            "jsonrpc": "2.0",
            "method": "turn/start",
            "params": turn_params,
            "id": next(self._request_id),
        }

        request_line = json.dumps(request) + "\n"
        self._process.stdin.write(request_line.encode())
        await self._process.stdin.drain()
        self.message_index += 1
        self.last_activity = datetime.now(UTC)
        self._turn_id = None
        logger.debug(f"Sent turn/start to Codex: {text[:80]!r}")
        transcript_offset = await self._get_transcript_offset()
        saw_text_output = False

        # Read streaming notifications until turn/completed or response
        while True:
            try:
                raw_line = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=self._read_timeout,
                )
            except TimeoutError:
                yield TextChunk(content="Codex response timed out while waiting for output.")
                yield DoneEvent(tool_calls_count=0)
                break
            if not raw_line:
                break
            line = raw_line.decode().strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # JSON-RPC response (has "id") = turn/start acknowledgment or final
            if "id" in data:
                if "error" in data:
                    err = data["error"]
                    yield TextChunk(content=f"Error: {err.get('message', err)}")
                    yield DoneEvent(tool_calls_count=0)
                    break
                result = data.get("result", {})
                if isinstance(result, dict):
                    turn_id = result.get("turnId")
                    if not turn_id:
                        turn = result.get("turn")
                        if isinstance(turn, dict):
                            turn_id = turn.get("id")
                    if turn_id:
                        self._turn_id = str(turn_id)
                # turn/start response just acknowledges — keep reading notifications
                continue

            # JSON-RPC notification (no "id")
            method = data.get("method", "")
            params = data.get("params", {})

            if method == "agent/messageDelta":
                # Streaming text content
                delta = params.get("delta", "")
                if delta:
                    saw_text_output = True
                    yield TextChunk(content=delta)

            elif method == "turn/completed":
                # Turn finished — extract usage data
                usage = params.get("usage", {})
                self._turn_id = None
                if not saw_text_output:
                    fallback_text = await self._get_transcript_assistant_text_since(
                        transcript_offset
                    )
                    if fallback_text:
                        yield TextChunk(content=fallback_text)
                yield DoneEvent(
                    tool_calls_count=0,
                    cost_usd=float(usage.get("cost_usd", 0.0)),
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    sdk_session_id=self.sdk_session_id,
                )
                break

            elif method == "turn/started":
                # Turn started notification — informational, skip
                turn_id = params.get("turnId")
                if turn_id:
                    self._turn_id = str(turn_id)
                logger.debug(f"Codex turn started: {turn_id or 'unknown'}")

            elif method == "item/started" or method == "item/completed":
                # Item lifecycle — skip for now
                logger.debug(f"Codex {method}: {params.get('itemId', 'unknown')}")

            elif method == "thread/closed":
                # Thread closed unexpectedly
                yield DoneEvent(tool_calls_count=0)
                break

    async def interrupt(self) -> None:
        """Send an interrupt request for the active turn."""
        if self._process and self._process.stdin and self._thread_id and self._turn_id:
            try:
                interrupt_request = {
                    "jsonrpc": "2.0",
                    "method": "turn/interrupt",
                    "params": {"threadId": self._thread_id, "turnId": self._turn_id},
                    "id": next(self._request_id),
                }
                line = json.dumps(interrupt_request) + "\n"
                self._process.stdin.write(line.encode())
                await self._process.stdin.drain()
                self._turn_id = None
            except Exception as e:
                logger.debug(f"Codex interrupt error: {e}")
        elif self._thread_id:
            logger.debug("Codex interrupt skipped: no active turn for %s", self._thread_id)

    async def drain_pending_response(self) -> None:
        pass

    async def stop(self) -> None:
        """Terminate Codex app-server process."""
        if self._process:
            try:
                if self._process.returncode is None:
                    if self._process.stdin:
                        try:
                            self._process.stdin.close()
                        except Exception:
                            pass
                    self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    except TimeoutError:
                        self._process.kill()
                        await self._process.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"Codex stop error: {e}")
            finally:
                self._process = None
                self._connected = False
                self.sdk_session_id = None
                self._thread_id = None
                self._turn_id = None

    async def switch_model(self, new_model: str) -> None:
        self._model = new_model

    def set_chat_mode(self, mode: str) -> None:
        self.chat_mode = mode

    @property
    def has_pending_plan(self) -> bool:
        return False

    @property
    def has_pending_question(self) -> bool:
        return False

    @property
    def has_pending_approval(self) -> bool:
        return False

    def provide_answer(self, answers: dict[str, Any]) -> None:
        pass

    def provide_approval(self, decision: str) -> None:
        pass

    def provide_plan_decision(self, decision: str) -> None:
        pass

    def approve_plan(self) -> None:
        pass

    def set_plan_feedback(self, feedback: str) -> None:
        pass

    async def sync_sdk_permission_mode(self) -> None:
        pass
