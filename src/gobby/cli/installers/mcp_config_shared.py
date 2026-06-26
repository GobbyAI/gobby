"""Shared MCP installer config helpers."""

import logging
import re
import sys
import time as _time_module
from collections.abc import Callable
from pathlib import Path
from shutil import copy2 as _copy2
from typing import Any, cast

_FACADE_MODULE = "gobby.cli.installers.mcp_config"
_LOGGER = logging.getLogger(_FACADE_MODULE)
_ORIGINAL_COPY2 = _copy2

_GOBBY_MCP_COMMAND = "gobby"
_GOBBY_MCP_ARGS = ["mcp-server"]
_CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC: int = 360


def _facade_attr(name: str, default: Any) -> Any:
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is None:
        return default
    return getattr(facade, name, default)


def _facade_copy2(src: Path, dst: Path) -> None:
    copy_func = cast(Callable[[Path, Path], Any], _copy2)
    if _copy2 is _ORIGINAL_COPY2:
        copy_func = cast(Callable[[Path, Path], Any], _facade_attr("copy2", _copy2))
    copy_func(src, dst)


def _facade_time() -> Any:
    return _facade_attr("time", _time_module)


def _facade_logger() -> logging.Logger:
    return cast(logging.Logger, _facade_attr("logger", _LOGGER))


def _remove_toml_table_block(existing_text: str, *, table_prefix: str) -> str:
    """Remove a TOML table and nested subtables while preserving trailing comments."""

    header_re = re.compile(r"(?m)^[ \t]*\[(?P<header>[^\]\n]+)\][ \t]*(?:#.*)?\n?")
    headers = list(header_re.finditer(existing_text))
    if not headers:
        return existing_text

    target_prefix = f"{table_prefix}."
    rebuilt: list[str] = []
    cursor = 0
    index = 0

    while index < len(headers):
        header = headers[index].group("header").strip()
        if header != table_prefix and not header.startswith(target_prefix):
            index += 1
            continue

        run_start = headers[index].start()
        next_index = index + 1
        while next_index < len(headers):
            next_header = headers[next_index].group("header").strip()
            if next_header == table_prefix or next_header.startswith(target_prefix):
                next_index += 1
                continue
            break

        block_end = headers[next_index].start() if next_index < len(headers) else len(existing_text)
        block = existing_text[run_start:block_end]
        preserved_suffix = _trailing_blank_or_comment_lines(block)

        rebuilt.append(existing_text[cursor:run_start])
        rebuilt.append(preserved_suffix)
        cursor = block_end
        index = next_index

    rebuilt.append(existing_text[cursor:])
    return "".join(rebuilt)


def _trailing_blank_or_comment_lines(text: str) -> str:
    lines = text.splitlines(keepends=True)
    suffix: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            suffix.append(line)
            continue
        break
    suffix.reverse()
    return "".join(suffix)


def _command_basename(command: Any) -> str | None:
    if not isinstance(command, str):
        return None
    normalized = command.replace("\\", "/").rstrip("/")
    if not normalized:
        return None
    return normalized.rsplit("/", maxsplit=1)[-1]


def _toml_string_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return None
    try:
        return [str(item) for item in value]
    except TypeError:
        return None


def _is_current_gobby_mcp_server_config(server_config: Any) -> bool:
    args = _toml_string_list(server_config.get("args"))
    return _command_basename(server_config.get("command")) == _GOBBY_MCP_COMMAND and args == [
        *_GOBBY_MCP_ARGS
    ]


def _needs_codex_gobby_mcp_tool_timeout(server_config: Any) -> bool:
    try:
        configured_timeout = float(server_config.get("tool_timeout_sec"))
    except (TypeError, ValueError):
        return True
    return configured_timeout < _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC


def _is_repairable_stale_gobby_mcp_server_config(server_config: Any) -> bool:
    if _command_basename(server_config.get("command")) != "uv":
        return False

    args = _toml_string_list(server_config.get("args"))
    if not args or len(args) < 3:
        return False
    if args[0] != "run" or args[-2:] != [_GOBBY_MCP_COMMAND, *_GOBBY_MCP_ARGS]:
        return False

    middle = args[1:-2]
    return middle == [] or (len(middle) == 2 and middle[0] == "--directory")


def _repair_stale_gobby_mcp_server_toml(
    existing_text: str,
    *,
    server_name: str,
) -> tuple[str | None, str | None]:
    """Return updated TOML text for Gobby MCP entries that need repair."""
    import tomlkit

    try:
        config = tomlkit.parse(existing_text)
    except tomlkit.exceptions.ParseError as e:
        return None, f"Failed to parse TOML {server_name} MCP config: {e}"

    mcp_servers = config.get("mcp_servers")
    if not hasattr(mcp_servers, "get"):
        return None, None

    server_config = cast(Any, mcp_servers).get(server_name)
    if not hasattr(server_config, "get"):
        return None, None

    updates: dict[str, Any] = {}
    if not _is_current_gobby_mcp_server_config(
        server_config
    ) and _is_repairable_stale_gobby_mcp_server_config(server_config):
        updates["command"] = _GOBBY_MCP_COMMAND
        updates["args"] = [*_GOBBY_MCP_ARGS]

    if _needs_codex_gobby_mcp_tool_timeout(server_config):
        updates["tool_timeout_sec"] = _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC

    if not updates:
        return None, None

    for key, value in updates.items():
        server_config[key] = value
    return tomlkit.dumps(config), None
