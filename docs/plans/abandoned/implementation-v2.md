# Back-Half Pipeline Overhaul — Launch-Ready E2E Path

## Context

The front-half agents (`planner`, `plan-adversary`, `expansion-qa`) were just repaired. The back-half agents (`developer`, `qa-dev`, `qa-reviewer` — all v2.1) haven't been exercised in weeks and have drifted from the canonical agent schema: they use a 2-step shape (`claim → work → terminate`) where the canonical shape is now 3-step (`claim → load_skill → work → terminate`), and they have no skill-loading enforcement. Launch needs a working end-to-end path.

This plan does four things as a coordinated set:

1. Align the back-half agents with the canonical schema (fixes the drift).
2. Add **configurable skill routing** — pipelines accept a `skills:` input list, agents load whatever skills the pipeline invoker configured (language-specific: `python`, `typescript`, `rust`), plus new `frontend-dev` / `backend-dev` **router skills** that direct an agent to the right language skills based on the claimed task's tags.
3. Introduce a **review loop** distinct from QA — `qa-dev` / `qa-reviewer` assess *code quality*; the new `spec-reviewer` assesses *intent match* against the plan artifact before merge.
4. Rename the pipelines to self-describing names (`planning-orchestrator`, `implementation-orchestrator`, new `review-orchestrator`), with `delivery-orchestrator` retained as the E2E wrapper. Add an interactive `integrator` agent for the non-happy-path merge cases (multiple worktrees, cherry-picking, conflict untangling) — `merge.yaml` stays as happy-path-only.

## Final shape

```
delivery-orchestrator (E2E wrapper)
  ├── planning-orchestrator     (was: front-half-orchestrator)
  │     └── planner, plan-adversary, expansion-qa, test-architect
  ├── implementation-orchestrator (was: orchestrator)
  │     └── developer → qa-dev → qa-reviewer
  ├── review-orchestrator        (NEW)
  │     └── spec-reviewer
  └── merge step
        └── merge (happy path, existing) — on non-happy-path, pipeline halts and human loads integrator persona

integrator persona (NEW, out-of-band) — loaded by a human into an active session when merge needs judgment
```

Task routing: expander tags leaf tasks with `tag:frontend` / `tag:backend` / `tag:fullstack`. Pipelines read the tag; agents consume it via session variable and router skill.

## Phases (each independently shippable)

### Phase 1 — Schema canonicalization (no behavior change)

Bring `developer.yaml`, `qa-dev.yaml`, `qa-reviewer.yaml` up to the canonical 3-step shape from `planner.yaml` / `plan-adversary.yaml`.

Files:
- `src/gobby/install/shared/workflows/agents/developer.yaml`
- `src/gobby/install/shared/workflows/agents/qa-dev.yaml`
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`

For each: insert a `load_skill` step between `claim` and the work step, add `skill_loaded: false` to `step_variables`, add a `get_skill` gated transition. Reference skill (temporary until Phase 2): developer → `implementation-playbook`, qa-dev → `qa-playbook`, qa-reviewer → `qa-review-playbook`. Bump version to `3.0`. Commit per file.

Verification:
- `uv run gobby install` syncs updated YAMLs to `workflow_definitions`.
- Spawn each agent manually via orchestrator; confirm it calls `get_skill` before work and the transition gate holds.
- `uv run pytest tests/workflows/test_agent_loader.py -v`.

### Phase 2 — Configurable skill routing

Infrastructure work (small — most of the plumbing exists):

- Extend `spawn_agent` tool to accept `skills: list[str]` in args; merge into `initial_variables` as `requested_skills`. File: `src/gobby/mcp_proxy/tools/agents/spawn_agent.py` (and its `_implementation.py`).
- Agent `load_skill` step iterates `vars.requested_skills` calling `get_skill` per entry; `skill_loaded` flips true once all are loaded. Update `developer.yaml` / `qa-dev.yaml` / `qa-reviewer.yaml` step to use the list instead of a single hardcoded name.
- `implementation-orchestrator` (renamed in Phase 3) accepts a top-level `skills:` pipeline input and forwards it to every `spawn_agent` call. Default: empty list (falls back to router skill via task tag).

New skill files under `src/gobby/install/shared/skills/` (or wherever bundled skills live — verify at execution time):
- `frontend-dev.md` — router skill: reads `tag:frontend` task tag, instructs agent to `get_skill` the `typescript` / `react` / `web-accessibility` skills as relevant. Style: mirrors `/gobby` skill router.
- `backend-dev.md` — router skill: reads `tag:backend`, instructs agent to `get_skill` `python` / `sqlite` / `fastapi` / whatever the task touches.

Tagging — one generic plumbing change, then pure config forever:

Current state (confirmed): `Task.status` and `labels` column accept free-form strings; `add_label` MCP tool has no whitelist; rule engine supports `mcp_call` effects on `task_created`. The expansion service (`src/gobby/tasks/expansion_service.py:467-474, 546`) already forwards labels to `create_task` — but it only synthesizes `parallel:<group>` labels from the `execution_group` field; the LLM output schema has no per-subtask `labels` field.

One-time generic change (not a per-tag code change — makes the mechanism extensible):
- Add an optional `labels: list[str]` field to the expansion LLM output schema. File: `src/gobby/tasks/expansion.py` (Pydantic model) + `src/gobby/tasks/expansion_service.py` (merge LLM-provided labels with the existing `parallel:*` labels before `create_task`).
- Update `src/gobby/tasks/prompts/expand-task.md` with a single instruction paragraph: "Populate `labels: [str]` when an area is obvious (e.g. `frontend`, `backend`, `fullstack`). Free-form — match what the plan describes."

After that, introducing a new tag vocabulary word = edit the prompt (bundled template, same surface as rules/skills). Never a Python change.

Fallback rule for cases where the LLM skips tagging — new YAML at `src/gobby/install/shared/workflows/rules/auto-tagging/tag-on-path-hint.yaml`. Fires on `task_created` when `labels` is empty; pattern-matches path hints in the task description (`web/` → frontend, `src/gobby/` → backend) and calls `add_label` via `mcp_call` effect. Pure rule config.

Verification:
- Fire `implementation-orchestrator` with `skills: [python]` — agent loads python skill.
- Fire with empty `skills` on a `tag:frontend` task — agent loads `frontend-dev` router, which loads typescript etc.
- Unit test: `tests/tasks/test_expansion.py` gets a case asserting tags land on leaves.

### Phase 3 — Pipeline rename

Rename files and DB rows:

- `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml` → `planning-orchestrator.yaml`
- `src/gobby/install/shared/workflows/pipelines/orchestrator.yaml` → `implementation-orchestrator.yaml`
- Update `name:` field inside each, bump `version`.
- Update `delivery-orchestrator.yaml` `invoke_pipeline:` refs to the new names.
- Update `dev-orchestrator.yaml` if it references the old names.
- Grep for `orchestrator` / `front-half-orchestrator` across `src/gobby/` and `tests/` — expected hits: loader comments, test fixtures, any MCP tool or pipeline YAML that references pipeline by name.
- DB rename: one-off SQL against `~/.gobby/gobby-hub.db` — no migration file (pre-launch, no users). Two UPDATE statements against `workflow_definitions.name`, run manually after the YAML rename commit. Pipeline sync (`src/gobby/workflows/sync_pipelines.py:91`) keys on the YAML `name` field — renamed rows match renamed YAMLs and sync-update normally.

Verification:
- Stop daemon; run the two `UPDATE workflow_definitions SET name=...` statements; start daemon; `uv run gobby install` re-syncs.
- `gobby pipelines list` shows new names only.
- `delivery-orchestrator` smoke fires both renamed sub-pipelines.

### Phase 4 — Review loop

New agent:
- `src/gobby/install/shared/workflows/agents/spec-reviewer.yaml` — claims a `review_approved` task, loads `spec-review` skill, compares the task's closed PR / commit(s) against the plan artifact + original parent task description. Approves (→ merge-ready) or rejects (→ reopens with `## Spec Review Findings — Round N`). Use `plan-adversary.yaml` as structural template; `end_agent_run` termination (same reasoning — reviewer, not worker).

New pipeline:
- `src/gobby/install/shared/workflows/pipelines/review-orchestrator.yaml` — scans for tasks in `review_approved` state (confirmed existing — `src/gobby/storage/tasks/_models.py:120-127`); dispatches `spec-reviewer`. On approval: sets `spec_reviewed: true` task label (pure config signal) so the merge step knows this task cleared spec review. On rejection: demotes task back to `in_progress` via existing transition tools and appends `## Spec Review Findings — Round N` to the task description. Reuses reentry-guard / scan / dispatch pattern from `implementation-orchestrator`.

New skill:
- `spec-review.md` — heuristics for checking spec-vs-work: does the diff implement what the plan described, are there scope creeps, missing acceptance criteria. Explicitly NOT code-quality review (that's qa-reviewer's job).

Task state: no schema change. `review_approved` already exists and already means "QA has signed off." Spec-reviewer acts as a second gate ON `review_approved` tasks before the merge step fires — controlled via pipeline logic + the `spec_reviewed` label, not via a new state.

Delivery orchestrator update:
- `delivery-orchestrator.yaml` gets a `run_review` step between `run_implementation` (renamed from `run_delivery` in Phase 3) and merge, conditional on implementation_complete.
- Merge step's precondition on the task changes from `is_merge_ready_sql()` (lifecycle_stage='review_approved') to the same predicate AND the `spec_reviewed` label present. Enforced in pipeline `when:` expressions, not in storage.

Verification:
- E2E smoke: fire `delivery-orchestrator` on a task with a plan artifact. Observe planning → implementation → review → merge chain. Manually reject at review; confirm task reopens to developer.
- `uv run pytest tests/workflows/test_pipeline_executor.py -v -k review`.

### Phase 5 — Integrator persona (human-in-the-loop)

Integrator is **not** a spawned automation agent — it's a persona a human loads into an active session when an integration situation gets non-trivial. Scope is the full integration surface: worktree/clone merges today, PR reviews and PR merges in follow-up work. The automation pipeline's job is to recognize "this is out of happy-path territory" and stop cleanly, not to auto-resolve.

New persona agent:
- `src/gobby/install/shared/workflows/agents/integrator.yaml` — `surfaces: [persona]` (NOT `spawn`). No step workflow, no tool restrictions — the human drives. Fields used: `role`, `goal`, `personality`, `instructions`, `workflows.skill_selectors` (auto-load the integrator skill set). Full git + `gh` access implied by unrestricted tool model on persona surfaces.
- Design the persona's `role` / `instructions` broadly enough to cover both **local integration** (worktrees, clones, cherry-picking, merge-order dependencies, conflict resolution) and **remote integration** (PR review, PR merges, branch cleanup, release cuts). Phase 5 ships with the local-integration skill; PR-integration skill is a deliberate follow-up slot.

Skills (under `src/gobby/install/shared/skills/`):
- `integrator-merge.md` — SHIP IN PHASE 5. Local playbook: identifying merge order from worktree dependency graph, when to cherry-pick vs merge, conflict-resolution heuristics, rollback/abort triggers. Encodes what the agent figured out this morning on 8 worktrees.
- `integrator-pr.md` — FOLLOW-UP (not in Phase 5 scope, but design the persona to accommodate it). PR review, squash vs merge-commit decisions, PR-stack management, coordinating dependent PRs.
- `integrator.md` — thin router skill (like `frontend-dev`) that conditionally directs to `integrator-merge` or `integrator-pr` based on what the human is handling. Ship this alongside `integrator-merge` so the PR follow-up is a pure skill-add, no persona rework.

`delivery-orchestrator` change:
- Merge step still invokes `merge.yaml` happy-path.
- On failure OR when `pending_worktree_count > 1`, the pipeline halts with an escalation output field `merge_requires_integrator: true` rather than attempting auto-resolution. Human loads `integrator` persona into their session to untangle.
- Do not build auto-dispatch to integrator — the whole point is that these situations need judgment.

Verification:
- Load integrator persona in a test session. Confirm `integrator` router skill auto-loads via `skill_selectors` and the human-facing preamble surfaces both merge and PR guidance slots.
- Trigger delivery-orchestrator on a contrived multi-worktree scenario; confirm pipeline halts with `merge_requires_integrator` signal rather than entering an auto-merge loop.

## Critical files to modify (quick reference)

| File | Phase | Purpose |
| --- | --- | --- |
| `src/gobby/install/shared/workflows/agents/developer.yaml` | 1, 2 | Add load_skill step; consume requested_skills list |
| `src/gobby/install/shared/workflows/agents/qa-dev.yaml` | 1, 2 | Same pattern |
| `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml` | 1, 2 | Same pattern |
| `src/gobby/install/shared/workflows/agents/spec-reviewer.yaml` | 4 | NEW — spec/intent reviewer |
| `src/gobby/install/shared/workflows/agents/integrator.yaml` | 5 | NEW — persona (surfaces: [persona]) for human-driven merge untangling |
| `src/gobby/install/shared/workflows/pipelines/planning-orchestrator.yaml` | 3 | Renamed from front-half-orchestrator |
| `src/gobby/install/shared/workflows/pipelines/implementation-orchestrator.yaml` | 3 | Renamed from orchestrator |
| `src/gobby/install/shared/workflows/pipelines/review-orchestrator.yaml` | 4 | NEW |
| `src/gobby/install/shared/workflows/pipelines/delivery-orchestrator.yaml` | 3, 4 | Update refs; add review step |
| `src/gobby/mcp_proxy/tools/agents/spawn_agent*.py` | 2 | Accept `skills:` arg; merge to initial_variables |
| `src/gobby/tasks/prompts/expand-task.md` | 2 | One-line instruction for free-form labels (config surface, not Python) |
| `src/gobby/install/shared/workflows/rules/auto-tagging/*.yaml` | 2 | NEW — fallback tagging rules (pure config) |
| `src/gobby/install/shared/skills/frontend-dev.md` | 2 | NEW — router skill |
| `src/gobby/install/shared/skills/backend-dev.md` | 2 | NEW — router skill |
| `src/gobby/install/shared/skills/spec-review.md` | 4 | NEW |
| `src/gobby/install/shared/skills/integrator.md` | 5 | NEW |
| `src/gobby/tasks/expansion.py` + `expansion_service.py` | 2 | Add `labels: list[str]` to LLM output schema; merge LLM labels with `parallel:*` labels |

## Execution order

Phases are ordered so each is independently committable and shippable:

1. Phase 1 first — low risk, unblocks real use of back-half today.
2. Phase 3 (rename) next — purely structural, best done before new pipelines land under old names.
3. Phase 2 (skill routing) — depends on Phase 1's load_skill step.
4. Phase 4 (review loop) — depends on Phase 3's naming and Phase 2's skill routing.
5. Phase 5 (integrator) — independent of 2-4; could also ship alongside Phase 1 if multi-worktree pain is urgent.

A single PR is not recommended — five phases, five PRs, five gobby-task lineages. Each phase ends with a commit and real-world smoke test.

## Verification — E2E smoke after all phases

1. Create a task with a plan artifact and a clear spec.
2. Fire `delivery-orchestrator` with `skills: [python, typescript]` input.
3. Observe: `planning-orchestrator` produces reviewed plan → `implementation-orchestrator` dispatches developer (loads both skills) → qa-dev → qa-reviewer → `review-orchestrator` dispatches spec-reviewer → merge/integrator closes the loop.
4. Inject a spec-mismatch in step 3's output; confirm spec-reviewer rejects and task reopens to developer.
5. Inject a merge conflict; confirm integrator takes over from happy-path merge.
6. `uv run pytest tests/workflows/ tests/tasks/ tests/storage/ -v` passes.
