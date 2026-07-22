# Feedback-Lesson Loop: Generalizing Review-Learning (#18598)

Research spike output for task #18598. Design recommendation for generalizing the
`gobby-review-learning` record / recall / promote loop from code review to every
feedback-bearing agent surface. Implementation is decomposed into tasks T1–T8
under the "Feedback-lesson loop: generalize review-learning" epic.

## Motivating evidence

- **The 51-round plan review** (#18430, `.gobby/plans/dream-stale-memory-reconciliation.md`):
  ~50 adversarial review rounds on a ~13-leaf-task plan in which no agent
  improved across rounds. Two distinct lesson classes were observed:
  1. **Reviewer misses** — a defect present in the reviewed text for multiple
     rounds before being flagged (e.g. R48 export fail-visibly rule vs R50
     remediation claim coexisted unflagged for 2 rounds). Primes the *reviewer*.
  2. **Fixer-induced defects** — a revision to rule A contradicting standing
     rules that reference A's old behavior. Dominant class for 10+ consecutive
     rounds. Primes the *fixer/reviser*.
- **The CodeRabbit loop works** (Josh, 2026-07-22): proven external feedback,
  concrete file anchors, compact relevance-scoped recall, recorded by whoever
  confirms. That contract is the bar for every new channel; this design
  generalizes the contract, not just the storage.
- **This spike's own review history is corroborating evidence**: the design
  below went through nine adversarial review rounds, and rounds 7–9 found
  almost exclusively follow-through defects in earlier rounds' fixes — the
  fixer-induced class, live, in the process that designed its own remedy.

## Hard constraints

- Review lessons are project-scoped (`include_global=False` everywhere; fail
  clearly when no project resolves — never fall back to global scope).
- Guardrail promotion keeps its actionable-signal gate (non-empty `prevention`,
  `principle`/`root_cause`, implementation anchor).
- Purely additive to the existing subsystem: legacy lessons and the CodeRabbit
  consumer contract are unchanged.

## Design

### 1. Taxonomy

- `source_kind` gains **`plan_review`** and **`task_validation`**
  (`src/gobby/review_learning/lessons.py`). Neither joins `CI_SOURCE_KINDS`;
  their proof discipline is embedded in the recording rules below.
- **`lesson-domain:<code|plan>` tag, required on every new lesson**, derived at
  record time from a **total** source_kind→domain map validated in `record()`.
  An unmapped source_kind is a hard error — the fail-closed point. Legacy
  untagged lessons are code-domain by construction.
- **Lesson classes ride `lesson_type`** with reserved canonical values:
  `reviewer-miss`, `fixer-induced-defect`, `recurring-validation-failure`,
  `qa-miss`, `validation-miss`. No new `audience`/`stage` fields —
  `(source-kind, lesson-type)` tag pairs answer every recall in this design.
- **Class-namespaced identity for every multi-class recorder** (plan loop and
  epic QA can mint two classes from one finding): class-scoped `pattern_id`
  (`plan-review:<lesson_type>:<category>:<check-key>`,
  `epic-qa:<lesson_type>:<check-key>`) and class-scoped fingerprint. Without
  it, the second class dies as `duplicate_occurrence` and promotion counts mix.
- **`check_key`** is an explicit validated kebab-case field naming the check
  that would have caught the defect — never derived from principle wording.
  Convergence mechanism: deterministic **`list_check_keys(lesson_domain,
  lesson_type[, category])`** enumerates the complete key set (from explicit
  `check-key:` tags — `pattern:` tags hash long identities and cannot serve
  enumeration); recorders must consult and reuse before minting; the skill
  seeds a canonical starter list. A versioned key/alias catalog is the
  documented escalation if fragmentation is observed despite enumeration.
- **Plan-lesson anchor**: namespaced `pattern_id` + synthetic
  `finding.rule_id = "plan-review:<adversary-category>"` (satisfies the
  promotion anchor gate). Never plan-file paths.

### 2. Recording discipline

Ground rule: **a rejection is never a lesson; a rejection whose fix was
subsequently confirmed by the same channel is.** The party holding cross-round
ground truth records — the reviser/coordinator side, never the reviewed agent
grading itself.

**Evidence machinery (plan flows).** Every review round captures an immutable
pre-review snapshot via the side-effecting gobby-plans tool
**`prepare_plan_review_round(plan_path)`** → `{evidence_id, plan_hash,
sections: [{section_id, section_hash}]}`, persisted in a daemon-owned
PostgreSQL **`plan_review_evidence`** table (project-scoped, immutable bytes,
finalized after approval/mint, crash-recoverable, orphan cleanup for taskless
rounds via finalize-or-expire on the next preparation for the same plan). The
adversary reviews the captured payload — never a fresh read of the live path
(TOCTOU guard). All hashes are pre-M1; the coordinator applies `## M1 Task
Manifest` only when the live hash still equals the captured hash (stale-write
guard). Agent-generated hash strings prove nothing; only tool-computed evidence
counts.

**Per-class evidence bundles** (a single section anchor misclassifies the
dominant cross-section contradiction — rule A changed, standing reference B
unchanged, finding points at B):

- *Reviewer-miss*: every participating `evidence_section_id` unchanged since an
  earlier reviewed round.
- *Fixer-induced*: causal changed-section IDs + before/after hashes + causal
  prior `finding_id` + introduction round.
- Dual-class minting requires both complete bundles. Unproven classification
  mints **nothing**.

**Per surface:**

| Surface | Recorder & proof | source_kind / lesson_type |
|---|---|---|
| Plan loop, interactive | `/gobby plan` coordinator at final approval, using changelog round evidence (`evidence_id` + hashes persisted with each round result). Cap ≤5/plan with class-aware selection. | `plan_review` / `reviewer-miss`, `fixer-induced-defect` |
| Plan loop, stage-native (phase 2) | Deterministic: `reject_review` gains a structured `findings` param (rendered + canonical JSON fence per round in `task.description`); idempotent `mint_plan_review_lessons(task_id, stage)` awaited at `approve_review`, re-callable as backfill. Approval response returns `lesson_mint_status`; the reviewer workflow attempts backfill once before `end_agent_run` on failure. | same |
| Validation gate | Prerequisite: structured `issues: list[Issue]` end-to-end (`validate.md` contract → `ValidationResult` → `_validation_result_from_data` → `_record_validation_iteration`) — the live path stores none today, so recurrence detection never fires. On a successful close with recurrence at the **configured** `recurring_issue_threshold` (default 3), the close response carries recurring candidates + passing evidence; the dev agent records then (one lesson/task). No auto-mint — templated lessons lack real principle/prevention. | `task_validation` / `recurring-validation-failure` |
| Epic QA | epic-review skill at confirm point with an explicit finding/confirmation schema (check_key, class, principle, prevention, anchor, confirmed-fix evidence); two classes, class-scoped identity. | `qa_rejection` / `qa-miss`, `validation-miss` |
| Plan enhancer | Never records (accepted suggestion ≠ proven defect); consumes fixer-induced lessons. | — |

**Complete surface matrix** (one disposition per surface):

| Surface | Disposition |
|---|---|
| CodeRabbit, code-reviewer, qa-reviewer, nightly-linter, nightly-test-fixer, trajectory-monitor | Existing loop — unchanged |
| Plan adversary, planner, interactive coordinator | Implementation (T3, T6) |
| Validation gate | Implementation (T4) |
| Epic QA | Implementation (T5) |
| Plan enhancer | Intentionally non-learning producer; consumer via injection (T2) |
| Expansion QA | Deferred — free-text reject notes (epic QA uses cited-subtask `fail_stage`, different plumbing); no recurrence evidence; needs its own finding schema first |
| Doc reviewer | Deferred — ordinary code-domain `qa_rejection` with file anchors; adopts the qa-reviewer contract verbatim when wanted |
| PR verdicts | Deferred — weakest evidence; structured verdicts already persisted in delivery state |
| Human corrections | Deferred — no domain contract yet (corrections span plan and code; was the leak vector) |

### 3. Recall / injection

- **Cross-domain leak fix**: deterministic domain partition on every
  code-domain recall path — the semantic review-lesson pass in
  `_search_recall_matches` and both file-recall query paths in
  `_candidate_lesson_memories` gain `tags_none=["lesson-domain:plan"]`, with
  the exclusion set centralized as a constant in `lessons.py`. Plan-domain
  callers opt in positively (`tags_all=["lesson-domain:plan"]`). Semantic
  ranking is never the safety mechanism.
- **New deterministic tool** `recall_review_lessons_by_class(lesson_domain,
  lesson_types, source_kinds=None, limit=3 clamp [1,5])` — `lesson_domain`
  required; source kinds must map to the domain; pattern-dedupe before the cap;
  deterministic ranking with full tie-breaks (occurrence desc, created_at desc,
  memory_id asc); output via `format_review_lesson_guidance` with a
  parameterized scope label.
- **Push injection rules** on `turn_start` (first `before_agent` expands to it
  for prompt-facing context), `agent_scope` with exact slugs:
  plan-adversary(+taskless) ← reviewer-miss; planner ← fixer-induced;
  plan-enhancer(+taskless) ← fixer-induced; qa-reviewer ← qa-miss. Formatter
  routing reuses `_format_review_lessons_result` (dedupe via
  `injected_review_lesson_ids`). Push beats pull — 51 rounds prove agents
  don't self-improve on request.
- **Interactive coordinator** (no `_agent_type`; rules can't reach it): the
  plan skill mandates fixer-induced recall before every coordinator revision.
- **Validator prompt**: `validate_leaf_task_with_llm` fetches `validation-miss`
  lessons (limit 3) into an empty-safe `lessons_section` slot in
  `validation/validate.md` — the validator learns from epic QA's catches.

### 4. Noise control

- Recall caps: class recall 3 [1,5]; file/semantic caps unchanged.
- Mint caps: ≤5 per plan approval with class-aware selection (≥1 slot per
  present class; reviewer-miss ranked by rounds missed, fixer-induced by causal
  occurrences then severity; deterministic tie-breaks); 1 per validation task;
  epic per confirmed finding.
- Promotion: **no promotion.py change** — `_confirmed_target` already honors
  explicit `guardrail_target` at occurrence 2+. Policy: plan classes →
  `checklist`; epic `qa-miss` → `checklist`; epic `validation-miss` →
  `validation`.
- **Retirement**: explicit `retire_review_lesson(pattern_id, evidence,
  session_id)` (overloading `record(decision="stale")` was rejected — that path
  early-returns before scope resolution). Retags `confirmed`→`stale` under the
  advisory lock, returns affected memory IDs, surfaces open `Guardrail:` task
  refs without auto-closing. No time-based pruning — the evidence shows
  under-recording, not accumulation.

### 5. Coexistence

Purely additive. Legacy lessons stay code-domain and behave exactly as today.
Regression: with plan lessons present, code-finding `recall_review_context`
returns zero `lesson-domain:plan` matches, including a plan lesson recorded
with a deliberately colliding file path.

## Deferred (with justification)

- **PR-verdict recording** — weakest evidence; `structured_pr_verdict` already
  persisted; no observed repeat-failure loop.
- **`human_correction` source kind** — corrections span plan and code; needs a
  domain contract before it can exist without reopening the leak.
- **`audience`/`stage` record fields** — redundant with (source-kind,
  lesson-type) tag pairs.
- **Versioned check-key catalog / canonicalization gating** — enumeration +
  mandatory reuse is the convergence mechanism; a canonicalization gate would
  block the legitimate second occurrence of a genuinely novel defect class.
  Catalog is the escalation if fragmentation is observed.
- **Time-based lesson pruning** — under-recording, not accumulation, is the
  observed failure; `retire_review_lesson` is the correction valve.
- **Expansion-QA / doc-reviewer recording** — see surface matrix.

## Adversarial review history (nine rounds, codex, 2026-07-22)

All blockers accepted except one (noted). The round-over-round pattern —
rounds 7–9 finding defects introduced by rounds 1–6's fixes — is itself the
fixer-induced-defect class this design records.

1. **R1**: fail-open partition → `lesson-domain` tag; durable round-proof
   requirement; coordinator recall gap; dual-class identity collision;
   validation recording moved to the success path; mint durability; retirement
   redesigned as an explicit operation.
2. **R2**: check-key identity granularity; per-section hashes over whole-plan
   hashes; `turn_start` event selection; file-recall partition; mint-backfill
   route; memory payload correction.
3. **R3**: full finding/attestation schema with validated check_key; trusted
   evidence producer (LLM-supplied hashes rejected); structured validation
   issues (live path stored none — recurrence detection never fired); backfill
   actor contract.
4. **R4**: required `lesson_domain` on class recall; per-class evidence bundles
   for cross-section contradictions; named evidence tool + capture timing;
   structured issues end-to-end; workflow-owned backfill; guardrail-target
   policy for both plan classes.
5. **R5**: capture-before-spawn self-invalidation guard; enhancer injection
   coverage; check_key definition reconciliation; class-aware mint selection.
6. **R6**: conflict-free task dependency graph; configured recurrence
   thresholds (no hardcoded counts); deterministic ranking tie-breaks.
7. **R7**: class-scoped identity generalized to all multi-class recorders
   (epic QA had repeated the R1 bug); atomic snapshot closing the TOCTOU gap;
   check-key stability contract; complete surface matrix.
8. **R8**: epic finding/confirmation schema; per-class epic guardrail targets;
   deterministic `list_check_keys` (**declined**: novel-key canonicalization
   gate — occurrence 1 is memory-only and enumeration auto-registers keys);
   pre-M1 hash semantics + stale-write guard + `prepare_plan_review_round`
   rename.
9. **R9**: explicit `check-key:` tags (pattern tags hash long identities);
   `list_check_keys` moved to T2 (T5 dependency order); `plan_review_evidence`
   PostgreSQL snapshot store with full lifecycle.

## Implementation tasks

Created under the "Feedback-lesson loop: generalize review-learning" epic.
Dependency graph (no two parallel-eligible tasks edit the same file):
T1→{T2,T4}; T2→T3; {T2,T4}→T5; T3→{T6,T8}; {T5,T6}→T7.

- **T1** Taxonomy + domain partition (`lessons.py`, `service.py`)
- **T2** Class recall tool + `list_check_keys` + injection wiring
- **T3** Plan-review evidence (`prepare_plan_review_round`, `plan_review_evidence`) + interactive plan-loop skills
- **T4** Validation-gate structured issues + success-path recording
- **T5** Epic QA two-class recording + validator prompt injection
- **T6** Stage-native structured findings + deterministic mint with backfill
- **T7** E2E + coexistence regression
- **T8** `retire_review_lesson` operation
