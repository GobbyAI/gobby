"""Codex CLI-backed web chat session via app-server protocol.

Mirrors CLIChatSession but wraps the Codex CLI subprocess for web chat.
Normalizes Codex app-server events to ChatEvent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
)

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

    Wraps the Codex CLI subprocess. Lifecycle hooks and approvals are
    handled via PendingInteractionManager, not in this class.
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
        self._accumulated_cost_usd: float = 0.0
        self._message_manager_source_session_id: str | None = None
        self._needs_history_injection: bool = False
        self._message_manager: Any = None

        # Codex internals
        self._model = model
        self._thread_id = thread_id
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str | None:
        return self._model

    async def start(self, model: str | None = None) -> None:
        """Spawn Codex CLI subprocess."""
        path = shutil.which("codex")
        if not path:
            raise FileNotFoundError("Codex CLI not found")

        cmd = [path]
        if model or self._model:
            cmd.extend(["--model", model or self._model or ""])
        if self._thread_id:
            cmd.extend(["resume", self._thread_id])

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._connected = True

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        """Send message to Codex CLI and stream responses."""
        text = _extract_text(content)
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("CodexCLIChatSession not started")

        self._process.stdin.write((text + "\n").encode())
        await self._process.stdin.drain()
        self.message_index += 1
        self.last_activity = datetime.now(UTC)

        # Read NDJSON lines from stdout and normalize to ChatEvent
        while True:
            raw_line = await self._process.stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode().strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "")

            if "delta" in event_type:
                text_content = data.get("delta", {}).get("content", "") or data.get("content", "")
                if text_content:
                    yield TextChunk(content=text_content)
            elif event_type == "turn.completed":
                usage = data.get("usage", {})
                yield DoneEvent(
                    tool_calls_count=0,
                    cost_usd=float(usage.get("cost_usd", 0.0)),
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                )
                break

    async def interrupt(self) -> None:
        """Send interrupt signal to Codex process."""
        if self._process:
            self._process.send_signal(signal.SIGINT)

    async def drain_pending_response(self) -> None:
        pass

    async def stop(self) -> None:
        """Terminate Codex CLI process."""
        if self._process:
            self._process.terminate()
            await self._process.wait()
            self._connected = False

    async def switch_model(self, new_model: str) -> None:
        self._model = new_model

    def set_chat_mode(self, mode: str) -> None:
        self.chat_mode = mode

    def provide_answer(self, answers: dict[str, str]) -> None:
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
