"""CLI command building for agent spawning.

Provides functions to construct CLI commands for Claude, Gemini, and Codex
with proper flags for prompts, permissions, and session management.
"""

from __future__ import annotations


def build_cli_command(
    cli: str,
    prompt: str | None = None,
    session_id: str | None = None,
    auto_approve: bool = False,
    working_directory: str | None = None,
    sandbox_args: list[str] | None = None,
    model: str | None = None,
    mode: str = "agent",
    output_format: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """
    Build the CLI command and env for any provider.

    Supports three modes:
    - "agent": Autonomous subagent (auto-approve, single-shot prompt)
    - "interactive": Multi-turn web chat (stream-json I/O, no prompt)
    - "headless": Single-turn headless query (not used for web chat)

    Each CLI has different syntax for passing prompts and handling permissions:

    Claude Code:
    - claude --session-id <uuid> --dangerously-skip-permissions -p [prompt]

    Gemini CLI:
    - gemini --approval-mode yolo "prompt" (one-shot)
    - gemini --acp (interactive ACP mode)

    Codex CLI:
    - codex --full-auto -C <dir> [PROMPT]

    Args:
        cli: CLI name (claude, gemini, codex)
        prompt: Optional prompt to pass (agent mode)
        session_id: Optional session ID
        auto_approve: If True, add flags to auto-approve actions/permissions
        working_directory: Optional working directory (used by Codex -C flag)
        sandbox_args: Optional list of CLI args for sandbox configuration
        model: Optional model name
        mode: "agent" (default), "interactive", or "headless"
        output_format: Output format override (e.g., "stream-json")
        env_overrides: Environment variable overrides

    Returns:
        Tuple of (command list, env dict) for subprocess execution
    """
    import os

    command = [cli]
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)

    if cli == "claude":
        # Claude CLI flags
        if session_id:
            command.extend(["--session-id", session_id])
        if model:
            command.extend(["--model", model])
        if auto_approve:
            command.append("--dangerously-skip-permissions")
        if mode == "interactive":
            fmt = output_format or "stream-json"
            command.extend(["--output-format", fmt, "--verbose", "--input-format", fmt])

    elif cli == "gemini":
        # Gemini CLI flags
        if model:
            command.extend(["--model", model])
        if auto_approve:
            command.extend(["--approval-mode", "yolo"])
        if mode == "interactive":
            command.append("--acp")
            if session_id:
                command.extend(["--resume", session_id])

    elif cli == "codex":
        # Codex CLI flags
        if model:
            command.extend(["--model", model])
        if auto_approve:
            command.append("--full-auto")
        if working_directory:
            command.extend(["-C", working_directory])

    # Add sandbox args before prompt (prompt must be last)
    if sandbox_args:
        command.extend(sandbox_args)

    # Prompt only in agent/headless mode (interactive mode uses stdin)
    if prompt and mode != "interactive":
        command.append(prompt)

    return command, env


def build_gemini_command_with_resume(
    gemini_external_id: str,
    prompt: str | None = None,
    auto_approve: bool = False,
    gobby_session_id: str | None = None,
    model: str | None = None,
) -> list[str]:
    """
    Build Gemini CLI command with session resume.

    Uses -r flag to resume a preflight-captured session, with session context
    injected into the initial prompt.

    Args:
        gemini_external_id: Gemini's session_id from preflight capture
        prompt: Optional user prompt
        auto_approve: If True, add --approval-mode yolo
        gobby_session_id: Gobby session ID to inject into context
        model: Optional model name to pass to the CLI (--model flag)

    Returns:
        Command list for subprocess execution
    """
    command = ["gemini"]

    # Resume the preflight session
    command.extend(["-r", gemini_external_id])

    if model:
        command.extend(["--model", model])
    if auto_approve:
        command.extend(["--approval-mode", "yolo"])

    # Build prompt with session context
    if gobby_session_id:
        context_prefix = (
            f"Your Gobby session_id is: {gobby_session_id}\n"
            f"Use this when calling Gobby MCP tools.\n\n"
        )
        full_prompt = context_prefix + (prompt or "")
    else:
        full_prompt = prompt or ""

    # Use -i for interactive mode with initial prompt
    if full_prompt:
        command.extend(["-i", full_prompt])

    return command


def build_codex_command_with_resume(
    codex_external_id: str,
    prompt: str | None = None,
    auto_approve: bool = False,
    gobby_session_id: str | None = None,
    working_directory: str | None = None,
    model: str | None = None,
) -> list[str]:
    """
    Build Codex CLI command with session resume.

    Uses `codex resume {session_id}` to resume a preflight-captured session,
    with session context injected into the prompt.

    Args:
        codex_external_id: Codex's session_id from preflight capture
        prompt: Optional user prompt
        auto_approve: If True, add --full-auto flag
        gobby_session_id: Gobby session ID to inject into context
        working_directory: Optional working directory override
        model: Optional model name to pass to the CLI (--model flag)

    Returns:
        Command list for subprocess execution
    """
    command = ["codex", "resume", codex_external_id]

    if model:
        command.extend(["--model", model])
    if auto_approve:
        command.append("--full-auto")

    if working_directory:
        command.extend(["-C", working_directory])

    # Build prompt with session context
    if gobby_session_id:
        context_prefix = (
            f"Your Gobby session_id is: {gobby_session_id}\n"
            f"Use this when calling Gobby MCP tools.\n\n"
        )
        full_prompt = context_prefix + (prompt or "")
    else:
        full_prompt = prompt or ""

    # Prompt is a positional argument after session_id
    if full_prompt:
        command.append(full_prompt)

    return command
