# Plan: Slim CLAUDE.md and Leverage Progressive Skill Disclosure

## Research Summary

Based on [Anthropic's Skill Authoring Best Practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices):

**Key Guidelines:**

- **Concise is key** - Every token must justify its existence
- **SKILL.md under 500 lines** - Split into bundled files if larger
- **Progressive disclosure is core** - SKILL.md points to bundled reference files
- **One level deep references** - Don't nest file references
- **Naming**: Use gerund form (`processing-pdfs`) or action-oriented (`process-pdfs`)
- **Description is critical** - Used for discovery among 100+ skills

**On Granularity:**
Anthropic doesn't explicitly distinguish "micro vs macro" skills, but says:
- Skills should be **focused** on specific domains/workflows
- Each skill should have **single responsibility** ("what it does AND when to use it")
- Split when approaching 500 lines

**Conclusion for Gobby:**

- **Keep macro-skills** (gobby-tasks at 305 lines is within best practices)
- **Add micro-skills for guardrails** (claiming-tasks, discovering-tools)
- **Extract duplicated context** to reduce token waste
- **MCP-only skills** - No slash commands, use progressive disclosure (`list_skills` → `get_skill`)

---

## Architectural Decision: MCP-Only Skills

**Decision**: Skills are stored in Gobby database only, NOT installed to `.claude/skills/`.

**Rationale**:

- Avoids slash command pollution (could have 100+ skills)
- Enforces progressive skill disclosure pattern
- Skills are accessed via `gobby-skills` MCP server: `list_skills()` → `get_skill(name="...")`
- Consistent with progressive tool disclosure (`list_tools()` → `get_tool_schema()`)

**Changes Required**:
1. Remove `install_shared_skills()` calls from installers
2. Ensure skills are auto-synced to Gobby database on daemon start
3. **Add `instructions` to Gobby MCP server** (FastMCP supports this)
4. Slim CLAUDE.md to routing layer (instructions live in MCP server now)

---

## Architectural Decision: MCP Server Instructions

**Pattern from [SkillPort](https://github.com/gotalab/skillport)**: Inject XML-structured instructions directly into the MCP server via FastMCP's `instructions` parameter.

**Current code** (`src/gobby/mcp_proxy/server.py:545`):
```python
mcp = FastMCP("gobby")  # No instructions!
```

**Proposed**:
```python
mcp = FastMCP("gobby", instructions=build_gobby_instructions())
```

**Instructions content** (`src/gobby/mcp_proxy/instructions.py`):
```xml
<gobby_system>

<usage>
Gobby provides progressive disclosure for MCP tools and skills.

## Tool Discovery
1. `list_mcp_servers()` — See available servers
2. `list_tools(server="...")` — Lightweight metadata (~100 tokens/tool)
3. `get_tool_schema(server, tool)` — Full schema when needed
4. `call_tool(server, tool, args)` — Execute

## Skill Discovery
1. `list_skills()` — Lightweight metadata (~100 tokens/skill)
2. `get_skill(name="...")` — Full content when needed
3. `search_skills(query="...")` — Find relevant skills

## Critical Rules
- Create/claim a task before editing files
- Pass session_id to task operations (from session context)
- NEVER load all schemas/skills upfront

</usage>

</gobby_system>
```

**Benefits**:
- Instructions travel with the MCP server, not static docs
- Every agent connecting to Gobby automatically learns the pattern
- Works across all CLIs (Claude, Gemini, Codex)
- Can be dynamically generated based on registered tools/skills

---

## Problem Statement

CLAUDE.md is 1,101 lines and conflates two distinct concerns:
1. **Building Gobby** - How to develop the Gobby codebase
2. **Building WITH Gobby** - How to use Gobby as an MCP server in any project

This causes:
- Agents don't follow progressive disclosure (instructions buried in long doc)
- Agents don't create/claim tasks before editing (rule lost in noise)
- Agents in OTHER projects don't get Gobby usage instructions at all

## Solution: Separate the Concerns

| Concern | Where | Audience |
| :--- | :--- | :--- |
| **Building Gobby** | `CLAUDE.md` | Agents developing Gobby itself |
| **Building WITH Gobby** | FastMCP `instructions` | Agents in ANY project using Gobby |

## Goals

1. **FastMCP instructions** - Startup sequence, progressive disclosure, rules (for ALL Gobby users)
2. **Slim CLAUDE.md** - Code conventions, architecture, testing (for Gobby developers only)
3. **MCP-only skills** - No slash command pollution
4. **Micro-skills** - Guardrails for when hooks block
5. **Hook error messages** - Reference skills when blocking

---

## Phase 1: Create Micro-Skills (Guardrails)

Create small, focused skills for common pain points. These are "remediation skills" - triggered when hooks block or agents get stuck.

**Naming**: Following Anthropic's gerund-form convention.

### 1.1 `starting-sessions` (~50 lines)

**Purpose**: First 5 things every session should do
**Triggers**: Session start, "how do I begin", initialization questions

```markdown
## Session Startup Checklist
1. list_mcp_servers() - discover available servers
2. Check session context for session_id
3. list_skills() - discover available skills
4. Claim or create a task before editing files
5. Use get_tool_schema() before calling unfamiliar tools
```

### 1.2 `claiming-tasks` (~40 lines)

**Purpose**: Quick reference when blocked by "no active task" error
**Triggers**: Hook blocks edit, "no active task" error, task-related questions

```markdown
## You've Been Blocked

The workflow system requires an active task before file modifications.

### Quick Fix
1. Create a task: call_tool("gobby-tasks", "create_task", {...})
2. Set to in_progress: call_tool("gobby-tasks", "update_task", {status: "in_progress"})
3. Now you can edit files
```

### 1.3 `discovering-tools` (~60 lines)

**Purpose**: The dual pattern for MCP tools AND skills
**Triggers**: Tool not found, schema questions, "what tools exist"

```markdown
## Progressive Disclosure Pattern

### For MCP Tools
1. list_mcp_servers() → discover servers
2. list_tools(server="...") → lightweight metadata
3. get_tool_schema(server, tool) → full schema when needed
4. call_tool(server, tool, args) → execute

### For Skills
1. list_skills() → lightweight metadata (~100 tokens/skill)
2. get_skill(name="...") → full content when needed
3. search_skills(query="...") → find relevant skills

NEVER load all schemas/skills upfront - it wastes context.
```

### 1.4 `committing-changes` (~40 lines)

**Purpose**: Commit message format and close flow
**Triggers**: Ready to commit, closing tasks, "how do I commit"

```markdown
## Commit and Close Workflow

1. Stage changes: git add <files>
2. Commit with task ID: git commit -m "[task-id] type: description"
3. Close task: call_tool("gobby-tasks", "close_task", {task_id, commit_sha})

### Valid commit types
feat, fix, refactor, test, docs, chore
```

**Location**: `src/gobby/install/shared/skills/` (bundled with Gobby)

---

## Phase 2: Slim CLAUDE.md (Building Gobby)

CLAUDE.md now focuses ONLY on developing the Gobby codebase itself.
All "how to use Gobby" content moves to FastMCP instructions.

Reduce from 1,101 lines to ~300 lines.

### 2.1 Keep (Gobby Development)
- **Project Overview** (~50 lines): What Gobby is, architecture
- **Directory Structure** (~50 lines): Source tree, key files
- **Development Commands** (~30 lines): uv, pytest, ruff, mypy
- **Code Conventions** (~50 lines): Types, async, PostgreSQL patterns
- **Testing Patterns** (~50 lines): Fixtures, markers, async tests
- **Troubleshooting** (~50 lines): Common dev issues

### 2.2 Remove (Now in FastMCP Instructions or Skills)
- Startup sequence → FastMCP instructions
- Progressive disclosure → FastMCP instructions
- Task workflows → FastMCP instructions + skills
- Session ID handling → FastMCP instructions
- MCP tool schemas → Progressive disclosure
- Skill index → `list_skills()` at runtime

### 2.3 New Structure

```markdown
# CLAUDE.md (~300 lines)

## Project Overview
Gobby is a local-first daemon that unifies AI coding assistants...
[Architecture diagram, key concepts]

## Directory Structure
src/gobby/
├── cli/           # CLI commands (Click)
├── mcp_proxy/     # MCP server and tool proxying
├── hooks/         # Hook event system
├── storage/       # PostgreSQL storage layer
...

## Development Commands
uv sync                    # Install dependencies
uv run pytest tests/ -v    # Run tests
uv run ruff check src/     # Lint
uv run mypy src/           # Type check

## Code Conventions
- All functions require type hints
- Use async for I/O-bound operations
- Always use connection context manager for PostgreSQL
- Use structured logging with context

## Testing Patterns
- Use fixtures from tests/conftest.py
- Mark async tests with @pytest.mark.asyncio
- Use markers: unit, slow, integration, e2e

## Common Issues
| Issue | Solution |
|-------|----------|
| Import errors | Run `uv sync` |
| Test failures | Check fixtures in conftest.py |
```

**Note**: No mention of startup sequence, progressive disclosure, or task rules.
Those are in FastMCP instructions for ALL Gobby users, not just Gobby developers.

---

## Phase 3: Update Hook Error Messages

When hooks block actions, include skill references:

### 3.1 Edit/Write Blocked (No Active Task)
**Current**: Generic error
**New**:

```text
Blocked: No active task. Create or claim a task before editing files.
See skill (MCP): claiming-tasks
```

### 3.2 Unknown Tool Called
**Current**: Tool not found
**New**:

```text
Tool not found. Use progressive disclosure:
1. list_tools(server="...") to discover tools
2. get_tool_schema(server, tool) for full schema
See skill (MCP): discovering-tools
```

**Files to modify**:
- `src/gobby/hooks/hook_manager.py` - Add skill hints to block responses
- `src/gobby/workflows/engine.py` - Tool restriction messages

---

## Phase 4: Extract Common Skill Patterns

Create `SKILL-COMMON.md` that skills can reference (or inline):

### 4.1 Session Context Block
Currently duplicated in 8+ skills:
```markdown
## Session Context
Your session_id is injected at session start. Look for:
session_id: <uuid>
Use this in all task operations.
```

### 4.2 Error Handling Block
```markdown
## Error Handling
- Check `success` field in responses
- On failure, check `error` field for details
- Common errors: missing session_id, task not found
```

### 4.3 Implementation Options

**Option A**: Shared file that skills import (complex)
**Option B**: Skill template with standard sections (simpler)
**Option C**: Just document the pattern, let skills duplicate (current)

**Recommendation**: Option B - create a template, update skills incrementally

---

## Phase 5: MCP Server Instructions (Building WITH Gobby)

Add instructions directly to the MCP server. This is how agents in ANY project learn to use Gobby.

**File**: `src/gobby/mcp_proxy/instructions.py`

```python
def build_gobby_instructions() -> str:
    """Build XML-structured instructions for Gobby MCP server.

    These instructions teach agents how to use Gobby correctly.
    Every agent connecting to Gobby receives these automatically.
    """
    return """<gobby_system>

<startup>
At the start of EVERY session:
1. `list_mcp_servers()` — Discover available servers
2. `list_skills()` — Discover available skills
3. Session ID: Look for `session_id: <uuid>` in your context.
   If missing, call:
   `call_tool("gobby-sessions", "get_current_session", {"external_id": "<your-session-id>", "source": "claude"})`
</startup>

<tool_discovery>
NEVER assume tool schemas. Use progressive disclosure:
1. `list_tools(server="...")` — Lightweight metadata (~100 tokens/tool)
2. `get_tool_schema(server, tool)` — Full schema when needed
3. `call_tool(server, tool, args)` — Execute
</tool_discovery>

<skill_discovery>
Skills provide detailed guidance. Use progressive disclosure:
1. `list_skills()` — Already done at startup
2. `get_skill(name="...")` — Full content when needed
3. `search_skills(query="...")` — Find by task description
</skill_discovery>

<rules>
- Create/claim a task before editing files
- Pass session_id to create_task (required), claim_task (required), and close_task (optional, for tracking)
- NEVER load all tool schemas upfront — use progressive disclosure
</rules>

</gobby_system>"""
```

**Update server.py and stdio.py**:
```python
from gobby.mcp_proxy.instructions import build_gobby_instructions

mcp = FastMCP("gobby", instructions=build_gobby_instructions())
```

---

## Verification Plan

### Test 1: New Session Follows Progressive Disclosure
1. Start fresh Claude Code session
2. Verify agent calls `list_mcp_servers()` first
3. Verify agent calls `list_skills()` early
4. Verify agent doesn't assume tool schemas

### Test 2: Edit Blocked Without Task
1. Try to edit file without active task
2. Verify error message references `claiming-tasks` skill
3. Invoke skill and follow instructions
4. Verify edit succeeds after task creation

### Test 3: Skill Progressive Disclosure
1. Call `list_skills()` - verify lightweight output
2. Call `get_skill(name="gobby-tasks")` - verify full content
3. Call `search_skills(query="commit")` - verify relevant results

### Test 4: CLAUDE.md Token Reduction
1. Measure current CLAUDE.md token count (~4,000+ tokens)
2. After slimming, verify <1,500 tokens
3. Verify critical invariants still present

---

## File Changes Summary

### New Files
- `src/gobby/mcp_proxy/instructions.py` - MCP server instructions builder
- `src/gobby/install/shared/skills/starting-sessions/SKILL.md` - Micro-skill
- `src/gobby/install/shared/skills/claiming-tasks/SKILL.md` - Micro-skill
- `src/gobby/install/shared/skills/discovering-tools/SKILL.md` - Micro-skill
- `src/gobby/install/shared/skills/committing-changes/SKILL.md` - Micro-skill
- `docs/skills/` - Mirror of bundled skills for GitHub users (human-readable reference)

### Modified Files
- `src/gobby/mcp_proxy/server.py` - Add `instructions` parameter to FastMCP
- `src/gobby/mcp_proxy/stdio.py` - Add `instructions` parameter to FastMCP
- `CLAUDE.md` - Slim from 1,101 to ~250 lines
- `AGENTS.md` - Align with new CLAUDE.md structure
- `GEMINI.md` - Align with new CLAUDE.md structure
- `src/gobby/hooks/hook_manager.py` - Add skill hints to errors
- `src/gobby/adapters/claude_code.py` - Session startup checklist (optional, may not be needed with MCP instructions)
- `src/gobby/adapters/gemini.py` - Session startup checklist (optional)
- `src/gobby/adapters/codex.py` - Session startup checklist (optional)

### Removed/Modified (MCP-Only Migration)
- `src/gobby/cli/installers/claude.py` - Remove `install_shared_skills()` call
- `src/gobby/cli/installers/gemini.py` - Remove `install_shared_skills()` call
- `src/gobby/cli/installers/codex.py` - Remove `install_shared_skills()` call
- `src/gobby/cli/installers/antigravity.py` - Remove `install_shared_skills()` call
- `src/gobby/cli/installers/shared.py` - Remove `install_shared_skills()` function (no longer needed)
- `src/gobby/runner.py` or `src/gobby/storage/skills.py` - Auto-sync bundled skills to DB on startup

### Installation Architecture Change

**Old approach** (slash commands):

```text
gobby install → copies skills to .claude/skills/ → exposed as /skill-name
```

**New approach** (MCP-only):

```text
gobby install → skills bundled in package → synced to DB on startup → accessed via list_skills()/get_skill()
```

**Skill locations**:

| Location | Purpose | Audience |
| :--- | :--- | :--- |
| `src/gobby/install/shared/skills/` | Bundled with package (pip/uvx) | Internal - auto-synced to DB |
| `docs/skills/` | Reference for GitHub users | External - manual reference |
| `.claude/skills/` | **REMOVED** - backup existing | N/A |

**Migration for existing installations**:
- Move `.claude/skills/*` to `.claude/skills.backup/` (preserve user customizations)
- Remove `.claude/commands/` references if Gobby-related

**For pip/uvx users**:
- Skills bundled in `src/gobby/install/shared/skills/`
- Auto-synced to Gobby database on daemon start
- Accessed via `list_skills()` → `get_skill()`

**For GitHub users** (reading source):
- Skills documented in `docs/skills/` folder
- Same content as bundled skills, human-readable location

### Optional (Phase 4)
- Existing skills in `src/gobby/install/shared/skills/*/SKILL.md` - Remove duplicated context

---

## Implementation Order

1. **Add MCP server instructions** - Create `instructions.py`, add to FastMCP
2. **Remove skill installation to .claude/skills/** - Remove from all installers
3. **Backup existing .claude/skills/** - Move to `.claude/skills.backup/` on next install
4. **Ensure DB sync on startup** - Skills from `install/shared/skills/` → Gobby database
5. **Create micro-skills** - Add guardrail skills to `install/shared/skills/`
6. **Create docs/skills/** - Mirror of bundled skills for GitHub reference
7. **Update hook error messages** - Add skill hints to block responses
8. **Slim CLAUDE.md** - Reduce to ~300 lines (Gobby development only)
9. **Update AGENTS.md/GEMINI.md** - Follow CLAUDE.md changes
10. **Extract common patterns** - Optional, incremental

Note: Session startup hooks may not be needed if MCP instructions work well.

---

## Open Questions (Resolved)

1. **Skill naming**: Use gerund form per Anthropic best practices
   - `starting-sessions`, `claiming-tasks`, `discovering-tools`, `committing-changes`
   - Drop `gobby-` prefix for micro-skills (distinguishes from server-mapped skills)

2. **Macro vs micro skills**: Keep both
   - **Macro skills** (gobby-tasks, etc.): Map to MCP servers, comprehensive guides
   - **Micro skills**: Guardrails/remediation, triggered by errors or specific questions

3. **Documentation location**: Skills are the documentation
   - CLAUDE.md becomes routing layer pointing to skills
   - No separate reference doc needed (skills ARE the reference)
   - Progressive skill disclosure (`list_skills` → `get_skill`) provides access
