"""Per-CLI terminal env policy for native spawns.

The gterm host hands every child the environment it inherited from the daemon that
started it, and its spawn env is set-only, so a key the CLI must never see is
removed by ``/usr/bin/env -u``. env execs the program in place, so the pane pid
stays the provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from gobby.agents.credential_inventory import CLI_DENIED_AMBIENT_KEYS, denied_ambient_keys
from gobby.agents.spawners.auth_env import CLI_ENV_ALLOWLIST, terminal_env_passthrough

_KNOWN_CLIS = frozenset(CLI_ENV_ALLOWLIST) | frozenset(CLI_DENIED_AMBIENT_KEYS)
_BLANKED_KEYS = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")


def infer_cli(command: Sequence[str]) -> str | None:
    """Name the provider CLI from argv, looking past an srt wrapper's ``--``."""
    if not command:
        return None
    candidates = [command[0]]
    if "--" in command:
        separator = command.index("--")
        if separator + 1 < len(command):
            candidates.append(command[separator + 1])
    for candidate in candidates:
        cli = Path(candidate).name.lower()
        if cli in _KNOWN_CLIS:
            return cli
    return None


def apply_native_env_policy(
    command: Sequence[str],
    env: Mapping[str, str] | None,
    auth_cli: str | None,
) -> tuple[list[str], dict[str, str]]:
    """Return the argv and env a native spawn sends, with the CLI's env policy applied."""
    argv = list(command)
    spawn_env = dict(env or {})
    cli = auth_cli or infer_cli(command)
    denied: tuple[str, ...] = ()
    if cli:
        for key, value in terminal_env_passthrough(cli).items():
            spawn_env.setdefault(key, value)
        denied = denied_ambient_keys(cli)
    for key in denied:
        spawn_env.pop(key, None)
    for key in _BLANKED_KEYS:
        spawn_env[key] = ""
    if denied:
        unset = [arg for key in denied for arg in ("-u", key)]
        argv = ["/usr/bin/env", *unset, "--", *argv]
    return argv, spawn_env


__all__ = ["apply_native_env_policy", "infer_cli"]
