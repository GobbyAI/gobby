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
from gobby.hooks.tool_outcomes import tool_outcome_from_data

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
# Failures raised before gcode consults the index: gcore GrantError::cli_code
# (crates/gcore/src/grant/mod.rs) and checkout resolution (crates/gcode/src/cli_error.rs).
# Every navigation in that checkout fails the same way until the environment is repaired.
_OUTAGE_ERROR_CODES = frozenset(
    {
        "daemon_required",
        "expired",
        "schema_mismatch",
        "deployment_mismatch",
        "api_contract_mismatch",
        "payload_skew",
        "remote_endpoint",
        "config_revision_mismatch",
        "revoked",
        "timeout",
        "malformed",
        "io",
        "project_required",
        "checkout_required",
        "checkout_mismatch",
    }
)
# Shell output carriers: Claude stdout/stderr and failure `error`, Codex and web-chat
# `output`, Grok `output_for_prompt`, Droid chat `error.message`.
_OUTPUT_TEXT_FIELDS = ("output", "stdout", "stderr", "output_for_prompt", "error", "message")
_EMPTY_OUTLINE_DIAGNOSTICS = (
    "file has no indexed symbols in current project:",
    "file type has no AST parser support; `gcode outline` is AST-only:",
    "file not indexed in current project:",
)
# Separator arguments that could print a forged JSON line through escapes or expansion.
_SEPARATOR_UNSAFE_CHARS = frozenset("{\\$`")


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


def annotate_navigation_outcome(data: dict[str, Any]) -> None:
    """Record gcode attempts, verified results, and scopes that fail open.

    Hooks cannot report every failure: Droid emits no hook for a nonzero exit and
    AGY's PostToolUse carries no output. An attempted scope therefore stays open
    until verified gcode output arrives. A reported failure opens its scope for the
    turn, since Codex app-server raises no pre-tool event for auto-approved commands.
    Typed outages open the whole checkout.
    """
    for field in (
        "canonical_code_index_attempts",
        "canonical_code_index_verified",
        "canonical_code_index_recovery",
    ):
        data.pop(field, None)
    segments = [
        segment
        for segment in data.get("canonical_code_navigation_segments") or []
        if segment.get("canonical_code_index_navigation") and segment.get("canonical_file_paths")
    ]
    if not segments:
        return
    attempts = [_scope(segment) for segment in segments]
    text = _output_text(data)
    codes = _gcode_error_codes(text)
    failed = bool(codes) or tool_outcome_from_data(data).succeeded is False
    recovery: list[dict[str, Any]] = []
    if _OUTAGE_ERROR_CODES.intersection(codes) and _output_is_gcode_only(data):
        recovery = [{"action": "outage", "paths": _outage_roots(data, segments)}]
    elif failed or (len(segments) == 1 and _is_empty_outline(segments[0], text)):
        recovery = attempts
    data["canonical_code_index_attempts"] = attempts
    data["canonical_code_index_verified"] = [] if failed or not text.strip() else attempts
    data["canonical_code_index_recovery"] = recovery


def _scope(segment: Mapping[str, Any]) -> dict[str, Any]:
    action = (
        "enumerate"
        if segment.get("canonical_code_index_command") == "gcode tree"
        else segment.get("canonical_code_navigation_action")
    )
    return {"action": action, "paths": list(segment["canonical_file_paths"])}


def _output_text(data: Mapping[str, Any]) -> str:
    """Join shell output from every provider carrier into gcode's line contract."""
    texts: list[str] = []
    for value in (data.get("tool_output"), data.get("error"), data.get("error_details")):
        if isinstance(value, str):
            texts.append(value)
        elif _is_gcode_error(value):
            texts.append(json.dumps(value))
        elif isinstance(value, Mapping):
            texts.extend(
                item for field in _OUTPUT_TEXT_FIELDS if isinstance(item := value.get(field), str)
            )
    return "\n".join(texts)


def _is_gcode_error(payload: object) -> bool:
    return (
        isinstance(payload, Mapping)
        and isinstance(payload.get("error"), str)
        and isinstance(payload.get("message"), str)
    )


def _gcode_error_codes(text: str) -> list[str]:
    """Return error codes from gcode's one-line JSON failure contract."""
    codes: list[str] = []
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if _is_gcode_error(payload):
            codes.append(payload["error"])
    return codes


def _output_is_gcode_only(data: Mapping[str, Any]) -> bool:
    """Only gcode may print the lines that open a checkout; other output could forge them.

    head/tail qualify only as filters (options and counts): a file operand prints file text.
    """
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    if not isinstance(command, str):
        return False
    return all(
        part[0] == "gcode"
        or part[:2] == ("git", "status")
        or (
            part[0] in {"head", "tail"}
            and all(arg.startswith("-") or arg.lstrip("+").isdigit() for arg in part[1:])
        )
        or (part[0] == "echo" and not _SEPARATOR_UNSAFE_CHARS.intersection("".join(part[1:])))
        for part in parse_shell_command(command).segments
        if part
    )


def _outage_roots(data: Mapping[str, Any], segments: list[Mapping[str, Any]]) -> list[str]:
    cwd = current_tool_cwd(data)
    roots = {
        resolve_tool_path(project, cwd)
        if (project := segment.get("canonical_code_index_project"))
        else current_project_root(data) or cwd
        for segment in segments
    }
    return sorted(str(root) for root in roots if root is not None)


def _is_empty_outline(segment: Mapping[str, Any], text: str) -> bool:
    """A successful outline without symbols leaves Read as the only source view."""
    if segment.get("canonical_code_index_command") != "gcode outline":
        return False
    if any(line.startswith(_EMPTY_OUTLINE_DIAGNOSTICS) for line in text.splitlines()):
        return True
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("total") == 0


def navigation_requires_index(
    data: Mapping[str, Any],
    variables: Mapping[str, Any],
    action: str | None = None,
    *,
    broad_only: bool = False,
) -> bool:
    """Every enforced segment must qualify for its own exemption or recovery."""
    segments = data.get("canonical_code_navigation_segments") or [data]
    written = {
        str(resolve_tool_path(path, current_tool_cwd(data)) or path)
        for path in variables.get("turn_written_paths", [])
    }
    opened = _opened_scopes(variables)
    for segment in segments:
        operation = segment.get("canonical_code_navigation_action")
        if not operation or (action is not None and operation != action):
            continue
        if broad_only and not segment.get("canonical_code_navigation_broad"):
            continue
        if (
            segment.get("canonical_code_index_navigation")
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
            not broad_only
            and operation == "read"
            and segment.get("canonical_source_read_scope") == "line_range"
            and variables.get("code_index_navigation_used_this_turn")
        ):
            continue
        paths = segment.get("canonical_file_paths") or []
        if paths and set(paths).issubset(written):
            continue
        if paths and all(
            any(_opens(record, operation, path) for record in opened) for path in paths
        ):
            continue
        return True
    return False


def _opened_scopes(variables: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Recovered scopes plus gcode attempts that never produced verified output."""
    attempts = variables.get("code_index_attempts") or []
    verified = variables.get("code_index_verified") or []
    unverified = [record for record in attempts if attempts.count(record) > verified.count(record)]
    return [
        record
        for record in [*(variables.get("code_index_recoveries") or []), *unverified]
        if isinstance(record, Mapping)
    ]


def _opens(record: Mapping[str, Any], operation: str, path: str) -> bool:
    targets = record.get("paths") or []
    if record.get("action") == "outage":
        return any(Path(path).is_relative_to(target) for target in targets)
    return record.get("action") == operation and any(
        path == target or (operation != "read" and Path(path).is_relative_to(target))
        for target in targets
    )
