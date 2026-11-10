"""Normal-login provider launch with isolated Gobby routing."""

from pathlib import Path

_NORMAL_ENV = (
    "HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "PATH",
    "TERM",
    "LANG",
    "LC_ALL",
    "SHELL",
    "USER",
    "LOGNAME",
)
_PARENT_ENV = (
    "GOBBY_SESSION_ID",
    "GOBBY_PARENT_SESSION_ID",
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
    "GOBBY_PORT",
    "GOBBY_DAEMON_PORT",
    "GOBBY_MACHINE_ID",
    "GOBBY_HOOKS_DISABLED",
    "GOBBY_SOURCE",
    "GOBBY_IS_SPAWNED_AGENT",
)


def seat_environment(ambient: dict[str, str], *, home: Path, port: int) -> dict[str, str]:
    # No provider files or credentials are inspected. Preserve ordinary config locations.
    env = {name: ambient[name] for name in _NORMAL_ENV if name in ambient}
    env.update(
        GOBBY_HOME=str(home),
        GOBBY_DAEMON_URL=f"http://127.0.0.1:{port}",
        GOBBY_TEST_PROTECT="1",
    )
    return env


def provider_launch(
    command: list[str], env: dict[str, str], *, provider: str | None = None
) -> list[str]:
    # Keep the terminal host's freshly assigned terminal/pane identity. The daemon's
    # sealed environment contains no operator keys; never serialize a credential to argv.
    prefix = ["/usr/bin/env"]
    for name in _PARENT_ENV:
        prefix.extend(["-u", name])
    if provider == "codex":
        from gobby.agents.spawn_executor_support import _codex_runtime_config_overrides

        arguments = [
            arg for value in _codex_runtime_config_overrides(None, env) for arg in ("-c", value)
        ]
        command = [command[0], *arguments, *command[1:]]
    return [*prefix, *(f"{name}={value}" for name, value in sorted(env.items())), *command]
