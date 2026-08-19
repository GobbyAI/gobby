"""Condition helper functions for rule engine expressions.

These functions are registered as allowed_funcs in SafeExpressionEvaluator
so they can be called from rule ``when`` conditions, e.g.:

    when: "task_tree_complete(variables.session_task)"
"""

import ast
import logging
import re
import textwrap
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from gobby.config.shell_lexing import shell_command_segments
from gobby.config.validation_detection import (
    is_validation_command as _config_is_validation_command,
)
from gobby.hooks.memory_recall_delivery import MEMORY_RECALL_DELIVERIES_VARIABLE
from gobby.tasks.state_semantics import projected_task_state

logger = logging.getLogger(__name__)


def pending_memory_recall_request_id(variables: Mapping[str, Any]) -> str | None:
    """Return the oldest valid pending recall request ID."""
    deliveries = variables.get(MEMORY_RECALL_DELIVERIES_VARIABLE)
    if not isinstance(deliveries, list):
        return None
    for delivery in deliveries:
        if not isinstance(delivery, Mapping) or delivery.get("status") != "pending":
            continue
        request_id = delivery.get("recall_request_id")
        references = delivery.get("references")
        if (
            isinstance(request_id, str)
            and request_id
            and isinstance(references, list)
            and any(
                isinstance(reference, Mapping)
                and isinstance(reference.get("memory_id"), str)
                and bool(reference.get("memory_id"))
                for reference in references
            )
        ):
            return request_id
    return None


def is_pending_memory_recall_call(
    tool_input: Any,
    expected_recall_request_id: str | None,
) -> bool:
    """Check whether a proxy call retrieves the exact oldest pending recall."""
    return bool(
        expected_recall_request_id
        and isinstance(tool_input, Mapping)
        and tool_input.get("server_name") == "gobby-memory"
        and tool_input.get("tool_name") == "get_recall_memories"
        and tool_input.get("recall_request_id") == expected_recall_request_id
    )


TaskIdRef = str | int | UUID | bytes | bytearray | memoryview
TaskIdInput = TaskIdRef | Iterable[TaskIdRef | None] | None
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_TASK_COMMIT_PROJECT_PATH_GUARD_FILE_SUFFIXES = frozenset(
    {
        "src/gobby/mcp_proxy/tools/task_commits.py",
        "src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py",
        "src/gobby/mcp_proxy/tools/_lifecycle_close.py",
    }
)
_PROJECT_PATH_RESOLVER_CALLS = frozenset({"resolve_task_repo_path", "_get_task_and_repo_path"})
_DIRECT_PROJECT_PATH_CWD_RE = re.compile(r"\bcwd\s*=\s*(?:(?:str|Path)\(\s*)?project_path\b")
_SOURCE_FRAGMENT_KEYS = frozenset(
    {
        "content",
        "new_string",
        "newStr",
        "new_text",
        "newText",
        "replacement",
        "text",
    }
)
_NESTED_EDIT_KEYS = frozenset({"changes", "edits"})
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


def task_commit_project_path_allowlist_violation(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> bool:
    """Detect edits that route raw ``project_path`` into task Git helper cwd."""
    if not _touches_task_commit_guard_file(event_data, tool_input):
        return False

    return any(
        _source_fragment_uses_direct_project_path_cwd(fragment)
        for fragment in _iter_source_fragments(tool_input)
    )


def _touches_task_commit_guard_file(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> bool:
    touched_paths: list[str] = []
    if event_data:
        canonical_paths = event_data.get("canonical_file_paths")
        if isinstance(canonical_paths, list | tuple):
            touched_paths.extend(path for path in canonical_paths if isinstance(path, str))
        canonical_path = event_data.get("canonical_file_path")
        if isinstance(canonical_path, str):
            touched_paths.append(canonical_path)

    try:
        from gobby.workflows.enforcement.blocking import get_touched_file_paths

        touched_paths.extend(get_touched_file_paths(tool_input))
    except (AttributeError, TypeError, ValueError):
        logger.debug("Unable to derive touched paths for task commit guardrail", exc_info=True)

    return any(_is_task_commit_guard_file(path) for path in touched_paths)


def first_tdd_code_path(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> str:
    """Return the first touched Python source path that should trigger TDD blocking."""
    return _first_matching_path(event_data, tool_input, _is_tdd_code_path)


def first_tdd_test_path(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> str:
    """Return the first touched test path for TDD observability."""
    return _first_matching_path(event_data, tool_input, _is_tdd_test_path)


def touches_claude_memory_path(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> bool:
    """Return True when canonical or native path fields touch Claude file memory."""
    return any(
        _is_claude_memory_path(path) for path in _event_and_tool_paths(event_data, tool_input)
    )


def touches_docker_policy_path(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> bool:
    """Return True when canonical or native path fields touch Docker policy."""
    return any(
        _is_docker_policy_path(path) for path in _event_and_tool_paths(event_data, tool_input)
    )


_UI_DESIGN_MARKUP_SUFFIXES = (
    ".tsx",
    ".jsx",
    ".vue",
    ".svelte",
    ".astro",
    ".css",
    ".scss",
    ".html",
)
_UI_DESIGN_SCRIPT_SUFFIXES = (".ts", ".js", ".mjs", ".cjs")


def touches_ui_design_path(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> bool:
    """Return True when a write touches UI or design files.

    Markup, style, and component extensions match anywhere. Bare script
    extensions match only under a ``web/`` segment so skill scripts and
    Node tooling stay out of design enforcement.
    """
    return any(is_ui_design_path(path) for path in _event_and_tool_paths(event_data, tool_input))


def is_ui_design_path(path: str) -> bool:
    """Return whether one path is a UI or design file."""
    normalized = _normalize_condition_path(path)
    if normalized.endswith(_UI_DESIGN_MARKUP_SUFFIXES):
        return True
    if not normalized.endswith(_UI_DESIGN_SCRIPT_SUFFIXES):
        return False
    return "/web/" in normalized or normalized.startswith("web/")


def _first_matching_path(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    predicate: Callable[[str], bool],
) -> str:
    for path in _event_and_tool_paths(event_data, tool_input):
        if predicate(path):
            return path
    return ""


def _event_and_tool_paths(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
) -> list[str]:
    paths: list[str] = []
    if event_data:
        _append_condition_path(paths, event_data.get("canonical_file_path"))
        canonical_paths = event_data.get("canonical_file_paths")
        if isinstance(canonical_paths, list | tuple):
            for path in canonical_paths:
                _append_condition_path(paths, path)

    if isinstance(tool_input, Mapping):
        for key in ("file_path", "path", "pattern"):
            _append_condition_path(paths, tool_input.get(key))
        file_paths = tool_input.get("file_paths")
        if isinstance(file_paths, list | tuple):
            for path in file_paths:
                _append_condition_path(paths, path)

    try:
        from gobby.workflows.enforcement.blocking import get_touched_file_paths

        for path in get_touched_file_paths(tool_input):
            _append_condition_path(paths, path)
    except (AttributeError, TypeError, ValueError):
        logger.debug("Unable to derive condition helper paths", exc_info=True)

    return paths


def _append_condition_path(paths: list[str], candidate: Any) -> None:
    if isinstance(candidate, str) and candidate and candidate not in paths:
        paths.append(candidate)


def _normalize_condition_path(path: str) -> str:
    return path.replace("\\", "/")


def _path_has_segment(path: str, segment: str) -> bool:
    return segment in [part for part in _normalize_condition_path(path).split("/") if part]


def _is_tdd_code_path(path: str) -> bool:
    normalized = _normalize_condition_path(path)
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.endswith(".py")
        and name not in {"__init__.py", "conftest.py"}
        and not _path_has_segment(normalized, "tests")
        and not name.startswith("test_")
        and not normalized.endswith("_test.py")
    )


def _is_tdd_test_path(path: str) -> bool:
    normalized = _normalize_condition_path(path)
    name = normalized.rsplit("/", 1)[-1]
    return (
        _path_has_segment(normalized, "tests")
        or name.startswith("test_")
        or normalized.endswith("_test.py")
    )


def _is_docker_policy_path(path: str) -> bool:
    normalized = _normalize_condition_path(path)
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return False

    lowered_parts = [part.lower() for part in parts]
    filename = lowered_parts[-1]
    if ".docker" in lowered_parts:
        return True
    if (
        filename == "dockerfile"
        or filename.startswith(("dockerfile.", "dockerfile-"))
        or filename.endswith(".dockerfile")
        or filename == "containerfile"
    ):
        return True
    if filename == ".dockerignore" or (
        filename.startswith("docker-bake.") and filename.endswith((".hcl", ".json"))
    ):
        return True
    return filename.endswith((".yml", ".yaml")) and (
        filename.startswith(("docker-compose", "podman-compose", "compose.", "compose-"))
    )


def _is_claude_memory_path(path: str) -> bool:
    """Match only Claude Code's file-based memory layouts.

    Blocked layouts are a ``memory`` directory directly under ``.claude/``
    (project-local) or under ``.claude/projects/<slug>/`` (user-level
    auto-memory). Independent substring checks are not enough: a repo checked
    out under ``.claude/worktrees/`` puts arbitrary source such as
    ``src/gobby/memory/`` beneath ``.claude/``, and those paths must not match.
    """
    parts = [part for part in _normalize_condition_path(path).split("/") if part]
    for index, part in enumerate(parts):
        if part != ".claude":
            continue
        if index + 1 < len(parts) and parts[index + 1] == "memory":
            return True
        if (
            index + 3 < len(parts)
            and parts[index + 1] == "projects"
            and parts[index + 3] == "memory"
        ):
            return True
    return False


def _is_task_commit_guard_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(
        normalized.endswith(suffix) for suffix in _TASK_COMMIT_PROJECT_PATH_GUARD_FILE_SUFFIXES
    )


def _iter_source_fragments(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key in _SOURCE_FRAGMENT_KEYS:
            fragment = value.get(key)
            if isinstance(fragment, str) and fragment.strip():
                yield fragment
        for key in _NESTED_EDIT_KEYS:
            nested = value.get(key)
            if isinstance(nested, list | tuple):
                for item in nested:
                    yield from _iter_source_fragments(item)
        return

    if isinstance(value, list | tuple):
        for item in value:
            yield from _iter_source_fragments(item)


def _source_fragment_uses_direct_project_path_cwd(fragment: str) -> bool:
    module = _parse_source_fragment(fragment)
    if module is None:
        return bool(_DIRECT_PROJECT_PATH_CWD_RE.search(fragment))
    return _statements_use_direct_project_path_cwd(module.body, {"project_path"})


def _parse_source_fragment(fragment: str) -> ast.Module | None:
    dedented = textwrap.dedent(fragment)
    for candidate in (
        dedented,
        "def _gobby_task_commit_guardrail_wrapper():\n" + textwrap.indent(dedented, "    "),
    ):
        try:
            return ast.parse(candidate)
        except SyntaxError:
            continue
    return None


def _statements_use_direct_project_path_cwd(
    statements: Sequence[ast.stmt],
    tainted_names: set[str],
) -> bool:
    local_tainted = set(tainted_names)
    for statement in statements:
        if _statement_uses_tainted_cwd(statement, local_tainted):
            return True
        if _nested_statements_use_direct_project_path_cwd(statement, local_tainted):
            return True
        _update_project_path_taint(statement, local_tainted)
    return False


def _nested_statements_use_direct_project_path_cwd(
    statement: ast.stmt,
    tainted_names: set[str],
) -> bool:
    for body, taint in _iter_nested_statement_bodies(statement, tainted_names):
        if _statements_use_direct_project_path_cwd(body, taint):
            return True
    return False


def _iter_nested_statement_bodies(
    statement: ast.stmt,
    tainted_names: set[str],
) -> Iterable[tuple[Sequence[ast.stmt], set[str]]]:
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
        function_taint = {
            arg.arg
            for arg in (
                *statement.args.posonlyargs,
                *statement.args.args,
                *statement.args.kwonlyargs,
            )
            if arg.arg == "project_path"
        }
        function_taint.add("project_path")
        yield statement.body, function_taint
        return

    if isinstance(statement, ast.If | ast.While | ast.For | ast.AsyncFor):
        yield statement.body, set(tainted_names)
        yield statement.orelse, set(tainted_names)
        return

    if isinstance(statement, ast.With | ast.AsyncWith):
        yield statement.body, set(tainted_names)
        return

    if isinstance(statement, ast.Try):
        yield statement.body, set(tainted_names)
        yield statement.orelse, set(tainted_names)
        yield statement.finalbody, set(tainted_names)
        for handler in statement.handlers:
            yield handler.body, set(tainted_names)


def _statement_uses_tainted_cwd(statement: ast.stmt, tainted_names: set[str]) -> bool:
    return any(
        isinstance(node, ast.Call) and _call_uses_tainted_cwd(node, tainted_names)
        for node in ast.walk(statement)
    )


def _call_uses_tainted_cwd(call: ast.Call, tainted_names: set[str]) -> bool:
    return any(
        keyword.arg == "cwd" and _expression_uses_tainted_name(keyword.value, tainted_names)
        for keyword in call.keywords
    )


def _expression_uses_tainted_name(expression: ast.AST, tainted_names: set[str]) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id in tainted_names for node in ast.walk(expression)
    )


def _update_project_path_taint(statement: ast.stmt, tainted_names: set[str]) -> None:
    if isinstance(statement, ast.Assign):
        _update_assigned_names(statement.targets, statement.value, tainted_names)
    elif isinstance(statement, ast.AnnAssign):
        _update_assigned_names([statement.target], statement.value, tainted_names)
    elif isinstance(statement, ast.AugAssign):
        _update_assigned_names([statement.target], statement.value, tainted_names)


def _update_assigned_names(
    targets: Sequence[ast.expr],
    value: ast.AST | None,
    tainted_names: set[str],
) -> None:
    assigned_names = [name for target in targets for name in _iter_assigned_names(target)]
    if not assigned_names:
        return
    if value is None or _is_project_path_resolver_call(value):
        for name in assigned_names:
            tainted_names.discard(name)
        return

    value_is_tainted = _expression_uses_tainted_name(value, tainted_names)
    for name in assigned_names:
        if value_is_tainted:
            tainted_names.add(name)
        else:
            tainted_names.discard(name)


def _iter_assigned_names(target: ast.expr) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
        return
    if isinstance(target, ast.Tuple | ast.List):
        for item in target.elts:
            yield from _iter_assigned_names(item)


def _is_project_path_resolver_call(value: ast.AST) -> bool:
    return isinstance(value, ast.Call) and _call_name(value) in _PROJECT_PATH_RESOLVER_CALLS


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


class TaskProvider(Protocol):
    db: Any

    def get_task(self, task_id: str) -> Any: ...
    def list_tasks(self, *, parent_task_id: str) -> Sequence[Any]: ...


def is_validation_command(command: Any) -> bool:
    """Retain reviewer-rule command classification outside completion readiness."""
    return _config_is_validation_command(command)


def is_task_complete(task: Any) -> bool:
    """Check if a task counts as complete for workflow purposes.

    A task is complete only when closure metadata projects to closed.
    """
    return projected_task_state(task) == "closed"


def is_gobby_build_command(command: Any) -> bool:
    """Return whether a shell command directly invokes ``gobby build``."""
    if not isinstance(command, str) or not command.strip():
        return False

    return any(_segment_invokes_gobby_build(segment) for segment in shell_command_segments(command))


def shell_command_invokes_gcode(command: Any) -> bool:
    """Return whether any shell command segment invokes ``gcode``."""
    if not isinstance(command, str) or not command.strip():
        return False

    for segment in shell_command_segments(command):
        tokens = _strip_env_assignments(segment)
        if tokens and _executable_name(tokens[0]) == "gcode":
            return True
    return False


def _segment_invokes_gobby_build(tokens: list[str]) -> bool:
    tokens = _strip_env_assignments(tokens)
    if not tokens:
        return False

    executable = _executable_name(tokens[0])
    if executable == "uv" and len(tokens) > 1 and tokens[1] == "run":
        return _segment_invokes_gobby_build(_strip_uv_run_options(tokens[2:]))
    if executable in {"python", "python3"} or executable.startswith("python3."):
        module_tokens = _python_module_tokens(tokens[1:])
        return module_tokens is not None and module_tokens[:2] == ["gobby", "build"]
    return executable == "gobby" and len(tokens) > 1 and tokens[1] == "build"


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_RE.match(tokens[index]):
        index += 1
    return tokens[index:]


def _strip_uv_run_options(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":  # nosec B105 # CLI option terminator, not a credential.
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


def _python_module_tokens(tokens: list[str]) -> list[str] | None:
    index = 0
    options_with_value = {"-W", "-X", "--check-hash-based-pycs"}
    while index < len(tokens):
        token = tokens[index]
        if token == "-m":  # nosec B105 # Python module flag, not a credential.
            return tokens[index + 1 :]
        if token == "--":  # nosec B105 # CLI option terminator, not a credential.
            return None
        if token in options_with_value and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith("-W") or token.startswith("-X"):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return None
    return None


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
        logger.warning("task_needs_human_review: Task '%s' not found", normalized)
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
    rows = db.fetchall("SELECT id FROM tasks WHERE seq_num = %s", (seq_num,))
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
        logger.debug("task_state_in: Task '%s' not found", normalized)
        return False

    normalized_states = {state.strip().lower() for state in states if isinstance(state, str)}
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


def all_tasks_have_label(
    task_manager: TaskProvider | None,
    task_id_or_ids: TaskIdInput,
    label: str,
) -> bool:
    """Return whether every current task row carries ``label``.

    Empty, malformed, and missing task sets fail closed. Task rows are loaded
    during each evaluation so cached session label metadata cannot grant an
    exemption.
    """
    if not task_manager or not isinstance(label, str) or not label:
        return False
    task_ids = _normalize_task_ids(task_id_or_ids, "all_tasks_have_label")
    if not task_ids:
        return False
    for task_id in task_ids:
        task = _get_task(task_manager, task_id)
        if task is None:
            return False
        labels = getattr(task, "labels", None)
        if not isinstance(labels, list | tuple | set) or label not in labels:
            return False
    return True


def _is_tree_complete(task_manager: Any, task_id: str) -> bool:
    """Check if a single task and its subtree are complete."""
    task = _get_task(task_manager, task_id)
    if not task:
        logger.warning("task_tree_complete: Task '%s' not found", task_id)
        return False

    task_closed = is_task_complete(task)
    resolved_task_id = getattr(task, "id", task_id)
    subtasks = task_manager.list_tasks(parent_task_id=resolved_task_id)

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
            "task_tree_complete: Task '%s' not explicitly closed but all %s subtask(s) "
            "complete — tree is complete",
            task_id,
            len(subtasks),
        )

    return True
