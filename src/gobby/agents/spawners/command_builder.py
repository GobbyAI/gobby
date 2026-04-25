"""CLI command building for agent spawning.

Provides functions to construct CLI commands for Claude, Gemini, Qwen, and Codex
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
    reasoning_effort: str | None = None,
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
    - codex --ask-for-approval never -C <dir> [PROMPT]

    Args:
        cli: CLI name (claude, gemini, qwen, codex)
        prompt: Optional prompt to pass (agent mode)
        session_id: Optional session ID
        auto_approve: If True, add flags to auto-approve actions/permissions
        working_directory: Optional working directory (used by Codex -C flag)
        sandbox_args: Optional list of CLI args for sandbox configuration
        model: Optional model name
        mode: "agent" (default), "interactive", or "headless"
        output_format: Output format override (e.g., "stream-json")
        env_overrides: Environment variable overrides. Callers are responsible
            for merging inherited environment variables if needed.

    Returns:
        Tuple of (command list, env dict) for subprocess execution
    """
    command = [cli]
    env: dict[str, str] = {}
    if env_overrides:
        env.update(env_overrides)

    if cli == "claude":
        # Claude CLI flags
        if session_id:
            command.extend(["--session-id", session_id])
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        if auto_approve:
            command.append("--dangerously-skip-permissions")
        if mode == "interactive":
            fmt = output_format or "stream-json"
            command.extend(["--output-format", fmt, "--verbose", "--input-format", fmt])

    elif cli in {"gemini", "qwen"}:
        # Gemini/Qwen CLI flags
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
        if cli == "codex" and reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if auto_approve:
            command.extend(["--ask-for-approval", "never"])
        if working_directory:
            command.extend(["-C", working_directory])

    # Add sandbox args before prompt (prompt must be last)
    if sandbox_args:
        command.extend(sandbox_args)

    # Prompt only in agent/headless mode (interactive mode uses stdin)
    if prompt and mode != "interactive":
        command.append(prompt)

    return command, env
