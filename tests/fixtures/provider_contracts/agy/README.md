# AGY Provider Contract Captures

Captured against AGY `1.0.1` on 2026-05-22, refreshed against AGY `1.0.8`
on 2026-06-16, refreshed for one-shot text generation against AGY `1.0.10`
on 2026-06-22, and re-baselined against AGY `1.0.11` on 2026-06-24.

Capture procedure:

1. Record `agy --version`, `agy --help`, and `agy changelog`.
2. Inspect `~/.gemini/antigravity-cli/bin/agentapi`.
3. Probe `agentapi send`, `agentapi resume`, and `agentapi stream` with both
   pipe and PTY execution.
4. Run fresh `agy --sandbox --print-timeout ... --print ...` prompts in a temp workspace.
5. Inspect `~/.gemini/antigravity-cli/cache/last_conversations.json`, recent
   logs under `~/.gemini/antigravity-cli/log/`, and conversation `.pb` paths.

Important result: `agentapi` exists as a hidden wrapper, but `agy help agentapi`
does not advertise it and `agentapi get-conversation-metadata ...` still fails
outside the Antigravity launcher with `ANTIGRAVITY_LS_ADDRESS is not set`.
AGY `1.0.11` exposes `--print`, `--prompt-interactive`, resume flags, hooks,
MCP configuration, and model listing. One-shot text generation works over a
plain pipe without a PTY, including JSON-via-directive, but timeouts return exit
0 with `Error:` on stdout and unknown `--model` values silently fall back to the
default model. Treat AGY web-chat and agent spawning as blocked unless AGY CLI
adds a documented streaming/session transport.
