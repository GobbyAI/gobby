# Gobby Platform Claude Code Hooks

This directory contains the hook dispatcher for the Gobby Platform, providing lifecycle integration between Claude Code sessions and the Gobby daemon/memory system.

## 📚 Documentation

- **[HOOK_SCHEMAS.md](./HOOK_SCHEMAS.md)** - Complete input/output schemas for all hook types
- **[hook_dispatcher.py](./hook_dispatcher.py)** - Central dispatcher that routes all hooks to the daemon

## Quick Start

### Installation

Install hooks and skills using the Gobby CLI:

```bash
# Install all Gobby hooks and skills to ~/.claude/
gobby install

# Install to specific CLIs only
gobby install --claude
gobby install --gemini
gobby install --codex
```

This will:
1. Copy `hook_dispatcher.py` and other hooks to `~/.claude/hooks/`
2. Copy skills to `~/.claude/skills/`
3. Back up your existing `settings.json`
4. Install hook configurations for all hook types

### Uninstallation

```bash
# Remove all Gobby hooks and skills (with confirmation)
gobby uninstall

# Uninstall from specific CLIs only
gobby uninstall --claude
gobby uninstall --gemini
gobby uninstall --codex
```

### Testing the Dispatcher

Test the dispatcher manually with sample input:

```bash
# Test SessionStart hook
echo '{"session_id": "test-123", "transcript_path": "/tmp/test.jsonl", "hook_event_name": "SessionStart", "source": "startup"}' | \
  uv run python .claude/hooks/hook_dispatcher.py --type session-start

# Test UserPromptSubmit hook
echo '{"session_id": "test-123", "prompt": "Hello", "hook_event_name": "UserPromptSubmit"}' | \
  uv run python .claude/hooks/hook_dispatcher.py --type user-prompt-submit

# Enable debug logging
echo '{"session_id": "test-123"}' | \
  uv run python .claude/hooks/hook_dispatcher.py --type session-end --debug
```

### Viewing Hook Logs

```bash
# Real-time dispatcher logs
tail -f ~/.gobby/logs/hook-manager.log

# Daemon logs (hook processing happens here)
tail -f ~/.gobby/logs/gobby.log

# All logs
tail -f ~/.gobby/logs/*.log
```

## Hook Types

All hooks are routed through `hook_dispatcher.py` to the daemon's `HookManager`.

### Session Lifecycle

| Hook Type | Trigger | Purpose |
|-----------|---------|---------|
| **SessionStart** | Session begins | Inject initial context, initialize state |
| **SessionEnd** | Session ends | Cleanup, archival, session summary generation |
| **PreCompact** | Before context compaction | Save snapshots, preserve context |

### User Interaction

| Hook Type | Trigger | Purpose |
|-----------|---------|---------|
| **UserPromptSubmit** | User submits prompt | Track user interactions, validate prompts |
| **Notification** | System notification | Handle alerts and messages |

### Tool Execution

| Hook Type | Trigger | Purpose |
|-----------|---------|---------|
| **PreToolUse** | Before tool execution | Validate, inject context, control flow |
| **PostToolUse** | After tool execution | Track tool usage, save to interaction history |

### Control Flow

| Hook Type | Trigger | Purpose |
|-----------|---------|---------|
| **Stop** | Session stopping | Graceful shutdown, final cleanup |
| **SubagentStart** | Subagent begins | Track subagent initialization, inject context |
| **SubagentStop** | Subagent ends | Subagent cleanup, track completion |

## Architecture

```
Claude Code Session
       ↓ (hook fires)
hook_dispatcher.py
       ↓ (HTTP POST to localhost:8765/hooks/execute)
Gobby Daemon (HookManager)
       ↓ (processes hook)
Database / Session Management
```

The dispatcher is a thin wrapper that:
1. Receives hook input from Claude Code via stdin
2. Checks if daemon is running
3. Forwards to daemon's `/hooks/execute` endpoint
4. Returns daemon response to Claude Code via stdout

All hook logic is implemented in `src/gobby/hooks/hook_manager.py`.

## Exit Codes

| Exit Code | Meaning | Behavior |
|-----------|---------|----------|
| `0` | Success | Hook output processed normally |
| `1` | Error | Error logged, execution continues |
| `2` | Block | Blocks operation and shows stderr to user |

## Daemon Endpoints

The dispatcher forwards hooks to:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/hooks/execute` | POST | Execute hook via HookManager |
| `/admin/status` | GET | Daemon health check |
| `/auth/health` | GET | Authentication status |

## Modifying Hook Behavior

Hook logic is implemented in `src/gobby/hooks/hook_manager.py`. To modify hook behavior:

1. Edit the appropriate handler in `HookManager`
2. Restart the daemon: `gobby restart`
3. Test with the dispatcher

Do not modify `hook_dispatcher.py` - it's just a router.

## Troubleshooting

### Hooks Not Working

```bash
# 1. Check daemon is running
gobby status

# 2. Check daemon logs for hook processing
tail -f ~/.gobby/logs/gobby.log

# 3. Check hook manager logs
tail -f ~/.gobby/logs/hook-manager.log

# 4. Test dispatcher manually
echo '{"session_id": "test-123"}' | \
  uv run python ~/.claude/hooks/hook_dispatcher.py --type session-start --debug
```

### Validate Settings

```bash
# Verify settings.json is correct
uv run python .claude/hooks/validate_settings.py
```

### Common Issues

| Issue | Solution |
|-------|----------|
| Daemon not running | Run `gobby start` |
| Hooks not installed | Run `gobby install` |
| Settings corrupted | Restore from backup or reinstall hooks |

## Resources

- **Schemas**: [HOOK_SCHEMAS.md](./HOOK_SCHEMAS.md)
- **Dispatcher**: [hook_dispatcher.py](./hook_dispatcher.py)
- **Hook Manager**: `src/gobby/hooks/hook_manager.py`
- **Validation**: [validate_settings.py](./validate_settings.py)
- **Official Docs**: https://docs.claude.com/en/docs/claude-code/hooks

---

*Last Updated: 2025-11-17*
