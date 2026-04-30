# Gobby Build Recovery Timeline

This is the post-#12725 recovery roadmap. The strategy plan lives at
`/Users/josh/.claude/plans/the-audit-is-elegant-manatee.md`. This document is
the operator-facing checklist for what ships, in what order, and what to do
between phases.

---

## Phase 0 — Strategy approval (now)

- Adversarial review of the strategy plan via `/gobby plan`.
- Integrate review feedback.
- Approve the strategy plan.
- Author Epic 1 plan at
  `.gobby/plans/task-NEW-plan-coverage-contract.md` using the new format the
  epic itself defines (section IDs, `**Acceptance:**` lines,
  `kind: deliverable | framing | deferred` front-matter). Hand-validated; the
  tooling that will mechanically validate this format does not exist yet.

**Exit criterion:** Epic 1 plan exists, plan-adversary approves it, the
strategy plan is checked in.

---

## Phase 1 — Epic 1: Plan-coverage contract

Ships the structural fix to expansion / expansion-qa / holistic-review so
plan-section coverage cannot vanish silently.

### Deliverables

- **A1** — Plan format with stable section IDs, `**Acceptance:**` lines, and
  `kind` front-matter. Updates to plan skill and planner agent. Plan-adversary
  rejects malformed plans.
- **A2** — Every leaf carries `plan-ref:<section-id>` labels. Provenance flows
  downstream.
- **A3** — Expansion-QA computes the input-coverage matrix mechanically by
  parsing the plan and the leaf labels. Rejects on uncovered `deliverable`
  sections without explicit deferrals.
- **A4** — Holistic-review consumes the same coverage matrix. `approve`
  verdict is blocked if any deliverable section has no PR diff.
- **A5** — Retro-conform `task-12725-lifecycle-dispatch.md` to the new format
  and generate `.gobby/plans/task-12725-compliance-matrix.md` from the merged
  commits. This matrix is the authoritative input to Epic 2.
- **A6** — Repo-wide CI test
  (`tests/build_pipeline/test_plan_coverage_matrix.py`) fails when active
  plans have unaccounted deliverable sections. `.gobby/plans/.grandfathered`
  exempts pre-existing merged epics.
- **A7** — Document the contract in `CLAUDE.md` and skill READMEs.

### Tests added

- `tests/build_pipeline/test_plan_coverage_matrix.py` — repo-wide gate.
- Expansion-QA tests that confirm rejection on uncovered deliverable sections.
- Holistic-review tests that confirm `approve` is blocked when coverage
  matrix has gaps.

### Bootstrap note

Epic 1's own plan is hand-validated against A1's format. Once A6 ships at the
end of Epic 1, Epic 1's plan becomes the first plan mechanically validated.

**Exit criterion:** All A1–A7 deliverables merged. Epic 1 plan validates
clean against its own tooling. `task-12725-compliance-matrix.md` exists and
shows the known gap inventory as `missing` / `partial`.

---

## Phase 2 — #12725 plan retrofit (A5 follow-through)

Bridges Epic 1 and Epic 2. Output is the input to Epic 2's expansion.

- Confirm `task-12725-lifecycle-dispatch.md` has section IDs, acceptance
  lines, and `kind` front-matter throughout.
- Run the new coverage tooling against the file and the merged commits.
- Generated matrix lives at
  `.gobby/plans/task-12725-compliance-matrix.md` and is the authoritative
  acceptance checklist for Epic 2.

**Exit criterion:** Compliance matrix is generated, reviewed, and Epic 2 can
use it as input.

---

## Phase 3 — Epic 2: #12725 gap recovery

Closes the implementation gap, expanded under the new contract from Epic 1
so its own coverage is provable.

### Phase B — Build entry-point contract (plan §3.1, §3.2, §3.4)

- Wire `load_build_config` and `resolve_profile` into the shared
  `build()` service. Override precedence:
  defaults → global config → project config → profile preset → flags.
- Default `target_branch` to current git HEAD when omitted; validate against
  `git branch --list`.
- Reject `--profile quick` on plan-file input. Reject non-`none` isolation on
  single-leaf builds. Reject `--isolation clone` when `clones_dir` is missing
  or unwritable.
- CLI / MCP / HTTP surfaces become thin parsers that delegate to
  `service.build()`.
- `_kick_dispatcher_tick()` reports `state seeded; dispatcher not installed`
  until Phase D ships. No silent zero return.
- Cascade resolved `isolation`, `yolo`, `stage-:*` labels to subtree on epic
  build.
- Tests: `test_entry_point_parity.py` proves CLI / MCP / HTTP produce
  identical `BuildResult`.

### Phase C — Lifecycle transition tools (plan §1.5, §1.8)

- `advance_lifecycle(db, task_id, to, reason, by_actor)` tool.
- Lifecycle-aware `mark_task_review_approved` (plan_review→test_arch, etc.).
- Lifecycle-aware `mark_task_review_rejected` with `cited_subtasks` for
  holistic; `expansion` clears `expansion_run_id` and increments attempts;
  `merging` keeps lifecycle and resets status=open.
- `de_escalate_task` extension with optional `lifecycle` and `reason`.
- Mutex-clearing event handlers wired into `claimed_by_session_id` set and
  `end_agent_run`.

### Phase D — Dispatcher package (plan §1.4, §1.6, §1.7, §1.9, §1.10)

- `src/gobby/dispatch/mutex.py` — async context manager wrapping the existing
  `TaskDispatchMutexManager`. TTL_BY_KIND, deferred release for spawn-kind,
  startup sweep, force-release.
- `src/gobby/dispatch/actions.py` — action dataclasses and `PROMPT_BUILDERS`
  registry.
- `src/gobby/dispatch/rules.py` — sixteen rules per the plan, helper
  functions, `MAX_EXPANSION_ATTEMPTS`, `AUTOMATED_LEAF_CATEGORIES`,
  yolo-never-escalates contract.
- `src/gobby/dispatch/dispatcher.py` — `TickReport` and async `run_tick`.
  Re-evaluation under mutex closes TOCTOU. Persist tick reports as JSONL.
- `src/gobby/dispatch/cron_registration.py` — registers `state-dispatcher`
  handler and idempotent cron row. Wired in `runner_init.py`.
- Expose `start_expansion_run_impl` for in-process dispatcher use.
- `_kick_dispatcher_tick` now actually runs a tick.

### Phase E — Agents and skills (plan §2.1–§2.10)

- Holistic-review skill + holistic-reviewer agent.
- Test-architect prose update (R4.F4): structured prose recommendations,
  not new `### N.N` sections.
- qa-reviewer gains `close_task` permission.
- Frontend-developer agent.
- Backend-developer agent (also default fallback for `rule_dispatch_leaf`).
- Planner clears `planning-current-verdict:rejected` label on resubmit
  (R2.F1).
- Expansion: stage-driven tree shape + Agent Selection step.
- Expansion-agent-selection skill.
- Expansion-QA transition contract: success calls
  `mark_task_review_approved`; failure calls `mark_task_review_rejected`
  with findings.
- Merge agent lifecycle integration: clean merge, conflict non-yolo,
  conflict yolo retries, yolo retries-exhausted force-advance with
  `## Yolo Fallbacks` marker and isolation preservation.

### Phase F — Retire old surfaces (plan §4.1–§4.4)

- Delete `src/gobby/conductor/`.
- Tombstone `orchestrator.yaml`, `front-half-orchestrator.yaml`,
  `conductor.yaml`, `dev-orchestrator.yaml`, `delivery-orchestrator.yaml`.
- Tombstone `developer.yaml`, `conductor.yaml` agent. Audit
  `pipeline-worker.yaml` and tombstone if unreferenced.
- Migration disables retired DB rows (preserves rows for drift detection).

### Phase G — Documentation (plan §4.5)

- `CLAUDE.md` Dispatch section.
- `GUIDING_PRINCIPLES.md` updates.
- `docs/` pages: rule-adding how-to, profiles-as-CLI-sugar, adjacent-table
  reference (`task_dispatch_mutex`, `task_artifacts`,
  `task_lifecycle_events`).
- All conductor references removed.

**Exit criterion:** Compliance matrix shows zero `missing` / `partial` rows
for deliverable sections. All Phase B–G tests green.

---

## Phase 4 — End-to-end validation

Run after Phase G. Acceptance gate for closing the recovery.

1. `tests/build_pipeline/test_plan_coverage_matrix.py` — repo-wide green.
2. `task-12725-compliance-matrix.md` — zero unaccounted deliverable
   sections.
3. `tests/build_pipeline/test_entry_point_parity.py` — CLI / MCP / HTTP
   identical `BuildResult` and DB state.
4. `tests/dispatch/test_dispatcher.py` — primed epic advances through
   `plan_review → test_arch → expanding → in_development → holistic_review →
   pr → merging → merged` across simulated ticks.
5. Holistic rejection with `cited_subtasks=[A, B]` reopens A and B and
   rewinds to `in_development`.
6. Yolo fallback test: `merge-attempts` cap triggers
   `append_description_section("## Yolo Fallbacks", ...)` and lifecycle
   advances to `merged` with isolation preserved.
7. Manual smoke: `uv run gobby build <plan>.md --profile full-yolo` in a
   sandbox project. Verify the first tick dispatches the first non-skipped
   agent and writes to `~/.gobby/logs/dispatcher.jsonl`.
8. `grep -r 'gobby.conductor' src/ tests/` returns nothing.
9. Coverage: `uv run pytest tests/dispatch/ tests/build_pipeline/
   tests/storage/tasks/test_transitions.py --cov=gobby` meets 80%.

---

## Out of scope (deferred)

- **#13552** — Real PR creation, richer merge / conflict handling,
  `merge_commit_sha` capture in `task_artifacts`. The dispatcher reaches the
  PR / merge boundary using existing tools; full PR automation is tracked
  separately. (Originally filed as #12728; recreated after cascade-delete.)

---

## What to do after expansion / qa fixes (Epic 1) ship

Operator quick-reference:

1. **Confirm Epic 1's own plan is mechanically valid.** Run the new coverage
   tooling against `task-NEW-plan-coverage-contract.md`. If it fails, the
   tooling is wrong, not the plan.
2. **Run A5 retrofit.** Update `task-12725-lifecycle-dispatch.md` to the new
   format. Generate `task-12725-compliance-matrix.md`. Eyeball the matrix —
   it should match the audit findings (sections 1.1–1.3, 1.1a–1.1d, 1.2 =
   `done`; 3.1 = `partial`; everything else = `missing`).
3. **Author the Epic 2 plan.** Use the matrix as the authoritative gap
   list. The Epic 2 plan, by virtue of being authored under the new
   contract, will pass A6 CI.
4. **Expand Epic 2 under the new contract.** Expansion-QA's mechanical
   coverage check must approve before any dev work starts. If it rejects,
   the plan or the expansion is incomplete — fix at the source, do not
   bypass.
5. **Ship Phase B first.** It is the smallest, highest-leverage wedge: it
   makes `gobby build` honest about its own state and unblocks every later
   phase.
6. **Phases C / D / E are sequential.** D depends on C's lifecycle tools;
   E's agents depend on D's dispatcher.
7. **F and G can run in parallel with E** once D is stable.
8. **Treat #13552 as a separate intake.** Do not re-scope it into Epic 2.
