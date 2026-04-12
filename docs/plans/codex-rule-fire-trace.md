# Rule Fire Trace: "Create a Python file" -> Task Close

This traces every rule that fires from the moment Codex receives the user directive through task closure, in chronological order. Rules are grouped by the Codex hook event that triggers them.

## Codex Event Mapping

| Codex Hook Event | Internal Event | Model-facing Output |
|---|---|---|
| `SessionStart` | `session_start` | `additionalContext` |
| `UserPromptSubmit` | `before_agent` | `additionalContext` |
| `PreToolUse` | `before_tool` | `systemMessage` |
| `PostToolUse` | `after_tool` | `additionalContext` |
| `Stop` | `turn_end` | `systemMessage` |

---

## Phase 0: Session Start (`SessionStart`)

These already fired when the session began. They establish the variable state everything else depends on.

| # | Rule | Effect | Notes |
|---|---|---|---|
| 1 | `capture-baseline-dirty-files-on-start` | `mcp_call` -> captures git dirty files | Always fires |
| 2 | `reset-memory-tracking-on-start` | `set_variable` | Resets memory tracking vars |
| 3 | `reset-plan-mode-on-session-start` | `set_variable` | Clears `plan_mode` |
| 4 | `reset-progressive-discovery` | `set_variable` x3 | Only on `clear`/`compact` source. Resets `unlocked_tools`, `servers_listed`, `listed_servers` |
| 5 | `reset-skill-injection` | `set_variable` | Clears `injected_skills` |
| 6 | `clear-pending-context-reset-on-start` | `set_variable` | Clears pending context reset flag |
| 7 | `inject-task-context-on-start` | `inject_context` | Injects any active task context (skipped on resume) |
| 8 | `inject-previous-session-summary` | `inject_context` | Injects prior session summary if available |
| 9 | `inject-compact-handoff` | `inject_context` | Only fires when `source == 'compact'` |
| 10 | `pipeline-auto-run` | conditional | Auto-runs pipelines if configured |

---

## Phase 1: User Submits "Create a Python file that does X" (`UserPromptSubmit`)

| # | Rule | Effect | Condition |
|---|---|---|---|
| 1 | `memory-recall-on-prompt` | `mcp_call` -> `search_memories(limit=2, min_score=0.6)`, result injected | Prompt >= 6 words |
| 2 | `memory-capture-nudge` | `inject_context` + sets `memory_nudge_fired=true` | Once per session, prompt >= 10 chars, not a slash command |

Other `before_agent` rules (`deliver-pending-messages`, `activate-pending-command`, `prepare-clear-handoff`, `inject-autonomous-mode`, `handle-plan-mode-entry`) have conditions that won't match for a normal interactive session receiving a fresh directive.

---

## Phase 2: Progressive Discovery -> Task Creation

The agent must create/claim a task before editing files. This requires walking the progressive discovery chain.

### 2a. `list_mcp_servers()` -- if not already cached

**PreToolUse:** No blocking rules.
**PostToolUse:**

| Rule | Effect |
|---|---|
| `track-servers-listed` | Sets `servers_listed = true` |

### 2b. `list_tools(server_name="gobby-tasks")`

**PreToolUse:** No blocking rules.
**PostToolUse:**

| Rule | Effect |
|---|---|
| `track-listed-servers` | Appends `"gobby-tasks"` to `listed_servers` |

### 2c. `get_tool_schema(server_name="gobby-tasks", tool_name="create_task")`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `require-server-listed-for-schema` | **PASSES** -- `gobby-tasks` is in `listed_servers` |

**PostToolUse:**

| Rule | Effect |
|---|---|
| `track-schema-lookup` | Appends `"gobby-tasks:create_task"` to `unlocked_tools` |
| `inject-task-creation-on-schema` | `mcp_call` -> `get_skill(name="task-creation")`, result injected. Sets `injected_skills += ['task-creation']` |

### 2d. `call_tool(server_name="gobby-tasks", tool_name="create_task", args={..., claim: true})`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `require-schema-before-call` | **PASSES** -- `gobby-tasks:create_task` is in `unlocked_tools` |

**PostToolUse:**

| Rule | Effect |
|---|---|
| `track-task-claim` | `observe` -- the `detect_task_claim` observer sets `task_claimed=true` and records the task ID |

---

## Phase 3: Reading Existing Code for Context

### 3a. First `Read` of a code file (e.g., `src/something.py`)

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `block-and-teach-code-index` | **BLOCKS** -- `code_index_loaded` is false, file extension is `.py`. Injects code-index skill, sets `code_index_loaded=true` |

The agent is told to use `gcode` instead. It retries.

### 3b. Second `Read` of a `.py` file (or after gcode exploration)

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `block-and-teach-code-index` | **PASSES** -- `code_index_loaded` is now true |
| `inject-python-skill` | Fires: injects Python coding standards skill, sets `injected_skills += ['python']` |

---

## Phase 4: Writing the Python File

### 4a. First `Write` or `Edit` of a code file

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `require-task-before-edit` | **PASSES** -- `task_claimed` is true |
| `block-and-teach-context7` | **BLOCKS** -- `context7_loaded` is false, file extension is `.py`. Injects context7 skill, sets `context7_loaded=true` |

Agent is told to check library docs first. It retries.

### 4b. Second `Write` or `Edit`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `require-task-before-edit` | **PASSES** |
| `block-and-teach-context7` | **PASSES** -- `context7_loaded` is now true |
| `block-edits-plan-mode` | **PASSES** -- not in plan mode |

File is written successfully.

---

## Phase 5: Running Tests

### `Bash(command="uv run pytest tests/test_new_file.py -v")`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `compress-bash-output` | **REWRITES** -- wraps command through `gsqz` output compressor. Auto-approved. |
| `require-uv` | Evaluates but inner `when` doesn't match -- command already uses `uv run`, not bare `python`/`pip` |
| `no-full-pytest-suite` | **PASSES** -- only blocks for `is_spawned_agent` (interactive session) |

**PostToolUse:**

| Rule | Outcome |
|---|---|
| `enforce-tdd-track-tests` | Tracks test file for TDD enforcement |
| `inject-tool-error-recovery` | Only fires **if test command fails** -- injects recovery guidance |

---

## Phase 6: Lint & Type Check (Error Triage)

### `Bash(command="uv run ruff check src/new_file.py")`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `compress-bash-output` | **REWRITES** through gsqz |

### `Bash(command="uv run mypy src/new_file.py")`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `compress-bash-output` | **REWRITES** through gsqz |

### `set_variable(name="errors_resolved", value=true, session_id="...")`

Clears the error-triage gate for Phase 8.

---

## Phase 7: Committing

### `Bash(command="git add src/new_file.py tests/test_new_file.py")`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `compress-bash-output` | **REWRITES** through gsqz |

### `Bash(command="git commit -m '[gobby-#N] feat: add ...' ")`

**PreToolUse:**

| Rule | Outcome |
|---|---|
| `compress-bash-output` | **REWRITES** through gsqz |

**PostToolUse:** The commit SHA is detected by the session layer (not a rule -- this is the `CommitDetector` observer). It links the commit to the active task and sets `task_has_commits=true`.

---

## Phase 8: Memory Review & Task Closure

### `set_variable(name="memory_review_completed", value=true, session_id="...")`

Clears the memory-review gate.

### `get_tool_schema(server_name="gobby-tasks", tool_name="close_task")`

**PostToolUse:**

| Rule | Effect |
|---|---|
| `track-schema-lookup` | Unlocks `gobby-tasks:close_task` |
| `inject-transition-skill` | Injects task-transitions skill |

### `call_tool(server_name="gobby-tasks", tool_name="close_task", args={task_id: "#N"})`

**PreToolUse** -- this is the gauntlet. All four gates evaluate:

| Rule | Outcome | Why |
|---|---|---|
| `require-schema-before-call` | **PASSES** | `close_task` is unlocked |
| `require-clean-tree-before-status` | **PASSES** | Working tree is clean (committed in Phase 7) |
| `require-commit-before-status` | **PASSES** | `task_has_commits=true` (set by commit detector) |
| `require-error-triage-before-status` | **PASSES** | `errors_resolved=true` (set in Phase 6) |
| `require-memory-review-before-status` | **PASSES** | `memory_review_completed=true` (set above) |
| `strip-skip-validation-with-commit` | Only fires if `skip_validation` was passed -- strips it since commits exist |

If any gate **BLOCKS**, the agent gets a specific error telling it what to do. Common failure modes:

- Forgot to commit -> `require-clean-tree-before-status` blocks
- Committed but SHA not detected -> `require-commit-before-status` blocks
- Didn't run lint/tests -> `require-error-triage-before-status` blocks
- Didn't review memories -> `require-memory-review-before-status` blocks

---

## Phase 9: Turn End (`Stop`)

| Rule | Outcome | Condition |
|---|---|---|
| `require-task-close` | **PASSES** | Task was closed in Phase 8, `task_claimed` is now false |
| `require-step-completion` | **PASSES** | No incomplete steps |
| `require-epic-tree-close` | Conditional | Only fires if this was an epic with subtasks |
| `guide-task-continuation` | Conditional | Only fires if `auto_task_ref` set with incomplete subtasks |
| `notify-task-tree-complete` | Conditional | Fires if task tree is fully complete |
| `digest-on-response` | `mcp_call` (background) | Always fires -- builds turn record for digest |

If `require-task-close` **BLOCKS** (task still claimed), and the agent tries `AskUserQuestion`:

| Rule | Outcome |
|---|---|
| `block-ask-during-stop-compliance` | **BLOCKS** -- forces the agent to commit/close instead of asking the user |

---

## Summary: Rule Fire Count by Phase

| Phase | Event | Rules Evaluated | Blocks |
|---|---|---|---|
| 0. Session Start | `SessionStart` | ~10 | 0 |
| 1. Prompt | `UserPromptSubmit` | 2 | 0 |
| 2. Task Creation | `PreToolUse` x4, `PostToolUse` x4 | ~8 | 0 (if discovery followed) |
| 3. Read Code | `PreToolUse` x2 | 2-3 | 1 (code-index teach) |
| 4. Write File | `PreToolUse` x2 | 3-4 | 1 (context7 teach) |
| 5. Run Tests | `PreToolUse`+`PostToolUse` | 3-4 | 0 |
| 6. Lint/Check | `PreToolUse` x2 | 2 | 0 |
| 7. Commit | `PreToolUse` x2 | 2 | 0 |
| 8. Close Task | `PreToolUse` | 5-6 | 0 (if gates cleared) |
| 9. Stop | `Stop` | 3-5 | 0 (if task closed) |

**Total: ~40-50 rule evaluations, 2 expected teaching blocks (code-index + context7) on first use.**

---

## Key Variables to Track for Testing

| Variable | Set By | Checked By |
|---|---|---|
| `task_claimed` | `track-task-claim` observer | `require-task-before-edit`, `require-task-close`, `block-native-task-tools-unclaimed` |
| `task_has_commits` | Commit detector observer | `require-commit-before-status`, `strip-skip-validation-with-commit` |
| `code_index_loaded` | `block-and-teach-code-index` | `block-and-teach-code-index` |
| `context7_loaded` | `block-and-teach-context7` | `block-and-teach-context7` |
| `errors_resolved` | Manual `set_variable` | `require-error-triage-before-status` |
| `memory_review_completed` | Manual `set_variable` | `require-memory-review-before-status` |
| `unlocked_tools` | `track-schema-lookup` | `require-schema-before-call` |
| `listed_servers` | `track-listed-servers` | `require-server-listed-for-schema` |
| `servers_listed` | `track-servers-listed` | Progressive discovery chain |
| `injected_skills` | Various skill injection rules | Prevents double-injection |
| `stop_attempts` | Stop hook increment | `require-task-close`, `block-ask-during-stop-compliance` |
