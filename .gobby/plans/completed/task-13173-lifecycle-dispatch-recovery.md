# Recovery Plan: Close lifecycle-dispatch plan-compliance gaps after #12725

> Round 5 — revised after plan-adversary Round 4 surfaced two further
> blocking findings (F16–F17) on the F14/F15 closure tails. All seventeen
> findings (F1–F17) were accepted in full and integrated. F1–F15 closures
> stand from earlier rounds; F16 (manifest-path consistency) and F17
> (`base_commit_sha` migration) close in this revision.

## Context

Epic #12725 ("lifecycle-dispatch") merged. The approved plan
(`.gobby/plans/task-12725-lifecycle-dispatch.md`, 36 sections) defined a full
state-driven dispatch system: storage foundation, build entrypoint contract,
dispatcher package, lifecycle transition tools, cron registration, agent/skill
content updates, and tombstoning of the conductor.

A code-level audit against the plan found that **only the foundation actually
landed**:

- Storage tables (`task_dispatch_mutex`, `task_artifacts`,
  `task_lifecycle_events`) and managers — done.
- Six new task columns (`lifecycle`, `allow_automation`, `yolo`, `isolation`,
  `assigned_agent`, `additional_skills`) — done.
- `BuildConfig`, `load_build_config`, `resolve_profile`,
  profile presets — defined in `src/gobby/config/build.py`, **but never called
  from any build entry point**.
- A `build()` service exists in `src/gobby/build/service.py` and accepts the
  fields, **but does not resolve profiles, does not load `BuildConfig`, does
  not default `target_branch`, and `_kick_dispatcher_tick()` returns 0**.
- `src/gobby/dispatch/` does not exist. No `rules.py`, `dispatcher.py`,
  `actions.py`, `TickReport`, `run_tick`, `state-dispatcher` cron, or
  `advance_lifecycle` tool.
- 30+ plan sections (§1.4–§1.10, §2.x, §3.2 entry-point wiring, §3.3–§3.4,
  §4.x) are unimplemented.

**The fundamental break is in the expansion → expansion-qa contract**, not just
in #12725's task tree:

1. **Expansion has no input-coverage contract.** It is a compression step that
   reads a plan and emits whatever subtasks the LLM decides are needed.
   Nothing enforces "every plan-section heading must produce ≥1 leaf or carry
   an explicit deferral marker." Sections vanish silently.
2. **Expansion-QA validates output structure, not input coverage.** It checks
   `### N.N` formatting, `assigned_agent` presence, no-planning-category, etc.
   — all internal coherence. It never opens the source plan and computes a
   coverage diff. A 30-section gap looks identical to a clean expansion.
3. **No plan-section provenance flows downstream.** Leaves carry no structured
   coverage records. Holistic-review's qualitative 4-point method
   (Scope/Reality/Testing/YAGNI) sees what was produced, not what was omitted.
   It cannot deterministically say "section §1.7 has no corresponding change."

A one-off compliance matrix for #12725 closes this epic but does not fix the
contract. The next plan-driven epic will fail the same way. Phase A of this
recovery is therefore a **structural fix** to the expansion/QA contract, not a
band-aid for #12725.

**Scope decision:** new work, not a reopen of #12725. The merged commits are
correct as far as they went; the foundation is intact and reused — but with
the F7 caveat below.

**This document is a strategy plan covering two epics**, not a single
implementation plan:

- **Epic 1 — Plan-coverage contract** (Phase A below). Detailed plan to be
  authored at `.gobby/plans/task-NEW-plan-coverage-contract.md` after this
  strategy is approved. Authored under, and validated by, the bootstrap
  coverage ledger described in A8.
- **Epic 2 — #12725 gap recovery** (Phases B–G below). Detailed plan to be
  authored at `.gobby/plans/task-NEW-12725-gap-recovery.md` after Epic 1
  ships, expanded under the new contract.

Epic 2 is gated on Epic 1.

**Bootstrap.** Epic 1's own plan cannot be coverage-checked by tooling that
does not yet exist. It is **not blanket-grandfathered**. Instead it is gated
by the bootstrap coverage ledger in A8: a manually authored
section-to-leaf-to-acceptance-item map, adversary-reviewed before any Epic 1
implementation work begins, and re-validated by the new tooling once it lands
(blocking Epic 1 close until that re-validation succeeds). Any
`.gobby/plans/.grandfathered` use is gated, scoped, and explicit, and is
itself owned by a removal task.

**F7 caveat on the foundation.** The merged storage tables and helpers prove
narrow invariants only (artifact pair/XOR, basic mutex acquire/expiry,
append-only events, migration shape). They **do not** prove `run_id` semantics
in spawn flows, startup sweep against `running_agents`, claim/end_agent_run
mutex clearing, FK cascade, candidate scanning with deps + mutex rows, or
artifact plan-path/hash semantics. A storage-foundation audit (Phase D0)
runs before the dispatcher is built so the recovery does not inherit silent
gaps.

---

## Phase A — Fix the Expansion/QA Contract (Structural)

Phase A is **a precursor epic of its own**. It ships before B–G start, because
B–G are the kind of work that must be expanded under the new contract for
their own coverage to be guaranteed. Trying to recover #12725 under the broken
contract just relocates the failure mode.

Phase A's deliverables are typed contracts, a deterministic coverage library,
the expansion-qa integration that uses it, an evidence-based holistic-review
gate, the #12725 retrofit, the bootstrap coverage ledger that protects Epic 1
itself, repo-wide CI, and documentation.

### A1. Plan format spec (typed grammar)

Update `src/gobby/install/shared/skills/plan/SKILL.md` (or equivalent
planning skill) and the planner agent so plans MUST emit a typed structure
the parser can consume.

The grammar must cover, at minimum:

- Heading levels `##` through `######`.
- Numeric section IDs (`## §1.7`, `### 1.7`, `### 1.1a`, `### 2.8b`).
- Alpha-prefixed section IDs (`## A1`, `## A10`, `### D0.1`, `### B5`),
  required because this very strategy uses them and any plan-driven epic
  may. **A numeric-only regex would silently skip Phase-A-style sections
  and recreate the original failure mode (F8).**
- Section IDs of any depth (`§1`, `§1.1`, `§1.1.1`, …) and with optional
  letter suffix on the last segment.

**Canonical regex** (frozen at strategy level; pinned in A2 fixture):

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
```

The trailing `|$` lookahead allows bare headings with no title or
delimiter (`### 1.1a`, `## A10`) — needed because both styles appear in
real plans.

Required parser fixtures (A2 must verify each), each pinned in **both
bare and titled forms**:

- Bare: `### 1.1a`, `### 1.1d`, `### 2.8a`, `### 2.8b`, `## A1`, `## A10`,
  `### D0.1`, `## B5`.
- Titled: `## §1.7 Decision rules`, `### 1.1a Lifecycle enum and
  automation fields`, `## A1 Plan format spec (typed grammar)`,
  `### D0.8 Dispatcher slot reservation primitive (F11)`.
- Negative case: `## Phase A — Fix the Expansion/QA Contract` (a heading
  with no canonical section ID; parser yields `kind: framing` or
  raises depending on context).

Other required structure:

- Front-matter per section: `kind: deliverable | framing | verification |
  deferred`. (Verification is its own kind so verification subsections do
  not need fake `Acceptance:` lines.)
- `**Acceptance:**` block per `deliverable` section, listing one or more
  numbered acceptance items: `A1.7.1`, `A1.7.2`, etc. Each item names a
  concrete artifact: a file path, a symbol, a test name, a documented
  behavior.
- Table-sourced deliverables (Phase E in this very plan uses one) must
  declare each row as its own acceptance item with a stable ID, so the
  parser can enumerate them deterministically.
- Deferral as a typed object (see A3 below); not free prose.

The plan-adversary review must reject plans missing IDs, kind, acceptance
items, or that contain section-ID collisions or unparseable headings. The
"reject" is mechanical (parser raises) before the adversary applies
qualitative judgment.

### A2. Plan AST and parser library

A new library `gobby.plans.parser` exposes:

- `parse_plan(path) -> PlanDocument` — pure function, no DB calls.
- `PlanDocument`: `plan_id`, `source_hash` (sha256 of the file contents at
  parse time), and `sections: list[PlanSection]`.
- `PlanSection`: `section_id`, `parent_id` (for nested sections),
  `heading_level`, `title`, `kind` (`deliverable | framing | verification |
  deferred`), `acceptance_items: list[AcceptanceItem]`,
  `deferral: Deferral | None`, `source_span` (line range).
- `AcceptanceItem`: `item_id`, `prose`, `artifact_kind` (file | symbol |
  test | behavior), `artifact_ref` (string).
- `Deferral`: `task_ref`, `reason`, `owner`, `original_acceptance_items`
  (carried over verbatim from the deferred section).

Parser fixture tests live in `tests/plans/test_parser.py` and pin behavior
against:

- `.gobby/plans/task-12725-lifecycle-dispatch.md` — numeric IDs incl.
  `1.1a–1.1d` and `2.8a–2.8b`.
- `.gobby/plans/task-13173-lifecycle-dispatch-recovery.md` — alpha IDs
  incl. `A1`, `A10`, `D0.1`, `B5`. (The recovery strategy reviews itself.)

The canonical regex from A1 is the parser's source of truth; the test
imports it as a constant and asserts each fixture string matches with the
expected `section_id` capture. Adding a new heading style without updating
the parser AND the regex breaks the fixture, not silently.

`source_hash` is the load-bearing invariant: every downstream artifact
(coverage manifest, expansion output, holistic-review evidence) records the
hash of the plan it was computed against. Any mismatch invalidates the
artifact and forces re-computation.

### A3. Typed deferral and typed coverage contract

Two related typed contracts replace the original "free-form labels" idea
(F1, F6).

**Deferral (typed object).** A section with `kind: deferred` must carry:

- `task_ref` — a real task ID that exists in the task store, is open or
  active (not closed), has a `deferred-from:<plan-id>:<section-id>` label,
  duplicates the deferred section's acceptance items into its own
  validation criteria, names a reason and an owner, and either is a
  dependency of the recovery epic or is explicitly out-of-scope with a
  cited parent task.

The parser populates `Deferral` and the coverage library validates each of
the above. A deferral pointing at a non-existent or closed task fails the
gate. A deferral whose `task_ref` lacks the provenance label fails. A
deferral missing duplicated acceptance fails. There is no plain-text
deferral escape hatch.

**Coverage records (structured `covers` entries).** Every leaf carries one
or more `covers` records (stored as structured task labels of the form
`covers:<plan-id>:<section-id>:<acceptance-item-id>`). A leaf may carry
multiple covers; an acceptance item may be covered by multiple leaves.

A `covers` record is valid only if:

- The named `plan-id` and `section-id` resolve in `PlanDocument`.
- The acceptance item exists in that section.
- The leaf's `validation_criteria` references the acceptance-item's
  artifact (file/symbol/test/behavior) by name. The library does substring
  + regex checks; over-broad leaves that claim coverage without
  referencing the specific artifact fail validation.

This closes F1: provenance is no longer a label that can be slapped on any
leaf. The matcher is structured and verifies the leaf actually points at
the named artifact.

### A4. Coverage library and `gobby plan coverage` CLI

A new module `src/gobby/plans/coverage.py` and a CLI `gobby plan coverage`
provide the deterministic gate everything else consumes (F5/F9).

Inputs are explicit, never implicit:

- `--plan <path>` — the plan file.
- `--plan-hash <sha256>` — required; must match the file. Mismatch raises.
- `--plan-id <id>` — canonical plan ID (e.g. `task-12725-lifecycle-dispatch`).
  Disambiguates multi-plan epics that share a task subtree.
- `--root-task <task-ref>` — required for `--task-tree db | jsonl`. The
  root of the subtree that should satisfy this plan. Coverage only counts
  leaves whose path-cache descends from this root. Without it the library
  cannot validate A3's "dependency of recovery epic" deferral rule, and
  could count leaves from unrelated plans (F9).
- `--project-id <uuid|slug>` — required when the task store may hold more
  than one project; namespaces `--root-task` and `--evidence`.
- `--task-tree <source>` — `db | jsonl | matrix-file`. `db` queries the
  task store via Gobby; `jsonl` reads `.gobby/tasks.jsonl`; `matrix-file`
  reads a pre-generated manifest (used by CI when no DB is available).
- `--evidence <kind>` — `commits:<range> | task-diff:<task-ref> |
  worktree-diff:<artifact-ref> | coverage-matrix:<path> | none`. Drives A6.
- `--manifest <path>` — output coverage manifest (see below).

Outputs:

- A typed `CoverageReport` with header `{plan_id, plan_hash,
  root_task_ref, project_id, generated_at, task_tree_source_hash,
  evidence_summary}` and rows: `(section_id, acceptance_item_id, status,
  leaves, evidence)` where `status ∈ {covered, deferred, missing,
  invalid}`. Each `leaf` row carries the leaf's task ref and its
  `validation_criteria` snippet that satisfied the artifact match.
- A coverage manifest keyed on the full identity tuple
  `(project_id, plan_id, root_task_ref, plan_hash)`. **Canonical path
  scheme**, computed by a single shared helper:

  ```python
  def coverage_manifest_path(project_id: str, root_task_ref: str, plan_id: str) -> Path:
      """Resolve the canonical coverage-manifest path. Single source of
      truth for every call site (A5 expansion-qa, A7 retrofit, A9 CI,
      task_artifacts.coverage_matrix_path, all docs).
      """
      return Path(
          ".gobby/plans/coverage"
          f"/{_sanitize(project_id)}"
          f"/{_sanitize(root_task_ref)}"
          f"/{_sanitize(plan_id)}.coverage.yaml"
      )
  ```

  **`_sanitize` rules** (portable across macOS, Linux, Windows):

  - Replace any character outside `[A-Za-z0-9._-]` with `-`.
  - Strip leading/trailing `-`, `.`, and `_`.
  - For `root_task_ref`: drop the leading `#` (`#12725` → `12725`).
  - Reject empty strings after sanitization (raise; never produce an
    empty path component).
  - Cap each component at 64 chars (truncate-with-hash if longer).

  `plan_hash` lives **inside** the file, not the path, so a regeneration
  overwrites in place. Pre-existing manifests with mismatched
  `plan_hash` are surfaced as CI failures (A9).

  **Duplicate-identity rejection.** A4's library refuses to generate
  two manifests with the same `(project_id, root_task_ref, plan_id)`
  identity unless the second is an explicit regeneration of the first
  (same `plan_hash` or an explicit `--regenerate` flag with audit log
  entry). Two different plans cannot share `plan_id` under the same
  root.

- Exit 0 on all-covered-or-deferred; non-zero otherwise. Specific exit
  codes per failure category so CI can attribute them.

**Multi-plan epics, multi-root plans.** Manifests are keyed on the full
identity tuple. The same plan reused across two root tasks in the same
project produces two manifests at distinct paths (one per
`<root_task_ref>` directory). Two different plans under the same root
produce two manifests at distinct paths (one per `<plan_id>` filename).
Task artifacts (`task_artifacts.coverage_matrix_path`) point at the
exact manifest for that root via `coverage_manifest_path(...)` —
**never** a shared or guessed path.

Expansion-QA, holistic-review, and CI all call this library — never their
own ad-hoc parser or matcher.

### A5. Expansion-QA integration

Update `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` and
the expansion-qa skill so validation calls the A4 library, not its own
parsing logic:

1. Resolve the epic's `plan_file_path`, `plan_id`, and `project_id`;
   re-compute `plan_hash`.
2. Call `coverage.evaluate(plan_path, plan_hash, plan_id=<id>,
   task_tree="db", root_task_ref=<epic_ref>, project_id=<id>,
   evidence="none")`. The library signature **rejects** `db`/`jsonl`
   calls without `plan_id`, `root_task_ref`, and `project_id` at type-
   and test-time, so a stale call site fails noisily.
3. Reject the run if any deliverable acceptance item has status
   `missing` or `invalid`. Cite each by `(section_id, item_id)` and by
   the leaves that claimed but failed coverage (so the rejection points
   at real artifacts).
4. Persist the resulting manifest to the canonical scoped path (see A4
   path scheme) and reference it from the epic
   (`task_artifacts.coverage_matrix_path`).

This is the gate the original epic lacked. It runs before any code task
starts, and the failure modes name the specific acceptance items the
expansion missed.

### A6. Evidence-based holistic-review integration (yolo-aware)

The holistic-review gate (F4) **does not** depend on PR diffs and **does
not** depend on Phase E's holistic-review skill being present. Phase A
ships the gate library; Phase E's holistic-review skill/agent (when they
land in Epic 2) wire the qualitative review on top of it.

The gate consumes a generic change-evidence artifact. **Four evidence
kinds, treated identically by the matcher:**

- `commits:<range>` — for PR-creating runs, derived from the merge
  target.
- `task-diff:<task-ref>` — aggregates commits **linked** to the task via
  existing `gobby-tasks:get_task_diff`.
- `worktree-diff:<artifact-ref>` — **resolves directly from the task's
  worktree or clone artifact** (F10). The artifact provides
  `worktree_path` (or `clone_path`), `target_branch`, and
  `base_commit_sha` — an immutable SHA captured at worktree/clone
  creation time. The library computes
  `git -C <path> diff <base_commit_sha>...HEAD`, **not** against
  `target_branch` (which is mutable: stale, advanced, or deleted
  locally). Required for yolo flows whose commits are local to the
  isolation and not yet linked to the task. If `base_commit_sha` is
  missing (e.g., a legacy row created before D0's migration; F17), or
  the SHA does not resolve in the artifact's repo, or both
  `worktree_path` and `clone_path` are empty, the resolver yields an
  `invalid` evidence row citing the artifact and requests repair —
  it does **not** silently degrade to no-evidence (F15). Repair for
  legacy rows: rerun `gobby build` against the same task to recreate
  the isolation with a captured base, or use a manual
  `set_artifact(base_commit_sha=...)` if the historical base can be
  recovered out-of-band.
- `coverage-matrix:<path>` — for dry runs (e.g., A8 ledger validation).

`none` is reserved for explicit operator override and emits an audit
marker.

**Lifecycle linkage point.** Worktree-local commits become
linked-task evidence at two well-defined points: (a) when the dev agent
calls `link_commit` or `mark_task_needs_review` (existing tools auto-link
recent commits in the agent's session), and (b) when the merge agent
finalizes and runs `link_commit` on the merge SHA. **Until either
happens, `worktree-diff` is the only valid evidence source for a
yolo/isolation run** — the gate must not require linkage to pass.

The gate verifies that every deliverable acceptance item has at least one
piece of evidence (commit touching the named file/symbol, test of the
named name, or documented behavior reachable in the diff). Yolo never
escalates: when evidence is missing the gate emits a structured rejection
that the rule layer (Phase E) interprets as `request_changes`, not as an
escalation. There is no path that escalates solely because a PR does not
exist or because commits are not yet linked.

### A7. #12725 retrofit and compliance matrix

After A1–A6 ship, retro-apply them to #12725:

- Edit `.gobby/plans/task-12725-lifecycle-dispatch.md` so it conforms to
  A1: stable section IDs (already mostly there), `kind` front-matter,
  `**Acceptance:**` items with stable item IDs.
- Run with full scope inputs:

  ```bash
  gobby plan coverage \
    --plan .gobby/plans/task-12725-lifecycle-dispatch.md \
    --plan-id task-12725-lifecycle-dispatch \
    --plan-hash <sha256> \
    --root-task '#12725' \
    --project-id <gobby-project-uuid> \
    --task-tree db \
    --evidence 'commits:<merged-range>' \
    --manifest <see A4 scoped path scheme>
  ```

- The resulting manifest at the path returned by
  `coverage_manifest_path(<project_id>, "12725", "task-12725-lifecycle-dispatch")`
  is the authoritative gap inventory and the acceptance checklist for
  Epic 2.

The expected output (from the audit so far) is roughly: §1.1, §1.1a–§1.1d,
§1.2, §1.3 = `covered`; §3.1 = `partial` (typed result calls out which
acceptance items are covered vs. missing); the rest = `missing`. If the
real run disagrees, that disagreement is itself the audit finding.

### A8. Bootstrap coverage ledger for Epic 1

Epic 1 cannot validate itself with tooling that does not yet exist
(F3). Hand-validation of format alone is insufficient. Replace the
blanket-grandfathering with a real ledger.

Deliverables:

- `.gobby/plans/task-NEW-plan-coverage-contract.coverage-ledger.yaml`
  authored manually before any Epic 1 implementation task is created. It
  enumerates every section of the Epic 1 plan, every acceptance item, and
  the planned leaf(s) that will cover each item. Owners and validation
  criteria per leaf are spelled out. This is what we lacked when #12725
  was expanded.
- A plan-adversary review of the ledger itself (separate spawn, separate
  artifact) that must approve before Epic 1 expansion runs. This catches
  ledger gaps the same way A5 catches expansion gaps.
- A blocking acceptance gate on Epic 1 closure: once A4's library exists,
  `gobby plan coverage --plan <epic-1-plan>` must produce a manifest
  whose rows match the ledger's expected coverage. Mismatch blocks Epic 1
  from closing.
- The `.gobby/plans/.grandfathered` mechanism is **not** used for Epic 1.
  It exists only for already-merged epics (e.g., #12725 itself before A7
  retrofit completes), and any addition to it requires a co-located
  removal task.

### A9. Repo-wide CI and manifest-hash gate

`tests/plans/test_plan_coverage_ci.py` (new):

- Walks every plan file under `.gobby/plans/` whose epic is not yet
  `merged`.
- For each plan, resolves `plan_id` and the linked `root_task_ref`(s)
  from `task_artifacts` (or from a committed plan-index file when the
  task DB is not available in CI). For each `(project_id, plan_id,
  root_task_ref)` identity, asserts a coverage manifest exists at
  `coverage_manifest_path(project_id, root_task_ref, plan_id)`.
- Re-computes `plan_hash` from the plan file and asserts it matches
  the manifest's `plan_hash`. A modified plan without a regenerated
  manifest fails CI.
- Calls `gobby plan coverage --plan <path> --plan-id <id> --plan-hash
  <h> --task-tree matrix-file --manifest <path>` and asserts zero
  `missing | invalid` rows.
- Asserts no orphan manifests: every manifest under
  `.gobby/plans/coverage/` resolves to a live plan + root pairing.
- Asserts no new entries in `.gobby/plans/.grandfathered` since the
  last signed-off commit; new entries require a parallel
  `# remove-by: <task-ref>` line and that task must exist and be open.

CI does not need the live task DB for this; the manifest is committed
and self-describing. The library re-validation against `db` runs at
expansion-qa time, not in CI.

### A10. Documentation

Document the contract in `CLAUDE.md` and in the plan/expansion/expansion-qa
skill READMEs:

- The plan grammar and typed AST (with the canonical regex).
- The deferral object and its provenance contract.
- Structured `covers:<...>` records as the only valid coverage signal;
  free-form `plan-ref:` labels are not honored.
- The `gobby plan coverage` CLI and its inputs/outputs.
- The yolo-aware evidence pipeline (commits | task-diff | matrix | none).
- The bootstrap ledger requirement for new epics until the tooling
  ecosystem is mature.

A1–A10 ship as a single epic ("Plan-coverage contract"). It blocks B–G.

---

## Phase B — Build Entry-Point Contract (plan §3.1, §3.2, §3.4)

Wire the existing config and profile-resolution code into the actual entry
points so `gobby build` honors the plan contract.

### B1. Resolve effective `BuildOptions` in the shared service

In `src/gobby/build/service.py`:

- Call `load_build_config(project_root)` at the top of `build()`.
- Call `resolve_profile(cfg, opts.profile, input_ref)` and merge the resulting
  `{skip_stages, isolation, yolo}` into `opts` with explicit precedence:
  **defaults → global config → project config → profile preset → explicit
  flags**.
- Default `opts.target_branch` to `git rev-parse --abbrev-ref HEAD` (in the
  project directory) when `None`. Validate against `git branch --list <name>`;
  raise with available-branches list on miss (plan §3.2 R4.F6).
- Reject `--profile quick` on plan-file input (plan §3.2 R4.F3).
- Reject non-`none` isolation on single-leaf builds (plan §3.2 R6.F2).
- Reject `--isolation clone` if `BuildConfig.clones_dir` is missing/unwritable
  (plan §3.2 R4.F2).

### B2. Strip duplicated parsing from CLI/MCP/HTTP

`src/gobby/cli/build.py`, `src/gobby/mcp_proxy/tools/build.py`, and
`src/gobby/servers/routes/build.py` must each:

- Parse argv/JSON into a raw `BuildOptions` (no resolution).
- Hand off to `service.build(input_ref, opts, db, project_id)`.
- Render the same `BuildResult`.

No surface should call `resolve_profile` itself — single source of truth in the
service.

### B3. Cross-surface parity test

`tests/build_pipeline/test_entry_point_parity.py`: drive the same fixture input
through CLI, MCP, and HTTP entry points and assert identical `BuildResult` and
identical post-state in `tasks` + `task_artifacts`. This is the test missing
today.

### B4. Cascade resolved state to subtree (plan §3.4)

When `gobby build` primes an epic, snapshot resolved `isolation`, `yolo`, and
`stage-:*` labels onto every descendant; set `allow_automation=true` on
subtree. Do **not** cascade `assigned_agent`, `additional_skills`, or
`lifecycle`. Implement in `src/gobby/build/service.py` using helpers in
`src/gobby/storage/tasks/_crud.py` (existing patterns).

### B5. Honest dispatcher boundary while Phase D is pending

Until Phase D ships, `_kick_dispatcher_tick()` must return a result the surface
can render as `state seeded; dispatcher not installed`, and the CLI/MCP/HTTP
output must surface that to the operator. Silent `return 0` is what hid the
incompleteness in the first place; we don't repeat it.

---

## Phase C — Lifecycle Transition Tools (plan §1.5, §1.8)

### C1. `advance_lifecycle` tool and helper

Add `advance_lifecycle(db, task_id, to: Lifecycle, reason: str, by_actor: str)`
in `src/gobby/storage/tasks/_transitions.py`. Mandatory `reason`. Resets
`status=open` unless transitioning `merging→merged` (then `status=closed`).
Writes a `task_lifecycle_events` row via the existing
`TaskLifecycleEventManager`. Expose as MCP tool on `gobby-tasks-ops`.

### C2. Lifecycle-aware `mark_task_review_approved`

Extend the existing tool: on approval, advance lifecycle per the plan stage
table (plan_review→test_arch, test_arch→expanding, expanding→in_development,
holistic_review→pr, merging→merged), reset status=open (or closed for merged),
and record an event with `reason="mark_task_review_approved"`,
`by_actor=current_session_agent`. Leaf in_development: no lifecycle change.

### C3. Lifecycle-aware `mark_task_review_rejected`

Extend signature with `cited_subtasks`. Behavior by lifecycle:

- `plan_review`: stay; add rejected label; increment round; append findings.
- `expanding`: stay; append findings; clear `expansion_run_id`; increment
  `expansion_attempts` (plan §1.8 R4.F1).
- `holistic_review`: require `cited_subtasks`; atomically append findings,
  reopen cited leaves, rewind to `in_development` (plan §1.8 R4.F5).
- `merging`: stay; reset `status=open`; append findings (plan §1.8 R6.F4).
- leaf `in_development`: no lifecycle change; `status=open`.

### C4. `de_escalate_task` extension

Add optional `lifecycle` and `reason` params; performs escalation clear,
status set, and lifecycle advance in one transaction (plan §1.8).

### C5. Mutex-clearing event handlers (plan §1.5)

In `src/gobby/hooks/event_handlers/_task.py` (or equivalent) hook into
`claimed_by_session_id` set and `end_agent_run`. When the claim's
`agent_run_id` matches `task_dispatch_mutex.run_id`, call
`clear_reservation(db, task_id, run_id)`. Same on `end_agent_run` for the
linked task. Existing storage helper already supports this; the wiring is
missing.

### C6. Tests

`tests/storage/tasks/test_transitions.py` and `tests/hooks/test_task_events.py`
covering each branch of C2/C3/C4 and the claim/end_agent_run handler paths.

---

## Phase D0 — Storage-Foundation Audit (F7 pre-flight)

Runs after Phase C and **before** Phase D. The merged storage helpers prove
narrow invariants only; the dispatcher's correctness depends on a wider set
that is not yet covered. We audit the foundation in code and add the missing
tests *before* we build on it. Any failure is fixed inside Epic 2, not
deferred.

### D0.1 `run_id` semantics under spawn

Tests in `tests/storage/tasks/test_dispatch_mutex_runid.py`:

- Spawn-kind acquire writes `run_id`; release path requires matching holder
  AND run_id.
- `clear_reservation(db, task_id, run_id)` is a no-op when run_id mismatches.
- Acquire on a row whose `run_id` is set blocks acquire by a different
  holder, even if `lease_until` has expired (TTL alone does not unlock
  spawn-kind).
- `clear_by_run_id(run_id)` removes all rows with that run_id, idempotent.

### D0.2 Startup sweep against `running_agents`

Tests in `tests/storage/tasks/test_dispatch_mutex_sweep.py`:

- `sweep_on_startup(db)` removes mutex rows whose `run_id` references a
  terminated `running_agents` row.
- Rows with live agents remain.
- Rows with no `run_id` (non-spawn kinds) are reaped only on TTL expiry.

### D0.3 Claim and end_agent_run handler integration

Tests in `tests/hooks/test_task_dispatch_mutex_handlers.py`:

- Setting `claimed_by_session_id` on a task with a matching mutex `run_id`
  clears the mutex.
- `end_agent_run` on a session linked to a task clears the linked task's
  mutex.

(These are the same handlers Phase C5 wires — D0 verifies the helpers they
call work end-to-end with realistic fixture state, not just unit-mocked.)

### D0.4 FK cascade on task delete

Tests in `tests/storage/tasks/test_artifacts_cascade.py` and
`test_lifecycle_events_cascade.py`:

- Deleting a `tasks` row cascades to `task_artifacts`,
  `task_dispatch_mutex`, and `task_lifecycle_events` per the schema.
- A failure mode here would orphan rows that mislead candidate scanning.

### D0.5 Candidate scanner with deps + mutex rows

Tests in `tests/storage/tasks/test_candidates.py`:

- `list_automation_candidates(db)` honors dependency blocks (BLOCKING_STATES
  per plan §1.3a).
- A candidate with an active mutex row is excluded from results (the rule
  layer re-checks under lock; the scanner pre-filter spares wasted work).
- Escalated dependencies always block; `yolo` does not bypass.

### D0.6 Artifact plan-path/hash and isolation-base semantics

Tests in `tests/storage/tasks/test_artifacts_plan_path.py`:

- Setting `plan_file_path` and `plan_file_hash` (new field if not present)
  is atomic.
- Reading them back returns identical strings.
- Updating `plan_file_path` without updating `plan_file_hash` raises (the
  pair is a typed unit, like `worktree_path/id` and `clone_path/id`).
- If `plan_file_hash` is not yet a column, D0 adds it via migration (small
  additive change; gated by test).

Tests in `tests/storage/tasks/test_artifacts_isolation_base.py` (F15):

- `base_commit_sha` is a new **nullable** column on `task_artifacts`,
  captured at worktree/clone creation for new isolations. D0 adds it
  via additive migration; existing rows from the post-#12725 baseline
  (which may already have `(worktree_path, worktree_id)` or
  `(clone_path, clone_id)` without `base_commit_sha`) are preserved
  intact (F17).
- **No CHECK constraint** requires `base_commit_sha` non-null on legacy
  rows. The schema-level invariant is weaker than the app-level one:

    - Schema CHECK: `base_commit_sha IS NULL` if both isolation families
      are NULL. (Forward-compatibility: a non-null `base_commit_sha`
      with no isolation family is rejected as nonsense.)
    - App-level enforcement (in `set_artifacts_atomic` and
      `_validate_constraints`): a **new write** that sets either
      isolation family without `base_commit_sha` raises
      `MissingIsolationBaseError`. Reads of existing rows with
      `base_commit_sha IS NULL` are tolerated; the A6 resolver yields
      an `invalid` evidence row citing the artifact and asks for
      repair.
- `clear_isolation_pair(family)` clears the family columns and
  `base_commit_sha` atomically.
- B-phase isolation handlers populate `base_commit_sha` via
  `git -C <path> rev-parse HEAD` immediately after creating the
  worktree/clone, before any agent runs against it. New isolations
  created after the migration always have a non-null base.
- Upgrade test: starting from a fixture DB at the post-#12725 baseline
  with one worktree row and one clone row (both lacking
  `base_commit_sha`), the migration leaves them in place; new isolation
  creates capture a base; A6 resolver returns `invalid` evidence rows
  for the legacy two and `covered` for a freshly created one.
- MCP artifact payloads (`get_artifacts`, `set_artifact`,
  `set_artifacts_atomic`, `clear_isolation_pair`) include
  `base_commit_sha` in their schemas with explicit nullability.
- `baseline_schema.sql` adds the column for fresh installs **with the
  same nullable semantics** so fresh and upgraded databases diverge
  only in row history, not in schema.

### D0.7 Migration upgrade behavior

Tests in `tests/storage/test_migration_upgrade.py`:

- Fresh-install schema matches `baseline_schema.sql`.
- Upgrade from a pre-#12725 baseline produces the same end-state as a fresh
  install (column-by-column).
- Upgrades preserve existing rows; do not drop user data.

### D0.8 Dispatcher slot reservation primitive (F11)

The dispatcher cannot rely on "count active agents and check against
`max_active`" because two ticks running concurrently (cron + build-kicked
+ manual) can each observe free capacity on disjoint candidates and both
spawn, exceeding the cap. Per-task mutex does not help — the tasks are
different. We need an atomic slot reservation primitive.

**Schema** (additive migration):

```sql
CREATE TABLE dispatcher_active_slots (
    project_id        TEXT NOT NULL,
    agent_run_id      TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    claimed_at        TIMESTAMP NOT NULL,
    ttl_until         TIMESTAMP NOT NULL,
    PRIMARY KEY (project_id, agent_run_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX idx_dispatcher_slots_project ON dispatcher_active_slots(project_id, ttl_until);
```

**Primitive** in `src/gobby/storage/tasks/_dispatch_slots.py`:

- `try_reserve_slot(db, project_id, max_active, agent_run_id, task_id, ttl) -> bool`
  — atomic; uses a single `INSERT ... WHERE (SELECT COUNT(*) FROM
  dispatcher_active_slots WHERE project_id = ? AND ttl_until > now) <
  max_active` pattern (or equivalent SQLite-safe atomic CAS). Returns
  False without inserting if the cap is full.
- `release_slot(db, project_id, agent_run_id)` — DELETE.
- `sweep_expired_slots(db, project_id, now)` — DELETE rows whose
  `ttl_until` has passed.
- `sweep_dead_slots_against_running_agents(db, project_id)` — DELETE rows
  whose `agent_run_id` is not in `running_agents`. Called on startup and
  periodically.

Tests in `tests/storage/tasks/test_dispatch_slots.py`:

- Two concurrent `try_reserve_slot` calls under cap=N where currently N-1
  are held: exactly one succeeds.
- N concurrent calls under cap=N: exactly N succeed.
- Expired rows are reaped by `sweep_expired_slots`.
- Dead-agent rows are reaped by
  `sweep_dead_slots_against_running_agents`.
- FK cascade on task delete clears the slot row.
- The primitive is project-scoped: project A holding N slots does not
  block project B from also holding N.

This primitive is consumed by D4 (`run_tick`); D8 covers the
end-to-end concurrent-tick race.

### D0.9 Audit summary task

A single audit summary task (this Phase D0) produces a written report of
which invariants held, which were fixed, and which (if any) required
schema changes. The Phase D plan reads that report; if D0 found anything
non-trivial, Phase D rules and helper code consume the verified primitives,
not assumptions.

---

## Phase D — Dispatcher Package (plan §1.4, §1.6, §1.7, §1.9, §1.10)

This is the largest phase. Land it after B, C, and D0 are green so the
dispatcher can rely on entry-point state, lifecycle tools, and a
foundation that has actually been audited.

### D1. `src/gobby/dispatch/mutex.py` (plan §1.4)

Async context manager `acquire(db, task_id, holder, kind, agent_run_id)`
wrapping the existing `TaskDispatchMutexManager`. Implement TTL_BY_KIND
(spawn=600s, expansion=120s, worktree=120s, lifecycle=30s, field=30s),
deferred release for spawn-kind via `detach_from_context(task_id)`,
`sweep_on_startup(db)` reconciling against `running_agents`, and
`force_release(db, task_id, reason)`.

### D2. `src/gobby/dispatch/actions.py` (plan §1.6)

Dataclass action types: `SpawnAgent`, `StartExpansionRun`, `CreateWorktree`,
`CreateClone`, `AdvanceLifecycle`, `CloseLeaf`, `EscalateTask`,
`AppendAuditMarker`, `Skip`. Union `Action`. `PROMPT_BUILDERS` registry mapping
builder key to `(prompt, initial_variables)` per agent.

### D3. `src/gobby/dispatch/rules.py` (plan §1.7)

Sixteen rules in the order specified by the plan, plus the helper functions
(`_stage_enabled`, `_current_verdict_rejected`, `_rounds_remaining`,
`_expansion_active`, `_expansion_run_completed`, `_expansion_attempts`,
`_target_branch`, `_is_coding_epic`, `_has_ready_subtasks`,
`_all_subtasks_closed`, `_has_worktree`, `_has_clone`,
`_has_isolation_artifact`, `_parent_epic`, `_now_iso`, `_is_yolo`,
`_skipped_stages`). Constants: `MAX_EXPANSION_ATTEMPTS=3`,
`AUTOMATED_LEAF_CATEGORIES={code, config, docs, test}`. Yolo-never-escalates
contract enforced per rule.

`evaluate(task) -> Action | list[Action]` is the single entry point for the
dispatcher.

### D4. `src/gobby/dispatch/dispatcher.py` (plan §1.9)

`TickReport` dataclass + `run_tick(db, project_id, holder, max_active) ->
TickReport` async. On first tick: sweep mutex AND sweep dispatcher slots
(both expired-by-TTL and dead-by-running_agents). Query
`list_automation_candidates(db, project_id)`; for each candidate, acquire
per-task mutex and **re-evaluate under lock** (closes per-task TOCTOU).

**Slot reservation (F11).** The cap check is no longer
"count then spawn"; it is `try_reserve_slot(db, project_id, max_active,
agent_run_id, task_id, ttl)` — atomic. If the call returns False the
slot is full this tick; the rule action becomes `Skip(reason="cap")` and
the tick records it. Slot release is wired to claim/end_agent_run
handlers (same pattern as per-task mutex), so concurrent ticks observing
the same free capacity cannot both spawn beyond cap, even on disjoint
candidates.

Dispatch each action via `_dispatch(db, task, action, agent_run_id,
report)`. Persist tick reports to `~/.gobby/logs/dispatcher.jsonl`.

Reuse existing infrastructure: `execute_spawn`,
`start_expansion_run_impl` (see D6), `WorktreeIsolationHandler`,
`CloneIsolationHandler`, `escalate_task`, `append_description_section`,
`set_artifacts_atomic`.

### D5. `src/gobby/dispatch/cron_registration.py` (plan §1.10)

`register_state_dispatcher(executor, db, config)` and
`ensure_state_dispatcher_cron_row(db, project_id)`. Call from
`src/gobby/runner_init.py` after `CronExecutor` is built and before
`CronScheduler.start()`. Cron row: id `state-dispatcher-main`, schedule
`60s`, action `handler:state-dispatcher`.

### D6. Expose `start_expansion_run_impl` (plan §2.8b)

Lift the existing MCP tool handler into a callable
`start_expansion_run_impl(...)` exposed from
`src/gobby/mcp_proxy/tools/tasks_ops.py` so the dispatcher can call it
in-process under the mutex. Return value must include `run.id` even on
compile failure.

### D7. Replace stub `_kick_dispatcher_tick`

Now actually invoke `run_tick` (single tick, in-process) and report dispatched
count. The Phase B5 honesty boundary becomes redundant once this is wired.

### D8. Tests

- `tests/dispatch/test_mutex.py` — acquire/release/expiry/sweep/force.
- `tests/dispatch/test_rules.py` — table-driven per-rule tests covering all
  branches and the yolo-never-escalates contract.
- `tests/dispatch/test_dispatcher.py` — end-to-end tick with mocked
  `execute_spawn`, TOCTOU re-evaluation under lock, JSONL
  persistence.
- `tests/dispatch/test_dispatcher_concurrent.py` — **F11 closure**:
  two concurrent `run_tick(project_id, holder, max_active=N)` calls on
  disjoint candidate sets, with N-1 slots already held, must spawn
  exactly one further task between them (not two). N concurrent ticks
  with cap=N and 0 held: exactly N spawn, the rest record `Skip("cap")`.
  Stale slot recovery: a dead `agent_run_id` is reaped before its slot
  blocks new spawns.
- `tests/dispatch/test_cron_registration.py` — handler registered, cron row
  idempotent.

---

## Phase E — Agents and Skills (plan §2.1–§2.10)

Land after D so dispatcher rules can spawn the agents and the agents can write
back through C's lifecycle tools.

| Section | Deliverable | File(s) |
| --- | --- | --- |
| §2.1 | Holistic-review skill (4-point method, verdict block, decision mapping) | `src/gobby/install/shared/skills/holistic-review/SKILL.md` |
| §2.2 | Holistic-reviewer agent | `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml` |
| §2.3 | Test-architect prose update (R4.F4) | `src/gobby/install/shared/workflows/agents/test-architect.yaml` + skill |
| §2.4 | qa-reviewer `close_task` permission | `qa-reviewer.yaml` allowed_mcp_tools |
| §2.5 | Frontend-developer agent | `frontend-developer.yaml` |
| §2.6 | Backend-developer agent (default fallback) | `backend-developer.yaml` |
| §2.7 | Planner clears `planning-current-verdict:rejected` on resubmit (R2.F1) | `planner.yaml` submit step |
| §2.8 | Expansion: stage-driven tree + Agent Selection step | `src/gobby/tasks/expansion_service.py`, `expansion.py`, prompts |
| §2.8a | Expansion-agent-selection skill | `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md` |
| §2.9 | Expansion-QA transition contract | `expansion-qa.yaml` (also touched by A5) |
| §2.10 | Merge agent lifecycle integration (yolo fallback, isolation cleanup) | `merge.yaml` |

Each agent/skill change ships with the per-section tests called out in the
plan's requirement matrix. Phase E's holistic-review wiring **consumes** the
A6 evidence library; it does not re-implement coverage logic.

---

## Phase F — Retire Old Surfaces (plan §4.1–§4.4)

### F1. Delete `src/gobby/conductor/`

Verify no `from gobby.conductor` or `import gobby.conductor` remains. Cron
registration moved to D5.

### F2. Tombstone obsolete pipelines

Replace contents in place (not delete) for `orchestrator.yaml`,
`front-half-orchestrator.yaml`, `conductor.yaml`,
`dev-orchestrator.yaml`, `delivery-orchestrator.yaml`. Add `deprecated` field
to `src/gobby/workflows/definitions.py` if missing (advisory only).

### F3. Tombstone obsolete agents

`conductor.yaml`, `developer.yaml`. Audit `pipeline-worker.yaml` —
tombstone if unreferenced.

### F4. Migration to disable retired DB rows

In `src/gobby/storage/migrations.py`, add a migration that flips
`enabled=false` on installed rows for the names in F2/F3. Preserves rows for
drift detection.

---

## Phase G — Documentation (plan §4.5)

Update `CLAUDE.md`, `GUIDING_PRINCIPLES.md`, and `docs/` to:

- Document the new dispatch architecture (point to `src/gobby/dispatch/`,
  describe how to add a rule, document `allow_automation`/`yolo`/`isolation`/
  stage-skip model).
- Make the profiles-as-CLI-sugar distinction explicit.
- Document `task_dispatch_mutex`, `task_artifacts`, `task_lifecycle_events`
  adjacency.
- Reference retired pipelines and link to #12728 for deferred PR/merge work.
- Remove all conductor references.

---

## Critical Files

**Reused (already landed; D0 audits the assumptions):**

- `src/gobby/storage/tasks/_dispatch_mutex.py`, `_artifacts.py`,
  `_lifecycle_events.py` — storage foundation.
- `src/gobby/config/build.py` — `BuildConfig`, `load_build_config`,
  `resolve_profile`, profile presets.
- `src/gobby/storage/migrations.py` — schema + new migrations in D0 (if
  needed for `plan_file_hash`) and F4.
- `src/gobby/storage/baseline_schema.sql` — fresh-install baseline.

**To create:**

- `src/gobby/plans/parser.py`, `coverage.py` — A2/A4 library.
- `src/gobby/cli/plan.py` — `gobby plan coverage` CLI.
- `src/gobby/storage/tasks/_dispatch_slots.py` — F11 slot reservation
  primitive.
- `src/gobby/dispatch/{mutex,actions,rules,dispatcher,cron_registration}.py`
- `src/gobby/install/shared/skills/{holistic-review,expansion-agent-selection}/SKILL.md`
- `src/gobby/install/shared/workflows/agents/{holistic-reviewer,frontend-developer,backend-developer}.yaml`
- `.gobby/plans/coverage/<project_id>/12725/task-12725-lifecycle-dispatch.coverage.yaml`
  (A7 manifest, computed via `coverage_manifest_path`).
- `.gobby/plans/task-NEW-plan-coverage-contract.coverage-ledger.yaml` (A8).
- `tests/plans/test_parser.py`, `test_coverage.py`,
  `test_plan_coverage_ci.py`.
- `tests/build_pipeline/test_entry_point_parity.py`.
- `tests/storage/tasks/test_dispatch_mutex_runid.py`,
  `test_dispatch_mutex_sweep.py`, `test_dispatch_slots.py`,
  `test_artifacts_cascade.py`, `test_lifecycle_events_cascade.py`,
  `test_candidates.py`, `test_artifacts_plan_path.py`,
  `test_artifacts_isolation_base.py`.
- `tests/storage/test_migration_upgrade.py`.
- `tests/hooks/test_task_dispatch_mutex_handlers.py`,
  `test_task_events.py`.
- `tests/dispatch/test_{mutex,rules,dispatcher,dispatcher_concurrent,cron_registration}.py`.

**To modify:**

- `src/gobby/build/service.py`, `src/gobby/cli/build.py`,
  `src/gobby/mcp_proxy/tools/build.py`, `src/gobby/servers/routes/build.py`.
- `src/gobby/storage/tasks/_transitions.py`, `_crud.py` (cascade helpers).
- `src/gobby/mcp_proxy/tools/tasks_ops.py` (`advance_lifecycle`,
  `start_expansion_run_impl` export).
- `src/gobby/hooks/event_handlers/_task.py` (mutex-clearing handlers).
- `src/gobby/runner_init.py` (cron registration).
- `src/gobby/install/shared/workflows/agents/{planner,qa-reviewer,test-architect,expansion-qa,merge}.yaml`.
- `src/gobby/install/shared/skills/plan/SKILL.md` (A1 grammar).
- All five tombstone-target pipelines and two tombstone-target agents.
- `CLAUDE.md`, `GUIDING_PRINCIPLES.md`, `docs/`.

**To delete:**

- `src/gobby/conductor/` (entire package).

---

## Verification

End-to-end acceptance, run after Phase G:

1. **Plan-coverage contract** (`tests/plans/test_plan_coverage_ci.py`)
   passes: every active plan under `.gobby/plans/` has a manifest with
   matching plan-hash and zero `missing | invalid` rows.
2. **#12725 compliance manifest** at the path returned by
   `coverage_manifest_path(<project_id>, "12725",
   "task-12725-lifecycle-dispatch")` has zero non-`covered | deferred`
   rows; all named symbols and tests verified.
3. **Bootstrap ledger validated**
   (`.gobby/plans/task-NEW-plan-coverage-contract.coverage-ledger.yaml`)
   matches the post-implementation manifest produced by the new tooling.
4. **Cross-surface parity** (`tests/build_pipeline/test_entry_point_parity.py`):
   driving a `gobby build <plan_file>` flow through CLI, MCP, and HTTP yields
   identical `BuildResult` and identical DB state.
5. **Storage-foundation audit** (Phase D0 test files) all green, including
   `tests/storage/tasks/test_dispatch_slots.py` for F11.
6. **Dispatcher tick** (`tests/dispatch/test_dispatcher.py`): a primed epic
   advances through `plan_review → test_arch → expanding → in_development →
   holistic_review → pr → merging → merged` across simulated ticks with
   mocked agent spawns.
7. **Concurrent dispatcher ticks (F11)**:
   `tests/dispatch/test_dispatcher_concurrent.py` proves two concurrent
   `run_tick` calls under cap=N with N-1 slots held spawn exactly one
   further task across both ticks combined; N concurrent ticks with cap=N
   spawn exactly N; stale slot recovery reaps dead `agent_run_id` before
   their slots block.
8. **Section-ID grammar (F8/F12)**: `tests/plans/test_parser.py`
   matches every fixture in **both** bare and titled forms — `### 1.1a`,
   `## A10`, `## §1.7 Decision rules`, `### D0.8 Dispatcher slot
   reservation primitive (F11)`, etc. — plus the negative case for a
   framing heading without a section ID. End-of-line bare headings parse
   correctly (the regex's `|$` lookahead is exercised).
9. **Coverage scope (F9/F13/F14/F16)**: `gobby plan coverage
   --root-task <ref> --plan-id <id> --project-id <id> --task-tree db`
   ignores leaves outside the named subtree; the library rejects
   `db`/`jsonl` calls that omit any of the three scope inputs at
   type/test time. Multi-root reuse: the same plan under two distinct
   root tasks produces two manifests at the paths returned by
   `coverage_manifest_path(...)` for each `(project_id, root_task_ref,
   plan_id)`. A9 CI rejects orphan manifests, stale `plan_hash`, and
   un-paired `.grandfathered` additions. The `coverage_manifest_path`
   helper sanitizes every component portably; duplicate-identity
   manifests are rejected unless explicitly regenerated.
10. **Lifecycle rejection paths**: holistic rejection with
    `cited_subtasks=[A, B]` reopens A and B and rewinds to `in_development`;
    expansion rejection clears `expansion_run_id` and increments attempts;
    merging rejection (yolo) increments `merge-attempts:N` and re-dispatches.
11. **Yolo evidence path (F10/F15/F17)**: a yolo run with
    worktree-local commits not yet linked to the task passes A6 via
    `worktree-diff:<artifact-ref>`, diffing against the immutable
    `base_commit_sha` captured at worktree creation. A run whose
    artifact has a missing or unresolvable `base_commit_sha` (including
    legacy rows from before D0's migration) produces an `invalid`
    evidence row citing the artifact and naming a repair path (not
    silent no-evidence). A yolo run missing evidence on any
    deliverable acceptance item yields `request_changes` (never
    `escalate`). Upgrade test from a fixture DB with legacy isolation
    rows (lacking `base_commit_sha`): migration preserves the rows;
    new isolations capture a base; A6 returns `invalid` for legacy
    and `covered` for fresh.
12. **Yolo fallbacks**: on `merge-attempts` cap, append_description_section
    is written under `## Yolo Fallbacks` and lifecycle advances to `merged`
    with isolation pair preserved.
13. **Manual smoke**: in a sandbox project,
    `uv run gobby build <plan>.md --profile full-yolo` produces a primed
    epic whose first tick dispatches the first non-skipped agent, visible
    in `~/.gobby/logs/dispatcher.jsonl`.
14. **Conductor removed**: `grep -r 'gobby.conductor' src/ tests/` returns
    no matches.
15. **Tombstones**: `gobby pipelines list` shows orchestrator family with
    `[DEPRECATED]` prefix and `enabled=false`.
16. **Coverage**: `uv run pytest tests/plans/ tests/dispatch/
    tests/build_pipeline/ tests/storage/tasks/ --cov=gobby
    --cov-report=term-missing` meets the project's 80% threshold for new
    modules.

---

## Process Fix Embedded in This Plan

The original epic failed silently because **the expansion → expansion-qa
contract has no input-coverage requirement and no plan-section provenance**.
Phase A is the structural fix:

- **A1**: typed plan grammar with section IDs, `kind` (incl. `verification`
  and `deferred`), and acceptance items at item-ID granularity.
- **A2**: a parser library with fixture tests pinning #12725's actual
  heading shapes; `source_hash` flows through every artifact.
- **A3**: typed deferral and structured `covers` records, so coverage is
  unspoofable.
- **A4**: a deterministic `gobby plan coverage` library/CLI consumed by
  expansion-qa, holistic-review, and CI.
- **A5**: expansion-qa rejects on missing acceptance items (mechanical, not
  LLM-judged).
- **A6**: evidence-based holistic-review gate that works with commits,
  task-diffs, or matrix files — not just PR diffs. Yolo never escalates
  for missing evidence; it requests changes.
- **A7**: #12725 retro-conformed and gets its compliance manifest as the
  acceptance checklist for Phases B–G.
- **A8**: a hand-authored bootstrap coverage ledger protects Epic 1's own
  plan, adversary-reviewed before implementation and re-validated by the
  new tooling before Epic 1 closes. No blanket grandfathering.
- **A9**: CI uses the same library as expansion-qa via committed manifests
  and source-hash gating; new `.grandfathered` entries require a paired
  removal task.
- **A10**: `CLAUDE.md` and skill READMEs document the contract so it is
  discoverable, not folklore.

Phase A ships as its own precursor epic. B–G run under the new contract.
Phase D0 audits the storage foundation that B–G build on **and** ships
the F11 slot-reservation primitive that the dispatcher consumes, so
concurrent ticks cannot exceed `max_active`. Without A and D0, the next
plan-driven epic fails the same way #12725 did and the dispatcher inherits
both schema-level uncertainty and a concurrency bug.
