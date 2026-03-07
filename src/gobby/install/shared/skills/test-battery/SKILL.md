---
name: test-battery
description: "Use when user asks to 'run test battery', 'orchestrator test', 'e2e test', 'test the orchestrator'. Interactive skill that walks through the orchestrator test battery step by step."
version: "1.1.0"
category: testing
triggers: test battery, orchestrator test, run test battery, e2e test
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby test-battery — Orchestrator Test Battery

Interactive skill that walks through the orchestrator test battery documented in `docs/guides/orchestrator-test-battery.md`. Each section becomes a checkpoint with structured output.

## Before Starting

1. Read the full test battery reference: `docs/guides/orchestrator-test-battery.md`
2. Verify prerequisites:
   - Daemon running: `gobby status`
   - Clean git state: `git status`
   - No other orchestrator pipelines running

## Workflow

Walk through each section interactively, reporting results as you go.

### Section 1: Setup

1. Create a minimal test plan at `.gobby/plans/test-battery.md` with 2-3 code tasks and 1 docs task
2. Create an epic task from the plan
3. Report:
   ```
   Section 1: Setup
   ✓ Plan created: .gobby/plans/test-battery.md
   ✓ Epic created: #<N> "Test Battery Feature"
   ```

### Section 2: Expansion

1. Run the `expand-task` pipeline on the epic
2. Verify subtasks were created with categories, validation criteria, and dependencies
3. If TDD is enabled, verify sandwich pattern
4. Check file annotations
5. Report:
   ```
   Section 2: Expansion
   ✓ Pipeline completed: <execution_id>
   ✓ Subtasks created: N tasks
   ✓ Dependencies wired: [list]
   ✓ File annotations: [count] files annotated
   ✗ TDD sandwich: [issue description]  (if applicable)
   ```

### Section 3: Orchestrator Run

1. Start the orchestrator pipeline with `continuation_prompt` (do NOT use `wait_for_completion` — the orchestrator is event-driven and each pass completes quickly)
2. Monitor progress — each pass is a separate pipeline execution triggered by agent completions. Check:
   - Worktree creation
   - Developer dispatch
   - Step workflow transitions
   - QA review
   - Merge
3. Report per-check:
   ```
   Section 3: Orchestrator Run
   ✓ Pipeline started: <execution_id>
   ✓ Worktree created: epic-<N>
   ✓ Developer dispatched: agent ar-<id>
   ✓ Step workflow: claim → implement → terminate
   ✓ QA review: task approved
   ✓ Merge: epic branch merged to target
   ```

**Monitoring:** Each orchestrator pass is a separate execution. The `continuation_prompt` fires when each pass completes — check `orchestration_complete` in the result to know when the overall work is finished. Do NOT block with `wait_for_completion`; let the continuation notify you. Use `gobby pipelines history orchestrator` to see the chain of passes, or check task states directly with `gobby tasks list --parent <epic_id>`.

### Section 3.7: Standalone Task Test (Optional)

1. Create a single non-epic task
2. Run the orchestrator on it
3. Verify it completes dev → QA → merge → close across event-driven passes
4. Report:
   ```
   Section 3.7: Standalone Task Test
   ✓ Standalone detected (is_standalone: true)
   ✓ Developer dispatched for task itself
   ✓ QA dispatched on needs_review
   ✓ Merge dispatched on review_approved
   ✓ orchestration_complete: true
   ```

### Section 4: Cleanup

1. Verify all tasks closed
2. Clean up test files and worktrees
3. Report:
   ```
   Section 4: Cleanup
   ✓ All subtasks closed
   ✓ Test files removed
   ✓ Worktree cleaned up
   ```

### Section 5: Issue Tracking

Create gobby tasks for any issues discovered during the test:

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Bug: <description>",
    "task_type": "bug",
    "description": "Found during orchestrator test battery: ...",
    "session_id": "<session_id>"
})
```

### Section 6: Final Report

Compile the full results:

```
═══════════════════════════════════════════
  Orchestrator Test Battery Results
═══════════════════════════════════════════

Section 1: Setup         [2/2 PASS]
Section 2: Expansion     [4/4 PASS]
Section 3: Orchestrator  [6/7 PASS, 1 FAIL]
Section 4: Cleanup       [2/2 PASS]

Total: 14/15 PASS

Issues Found: 2
  #<N> Bug: QA agent couldn't see worktree files
  #<N> Bug: Merge agent timed out on large diff

Overall: PASS WITH ISSUES
═══════════════════════════════════════════
```

## Tips

- Use `gobby pipelines history orchestrator` to see the chain of event-driven passes
- Use `gobby pipelines status <id>` to check a specific pass and its `orchestration_complete` result
- Use `gobby agents ps` to see running agents
- Use `gobby tasks list --parent <epic_id>` to check task states
- If an agent gets stuck, use `gobby agents kill <run_id>`
- The full battery typically takes 10-30 minutes depending on task complexity and LLM speed
