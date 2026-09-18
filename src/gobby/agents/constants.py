"""
Constants for agent spawning and terminal mode.

This module defines environment variables used to pass context to
spawned terminal processes. When an agent spawns a child in terminal
mode, these environment variables are set in the child process.
"""

import hashlib
import re
import tempfile
from pathlib import Path

from gobby.paths import get_gobby_home
from gobby.utils.local_token import GOBBY_AGENT_API_TOKEN_ENV, issue_agent_api_token
from gobby.utils.machine_id import get_machine_id

# ============================================================================
# Terminal Mode Environment Variables
# ============================================================================
# These environment variables are set when spawning a terminal-mode agent.
# The child CLI process reads these to pick up its prepared state.
# ============================================================================

# Session identifier for the pre-created child session
# The spawned CLI uses this to connect to its session via hooks
GOBBY_SESSION_ID = "GOBBY_SESSION_ID"

# Terminal and pane identity, scoped to the process it names. The native
# runtime sets GOBBY_TERMINAL_ID on every spawn; workspace pane spawns add the
# pane names (GOBBY_PANE_REF is `n1:w1:t2:p3`). The daemon pops all of them
# from its own environment at startup so no child inherits a pane's identity.
GOBBY_TERMINAL_ID = "GOBBY_TERMINAL_ID"
GOBBY_NODE_ID = "GOBBY_NODE_ID"
GOBBY_NODE_REF = "GOBBY_NODE_REF"
GOBBY_WORKSPACE_ID = "GOBBY_WORKSPACE_ID"
GOBBY_TAB_ID = "GOBBY_TAB_ID"
GOBBY_PANE_ID = "GOBBY_PANE_ID"
GOBBY_PANE_REF = "GOBBY_PANE_REF"
IDENTITY_ENV_VARS = (
    GOBBY_TERMINAL_ID,
    GOBBY_NODE_ID,
    GOBBY_NODE_REF,
    GOBBY_WORKSPACE_ID,
    GOBBY_TAB_ID,
    GOBBY_PANE_ID,
    GOBBY_PANE_REF,
)

# Parent session identifier for context resolution
# Used to look up parent session for context injection
GOBBY_PARENT_SESSION_ID = "GOBBY_PARENT_SESSION_ID"

# Agent run record identifier
# Links the terminal process back to its agent_runs record
GOBBY_AGENT_RUN_ID = "GOBBY_AGENT_RUN_ID"

# Run-bound daemon capability; never contains the operator's local CLI token.
GOBBY_AGENT_API_TOKEN = GOBBY_AGENT_API_TOKEN_ENV

# Workflow name to activate on session start
# The hook reads this and activates the workflow for the session
GOBBY_WORKFLOW_NAME = "GOBBY_WORKFLOW_NAME"

# Project identifier for the session
# Used for project-scoped operations
GOBBY_PROJECT_ID = "GOBBY_PROJECT_ID"

# Current agent nesting depth
# 0 = human-initiated, 1+ = agent-spawned
GOBBY_AGENT_DEPTH = "GOBBY_AGENT_DEPTH"

# Maximum allowed agent depth
# Prevents infinite nesting
GOBBY_MAX_AGENT_DEPTH = "GOBBY_MAX_AGENT_DEPTH"

# Initial prompt for the agent (short prompts only)
# For longer prompts, use GOBBY_PROMPT_FILE instead
GOBBY_PROMPT = "GOBBY_PROMPT"

# Path to file containing initial prompt (for long prompts)
# Takes precedence over GOBBY_PROMPT if both are set
GOBBY_PROMPT_FILE = "GOBBY_PROMPT_FILE"

# uv cache path for validation commands run by spawned agents.
UV_CACHE_DIR = "UV_CACHE_DIR"

# Cargo home path for Rust validation commands run by sandboxed spawned agents.
CARGO_HOME = "CARGO_HOME"

# Shared per-project cargo build directory (see gobby.agents.cargo_target).
CARGO_TARGET_DIR = "CARGO_TARGET_DIR"

# How long Claude Code may block its first turn waiting for an `--mcp-config`
# server it has to wait for. Claude Code's own default is 5s, and a cold
# `uv run ... gobby mcp-server` inside an agent sandbox has been measured past
# 7s on a loaded machine, so at the default an agent can reach the model before
# its Gobby tools exist. Overshooting costs nothing: the wait ends as soon as
# the handshake lands.
MCP_CONNECT_TIMEOUT_MS = "MCP_CONNECT_TIMEOUT_MS"
MCP_CONNECT_TIMEOUT_MS_VALUE = "60000"

# Claude Code's connection deadline for every MCP server it launches, whichever
# config scope names it (its own default is 30s). Beside concurrent agent spawns
# a cold `gobby mcp-server` took 25s to connect and two siblings passed 30s,
# leaving those agents without Gobby tools for their whole run. 120s matches
# Codex's `mcp_servers.gobby.startup_timeout_sec`.
MCP_TIMEOUT = "MCP_TIMEOUT"
MCP_TIMEOUT_VALUE = "120000"

# Env every managed Claude launch carries, on spawn and on resume. Gobby owns
# agent memory, and a prompt suggestion renders in the composer exactly like
# typed text: composer probes would read it as an operator draft and hold every
# wake and watchdog injection into an idle agent.
CLAUDE_MANAGED_AGENT_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
}


def get_agent_session_cache_dir(session_id: str, *path_components: str) -> Path:
    """Return a safe per-session cache directory path for spawned agents."""
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]", "-", session_id).strip("-")[:80]
    if not safe_prefix:
        safe_prefix = "unknown-session"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir(), *path_components, f"{safe_prefix}-{digest}")


def get_agent_uv_cache_dir(session_id: str) -> str:
    """Return a writable, per-session uv cache directory for spawned agents."""
    return str(get_agent_session_cache_dir(session_id, "gobby", "uv-cache"))


def ensure_agent_uv_cache_dir(session_id: str) -> str:
    """Create and return the spawned agent's uv cache directory."""
    cache_dir = Path(get_agent_uv_cache_dir(session_id))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return str(cache_dir)


def shared_agent_cargo_home_dir() -> Path:
    """Return the one Cargo home that every spawned agent shares.

    Cargo fingerprints embed dependency source paths under
    ``$CARGO_HOME/registry/src``, so a per-session home invalidates every
    dependency in the deliberately shared ``CARGO_TARGET_DIR`` and makes each
    agent rebuild the whole graph. This lives beside that shared target
    directory under Gobby home; the operator's own ``~/.cargo`` is untouched.
    """
    return get_gobby_home() / "cache" / "cargo-home"


def get_agent_cargo_home_dir(session_id: str) -> str:
    """Return the shared Cargo home directory for sandboxed agents.

    ``session_id`` is deliberately unused: the home is shared on purpose. The
    parameter stays so this keeps satisfying the ``Callable[[str], str]``
    contract of the ``SPAWN_CACHE_POLICY`` table.
    """
    return str(shared_agent_cargo_home_dir())


def ensure_agent_cargo_home_dir(session_id: str) -> str:
    """Create and return the shared Cargo home directory.

    ``session_id`` is deliberately unused; see ``get_agent_cargo_home_dir``.
    """
    cargo_home = shared_agent_cargo_home_dir()
    cargo_home.mkdir(parents=True, exist_ok=True)
    return str(cargo_home)


def get_terminal_env_vars(
    session_id: str,
    parent_session_id: str,
    agent_run_id: str,
    project_id: str,
    workflow_name: str | None = None,
    agent_depth: int = 1,
    max_agent_depth: int = 5,
    prompt: str | None = None,
    prompt_file: str | None = None,
    operator_token: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, str]:
    """
    Build environment variables dict for spawning a terminal-mode agent.

    Args:
        session_id: The pre-created child session ID.
        parent_session_id: The parent session ID for context resolution.
        agent_run_id: The agent run record ID.
        project_id: The project ID.
        workflow_name: Optional workflow to activate.
        agent_depth: Current nesting depth (default: 1).
        max_agent_depth: Maximum allowed depth (default: 5).
        prompt: Optional short prompt (for inline passing).
        prompt_file: Optional path to file containing prompt (for long prompts).
        operator_token: Operator token used to mint the run capability.
        timeout_seconds: The run's declared timeout, bounding capability expiry.

    Returns:
        Dict of environment variable name to value.
    """
    from gobby.agents.cargo_target import ensure_shared_cargo_target_dir
    from gobby.agents.spawn_cache_policy import build_spawn_cache_env
    from gobby.utils.daemon_url import daemon_url

    env = {
        # Resolve before sandboxing hides the operator bootstrap credentials.
        "GOBBY_DAEMON_URL": daemon_url(),
        GOBBY_SESSION_ID: session_id,
        GOBBY_AGENT_RUN_ID: agent_run_id,
        GOBBY_PROJECT_ID: project_id,
        GOBBY_AGENT_DEPTH: str(agent_depth),
        GOBBY_MAX_AGENT_DEPTH: str(max_agent_depth),
        **build_spawn_cache_env(session_id),
        CARGO_TARGET_DIR: ensure_shared_cargo_target_dir(project_id),
    }
    if operator_token:
        env[GOBBY_AGENT_API_TOKEN] = issue_agent_api_token(
            operator_token,
            agent_run_id=agent_run_id,
            session_id=session_id,
            project_id=project_id,
            machine_id=get_machine_id(),
            timeout_seconds=timeout_seconds,
        )

    if parent_session_id:
        env[GOBBY_PARENT_SESSION_ID] = parent_session_id

    if workflow_name:
        env[GOBBY_WORKFLOW_NAME] = workflow_name

    if prompt_file:
        env[GOBBY_PROMPT_FILE] = prompt_file
    elif prompt:
        env[GOBBY_PROMPT] = prompt

    # Inject trace context for propagation to child process
    from gobby.telemetry import inject_into_env

    env = inject_into_env(env)

    return env


# List of all environment variable names for documentation
ALL_TERMINAL_ENV_VARS = [
    "GOBBY_DAEMON_URL",
    GOBBY_SESSION_ID,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_AGENT_RUN_ID,
    GOBBY_AGENT_API_TOKEN,
    GOBBY_WORKFLOW_NAME,
    GOBBY_PROJECT_ID,
    GOBBY_AGENT_DEPTH,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_PROMPT,
    GOBBY_PROMPT_FILE,
    UV_CACHE_DIR,
    CARGO_HOME,
    CARGO_TARGET_DIR,
    *IDENTITY_ENV_VARS,
]
