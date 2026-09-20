"""CLI command building for agent spawning.

    Provides functions to construct CLI commands for Claude, Grok, Qwen,
Codex, and Droid with proper flags for prompts, permissions, and session management.
"""

from __future__ import annotations

from gobby.agents.provider_capabilities import PROVIDER_CAPABILITIES, provider_reasoning_flag


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
    codex_oss_provider: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """
    Build the CLI command and env for any provider.

    Supports three modes:
    - "agent": Autonomous subagent (auto-approve, single-shot prompt)
    - "interactive": Multi-turn provider session. Droid uses its terminal UI;
      stream-capable providers use structured I/O without a positional prompt.
    - "headless": Single-turn headless query (not used for web chat)

    Each CLI has different syntax for passing prompts and handling permissions:

    Claude Code:
    - claude --session-id <uuid> --dangerously-skip-permissions -p [prompt]

    Codex CLI:
    - codex --ask-for-approval never --disable guardian_approval -C <dir>
      -c check_for_update_on_startup=false [PROMPT]

    Droid CLI:
    - droid exec --cwd <dir> [--model <id>]
      [--reasoning-effort <level>] --auto <low|high> [PROMPT]

    Args:
        cli: CLI name (claude, grok, qwen, codex, droid)
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
        codex_oss_provider: Optional Codex OSS local provider (lmstudio or ollama).

    Returns:
        Tuple of (command list, env dict) for subprocess execution
    """
    if cli not in PROVIDER_CAPABILITIES:
        supported = ", ".join(sorted(PROVIDER_CAPABILITIES))
        raise ValueError(f"Unsupported CLI: {cli}. Must be one of: {supported}")

    command = [cli]
    env: dict[str, str] = {}
    reasoning_flag = provider_reasoning_flag(cli)
    sandbox_args_consumed = False
    prompt_consumed = False
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
        if reasoning_effort and reasoning_effort != "auto" and reasoning_flag == "claude-effort":
            command.extend(["--effort", reasoning_effort])
        if auto_approve:
            command.append("--dangerously-skip-permissions")
        if mode == "interactive":
            fmt = output_format or "stream-json"
            command.extend(["--output-format", fmt, "--verbose", "--input-format", fmt])

    elif cli == "qwen":
        # Qwen CLI flags
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
            if (
                reasoning_effort
                and reasoning_effort != "auto"
                and reasoning_flag == "reasoning-effort"
            ):
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
            if (
                reasoning_effort
                and reasoning_effort != "auto"
                and reasoning_flag == "reasoning-effort"
            ):
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
        if codex_oss_provider:
            command.extend(["--oss", "--local-provider", codex_oss_provider])
            if model:
                command.extend(["-m", model])
        elif model:
            command.extend(["--model", model])
        if reasoning_effort and reasoning_effort != "auto" and reasoning_flag == "codex-config":
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if auto_approve:
            command.extend(["--ask-for-approval", "never", "--disable", "guardian_approval"])
        if working_directory:
            command.extend(["-C", working_directory])
        for override in config_overrides or []:
            command.extend(["-c", override])
        # Spawned runs must never stop at Codex's interactive upgrade menu.
        # Keep this last so user- or endpoint-provided overrides cannot re-enable it.
        # Never disable Codex's code-mode host here: gpt-5.6 models are
        # `tool_mode=code_mode_only`, so the host (codex-code-mode-host, a sibling
        # of the codex binary) is their only MCP executor and turning it off makes
        # every Gobby MCP call fail closed as "code-mode host is disabled" (#21753).
        # A host that cannot launch is a machine-level install/Gatekeeper problem.
        command.extend(["-c", "check_for_update_on_startup=false"])

    elif cli == "droid":
        # Agent mode is the one-shot `droid exec` path. Interactive mode uses the
        # terminal UI so AskUser, permission dialogs, interrupts, and replacement
        # prompts remain available to the managed terminal controller.
        if mode != "interactive":
            command.append("exec")
        if resume_session_id:
            command.extend(
                ["--resume" if mode == "interactive" else "--session-id", resume_session_id]
            )
        if working_directory and mode != "interactive":
            command.extend(["--cwd", working_directory])
        if model:
            command.extend(["--model", model])
        if reasoning_effort and reasoning_effort != "auto" and reasoning_flag == "reasoning-effort":
            command.extend(["--reasoning-effort", reasoning_effort])
        if mode != "interactive":
            command.extend(["--auto", "high" if auto_approve else "low"])

    elif cli == "agy":
        # Terminal spawn uses the 1.1.7/1.1.3 recorded TUI flags, not print-mode
        # `-p` / stream-json. `--mode` is never added (records 1.1.14 and 1.1.23).
        if resume_session_id:
            command.extend(["--conversation", resume_session_id])
        if auto_approve:
            command.append("--dangerously-skip-permissions")
        if working_directory:
            command.extend(["--add-dir", working_directory])
        if model:
            command.extend(["--model", model])
        if reasoning_effort and reasoning_effort != "auto" and reasoning_flag == "claude-effort":
            command.extend(["--effort", reasoning_effort])
        # Autonomous spawns must start without a TUI input event. AGY 1.1.25
        # documents --print as its non-interactive single-prompt mode.
        if prompt and mode != "interactive":
            command.extend(["--print", prompt])
            prompt_consumed = True

    # Add sandbox args before prompt (prompt must be last)
    if sandbox_args and not sandbox_args_consumed:
        command.extend(sandbox_args)

    if cli == "codex" and resume_session_id:
        command.append(resume_session_id)

    # Prompt only in agent/headless mode (interactive mode uses stdin)
    if prompt and (mode != "interactive" or cli == "droid") and not prompt_consumed:
        command.append(prompt)

    return command, env
