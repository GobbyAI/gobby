"""Codex hooks.json adapter implementation."""

from __future__ import annotations

import copy
import json
import logging
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

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
    functions_exec_shell_input,
)
from gobby.adapters.codex_impl.lifecycle_events import annotate_codex_hook_event
from gobby.adapters.codex_impl.literal_shell_calls import literal_exec_calls, literal_shell_calls
from gobby.adapters.codex_impl.native_exec_failures import is_simple_native_exec_cell
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
from gobby.hooks.shell_execution import literal_shell_workdir
from gobby.sessions.reasoning_effort import observed_reasoning_effort
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.codex_items import normalize_command_execution

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)

_TRANSCRIPT_READ_CHUNK_BYTES = 64 * 1024


def _node_shell_workdir(input_data: dict[str, Any]) -> str | None:
    """Prove a node command's directory without accessing its provider transcript."""
    tool_input = input_data.get("tool_input")
    command = (
        tool_input.get("command", tool_input.get("cmd")) if isinstance(tool_input, dict) else None
    )
    return literal_shell_workdir(command) if isinstance(command, str) else None


# A pending call's record can still be mid-write, or not yet flushed, when the hook reads the
# rollout. Re-reading a few times covers that window; a longer gap stays refused.
_PENDING_RECORD_RETRIES = 2
_PENDING_RECORD_RETRY_DELAY_SECONDS = 0.2


class _PendingLookup(NamedTuple):
    """One snapshot of the rollout: a proven workdir, or the guard that refused it."""

    workdir: str | None
    refusal: str = ""
    # Only an unterminated tail or a missing match can be completed by a later write.
    retryable: bool = False
    outer_call_id: str | None = None


def _pending_shell_lookup(transcript_path: str, command: str) -> _PendingLookup:
    """Read the rollout once and prove the pending call that launched ``command``."""
    parser = CodexTranscriptParser()
    validated: dict[str, str | None] = {}
    batches: dict[str, list[dict[str, Any]]] = {}
    simple_cells: set[str] = set()
    try:
        with Path(transcript_path).open(encoding="utf-8") as transcript:
            for index, line in enumerate(transcript):
                if not line.endswith("\n"):
                    # An incomplete provider record cannot prove the active call.
                    return _PendingLookup(
                        None, f"incomplete provider record at line {index}", retryable=True
                    )
                json.loads(line)  # A malformed record could hide a competing pending call.
                parsed = parser.parse_line(line, index)
                if isinstance(parsed, ParsedMessage) and parsed.content_type == "tool_use":
                    arguments = parsed.tool_input or {}
                    if parsed.tool_use_id and parsed.tool_name in FUNCTIONS_EXEC_NAMES:
                        if is_simple_native_exec_cell(arguments.get("raw")):
                            simple_cells.add(parsed.tool_use_id)
                        calls = literal_exec_calls(arguments.get("raw"))
                        if calls is not None and len(calls) > 1:
                            # Attribution reads every member; only absolute workdirs prove cwd.
                            batches[parsed.tool_use_id] = calls
                            if literal_shell_calls(arguments.get("raw")) is not None:
                                cwd = arguments.get("cwd")
                                validated[parsed.tool_use_id] = (
                                    cwd if isinstance(cwd, str) else None
                                )
                    if parsed.tool_use_id and (
                        parsed.tool_name in DIRECT_EXEC_NAMES
                        or (
                            parsed.tool_name in FUNCTIONS_EXEC_NAMES
                            and functions_exec_shell_input(arguments.get("raw")) is not None
                        )
                    ):
                        cwd = arguments.get("cwd")
                        validated[parsed.tool_use_id] = cwd if isinstance(cwd, str) else None
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        return _PendingLookup(None, f"rollout unreadable or malformed: {exc}")
    chain = parser.snapshot_state()["execution_chain"]
    # Process IDs describe commands that already started. Losing old process
    # receipts must not poison a new command's cwd proof; unresolved calls and
    # yielded cells can still launch it, so their overflow remains fail closed.
    if not chain["pending_complete"] or chain["ambiguous_cells"]:
        return _PendingLookup(None, "pending calls overflowed or are ambiguous")
    pending = {
        entry["outer_call_id"]: entry
        for key in ("calls", "cells")
        for entry in chain[key].values()
        if entry.get("session_id") is None
    }
    # Typed input reached a shell this chain could not attribute: its pending cell may run it.
    sticky = set(chain["sticky_unattributed"])
    if any(entry["outer_call_id"] in sticky for entry in pending.values()):
        return _PendingLookup(
            None, "typed input reached an unmapped session; a pending cell is unattributed"
        )
    invocations = [
        {**entry, "literal_command": call["cmd"], "workdir": call.get("workdir")}
        for entry in pending.values()
        for call in batches.get(
            entry["outer_call_id"], [{"cmd": entry["literal_command"], "workdir": entry["workdir"]}]
        )
    ]
    if any(entry["literal_command"] is None for entry in invocations):
        return _PendingLookup(None, "unattributed pending call blocks every proof")
    matches = [entry for entry in invocations if entry["literal_command"] == command]
    if len(matches) != 1:
        return _PendingLookup(
            None, f"{len(matches)} pending calls match the command", retryable=not matches
        )
    match = matches[0]
    if match["outer_call_id"] not in validated:
        return _PendingLookup(None, "matched call is not a validated shell call")
    cwd = current_tool_cwd(
        {"cwd": validated[match["outer_call_id"]], "tool_input": {"workdir": match["workdir"]}}
    )
    if cwd is None:
        return _PendingLookup(None, "matched call has no proven cwd")
    outer = match["outer_call_id"]
    # A yielded or multi-command cell can no longer vouch for one inner invocation.
    unique_outer = outer if outer in chain["calls"] and outer in simple_cells else None
    return _PendingLookup(str(cwd), outer_call_id=unique_outer)


_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PLAIN_WORD = re.compile(r"[A-Za-z0-9_.+-]+")


def _redacted_command_head(command: str) -> tuple[str, int]:
    """Name the executable and count argv for diagnostics, never argument or env values."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return "<unparsed>", len(command.split())
    words = [word for word in argv if not _ENV_ASSIGNMENT.match(word)]
    head = words[0].rsplit("/", 1)[-1] if words else ""
    return (head if _PLAIN_WORD.fullmatch(head) else "<redacted>"), len(argv)


def _pending_shell_workdir(
    input_data: dict[str, Any], *, correlation: dict[str, str] | None = None
) -> str | None:
    """Borrow cwd only from one proven, still-pending provider shell call.

    Native Bash hooks omit nested exec_command.workdir. Reuse the transcript
    execution correlator, including yielded cells, instead of guessing from the
    session's task or the most recent matching historical command.
    """
    if node_origin():
        workdir = _node_shell_workdir(input_data)
        if workdir is None:
            logger.warning("cwd proof refused: node command is not a proven literal cd")
        return workdir
    transcript_path = input_data.get("transcript_path")
    tool_input = input_data.get("tool_input")
    if not isinstance(transcript_path, str) or not isinstance(tool_input, dict):
        logger.warning("cwd proof refused: transcript path or tool input is missing")
        return None
    command = tool_input.get("command", tool_input.get("cmd"))
    if not isinstance(command, str):
        logger.warning("cwd proof refused: command is not text")
        return None
    lookup = _pending_shell_lookup(transcript_path, command)
    for _ in range(_PENDING_RECORD_RETRIES):
        if lookup.workdir is not None or not lookup.retryable:
            break
        time.sleep(_PENDING_RECORD_RETRY_DELAY_SECONDS)
        lookup = _pending_shell_lookup(transcript_path, command)
    if lookup.workdir is None:
        head, argc = _redacted_command_head(command)
        logger.warning(
            "cwd proof refused: %s (session=%s head=%s argc=%d)",
            lookup.refusal,
            input_data.get("session_id"),
            head,
            argc,
        )
    elif correlation is not None and lookup.outer_call_id is not None:
        explicit = any(tool_input.get(key) for key in ("workdir", "cwd"))
        if not explicit or current_tool_cwd(input_data) == Path(lookup.workdir):
            correlation["outer_call_id"] = lookup.outer_call_id
    return lookup.workdir


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
        shell_correlation: dict[str, str] = {}

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
                workdir = _pending_shell_workdir(input_data, correlation=shell_correlation)
                if workdir is not None:
                    input_data = {
                        **input_data,
                        "tool_input": {**shell_input, "workdir": workdir},
                    }
                else:
                    shell_cwd_unproven = True
            elif current_tool_cwd(input_data) is None:
                shell_cwd_unproven = True
            elif not node_origin():
                path = input_data.get("transcript_path")
                command = shell_input.get("command", shell_input.get("cmd"))
                if isinstance(path, str) and isinstance(command, str):
                    lookup = _pending_shell_lookup(path, command)
                    if (
                        lookup.workdir is not None
                        and lookup.outer_call_id is not None
                        and current_tool_cwd(input_data) == Path(lookup.workdir)
                    ):
                        shell_correlation["outer_call_id"] = lookup.outer_call_id

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
        if shell_correlation:
            metadata["_native_shell_outer_call_id"] = shell_correlation["outer_call_id"]
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
