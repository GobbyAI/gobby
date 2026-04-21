# Interactive Plan-Adversary Loop for `/gobby plan`

## Context

Today the user runs this loop by hand across **two terminal sessions**: one holds the pair-programming planner (Claude), the other holds the Codex-backed adversary. The plan file is copy-pasted back and forth. We want one session — CLI *or* web UI — to drive the whole loop: draft → hand to adversary → receive findings → revise → approve → expand.

The autonomous front-half orchestrator (`front_half_tick`, `front-half-orchestrator.yaml`) already does this for spawned-planner flows. We keep it untouched for overnight/autonomous use cases (e.g., a dev agent mid-task deciding it needs planning). This plan only adds an **interactive** path.

### Key design insights

**Spawn auto-handles the claim.** The `spawn_agent` path auto-injects `assigned_task_id` from its `task_id` parameter (`spawn_agent/_implementation.py:375`) and auto-claims the task for the child session before the workflow runs (`:499`). The interactive session neither claims the planning task itself nor passes any `initial_variables` (not on the public MCP schema anyway). The adversary's own `claim_task` step remains as idempotent cleanup, not the primary claim mechanism. No new primitives, no rule relaxation.

**Race prevention comes from the attach-time guard, not from child labels.** `front_half_tick` operates on the PARENT and force-adds `conductor:front-half` to it, then creates its own labeled stage children (`_front_half.py:75`, `:581`). Withholding labels on our interactive planning epic doesn't prevent `front_half_tick` from ticking the parent on its own. The real isolation guarantee is Step 1's hard-block guard: we refuse to attach if the parent is under **active** front-half management (`conductor:front-half` present AND `conductor:front-half-complete` absent, OR any live `conductor-stage:*` child). Parents that previously completed autonomous planning are still attachable for a second interactive pass.

**Methodology (both drafting and reviewing) belongs in dedicated skills.** Today:
- The adversary's review heuristics live inline in `plan-adversary.yaml`.
- The autonomous `planner.yaml` agent has NO drafting methodology — it only gets "draft a plan, mark it for review" with no guidance on phase structure, TDD rules, task format, or categorization. This is a latent quality bug: the autonomous planner produces plans the expand pipeline may struggle to parse.
- The interactive `plan` skill has the full methodology, but it's not reusable.

Fix both at once by extracting two sibling methodology skills:
- `plan-draft` — how to structure a plan document (phases, task format, TDD compatibility, categories, hierarchy, dependency notation).
- `plan-review` — how to review one adversarially (cynical attitude, path walking, traceability, gobby-format checks).

Both the interactive `/gobby plan` skill and the autonomous `planner.yaml` agent load `plan-draft`. The interactive adversarial loop and the autonomous `plan-adversary.yaml` agent load `plan-review`. Single source of truth on each side.

**BMAD review principles are feature-scale friendly even though BMAD's planning suite isn't.** The `.claude/bmad-skills/` directory has three review skills worth mining for `plan-review`:
- `bmad-review-adversarial-general` — attitude: cynical, "at least 10 findings", focus on what's missing not wrong.
- `bmad-review-edge-case-hunter` — method: mechanically walk every branch/boundary; structured JSON findings.
- `bmad-check-implementation-readiness` — traceability: every requirement maps to a plan item; no orphan plan items.

BMAD's full planning suite (PRD → architecture → epics → stories) is project-scale and *not* a fit for our feature/task-scale `/gobby plan` — that remains a separate workstream if ever desired. Review principles, by contrast, apply at both scales.

## Design decisions (final)

| Decision | Choice | Why |
|---|---|---|
| Entrypoint | A/P menu in `skills/plan/SKILL.md` Step 1 — **A**dversarial (new), **P**lain (existing `/gobby expand` handoff). | Gobby ships only `/gobby`; everything is a skill. Works identically in CLI and web UI. We do NOT offer a "custom workflow" branch: the existing plan skill documents `call_tool("gobby-workflows", "activate_workflow", ...)` but that tool is not actually registered (`mcp_proxy/tools/workflows/__init__.py` exposes no `activate_workflow`). Keeping the `plan-expansion` workflow as manual documentation at the bottom of `plan/SKILL.md` — users can activate it through whatever path they use today. Adding a real `activate_workflow` MCP tool is a separate follow-up. |
| Handoff mechanism | `spawn_agent(agent="plan-adversary", task_id=planning_task_id, parent_session_id=<self>)` — spawn auto-injects `assigned_task_id` and auto-claims for the child session | Verified in `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:375` and `:499`. No need for `initial_variables` (not exposed on the public MCP tool anyway). Adversary's own `claim_task` step becomes idempotent cleanup, not the primary claim. |
| Planner claim? | **No** — planner never claims the planning task | Spawn auto-claim handles it; no rule relaxation, no `assign_to_session`, no race. |
| Planning task shape | `task_type="epic"` (not leaf task) | Epics are exempt from `close_task`'s `changes_summary` requirement (`_lifecycle_close.py:122-125`). Cleanly sidesteps leaf-close gymnastics for an orchestration container. |
| Race with autonomous conductor (symmetric) | **Two-sided mutual exclusion with session-owned locks.** (a) Step 1 attach guard: block if parent is under active front-half management OR carries any `interactive:planning-in-progress:*` label from another session. (b) Session-owned lock label: Step 1 applies `interactive:planning-in-progress:<self_session_id>` to the PARENT immediately after the guard passes. (c) New rule `block-front-half-on-interactive-lock` fires `before_tool` on `gobby-tasks-ops:front_half_tick` and blocks if the resolved parent carries any `interactive:planning-in-progress:*` label (rule doesn't care whose; any lock blocks). (d) Terminal cleanup removes only the **current session's** label (`remove_label(...label=f"interactive:planning-in-progress:{self}")`), never another session's. | A bare shared label has no owner and lets two interactive sessions silently stomp on each other (first to exit unlocks the other). Session-qualified labels give every lock an identifiable owner without introducing a new DB field. Rule-based enforcement keeps `_front_half.py` untouched. |
| State persistence | Top-level MCP proxy tools `set_variable(name, value, session_id="#<self>")` and `get_variable(name, session_id=...)` — wired at `server.py:524-525` via `tool_handler.set_variable`/`get_variable`, backed by `mcp_proxy/tools/workflows/_variables.py:63`. | This is distinct from the `gobby-workflows` registry's variable-definition CRUD (that's template management). The proxy-level session-variable pair is the right primitive for skill-state persistence. Call directly; no progressive discovery. |
| Workflow-choice variable name | **`plan_review_mode`** (values: `"adversarial"` or `"plain"`) — **NOT** `plan_mode` | `plan_mode` is an existing boolean used across 10+ enforcement rules (`handle-plan-mode-entry.yaml`, `handle-plan-mode-exit.yaml`, `block-edits-plan-mode.yaml`, `require-task-before-edit.yaml`, `require-task-close.yaml`, `require-epic-tree-close.yaml`, `require-error-triage.yaml`, `reset-plan-mode-on-session-start.yaml`, `require-memory-review-before-status.yaml`). Overloading it with a string value would break those rules, and `ExitPlanMode` forcibly resets it to `false` anyway — our choice would evaporate at round 1's approval boundary. |
| Feedback delivery | `gobby-workflows:wait_for_completion(completion_id=adversary_run_id)` blocks the planner turn | Push-based via `asyncio.Event`. Verified in `src/gobby/events/completion_registry.py:101-120`: no polling, fires immediately on agent exit, and stored results are safe against early completion (subsequent `wait()` returns instantly). `spawn_agent` already registers the `run_id` (`spawn_agent/_factory.py:464`). Works identically in CLI and web UI — it's a daemon-level asyncio primitive. |
| Parent task | Ask at invocation: attach to `#N` or create new `task_type=epic, category=planning` | User-chosen. |
| Round tracking | `planning-round:N` label on the planning task, bumped by the skill between rounds | Matches the label convention already used in `_front_half.py` so the existing `_planning_round` helper could be reused in tests. |
| Orchestration | Skill drives the loop directly | Skill owns turn UX; pipelines are for headless dispatch, which we don't have here. |
| Approval → expansion | Skill invokes `expand-task` pipeline via `run_pipeline` + `wait_for_completion(execution_id=...)` | Reuses the approved hand-off path. Skill exits after expansion; test-architecture stays in autonomous front-half. |
| CLI/web parity | All work flows through daemon + MCP (spawn, wait, get_task, run_pipeline). Nothing CLI-specific. | Explicitly verified end-to-end. |

## Execution plan

### 0. Precursor — resolve the `refactor` category drift

Before extracting `plan-draft` we must fix a real contract drift that would otherwise fossilize in the new skill: `plan/SKILL.md:184` documents `refactor` as a canonical category and `expansion_service.py:566` emits refactor tasks, but `_models.py:VALID_CATEGORIES` and the `create_task` MCP schema (`_crud.py:289`) reject it. Without fixing this, `plan-draft` either lies about the category list or codifies the inconsistency.

Fix: add `refactor` to `VALID_CATEGORIES` (`_models.py:27`) and the `create_task` schema enum (`_crud.py:289`). That aligns the MCP contract with what the rest of the system already produces and consumes. One small DB migration note: no schema change needed (category is a freeform string column with runtime validation), just the enum set.

Tests: update any existing category-validation tests to include `refactor`; add a happy-path `create_task(category="refactor")` test.

### 1. New skill — `src/gobby/install/shared/skills/plan-draft/SKILL.md`

Extract the methodology sections of the current `plan` skill into a standalone skill that both the interactive session and the autonomous planner consume. Structure:

- **Frontmatter**: `name: plan-draft`, `description: "Methodology for drafting a gobby plan document: phases, task format, TDD compatibility, categories, hierarchy, and dependency notation. Use when drafting or revising a plan artifact."`, `category: core`, `metadata.gobby.audience: all` (valid values per `skills/parser.py:62` — `"all"` | `"interactive"` | `"autonomous"` | `"orchestrator"` | `"worker"`). We use `"all"` because the skill is invoked manually via `get_skill` from both interactive sessions and spawned agents; audience-aware injection isn't used for this skill today, but `"all"` keeps the metadata valid if it ever is.
- **Plan structure** (extracted from current `plan/SKILL.md` Steps 2–3): epic title, phases, atomic tasks, dependency notation. Include the example plan template verbatim.
- **Plan content = subtask descriptions** section: extracted verbatim — implementing agents see ONLY their own task section, so each section must be self-contained.
- **TDD compatibility**: no explicit test tasks (`[TDD]`, `[IMPL]`, `[REF]` prefixes forbidden in drafts — expansion auto-inserts them).
- **Task categories**: canonical list (`code`, `config`, `docs`, `refactor`, `test`, `research`, `planning`, `manual`) with which get TDD sandwiches.
- **Phase heading syntax**: canonical `## Phase N: Name`; tolerated variants documented.
- **Task granularity guidelines**: atomic, testable, verb-led, scoped, self-contained.
- **Task hierarchy**: root epic → phase sub-epics → feature tasks → TDD sandwich children (produced by `/gobby expand`).
- **Verification checklist** (extracted from current Step 4): no explicit test tasks, dep tree valid, categories correct, phase syntax matches.

### 2. New skill — `src/gobby/install/shared/skills/plan-review/SKILL.md`

Create a sibling to `plan`. Structure:

- **Frontmatter**: `name: plan-review`, `description: "Review a gobby plan document for missing requirements, bad sequencing, unhandled edge cases, weak testability, and traceability gaps. Use when asked to review or critique a plan."`, `category: core`, `metadata.gobby.audience: all` (per `skills/parser.py:62`; same reasoning as `plan-draft`).
- **Role & attitude section** (adapted from `bmad-review-adversarial-general`, tuned to the existing adversary contract in `plan-adversary.yaml:29`): rigorous reviewer — look for what's missing, not just what's wrong. **No finding quotas.** If the first pass finds nothing, re-check once methodically (walk the method + traceability sections again). If the second pass still finds nothing, approve cleanly — do not manufacture findings. Precise professional tone; no profanity or personal attacks.
- **Method section** (from `bmad-review-edge-case-hunter`): mechanically walk every branching path and boundary implied by the plan. Catalog: missing else/default cases, unguarded inputs, race conditions, timeout gaps, unhandled error paths, data-shape transitions between phases. Report only un-addressed paths.
- **Traceability section** (from `bmad-check-implementation-readiness`): cross-reference the plan's phases/tasks against the parent task's requirements section. Flag: requirements with no matching task, tasks with no corresponding requirement, phase-dependency gaps.
- **Escalation policy**: only escalate with `planning_changes_requested:` when there are **blocking** findings. Non-blocking nits stay in the findings section but do not block approval on their own.
- **Gobby-specific checks**:
  - No explicit test tasks (violates TDD sandwich model).
  - Every `code`/`config` task has a concrete target file path.
  - Task categories valid (`code`, `config`, `docs`, `refactor`, `test`, `research`, `planning`, `manual`).
  - Dependency tree is acyclic and all referenced phases/tasks exist.
  - Phase structure matches `## Phase N: Name` convention so `/gobby expand` parses it.
  - Each `### N.N` task section is self-contained (implementing agent sees only that section — skill says this explicitly).
- **Output format**: write findings to a **round-scoped** section heading `## Adversary Findings — Round N` where N is the **display** round (`planning-round` label value **+ 1**, matching the UI and adversary prompt). First round = "Round 1", second = "Round 2", etc. The interactive planner passes the display round in the adversary prompt so agent and UI stay aligned. **Do not** overwrite previous rounds' sections; preserve them for audit. Each finding: severity (`blocking` or `nit`), category (`missing-requirement` / `bad-sequencing` / `unhandled-edge` / `weak-testability` / `traceability` / `gobby-format`), location (phase/task ref), description, suggested fix. The interactive planner extracts the section matching the CURRENT display round, not a generic `## Adversary Findings` header — prevents round-1 findings from leaking into round-2 view. If **no blocking findings** remain after the second pass, the skill instructs the agent to call `mark_task_review_approved`; otherwise call `escalate_task(reason="planning_changes_requested: <one-line summary>")`.
- **Halt conditions**: plan file missing, plan file empty, plan has no phases, or parent task (plus any docs it references) does not provide enough context to review. On that last condition, escalate with `needs_requirements: <concrete missing questions>` — the same escalation contract the autonomous planner uses. **Do not** require a literal `## Requirements` heading; the parent task description plus referenced docs is the canonical source, per `plan-adversary.yaml:25` and `planner.yaml:25`.

### 3. Agent updates

**`src/gobby/install/shared/workflows/agents/plan-adversary.yaml`:**
- Trim the inline `instructions:` review-heuristics block. Replace with a pointer: "You are the adversarial plan reviewer. Load the `plan-review` skill via `get_skill(name="plan-review")` on `gobby-skills` as your first action after claiming the task. Follow that skill's heuristics exactly."
- Keep critical rules (don't close_task, don't reopen, don't spawn, always use uv, progressive discovery) — those are agent-safety, not review content.
- Add a workflow step before `claim` (or as part of `claim`) that calls `get_skill(name="plan-review")` to guarantee skill content is in context before the `review` step runs.

**`src/gobby/install/shared/workflows/agents/planner.yaml`:**
- Trim the inline `instructions:` block similarly. Today it only gives "what to do" orders (draft, escalate, mark-review) with no methodology. Replace with: "You are the spawned plan drafter. Load the `plan-draft` skill via `get_skill(name="plan-draft")` on `gobby-skills` as your first action after claiming the task. Follow that skill's methodology exactly when writing/revising the plan artifact."
- Keep critical rules (don't close_task, don't spawn, etc.) and the escalation prefix conventions (`needs_requirements:`, `planning_changes_requested:`) — those are the autonomous contract with `_front_half.py` state machine.
- Add a workflow step before the `plan` step that calls `get_skill(name="plan-draft")`.

### 4. Skill changes — `src/gobby/install/shared/skills/plan/SKILL.md`

**Delegate methodology to `plan-draft`.** Remove the existing "Plan Format Reference", "Phase Heading Syntax", "Task Granularity Guidelines", "TDD Compatibility", "Task Categories", and "Task Hierarchy" sections (they move to `plan-draft`). Replace with a short pointer: "Plan structure, TDD rules, and format are defined by the `plan-draft` skill. Load it via `get_skill(name='plan-draft')` before drafting, and follow its verification checklist."

**Renumber existing steps** so the new mode/parent step becomes Step 1 and existing content shifts by one. Final sequence:

| # | Name | Source |
|---|---|---|
| 0 | Enter Plan Mode | existing |
| 1 | **Mode & Parent Task** | **new** |
| 2 | Requirements Gathering | was Step 1 |
| 3 | Draft Plan Structure | was Step 2 — now delegates to `plan-draft` |
| 4 | Write Plan Document | was Step 3 — now delegates to `plan-draft` |
| 5 | Plan Verification | was Step 4 — now runs `plan-draft`'s verification checklist |
| 6 | User Approval | was Step 5 — branches on `plan_review_mode` |
| 7 | **Adversarial Review Loop** | **new** (only when `plan_review_mode == "adversarial"`) |
| 8 | **Approval Handoff / Expansion** | **new** (adversarial path) |
| 9 | **Round-budget Exhausted / Abort** | **new** (adversarial error path; `max_rounds` configurable from Step 1) |

The "Optional: Workflow-Enforced Planning" section stays at the bottom of the file as a separate reference, unchanged.

**Step 1 — Mode & Parent Task** (new, runs after Plan Mode entry):
- Present A/P menu with `A` (adversarial) recommended.
- If `A`, ask for round budget (default 3, accept any positive integer). Persist as `max_rounds`.
- Ask: "Attach to existing task `#N` or create a new planning root?"
  - New: `create_task(task_type="epic", category="planning", title=<from user>)`.
  - Existing: `get_task(#N)` then **active-management guard**:
    1. `active_fh = "conductor:front-half" in labels and "conductor:front-half-complete" not in labels`
    2. **Query each known stage label explicitly** (not a single `list_tasks` call — default `limit=50` from `_queries.py:66,178` can hide live children on large parents). For each of `conductor-stage:requirements`, `conductor-stage:planning`, `conductor-stage:expansion`, `conductor-stage:test-architecture`: call `list_tasks(parent_task_id=#N, label=<stage_label>, limit=200)`, and if any returned task has `status != "closed"`, flag `has_live_stage_child = True`. Limit of 200 per label is defensive — each stage only ever creates one active task per round, so hitting 200 would indicate pathological drift anyway.
    If either `active_fh` or `has_live_stage_child` is true, error out: "Parent #N is under active autonomous front-half management. Either wait for that flow to complete, detach it, or start a new planning root." Skill exits. A parent that previously went through autonomous planning and completed cleanly (both `conductor:front-half` AND `conductor:front-half-complete`, no live stage children) is allowed.
  - **Also check for concurrent interactive sessions** on the same parent: enumerate **all** labels on the parent matching prefix `interactive:planning-in-progress:` (labels can accumulate — `add_label` only dedupes exact strings and `remove_label` only removes exact strings, `storage/tasks/_lifecycle.py:120`, `:131`). For each such label, extract the session suffix and classify:
    - **Ours** (suffix == current session) → resume scenario; remember this, skip live/stale classification.
    - **Live** → session record exists AND passes a liveness predicate. Define "live" conservatively: `status == "active"`, `status == "paused"`, `status == "waiting"`, or `can_proxy_attach == True` or `has_terminal_liveness == True` (`storage/session_models.py:191`, `:179`). Paused/waiting are routinely used between turns (`hooks/event_handlers/_agent.py:370`, `:392`) so they are NOT orphaned. If ANY lock is classified live and not ours, error out immediately: "Parent #N is under an active interactive planning session (`<session_ref>`). Wait, ask for abort, or start a new planning root." Skill exits.
    - **Orphaned** — session record is absent, explicitly terminal (e.g., `ended`, `archived`, `crashed`), OR fails the liveness predicate. Queue the label for removal. If uncertain, treat as live (bias toward false negative over reaping a live session's lock).
  - After the sweep: remove every queued-orphan label with `remove_label(task_id=plan_parent_ref, label=<exact orphan string>)`, surface one notice to the user summarizing recoveries ("Recovered N orphaned planning locks from terminated sessions"), then proceed. If the sweep classified one label as ours, fall through to the resume branch instead of acquiring a new lock.
- **Acquire the session-owned lock immediately after the guard passes**:

  ```python
  lock_label = f"interactive:planning-in-progress:{self_session_id}"
  add_label(task_id=plan_parent_ref, label=lock_label)
  set_variable(name="interactive_lock_label", value=lock_label, session_id="#<self>")
  ```

  Persisting the exact label string is load-bearing — terminal cleanup must pass the same value to `remove_label` (exact-match only). The new rule (Section 5) blocks autonomous `front_half_tick` on this parent from now until the label is removed. This protects the whole interactive path.
- Store `plan_parent_ref` via top-level `set_variable(name="plan_parent_ref", value=..., session_id="#<self>")`.
- **Always set `plan_review_mode` for every menu choice** — `A` → `"adversarial"`, `P` → `"plain"`. Never leave it un-set on a plain-mode run: `set_variable`'d values are persistent (`server.py:442`, `_variables.py:63`) and `ExitPlanMode` only clears `plan_mode`/`plan_skill_loaded` (`handle-plan-mode-exit.yaml:4`). A previous adversarial run's variable would otherwise silently steer a later plain run down the adversarial branch.
- If resumption state is present (`planning_task_id` var exists and the task is still live), offer **resume / abort / restart** instead of the normal menu.

**Steps 2–5** — shift existing Steps 1–4 down by one. Content unchanged.

**Step 6 — User Approval** (renumbered from existing Step 5):
- Present the plan and verification results as today. The user approval boundary runs through Claude Code's native `ExitPlanMode` / `provide_plan_decision` path (web UI has an equivalent plan-approval websocket handler). The skill must not rely on a synthetic "user approved?" prompt — it must wait for the real approval event.
- On approval, branch on `plan_review_mode`:
  - `"plain"` → behave as today: tell user to run `/gobby expand`, run terminal cleanup, exit.
  - `"adversarial"` → proceed to Step 7.

**Step 7 — Adversarial Review Loop** (new, adversarial only):

1. Create the planning task **as an epic** (sidesteps leaf-close's `changes_summary` requirement). The parent lock was already acquired in Step 1 — do not duplicate here.

   ```python
   create_task(
     parent_task_id=plan_parent_ref,
     task_type="epic",
     title=f"Interactive plan for {parent_ref}",
     category="planning",
     labels=["interactive:planning", "planning-round:0"],
   )
   ```

   Store `planning_task_id` in a session var. **Do NOT** apply `conductor:front-half`.

2. Write/move the plan artifact to the canonical path `.gobby/plans/task-<parent_seq>-plan.md`.

3. Read `current_round` from the `planning-round:N` label (default 0; the label is 0-indexed internal state, matching the autonomous front-half convention). **For all user-facing and adversary-facing output, use `current_round + 1`** — so the first round is labeled `planning-round:0` internally but appears as "Round 1" in UI messages, the adversary prompt, and the findings heading. Surface "Round `{current_round + 1}` of `{max_rounds}`" to the user.

4. Spawn adversary:

   ```python
   spawn_agent(
     agent="plan-adversary",
     task_id=planning_task_id,
     parent_session_id=<self>,
     prompt=<task refs + plan file path, mirroring `_adversary_prompt` from `_front_half.py:895`>,
   )
   ```

   The spawn path auto-injects `assigned_task_id` and auto-claims the task for the child session (`spawn_agent/_implementation.py:375`, `:499`). Capture `run_id` and persist `adversary_run_id` in a session var.

5. Surface status ("Adversary reviewing — blocking turn") and call `wait_for_completion(completion_id=adversary_run_id)`. Turn blocks.

6. On return, `get_task(planning_task_id)`:
   - `status == review_approved` → go to Step 8.
   - `status == escalated` with `escalation_reason` starting `planning_changes_requested:` → extract `## Adversary Findings — Round <current_round + 1>` from description (display-round number matches the heading the adversary wrote; prevents leaking prior rounds' findings into the new-round view), present verbatim to user. Bump round label via `update_task`. Call `de_escalate_task(task_id=planning_task_id, reason="Adversary requested changes; starting next revision round", target_status="open")` — all three args spelled out (actual signature: `task_id` and `reason` are required, `target_status` is optional per `_lifecycle_status.py:461-476`). If new round ≥ `max_rounds`, go to Step 9. Otherwise: **re-enter plan mode** (call `EnterPlanMode`), revise the plan file with the user, route the revised plan through the real `ExitPlanMode` approval boundary again (this is the "each round has user approval" contract Codex flagged), then loop to step 7.4 with the new plan.
   - `status == escalated` with reason starting `needs_requirements:` → surface questions, `de_escalate_task(task_id=planning_task_id, reason="User providing clarifications", target_status="open")`, re-enter plan mode, re-gather with user, revise plan, route through ExitPlanMode, loop to step 7.4.
   - Any other terminal state → treat as adversary crash: surface state + raw wait result, go to Step 9.

**Why re-enter plan mode each round:** the user's native approval boundary — the whole point of pair-programming around a spec — lives on the `ExitPlanMode` / `provide_plan_decision` path (`chat_session_permissions.py:117`). That path only runs while plan mode is active (`plan_mode=true`). Pure plan-file edits outside plan mode would technically work (edits to `.gobby/plans/*.md` are allowed by `blocking.py:230`), but they would skip the approval gate — rounds 2–3 would silently degrade to "file edit, re-spawn adversary" with no user checkpoint. Re-entering plan mode restores the approval boundary. `ExitPlanMode` itself only clears `plan_mode` and `plan_skill_loaded` (`handle-plan-mode-exit.yaml:4`); our `plan_review_mode` variable survives across the boundary — we clear it ourselves only at terminal exit. Chat-session plan-state reset also clears UI plan-artifact vars like `_plan_file_path` when mode changes (`chat_session_permissions.py:362`) — we re-write the artifact path on each re-entry, so that's harmless.

**Step 8 — Approval Handoff / Expansion** (new):

The skill stays in control through both success and failure retries — no "exit and run a raw tool call" paths. `expand-task` pipeline only owns the run/validation (`expand-task.yaml:66,:74`); everything around it (retry, planning-epic close, state cleanup) is the skill's job.

1. `run_pipeline(name="expand-task", inputs={"task_id": plan_parent_ref, "plan_file": artifact_path})`. Capture `execution_id`.
2. `wait_for_completion(completion_id=execution_id)`.
3. Branch on pipeline outcome (both run-failure and validation-failure short-circuit the pipeline):
   - **Success** → report child-task count via `get_pipeline_status`; `close_task(planning_task_id, reason="Interactive planning complete; expansion launched")` (epic, no `changes_summary` required); **run terminal cleanup** (clear all session vars, remove `interactive:planning-in-progress` label from parent); skill exits.
   - **Failure** → surface the pipeline error to the user. **Do not close the planning task.** Present three choices:
     - **Retry** (default): skill loops back to step 8.1 with the same inputs. Counter failures; after 3 consecutive failures, fall through to Escalate.
     - **Retry with overrides**: ask for `provider`/`model` overrides on the expansion run, then loop back to step 8.1 with those inputs added.
     - **Escalate**: `escalate_task(planning_task_id, reason=f"expansion_failed: {error}")`, run terminal cleanup, skill exits. User can pick up the escalated planning epic later.
4. Do NOT advance into test-architecture — that stage belongs to the autonomous front-half.

**Step 9 — Round-budget exhausted / Abort** (new; triggered when round ≥ `max_rounds` or adversary crashed):
- Present the final `## Adversary Findings` and escalation reasons to the user.
- **Every exit path from Step 9 must properly dispose of the planning epic** — leaving it open strands the parent (can't close later with live children).
- All three exit paths run terminal cleanup (clear session vars + remove the session-owned parent lock). They are each a true termination of the current interactive-planning run.
- Offer three choices:
  - **Revise manually + run `/gobby expand` directly** — before exit, `close_task(planning_task_id, reason="User bypassed adversarial gate; running /gobby expand manually")`, run terminal cleanup, skill exits; plan file stays in place.
  - **Abort** — `close_task(planning_task_id, reason="User aborted adversarial planning")`, run terminal cleanup, skill exits.
  - **Restart** — `close_task(planning_task_id, reason="Restart: planning round budget exhausted, beginning a new attempt")`, run terminal cleanup (closes the planning epic, clears all session vars, releases the parent lock), then re-enter the skill at Step 1. The user gets a fresh mode menu, can re-confirm `max_rounds`, the guard re-runs against current parent state, and the lock is re-acquired cleanly. This is deliberately a full re-seed — restart stays inside-the-lock only if we had owner semantics deep enough to distinguish "same attempt" from "next attempt", which would need more primitives than we're willing to add for this feature. Fresh restart is simpler and coherent with the lock lifecycle.

### 5a. Session-end orphan-lock sweep — `src/gobby/hooks/event_handlers/_session_end.py`

Add a small block to the existing `handle_session_end` method after the `session_id` resolution block. Given the ending session's id, find all tasks carrying `interactive:planning-in-progress:<session_id>` and remove that label. This proactively clears the lock when sessions end normally — even if the planner session dies before reaching its own terminal cleanup (e.g., browser tab closed, tmux pane killed).

Implementation sketch (not final code):

```python
if session_id and self._task_manager:
    lock_label = f"interactive:planning-in-progress:{session_id}"
    stale = self._task_manager.list_tasks(label=lock_label, limit=50)
    for t in stale:
        self._task_manager.remove_label(t.id, lock_label)
        self.logger.info(f"SESSION_END: released interactive-plan lock on task {t.id}")
```

This is belt-to-the-Step-1-adjudication's suspenders. Most clean exits go through the skill's own terminal cleanup. Session-end sweeps the tail cases where the skill never got a chance to run.

### 5b. New rule — `src/gobby/install/shared/workflows/rules/task-enforcement/block-front-half-on-interactive-lock.yaml`

Small new rule that enforces two-sided mutual exclusion without editing `_front_half.py`:

```yaml
tags: [task-enforcement, enforcement, tasks, gobby, default]

rules:
  block-front-half-on-interactive-lock:
    description: "Block front_half_tick on parents carrying any interactive-planning lock"
    event: before_tool
    enabled: true
    priority: 35
    when: >
      (event.data.get('tool_name') == 'mcp__gobby__call_tool'
       or event.data.get('tool_name') == 'call_tool')
      and tool_input.get('server_name') == 'gobby-tasks-ops'
      and tool_input.get('tool_name') == 'front_half_tick'
      and task_has_label_prefix(tool_input.get('arguments', {}).get('task_id'),
                                'interactive:planning-in-progress:')
    effects:
      - type: block
        reason: |
          Parent task is under active interactive plan-adversary loop.
          Wait for that flow to complete (it removes the lock on terminal exit),
          or ask the user to abort the interactive session.
```

The helper `task_has_label_prefix(task_id, prefix)` does not exist yet — the current safe-evaluator only wires helpers like `task_tree_complete` and `task_needs_human_review` (`workflows/safe_evaluator.py:336`). We add `task_has_label_prefix` alongside those: resolve the task ref, read the label set, return `True` iff any label `startswith(prefix)`. The prefix variant (rather than exact match) is necessary because each session owns its own `interactive:planning-in-progress:<session_id>` label — the rule just needs to detect that SOMEONE holds a lock, not who specifically. Small addition, same registration pattern as the existing helpers. No changes to the front-half state machine itself.

### 6. Resume handling & state hygiene

**State variables** — persist via top-level proxy `set_variable(name, value, session_id="#<self>")` and read back with `get_variable(...)`:
- `plan_review_mode` — always set on Step 1 choice (never left stale).
- `plan_parent_ref` — parent task reference.
- `planning_task_id` — current round's planning epic.
- `artifact_path` — canonical plan file path.
- `adversary_run_id` — run id of the in-flight adversary (cleared after wait returns).
- `current_round` — cached round-count for UI; label on the task is source of truth.
- `max_rounds` — default 3, configurable per run (see below).

**Lock label persistence.** Step 1 acquires the lock label `interactive:planning-in-progress:<self_session_id>` and **must persist the exact string value** in a session var (e.g., `interactive_lock_label`). `remove_label` is exact-match only (`storage/tasks/_lifecycle.py:131`) — cleanup must pass the same string that was added, not the unsuffixed prefix.

**Terminal exit cleanup (MUST).** Every path that exits the skill — normal completion (Step 8 success), expansion escalation (Step 8 failure "Escalate" choice), bypass/abort/restart (Step 9 all three), adversary crash — **must** do two things before returning, via a single "reset state" helper referenced at every exit point:
1. `remove_label(task_id=plan_parent_ref, label=<exact value from interactive_lock_label var>)` — releases the session-owned lock. Using the exact stored string guarantees we remove the same label we added, regardless of how `self_session_id` was rendered.
2. Clear all session variables listed above (including `interactive_lock_label` last).

**Resume.** On skill entry, if `planning_task_id` is set and the task is still live (status ≠ `closed`), Step 1 offers **resume / abort / restart** instead of the normal mode menu. Abort and restart both flow through the terminal cleanup.

**Configurable round cap.** `max_rounds` defaults to 3 but the user can override at Step 1 (e.g., "A, 5 rounds" or a separate prompt). Useful observation from our own dog-fooding of this design: three adversary passes wasn't quite enough — we iterated four times before the Codex reviewer signed off on a plan. Three rounds remains a reasonable default for simple features, but users should be able to bump it for complex work without forking the skill.

Covered edge cases:
- **User cancels mid-wait**: `wait_for_completion` is interruptible. Adversary still owns the claim; next invocation detects the in-flight state via session vars + `get_task` and offers resume/abort/restart.
- **Adversary crashes / never completes**: `wait_for_completion` returns an error/timeout status (or wakes when the lifecycle monitor notifies on agent death). Route to Step 9. If the adversary died mid-claim and never released, the skill can force-release via `reopen_task` or `de_escalate_task` as part of the restart path.
- **Session compaction mid-wait**: `context-handoff` rules re-inject task context on compaction. Session vars persist across compaction; skill re-enters at the right branch based on task status.
- **Web UI vs CLI**: the whole flow is daemon-driven via MCP. Web UI just sees a long-running tool call during `wait_for_completion` — same as any async MCP tool. No UI-specific work needed.
- **Stale plan file from pre-adversarial drafting**: Step 7.2 writes/moves content into the canonical `.gobby/plans/task-<parent_seq>-plan.md` path. Adversary and `expand-task` both read from that canonical location.
- **Early completion race (adversary finishes before skill calls wait)**: handled by the completion registry — `notify()` stores results against the registered `run_id`, and a later `wait()` returns immediately if the event has already fired.

### 7. Verification

- **Integration — `tests/integration/test_interactive_plan_adversary_loop.py`** (new): drive the skill against a stub adversary that returns canned `planning_changes_requested:` findings on round 1 and approves on round 2. Assert: turn held open via `wait_for_completion`, findings extracted from description, round label bumps, plan mode is re-entered before round-2 revision, `expand-task` pipeline starts after approval, expansion completes, planning epic is closed cleanly, skill exits.
- **Isolation test** (new, replaces earlier mis-framed assertion): **the real isolation guarantee is the Step 1 attach guard**, not any label on the interactive planning epic itself (front_half_tick labels the PARENT, not the passed task — `_front_half.py:75`, `:581`). Assert: (a) guard rejects attach on actively-managed parent with `conductor:front-half` and no `-complete`; (b) guard rejects attach on parent with a live `conductor-stage:*` child; (c) guard **accepts** attach on parent that has BOTH `conductor:front-half` AND `conductor:front-half-complete` with no live stage children (previously-completed front-half work can have a second interactive planning pass).
- **ExitPlanMode path test** (new): exercise the real `ExitPlanMode` / `provide_plan_decision` boundary end-to-end for EACH round — not just round 1. Assert round 2 and round 3 also route through the actual plan-decision handler after plan-mode re-entry.
- **Round-budget-exhausted path test**: all three rounds escalate. Assert skill lands on Step 9, presents findings, and each of the three exit choices (bypass, abort, restart) properly closes the old planning epic before exit or re-creation.
- **Expansion-failure branch test**: stub `expand-task` to fail. Assert Step 8 does NOT close the planning task and surfaces the failure; both recovery branches (retry / escalate) work.
- **Autonomous skill-load tests** (new): assert `planner.yaml` loads `plan-draft` and `plan-adversary.yaml` loads `plan-review` before their respective work steps.
- **Category drift test** (precursor): `create_task(category="refactor")` succeeds after the precursor fix; fails today.
- **Web-UI smoke**: drive `/gobby plan` from the web chat; verify `wait_for_completion` holds the websocket round-trip and findings come back into the chat stream.
- **Manual CLI smoke**: same flow in Claude Code terminal.

### 8. Critical files

**Add:**
- `src/gobby/install/shared/skills/plan-draft/SKILL.md` — new shared drafting methodology skill.
- `src/gobby/install/shared/skills/plan-review/SKILL.md` — new shared review methodology skill.
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-front-half-on-interactive-lock.yaml` — new mutual-exclusion rule.
- `tests/integration/test_interactive_plan_adversary_loop.py` — end-to-end loop test.
- `tests/integration/test_interactive_plan_exit_plan_mode.py` — real `ExitPlanMode` approval boundary (filename references the native tool, not our new variable).
- `tests/integration/test_interactive_plan_guard.py` — Step 1 hard-block on actively-managed parent; accept previously-completed parent.
- `tests/integration/test_interactive_plan_mutex.py` — mutual exclusion tests: (a) autonomous `front_half_tick` blocked on a parent with any `interactive:planning-in-progress:*` label; (b) **lock is held for the full session**: acquire at Step 1, try to autonomously tick while skill is still at Step 3 (drafting) — must be blocked; (c) **second interactive session on same parent is blocked at Step 1 guard** when the first session's lock exists and is still live; (d) first session's terminal cleanup removes **only its own** session-suffixed label — never another session's; (e) cleanup uses the exact label string from `interactive_lock_label` (not the unsuffixed prefix); (f) after first session's cleanup, autonomous front-half can run on the parent.
- `tests/integration/test_interactive_plan_orphan_lock.py` — orphan-lock recovery: (a) session_end handler removes `interactive:planning-in-progress:<ended_session>` across all tasks; (b) Step 1 stale-lock adjudication handles multiple accumulated `interactive:planning-in-progress:*` labels on a single parent — iterates all of them, reaps orphans, blocks on any live one; (c) "live" classification is conservative — sessions with `status=paused` / `status=waiting` / `has_terminal_liveness=True` / `can_proxy_attach=True` are NOT reaped; only absent, terminal-status, or non-attachable sessions are reaped; (d) Step 1 does NOT remove locks from sessions that are still live; (e) round numbering: label is `planning-round:0` on first round but all user-visible surfaces (UI, heading, adversary prompt) show "Round 1".
- `tests/integration/test_interactive_plan_expansion_retry.py` — Step 8 failure → retry → success keeps the skill in control; three-failures-then-escalate path works.
- `tests/integration/test_interactive_plan_findings_round_scoping.py` — round-1 findings do not leak into round-2 view; historical `## Adversary Findings — Round N` sections remain in description for audit.
- `tests/integration/test_interactive_plan_state_cleanup.py` — every terminal exit clears all session vars AND removes the parent lock label.
- `tests/skills/test_plan_draft_skill.py` — skill-content tests (structure, categories, TDD rules, verification checklist).
- `tests/skills/test_plan_review_skill.py` — skill-content tests (no quota, re-check-then-approve, round-scoped headers, escalation policy).
- `tests/agents/test_planner_loads_plan_draft.py` — assert autonomous planner gets `plan-draft` in context.
- `tests/agents/test_plan_adversary_loads_plan_review.py` — assert autonomous adversary gets `plan-review` in context.

**Edit:**
- `src/gobby/storage/tasks/_models.py` — add `refactor` to `VALID_CATEGORIES` (precursor).
- `src/gobby/mcp_proxy/tools/tasks/_crud.py` — add `refactor` to `create_task` schema enum (precursor).
- `src/gobby/workflows/safe_evaluator.py` — add `task_has_label_prefix(task_id, prefix) -> bool` helper alongside the existing `task_tree_complete`/`task_needs_human_review` wiring (around line 336). Returns `True` iff any label on the task starts with `prefix`. Required by the new mutex rule to detect session-owned locks without caring about ownership.
- `src/gobby/hooks/event_handlers/_session_end.py` — add orphan-lock sweep block (Section 5a) that removes `interactive:planning-in-progress:<session_id>` labels across all tasks when a session ends.
- `src/gobby/install/shared/skills/plan/SKILL.md` — remove embedded methodology sections (moved to `plan-draft`), add Step 1 (mode + parent + guard + lock acquisition), Step 6 branching (real ExitPlanMode path, A/P only), Steps 7–9 (adversarial loop, approval/expansion with failure-retry-loop, failure/abort/restart with proper epic close + lock release), resume handling with terminal cleanup helper.
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml` — trim inline review instructions, add `plan-review` skill-load step.
- `src/gobby/install/shared/workflows/agents/planner.yaml` — trim inline instructions, add `plan-draft` skill-load step.

**Reference only (no edits):**
- `src/gobby/install/shared/workflows/pipelines/expand-task.yaml` — reused unchanged.
- `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml` — reused unchanged; the autonomous dispatch path continues to work with the trimmed planner/adversary YAMLs because the critical rules and escalation contract are preserved.
- `src/gobby/mcp_proxy/tools/tasks/_front_half.py` — reference for adversary prompt shape and round-label semantics; no edits.
- `src/gobby/events/completion_registry.py` — confirmed push-based wait semantics; no edits.
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-needs-review-interactive.yaml` — stays unchanged; interactive session never calls blocked transitions.
- `.claude/bmad-skills/bmad-review-adversarial-general/SKILL.md`, `.claude/bmad-skills/bmad-review-edge-case-hunter/SKILL.md`, `.claude/bmad-skills/bmad-check-implementation-readiness/workflow.md` — source material for `plan-review`.

## Out of scope (follow-up tasks)

1. **Dev-agent mid-task planning decision**: when a spawned dev agent picks up a task that turns out to need planning, it should trigger the autonomous `front-half-orchestrator` pipeline before implementing. Separate change to dev-agent definitions + a new rule.
2. **Unifying the autonomous and interactive flows**: the autonomous path still uses `mark_task_needs_review` as the handoff signal; interactive uses direct spawn. Could unify later, but blast radius is large — defer until this interactive loop is proven.
3. **Project-scale BMAD planning**: a separate `/gobby project` skill (or equivalent) that produces the full BMAD suite — PRD, architecture, epics, stories, implementation-readiness check. Different scale and audience than feature/task planning; should not retrofit `/gobby plan`.
4. **Real `activate_workflow` MCP tool**: the existing plan skill documents `call_tool("gobby-workflows", "activate_workflow", ...)` but that tool is not actually registered. Adding it would unblock a real "C — Custom workflow" menu branch. Also worth auditing the existing docs that reference the non-existent tool.
