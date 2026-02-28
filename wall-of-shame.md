# Wall of Shame

Failed fix attempts for bugs that should have been caught earlier.

## #9206, #9209 — Web UI Slash Commands Broken

**Symptom:** `/gobby:canvas`, `/skills`, `/mcp` and other slash commands work in CLI but fail silently in the Web UI.

### Attempt 1: Commit `1bffaca6` — "Fix Web UI slash command bugs"

**What it tried:** Unknown adjustments to the web chat path.

**Why it failed:** Did not address the root cause. `_fire_lifecycle()` in `chat.py` only called `workflow_handler.evaluate()` (the RuleEngine for declarative rules). It **never called `EventHandlers.handle_before_agent()`**, which is where skill interception (`_intercept_skill_command`) actually lives.

**Root cause:** The CLI path in `HookManager.handle()` runs two things in sequence:
1. `_evaluate_workflow_rules(event)` → RuleEngine
2. `handler(event)` → `EventHandlers.handle_before_agent()` → skill interception

The Web UI path only did step 1. Step 2 was completely missing.

**Not the actual fix.** Wired `EventHandlers` to the WebSocket server and dispatched from `_fire_lifecycle()`. This helps CLI-style skill interception in the web backend but doesn't fix the real issue.

### Attempt 2: Commit `9be33b0e` — "Wire EventHandlers from _fire_lifecycle"

**What it tried:** Same as above — assumed the problem was missing `EventHandlers` dispatch in `_fire_lifecycle()`.

**Why it failed:** Wrong mental model entirely. Web UI slash commands (`/skills`, `/mcp`, `/gobby`) are **modal-based UI commands**, not text that gets intercepted by hooks. They pop up a modal where the user selects a skill or MCP tool. The modal is responsible for injecting context into the chat stream — hooks are not involved at all.

For skills specifically, `SkillBrowserModal.handleRun()` called `onRunSkill(skillName)` which sent `/gobby:skillName` as a plain chat message hoping hook interception would resolve it. But there is no `/gobby:*` in the Web UI — that's a CLI concept.

**Actual fix:** `SkillBrowserModal` should use `onSendMessage(content, injectContext)` — the same pattern `ToolBrowserModal` already uses — passing `selectedSkill.content` as `injectContext` so it gets injected via `additionalContext` on the SDK conversation.

### Attempt 2 (Additional Failure): Commit `9be33b0e` — "Wire EventHandlers from _fire_lifecycle"

**What it tried:** Wired `EventHandlers` to the WebSocket server and dispatched `handle_before_agent()` from `_fire_lifecycle()` after rule evaluation. Assumed `/gobby:canvas` was a skill command.

**Why it failed:** `/gobby:canvas` is NOT a skill. `canvas` is an MCP server. The `_intercept_skill_command` path in `handle_before_agent()` calls `_skill_manager.resolve_skill_name("canvas")`, which returns `None`, so it falls through to `_skill_not_found_context` — returning a "Skill 'canvas' not found" error. The fix only helps actual skill-based slash commands (`/gobby`, `/gobby:bridge`, `/gobby:expand`, etc.) but does nothing for `/gobby:canvas`, `/skills`, `/mcp` which are not skills at all.

**Root cause (still open):** The plan incorrectly diagnosed `/gobby:canvas` as a skill interception issue. The actual problem is that these commands need a different dispatch mechanism entirely — they're MCP server interactions, not skill lookups.
