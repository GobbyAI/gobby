"""Condition helper functions for rule engine expressions.

These functions are registered as allowed_funcs in SafeExpressionEvaluator
so they can be called from rule ``when`` conditions, e.g.:

    when: "task_tree_complete(variables.session_task)"
"""

import logging
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from gobby.config.validation_detection import is_validation_command as _config_is_validation_command
from gobby.tasks.state_semantics import projected_task_state
from gobby.workflows.verification_evidence import (
    VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND,
    VERIFICATION_EVIDENCE_VARIABLE,
)

logger = logging.getLogger(__name__)

TaskIdRef = str | int | UUID | bytes | bytearray | memoryview
TaskIdInput = TaskIdRef | Iterable[TaskIdRef | None] | None
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_SEGMENT_SEPARATORS = frozenset({"&&", "||", ";", "|"})
_UV_RUN_OPTIONS_WITH_VALUE = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--group",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-extra",
        "--no-group",
        "--only-group",
        "--package",
        "--prerelease",
        "--project",
        "--python",
        "--refresh-package",
        "--reinstall-package",
        "--resolution",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-C",
        "-P",
        "-f",
        "-i",
        "-p",
        "-w",
    }
)


class TaskProvider(Protocol):
    db: Any

    def get_task(self, task_id: str) -> Any: ...
    def list_tasks(self, parent_task_id: str) -> Sequence[Any]: ...


def is_task_complete(task: Any) -> bool:
    """Check if a task counts as complete for workflow purposes.

    A task is complete only when closure metadata projects to closed.
    """
    return projected_task_state(task) == "closed"


def is_validation_command(command: Any) -> bool:
    """Return whether a shell command invokes a validation tool."""
    return _config_is_validation_command(command)


def is_gobby_build_command(command: Any) -> bool:
    """Return whether a shell command directly invokes ``gobby build``."""
    if not isinstance(command, str) or not command.strip():
        return False

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SHELL_SEGMENT_SEPARATORS:
            if _segment_invokes_gobby_build(segment):
                return True
            segment = []
            continue
        segment.append(token)
    return False


def _segment_invokes_validation(tokens: list[str]) -> bool:
    tokens = _strip_env_assignments(tokens)
    if not tokens:
        return False

    executable = _executable_name(tokens[0])
    if executable == "uv" and len(tokens) > 1 and tokens[1] == "run":
        return _segment_invokes_validation(_strip_uv_run_options(tokens[2:]))
    if executable in {"python", "python3"} or executable.startswith("python3."):
        return _python_module_invokes_validation(tokens[1:])
    if executable in {"pytest", "tox", "nox", "vitest", "jest"}:
        return True
    if executable in {"mypy", "pyright", "basedpyright"}:
        return True
    if executable == "coverage":
        return len(tokens) > 1 and tokens[1] == "run"
    if executable == "ruff":
        return len(tokens) > 1 and (
            tokens[1] == "check" or (tokens[1] == "format" and "--check" in tokens[2:])
        )
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        return _package_manager_invokes_validation(tokens[1:])
    if executable in {"cargo", "go", "mix"}:
        return len(tokens) > 1 and tokens[1] == "test"
    if executable == "make":
        return len(tokens) > 1 and tokens[1] in {"test", "tests"}
    return False


def _segment_invokes_gobby_build(tokens: list[str]) -> bool:
    tokens = _strip_env_assignments(tokens)
    if not tokens:
        return False

    executable = _executable_name(tokens[0])
    if executable == "uv" and len(tokens) > 1 and tokens[1] == "run":
        return _segment_invokes_gobby_build(_strip_uv_run_options(tokens[2:]))
    if executable in {"python", "python3"} or executable.startswith("python3."):
        return (
            len(tokens) > 3 and tokens[1] == "-m" and tokens[2] == "gobby" and tokens[3] == "build"
        )
    return executable == "gobby" and len(tokens) > 1 and tokens[1] == "build"


def completion_evidence_ready(variables: Mapping[str, Any] | None) -> bool:
    """Return whether current session evidence is sufficient for completion."""
    if not isinstance(variables, Mapping):
        return False

    evidence_items = variables.get(VERIFICATION_EVIDENCE_VARIABLE)
    if not isinstance(evidence_items, list):
        return False

    successful_evidence_seen = False
    failed_validation_unresolved = False

    for item in evidence_items:
        if not isinstance(item, Mapping):
            continue
        evidence_type = item.get("evidence_type")
        if not isinstance(evidence_type, str):
            continue

        success = item.get("success")
        if evidence_type == VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND:
            if success is True:
                successful_evidence_seen = True
                failed_validation_unresolved = False
            elif success is False:
                failed_validation_unresolved = True
            continue

        if success is True:
            successful_evidence_seen = True

    return successful_evidence_seen and not failed_validation_unresolved


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_RE.match(tokens[index]):
        index += 1
    return tokens[index:]


def _strip_uv_run_options(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if not token.startswith("-"):
            return tokens[index:]
        if "=" in token:
            index += 1
            continue
        if token in _UV_RUN_OPTIONS_WITH_VALUE:
            index += 2
            continue
        index += 1
    return []


def _python_module_invokes_validation(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[0] != "-m":
        return False
    module = tokens[1]
    if module in {"pytest", "mypy", "pyright", "basedpyright", "tox", "nox"}:
        return True
    if module in {"coverage"}:
        return len(tokens) > 2 and tokens[2] == "run"
    return (
        module == "ruff"
        and len(tokens) > 2
        and (tokens[2] == "check" or (tokens[2] == "format" and "--check" in tokens[3:]))
    )


def _package_manager_invokes_validation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] in {"test", "lint"}:
        return True
    return len(tokens) > 1 and tokens[0] == "run" and tokens[1] in {"test", "lint"}


def _executable_name(executable: str) -> str:
    return executable.rsplit("/", 1)[-1]


def task_needs_human_review(task_manager: TaskProvider | None, task_id: TaskIdRef | None) -> bool:
    """Check if a task has been escalated for human review.

    Returns True when escalation metadata projects to escalated.

    Used in rule conditions like:
        when: "task_needs_human_review(variables.session_task)"
    """
    if not task_id:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = _get_task(task_manager, normalized)
    if not task:
        logger.warning(f"task_needs_human_review: Task '{normalized}' not found")
        return False

    return projected_task_state(task) == "escalated"


def _normalize_task_id(task_id: Any) -> str:
    """Normalize a task_id to string format.

    Handles int seq_nums (e.g. 9438 from auto_task_ref) by converting to '#9438'.
    """
    if isinstance(task_id, int):
        return f"#{task_id}"
    if isinstance(task_id, bytes | bytearray | memoryview):
        try:
            return str(UUID(bytes=bytes(task_id)))
        except ValueError:
            # Invalid UUID byte buffers can come from malformed rule variables;
            # stringify them so callers fail closed instead of raising.
            return str(task_id)
    return str(task_id)


def _get_task(task_manager: TaskProvider, task_id: str) -> Any | None:
    try:
        return task_manager.get_task(task_id)
    except ValueError:
        pass
    if not (task_id.startswith("#") or task_id.isdigit()):
        return None
    try:
        seq_num = int(task_id[1:] if task_id.startswith("#") else task_id)
    except ValueError:
        return None
    db = getattr(task_manager, "db", None)
    if db is None:
        return None
    rows = db.fetchall("SELECT id FROM tasks WHERE seq_num = ?", (seq_num,))
    if len(rows) != 1:
        return None
    try:
        return task_manager.get_task(rows[0]["id"])
    except ValueError:
        return None


def task_tree_complete(task_manager: TaskProvider | None, task_id: TaskIdInput) -> bool:
    """Check if a task tree is complete (all work is done).

    A task tree is complete when either:
    - The task is explicitly closed, OR
    - The task has subtasks and ALL subtasks are recursively complete

    Used in rule conditions like:
        when: "task_tree_complete(variables.session_task)"
        when: "task_tree_complete(variables.auto_task_ref)"
    """
    task_ids = _normalize_task_ids(task_id, "task_tree_complete")
    if task_ids is None:
        return False
    if not task_ids:
        return True

    if not task_manager:
        logger.warning("task_tree_complete: No task_manager available")
        return False

    for tid in task_ids:
        if not _is_tree_complete(task_manager, tid):
            return False

    return True


def task_state_in(
    task_manager: TaskProvider | None, task_id: TaskIdRef | None, *states: str
) -> bool:
    """Check whether the task's projected stage-native state is in the provided set."""
    if not task_id or not states:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = _get_task(task_manager, normalized)
    if not task:
        logger.debug(f"task_state_in: Task '{normalized}' not found")
        return False

    normalized_states = {state.strip() for state in states if isinstance(state, str)}
    return projected_task_state(task) in normalized_states


def _normalize_task_ids(task_id_or_ids: TaskIdInput, caller_name: str) -> list[str] | None:
    """Normalize a single task ref or iterable of refs to string refs."""
    if task_id_or_ids is None:
        return []
    if isinstance(task_id_or_ids, str | int | UUID):
        return [_normalize_task_id(task_id_or_ids)]
    if isinstance(task_id_or_ids, bytes | bytearray | memoryview):
        return [_normalize_task_id(task_id_or_ids)]
    if isinstance(task_id_or_ids, Iterable):
        task_ids: list[str] = []
        for item in task_id_or_ids:
            if item is None:
                continue
            if isinstance(item, bytes | bytearray | memoryview):
                task_ids.append(_normalize_task_id(item))
                continue
            task_ids.append(_normalize_task_id(item))
        return task_ids
    logger.warning("%s: Unexpected task_id type: %s", caller_name, type(task_id_or_ids))
    return None


def task_type_in(
    task_manager: TaskProvider | None, task_id_or_ids: TaskIdInput, *types: str
) -> bool:
    """Check whether any referenced task has a task_type in the provided set.

    Accepts UUIDs, ``#N`` refs, integer seq refs, and iterables containing any
    mix of those forms.
    """
    if not task_manager or not types:
        return False

    normalized_types = {
        task_type.strip().lower() for task_type in types if isinstance(task_type, str)
    }
    if not normalized_types:
        return False

    task_ids = _normalize_task_ids(task_id_or_ids, "task_type_in")
    if task_ids is None:
        return False

    for task_id in task_ids:
        task = _get_task(task_manager, task_id)
        if not task:
            logger.debug("task_type_in: Task '%s' not found", task_id)
            continue
        task_type = getattr(task, "task_type", None)
        if isinstance(task_type, str) and task_type.strip().lower() in normalized_types:
            return True
    return False


def _is_tree_complete(task_manager: Any, task_id: str) -> bool:
    """Check if a single task and its subtree are complete."""
    task = _get_task(task_manager, task_id)
    if not task:
        logger.warning(f"task_tree_complete: Task '{task_id}' not found")
        return False

    task_closed = is_task_complete(task)
    subtasks = task_manager.list_tasks(parent_task_id=task_id)

    if not subtasks:
        if not task_closed:
            logger.debug(
                "task_tree_complete: Leaf task '%s' is not complete (state=%s)",
                task_id,
                projected_task_state(task),
            )
        return task_closed

    for subtask in subtasks:
        if not _is_tree_complete(task_manager, subtask.id):
            return False

    if not task_closed:
        logger.debug(
            f"task_tree_complete: Task '{task_id}' not explicitly closed but all "
            f"{len(subtasks)} subtask(s) complete — tree is complete"
        )

    return True
