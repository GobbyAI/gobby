"""Codex hooks.json adapter implementation."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.capabilities import (
    CODEX_EVENT_MAP,
    ContextChannel,
    get_provider_capabilities,
)
from gobby.adapters.codex_impl.execution_chain import (
    DIRECT_EXEC_NAMES,
    FUNCTIONS_EXEC_NAMES,
    extract_functions_exec_command,
    functions_exec_shell_input,
)
from gobby.adapters.codex_impl.lifecycle_events import annotate_codex_hook_event
from gobby.adapters.codex_impl.shared import (
    TOOL_MAP as SHARED_TOOL_MAP,
)
from gobby.adapters.degradation import (
    AdapterDegradationKind,
    persist_kwargs_from_hook_response,
    record_adapter_degradation,
    record_unsupported_response_fields,
    truncate_context_for_adapter,
)
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.checkout_reads import node_origin
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.sessions.reasoning_effort import observed_reasoning_effort
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.codex_items import normalize_command_execution

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)

_TRANSCRIPT_READ_CHUNK_BYTES = 64 * 1024


def _pending_shell_workdir(input_data: dict[str, Any]) -> str | None:
    """Borrow cwd only from one proven, still-pending provider shell call.

    Native Bash hooks omit nested exec_command.workdir. Reuse the transcript
    execution correlator, including yielded cells, instead of guessing from the
    session's task or the most recent matching historical command.
    """
    if node_origin():
        return None
    transcript_path = input_data.get("transcript_path")
    tool_input = input_data.get("tool_input")
    if not isinstance(transcript_path, str) or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command", tool_input.get("cmd"))
    if not isinstance(command, str):
        return None
    parser = CodexTranscriptParser()
    validated: dict[str, str | None] = {}
    try:
        with Path(transcript_path).open(encoding="utf-8") as transcript:
            for index, line in enumerate(transcript):
                if not line.endswith("\n"):
                    return None  # An incomplete provider record cannot prove the active call.
                json.loads(line)  # A malformed record could hide a competing pending call.
                parsed = parser.parse_line(line, index)
                if isinstance(parsed, ParsedMessage) and parsed.content_type == "tool_use":
                    arguments = parsed.tool_input or {}
                    if parsed.tool_use_id and (
                        parsed.tool_name in DIRECT_EXEC_NAMES
                        or (
                            parsed.tool_name in FUNCTIONS_EXEC_NAMES
                            and functions_exec_shell_input(arguments.get("raw")) is not None
                        )
                    ):
                        cwd = arguments.get("cwd")
                        validated[parsed.tool_use_id] = cwd if isinstance(cwd, str) else None
    except (OSError, UnicodeError, ValueError):
        return None
    chain = parser.snapshot_state()["execution_chain"]
    if not chain["complete"] or chain["ambiguous_cells"] or chain["ambiguous_sessions"]:
        return None
    pending = {
        entry["outer_call_id"]: entry
        for key in ("calls", "cells")
        for entry in chain[key].values()
        if entry.get("session_id") is None
    }
    if any(entry["literal_command"] is None for entry in pending.values()):
        return None
    matches = [entry for entry in pending.values() if entry["literal_command"] == command]
    if len(matches) != 1 or matches[0]["outer_call_id"] not in validated:
        return None
    match = matches[0]
    cwd = current_tool_cwd(
        {"cwd": validated[match["outer_call_id"]], "tool_input": {"workdir": match["workdir"]}}
    )
    return str(cwd) if cwd is not None else None


def _command_execution_result(input_data: dict[str, Any]) -> dict[str, Any] | None:
    """Recover the authoritative Codex command result for one hook payload."""
    if node_origin():
        return None
    transcript_path = input_data.get("transcript_path")
    tool_use_id = input_data.get("tool_use_id")
    if not isinstance(transcript_path, str) or not isinstance(tool_use_id, str):
        return None

    try:
        with Path(transcript_path).open("rb") as transcript:
            transcript.seek(0, 2)
            position = transcript.tell()
            pending = b""
            while position > 0:
                read_size = min(_TRANSCRIPT_READ_CHUNK_BYTES, position)
                position -= read_size
                transcript.seek(position)
                records = (transcript.read(read_size) + pending).split(b"\n")
                pending = records[0]
                for record in reversed(records[1:]):
                    result = _result_from_command_execution_record(record, tool_use_id)
                    if result is not None:
                        return result
            return _result_from_command_execution_record(pending, tool_use_id)
    except OSError:
        return None


def _result_from_command_execution_record(record: bytes, tool_use_id: str) -> dict[str, Any] | None:
    if not record.strip():
        return None
    try:
        data = json.loads(record)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("id") != tool_use_id:
        return None
    outcome = normalize_command_execution(item)
    if outcome is None or outcome.exit_code is None:
        return None
    return {
        "command": outcome.command,
        "cwd": outcome.cwd,
        "exit_code": outcome.exit_code,
        "success": outcome.success,
        "output": outcome.output,
        "outcome_provenance": "codex.event_msg.command_execution",
    }


class CodexHooksAdapter(BaseAdapter):
    """Adapter for Codex CLI hooks.json lifecycle events.

    Translates Codex hooks.json payloads to unified HookEvent format and
    converts HookResponse back to the event-specific JSON schema Codex expects
    on hook stdout.
    """

    @property
    def source(self) -> SessionSource:
        return SessionSource.CODEX

    # Event type mapping: Codex PascalCase hook names -> unified HookEventType
    EVENT_MAP: dict[str, HookEventType] = dict(CODEX_EVENT_MAP)

    # Hook events where context must be routed through top-level systemMessage.
    # These schemas do not support hookSpecificOutput.additionalContext.
    SYSTEM_MESSAGE_ONLY_EVENTS: set[str] = {
        "PreToolUse",
        "PermissionRequest",
        "PreCompact",
        "PostCompact",
        "Stop",
    }
    COMPACT_EVENTS: set[str] = {"PreCompact", "PostCompact"}

    def __init__(self, hook_manager: HookManager | None = None):
        self._hook_manager = hook_manager

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent | None:
        """Convert Codex hooks.json payload to HookEvent."""
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data") or {}

        event_type = self.EVENT_MAP.get(hook_type)
        if event_type is None:
            logger.warning("Codex hooks: unsupported hook type '%s'", hook_type)
            return None

        session_id = input_data.get("session_id", "")
        raw_tool_input = input_data.get("tool_input")
        shell_cwd_unproven = False

        from gobby.hooks.normalization import (
            is_shell_tool,
            normalize_tool_fields,
            normalize_tool_outcome,
        )

        if hook_type == "PreToolUse" and is_shell_tool(input_data.get("tool_name")):
            shell_input = input_data.get("tool_input")
            if not isinstance(shell_input, dict):
                shell_cwd_unproven = True
            elif not any(shell_input.get(key) for key in ("workdir", "cwd")):
                workdir = _pending_shell_workdir(input_data)
                if workdir is not None:
                    input_data = {
                        **input_data,
                        "tool_input": {**shell_input, "workdir": workdir},
                    }
                else:
                    shell_cwd_unproven = True
            elif current_tool_cwd(input_data) is None:
                shell_cwd_unproven = True

        if hook_type == "PostToolUse" and is_shell_tool(input_data.get("tool_name")):
            command_result = _command_execution_result(input_data)
            if command_result is not None:
                shell_input = dict(raw_tool_input) if isinstance(raw_tool_input, dict) else {}
                shell_input["command"] = command_result["command"]
                if command_result["cwd"] is not None:
                    shell_input["workdir"] = command_result["cwd"]
                input_data = {
                    **input_data,
                    "tool_input": shell_input,
                    "tool_result": command_result,
                }
                normalize_tool_outcome(
                    input_data,
                    explicit_success=command_result["exit_code"] == 0,
                    provenance="codex.event_msg.command_execution",
                )
                input_data["_tool_outcome_locked"] = True

        # Normalize event data (same as Claude — reuse shared normalization)
        normalized_data = normalize_tool_fields(dict(input_data))
        effort = observed_reasoning_effort(normalized_data)
        if effort is None:
            normalized_data.pop("effort", None)
        else:
            normalized_data["effort"] = effort
        raw_tool_name = normalized_data.get("tool_name")
        if isinstance(raw_tool_name, str):
            normalized_tool_name = SHARED_TOOL_MAP.get(raw_tool_name, raw_tool_name)
            if normalized_tool_name != raw_tool_name:
                normalized_data.setdefault("_original_tool_name", raw_tool_name)
                normalized_data["tool_name"] = normalized_tool_name

        # Check for failure on PostToolUse
        is_failure = normalized_data.get("is_error", False)
        metadata: dict[str, Any] = {"_native_hook_type": hook_type}
        if hook_type == "PreToolUse" and raw_tool_name in FUNCTIONS_EXEC_NAMES:
            arguments = input_data.get("arguments", raw_tool_input)
            if isinstance(arguments, dict):
                arguments = arguments.get("arguments")
            command = extract_functions_exec_command(arguments)
            if command is not None:
                wrapper = normalize_tool_fields(
                    {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": None}
                )
                metadata["_native_shell_wrapper_read_only"] = (
                    wrapper.get("canonical_read_only") is True
                )
        if shell_cwd_unproven:
            # The launcher's cwd cannot vouch for a script in a computed workdir.
            # Keep it for session lookup, but classify without it for the read-only proof.
            unscoped_input = dict(raw_tool_input) if isinstance(raw_tool_input, dict) else {}
            unscoped_input.pop("workdir", None)
            unscoped_input.pop("cwd", None)
            unscoped = normalize_tool_fields(
                {"tool_name": "Bash", "tool_input": unscoped_input, "cwd": None}
            )
            metadata["_native_shell_cwd_unproven"] = True
            metadata["_native_shell_read_only"] = unscoped.get("canonical_read_only") is True
        if is_failure:
            metadata["is_failure"] = True
        if isinstance(raw_tool_input, dict):
            metadata["raw_tool_input"] = copy.deepcopy(raw_tool_input)
        original_tool_name = normalized_data.pop("_original_tool_name", None)
        if original_tool_name:
            metadata["original_tool_name"] = original_tool_name
            metadata["normalized_tool_name"] = normalized_data.get("tool_name")
        self._copy_platform_session_metadata(native_event, metadata)

        event = HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=self._hook_event_timestamp(native_event),
            machine_id=input_data.get("machine_id"),
            cwd=input_data.get("cwd"),
            data=normalized_data,
            metadata=metadata,
        )
        return annotate_codex_hook_event(event, hook_type)

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to Codex hooks.json expected format."""
        hook_event_name = hook_type or "Unknown"
        capabilities = get_provider_capabilities(self.source)
        capability = capabilities.get_hook(hook_event_name)
        context_channel = capability.context_channel if capability else ContextChannel.NONE
        record_unsupported_response_fields(
            response,
            provider=self.source,
            hook_type=hook_type,
            capability=capability,
            event_logger=logger,
        )
        if hook_event_name == "Interrupt":
            if response.system_message:
                return {"systemMessage": response.system_message}
            return {}

        normalized_reason = (
            None
            if hook_event_name == "SubagentStart"
            else normalize_adapter_response_reason(
                response,
                adapter_name=self.__class__.__name__,
                hook_type=hook_type,
                logger=logger,
            )
        )

        result: dict[str, Any] = {"continue": True}

        if response.decision in ("deny", "block") and hook_event_name != "SubagentStart":
            if hook_event_name == "PermissionRequest":
                decision: dict[str, Any] = {"behavior": "deny"}
                if normalized_reason:
                    decision["message"] = normalized_reason
                result["hookSpecificOutput"] = {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }

            elif hook_event_name in self.COMPACT_EVENTS:
                return {
                    "continue": False,
                    "stopReason": normalized_reason or "Blocked by Gobby hook",
                }

            elif hook_event_name == "PreToolUse":
                deny_result: dict[str, Any] = {
                    "decision": "block",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                    },
                }
                if normalized_reason:
                    deny_result["reason"] = normalized_reason
                    deny_result["hookSpecificOutput"]["permissionDecisionReason"] = (
                        normalized_reason
                    )

                system_parts: list[tuple[str, str]] = []
                if response.system_message:
                    system_parts.append(("system_message", response.system_message))
                system_parts.extend(response.context_contributors())
                if system_parts:
                    if response.context:
                        record_adapter_degradation(
                            provider=self.source,
                            hook_type=hook_type,
                            kind=AdapterDegradationKind.REROUTED_FIELD,
                            response_field="context",
                            destination_channel=ContextChannel.SYSTEM_MESSAGE,
                            event_logger=logger,
                        )
                    deny_result["systemMessage"] = truncate_context_for_adapter(
                        system_parts,
                        provider=self.source,
                        hook_type=hook_type,
                        destination_channel=ContextChannel.SYSTEM_MESSAGE,
                        event_logger=logger,
                        **persist_kwargs_from_hook_response(response, self._hook_manager),
                    )
                return deny_result

            elif hook_event_name == "SubagentStop":
                return {
                    "continue": True,
                    "decision": "block",
                    "reason": normalized_reason or "Blocked by Gobby hook",
                }

            else:
                block_result: dict[str, Any] = {"continue": False, "decision": "block"}
                if normalized_reason:
                    block_result["reason"] = normalized_reason
                return block_result

        if hook_event_name == "PermissionRequest" and "hookSpecificOutput" not in result:
            behavior = response.permission_decision
            if behavior is None and response.auto_approve:
                behavior = "allow"
            if behavior is not None:
                result["hookSpecificOutput"] = {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": behavior},
                }

        if hook_event_name == "PreToolUse":
            permission_decision = response.permission_decision
            if permission_decision is None and response.auto_approve:
                permission_decision = "allow"
            updated_input = (
                response.modified_input if isinstance(response.modified_input, dict) else None
            )
            if updated_input is not None and permission_decision is None:
                # Codex accepts ``updatedInput`` only next to
                # ``permissionDecision: "allow"``; every other shape is reported
                # as a failed hook and the original input runs. A rewrite on its
                # own therefore carries the approval; an explicit deny keeps its
                # decision and discards the rewrite.
                permission_decision = "allow"
            if permission_decision is not None:
                hook_output = result.setdefault(
                    "hookSpecificOutput",
                    {"hookEventName": "PreToolUse"},
                )
                hook_output["permissionDecision"] = permission_decision
                if updated_input is not None and permission_decision == "allow":
                    hook_output["updatedInput"] = updated_input

        # Build additionalContext from all context sources. Keep high-value
        # session/system context ahead of large workflow payloads.
        context_parts: list[tuple[str, str]] = []

        start_hook = hook_event_name in {"SessionStart", "SubagentStart"}

        # Route system_message by event type:
        # - systemMessage-only events: visible systemMessage
        # - SessionStart, SubagentStart: startup context only via additionalContext
        # - UserPromptSubmit, PostToolUse: additionalContext only (hidden from user)
        if response.system_message:
            if context_channel is ContextChannel.SYSTEM_MESSAGE:
                result["systemMessage"] = response.system_message
            else:
                # Always feed to model via additionalContext
                context_parts.insert(0, ("system_message", response.system_message))

        # Session metadata (Gobby session ID, terminal context, etc.)
        if response.metadata:
            gobby_session_id = response.metadata.get("session_id")

            if gobby_session_id:
                context_lines = build_first_hook_session_metadata_lines(
                    response.metadata,
                    include_session_id_line=not (
                        start_hook and system_message_has_session_banner(response.system_message)
                    ),
                    include_tty=False,
                )
                if context_lines:
                    context_parts.append(("metadata", "\n".join(context_lines)))

        # Workflow-injected context (inject_context action). This can be large,
        # so place it after session-critical context before applying the budget.
        context_parts.extend(response.context_contributors())

        # Build hookSpecificOutput or systemMessage based on event type.
        if context_parts:
            combined_context = truncate_context_for_adapter(
                context_parts,
                provider=self.source,
                hook_type=hook_type,
                destination_channel=context_channel,
                event_logger=logger,
                **persist_kwargs_from_hook_response(response, self._hook_manager),
            )
            if context_channel is ContextChannel.SYSTEM_MESSAGE:
                if response.context:
                    record_adapter_degradation(
                        provider=self.source,
                        hook_type=hook_type,
                        kind=AdapterDegradationKind.REROUTED_FIELD,
                        response_field="context",
                        destination_channel=ContextChannel.SYSTEM_MESSAGE,
                        event_logger=logger,
                    )
                # Append to existing systemMessage (from system_message routing above)
                # instead of overwriting it.
                if "systemMessage" in result:
                    result["systemMessage"] += "\n\n" + combined_context
                else:
                    result["systemMessage"] = combined_context
            elif context_channel is ContextChannel.ADDITIONAL_CONTEXT:
                hook_specific = result.get("hookSpecificOutput")
                if not isinstance(hook_specific, dict):
                    hook_specific = {"hookEventName": hook_event_name}
                    result["hookSpecificOutput"] = hook_specific
                else:
                    hook_specific.setdefault("hookEventName", hook_event_name)
                hook_specific["additionalContext"] = combined_context

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: HookManager
    ) -> dict[str, Any]:
        """Process Codex hooks.json event."""
        hook_event = self.translate_to_hook_event(native_event)
        if hook_event is None:
            return {}

        hook_type = native_event.get("hook_type", "")
        self._hook_manager = hook_manager
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)


__all__ = ["CodexHooksAdapter"]
