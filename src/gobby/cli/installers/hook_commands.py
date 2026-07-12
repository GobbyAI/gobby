"""Shared helpers for rendering and identifying Gobby hook commands."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from gobby.utils.native_bin import resolve_native_bin_or_default

_GOBBY_OWNED_MARKER = "--gobby-owned"


def is_gobby_hook_command(command: str) -> bool:
    """Return whether a command string belongs to Gobby-managed hooks."""
    return _GOBBY_OWNED_MARKER in command


def config_contains_gobby_hook(node: Any) -> bool:
    """Recursively detect Gobby-managed hook commands in a config fragment."""
    if isinstance(node, str):
        return is_gobby_hook_command(node)

    if isinstance(node, list):
        return any(config_contains_gobby_hook(item) for item in node)

    if isinstance(node, dict):
        for field in ("command", "cmd", "script"):
            value = node.get(field)
            if isinstance(value, str) and is_gobby_hook_command(value):
                return True
        return any(
            config_contains_gobby_hook(value)
            for value in node.values()
            if isinstance(value, (dict, list))
        )

    return False


def remove_gobby_hook_handlers(
    groups: list[Any],
    *,
    is_gobby_hook: Callable[[Any], bool] = config_contains_gobby_hook,
) -> tuple[list[Any], bool]:
    """Remove Gobby handlers while retaining unrelated handlers and group metadata."""
    cleaned_groups: list[Any] = []
    removed = False

    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(handlers, list):
            cleaned_handlers = [handler for handler in handlers if not is_gobby_hook(handler)]
            removed |= len(cleaned_handlers) != len(handlers)
            if cleaned_handlers:
                cleaned_group = deepcopy(group)
                cleaned_group["hooks"] = cleaned_handlers
                cleaned_groups.append(cleaned_group)
            continue

        if is_gobby_hook(group):
            removed = True
        else:
            cleaned_groups.append(deepcopy(group))

    return cleaned_groups, removed


def merge_gobby_hook_groups(
    existing_groups: Any,
    gobby_groups: Any,
    *,
    is_gobby_hook: Callable[[Any], bool] = config_contains_gobby_hook,
) -> list[Any]:
    """Preserve user hook handlers, replace old Gobby handlers, and append current ones."""
    existing = (
        existing_groups
        if isinstance(existing_groups, list)
        else ([] if existing_groups is None else [existing_groups])
    )
    managed = (
        gobby_groups
        if isinstance(gobby_groups, list)
        else ([] if gobby_groups is None else [gobby_groups])
    )
    cleaned, _ = remove_gobby_hook_handlers(existing, is_gobby_hook=is_gobby_hook)
    return [*cleaned, *deepcopy(managed)]


def build_hook_command_prefix(
    hooks_dir: Path,
    *,
    ghook_bin: str | None = None,
) -> str:
    """Build the shared command prefix for hook templates.

    The returned prefix always includes `_GOBBY_OWNED_MARKER`, so removing
    `hooks_dir` would be a breaking API change without changing most runtime
    behavior. `hooks_dir` is retained for compatibility; hook commands no
    longer use it for Stop-specific wrapping.
    """
    resolved_ghook = ghook_bin or resolve_native_bin_or_default("ghook")
    return f"{shlex.quote(resolved_ghook)} {_GOBBY_OWNED_MARKER}"


def build_hook_command(
    cli_name: str,
    hook_type: str,
    hooks_dir: Path,
    *,
    ghook_bin: str | None = None,
) -> str:
    """Build the full raw ghook command for a CLI hook type."""
    prefix = build_hook_command_prefix(hooks_dir, ghook_bin=ghook_bin)
    return f"{prefix} --cli={cli_name} --type={hook_type}"


_GOBBY_HOOK_COMMAND_PLACEHOLDER = "__GOBBY_HOOK_COMMAND__"


def rewrite_hook_template_commands(
    hooks_config: dict[str, Any],
    *,
    cli_name: str,
    hooks_dir: Path,
    ghook_bin: str | None = None,
) -> dict[str, Any]:
    """Rewrite Gobby hook commands to the current ghook executable.

    The rewrite normalizes only the ``ghook --gobby-owned`` executable prefix so
    the resolved absolute binary is used. It deliberately **preserves each
    command's existing flags** (notably ``--cli`` and ``--type``): the template
    already encodes the correct native ``--type`` token for every CLI, and
    re-deriving ``--type`` from the PascalCase settings key is exactly what made
    Claude hooks resolve to ``NOTIFICATION``. Bare ``__GOBBY_HOOK_COMMAND__``
    placeholders (the Droid template) carry no flags, so they are filled from the
    template hook key via :func:`build_hook_command`. Non-Gobby commands are left
    untouched.
    """
    hooks = hooks_config.get("hooks")
    if not isinstance(hooks, dict):
        return hooks_config

    prefix = build_hook_command_prefix(hooks_dir, ghook_bin=ghook_bin)
    for hook_type, hook_config in hooks.items():
        placeholder_command = build_hook_command(
            cli_name,
            hook_type,
            hooks_dir,
            ghook_bin=ghook_bin,
        )
        _rewrite_commands(
            hook_config,
            prefix=prefix,
            placeholder_command=placeholder_command,
        )

    return hooks_config


def _rewrite_command_string(command: str, *, prefix: str, placeholder_command: str) -> str:
    """Rewrite a single command string while preserving its flags."""
    if command.strip() == _GOBBY_HOOK_COMMAND_PLACEHOLDER:
        # Bare placeholder (Droid template) — build the full command from the key.
        return placeholder_command
    if _is_legacy_gobby_hook_script(command):
        return placeholder_command
    if _GOBBY_OWNED_MARKER not in command:
        # Foreign / non-Gobby command — leave untouched.
        return command
    # Keep everything after the --gobby-owned marker (--cli, --type, and any
    # future flags); swap only the executable prefix up to and including it.
    suffix = command.split(_GOBBY_OWNED_MARKER, 1)[1]
    return f"{prefix}{suffix}"


def _is_legacy_gobby_hook_script(command: str) -> bool:
    """Return whether a command references an old direct Gobby hook script."""
    legacy_names = {"hook_dispatcher.py", "hook.py"}
    try:
        tokens = shlex.split(command.replace("\\", "/"))
    except ValueError:
        return False
    for token in tokens:
        filename = token.strip("\"'").rsplit("/", 1)[-1]
        if filename in legacy_names:
            return True
    return False


def _rewrite_commands(node: Any, *, prefix: str, placeholder_command: str) -> None:
    """Recursively rewrite command entries inside a hook template fragment."""
    if isinstance(node, list):
        for item in node:
            _rewrite_commands(item, prefix=prefix, placeholder_command=placeholder_command)
        return

    if not isinstance(node, dict):
        return

    for field in ("command", "cmd", "script"):
        value = node.get(field)
        if isinstance(value, str):
            node[field] = _rewrite_command_string(
                value,
                prefix=prefix,
                placeholder_command=placeholder_command,
            )

    for value in node.values():
        _rewrite_commands(value, prefix=prefix, placeholder_command=placeholder_command)
