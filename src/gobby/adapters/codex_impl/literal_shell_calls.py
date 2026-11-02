"""Static shell argument provenance, independent of execution receipt correlation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl.cell_shell_reach import may_reach_shell, static_code

_CALL = re.compile(r"\btools\.exec_command\s*\(")
_REPEATED = re.compile(r"\b(?:do|for|while|function)\b|=>|\.(?:forEach|map|reduce)\s*\(")
_KEY = re.compile(r'("(?:\\.|[^"\\])*"|[$A-Za-z_][$\w]*)\s*:')


def literal_shell_calls(arguments: Any) -> list[dict[str, Any]] | None:
    """Read all statically invoked literal shells; batch members need explicit absolute cwd."""
    if not isinstance(arguments, str):
        return None
    # Known direct shells are inspected below; reject every other dynamic shell path.
    if may_reach_shell(_CALL.sub("tools.literal_shell(", arguments)):
        return None
    code = static_code(arguments)
    if code is None or _REPEATED.search(code):
        return None
    matches = list(_CALL.finditer(code))
    calls = []
    for match in matches:
        fields = _literal_arguments(arguments[match.end() :].lstrip())
        if fields is None:
            return None
        command = fields.get("cmd")
        if not isinstance(command, str) or not command:
            return None
        workdir = fields.get("workdir")
        if "workdir" in fields and (not isinstance(workdir, str) or not workdir):
            return None
        if len(matches) > 1 and (not isinstance(workdir, str) or not Path(workdir).is_absolute()):
            return None
        calls.append(fields)
    return calls or None


def _literal_arguments(source: str) -> dict[str, Any] | None:
    """Decode a JS object with JSON literal values and no overwritten or computed fields."""
    if not source.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    fields: dict[str, Any] = {}
    source = source[1:].lstrip()
    while source and not source.startswith("}"):
        key_match = _KEY.match(source)
        if key_match is None:
            return None
        raw_key = key_match[1]
        try:
            key = json.loads(raw_key) if raw_key.startswith('"') else raw_key
        except ValueError:
            return None
        if key in fields:
            return None
        source = source[key_match.end() :].lstrip()
        try:
            value, end = decoder.raw_decode(source)
        except ValueError:
            return None
        fields[key] = value
        source = source[end:].lstrip()
        if source.startswith(","):
            source = source[1:].lstrip()
        elif not source.startswith("}"):
            return None
    return fields if source.startswith("}") and source[1:].lstrip().startswith(")") else None
