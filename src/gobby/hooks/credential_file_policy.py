"""Detect tool access to local credential files without opening their contents."""

import ast
import os
import re
import shlex
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
    + r")(?![\w./-])",
    re.IGNORECASE,
)
_ANSI_STRING = re.compile(r"\$'((?:\\.|[^'\\])*)'")
_ANSI_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3}|.)")
_PATH_ASSIGNMENT = re.compile(r"\b[A-Za-z_]\w*=(?=[~/$.'\"])")


def _ansi_literal(match: re.Match[str]) -> str:
    def decode(escape: re.Match[str]) -> str:
        value = escape[1]
        if value[0] in "xuU" and len(value) > 1:
            number = int(value[1:], 16)
            return chr(number) if number <= 0x10FFFF else value
        if value[0] in "01234567":
            return chr(int(value, 8))
        return {
            "a": "\a",
            "b": "\b",
            "e": "\x1b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
        }.get(value, value)

    return shlex.quote(_ANSI_ESCAPE.sub(decode, match[1]))


def _literal_string(node: ast.AST) -> str | None:
    """Fold only literal concatenation and environment expansion; never execute code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left), _literal_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        value = _literal_string(node.args[0])
        if value is not None and name in {"expanduser", "expandvars"}:
            return os.path.expanduser(value) if name == "expanduser" else os.path.expandvars(value)
    return None


def _interpreter_paths(command: str) -> list[str]:
    """Recover statically named paths split across an inline Python expression."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    paths: list[str] = []
    for previous, token in zip(tokens, tokens[1:], strict=False):
        if previous != "-c":
            continue
        try:
            tree = ast.parse(token)
            paths.extend(value for node in ast.walk(tree) if (value := _literal_string(node)))
        except (SyntaxError, ValueError, RecursionError):
            continue
    return paths


def _protected_target(target: Path, root: Path, *, start: int | None) -> bool:
    # macOS can preserve spelling while resolving to the same case-insensitive inode.
    # Conservatively protect those spellings on every filesystem.
    target, root = Path(str(target).casefold()), Path(str(root).casefold())
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
    literal_paths: list[str] = []
    if data.get("tool_name") == "Bash" and isinstance(tool_input, Mapping):
        command = tool_input.get("command", tool_input.get("cmd"))
        if isinstance(command, str):
            literal_paths = _interpreter_paths(command)
            command = _ANSI_STRING.sub(_ansi_literal, command)
            command = _PATH_ASSIGNMENT.sub("", command)
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
    for literal in literal_paths:
        extra = tool_read_targets(
            {**data, "tool_name": "Read"}, {"file_path": literal}, preserve_lexical=True
        )
        if extra is None:
            return True
        targets.extend(extra)
    for target, start in targets:
        for root in roots:
            if _protected_target(target, root, start=start):
                return True
            if data.get("tool_name") == "Grep" and root.is_relative_to(target):
                return True
    return False
