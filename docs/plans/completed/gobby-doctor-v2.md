# Plan: Rename diagnostic to doctor with security audit

**Status:** Approved
**Date:** 2026-01-26
**Task:** #6192
**Source:** Clawdbot research - borrowing security audit pattern

## Summary
Rename the `diagnostic` skill to `doctor` and add security audit checks inspired by Clawdbot's `doctor` command. Default behavior runs both functional and security checks.

## Changes

### File: `src/gobby/install/shared/skills/diagnostic/SKILL.md`
**Action:** Rename directory to `doctor/` and update content

**Subcommand Structure:**
| Command | Behavior |
|---------|----------|
| `/gobby doctor` | Shows help with available options |
| `/gobby doctor --functional` | Functional tests only (phases 1-3, no worktree/clone) |
| `/gobby doctor --security` | Security audit only (phase 5) |
| `/gobby doctor --all` | Everything: functional + security + resource-heavy (worktree/clone) |
| `/gobby doctor --cleanup` | Cleanup stale `__diag__` artifacts |

**New Phase 5: Security Audit** (runs by default, or with `--security`)

| Check | Description | Pass Criteria |
|-------|-------------|---------------|
| 5.1 File Permissions | Check ~/.gobby/config.yaml, .mcp.json, PostgreSQL hub | All files 0o600 |
| 5.2 Plaintext Secrets | Scan config for hardcoded API keys | No plaintext keys in `llm_providers.api_keys` |
| 5.3 HTTP Binding | Check daemon network binding | Warn if 0.0.0.0 with no firewall note |
| 5.4 Webhook URLs | Validate webhook endpoints | All use HTTPS |
| 5.5 Log Level | Check logging.level setting | Warn if DEBUG in production |
| 5.6 Plugin Security | Check hook_extensions.plugins.enabled | Warn if enabled |
| 5.7 MCP Server URLs | Validate remote MCP server URLs | All remote URLs use HTTPS |
| 5.8 Permissive Skills | Check skills with `allowed-tools: ["*"]` | List any found (warning) |

## Implementation Steps

1. Rename directory: `diagnostic/` → `doctor/`
2. Update skill metadata (name, description, trigger phrases)
3. Keep existing phases 1-4 as "Functional Tests"
4. Add Phase 5: Security Audit
5. Update subcommands section with new flag logic
6. Update output format to show both sections

## Verification

1. Run `/gobby doctor` - should show help with available options
2. Run `/gobby doctor --functional` - runs phases 1-3 (no worktree/clone)
3. Run `/gobby doctor --security` - runs phase 5 only
4. Run `/gobby doctor --all` - runs everything including worktree/clone tests
5. Run `/gobby doctor --cleanup` - removes stale `__diag__` artifacts

---

## Part 2: `/gobby usage` Skill

### Background
Gobby already has the infrastructure:
- `gobby-metrics.get_usage_report(days)` - returns token counts by model
- `gobby-metrics.get_budget_status()` - returns daily budget status
- `SessionTokenTracker` - aggregates from sessions table

**Gap identified**: Claude Code transcripts contain `message.model` ("claude-opus-4-5-20251101") but the parser only extracts `message.usage`, not the model. This is why sessions show model="unknown" and cost=$0.

### Fix: Extract model from transcripts

**File:** `src/gobby/sessions/transcripts/claude.py`

In `_extract_usage()`, also extract and return the model:
```python
def _extract_usage(self, data: dict[str, Any]) -> tuple[TokenUsage | None, str | None]:
    """Extract token usage and model from message data."""
    message = data.get("message", {})
    model = message.get("model")  # "claude-opus-4-5-20251101"
    usage_data = message.get("usage")
    # ... existing usage extraction ...
    return usage, model
```

**File:** `src/gobby/sessions/transcripts/base.py`

Add `model` field to `ParsedMessage`:
```python
@dataclass
class ParsedMessage:
    # ... existing fields ...
    model: str | None = None
```

**File:** `src/gobby/sessions/processor.py`

When processing messages, capture the model and update the session record.

### Skill Design

**File:** `src/gobby/install/shared/skills/usage/SKILL.md`

**Commands:**
| Command | Behavior |
|---------|----------|
| `/gobby usage` | Shows help |
| `/gobby usage --today` | Today's usage summary |
| `/gobby usage --week` | Last 7 days summary |
| `/gobby usage --budget` | Budget status only |

**Output Format:**
```
Usage Summary (Last 7 Days)
───────────────────────────────────
Sessions:        278
Input tokens:    3.6M
Output tokens:   66K
Cache reads:     2.3B
Cost:            $0.00 (model unknown)

Budget Status (Today)
───────────────────────────────────
Daily limit:     $50.00
Used today:      $0.00
Remaining:       $50.00 (100%)
```

### Implementation
1. Create new skill `usage/SKILL.md`
2. Skill calls `gobby-metrics.get_usage_report()` and `get_budget_status()`
3. Formats output for readability

### Verification
1. Run `/gobby usage --today` - should show token counts and cost (once model fix is in)
2. Call `gobby-metrics.get_usage_report(days=1)` - model should no longer be "unknown"
