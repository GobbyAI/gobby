"""
SDK-based autonomous agent runner.

Replaces the subprocess-based headless spawner with an in-process
ClaudeSDKClient that gets proper hooks, MCP proxy access, and session
lifecycle — the same foundation as the web chat UI.

Usage from spawn_executor:
    runner = AutonomousRunner(...)
    task = asyncio.create_task(runner.run())
    # task stored on RunningAgent for lifecycle monitoring
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    ResultMessage,
    TextBlock,
)
from claude_agent_sdk.types import (
    HookInput as SDKHookInput,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    StreamEvent,
    SyncHookJSONOutput,
    ToolPermissionContext,
)

from gobby.servers.chat_session_helpers import (
    _find_cli_path,
    _find_mcp_config,
    _response_to_compact_output,
    _response_to_post_tool_output,
    _response_to_pre_tool_output,
    _response_to_prompt_output,
    _response_to_stop_output,
)

logger = logging.getLogger(__name__)

# Type alias for the lifecycle callback provided by the caller.
# Receives event data dict, returns workflow response dict or None.
LifecycleCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


async def _approve_all_tools(
    _tool: str,
    _input: dict[str, Any],
    _ctx: ToolPermissionContext,
) -> PermissionResultAllow:
    """Autonomous agents approve all tool calls unconditionally."""
    return PermissionResultAllow()


class AutonomousRunner:
    """In-process SDK runner for autonomous agent execution.

    Autonomous agents auto-approve all tool calls and run until completion
    (or max_turns). Hooks bridge to the workflow engine via lifecycle
    callbacks, giving full rule enforcement without a terminal subprocess.
    """

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        project_id: str,
        cwd: str,
        prompt: str,
        model: str | None = None,
        system_prompt: str | None = None,
        max_turns: int | None = None,
        agent_run_manager: Any = None,
        on_before_agent: LifecycleCallback | None = None,
        on_pre_tool: LifecycleCallback | None = None,
        on_post_tool: LifecycleCallback | None = None,
        on_stop: LifecycleCallback | None = None,
        on_pre_compact: LifecycleCallback | None = None,
        seq_num: int | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.project_id = project_id
        self.cwd = cwd
        self.prompt = prompt
        self.model = model
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.agent_run_manager = agent_run_manager
        self.seq_num = seq_num
        self.resume_session_id = resume_session_id

        # Lifecycle callbacks
        self._on_before_agent = on_before_agent
        self._on_pre_tool = on_pre_tool
        self._on_post_tool = on_post_tool
        self._on_stop = on_stop
        self._on_pre_compact = on_pre_compact

        # Captured after first ResultMessage
        self.sdk_session_id: str | None = None
        self._client: ClaudeSDKClient | None = None

    async def run(self) -> str:
        """Execute the autonomous agent run.

        Returns the accumulated text output from the agent.
        Stores sdk_session_id for future cross-mode resume.
        """
        cli_path = _find_cli_path()
        if not cli_path:
            error = "Claude CLI not found in PATH"
            logger.error(f"AutonomousRunner: {error}")
            if self.agent_run_manager:
                self.agent_run_manager.fail(self.run_id, error=error)
            raise RuntimeError(error)

        mcp_config = _find_mcp_config()

        # Build system prompt with environment context
        system_prompt = self.system_prompt or "You are an autonomous coding agent."
        system_prompt += f"\n\n## Environment\n- Working directory: {self.cwd}\n"
        session_ref = f"#{self.seq_num}" if self.seq_num else self.session_id
        system_prompt += f"- Session ID: {session_ref} (use for session_id params in MCP tools)\n"
        if self.project_id:
            system_prompt += f"- Project ID: {self.project_id}\n"

        # Build SDK hooks
        sdk_hooks = self._build_sdk_hooks()

        # Environment variables for session matching
        env: dict[str, str] = {
            "GOBBY_SESSION_ID": self.session_id,
            "GOBBY_SOURCE": "autonomous_sdk",
        }
        if self.project_id:
            env["GOBBY_PROJECT_ID"] = self.project_id

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=self.max_turns,
            model=self.model or "sonnet",
            allowed_tools=["mcp__gobby__*"],
            can_use_tool=_approve_all_tools,  # Autonomous: approve all
            cli_path=cli_path,
            mcp_servers=mcp_config if mcp_config is not None else {},
            cwd=self.cwd,
            hooks=cast(Any, sdk_hooks) if sdk_hooks else None,
            env=env,
        )

        self._client = ClaudeSDKClient(options=options)
        accumulated_text = ""

        try:
            await self._client.connect()
            logger.info(
                "AutonomousRunner started: run_id=%s session=%s model=%s max_turns=%s",
                self.run_id,
                session_ref,
                self.model or "sonnet",
                self.max_turns,
            )

            # Send the prompt
            await self._client.query(self.prompt)

            # Consume the response stream
            async for message in self._client.receive_response():
                if message is None:
                    continue
                if isinstance(message, StreamEvent):
                    continue
                if isinstance(message, ResultMessage):
                    # Capture SDK session ID for cross-mode resume
                    if not self.sdk_session_id:
                        self.sdk_session_id = message.session_id
                    if message.result:
                        accumulated_text += message.result
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            accumulated_text += block.text

            logger.info(
                "AutonomousRunner completed: run_id=%s sdk_session=%s chars=%d",
                self.run_id,
                self.sdk_session_id,
                len(accumulated_text),
            )

            # Store SDK session ID for cross-mode resume
            if self.sdk_session_id and self.agent_run_manager:
                self.agent_run_manager.update_sdk_session_id(self.run_id, self.sdk_session_id)

            # Mark the agent run as complete
            if self.agent_run_manager:
                self.agent_run_manager.complete(
                    self.run_id,
                    result=accumulated_text[:10_000],  # Cap stored result
                )

            return accumulated_text

        except asyncio.CancelledError:
            logger.info("AutonomousRunner cancelled: run_id=%s", self.run_id)
            if self.agent_run_manager:
                self.agent_run_manager.fail(self.run_id, error="Cancelled")
            raise
        except Exception as e:
            logger.error(
                "AutonomousRunner failed: run_id=%s error=%s",
                self.run_id,
                e,
                exc_info=True,
            )
            if self.agent_run_manager:
                self.agent_run_manager.fail(self.run_id, error=str(e))
            raise
        finally:
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    logger.debug("AutonomousRunner: disconnect error (ignored)", exc_info=True)
                self._client = None

    def _build_sdk_hooks(self) -> dict[str, list[HookMatcher]] | None:
        """Build simplified SDK hook matchers for autonomous execution.

        No plan mode, no tool approval UI, no history injection.
        Just the core lifecycle hooks for rule engine integration.
        """
        hooks: dict[str, list[HookMatcher]] = {}

        if self._on_before_agent:
            cb = self._on_before_agent

            async def _prompt_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                data = {"prompt": inp.get("prompt", ""), "source": "autonomous_sdk"}
                resp = await cb(data)
                return _response_to_prompt_output(resp)

            hooks["UserPromptSubmit"] = [HookMatcher(matcher=None, hooks=[_prompt_hook])]

        if self._on_pre_tool:
            cb_pre = self._on_pre_tool

            async def _pre_tool_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
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
                data = {"trigger": inp.get("trigger", "auto")}
                resp = await cb_compact(data)
                return _response_to_compact_output(resp)

            hooks["PreCompact"] = [HookMatcher(matcher=None, hooks=[_compact_hook])]

        return hooks if hooks else None
