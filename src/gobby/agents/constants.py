"""
Constants for agent spawning and terminal mode.

This module defines environment variables used to pass context to
spawned terminal processes. When an agent spawns a child in terminal
mode, these environment variables are set in the child process.
"""

# ============================================================================
# Terminal Mode Environment Variables
# ============================================================================
# These environment variables are set when spawning a terminal-mode agent.
# The child CLI process reads these to pick up its prepared state.
# ============================================================================

# Session identifier for the pre-created child session
# The spawned CLI uses this to connect to its session via hooks
GOBBY_SESSION_ID = "GOBBY_SESSION_ID"

# Parent session identifier for context resolution
# Used to look up parent session for context injection
GOBBY_PARENT_SESSION_ID = "GOBBY_PARENT_SESSION_ID"

# Agent run record identifier
# Links the terminal process back to its agent_runs record
GOBBY_AGENT_RUN_ID = "GOBBY_AGENT_RUN_ID"

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

    Returns:
        Dict of environment variable name to value.
    """
    env = {
        GOBBY_SESSION_ID: session_id,
        GOBBY_AGENT_RUN_ID: agent_run_id,
        GOBBY_PROJECT_ID: project_id,
        GOBBY_AGENT_DEPTH: str(agent_depth),
        GOBBY_MAX_AGENT_DEPTH: str(max_agent_depth),
    }

    if parent_session_id:
        env[GOBBY_PARENT_SESSION_ID] = parent_session_id

    if workflow_name:
        env[GOBBY_WORKFLOW_NAME] = workflow_name

    if prompt_file:
        env[GOBBY_PROMPT_FILE] = prompt_file
    elif prompt:
        env[GOBBY_PROMPT] = prompt

    return env


# List of all environment variable names for documentation
ALL_TERMINAL_ENV_VARS = [
    GOBBY_SESSION_ID,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_AGENT_RUN_ID,
    GOBBY_WORKFLOW_NAME,
    GOBBY_PROJECT_ID,
    GOBBY_AGENT_DEPTH,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_PROMPT,
    GOBBY_PROMPT_FILE,
]
