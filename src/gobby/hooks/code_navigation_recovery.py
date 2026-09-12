"""Path- and operation-scoped filesystem fallback for indexed navigation."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.config.shell_lexing import parse_shell_command
from gobby.hooks._path_scope import (
    code_navigation_may_touch_project,
    current_project_root,
    current_tool_cwd,
    resolve_tool_path,
)

# Gcode's fixed directory exclusions (index/indexer/util.rs). Build/dist are
# root-only there; nested generated output is recognized through Git below.
_INDEX_EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        "vendor",
        ".next",
        ".nuxt",
        "coverage",
        ".cache",
    }
)


def gcode_targets(parts: list[str], command: str) -> list[str]:
    """Extract operands, never mistake a search query or option value for a path."""
    subcommand = command.removeprefix("gcode ")
    if subcommand not in {
        "outline",
        "symbol-at",
        "tree",
        "grep",
        "search",
        "search-symbol",
        "search-text",
        "search-content",
    }:
        return []
    start = 1
    while start < len(parts):
        if parts[start] in {"--project", "--format"}:
            start += 2
        elif parts[start].startswith("-"):
            start += 1
        else:
            break
    start += 1
    operands: list[str] = []
    value_options = {
        "--project",
        "--format",
        "--limit",
        "--offset",
        "--token-budget",
        "--language",
        "--kind",
        "-m",
        "--max-count",
        "-g",
        "--glob",
        "-A",
        "-B",
        "-C",
    }
    index = start
    while index < len(parts):
        token = parts[index]
        if token == "--":
            operands.extend(parts[index + 1 :])
            break
        if token in value_options:
            index += 2
            continue
        if not token.startswith("-"):
            operands.append(token)
        index += 1
    if subcommand in {"grep", "search", "search-symbol", "search-text", "search-content"}:
        return operands[1:] or ["."]
    if subcommand == "symbol-at":
        return [re.sub(r":\d+(?::\d+)?$", "", p) for p in operands]
    return operands or (["."] if subcommand == "tree" else [])


def _ignored_target(path: Path, root: Path | None) -> bool:
    """Git's own exclusion matcher handles generated trees and negation rules."""
    if root is None:
        return False
    if not path.is_relative_to(root):
        # Repository scope also includes linked checkouts. Their exclusions
        # must be resolved against their own root and .gitignore.
        root = next((parent for parent in path.parents if (parent / ".git").exists()), None)
        if root is None:
            return False
    # A wildcard may include indexed siblings; require a literal target.
    if any(char in str(path) for char in "*?[]$"):
        return False
    relative = path.relative_to(root)
    if relative.parts and (
        relative.parts[0] in {"build", "dist"}
        or any(part in _INDEX_EXCLUDED_DIRS for part in relative.parts)
    ):
        return True
    # Gcode explicitly rescues these paths even when Git ignores them. For a
    # directory request, any overlapping allowlist prefix retains enforcement.
    prefixes = [Path(".gobby/plans"), Path(".github/workflows")]
    try:
        config = json.loads((root / ".gobby/gcode.json").read_text())
    except (OSError, ValueError):
        config = {}
    if isinstance(config, dict) and isinstance(config.get("index"), dict):
        allowlist = config["index"].get("hidden_allowlist")
        for pattern in allowlist if isinstance(allowlist, list) else []:
            if isinstance(pattern, str):
                literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
                prefixes.append(
                    Path(literal).parent if not literal.endswith("/") else Path(literal)
                )
    if any(
        relative.is_relative_to(prefix) or prefix.is_relative_to(relative) for prefix in prefixes
    ):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", str(path)],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def annotate_navigation(data: Mapping[str, Any], metadata: dict[str, Any]) -> None:
    """Resolve each shell segment independently before rules see the aggregate."""
    segments = metadata.get("canonical_code_navigation_segments")
    if not isinstance(segments, list):
        segments = [dict(metadata)] if metadata.get("canonical_code_navigation_action") else []
    root, cwd = current_project_root(data), current_tool_cwd(data)
    for segment in segments:
        paths = segment.get("canonical_file_paths") or []
        base = cwd
        if segment.get("canonical_code_index_navigation"):
            project = segment.get("canonical_code_index_project")
            base = resolve_tool_path(project, cwd) if project else root or cwd
        resolved = [resolve_tool_path(p, base) for p in paths]
        segment["canonical_file_paths"] = [
            str(path) if path is not None else raw
            for raw, path in zip(paths, resolved, strict=True)
        ]
        segment["canonical_code_navigation_repo_scope"] = code_navigation_may_touch_project(
            paths,
            cwd=cwd,
            project_root=root,
        )
        segment["canonical_code_navigation_excluded"] = bool(resolved) and all(
            path is not None
            and (
                not code_navigation_may_touch_project([str(path)], cwd=cwd, project_root=root)
                or _ignored_target(path, root)
            )
            for path in resolved
        )
    metadata["canonical_code_navigation_segments"] = segments


def navigation_recovery(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Grant recovery only when the outcome is attributable to the requested scope."""
    batched = _batched_error_recovery(data)
    if batched:
        return batched
    if not data.get("canonical_code_index_navigation"):
        return []
    segments = data.get("canonical_code_navigation_segments") or []
    if len(segments) != 1:
        return []
    segment = segments[0]
    paths = segment.get("canonical_file_paths") or []
    if not paths:
        return []
    failed = data.get("is_error") or data.get("canonical_code_index_error")
    output = data.get("tool_output")
    if isinstance(output, Mapping):
        output = output.get("output", output)
    text = output if isinstance(output, str) else json.dumps(output)
    command = segment.get("canonical_code_index_command")
    empty_outline = command == "gcode outline" and any(
        line.startswith(diagnostic)
        for line in text.splitlines()
        for diagnostic in (
            "file has no indexed symbols in current project:",
            "file type has no AST parser support; `gcode outline` is AST-only:",
            "file not indexed in current project:",
        )
    )
    if command == "gcode outline" and not empty_outline:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None
        empty_outline = isinstance(payload, dict) and payload.get("total") == 0
    if not failed and not empty_outline:
        return []
    action = (
        "enumerate" if command == "gcode tree" else segment.get("canonical_code_navigation_action")
    )
    return [{"action": action, "paths": paths}]


def _batched_error_recovery(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recognize one structured failure per gcode invocation in a sequential batch.

    Gcode's failure contract is one JSON line per invocation. A failed aggregate
    exit alone proves nothing about earlier segments. Permit status inspection as
    the only non-gcode segment; arbitrary commands could manufacture error output.
    """
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return []
    command = tool_input.get("command") or tool_input.get("cmd")
    if not isinstance(command, str):
        return []
    parsed = parse_shell_command(command)
    if not parsed.operators or any(op not in {";", "\n"} for op in parsed.operators):
        return []
    gcode_count = 0
    for part in parsed.segments:
        if part and part[0] == "gcode":
            gcode_count += 1
        elif part[:2] != ("git", "status"):
            return []
    segments = data.get("canonical_code_navigation_segments") or []
    if (
        not gcode_count
        or len(segments) != gcode_count
        or any(not segment.get("canonical_code_index_navigation") for segment in segments)
    ):
        return []
    output = data.get("tool_output")
    if isinstance(output, Mapping):
        output = output.get("output")
    if not isinstance(output, str):
        return []
    errors = 0
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("error"), str)
            and isinstance(payload.get("message"), str)
        ):
            errors += 1
        else:
            return []
    if errors != gcode_count:
        return []
    return [
        {
            "action": "enumerate"
            if segment.get("canonical_code_index_command") == "gcode tree"
            else segment.get("canonical_code_navigation_action"),
            "paths": segment["canonical_file_paths"],
        }
        for segment in segments
        if segment.get("canonical_file_paths")
    ]


def navigation_requires_index(
    data: Mapping[str, Any],
    variables: Mapping[str, Any],
    action: str | None = None,
) -> bool:
    """Every enforced segment must qualify for its own exemption or recovery."""
    segments = data.get("canonical_code_navigation_segments") or [data]
    written = {
        str(resolve_tool_path(path, current_tool_cwd(data)) or path)
        for path in variables.get("turn_written_paths", [])
    }
    for segment in segments:
        operation = segment.get("canonical_code_navigation_action")
        if not operation or (action is not None and operation != action):
            continue
        if (
            segment.get("canonical_code_index_navigation")
            or not segment.get("canonical_code_navigation_broad")
            or segment.get("canonical_code_navigation_repo_scope") is False
            or segment.get("canonical_code_navigation_excluded")
            or segment.get("canonical_search_revision_scoped")
        ):
            continue
        if operation == "enumerate" and not segment.get(
            "canonical_code_navigation_gcode_supported"
        ):
            continue
        if (
            operation == "read"
            and segment.get("canonical_source_read_scope") == "line_range"
            and variables.get("code_index_navigation_used_this_turn")
        ):
            continue
        paths = segment.get("canonical_file_paths") or []
        if paths and set(paths).issubset(written):
            continue
        recovered = {
            path
            for record in variables.get("code_index_recoveries", [])
            if isinstance(record, Mapping) and record.get("action") == operation
            for path in record.get("paths", [])
        }
        if paths and all(
            any(
                path == target or (operation != "read" and Path(path).is_relative_to(target))
                for target in recovered
            )
            for path in paths
        ):
            continue
        return True
    return False
