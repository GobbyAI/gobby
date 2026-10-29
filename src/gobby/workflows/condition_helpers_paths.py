"""Path-scope condition helpers registered in the rule condition namespace."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from gobby.hooks._path_scope import (
    _is_temp_path,
    checkout_root,
    current_project_root,
    current_tool_cwd,
    resolve_tool_path,
)
from gobby.hooks.checkout_reads import CheckoutReadUnavailable
from gobby.workflows.condition_helpers import _event_and_tool_paths, touches_docker_policy_path
from gobby.workflows.git_commit_parsing import (
    parse_git_commit_invocations,
    resolve_commit_inspect_cwd,
)


def _resolved_write_paths(event_data: Mapping[str, Any] | None, tool_input: Any) -> list[Path]:
    """Resolve every write path, or return [] when any path cannot be resolved.

    Relative paths resolve against the tool cwd (falling back to the project
    root); `..` segments and symlinks resolve before any check.
    """
    data = event_data or {}
    base = current_tool_cwd(data) or current_project_root(data)
    resolved: list[Path] = []
    for raw_path in _event_and_tool_paths(event_data, tool_input):
        path = resolve_tool_path(raw_path, base)
        if path is None:
            return []
        resolved.append(path)
    return resolved


def write_paths_within(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    dirs: Iterable[str],
) -> bool:
    """True when every write path equals or descends from one allowed directory.

    Containment is component-wise, so `docs-other/` is outside `docs/`. A
    relative directory is project-relative. An empty path set is False.
    """
    try:
        paths = _resolved_write_paths(event_data, tool_input)
        if not paths:
            return False
        root = current_project_root(event_data or {})
        allowed = [
            directory
            for directory in (
                resolve_tool_path(raw, root) for raw in ([dirs] if isinstance(dirs, str) else dirs)
            )
            if directory is not None
        ]
        return all(any(path.is_relative_to(directory) for directory in allowed) for path in paths)
    except CheckoutReadUnavailable:
        # A node write whose path facts cannot be read is not provably in scope.
        return False


def write_paths_match(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    pattern: str,
) -> bool:
    """True when every resolved absolute write path fullmatches `pattern`.

    A leading `~/` anchors the pattern at the current user's resolved home, so
    shipped rules never name one machine's home directory. A leading `./`
    anchors it at the resolved project root, independently of the tool cwd.
    """
    if pattern.startswith("~/"):
        pattern = re.escape(str(Path.home().resolve())) + pattern[1:]
    elif pattern.startswith("./"):
        root = current_project_root(event_data or {})
        if root is None:
            return False
        pattern = re.escape(str(root)) + pattern[1:]
    paths = _resolved_write_paths(event_data, tool_input)
    return bool(paths) and all(re.fullmatch(pattern, str(path)) for path in paths)


WORKSPACE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
# Daemon-owned repository metadata and project config never live in a seat's folder.


def agent_workspace_write_dirs(variables: Mapping[str, Any]) -> list[str]:
    """Workspace folders a seat may write: its session folder and its epic folder."""
    from gobby.agents.agent_workspaces import agent_workspace_write_dirs as write_dirs

    return write_dirs(variables)


def workspace_contains(path: Path, directory: Path) -> bool | None:
    """Prove containment, or return None for an unverifiable case-only alias."""
    prefix_parts = path.parts[: len(directory.parts)]
    if prefix_parts == directory.parts:
        return True
    if tuple(part.casefold() for part in prefix_parts) != tuple(
        part.casefold() for part in directory.parts
    ):
        return False
    try:
        return Path(*prefix_parts).samefile(directory)
    except OSError:
        return None


def _workspace_write_refused(
    path: Path, root: Path, allowed: list[Path], task_worktree: Path | None = None
) -> bool:
    within_root = workspace_contains(path, root)
    if within_root is not True:
        return within_root is None
    for directory in allowed:
        if workspace_contains(path, directory) is True:
            # Reserve metadata names regardless of spelling; Path.resolve keeps
            # input casing even when the volume resolves it to the same directory.
            parts = tuple(part.casefold() for part in path.parts[len(directory.parts) :])
            if ".git" in parts:
                return True
            if ".gobby" not in parts:
                return False
            # Plan documents are repository content, not daemon configuration.
            index = parts.index(".gobby")
            if (
                parts.count(".gobby") == 1
                and parts[index + 1 : index + 2] == ("plans",)
                and len(parts) > index + 2
                and path.suffix.casefold() == ".md"
            ):
                return False
            # A nested task checkout owns its repository files, including
            # tracked baselines and newly added .gobby configuration.
            try:
                checkout = checkout_root(path)
            except (OSError, CheckoutReadUnavailable):
                return True
            return not (
                checkout is not None
                and (len(checkout.parts) > len(directory.parts) or checkout == task_worktree)
                and workspace_contains(checkout, directory) is True
            )
    return True


def agent_workspace_write_violation(
    event_data: Mapping[str, Any] | None,
    tool_input: Any,
    variables: Mapping[str, Any],
    *,
    task_worktree: Path | None = None,
) -> bool:
    """True when a write lands under the workspace root outside the seat's own or epic folder.

    Git metadata stays refused. Gobby config is writable only in a nested
    repository checkout; Markdown plan documents under ``.gobby/plans`` also
    remain writable outside checkouts. Explicit Git commit pathspecs obey the
    same boundary; literal message text is not a pathspec. A read of a predecessor
    folder is the read rule's concern.
    """
    from gobby.agents.agent_workspaces import workspaces_root

    data = event_data or {}
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    # Structured patches may carry prose in command; only shell tools execute it.
    commits = (
        parse_git_commit_invocations(command)
        if data.get("tool_name") == "Bash" and isinstance(command, str)
        else ()
    )
    is_write = (
        data.get("tool_name") in WORKSPACE_WRITE_TOOLS or data.get("canonical_tool_kind") == "write"
    )
    if not is_write and not commits:
        return False
    root = resolve_tool_path(str(workspaces_root()), None)
    if root is None:
        return False
    base = current_tool_cwd(data) or current_project_root(data)
    paths = _resolved_write_paths(event_data, tool_input) if is_write else []
    for invocation in commits:
        if invocation.cwd_unverified:
            return True
        if not invocation.pathspecs:
            continue
        if base is None:
            return True
        commit_cwd = resolve_commit_inspect_cwd(
            invocation, event_cwd=str(base), project_path=str(base)
        )
        for pathspec in invocation.pathspecs:
            path = resolve_tool_path(pathspec, Path(commit_cwd))
            if path is None:
                return True
            paths.append(path)
    allowed = [
        directory
        for directory in (
            resolve_tool_path(raw, base) for raw in agent_workspace_write_dirs(variables)
        )
        if directory is not None
    ]
    if task_worktree is not None:
        allowed.append(task_worktree)
    return any(_workspace_write_refused(path, root, allowed, task_worktree) for path in paths)


# Provider scratchpads and per-user temp dirs live under /private/var/folders on macOS.
_PROVIDER_TEMP_ROOT = Path("/private/var/folders")
_TMPDIR_OPERAND = re.compile(r"\$\{?TMPDIR\b")


def _temp_write_targets(event_data: Mapping[str, Any] | None) -> list[Path]:
    """Resolved write targets. A command the normalizer names no write target for, such as
    `mv`, falls back to every path it touches, so a temp end still counts (fail closed).
    """
    data = event_data or {}
    raw_paths = data.get("canonical_write_file_paths")
    if not isinstance(raw_paths, list):
        raw_paths = data.get("canonical_file_paths") or []
    base = current_tool_cwd(data) or current_project_root(data)
    return [path for raw in raw_paths if (path := resolve_tool_path(raw, base)) is not None]


def temp_scratchpad_write(event_data: Mapping[str, Any] | None, tool_input: Any) -> bool:
    """True when a write lands in a temp root, or a shell write's target is an unresolved $TMPDIR.

    Any one temp target is enough, unlike the all-paths allowlist helpers. The roots are
    /tmp, /private/tmp, the live tempdir (resolved at call time) and /private/var/folders.
    """
    data = event_data or {}
    if any(
        _is_temp_path(path) or path.is_relative_to(_PROVIDER_TEMP_ROOT)
        for path in _temp_write_targets(data)
    ):
        return True
    # A write whose target the normalizer could not resolve marks its scope unknown; a
    # $TMPDIR operand in such a command is the one unresolved target that is a temp write.
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    return (
        data.get("canonical_repo_mutation_scope_unknown") is True
        and isinstance(command, str)
        and _TMPDIR_OPERAND.search(command) is not None
    )


PATH_CONDITION_HELPERS: dict[str, Callable[..., Any]] = {
    "touches_docker_policy_path": touches_docker_policy_path,
    "write_paths_within": write_paths_within,
    "write_paths_match": write_paths_match,
    "agent_workspace_write_dirs": agent_workspace_write_dirs,
    "agent_workspace_write_violation": agent_workspace_write_violation,
    "temp_scratchpad_write": temp_scratchpad_write,
}
