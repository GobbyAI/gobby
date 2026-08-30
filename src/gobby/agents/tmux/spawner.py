"""TmuxSpawner — the sole terminal spawning backend for Gobby.

Creates tmux sessions on Gobby's isolated socket (``-L gobby``) via
:class:`TmuxSessionManager`.  Also provides :meth:`spawn_agent` which
builds the CLI command, environment variables, and prompt handling
previously owned by the terminal orchestration layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import time
from pathlib import Path

from gobby.agents.constants import get_terminal_env_vars
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths
from gobby.agents.sandbox_resolvers import get_sandbox_resolver
from gobby.agents.spawn_cache_policy import apply_spawn_cache_policy
from gobby.agents.spawners.auth_env import CLI_DENIED_AMBIENT_KEYS, terminal_env_passthrough
from gobby.agents.spawners.base import (
    SpawnResult,
    TerminalSpawnerBase,
    make_spawn_env,
)
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.agents.spawners.prompt_manager import MAX_ENV_PROMPT_LENGTH, create_prompt_file
from gobby.agents.tmux.session_manager import TmuxSessionInfo, TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.storage.terminals import AttachLocator
from gobby.terminals.runtime import InvalidSpawnKeyError
from gobby.utils.local_token import read_local_api_token

logger = logging.getLogger(__name__)
_SUPPORTED_AUTH_CLIS = frozenset({"claude", "codex", "grok", "qwen", "droid"})
_SPAWN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def validate_spawn_key(spawn_key: str) -> str:
    """Reject a spawn_key that tmux would have to rewrite."""
    if not spawn_key or not _SPAWN_KEY_RE.fullmatch(spawn_key):
        raise InvalidSpawnKeyError(spawn_key)
    rewritten = re.sub(r"[^a-zA-Z0-9_-]", "-", spawn_key)
    rewritten = re.sub(r"-{2,}", "-", rewritten).lstrip("-")
    if rewritten != spawn_key:
        raise InvalidSpawnKeyError(spawn_key)
    return spawn_key


def tmux_spawn_shell_and_env(
    command: list[str],
    env: dict[str, str] | None,
    auth_cli: str | None,
) -> tuple[str, dict[str, str]]:
    """Build the pane shell command and extra env TmuxSessionManager.create_session needs."""
    shell_cmd = shlex.join(command) if len(command) > 1 else command[0]
    cli = auth_cli or _infer_auth_cli(command)
    denied = CLI_DENIED_AMBIENT_KEYS.get(cli or "", frozenset())
    unset_names = ["VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", *sorted(denied)]
    shell_cmd = f"unset {' '.join(unset_names)}; {shell_cmd}"
    spawn_env = dict(env or {})
    if cli:
        for key, value in terminal_env_passthrough(cli).items():
            spawn_env.setdefault(key, value)
        for key in denied:
            spawn_env.pop(key, None)
    apply_spawn_cache_policy(spawn_env)
    clean_env = make_spawn_env(spawn_env)
    extra_env = {k: v for k, v in clean_env.items() if k in spawn_env}
    extra_env["VIRTUAL_ENV"] = ""
    extra_env["VIRTUAL_ENV_PROMPT"] = ""
    return shell_cmd, extra_env


def _infer_auth_cli(command: list[str]) -> str | None:
    """Infer the provider CLI from a command argv list."""
    if not command:
        return None
    candidates = [command[0]]
    if "--" in command:
        separator = command.index("--")
        if separator + 1 < len(command):
            candidates.append(command[separator + 1])
    for candidate in candidates:
        cli = Path(candidate).name.lower()
        if cli in _SUPPORTED_AUTH_CLIS:
            return cli
    return None


class TmuxSpawner(TerminalSpawnerBase):
    """Spawner that creates tmux sessions on Gobby's isolated socket.

    * Uses ``-L gobby`` by default (configurable via :class:`TmuxConfig`).
    * Delegates to :class:`TmuxSessionManager` for session lifecycle.
    * Stores ``tmux_session_name`` on :class:`SpawnResult` so the caller
      can start output streaming and register the name on the agent.
    """

    def __init__(
        self,
        config: TmuxConfig,
        session_manager: TmuxSessionManager | None = None,
    ) -> None:
        self._config = config
        self._session_manager = session_manager or TmuxSessionManager(self._config)

    @property
    def terminal_type(self) -> str:
        return "tmux"

    @property
    def session_manager(self) -> TmuxSessionManager:
        return self._session_manager

    def is_available(self) -> bool:
        if not self._config.enabled:
            return False
        return self._session_manager.is_available()

    async def spawn_async(
        self,
        command: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        title: str | None = None,
        auth_cli: str | None = None,
        spawn_key: str | None = None,
    ) -> SpawnResult:
        """Spawn a command inside a new tmux session."""
        if spawn_key is None:
            raise InvalidSpawnKeyError("")
        return await self._async_spawn(command, cwd, env, title, auth_cli, spawn_key=spawn_key)

    def spawn(
        self,
        command: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        title: str | None = None,
        auth_cli: str | None = None,
        spawn_key: str | None = None,
    ) -> SpawnResult:
        """Spawn a command inside a new tmux session for sync callers."""
        if spawn_key is None:
            raise InvalidSpawnKeyError("")
        return asyncio.run(
            self.spawn_async(command, cwd, env, title, auth_cli, spawn_key=spawn_key)
        )

    async def _async_spawn(
        self,
        command: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        title: str | None = None,
        auth_cli: str | None = None,
        *,
        spawn_key: str,
    ) -> SpawnResult:
        """Async implementation of spawn."""
        del title  # Display metadata is persisted on the terminal row, not the tmux name.
        session_name = validate_spawn_key(spawn_key)
        shell_cmd, extra_env = tmux_spawn_shell_and_env(command, env, auth_cli)

        try:
            info = await self._session_manager.create_session(
                name=session_name,
                command=shell_cmd,
                cwd=str(cwd),
                env=extra_env,
            )
        except Exception as e:
            return SpawnResult(
                success=False,
                message=f"Failed to spawn tmux session: {e}",
                error=str(e),
            )

        try:
            verified_info, verification_error, pane_output = await self._verify_live_pane(info.name)
        except Exception as e:
            with contextlib.suppress(Exception):
                await self._session_manager.kill_session(info.name, missing_ok=True)
            return SpawnResult(
                success=False,
                message=f"tmux session '{info.name}' failed live-pane verification",
                error=f"tmux session verification failed: {e}",
            )
        if verified_info is None:
            with contextlib.suppress(Exception):
                await self._session_manager.kill_session(info.name, missing_ok=True)
            error = verification_error or "tmux session verification failed"
            if pane_output:
                error = f"{error}\nPane output:\n{pane_output}"
            return SpawnResult(
                success=False,
                message=f"tmux session '{info.name}' failed live-pane verification",
                error=error,
            )

        attach_cmd = self._attach_command(verified_info.name)
        result = SpawnResult(
            success=True,
            message=(f"Spawned tmux session '{verified_info.name}' (attach: {attach_cmd})"),
            pid=verified_info.pane_pid,
            terminal_type=self.terminal_type,
            locator=AttachLocator(
                backend="tmux",
                frame_host_epoch="",
                pane_id=verified_info.pane_id,
            ),
            tmux_socket_name=self._config.socket_name,
            tmux_socket_path=self._config.socket_path,
        )
        return result

    async def _verify_live_pane(
        self, session_name: str
    ) -> tuple[TmuxSessionInfo | None, str | None, str | None]:
        """Verify tmux created a live pane and return its authoritative metadata."""
        deadline = time.monotonic() + 2.0
        last_error = f"tmux session '{session_name}' was not found"
        while True:
            info = await self._session_manager.get_session(session_name)
            if info is None:
                last_error = f"tmux session '{session_name}' was not found"
            elif info.pane_dead:
                output = await self._capture_failure_output(session_name)
                return None, f"tmux session '{session_name}' pane is dead", output
            elif info.pane_pid is None:
                last_error = f"tmux session '{session_name}' has no pane PID"
            else:
                return info, None, None

            if time.monotonic() >= deadline:
                output = (
                    await self._capture_failure_output(session_name)
                    if info is not None and info.pane_pid is None
                    else None
                )
                return None, last_error, output
            await asyncio.sleep(0.1)

    async def _capture_failure_output(self, session_name: str) -> str | None:
        try:
            output = await self._session_manager.capture_pane(session_name, lines=50)
        except Exception:
            logger.debug("Failed to capture tmux pane %r after exit", session_name, exc_info=True)
            return None
        if not output or not output.strip():
            return None
        return output.strip()[-4096:]

    def _attach_command(self, session_name: str) -> str:
        """Return a tmux attach command for the configured socket."""
        if self._config.socket_path:
            return f"tmux -S {shlex.quote(self._config.socket_path)} attach -t {session_name}"
        if self._config.socket_name:
            return f"tmux -L {self._config.socket_name} attach -t {session_name}"
        return f"tmux attach -t {session_name}"

    # ------------------------------------------------------------------
    # spawn_agent terminal orchestration
    # ------------------------------------------------------------------

    def spawn_agent(
        self,
        cli: str,
        cwd: str | Path,
        session_id: str,
        parent_session_id: str,
        agent_run_id: str,
        project_id: str,
        workflow_name: str | None = None,
        agent_depth: int = 1,
        max_agent_depth: int = 5,
        prompt: str | None = None,
        sandbox_config: SandboxConfig | None = None,
        *,
        spawn_key: str,
    ) -> SpawnResult:
        """Spawn a CLI agent in a new tmux session with Gobby env vars.

        Args:
        cli: CLI to run (e.g., "claude", "qwen", "codex").
            cwd: Working directory.
            session_id: Pre-created child session ID.
            parent_session_id: Parent session for context resolution.
            agent_run_id: Agent run record ID.
            project_id: Project ID.
            workflow_name: Optional workflow to activate.
            agent_depth: Current nesting depth.
            max_agent_depth: Maximum allowed depth.
            prompt: Optional initial prompt.
            sandbox_config: Optional sandbox configuration.

        Returns:
            SpawnResult with success status.
        """
        # Resolve sandbox configuration if enabled
        sandbox_args: list[str] | None = None
        sandbox_env: dict[str, str] = {}

        if sandbox_config and getattr(sandbox_config, "enabled", False):
            resolved_paths = compute_sandbox_paths(sandbox_config, str(cwd))
            resolver = get_sandbox_resolver(cli)
            sandbox_args, sandbox_env = resolver.resolve(sandbox_config, resolved_paths)

        # Build command with prompt as CLI argument
        command, cmd_env = build_cli_command(
            cli,
            prompt=prompt,
            session_id=session_id,
            auto_approve=True,
            working_directory=str(cwd) if cli == "codex" else None,
            sandbox_args=sandbox_args,
        )

        # Handle prompt for environment variables
        prompt_env: str | None = None
        prompt_file: str | None = None

        if prompt:
            if len(prompt) <= MAX_ENV_PROMPT_LENGTH:
                prompt_env = prompt
            else:
                prompt_file = create_prompt_file(prompt, session_id)

        # Build environment
        env = get_terminal_env_vars(
            session_id=session_id,
            parent_session_id=parent_session_id,
            agent_run_id=agent_run_id,
            project_id=project_id,
            workflow_name=workflow_name,
            agent_depth=agent_depth,
            max_agent_depth=max_agent_depth,
            prompt=prompt_env,
            prompt_file=prompt_file,
            operator_token=read_local_api_token(),
        )

        # Merge command builder env and sandbox env
        env.update(cmd_env)
        if sandbox_env:
            env.update(sandbox_env)

        # Set title (avoid colons/parentheses which some terminals misinterpret)
        title = f"gobby-{cli}-d{agent_depth}"

        return self.spawn(
            command=command,
            cwd=cwd,
            env=env,
            title=title,
            auth_cli=cli,
            spawn_key=spawn_key,
        )
