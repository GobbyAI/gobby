"""SDK hook construction for ChatSession."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

from claude_agent_sdk import HookContext, HookMatcher, PermissionResultDeny
from claude_agent_sdk.types import HookInput as SDKHookInput
from claude_agent_sdk.types import SyncHookJSONOutput, UserPromptSubmitHookSpecificOutput

from gobby.adapters.degradation import (
    AdditionalContextPersistKwargs,
    tool_result_store_from_hook_manager,
)
from gobby.hooks.context_limits import additional_context_limit_for
from gobby.hooks.events import SessionSource
from gobby.servers.chat_session_helpers import (
    _PLAN_FILE_PATTERN,
    _bound_resp_context,
    _response_to_compact_output,
    _response_to_post_tool_output,
    _response_to_pre_tool_output,
    _response_to_prompt_output,
    _response_to_stop_output,
    _response_to_subagent_output,
)

logger = logging.getLogger(__name__)


class ChatSessionHooksMixin:
    """Build Claude SDK hooks from ChatSession lifecycle callbacks."""

    conversation_id: str
    db_session_id: str | None
    project_id: str | None
    chat_mode: str
    _needs_history_injection: bool
    _plan_approved: bool
    _plan_broadcast_sent: bool
    _preapproved_tool_use_ids: set[str]
    _session_manager_ref: Any | None
    _transcript_path_captured: bool
    _on_before_agent: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_pre_compact: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_subagent_start: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_subagent_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_plan_ready: Callable[[str | None, dict[str, Any], str | None], Awaitable[None]] | None

    def _additional_context_persist(self) -> AdditionalContextPersistKwargs:
        """Session/project/store kwargs for additionalContext overflow persist."""
        return {
            "store": tool_result_store_from_hook_manager(self._session_manager_ref),
            "session_id": self.db_session_id,
            "project_id": self.project_id,
        }

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
                # Capture transcript_path on first invocation.
                if not self._transcript_path_captured and self._session_manager_ref:
                    transcript_path = inp.get("transcript_path")
                    if transcript_path and self.db_session_id:
                        try:
                            self._session_manager_ref.update(
                                self.db_session_id, transcript_path=str(transcript_path)
                            )
                            self._transcript_path_captured = True
                            logger.debug(
                                "Captured transcript_path for session %s: %s",
                                self.db_session_id[:8],
                                transcript_path,
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to capture transcript_path for session %s: %s",
                                self.db_session_id[:8],
                                e,
                                exc_info=True,
                            )

                data = {"prompt": inp.get("prompt", ""), "source": "claude"}
                resp = await cb(data)
                persist = self._additional_context_persist()
                output = _response_to_prompt_output(
                    resp if isinstance(resp, dict) else None, persist=persist
                )

                context_parts: list[tuple[str, str]] = []

                hook_specific = output.get("hookSpecificOutput")
                if hook_specific and isinstance(hook_specific, dict):
                    existing = hook_specific.get("additionalContext")
                    if existing:
                        context_parts.append(("hook_context", str(existing)))

                plan_ctx = getattr(self, "_consume_plan_mode_context", lambda: None)()
                if plan_ctx:
                    context_parts.append(("plan_mode", str(plan_ctx)))

                if context_parts:
                    output["hookSpecificOutput"] = UserPromptSubmitHookSpecificOutput(
                        hookEventName="UserPromptSubmit",
                        additionalContext=_bound_resp_context(
                            resp if isinstance(resp, dict) else None,
                            "\n\n".join(part for _, part in context_parts).strip(),
                            contributor_sizes={label: len(part) for label, part in context_parts},
                            persist=persist,
                        ),
                    )

                # Inject conversation history on first prompt of a recreated session.
                if self._needs_history_injection:
                    self._needs_history_injection = False
                    existing = ""
                    hook_specific = output.get("hookSpecificOutput")
                    if hook_specific and isinstance(hook_specific, dict):
                        existing = str(hook_specific.get("additionalContext", "") or "")
                    history_budget = (
                        additional_context_limit_for(SessionSource.CLAUDE) - len(existing) - 4
                    )
                    if history_budget > 500:
                        history_ctx = await cast(Any, self)._load_history_context(
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
                                additionalContext=_bound_resp_context(
                                    resp if isinstance(resp, dict) else None,
                                    combined,
                                    contributor_sizes={
                                        "existing": len(existing),
                                        "history": len(history_ctx),
                                    },
                                    persist=persist,
                                ),
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
                raw_tool_name = inp.get("tool_name", "")
                tool_name = raw_tool_name if isinstance(raw_tool_name, str) else ""
                tool_input = inp.get("tool_input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                data = {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }
                resp = await cb_pre(data)
                persist = self._additional_context_persist()
                if resp and resp.get("decision") == "block":
                    return _response_to_pre_tool_output(resp, persist=persist)

                effective_input = tool_input
                modified_input = resp.get("modified_input") if isinstance(resp, dict) else None
                if isinstance(modified_input, dict):
                    effective_input = modified_input

                session = cast(Any, self)
                approval_required = session._needs_tool_approval(tool_name, effective_input)
                permission = await session._resolve_tool_permission(
                    tool_name,
                    effective_input,
                    tool_use_id=(
                        tool_use_id
                        if isinstance(tool_use_id, str) and tool_use_id
                        else f"tool-{uuid4().hex}"
                    ),
                    invoke_pre_tool_callback=False,
                )

                if isinstance(permission, PermissionResultDeny):
                    deny_resp = dict(resp or {})
                    deny_resp["decision"] = "block"
                    deny_resp["reason"] = permission.message
                    return _response_to_pre_tool_output(deny_resp, persist=persist)

                allow_resp = dict(resp or {})
                updated_input = permission.updated_input
                if isinstance(updated_input, dict) and updated_input != tool_input:
                    allow_resp["modified_input"] = updated_input
                if approval_required:
                    allow_resp["auto_approve"] = True
                    if isinstance(tool_use_id, str) and tool_use_id:
                        self._preapproved_tool_use_ids.add(tool_use_id)
                return _response_to_pre_tool_output(allow_resp, persist=persist)

            hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[_pre_tool_hook])]

        if self._on_post_tool:
            cb_post = self._on_post_tool

            async def _post_tool_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                tool_name = inp.get("tool_name", "")
                tool_input = inp.get("tool_input", {})

                # Detect plan file writes in plan mode and broadcast to frontend.
                # Read is deliberately excluded: consulting an existing plan file
                # must not trigger the approval prompt.
                if (
                    tool_name in ("Write", "Edit")
                    and self.chat_mode == "plan"
                    and not self._plan_approved
                    and isinstance(tool_input, dict)
                ):
                    file_path = tool_input.get("file_path", "")
                    if isinstance(file_path, str) and _PLAN_FILE_PATTERN.match(file_path):
                        session = cast(Any, self)
                        plan_content = session._read_plan_file(file_path)
                        session._reset_plan_broadcast_if_revised(plan_content)
                        if plan_content and self._on_plan_ready and not self._plan_broadcast_sent:
                            session._remember_plan_artifact(
                                file_path=file_path,
                                content=plan_content,
                                allowed_prompts=tool_input.get("allowedPrompts"),
                            )
                            await self._on_plan_ready(plan_content, tool_input, tool_use_id)
                            self._plan_broadcast_sent = True
                            logger.info(
                                "Plan file written, broadcast plan_pending_approval for %s",
                                self.conversation_id[:8],
                            )

                data = {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_response": inp.get("tool_response"),
                }
                resp = await cb_post(data)
                return _response_to_post_tool_output(
                    resp, persist=self._additional_context_persist()
                )

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
                return _response_to_stop_output(resp, persist=self._additional_context_persist())

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
                return _response_to_compact_output(resp, persist=self._additional_context_persist())

            hooks["PreCompact"] = [HookMatcher(matcher=None, hooks=[_compact_hook])]

        if self._on_subagent_start:
            cb_sub_start = self._on_subagent_start

            async def _subagent_start_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                data = {
                    "session_id": inp.get("session_id", ""),
                    "source": "claude",
                }
                resp = await cb_sub_start(data)
                return _response_to_subagent_output(
                    resp, "SubagentStart", persist=self._additional_context_persist()
                )

            hooks["SubagentStart"] = [HookMatcher(matcher=None, hooks=[_subagent_start_hook])]

        if self._on_subagent_stop:
            cb_sub_stop = self._on_subagent_stop

            async def _subagent_stop_hook(
                inp: SDKHookInput,
                tool_use_id: str | None,
                ctx: HookContext,
            ) -> SyncHookJSONOutput:
                data = {
                    "session_id": inp.get("session_id", ""),
                    "source": "claude",
                }
                resp = await cb_sub_stop(data)
                return _response_to_subagent_output(
                    resp, "SubagentStop", persist=self._additional_context_persist()
                )

            hooks["SubagentStop"] = [HookMatcher(matcher=None, hooks=[_subagent_stop_hook])]

        return hooks if hooks else None
