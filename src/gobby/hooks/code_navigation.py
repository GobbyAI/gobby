"""Code-navigation metadata helpers for shell hook normalization."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

CODE_FILE_EXTENSIONS = frozenset(
    (
        "py",
        "rs",
        "ts",
        "tsx",
        "js",
        "jsx",
        "go",
        "java",
        "rb",
        "c",
        "cpp",
        "h",
        "hpp",
        "cs",
        "kt",
        "swift",
        "scala",
    )
)
GCODE_NAVIGATION_COMMANDS = frozenset(
    (
        "grep",
        "search",
        "search-symbol",
        "search-text",
        "search-content",
        "outline",
        "symbol",
        "symbol-at",
        "symbols",
        "callers",
        "callees",
        "graph view",
        "usages",
        "imports",
        "path",
        "blast-radius",
        "tree",
        "repo-outline",
        "kinds",
    )
)
GCODE_SEARCH_COMMANDS = frozenset(
    ("grep", "search", "search-symbol", "search-text", "search-content")
)
MAX_NARROW_SOURCE_LINES = 40
_FIND_FILESYSTEM_ONLY_PREDICATES = frozenset(
    {
        "-amin",
        "-anewer",
        "-atime",
        "-cmin",
        "-cnewer",
        "-ctime",
        "-delete",
        "-empty",
        "-exec",
        "-execdir",
        "-executable",
        "-fls",
        "-fprint",
        "-fprintf",
        "-fstype",
        "-inum",
        "-links",
        "-ls",
        "-mmin",
        "-mount",
        "-mtime",
        "-newer",
        "-ok",
        "-okdir",
        "-perm",
        "-printf",
        "-readable",
        "-samefile",
        "-size",
        "-writable",
        "-xdev",
    }
)


def shell_command_name(command: str) -> str:
    return command.rsplit("/", maxsplit=1)[-1]


def line_count_from_tool_input(tool_input: Any) -> int | None:
    if not isinstance(tool_input, Mapping):
        return None
    for key in ("limit", "line_count", "lineCount"):
        parsed = _positive_int(tool_input.get(key))
        if parsed is not None:
            return parsed
    start = _positive_int(tool_input.get("start_line") or tool_input.get("startLine"))
    end = _positive_int(tool_input.get("end_line") or tool_input.get("endLine"))
    if start is not None and end is not None and end >= start:
        return end - start + 1
    return None


def sed_line_count(parts: list[str], positional_args: list[str]) -> int | None:
    if not _sed_has_quiet_option(parts):
        return None
    for token in positional_args:
        match = re.fullmatch(r"(\d+)p", token)
        if match:
            return 1
        match = re.fullmatch(r"(\d+),(\d+)p", token)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            return end - start + 1 if end >= start else None
        match = re.fullmatch(r"(\d+),\+(\d+)p", token)
        if match:
            return int(match.group(2)) + 1
    return None


def count_option_line_count(parts: list[str]) -> int | None:
    if not parts or shell_command_name(parts[0]) not in {"head", "tail"}:
        return None
    for index, part in enumerate(parts[1:], start=1):
        if part in {"-n", "--lines"}:
            if index + 1 < len(parts):
                return _positive_int(parts[index + 1])
            return None
        if part.startswith("--lines="):
            return _positive_int(part.split("=", maxsplit=1)[1])
        if part.startswith("-n") and len(part) > 2:
            return _positive_int(part[2:])
        if re.fullmatch(r"-\d+", part):
            return _positive_int(part[1:])
    return None


def source_read_navigation_metadata(
    paths: list[str],
    *,
    line_count: int | None,
    read_scope: str,
) -> dict[str, Any]:
    if not any(_is_source_file_path(path) for path in paths):
        return {}
    narrow = line_count is not None and line_count <= MAX_NARROW_SOURCE_LINES
    metadata: dict[str, Any] = {
        "canonical_code_navigation_action": "read",
        "canonical_code_navigation_broad": not narrow,
        "canonical_narrow_source_context": narrow,
        "canonical_source_read_scope": read_scope,
    }
    if line_count is not None:
        metadata["canonical_source_line_count"] = line_count
    return metadata


def search_navigation_metadata(paths: Sequence[str] = ()) -> dict[str, Any]:
    """Return search navigation metadata for a search over ``paths``.

    Gcode content search covers safe project text in addition to AST-supported
    source files. Path scope later decides whether the targets belong to an
    indexed checkout.
    """
    return {
        "canonical_code_navigation_action": "search",
        "canonical_code_navigation_broad": True,
    }


def enumerate_navigation_metadata(paths: Sequence[str] = ()) -> dict[str, Any]:
    """Return navigation metadata for filesystem enumeration over ``paths``."""
    return {
        "canonical_code_navigation_action": "enumerate",
        "canonical_code_navigation_broad": (
            not paths or any(_may_hold_source(path) for path in paths)
        ),
    }


def find_navigation_metadata(parts: Sequence[str], paths: Sequence[str] = ()) -> dict[str, Any]:
    """Return navigation metadata for a ``find`` command.

    Name, path, type, and depth predicates describe indexed-tree navigation and
    map to ``gcode tree``. Predicates that depend on live filesystem metadata,
    exact output formatting, or side effects remain ordinary shell operations.
    """
    metadata = enumerate_navigation_metadata(paths)
    metadata["canonical_code_navigation_gcode_supported"] = not any(
        part in _FIND_FILESYSTEM_ONLY_PREDICATES or part.startswith("-newer") for part in parts[1:]
    )
    return metadata


def gcode_navigation_metadata(parts: list[str]) -> tuple[str, dict[str, Any]] | None:
    if len(parts) < 2 or shell_command_name(parts[0]) != "gcode":
        return None
    command_index = 1
    while command_index < len(parts):
        option = parts[command_index]
        if option in {"--project", "--format"}:
            command_index += 2
        elif option in {"--quiet", "--verbose", "--allow-stale"} or option.startswith(
            ("--project=", "--format=")
        ):
            command_index += 1
        else:
            break
    if command_index >= len(parts):
        return None
    subcommand = parts[command_index]
    if subcommand == "graph" and parts[command_index + 1 : command_index + 2] == ["view"]:
        subcommand = "graph view"
    if subcommand not in GCODE_NAVIGATION_COMMANDS:
        return None
    kind = "search" if subcommand in GCODE_SEARCH_COMMANDS else "read"
    return kind, {
        "canonical_code_index_navigation": True,
        "canonical_code_index_command": f"gcode {subcommand}",
        "canonical_code_navigation_action": kind,
    }


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _sed_has_quiet_option(parts: list[str]) -> bool:
    for part in parts[1:]:
        if part in {"--quiet", "--silent"}:
            return True
        if part.startswith("--"):
            continue
        if part.startswith("-") and "n" in part[1:]:
            return True
    return False


def _is_source_file_path(path: str) -> bool:
    return path.rpartition(".")[2].lower() in CODE_FILE_EXTENSIONS


def _may_hold_source(path: str) -> bool:
    """Return True unless ``path`` is a file whose extension marks it as non-source."""
    if path.endswith("/") or path in {".", ".."}:
        return True
    name = path.rpartition("/")[2]
    stem, _, extension = name.rpartition(".")
    if not stem:
        # No extension, or a lone leading dot (`.github` may be a directory).
        return True
    return extension.lower() in CODE_FILE_EXTENSIONS
