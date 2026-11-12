"""Persist tool-start identity separately from provider result delivery."""

from collections.abc import Mapping
from typing import Any

from gobby.adapters.codex_impl.execution_chain import (
    DIRECT_EXEC_NAMES,
    FUNCTIONS_EXEC_NAMES,
    WAIT_NAMES,
    WRITE_STDIN_NAMES,
    decoded_exec_results,
    definitive_exit_code,
    exec_session_id,
    extract_direct_exec_running_session_id,
    extract_direct_exec_terminal_result,
    extract_direct_write_stdin_session_id,
    extract_functions_exec_command,
    extract_functions_write_stdin_session_id,
    extract_wait_cell_id,
    extract_yielded_cell_id,
    structured_exec_results,
)
from gobby.adapters.codex_impl.native_exec_failures import failed_native_outer_calls
from gobby.hooks.checkout_reads import node_origin
from gobby.hooks.events import HookEvent, HookEventType, SessionSource, tool_execution_id
from gobby.tasks.transcript_background import background_job_receipt
from gobby.workflows.git_operation_ownership import OPERATIONS_VARIABLE, retire_operation_calls
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import task_selected_at


def _call_key(event: HookEvent) -> str | None:
    call_id = tool_execution_id(event)
    return f"{event.source.value}:{call_id}" if call_id else None


def _bindings(variables: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = variables.get("task_tool_bindings")
    if not isinstance(raw, dict):
        return {}
    return {key: dict(value) for key, value in raw.items() if isinstance(value, dict)}


def pending_fencing_calls(variables: Mapping[str, Any], task_id: str | None) -> list[str]:
    """Return running calls bound to a task other than ``task_id`` that fence a switch.

    ``task_id`` None means the target is unknown, so every bound fencing call counts.
    Rules decide whether these calls refuse a selection; this only reports them.
    """
    if task_id is not None and variables.get("active_task_id") == task_id:
        return []
    calls = variables.get("task_tool_bindings")
    if not isinstance(calls, Mapping):
        return []
    return sorted(
        str(key)
        for key, call in calls.items()
        if isinstance(call, Mapping)
        and call.get("pending") is True
        and call.get("task_id") is not None
        and call.get("task_id") != task_id
        and call.get("fences_switch") is True
    )


def _fences_switch(event: HookEvent) -> bool:
    name = event.data.get("tool_name", "")
    if name in {"Bash", "Shell", "Task", "Agent"} or name in DIRECT_EXEC_NAMES:
        return True
    if name in FUNCTIONS_EXEC_NAMES:
        arguments = event.data.get("arguments", event.data.get("tool_input"))
        return extract_functions_exec_command(arguments) is not None
    return event.data.get("canonical_tool_kind") == "shell"


def _continuation_alias(event: HookEvent) -> str | None:
    name = event.data.get("_original_tool_name", event.data.get("tool_name", ""))
    arguments = event.data.get("arguments", event.data.get("tool_input"))
    token = None
    kind = "session"
    if name in WRITE_STDIN_NAMES:
        token = extract_direct_write_stdin_session_id(arguments)
    elif name in FUNCTIONS_EXEC_NAMES:
        token = extract_functions_write_stdin_session_id(arguments)
    elif name in WAIT_NAMES:
        kind = "cell"
        token = extract_wait_cell_id({"tool_input": arguments})
    elif name in {"TaskOutput", "TaskStop"} and isinstance(arguments, dict):
        kind = "job"
        token = arguments.get("task_id")
    return f"{event.source.value}:{kind}:{token}" if token is not None else None


def _code_mode_call(event: HookEvent) -> bool:
    name = event.data.get("_original_tool_name") or event.metadata.get("original_tool_name")
    return (name or event.data.get("tool_name")) in FUNCTIONS_EXEC_NAMES | WAIT_NAMES


def _exec_results(event: HookEvent, output: Any) -> list[dict[str, Any]]:
    """Decode printed results only for code-mode cells; other output is command text."""
    if _code_mode_call(event):
        return decoded_exec_results(output)
    return structured_exec_results(output)


def _natively_exited(event: HookEvent) -> bool:
    """A command whose own completion carries an exit code no longer runs.

    Code-mode cells are excluded: a cell can exit while an exec it printed still runs.
    """
    canonical = event.data.get("tool_result")
    return (
        isinstance(canonical, dict)
        and definitive_exit_code(canonical) is not None
        and not _code_mode_call(event)
    )


def _returned_aliases(event: HookEvent, *, shell_call: bool) -> list[str]:
    output = event.data.get("tool_output", event.data.get("tool_response"))
    aliases: set[str] = set()
    # Only a code-mode cell can yield one; other output is command text.
    cell = extract_yielded_cell_id(event.data) if _code_mode_call(event) else None
    if cell is not None:
        aliases.add(f"{event.source.value}:cell:{cell}")
    # MCP results also carry session_id. Only a call that began as shell work
    # can return an execution session, including polls after reconstruction.
    if shell_call and not _natively_exited(event):
        session = extract_direct_exec_running_session_id(output)
        if session is not None:
            aliases.add(f"{event.source.value}:session:{session}")
        for result in _exec_results(event, output):
            session = exec_session_id(result)
            if session is not None and result.get("exit_code") is None:
                aliases.add(f"{event.source.value}:session:{session}")
    receipt = background_job_receipt(output) if event.source.value == "claude" else None
    if receipt is not None:
        aliases.add(f"{event.source.value}:job:{receipt['job']}")
    return sorted(aliases)


def _terminal_background_receipt(event: HookEvent) -> bool:
    output = event.data.get("tool_output", event.data.get("tool_response"))
    name = event.data.get("_original_tool_name", event.data.get("tool_name", ""))
    if name == "TaskStop" and event.metadata.get("is_failure") is not True:
        return output is not None
    if extract_direct_exec_terminal_result(output) is not None:
        return True
    results = _exec_results(event, output)
    canonical = event.data.get("tool_result")
    if isinstance(canonical, dict):
        results.append(canonical)
    if isinstance(output, dict) and isinstance(output.get("task"), dict):
        results.append(output["task"])
    return any(definitive_exit_code(result) is not None for result in results)


def _matching_key(event: HookEvent, variables: dict[str, Any]) -> str | None:
    key = _call_key(event)
    calls = _bindings(variables)
    if key in calls:
        return key
    alias = _continuation_alias(event)
    matches = [
        call_key
        for call_key, call in calls.items()
        if (key is not None and key in call.get("aliases", []))
        or (alias is not None and alias in call.get("aliases", []))
    ]
    if matches or key is not None:
        return matches[0] if len(matches) == 1 else None
    history = variables.get("task_selection_history", [])
    matches = [
        key
        for key, call in _bindings(variables).items()
        if call.get("anonymous") is True
        and call.get("source") == event.source.value
        and call.get("tool_name") == event.data.get("tool_name", "")
        and call.get("pending") is True
        and call.get("selection_count") == len(history)
    ]
    return matches[0] if len(matches) == 1 else None


def cleanup_task_tool_bindings(
    event: HookEvent,
    manager: SessionVariableManager | None,
    session_id: str,
    variables: dict[str, Any],
) -> None:
    """Clear abandoned calls before a turn/session boundary's rule evaluation."""
    if not session_id or manager is None or event.metadata.get("_native_subagent_binding"):
        return
    boundaries = {
        HookEventType.STOP: "turn_end",
        HookEventType.AFTER_AGENT: "turn_end",
        HookEventType.STOP_FAILURE: "turn_end",
        HookEventType.BEFORE_AGENT: "prompt",
        HookEventType.INTERRUPT: "interrupt",
        HookEventType.SESSION_END: "session_end",
    }
    boundary = boundaries.get(event.event_type)
    if boundary is None:
        return
    TaskToolBindings(manager, session_id).clear_pending(boundary)
    persisted = manager.get_variable_subset(session_id, ("task_tool_bindings",))
    if "task_tool_bindings" in persisted:
        variables["task_tool_bindings"] = persisted["task_tool_bindings"]


class TaskToolBindings:
    def __init__(self, manager: SessionVariableManager, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id

    def _checkout_selections(
        self, event: HookEvent, variables: dict[str, Any]
    ) -> dict[str, str | None]:
        from gobby.storage.sessions import SessionManager
        from gobby.workflows.checkout_task_attribution import checkout_task_selections

        project_id = event.project_id
        if project_id is None:
            session = SessionManager(self.manager.db).get(self.session_id)
            project_id = session.project_id if session is not None else None
        return (
            checkout_task_selections(
                self.manager.db, variables, project_id, event.timestamp.timestamp()
            )
            if project_id is not None
            else {}
        )

    def begin_turn(self, event: HookEvent) -> None:
        if event.source.value != "codex" or not event.data.get("turn_id"):
            return

        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            variables["task_tool_turn"] = {
                "turn_id": event.data["turn_id"],
                "started_at": event.timestamp.timestamp(),
                "selection_count": len(variables.get("task_selection_history", [])),
                "checkout_tasks": self._checkout_selections(event, variables),
            }
            return None, True

        self.manager._mutate_variables(
            self.session_id, mutate, keys=("task_tool_turn", "claimed_tasks")
        )

    def start(self, event: HookEvent) -> None:
        key = _call_key(event)
        anonymous = key is None
        key = key or f"{event.source.value}:anonymous:{event.timestamp.isoformat()}"

        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            calls = _bindings(variables)
            original = _matching_key(event, variables)
            if _continuation_alias(event) is not None:
                if original is None:
                    return None, False
                aliases = calls[original].setdefault("aliases", [])
                if key not in aliases:
                    aliases.append(key)
                    variables["task_tool_bindings"] = calls
                    return None, True
                return None, False
            if event.metadata.get("_native_subagent_binding"):
                owned = variables.get("claimed_tasks", {})
                stale = [
                    call_key
                    for call_key, call in calls.items()
                    if call.get("pending") is True
                    and call.get("tool_name") in {"Task", "Agent"}
                    and isinstance(call.get("task_id"), str)
                    and call["task_id"] not in owned
                ]
                if stale:
                    raise ValueError(
                        f"Native agent calls {', '.join(sorted(stale))} lost task ownership; "
                        "use claim_task to restore ownership or stop the native agent before "
                        "retrying its edit."
                    )
            if key in calls:
                return None, False
            started_at = event.timestamp.timestamp()
            calls[key] = {
                "started_at": started_at,
                "task_id": task_selected_at(variables, started_at),
                "tool_name": str(event.data.get("tool_name", "")),
                "pending": True,
                "background": False,
                "fences_switch": _fences_switch(event),
                "anonymous": anonymous,
                "source": event.source.value,
                "checkout_tasks": self._checkout_selections(event, variables),
            }
            outer = event.metadata.get("_native_shell_outer_call_id")
            if event.source == SessionSource.CODEX and isinstance(outer, str) and outer:
                calls[key]["native_outer_call_id"] = outer
            if anonymous:
                calls[key]["selection_count"] = len(variables.get("task_selection_history", []))
            variables["task_tool_bindings"] = calls
            return None, True

        self.manager._mutate_variables(
            self.session_id, mutate, keys=("task_tool_bindings", "claimed_tasks")
        )

    def reconcile_native_failures(self, event: HookEvent) -> None:
        """Drop uniquely proven abandoned native calls, preserving all edit ownership."""
        if node_origin():
            return
        if event.source != SessionSource.CODEX or event.metadata.get("_native_hook_type") not in {
            "PreToolUse",
            "PostToolUse",
        }:
            return
        path = event.data.get("transcript_path")
        if not isinstance(path, str):
            return
        state = self.manager.get_variable_subset(self.session_id, ("task_tool_bindings",))
        calls = _bindings(state)
        current_key = _matching_key(event, state)
        candidates = {
            call["native_outer_call_id"]
            for key, call in calls.items()
            if key != current_key
            and isinstance(call.get("native_outer_call_id"), str)
            and call.get("pending") is True
            and not call.get("background")
            and not call.get("aliases")
        }
        if not candidates or not (failed := failed_native_outer_calls(path, candidates)):
            return

        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            current = _bindings(variables)
            retired: set[str] = set()
            for outer in failed:
                matching = [
                    key
                    for key, call in current.items()
                    if call.get("native_outer_call_id") == outer
                ]
                if len(matching) != 1:
                    continue
                key = matching[0]
                call = current[key]
                if call.get("pending") is not True or call.get("background") or call.get("aliases"):
                    continue
                call.update(pending=False, abandoned=True)
                retired.add(key.partition(":")[2])
            if not retired:
                return None, False
            variables["task_tool_bindings"] = current
            for name in ("claimed_shell_snapshots", "worktree_shell_calls"):
                snapshots = variables.get(name)
                if isinstance(snapshots, dict):
                    variables[name] = {k: v for k, v in snapshots.items() if k not in retired}
            kept = {key.partition(":")[2] for key in current} - retired
            retire_operation_calls(variables, kept)
            return None, True

        self.manager._mutate_variables(
            self.session_id,
            mutate,
            keys=(
                "task_tool_bindings",
                "claimed_shell_snapshots",
                "worktree_shell_calls",
                OPERATIONS_VARIABLE,
            ),
        )

    def is_abandoned(self, event: HookEvent) -> bool:
        """A late post cannot re-adopt edits or credit an outcome we could not prove."""
        state = self.manager.get_variable_subset(self.session_id, ("task_tool_bindings",))
        key = _matching_key(event, state)
        return key is not None and _bindings(state)[key].get("abandoned") is True

    def clear_pending(self, boundary: str) -> None:
        """Retire completed/abandoned calls; only live background work crosses turns.

        A new prompt proves the previous turn ended, so it retires that turn's foreground
        bindings. It keeps the turn marker, which belongs to the turn the prompt starts.
        """
        if boundary not in {"prompt", "interrupt", "turn_end", "session_end", "clear"}:
            raise ValueError(f"Unsupported tool-binding cleanup boundary: {boundary}")

        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            calls = _bindings(variables)
            kept = {
                key: call
                for key, call in calls.items()
                if (
                    call.get("pending") is True
                    and call.get("background") is True
                    and boundary in {"prompt", "interrupt", "turn_end"}
                )
                or (call.get("abandoned") is True and boundary not in {"session_end", "clear"})
            }
            ends_turn = boundary != "prompt"
            changed = kept != calls or (ends_turn and "task_tool_turn" in variables)
            live_call_ids = {key.partition(":")[2] for key in kept}
            retired = retire_operation_calls(variables, live_call_ids)
            for name in ("claimed_shell_snapshots", "worktree_shell_calls"):
                snapshots = variables.get(name)
                if isinstance(snapshots, dict):
                    retained = {
                        key: value for key, value in snapshots.items() if key in live_call_ids
                    }
                    if retained != snapshots:
                        variables[name] = retained
                        retired = True
            if changed:
                variables["task_tool_bindings"] = kept
                if ends_turn:
                    variables.pop("task_tool_turn", None)
            return None, changed or retired

        self.manager._mutate_variables(
            self.session_id,
            mutate,
            keys=(
                "task_tool_bindings",
                "task_tool_turn",
                OPERATIONS_VARIABLE,
                "claimed_shell_snapshots",
                "worktree_shell_calls",
            ),
        )

    def started_at(self, event: HookEvent) -> float | None:
        variables = self.manager.get_variable_subset(
            self.session_id,
            ("task_tool_bindings", "task_tool_turn", "task_selection_history", "claimed_tasks"),
        )
        key = _matching_key(event, variables)
        calls = _bindings(variables)
        call = calls.get(key, {}) if key else {}
        if not call:
            turn = variables.get("task_tool_turn", {})
            if (
                event.source.value == "codex"
                and event.data.get("item_type") == "fileChange"
                and isinstance(turn, dict)
                and turn.get("turn_id") == event.data.get("turn_id")
                and bool(turn.get("turn_id"))
                and turn.get("selection_count") == len(variables.get("task_selection_history", []))
            ):
                value = turn.get("started_at")
                if isinstance(value, (int, float)) and value <= event.timestamp.timestamp():
                    return float(value)
            return None
        task_id = call.get("task_id")
        if isinstance(task_id, str) and task_id not in variables.get("claimed_tasks", {}):
            checkout_tasks = call.get("checkout_tasks", {})
            if not isinstance(checkout_tasks, dict) or not any(
                target in variables.get("claimed_tasks", {})
                for target in checkout_tasks.values()
                if isinstance(target, str)
            ):
                return None
        value = call.get("started_at")
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    def complete(self, event: HookEvent) -> None:
        def mutate(variables: dict[str, Any]) -> tuple[None, bool]:
            key = _matching_key(event, variables)
            calls = _bindings(variables)
            if key is None or key not in calls:
                return None, False
            if calls[key].get("abandoned") is True:
                return None, False
            aliases = _returned_aliases(event, shell_call=calls[key].get("fences_switch") is True)
            calls[key]["aliases"] = sorted(set(calls[key].get("aliases", [])) | set(aliases))
            pending = event.data.get("_verification_pending") is True or bool(aliases)
            continuation = _continuation_alias(event) is not None
            if calls[key].get("background") is True and continuation:
                pending = pending or not _terminal_background_receipt(event)
            if event.metadata.get("is_failure") is True and not continuation:
                pending = False
            calls[key].update(pending=pending, background=pending)
            variables["task_tool_bindings"] = calls
            return None, True

        self.manager._mutate_variables(self.session_id, mutate, keys=("task_tool_bindings",))
