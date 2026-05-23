# AGY Provider Contract Captures

Captured against AGY `1.0.1` on 2026-05-22.

Capture procedure:

1. Record `agy --version`, `agy --help`, and `agy changelog`.
2. Inspect `~/.gemini/antigravity-cli/bin/agentapi`.
3. Probe `agentapi send`, `agentapi resume`, and `agentapi stream` with both
   pipe and PTY execution.
4. Run a fresh `agy --print "..."` prompt in a temp workspace.
5. Inspect `~/.gemini/antigravity-cli/cache/last_conversations.json`, recent
   logs under `~/.gemini/antigravity-cli/log/`, and conversation `.pb` paths.

Important result: `agentapi` exists as a wrapper but did not expose a stable
machine transport in AGY `1.0.1`. Treat AGY web-chat support as blocked until a
real send/resume/stream contract is available.
