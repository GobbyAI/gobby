"""Normalize provider tool arguments for transcript-derived task evidence."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_COMMAND_KEYS = ("cmd", "command", "script")


def extract_command(arguments: dict[str, Any]) -> str:
    """Return the first non-empty shell command exposed by a provider tool."""
    for key in _COMMAND_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def exact_patch(arguments: Any) -> str | None:
    """Extract a literal patch, never infer execution of arbitrary wrapper code."""
    if isinstance(arguments, str):
        return arguments if arguments.startswith("*** Begin Patch\n") else None
    if not isinstance(arguments, dict):
        return None
    arguments = {key: value for key, value in arguments.items() if key not in {"cwd", "workdir"}}
    if len(arguments) != 1:
        return None
    for key in ("raw", "patch", "input"):
        value = arguments.get(key)
        if isinstance(value, str) and value.startswith("*** Begin Patch\n"):
            return value
    code = arguments.get("code") or arguments.get("raw")
    if isinstance(code, str):
        match = re.fullmatch(
            r'\s*text\(await tools\.apply_patch\(("(?:[^"\\]|\\.)*")\)\);?\s*', code
        )
        if match:
            try:
                return exact_patch(json.loads(match[1]))
            except json.JSONDecodeError:
                return None
    return None


def patch_sources(
    arguments: Any, root: str | None, sources: dict[str, str | None]
) -> dict[str, str] | None:
    """Reconstruct only exact additions or uniquely matched ordered update hunks."""
    patch = exact_patch(arguments)
    if patch is None:
        return None
    lines = patch.splitlines(keepends=True)
    if not lines or lines.pop(0).rstrip("\r\n") != "*** Begin Patch":
        return None
    if not lines or lines.pop().rstrip("\r\n") != "*** End Patch":
        return None
    result: dict[str, str] = {}
    while lines:
        header = lines.pop(0).rstrip("\r\n")
        if not header.startswith(("*** Add File: ", "*** Update File: ")):
            return None
        path = Path(header.split(": ", 1)[1])
        if not path.is_absolute():
            if root is None:
                return None
            path = Path(root, path)
        path = path.resolve()
        if root is not None and not path.is_relative_to(Path(root).resolve()):
            return None
        key = str(path)
        if key in result:
            return None
        body: list[str] = []
        while lines and not lines[0].startswith("*** "):
            body.append(lines.pop(0))
        if header.startswith("*** Add File: "):
            if (
                sources.get(key) is not None
                or not body
                or any(not line.startswith("+") for line in body)
            ):
                return None
            result[key] = "".join(line[1:] for line in body)
            continue
        before = sources.get(key)
        if before is None or not body:
            return None
        after, offset = before, 0
        while body:
            marker = body.pop(0).rstrip("\r\n")
            if marker != "@@":
                return None
            old: list[str] = []
            new: list[str] = []
            while body and not body[0].startswith("@@"):
                line = body.pop(0)
                if not line or line[0] not in " +-":
                    return None
                if line[0] in " -":
                    old.append(line[1:])
                if line[0] in " +":
                    new.append(line[1:])
            original, replacement = "".join(old), "".join(new)
            if not original or after.count(original) != 1:
                return None
            index = after.find(original)
            if index < offset:
                return None
            after = after[:index] + replacement + after[index + len(original) :]
            offset = index + len(replacement)
        result[key] = after
    return result or None


def patch_write_input(arguments: Any, root: str | None) -> dict[str, Any] | None:
    """Project an exact patch against current bytes for the pre-write stub check."""
    patch = exact_patch(arguments)
    if patch is None:
        return None
    sources: dict[str, str | None] = {}
    for name in re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", patch, re.MULTILINE):
        path = Path(name)
        if not path.is_absolute():
            if root is None:
                return None
            path = Path(root, path)
        try:
            sources[str(path.resolve())] = path.read_text() if path.exists() else None
        except (OSError, UnicodeError):
            return None
    projected = patch_sources(arguments, root, sources)
    if projected is None:
        return None
    return {"changes": [{"file_path": path, "content": text} for path, text in projected.items()]}


def normalize_tool_name(tool_name: str) -> str:
    """Reduce provider-qualified tool names to their normalized basename."""
    normalized = tool_name.casefold().replace("-", "_")
    for separator in ("__", ".", "/"):
        if separator in normalized:
            normalized = normalized.rsplit(separator, 1)[-1]
    return normalized


def tool_workdir(arguments: dict[str, Any]) -> str | None:
    """Return the absolute working directory a tool call names, if any."""
    for key in ("workdir", "cwd"):
        cwd = arguments.get(key)
        if isinstance(cwd, str) and os.path.isabs(cwd):
            return cwd
    return None


__all__ = [
    "exact_patch",
    "extract_command",
    "normalize_tool_name",
    "patch_sources",
    "patch_write_input",
    "tool_workdir",
]
