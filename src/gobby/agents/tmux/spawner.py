"""TmuxSpawner — the sole terminal spawning backend for Gobby.

Creates tmux sessions on Gobby's isolated socket (``-L gobby``) via
:class:`TmuxSessionManager`.  Also provides :meth:`spawn_agent` which
builds the CLI command, environment variables, and prompt handling
previously owned by the now-removed ``TerminalSpawner`` orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
import uuid
from pathlib import Path

from gobby.agents.constants import (
    GOBBY_SESSION_ID,
    UV_CACHE_DIR,
    ensure_agent_uv_cache_dir,
    get_terminal_env_vars,
)
from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths, get_sandbox_resolver
from gobby.agents.spawners.auth_env import terminal_env_passthrough
from gobby.agents.spawners.base import (
    SpawnResult,
    TerminalSpawnerBase,
    make_spawn_env,
)
from gobby.agents.spawners.command_builder import build_cli_command
from gobby.agents.spawners.prompt_manager import MAX_ENV_PROMPT_LENGTH, create_prompt_file
from gobby.agents.tmux.session_manager import TmuxSessionInfo, TmuxSessionManager
from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)
_SUPPORTED_AUTH_CLIS = frozenset({"claude", "codex", "gemini", "grok", "qwen", "droid"})


def _infer_auth_cli(command: list[str]) -> str | None:
    """Infer the provider CLI from a command argv list."""
    if not command:
        return None
    cli = Path(command[0]).name.lower()
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
        config: TmuxConfig | None = None,
    ) -> None:
        self._config = config or TmuxConfig()
        self._session_manager = TmuxSessionManager(self._config)

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

    def spawn(
        self,
        command: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        title: str | None = None,
    ) -> SpawnResult:
        """Spawn a command inside a new tmux session (sync wrapper).

        The heavy lifting is async; we bridge to the running event loop
        or create a temporary one for sync callers.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    self._async_spawn(command, cwd, env, title),
                )
                return future.result(timeout=30)
        else:
            return asyncio.run(self._async_spawn(command, cwd, env, title))

    async def _async_spawn(
        self,
        command: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        title: str | None = None,
    ) -> SpawnResult:
        """Async implementation of spawn."""
        suffix = uuid.uuid4().hex[:8]
        base = title or f"{self._config.session_prefix}-{int(time.time())}"
        session_name = f"{base}-{suffix}"
        # Sanitise (TmuxSessionManager also sanitises, but normalise here
        # so the name we return is consistent).
        session_name = re.sub(r"[^a-zA-Z0-9_-]", "-", session_name)
        session_name = re.sub(r"-{2,}", "-", session_name)
        session_name = session_name.lstrip("-")
        if not session_name:
            session_name = "gobby-session"

        shell_cmd = shlex.join(command) if len(command) > 1 else command[0]

        # Unset VIRTUAL_ENV in the tmux session to avoid uv warnings
        # when the agent runs in a worktree/clone with a different CWD.
        # tmux inherits the daemon's env; -e can only SET vars, not unset.
        shell_cmd = f"unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT; {shell_cmd}"

        spawn_env = dict(env or {})
        if cli := _infer_auth_cli(command):
            for key, value in terminal_env_passthrough(cli).items():
                spawn_env.setdefault(key, value)
        if UV_CACHE_DIR not in spawn_env:
            spawn_env[UV_CACHE_DIR] = ensure_agent_uv_cache_dir(
                spawn_env.get(GOBBY_SESSION_ID) or "unknown-session"
            )

        # Merge env with a clean spawn env
        clean_env = make_spawn_env(spawn_env)
        # Only pass the *extra* env vars that differ from os.environ
        extra_env = {k: v for k, v in clean_env.items() if k in spawn_env}

        # tmux -e can only SET vars, not unset them.  Override to empty so
        # the session doesn't inherit the daemon's VIRTUAL_ENV (causes uv
        # "does not match project environment" errors in worktrees/clones).
        # The shell-level unset above provides full removal; this prevents
        # inheritance if the unset doesn't take effect (e.g. login shells
        # that re-source profiles).
        extra_env["VIRTUAL_ENV"] = ""
        extra_env["VIRTUAL_ENV_PROMPT"] = ""

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
            verified_info, verification_error = await self._verify_live_pane(info.name)
        except Exception as e:
            return SpawnResult(
                success=False,
                message=f"tmux session '{info.name}' failed live-pane verification",
                error=f"tmux session verification failed: {e}",
            )
        if verified_info is None:
            return SpawnResult(
                success=False,
                message=f"tmux session '{info.name}' failed live-pane verification",
                error=verification_error or "tmux session verification failed",
            )

        attach_cmd = self._attach_command(verified_info.name)
        result = SpawnResult(
            success=True,
            message=(f"Spawned tmux session '{verified_info.name}' (attach: {attach_cmd})"),
            pid=verified_info.pane_pid,
            terminal_type=self.terminal_type,
            tmux_session_name=verified_info.name,
            tmux_socket_name=self._config.socket_name,
            tmux_socket_path=self._config.socket_path,
        )
        return result

    async def _verify_live_pane(
        self, session_name: str
    ) -> tuple[TmuxSessionInfo | None, str | None]:
        """Verify tmux created a live pane and return its authoritative metadata."""
        deadline = time.monotonic() + 2.0
        last_error = f"tmux session '{session_name}' was not found"
        while True:
            info = await self._session_manager.get_session(session_name)
            if info is None:
                last_error = f"tmux session '{session_name}' was not found"
            elif info.pane_dead:
                return None, f"tmux session '{session_name}' pane is dead"
            elif info.pane_pid is None:
                last_error = f"tmux session '{session_name}' has no pane PID"
            else:
                return info, None

            if time.monotonic() >= deadline:
                return None, last_error
            await asyncio.sleep(0.1)

    def _attach_command(self, session_name: str) -> str:
        """Return a tmux attach command for the configured socket."""
        if self._config.socket_path:
            return f"tmux -S {shlex.quote(self._config.socket_path)} attach -t {session_name}"
        if self._config.socket_name:
            return f"tmux -L {self._config.socket_name} attach -t {session_name}"
        return f"tmux attach -t {session_name}"

    # ------------------------------------------------------------------
    # spawn_agent  (moved from the former TerminalSpawner orchestrator)
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
    ) -> SpawnResult:
        """Spawn a CLI agent in a new tmux session with Gobby env vars.

        Args:
            cli: CLI to run (e.g., "claude", "gemini", "codex").
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
        )
