"""Shared Codex shell-execution correlation for live events and transcripts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.tool_outcomes import ToolOutcome, ToolOutcomeStatus

FUNCTIONS_EXEC_NAMES = frozenset({"exec", "functions.exec"})
DIRECT_EXEC_NAMES = frozenset({"exec_command", "functions.exec_command"})
WAIT_NAMES = frozenset({"wait", "functions.wait"})
WRITE_STDIN_NAMES = frozenset({"write_stdin", "functions.write_stdin"})

_DIRECT_EXEC_COMPLETION_RE = re.compile(
    r"\AChunk ID: [^\r\n]+\r?\n"
    r"Wall time: [^\r\n]+\r?\n"
    r"Process exited with code (?P<exit_code>-?\d+)\r?\n"
)
_DIRECT_EXEC_RUNNING_RE = re.compile(
    r"\AChunk ID: [^\r\n]+\r?\n"
    r"Wall time: [^\r\n]+\r?\n"
    r"Process running with session ID (?P<session_id>\d+)\r?\n"
)
_EXEC_COMMAND_CALL_RE = re.compile(r"\btools\.exec_command\s*\(")
_EXEC_COMMAND_LITERAL_RE = re.compile(
    r'(?:^|[{,])\s*cmd\s*:\s*("(?:\\.|[^"\\])*")',
    re.DOTALL,
)
_REPEATED_EXEC_SCAFFOLD_RE = re.compile(
    r"\b(?:do|for|while)\b|\.(?:forEach|map|reduce)\s*\(|\bPromise\.all\s*\("
)
_WRITE_STDIN_CALL_RE = re.compile(r"\btools\.write_stdin\s*\(")
_WRITE_STDIN_SESSION_RE = re.compile(r"\bsession_id\s*:\s*(\"(?:\\.|[^\"\\])*\"|-?\d+)")
_YIELDED_CELL_RE = re.compile(r"^Script running with cell ID ([A-Za-z0-9._:-]+)\s*$")


def extract_functions_exec_command(arguments: Any) -> str | None:
    """Extract one literal nested ``exec_command`` command, failing closed."""
    if isinstance(arguments, dict):
        command = arguments.get("cmd")
        return command if isinstance(command, str) and command else None
    if not isinstance(arguments, str):
        return None
    if len(_EXEC_COMMAND_CALL_RE.findall(arguments)) != 1:
        return None
    matches = _EXEC_COMMAND_LITERAL_RE.findall(arguments)
    if len(matches) != 1:
        return None
    scaffold = arguments.replace(matches[0], '""', 1)
    if _REPEATED_EXEC_SCAFFOLD_RE.search(scaffold):
        return None
    try:
        command = json.loads(matches[0])
    except (TypeError, ValueError):
        return None
    return command if isinstance(command, str) and command else None


def validate_functions_exec_wrapper(arguments: Any) -> str | None:
    """Reject ambiguous nested shell wrappers before they execute."""
    if isinstance(arguments, dict) and "arguments" in arguments:
        return validate_functions_exec_wrapper(arguments["arguments"])
    if not isinstance(arguments, str):
        return None
    call_count = len(_EXEC_COMMAND_CALL_RE.findall(arguments))
    if call_count == 0:
        return None
    if call_count > 1:
        return (
            "functions.exec shell wrappers may contain exactly one static exec_command call; "
            "run batched validations as separate native commands"
        )
    if extract_functions_exec_command(arguments) is None:
        return (
            "functions.exec shell wrapper command must be one statically attributable literal "
            "exec_command call"
        )
    return None


def extract_direct_exec_command(arguments: Any) -> str | None:
    """Extract the command from one direct Codex ``exec_command`` call."""
    decoded = arguments
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    if not isinstance(decoded, dict):
        return None
    command = decoded.get("cmd")
    return command if isinstance(command, str) and command else None


def _normalize_session_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def extract_functions_write_stdin_session_id(arguments: Any) -> str | None:
    """Extract one literal nested or direct write_stdin session ID."""
    if isinstance(arguments, dict):
        return _normalize_session_id(arguments.get("session_id"))
    if not isinstance(arguments, str):
        return None
    if len(_WRITE_STDIN_CALL_RE.findall(arguments)) != 1:
        return None
    matches = _WRITE_STDIN_SESSION_RE.findall(arguments)
    if len(matches) != 1:
        return None
    try:
        return _normalize_session_id(json.loads(matches[0]))
    except (TypeError, ValueError):
        return None


def extract_direct_write_stdin_session_id(arguments: Any) -> str | None:
    """Extract a session ID from a native direct write_stdin argument object."""
    decoded = arguments
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    if not isinstance(decoded, dict):
        return None
    return _normalize_session_id(decoded.get("session_id"))


def _iter_output_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    result: list[str] = []
    if isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                result.append(block["text"])
            elif isinstance(block, (dict, list, str)):
                result.extend(_iter_output_text(block))
        return result
    if isinstance(value, dict):
        for key in ("content", "contentItems", "output"):
            nested = value.get(key)
            if isinstance(nested, (dict, list, str)):
                result.extend(_iter_output_text(nested))
    return result


def decoded_exec_results(value: Any) -> list[dict[str, Any]]:
    """Decode exact structured result objects without reading prose stdout as outcome."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, list):
        results: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                results.extend(decoded_exec_results(item["text"]))
            elif isinstance(item, (dict, list, str)):
                results.extend(decoded_exec_results(item))
        return results
    if not isinstance(value, dict):
        return []
    has_exit = any(
        isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
        for key in ("exit_code", "exitCode", "returncode")
    )
    has_boolean = any(
        isinstance(value.get(key), bool) for key in ("success", "isError", "is_error")
    )
    has_session = exec_session_id(value) is not None
    if has_exit or has_boolean or has_session:
        return [value]
    for wrapper_key in ("content", "contentItems", "output"):
        wrapped = value.get(wrapper_key)
        if isinstance(wrapped, (dict, list, str)):
            nested = decoded_exec_results(wrapped)
            if nested:
                return nested
    return []


def definitive_exit_code(result: Mapping[str, Any]) -> int | None:
    values = [result[key] for key in ("exit_code", "exitCode", "returncode") if key in result]
    if not values or any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values) else None


def _has_structured_outcome(result: Mapping[str, Any]) -> bool:
    if definitive_exit_code(result) is not None:
        return True
    values = [result[key] for key in ("success", "isError", "is_error") if key in result]
    return bool(values) and all(isinstance(value, bool) for value in values)


def _canonical_tool_outcome(
    result: Mapping[str, Any],
    *,
    provenance: str,
) -> ToolOutcome:
    exit_code = definitive_exit_code(result)
    if exit_code is not None:
        status = ToolOutcomeStatus.SUCCEEDED if exit_code == 0 else ToolOutcomeStatus.FAILED
        return ToolOutcome(status, exit_code=exit_code, provenance=provenance)
    success = result.get("success")
    if isinstance(success, bool):
        status = ToolOutcomeStatus.SUCCEEDED if success else ToolOutcomeStatus.FAILED
        return ToolOutcome(status, provenance=provenance)
    return ToolOutcome(ToolOutcomeStatus.UNKNOWN, provenance=provenance)


def exec_session_id(result: Mapping[str, Any]) -> str | None:
    return _normalize_session_id(result.get("session_id", result.get("sessionId")))


def explicit_result_command(result: Mapping[str, Any]) -> str | None:
    values = [result[key] for key in ("cmd", "command") if key in result]
    if not values or any(not isinstance(value, str) or not value for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values) else None


def extract_direct_exec_terminal_result(value: Any) -> dict[str, Any] | None:
    """Read one exact terminal result from Codex's native exec envelope."""
    matches: list[dict[str, Any]] = []
    for text in _iter_output_text(value):
        match = _DIRECT_EXEC_COMPLETION_RE.match(text)
        if match is not None:
            matches.append(
                {
                    "exit_code": int(match.group("exit_code")),
                    "output": text[match.end() :],
                }
            )
    return matches[0] if len(matches) == 1 else None


def extract_direct_exec_running_session_id(value: Any) -> str | None:
    """Read one exact pending session ID from Codex's native exec envelope."""
    matches: list[str] = []
    for text in _iter_output_text(value):
        match = _DIRECT_EXEC_RUNNING_RE.match(text)
        if match is not None:
            matches.append(match.group("session_id"))
    return matches[0] if len(matches) == 1 else None


def extract_exec_session_id(data: Mapping[str, Any]) -> str | None:
    """Read one structured PTY session ID from a live wrapper result."""
    output = data.get("tool_output", data.get("tool_response"))
    for result in decoded_exec_results(output):
        session_id = exec_session_id(result)
        if session_id is not None:
            return session_id
    return None


def extract_yielded_cell_id(data: Mapping[str, Any]) -> str | None:
    """Read the functions wrapper correlation token without inferring outcome."""
    output = data.get("tool_output", data.get("tool_response"))
    for text in _iter_output_text(output):
        for line in text.splitlines():
            first_nonblank = line.strip()
            if not first_nonblank:
                continue
            match = _YIELDED_CELL_RE.fullmatch(first_nonblank)
            return match.group(1) if match else None
    return None


def extract_wait_cell_id(data: Mapping[str, Any]) -> str | None:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    return _normalize_session_id(tool_input.get("cell_id"))


@dataclass(frozen=True)
class PendingExecution:
    """One original shell execution carried through wait and stdin polling."""

    outer_call_id: str
    literal_command: str | None = None
    cell_id: str | None = None
    session_id: str | None = None
    direct: bool = False

    def to_state(self) -> dict[str, Any]:
        return {
            "outer_call_id": self.outer_call_id,
            "literal_command": self.literal_command,
            "cell_id": self.cell_id,
            "session_id": self.session_id,
            "direct": self.direct,
        }

    @classmethod
    def from_state(cls, value: Any) -> PendingExecution | None:
        if not isinstance(value, dict):
            return None
        outer_call_id = value.get("outer_call_id")
        if not isinstance(outer_call_id, str) or not outer_call_id:
            return None
        optional = {key: value.get(key) for key in ("literal_command", "cell_id", "session_id")}
        if any(item is not None and not isinstance(item, str) for item in optional.values()):
            return None
        direct = value.get("direct", False)
        if not isinstance(direct, bool):
            return None
        return cls(outer_call_id=outer_call_id, direct=direct, **optional)


@dataclass(frozen=True)
class ExecutionResolution:
    """Result of correlating one completed Codex tool-call output."""

    state: Literal["unrelated", "pending", "terminal", "unknown"]
    execution: PendingExecution | None = None
    results: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


class ExecutionChainCorrelator:
    """One state machine shared by live notification and transcript reconciliation."""

    def __init__(self, max_pending: int = 64) -> None:
        self._max_pending = max_pending
        self._calls: dict[str, PendingExecution] = {}
        self._cells: dict[str, PendingExecution] = {}
        self._sessions: dict[str, PendingExecution] = {}
        self._ambiguous_cells: set[str] = set()
        self._ambiguous_sessions: set[str] = set()
        self._live_sequence = 0

    def register_call(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        *,
        allow_unattributed: bool = False,
    ) -> bool:
        execution: PendingExecution | None = None
        if name in DIRECT_EXEC_NAMES:
            command = extract_direct_exec_command(arguments)
            if command is not None:
                execution = PendingExecution(call_id, command, direct=True)
        elif name in FUNCTIONS_EXEC_NAMES:
            command = extract_functions_exec_command(arguments)
            session_id = extract_functions_write_stdin_session_id(arguments)
            if command is not None:
                execution = PendingExecution(call_id, command)
            elif session_id is not None:
                execution = self._sessions.get(session_id)
            elif allow_unattributed:
                execution = PendingExecution(call_id)
        elif name in WRITE_STDIN_NAMES:
            session_id = extract_direct_write_stdin_session_id(arguments)
            if session_id is not None and session_id not in self._ambiguous_sessions:
                execution = self._sessions.get(session_id)
        elif name in WAIT_NAMES:
            decoded = arguments
            if isinstance(arguments, str):
                try:
                    decoded = json.loads(arguments)
                except json.JSONDecodeError:
                    decoded = {}
            if isinstance(decoded, dict):
                cell_id = _normalize_session_id(decoded.get("cell_id"))
                if cell_id is not None and cell_id not in self._ambiguous_cells:
                    execution = self._cells.get(cell_id)
        if execution is None:
            return False
        self._set_pending(self._calls, call_id, execution)
        return True

    def resolve_output(self, call_id: str, output: Any) -> ExecutionResolution:
        execution = self._calls.pop(call_id, None)
        if execution is None:
            return ExecutionResolution("unrelated")
        yielded_cell = extract_yielded_cell_id({"tool_output": output})
        if yielded_cell is not None:
            pending = replace(execution, cell_id=yielded_cell)
            self._set_pending(self._cells, yielded_cell, pending)
            return ExecutionResolution("pending", execution=pending)

        running_session = extract_direct_exec_running_session_id(output)
        if running_session is not None:
            pending = replace(execution, session_id=running_session)
            self._set_pending(self._sessions, running_session, pending)
            return ExecutionResolution("pending", execution=pending)

        native_terminal = extract_direct_exec_terminal_result(output)
        if native_terminal is not None:
            results = [native_terminal]
        elif execution.direct:
            results = []
        else:
            results = decoded_exec_results(output)
        terminal_results = tuple(result for result in results if _has_structured_outcome(result))
        if terminal_results:
            self._clear_execution(execution)
            return ExecutionResolution(
                "terminal",
                execution=execution,
                results=terminal_results,
            )

        session_ids = {
            session_id for result in results if (session_id := exec_session_id(result)) is not None
        }
        if len(results) == 1 and len(session_ids) == 1:
            session_id = next(iter(session_ids))
            pending = replace(execution, session_id=session_id)
            self._set_pending(self._sessions, session_id, pending)
            return ExecutionResolution("pending", execution=pending)

        self._clear_execution(execution)
        return ExecutionResolution(
            "unknown",
            execution=execution,
            reason="terminal_result_missing_structured_outcome",
        )

    def correlate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Correlate one live completed item and promote terminal results to Bash."""
        name = str(data.get("_original_tool_name") or data.get("tool_name") or "")
        arguments = data.get("arguments", data.get("tool_input"))
        command = data.pop("_dynamic_exec_command", None)
        if isinstance(command, str) and command:
            arguments = {"cmd": command}
        self._live_sequence += 1
        call_id = str(
            data.get("callId")
            or data.get("call_id")
            or data.get("toolUseId")
            or data.get("tool_use_id")
            or data.get("item_id")
            or data.get("itemId")
            or data.get("id")
            or f"live:{self._live_sequence}"
        )
        if not self.register_call(call_id, name, arguments):
            return data
        output = data.pop("_direct_exec_native_output", None)
        if output is None:
            output = data.get("tool_output", data.get("tool_response"))
        resolution = self.resolve_output(call_id, output)
        execution = resolution.execution
        if execution is None:
            return data
        data["_original_tool_name"] = name
        data["tool_name"] = "Bash"
        data["tool_input"] = {"command": execution.literal_command}
        data["verification_execution_id"] = execution.outer_call_id
        if resolution.state == "pending":
            data["_verification_pending"] = True
            data["tool_result"] = {
                "success": None,
                "outcome_provenance": "codex.execution_chain.pending",
            }
        elif resolution.state == "terminal" and len(resolution.results) == 1:
            data["tool_result"] = dict(resolution.results[0])
        else:
            data["tool_result"] = {
                "success": None,
                "outcome_provenance": "codex.execution_chain",
                "unknown_reason": resolution.reason or "ambiguous_terminal_results",
            }
        data["tool_output"] = data["tool_result"]
        data.pop("tool_response", None)
        data["tool_outcome"] = _canonical_tool_outcome(
            data["tool_result"],
            provenance=(
                "codex.execution_chain.pending"
                if resolution.state == "pending"
                else "codex.execution_chain"
            ),
        ).to_dict()
        data["_tool_outcome_locked"] = True
        normalize_tool_fields(data)
        return data

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "calls": {key: value.to_state() for key, value in self._calls.items()},
            "cells": {key: value.to_state() for key, value in self._cells.items()},
            "sessions": {key: value.to_state() for key, value in self._sessions.items()},
            "ambiguous_cells": sorted(self._ambiguous_cells),
            "ambiguous_sessions": sorted(self._ambiguous_sessions),
        }

    def hydrate_state(self, state: Mapping[str, Any]) -> None:
        self._calls = self._hydrate_map(state.get("calls"))
        self._cells = self._hydrate_map(state.get("cells"))
        self._sessions = self._hydrate_map(state.get("sessions"))
        self._ambiguous_cells = self._hydrate_ambiguities(state.get("ambiguous_cells"))
        self._ambiguous_sessions = self._hydrate_ambiguities(state.get("ambiguous_sessions"))

    def _hydrate_map(self, value: Any) -> dict[str, PendingExecution]:
        if not isinstance(value, dict):
            return {}
        hydrated: dict[str, PendingExecution] = {}
        for key, item in value.items():
            execution = PendingExecution.from_state(item)
            if isinstance(key, str) and key and execution is not None:
                hydrated[key] = execution
            if len(hydrated) >= self._max_pending:
                break
        return hydrated

    def _set_pending(
        self,
        pending: dict[str, PendingExecution],
        key: str,
        execution: PendingExecution,
    ) -> None:
        ambiguous = (
            self._ambiguous_cells
            if pending is self._cells
            else self._ambiguous_sessions
            if pending is self._sessions
            else None
        )
        existing = pending.get(key)
        if (
            ambiguous is not None
            and existing is not None
            and (
                existing.outer_call_id != execution.outer_call_id
                or existing.literal_command != execution.literal_command
            )
        ):
            pending.pop(key, None)
            if len(ambiguous) >= self._max_pending and key not in ambiguous:
                ambiguous.pop()
            ambiguous.add(key)
            return
        if ambiguous is not None and key in ambiguous:
            return
        if key not in pending and len(pending) >= self._max_pending:
            pending.pop(next(iter(pending)))
        pending[key] = execution

    def _hydrate_ambiguities(self, value: Any) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {item for item in value[: self._max_pending] if isinstance(item, str) and item}

    def _clear_execution(self, execution: PendingExecution) -> None:
        if execution.cell_id is not None:
            self._cells.pop(execution.cell_id, None)
        if execution.session_id is not None:
            self._sessions.pop(execution.session_id, None)


DynamicExecCorrelator = ExecutionChainCorrelator
