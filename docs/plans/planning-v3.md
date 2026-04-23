# Plan: Fresh-Context Planner + First-Class `plan_file` (Pre-Launch Foundation)

## Context

`/gobby plan` works well interactively but has a concrete cost problem: the skill-executing session is also the planner session, so it accumulates across rounds — observed at ~40% of 1M tokens on non-trivial tasks. That drives token cost per round, risks mid-loop compaction (losing plan coherence), and creates cache-invalidation pressure. Adversary already gets fresh context each round; planner does not. Fixing the planner's context burn symmetrically is the pre-launch technical win.

Orthogonal but cheap: `.gobby/plans/` is git-ignored and not auto-created, `plan_file` has no schema presence on tasks, and there's no guard keeping plan references canonical. This epic lands the small foundation pieces alongside the context fix.

**Explicitly out of scope for launch:** fire-and-forget cron-tick loop, `plan_review_state` JSON blob, new MCP namespace, launching-session notification primitive. Those are the right 1.0 story paired with the back-half pipeline overhaul — they're not launch-blocking. Interactive watch-it-happen UX is fine for the "come look at my product" launch and preserves the open contribution surface.

Intended outcome: planner token cost per round stays bounded (fresh session each revision), plan artifact has a first-class path column usable by `expand-task` and future spec-review work, `.gobby/plans/` becomes a git-tracked canonical location with a validation guard keeping agents from drifting paths.

## Recommended Approach

**Context fix — fresh planner per round via in-skill spawn:** `/gobby plan` Step 7 already spawns a fresh adversary agent and waits for verdict. Use the same pattern for the planner on revision rounds: on adversary rejection, the skill spawns a fresh `planner` agent with `context={plan_file, round: N, prior_findings_ref}` instead of doing the revision work in-session. Planner agent reads `plan_file` from disk, reads prior `## Adversary Findings — Round N` headings from task description (already appended by `mark_task_review_rejected`), revises the plan file, calls `mark_task_needs_review`, terminates. Skill session resumes, spawns adversary as today. Skill session holds the orchestration state (round counter, config, max_rounds) in session vars — no new blob, no new table. Bounded planner context per round.

**Plan file foundation:** Add `plan_file TEXT` column to tasks with a validation guard in `create_task`/`update_task` keeping the path under `.gobby/plans/task-<id>-<slug>.md`. Auto-create `.gobby/plans/` on `gobby init`. Remove the `.gitignore` entry so plans track in git. Update the skill to write `task-<id>-<slug>.md` (from `task-<id>-plan.md`) and persist the relative path via `update_task(plan_file=...)`. Update `expand-task` to propagate the parent's `plan_file` to each child — child dev agents get the plan as big-picture read-only context.

**What stays as it is:** Interactive skill flow (Steps 1–6 unchanged, Step 9 terminal unchanged). No cron. No fire-and-forget. Adversary already respawns per round. The only orchestration change is planner revisions also respawn.

## Files to Touch (by subtask)

### Subtask 1: Add `plan_file` column to tasks

- `src/gobby/storage/tasks/_models.py:172` — add `plan_file: str | None = None` to `Task` dataclass
- `src/gobby/storage/tasks/_models.py:263` — include `plan_file` in `to_dict()`
- `src/gobby/storage/tasks/_models.py:314` — include `plan_file` in `to_brief()`
- `src/gobby/storage/migrations.py` — new callable migration (next version, likely v218) using `_add_column_if_missing(db, "tasks", "plan_file TEXT", "plan_file")` pattern (template: `_migrate_task_lifecycle_stage` at line 483); bump `BASELINE_VERSION`
- `src/gobby/storage/baseline_schema.sql:300` — add `plan_file TEXT` to tasks DDL
- Tests in `tests/storage/test_storage_tasks.py` — column presence, serialization round-trip, existing rows get NULL

### Subtask 2: `plan_file` validation guard in MCP tools

- `src/gobby/mcp_proxy/tools/tasks/_crud.py:34` (create_task) — accept `plan_file` param; validate regex `^\.gobby/plans/task-\d+-[\w-]+\.md$`; reject `~/.claude/plans/*`, `~/.gemini/plans/*`, `~/.codex/plans/*`, absolute paths, `..` traversal; use the error-dict pattern at lines 103–107
- `src/gobby/mcp_proxy/tools/tasks/_crud.py:389` (update_task) — same guard
- Error copy: `"plan_file must be a relative path matching .gobby/plans/task-<id>-<slug>.md. This field is reference-only context assigned by the planning workflow — it is not where you plan your own task. For internal task breakdown, use your CLI's native plan mode or decompose via create_task."`
- Tests in `tests/mcp_proxy/tools/test_tasks_crud.py` — reject CLI-native paths, absolute, traversal; accept canonical; round-trip via `get_task`

### Subtask 3: Auto-create `.gobby/plans/` on init; un-ignore

- `src/gobby/utils/project_init.py:356` — after `gobby_dir.mkdir(exist_ok=True)`, add `(gobby_dir / "plans").mkdir(exist_ok=True)`
- `.gitignore:233` — remove `.gobby/plans/` line
- Tests in `tests/utils/test_project_init.py` — assert `.gobby/plans/` exists after init

### Subtask 4: Fresh-context planner on revision rounds

Edit `src/gobby/install/shared/skills/plan/SKILL.md`:

- **Step 4 (Write Plan Document):** canonical filename changes from `.gobby/plans/task-<parent_seq>-plan.md` to `.gobby/plans/task-<parent_seq>-<slug>.md`. Slug = kebab-case of planning-task title, ASCII-only, max ~40 chars. After write, call `update_task(task_id=planning_task_id, plan_file="<relative-path>")` to persist.
- **Step 7 (Review Loop):** keep the existing spawn-adversary-and-wait structure. On adversary rejection (task transitions back to `open`), **replace the in-session revision step with a fresh planner spawn**:
  1. `spawn_agent(agent='planner', assigned_task_id=planning_task_id, context={plan_file, round: current_round + 1, max_rounds})`
  2. Wait for the planner run to terminate (same session-var tracking pattern the skill already uses for `adversary_run_id` — track `planner_run_id` and await termination).
  3. Read current task status. If `needs_review` (planner submitted), proceed to spawn next adversary round as today. If `escalated`, follow existing escalation handling.
  4. Skill session does not hold plan content between rounds — it only holds orchestration state (round counter, max_rounds, planner/adversary config, launching context). All plan content lives in `.gobby/plans/task-<id>-<slug>.md` on disk (the planner reads/writes) and `## Adversary Findings — Round N` headings in task description (the adversary appends on rejection, the next planner reads).
- **Planner agent prompt (`src/gobby/install/shared/workflows/agents/planner.yaml`):** add a small round-aware clause — when `context.round > 1`, instruct the planner to read the task description's `## Adversary Findings — Round N` sections (most recent first) and the current `plan_file` content, then revise the plan file in place and call `mark_task_needs_review`. For round 1 (should not occur in this skill's flow since initial draft is interactive in Step 4, but useful for the autonomous path if reused) — draft from scratch.
- **Steps 1–6, 8, 9:** unchanged.

Tests: manual E2E in verification. If there's a unit test for the skill flow, update its fixtures to reflect the new spawn path.

### Subtask 5: `expand-task` subtask `plan_file` inheritance

- `src/gobby/install/shared/workflows/pipelines/expand-task.yaml` and backing compiler/task-creator logic in `src/gobby/tasks/expansion.py` / `expansion_service.py`
- When creating child tasks, pass the parent's `plan_file` to each child's `create_task(plan_file=...)` call — children inherit the same canonical path
- Guard validates each pass-through unchanged
- Subtask prompts / task descriptions should include a note: "`plan_file` is big-picture context only. Your scope is this task's stated work; do not expand scope based on plan_file content. Validator enforces task-scoped diffs."
- Tests: parent with `plan_file=.gobby/plans/task-42-foo.md` produces children all carrying the same `plan_file`

### Subtask 6: Grandfather existing plans + tidy `.gobby/plans/`

- Leave existing `.gobby/plans/task-<id>-plan.md` files untouched and readable. No rename migration.
- **Move `.gobby/plans/brand-standards.md` → `docs/plans/abandoned/brand-standards.md`** (it's not a plan, it's an out-of-date doc that ended up in the wrong place). `.gobby/plans/` becomes strictly task-plan files going forward, which makes the "every file maps to a task_id" invariant cleanly enforceable later.
- Existing tasks have `plan_file=NULL` — column is nullable, no backfill required.

## Functions/Utilities to Reuse

- `_add_column_if_missing(db, table, column_def, column_name)` — standard ALTER pattern in `storage/migrations.py`
- `spawn_agent` (gobby-agents MCP) — already the mechanism for spawning adversary in Step 7; planner dispatch uses the same call with agent='planner'
- Existing session-var tracking in the skill for `adversary_run_id` — extend with `planner_run_id` for round revisions
- `mark_task_needs_review` / `mark_task_review_approved` / `mark_task_review_rejected` / `escalate_task` — verdicts unchanged; #12617 fix makes `in_progress → open` reject native
- Error-dict rejection pattern from `_crud.py:103–107` — template for the `plan_file` guard

## Verification

1. **Storage:** `uv run pytest tests/storage/test_storage_tasks.py tests/mcp_proxy/tools/test_tasks_crud.py -v` — column presence, serialization, guard rejects bad paths, accepts canonical.
2. **Init:** `cd /tmp/fresh && uv run gobby init && ls -la .gobby/plans/` — directory present.
3. **Gitignore:** `git check-ignore .gobby/plans/task-1-foo.md; echo exit=$?` — exit 1 (not ignored).
4. **E2E interactive planning:**
   - `uv run gobby start --verbose`
   - In Claude Code session: `/gobby plan "Add a Reset button to the dashboard"`
   - Walk through Steps 1–6 normally. Step 4 writes to `.gobby/plans/task-<id>-<slug>.md`. `uv run gobby tasks show <id>` shows `plan_file` populated.
   - At Step 7 review loop, craft the plan thin enough that the adversary rejects round 1.
   - Observe the skill spawning a fresh planner agent (`gobby agents list` shows it), planner terminating, adversary spawning and reviewing the revision. Token count in the skill-executing session stays bounded across rounds.
   - On approval, `expand-task` runs; `uv run gobby tasks list --parent <planning_task_id>` shows children all carrying the same `plan_file` value.
5. **Guard error path:** in a test session, call `update_task(task_id=X, plan_file="~/.claude/plans/foo.md")` — returns the guard error with steering copy.

## Out of Scope (Explicit Deferrals — 1.0 Targets)

- **Fire-and-forget cron-tick loop + `plan-adversary-loop-tick.yaml` pipeline** — bundle with back-half overhaul Phase 1-5 for the "full spec-to-prod automation" 1.0 narrative. Interactive watch-it-happen is launch-right.
- **`plan_review_state` JSON blob column + `gobby-plan-review` MCP namespace + `start`/`tick`/`finalize` tools** — only needed by the cron-tick flow; skill session holds orchestration state in the interactive path.
- **Launching-session notification primitive (`send_message` to launching session, global pager)** — no background loops to notify from.
- **Approval-gate escalation semantics for pipeline-driven loops** — interactive flow escalates via `escalate_task` as today.
- **Rename existing `task-<id>-plan.md` files** — grandfathered.
- **Native pipeline `repeat:` / `for_each:` primitive** — not needed here; reconsider if a cron-tick pattern lands post-launch and wants a native abstraction.
- **Validator enforcement of `plan_file` scope boundary** — prompt-level hint on subtasks is enough for launch; dedicated scope-guard hook is future hardening.
