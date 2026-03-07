"""
Chat session backed by ClaudeSDKClient for persistent multi-turn conversations.

Each ChatSession wraps a ClaudeSDKClient instance that maintains conversation
context across messages. Sessions are keyed by conversation_id (stable across
WebSocket reconnections) rather than ephemeral client_id.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    HookInput as SDKHookInput,
)
from claude_agent_sdk.types import (
    StreamEvent,
    SyncHookJSONOutput,
    UserPromptSubmitHookSpecificOutput,
)

import gobby.llm.sdk_compat  # noqa: F401 — monkey-patch parse_message
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    resolve_context_window,
)
from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT as _ADDITIONAL_CONTEXT_LIMIT,
)
from gobby.llm.sdk_utils import (
    format_exception_group,
    sanitize_error,
)
from gobby.llm.sdk_utils import (
    parse_server_name as _parse_server_name,
)
from gobby.servers.chat_session_helpers import (
    PendingApproval,
    _find_cli_path,
    _find_mcp_config,
    _find_project_root,
    _load_chat_system_prompt,
    _response_to_compact_output,
    _response_to_post_tool_output,
    _response_to_pre_tool_output,
    _response_to_prompt_output,
    _response_to_stop_output,
)
from gobby.servers.chat_session_permissions import ChatSessionPermissionsMixin

logger = logging.getLogger(__name__)


@dataclass
class ChatSession(ChatSessionPermissionsMixin):
    """
    A persistent chat session backed by ClaudeSDKClient.

    Maintains conversation context across messages and survives
    WebSocket disconnections. Sessions are identified by conversation_id.
    """

    conversation_id: str
    db_session_id: str | None = field(default=None)
    seq_num: int | None = field(default=None)
    project_id: str | None = field(default=None)
    project_path: str | None = field(default=None)
    message_index: int = field(default=0)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _connected: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _model: str | None = field(default=None, repr=False)
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, str] | None = field(default=None, repr=False)
    _pending_approval: PendingApproval | None = field(default=None, repr=False)
    _pending_approval_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_approval_decision: str | None = field(default=None, repr=False)
    _approved_tools: set[str] = field(default_factory=set, repr=False)
    chat_mode: str = field(default="plan", repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _plan_approval_completed: bool = field(default=False, repr=False)
    _plan_file_path: str | None = field(default=None, repr=False)
    _pending_plan_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_plan_decision: str | None = field(default=None, repr=False)
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None = field(
        default=None, repr=False
    )
    _tool_approval_config: Any | None = field(default=None, repr=False)
    _tool_approval_callback: Any | None = field(default=None, repr=False)
    _needs_history_injection: bool = field(default=False, repr=False)
    _last_model: str | None = field(default=None, repr=False)
    _message_manager: Any | None = field(default=None, repr=False)
    _message_manager_source_session_id: str | None = field(default=None, repr=False)
    _pending_agent_name: str | None = field(default=None, repr=False)
    _max_history_message_chars: int = field(default=2000, repr=False)
    _max_history_total_chars: int = field(default=30_000, repr=False)
    _accumulated_output_tokens: int = field(default=0, repr=False)
    _accumulated_cost_usd: float = field(default=0.0, repr=False)
    sdk_session_id: str | None = field(default=None, repr=False)
    system_prompt_override: str | None = field(default=None, repr=False)
    resume_session_id: str | None = field(default=None, repr=False)
    _session_manager_ref: Any | None = field(default=None, repr=False)
    _jsonl_path_captured: bool = field(default=False, repr=False)

    # Lifecycle callbacks — set by ChatMixin to bridge SDK hooks to workflow engine
    _on_before_agent: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_pre_compact: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None = field(default=None, repr=False)
    _on_mode_persist: Callable[[str], None] | None = field(default=None, repr=False)

    async def start(self, model: str | None = None) -> None:
        """Connect the ClaudeSDKClient with configured options."""
        cli_path = _find_cli_path()
        if not cli_path:
            raise RuntimeError("Claude CLI not found in PATH")

        mcp_config = _find_mcp_config()
        self._model = model

        # Use the project's repo_path if available (set by web UI project selector),
        # otherwise fall back to gobby project root (dev mode) or cwd.
        if self.project_path:
            cwd = self.project_path
        else:
            project_root = _find_project_root()
            cwd = str(project_root) if project_root else str(Path.cwd())

        # SDK resume carries its own system prompt and context — skip construction
        if self.resume_session_id:
            system_prompt = None
        else:
            if self.system_prompt_override:
                system_prompt = self.system_prompt_override
            else:
                system_prompt = _load_chat_system_prompt()
            # Inject working directory so the agent doesn't hallucinate paths
            system_prompt += f"\n\n## Environment\n- Working directory: {cwd}\n"
            if self.db_session_id:
                session_ref = f"#{self.seq_num}" if self.seq_num else self.db_session_id
                system_prompt += (
                    f"- Session ID: {session_ref} (use for session_id params in MCP tools)\n"
                )
            if self.project_id:
                system_prompt += f"- Project ID: {self.project_id}\n"

        # Build SDK hooks from lifecycle callbacks
        sdk_hooks = self._build_sdk_hooks()

        # Pass session context to the CLI subprocess so it attaches to the
        # web chat's pre-created session instead of creating a new one.
        env: dict[str, str] = {}
        if self.db_session_id:
            env["GOBBY_SESSION_ID"] = self.db_session_id
            env["GOBBY_SOURCE"] = "claude_sdk_web_chat"
        if self.project_id:
            env["GOBBY_PROJECT_ID"] = self.project_id

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=None,
            model=model or "opus",
            allowed_tools=["mcp__gobby__*"],
            can_use_tool=self._can_use_tool,
            cli_path=cli_path,
            mcp_servers=mcp_config if mcp_config is not None else {},
            cwd=cwd,
            hooks=cast(Any, sdk_hooks) if sdk_hooks else None,
            env=env or {},
            # Enable partial messages so we receive StreamEvent objects with
            # per-API-call usage from message_start events. Without this, the
            # ResultMessage.usage contains accumulated token counts across ALL
            # API calls in the agentic loop, making context % wildly wrong.
            include_partial_messages=True,
            # SDK native resume — picks up exact conversation state from a
            # previous session (terminal, autonomous, or web chat).
            resume=self.resume_session_id,
            continue_conversation=bool(self.resume_session_id),
        )

        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._connected = True
        self.last_activity = datetime.now(UTC)
        logger.debug(f"ChatSession {self.conversation_id} started")

    def _build_sdk_hooks(self) -> dict[str, list[HookMatcher]] | None:
        """Build SDK hook matchers from lifecycle callbacks."""
        hooks: dict[str, list[HookMatcher]] = {}

        if self._on_before_agent:
            cb = self._on_before_agent

            async def _prompt_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                # Capture transcript_path on first invocation
                if not self._jsonl_path_captured and self._session_manager_ref:
                    transcript_path = inp.get("transcript_path")
                    if transcript_path and self.db_session_id:
                        try:
                            self._session_manager_ref.update(
                                self.db_session_id, jsonl_path=str(transcript_path)
                            )
                            self._jsonl_path_captured = True
                            logger.debug(
                                "Captured jsonl_path for session %s: %s",
                                self.db_session_id[:8],
                                transcript_path,
                            )
                        except Exception as e:
                            logger.warning("Failed to capture jsonl_path: %s", e)

                data = {"prompt": inp.get("prompt", ""), "source": "claude_sdk_web_chat"}
                resp = await cb(data)
                output = _response_to_prompt_output(resp)

                context_parts = []

                hook_specific = output.get("hookSpecificOutput")
                if hook_specific and isinstance(hook_specific, dict):
                    existing = hook_specific.get("additionalContext")
                    if existing:
                        context_parts.append(str(existing))

                plan_ctx = getattr(self, "_consume_plan_mode_context", lambda: None)()
                if plan_ctx:
                    context_parts.append(plan_ctx)

                if context_parts:
                    output["hookSpecificOutput"] = UserPromptSubmitHookSpecificOutput(
                        hookEventName="UserPromptSubmit",
                        additionalContext="\n\n".join(context_parts).strip(),
                    )

                # Inject conversation history on first prompt of a recreated session
                if self._needs_history_injection:
                    self._needs_history_injection = False
                    # Calculate budget: existing context gets priority, history fills the rest
                    existing = ""
                    hook_specific = output.get("hookSpecificOutput")
                    if hook_specific and isinstance(hook_specific, dict):
                        existing = str(hook_specific.get("additionalContext", "") or "")
                    history_budget = _ADDITIONAL_CONTEXT_LIMIT - len(existing) - 4  # "\n\n" joiner
                    if history_budget > 500:  # Only inject if meaningful space remains
                        history_ctx = await self._load_history_context(
                            max_total_chars=history_budget
                        )
                        if history_ctx:
                            combined = (
                                (existing + "\n\n" + history_ctx).strip()
                                if existing
                                else history_ctx
                            )
                            output["hookSpecificOutput"] = UserPromptSubmitHookSpecificOutput(
                                hookEventName="UserPromptSubmit",
                                additionalContext=combined,
                            )

                return output

            hooks["UserPromptSubmit"] = [HookMatcher(matcher=None, hooks=[_prompt_hook])]

        if self._on_pre_tool:
            cb_pre = self._on_pre_tool

            async def _pre_tool_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                # DEBUG: log raw SDK hook input keys to diagnose hook issues
                logger.debug(
                    "_pre_tool_hook raw inp keys=%s, tool_name=%r",
                    list(inp.keys()) if isinstance(inp, dict) else type(inp).__name__,
                    inp.get("tool_name") if isinstance(inp, dict) else "N/A",
                )
                data = {
                    "tool_name": inp.get("tool_name", ""),
                    "tool_input": inp.get("tool_input", {}),
                }
                resp = await cb_pre(data)
                return _response_to_pre_tool_output(resp)

            hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[_pre_tool_hook])]

        if self._on_post_tool:
            cb_post = self._on_post_tool

            async def _post_tool_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                data = {
                    "tool_name": inp.get("tool_name", ""),
                    "tool_input": inp.get("tool_input", {}),
                    "tool_response": inp.get("tool_response"),
                }
                resp = await cb_post(data)
                return _response_to_post_tool_output(resp)

            hooks["PostToolUse"] = [HookMatcher(matcher=None, hooks=[_post_tool_hook])]

        if self._on_stop:
            cb_stop = self._on_stop

            async def _stop_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                data = {"stop_hook_active": inp.get("stop_hook_active", False)}
                resp = await cb_stop(data)
                return _response_to_stop_output(resp)

            hooks["Stop"] = [HookMatcher(matcher=None, hooks=[_stop_hook])]

        if self._on_pre_compact:
            cb_compact = self._on_pre_compact

            async def _compact_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                data = {
                    "trigger": inp.get("trigger", "auto"),
                }
                resp = await cb_compact(data)
                return _response_to_compact_output(resp)

            hooks["PreCompact"] = [HookMatcher(matcher=None, hooks=[_compact_hook])]

        return hooks if hooks else None

    async def _load_history_context(self, max_total_chars: int | None = None) -> str | None:
        """Load prior conversation messages and format as context for injection.

        Args:
            max_total_chars: Override for maximum total characters. If None, uses
                the instance default (_max_history_total_chars). Callers should pass
                a budget that accounts for other additionalContext content to avoid
                Claude Code's 10K truncation limit.

        Returns a formatted string with conversation history, or None if
        no messages exist or an error occurs.
        """
        if not self._message_manager:
            return None
        target_id = self._message_manager_source_session_id or self.db_session_id
        if not target_id:
            return None

        try:
            messages = await self._message_manager.get_messages(target_id, limit=50)
            if not messages:
                return None

            # Filter to user/assistant text messages only
            text_messages = [
                m
                for m in messages
                if m.get("role") in ("user", "assistant")
                and m.get("content_type") == "text"
                and m.get("content")
            ]
            if not text_messages:
                return None

            max_msg_chars = self._max_history_message_chars
            effective_max = (
                max_total_chars if max_total_chars is not None else self._max_history_total_chars
            )
            # Reserve space for the XML wrapper + inter-entry separators (~180 chars)
            wrapper_overhead = 200
            content_budget = effective_max - wrapper_overhead
            if content_budget <= 0:
                return None

            parts: list[str] = []
            total = 0

            for m in text_messages:
                role_label = "**User:**" if m["role"] == "user" else "**Assistant:**"
                content = m["content"]
                if len(content) > max_msg_chars:
                    content = content[:max_msg_chars] + "..."
                entry = f"{role_label} {content}"
                if total + len(entry) > content_budget:
                    break
                parts.append(entry)
                total += len(entry)

            if not parts:
                return None

            return (
                "<conversation-history>\n"
                "The following is the prior conversation history for this session, "
                "restored after session recreation. Use it to maintain continuity.\n\n"
                + "\n\n".join(parts)
                + "\n</conversation-history>"
            )
        except Exception as e:
            logger.warning(f"Failed to load history context for {self.conversation_id}: {e}")
            return None

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        """
        Send a user message and yield streaming events.

        Content can be a plain string or a list of content blocks
        (e.g. text + images in the standard Claude API format).

        Yields ChatEvent instances (TextChunk, ToolCallEvent,
        ToolResultEvent, DoneEvent) matching the existing protocol.
        """
        if not self._client or not self._connected:
            raise RuntimeError("ChatSession not connected. Call start() first.")

        async with self._lock:
            self.last_activity = datetime.now(UTC)

            if isinstance(content, list):
                # SDK streaming mode expects the transport protocol format:
                # {"type": "user", "message": {"role": "user", "content": ...}}
                # NOT just {"role": "user", "content": ...}
                async def _content_blocks() -> AsyncIterator[dict[str, Any]]:
                    yield {
                        "type": "user",
                        "message": {"role": "user", "content": content},
                        "parent_tool_use_id": None,
                    }

                await self._client.query(_content_blocks())
            else:
                await self._client.query(content)

            tool_calls_count = 0
            needs_spacing_before_text = False
            has_text = False
            context_window: int | None = None
            # Track the LAST API call's input usage from message_start stream
            # events. ResultMessage.usage accumulates across ALL API calls in
            # the agentic loop, making total_input wildly exceed context_window
            # for tool-heavy turns.  message_start gives per-call values.
            _last_call_input: dict[str, int] | None = None
            try:
                async for message in self._client.receive_response():
                    if message is None:
                        continue
                    if isinstance(message, StreamEvent):
                        # Capture per-API-call input usage from message_start.
                        # Each API call in the agentic loop emits one; the last
                        # one reflects the actual current context window load.
                        ev = message.event
                        if isinstance(ev, dict) and ev.get("type") == "message_start":
                            msg_body = ev.get("message")
                            if isinstance(msg_body, dict):
                                u = msg_body.get("usage")
                                if isinstance(u, dict):
                                    _last_call_input = u
                        continue
                    if isinstance(message, ResultMessage):
                        # Capture SDK session_id on first ResultMessage
                        if not self.sdk_session_id:
                            self.sdk_session_id = message.session_id
                        # Fallback: if no text was streamed (e.g. Opus thinking-only
                        # response), emit the ResultMessage.result as a TextChunk
                        if message.result and not has_text:
                            yield TextChunk(content=message.result)
                        cost_usd = getattr(message, "total_cost_usd", None)
                        duration_ms = getattr(message, "duration_ms", None)
                        # Extract token usage from ResultMessage.usage dict
                        # (AssistantMessage does NOT carry usage in the SDK)
                        _raw_usage = getattr(message, "usage", None)
                        has_usage = isinstance(_raw_usage, dict)
                        if not has_usage:
                            logger.warning(
                                "ResultMessage missing usage for session %s",
                                self.conversation_id[:8],
                            )
                        usage: dict[str, Any] = (
                            cast(dict[str, Any], _raw_usage) if has_usage else {}
                        )

                        # --- Context window tracking (per-call) ---
                        # Prefer per-call values from message_start stream events
                        # (accurate context size). Fall back to ResultMessage.usage
                        # which is accumulated across all API calls in the turn.
                        if _last_call_input:
                            uncached_input = _last_call_input.get("input_tokens", 0) or 0
                            cache_read = _last_call_input.get("cache_read_input_tokens", 0) or 0
                            cache_creation = (
                                _last_call_input.get("cache_creation_input_tokens", 0) or 0
                            )
                        else:
                            uncached_input = usage.get("input_tokens", 0) or 0
                            cache_read = usage.get("cache_read_input_tokens", 0) or 0
                            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                        total_input = uncached_input + cache_read + cache_creation

                        # Output tokens: use accumulated from ResultMessage
                        # (correct for cost tracking; not part of context %)
                        output_tokens = usage.get("output_tokens", 0) or 0

                        _model_usage = getattr(message, "_model_usage", None)
                        context_window = resolve_context_window(self._last_model, _model_usage)

                        logger.info(
                            "DoneEvent: uncached=%d cache_read=%d cache_creation=%d "
                            "total_input=%d output=%d context_window=%s "
                            "per_call=%s",
                            uncached_input,
                            cache_read,
                            cache_creation,
                            total_input,
                            output_tokens,
                            context_window,
                            _last_call_input is not None,
                        )
                        yield DoneEvent(
                            tool_calls_count=tool_calls_count,
                            cost_usd=cost_usd,
                            duration_ms=duration_ms,
                            input_tokens=uncached_input if has_usage else None,
                            output_tokens=output_tokens if has_usage else None,
                            cache_read_input_tokens=cache_read if has_usage else None,
                            cache_creation_input_tokens=cache_creation if has_usage else None,
                            total_input_tokens=total_input if has_usage else None,
                            context_window=context_window,
                            sdk_session_id=self.sdk_session_id,
                        )

                    elif isinstance(message, AssistantMessage):
                        self._last_model = getattr(message, "model", None)
                        logger.debug("AssistantMessage model=%s", self._last_model)
                        for block in message.content:
                            if isinstance(block, ThinkingBlock):
                                yield ThinkingEvent(content=block.thinking)
                            elif isinstance(block, TextBlock):
                                has_text = True
                                text = block.text
                                if needs_spacing_before_text and text:
                                    text = text.lstrip("\n")
                                    if text:
                                        text = "\n\n" + text
                                yield TextChunk(content=text)
                                needs_spacing_before_text = False
                            elif isinstance(block, ToolUseBlock):
                                tool_calls_count += 1
                                server_name = _parse_server_name(block.name)
                                # Set spacing flag eagerly — if the tool is denied
                                # at the permission gate (e.g. task validation) the
                                # SDK may not yield a ToolResultBlock, leaving the
                                # flag unset and causing run-on text like
                                # "commit.The commit".  ToolResultBlock re-sets it
                                # harmlessly when it does arrive.
                                needs_spacing_before_text = True
                                yield ToolCallEvent(
                                    tool_call_id=block.id,
                                    tool_name=block.name,
                                    server_name=server_name,
                                    arguments=block.input if isinstance(block.input, dict) else {},
                                )

                    elif isinstance(message, UserMessage):
                        if isinstance(message.content, list):
                            for block in message.content:
                                if isinstance(block, ToolResultBlock):
                                    is_error = getattr(block, "is_error", False)
                                    # Serialize content safely — it can be str,
                                    # list of content blocks, or other types
                                    raw = block.content
                                    if isinstance(raw, str):
                                        content_str = raw
                                    elif isinstance(raw, list):
                                        parts = []
                                        for item in raw:
                                            item_text: str | None = getattr(item, "text", None)
                                            if item_text is not None:
                                                parts.append(item_text)
                                            else:
                                                parts.append(str(item))
                                        content_str = "\n".join(parts)
                                    else:
                                        content_str = str(raw) if raw is not None else ""
                                    yield ToolResultEvent(
                                        tool_call_id=block.tool_use_id,
                                        success=not is_error,
                                        result=content_str if not is_error else None,
                                        error=content_str if is_error else None,
                                    )
                                    needs_spacing_before_text = True

            except ExceptionGroup as eg:
                yield TextChunk(content=f"Generation failed: {format_exception_group(eg)}")
                if context_window is None:
                    context_window = self._resolve_context_window_fallback()
                yield DoneEvent(tool_calls_count=tool_calls_count, context_window=context_window)
            except Exception as e:
                logger.error(f"ChatSession {self.conversation_id} error: {e}", exc_info=True)
                yield TextChunk(content=f"Generation failed: {sanitize_error(e)}")
                if context_window is None:
                    context_window = self._resolve_context_window_fallback()
                yield DoneEvent(tool_calls_count=tool_calls_count, context_window=context_window)

    def _resolve_context_window_fallback(self) -> int | None:
        """Resolve context_window from _last_model for error paths."""
        return resolve_context_window(self._last_model, None)

    async def interrupt(self) -> None:
        """Interrupt the current response stream."""
        if self._client and self._connected:
            try:
                await self._client.interrupt()
            except Exception as e:
                logger.warning(f"ChatSession {self.conversation_id} interrupt error: {e}")

    async def drain_pending_response(self) -> None:
        """Drain any buffered response events from the SDK after an interrupt.

        After ``interrupt()`` + task cancellation, the SDK may still have
        stale response events in its internal buffer.  If not consumed,
        those events leak into the **next** ``receive_response()`` call,
        causing the off-by-one bug where the response to message N+1
        actually contains content generated for message N.

        This method must be called **after** the streaming task has been
        cancelled and the ``_lock`` has been released.
        """
        if not self._client or not self._connected:
            return
        try:
            async with asyncio.timeout(1.0):
                async for _ in self._client.receive_response():
                    pass
        except TimeoutError:
            logger.debug(f"ChatSession {self.conversation_id}: drain timed out (no stale events)")
        except Exception as e:
            logger.debug(f"ChatSession {self.conversation_id}: drain error (expected): {e}")

    async def stop(self) -> None:
        """Disconnect the ClaudeSDKClient and clean up."""
        if self._client:
            try:
                await self._client.disconnect()
            except RuntimeError as e:
                # The SDK's Query._tg.__aexit__() raises RuntimeError when
                # stop() is called from a different asyncio task than the one
                # that called start() (e.g. idle cleanup or shutdown).
                if "cancel scope" in str(e):
                    logger.debug(
                        f"ChatSession {self.conversation_id} cross-task disconnect (expected): {e}"
                    )
                else:
                    logger.debug(
                        f"ChatSession {self.conversation_id} disconnect error (expected): {e}"
                    )
            except Exception as e:
                logger.debug(f"ChatSession {self.conversation_id} disconnect error (expected): {e}")
            finally:
                self._client = None
                self._connected = False
                logger.debug(f"ChatSession {self.conversation_id} stopped")

    @property
    def model(self) -> str | None:
        """The current model for this session."""
        return self._model

    async def switch_model(self, new_model: str) -> None:
        """Switch to a different Claude model mid-conversation."""
        if not self._client or not self._connected:
            raise RuntimeError("ChatSession not connected")
        await self._client.set_model(new_model)
        self._model = new_model

    @property
    def is_connected(self) -> bool:
        """Whether the session is currently connected."""
        return self._connected
