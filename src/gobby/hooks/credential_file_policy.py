"""Detect tool access to local credential files without opening their contents."""

import re
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from gobby.hooks._path_scope import resolve_tool_path
from gobby.paths import get_gobby_home
from gobby.workflows.workspace_read_targets import tool_read_targets

_FILE_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "Edit", "Write", "MultiEdit"})
_TOP_LEVEL = ("bootstrap.yaml", "local_cli_token")
_ANY_DEPTH = (".secret_kek", ".secret_salt")
_BARE_CREDENTIAL = re.compile(
    r"(?<![\w./-])("
    + "|".join(re.escape(name) for name in (*_TOP_LEVEL, *_ANY_DEPTH))
    + r")(?![\w./-])"
)


def _protected_target(target: Path, root: Path, *, start: int | None) -> bool:
    pattern = start is not None
    if any(
        fnmatchcase(str(root / name), str(target)) if pattern else target == root / name
        for name in _TOP_LEVEL
    ):
        return True
    prefix = Path(*target.parts[:start]) if start is not None else target
    if not (prefix.is_relative_to(root) or (pattern and root.is_relative_to(prefix))):
        return False
    return any(
        fnmatchcase(name, target.name) if pattern else target.name == name for name in _ANY_DEPTH
    )


def credential_file_access(event_data: Mapping[str, Any] | None, tool_input: Any) -> bool:
    """Return whether normalized tool operands name protected credential paths."""
    data = {**(event_data or {}), "tool_input": tool_input}
    if data.get("tool_name") not in _FILE_TOOLS:
        return False
    roots = {
        root
        for raw in (get_gobby_home(), Path.home() / ".gobby")
        if (root := resolve_tool_path(str(raw), None)) is not None
    }
    roots.update(raw.absolute() for raw in (get_gobby_home(), Path.home() / ".gobby"))
    if data.get("tool_name") == "Bash" and isinstance(tool_input, Mapping):
        command = tool_input.get("command", tool_input.get("cmd"))
        if isinstance(command, str):
            # The shared reader recognizes path-like words. Promote bare credential
            # basenames to relative paths before its cwd/wrapper/data analysis.
            tool_input = {**tool_input, "command": _BARE_CREDENTIAL.sub(r"./\1", command)}
    if (
        data.get("tool_name") == "Grep"
        and isinstance(tool_input, Mapping)
        and not tool_input.get("path")
    ):
        tool_input = {**tool_input, "path": data.get("cwd")}
    targets = tool_read_targets(data, tool_input, preserve_lexical=True)
    if targets is None:
        return True
    for target, start in targets:
        for root in roots:
            if _protected_target(target, root, start=start):
                return True
            if data.get("tool_name") == "Grep" and root.is_relative_to(target):
                return True
    return False
