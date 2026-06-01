"""CLI command building for agent spawning.

    Provides functions to construct CLI commands for Claude, Gemini, Grok, Qwen,
Codex, and Droid with proper flags for prompts, permissions, and session management.
"""

from __future__ import annotations


def build_cli_command(
    cli: str,
    prompt: str | None = None,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    auto_approve: bool = False,
    working_directory: str | None = None,
    sandbox_args: list[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    mode: str = "agent",
    output_format: str | None = None,
    env_overrides: dict[str, str] | None = None,
    config_overrides: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
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
    - reasoning effort is resolved before command construction; current Gemini
      terminal mode relies on the selected model/settings rather than a stable flag

    Codex CLI:
    - codex --ask-for-approval never --disable guardian_approval -C <dir> [PROMPT]

    Droid CLI:
    - droid exec --input-format stream-json --cwd <dir> [--model <id>]
      [--reasoning-effort <level>] --auto <low|high> [PROMPT]

    Args:
        cli: CLI name (claude, gemini, grok, qwen, codex, droid)
        prompt: Optional prompt to pass (agent mode)
        session_id: Optional session ID
        auto_approve: If True, add flags to auto-approve actions/permissions
        working_directory: Optional working directory (used by Codex -C and Droid --cwd)
        sandbox_args: Optional list of CLI args for sandbox configuration
        model: Optional model name
        mode: "agent" (default), "interactive", or "headless"
        output_format: Output format override (e.g., "stream-json")
        env_overrides: Environment variable overrides. Callers are responsible
            for merging inherited environment variables if needed.
        config_overrides: CLI configuration overrides for providers that
            support `-c key=value` flags. Currently used by Codex.
        disallowed_tools: Provider-native tool names to remove from the toolset.
            Currently supported for Claude.

    Returns:
        Tuple of (command list, env dict) for subprocess execution
    """
    command = [cli]
    env: dict[str, str] = {}
    sandbox_args_consumed = False
    if env_overrides:
        env.update(env_overrides)

    if cli == "claude":
        # Claude CLI flags
        if resume_session_id:
            command.extend(["--resume", resume_session_id])
        elif session_id:
            command.extend(["--session-id", session_id])
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        if disallowed_tools:
            command.extend(["--disallowedTools", *disallowed_tools])
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
        if resume_session_id:
            command.extend(["--resume", resume_session_id])
        if mode == "interactive":
            command.append("--acp")
            if session_id and not resume_session_id:
                command.extend(["--resume", session_id])

    elif cli == "grok":
        if mode == "interactive":
            command.extend(["agent", "--no-leader", "--always-approve"])
            if model:
                command.extend(["--model", model])
            if reasoning_effort:
                command.extend(["--reasoning-effort", reasoning_effort])
            command.append("stdio")
        else:
            if auto_approve:
                command.append("--always-approve")
            command.append("--no-alt-screen")
            if working_directory:
                command.extend(["--cwd", working_directory])
            if model:
                command.extend(["--model", model])
            if reasoning_effort:
                command.extend(["--reasoning-effort", reasoning_effort])
            if resume_session_id:
                command.extend(["--resume", resume_session_id])
            if sandbox_args:
                command.extend(sandbox_args)
                sandbox_args_consumed = True
            command.append("--single")

    elif cli == "codex":
        # Codex CLI flags
        if resume_session_id:
            command.append("resume")
        if model:
            command.extend(["--model", model])
        if cli == "codex" and reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if auto_approve:
            command.extend(["--ask-for-approval", "never", "--disable", "guardian_approval"])
        if working_directory:
            command.extend(["-C", working_directory])
        for override in config_overrides or []:
            command.extend(["-c", override])

    elif cli == "droid":
        # Droid exec flags, verified against `droid exec --help` on v0.106.0.
        command.extend(["exec", "--input-format", "stream-json"])
        if resume_session_id:
            command.extend(["--session-id", resume_session_id])
        if working_directory:
            command.extend(["--cwd", working_directory])
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--reasoning-effort", reasoning_effort])
        command.extend(["--auto", "high" if auto_approve else "low"])

    # Add sandbox args before prompt (prompt must be last)
    if sandbox_args and not sandbox_args_consumed:
        command.extend(sandbox_args)

    if cli == "codex" and resume_session_id:
        command.append(resume_session_id)

    # Prompt only in agent/headless mode (interactive mode uses stdin)
    if prompt and mode != "interactive":
        command.append(prompt)

    return command, env
