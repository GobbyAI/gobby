# Feedback-Lesson Loop: Generalize Review-Learning

**Plan ID:** feedback-lesson-loop

## Overview
`kind: framing`

Generalize the `gobby-review-learning` record / recall / promote loop from code
review to every feedback-bearing agent surface: the plan adversary, the
planner/fixer, the interactive plan coordinator, the task-validation gate, and
epic QA. Design record: `docs/spikes/feedback-lesson-loop.md` (research spike
#18598, nine adversarial design-review rounds). Where the spike and this plan
disagree, **this plan is authoritative**: the spike's whole-plan-hash freshness
check, its finalize-after-approval/mint evidence lifecycle, and its singular
`evidence_section_id` bundles are superseded by the reviewed-section freshness
surface (3.1), the finalize-on-durable-round-result lifecycle (3.1), and the
`participating_section_ids`/`causal_section_ids` set bundles (3.2, 6.x).

Motivating evidence: a 51-round adversarial plan review (#18430) in which no
agent improved across rounds. Two lesson classes were observed — **reviewer
misses** (defect present in reviewed text for multiple rounds before being
flagged; primes the reviewer) and **fixer-induced defects** (a revision to rule
A contradicting standing rules referencing A's old behavior; dominant class for
10+ consecutive rounds; primes the fixer). The CodeRabbit lesson loop already
works in practice; its contract — proven feedback, concrete anchors, compact
relevance-scoped recall, recorded by whoever confirms — is the bar every new
channel must meet.

## Constraints
`kind: framing`

- Review lessons are project-scoped: `include_global=False` on every lesson
  search/list; fail clearly when no project resolves; never fall back to
  global/personal scope.
- Purely additive: legacy lessons, the CodeRabbit consumer/rule contract, and
  `recall_review_lessons_for_files`'s external behavior for code lessons are
  unchanged. No data migration.
- **Cross-domain leak protection is mandatory**: plan-loop lessons must never
  surface in code-review recall (CodeRabbit, code-reviewer, qa-reviewer) and
  vice versa. Deterministic tag partitioning is the safety mechanism — semantic
  ranking never is.
- Recording evidence bar: a rejection is never a lesson; a rejection whose fix
  was subsequently confirmed by the same channel is. The party holding
  cross-round ground truth records — never the reviewed agent grading itself.
  Unproven classification mints nothing.
- No `promotion.py` ladder changes: `_confirmed_target` already honors explicit
  `guardrail_target` at occurrence 2+; recorders supply per-class targets.
- Non-goals (deliberately out of scope, with rationale in the spike doc):
  PR-verdict recording (weakest evidence; verdicts already persisted),
  `human_correction` source kind (no domain contract yet — reopens the leak),
  expansion-QA and doc-reviewer recording (no recurrence evidence; doc-reviewer
  can adopt the existing qa-reviewer contract verbatim later), `audience`/
  `stage` record fields (redundant with source-kind + lesson-type tag pairs),
  time-based lesson pruning (observed failure is under-recording, not
  accumulation), and a versioned check-key catalog (escalation path only).

## P1: Taxonomy and Domain Partition
`kind: framing`

**Goal**: Every lesson carries a deterministic domain partition and class-scoped
identity so new feedback channels can record without leaking into or corrupting
existing code-review lessons.

### 1.1 Extend lesson taxonomy with plan/validation source kinds, lesson-domain tagging, and class-scoped identity [category: code]
`kind: deliverable`

Target: `src/gobby/review_learning/lessons.py`
Target: `src/gobby/review_learning/service.py`
Target: `tests/review_learning/test_lessons.py`
Target: `tests/review_learning/test_storage_contract.py`

Extend the closed enums and deterministic tag scheme in
`src/gobby/review_learning/lessons.py`:

- `SourceKind` / `VALID_SOURCE_KINDS` gain `plan_review` and `task_validation`.
  Neither joins `CI_SOURCE_KINDS` (their proof discipline lives in the
  recording contracts, not the verified-fix CI gate).
- New closed vocabulary `LessonDomain = Literal["code", "plan"]` and a **total**
  map `SOURCE_KIND_DOMAIN: dict[str, str]` covering every valid source kind
  (`plan_review` → `plan`; every other kind → `code`). `derive_lesson_domain()`
  raises `ValueError` on any source kind missing from the map — record is
  fail-closed; adding a source kind without a domain mapping is a hard error
  surfaced by tests.
- `build_tags()` emits on every lesson: required `lesson-domain:<domain>`,
  plus `check-key:<key>` and `category:<slug>` when the finding carries them
  (`pattern:` tags hash identities >52 chars via `pattern_key_for` and cannot
  serve key enumeration — the explicit tags exist for `list_check_keys`).
- Centralized constant `CODE_DOMAIN_EXCLUDED_TAGS = ("lesson-domain:plan",)` —
  the single place code-domain recall exclusion is defined.
- Reserved canonical `lesson_type` values documented in the module docstring:
  `reviewer-miss`, `fixer-induced-defect`, `recurring-validation-failure`,
  `qa-miss`, `validation-miss`. The field remains otherwise free-form.
- **Class-scoped identity for multi-class recorders**: `derive_lesson_identity`
  accepts explicit class-namespaced `pattern_id`s of the forms
  `plan-review:<lesson_type>:<category>:<check-key>` and
  `epic-qa:<lesson_type>:<check-key>`, where `<check-key>` is an explicit
  validated kebab-case field from the finding (`validate_check_key()` —
  syntax: `^[a-z0-9]+(-[a-z0-9]+)*$`; never derived from principle wording).
  Multi-class recorders also pass a class-scoped fingerprint so two classes
  minted from one finding hold distinct fingerprints and occurrence keys
  (today the second dies as `duplicate_occurrence` in
  `ReviewLearningService.record`). Generic fingerprint behavior is unchanged
  for single-class recorders. symbol: `gobby.review_learning.fingerprint`.
- `record()` in `service.py` derives and validates the domain, stamps the
  domain tag, and accepts the class-scoped identity fields.

**Acceptance:**

- 1.1.1 - `plan_review` and `task_validation` validate as source kinds and are
  absent from `CI_SOURCE_KINDS`. file: `src/gobby/review_learning/lessons.py`.
- 1.1.2 - `derive_lesson_domain` is total over `VALID_SOURCE_KINDS` and raises
  `ValueError` on an unmapped kind; a test enumerates the full enum against the
  map. test: `tests/review_learning/test_lessons.py::test_source_kind_domain_map_total`.
- 1.1.3 - Every recorded lesson carries `lesson-domain:<domain>`; findings with
  `check_key`/`category` emit `check-key:` and `category:` tags.
  test: `tests/review_learning/test_lessons.py::test_domain_and_check_key_tags`.
- 1.1.4 - Two classes minted from one finding via class-namespaced identity
  persist as two lessons with distinct pattern keys, fingerprints, and
  occurrence counts, for both `plan-review:` and `epic-qa:` namespaces.
  test: `tests/review_learning/test_lessons.py::test_dual_class_identity_separation`.
- 1.1.5 - `validate_check_key` accepts kebab-case slugs and rejects uppercase,
  whitespace, and empty values. symbol: `gobby.review_learning.lessons.validate_check_key`.
- 1.1.6 - Compatibility is **exact-plus-domain-tag**: persisted legacy rows are
  untouched (no data migration); existing record-call signatures, identities,
  fingerprints, and promotion inputs remain stable; a lesson newly recorded
  through an existing call site gains exactly `lesson-domain:code` on top of
  its previous tag set and nothing else. The existing `tests/review_learning/`
  suite passes with only the domain-tag assertion updated.
  behavior: "legacy record compatibility" in `tests/review_learning/test_storage_contract.py`.

### 1.2 Apply the domain partition to every code-domain recall path [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/review_learning/service.py`
Target: `tests/review_learning/test_recall_context.py`
Target: `tests/review_learning/test_file_paths.py`

Apply `CODE_DOMAIN_EXCLUDED_TAGS` as `tags_none` on every code-domain recall
path:

- `_search_recall_matches` (semantic review-lesson pass in
  `recall_review_context`): the `tags_all=["review-lesson"]` pass adds
  `tags_none=list(CODE_DOMAIN_EXCLUDED_TAGS)`. The non-lesson pass already
  excludes `review-lesson` and is unchanged.
- `_candidate_lesson_memories` (file recall): both the tagged fast path and the
  legacy content-scan fallback add the same `tags_none`. Nothing enforces
  plan-lesson pathlessness, so file recall needs the filter as defense in
  depth.
- `recall_review_context` stays code-domain-only: no `lesson_domain`
  parameter. Every named plan consumer in this plan uses
  `recall_review_lessons_by_class`; a semantic plan-recall branch returns
  only with a named consumer. No implicit cross-domain recall exists.

**Acceptance:**

- 1.2.1 - With plan-domain lessons present, code-domain
  `recall_review_context` returns zero matches tagged `lesson-domain:plan`.
  test: `tests/review_learning/test_recall_context.py::test_code_domain_excludes_plan_lessons`.
- 1.2.2 - A plan-domain lesson recorded with a deliberately colliding code file
  path never surfaces from `recall_review_lessons_for_files` on either the
  tagged fast path or the legacy content-scan path.
  test: `tests/review_learning/test_file_paths.py::test_plan_lesson_colliding_path_excluded`.
- 1.2.3 - The exclusion set is read from the single
  `CODE_DOMAIN_EXCLUDED_TAGS` constant at every call site (no inline string
  literals). symbol: `gobby.review_learning.lessons.CODE_DOMAIN_EXCLUDED_TAGS`.

## P2: Class Recall and Push Injection
`kind: framing`

**Goal**: Agents receive relevant lessons for their class of work
deterministically at spawn, without relying on remembering to ask.

### 2.1 Deterministic class recall tool and check-key enumeration [category: code] (depends: 1.2)
`kind: deliverable`

Target: `src/gobby/review_learning/class_recall.py` (new)
Target: `src/gobby/review_learning/service.py`
Target: `src/gobby/mcp_proxy/tools/review_learning.py`
Target: `src/gobby/workflows/engine/effects.py`
Target: `src/gobby/review_learning/guidance.py`
Target: `tests/review_learning/test_class_recall.py` (new)
Target: `tests/workflows/test_review_learning_rules.py`

New module `src/gobby/review_learning/class_recall.py` (keeps `service.py`
under the size cap) with two operations, both delegated from
`ReviewLearningService` and registered on the `gobby-review-learning` MCP
registry:

```python
async def recall_review_lessons_by_class(
    lesson_domain: str,            # REQUIRED — free-form lesson_type would
    lesson_types: list[str],       # cross-pollinate across domains without it
    source_kinds: list[str] | None = None,
    limit: int = 3,                # clamped [1, 5]
) -> dict[str, Any]: ...

async def list_check_keys(
    lesson_domain: str,
    lesson_type: str,
    category: str | None = None,
) -> dict[str, Any]: ...
```

- Predicate: `tags_all=["review-lesson", "confirmed",
  f"lesson-domain:{lesson_domain}"]` AND `lesson_type ∈ lesson_types` AND
  (`source_kinds is None` OR `source_kind ∈ source_kinds`). Lists OR
  internally, conditions AND. Every supplied source kind must map to
  `lesson_domain` via `SOURCE_KIND_DOMAIN` (validation error otherwise).
- Pattern-dedupe **before** the cap (keep newest per pattern), then rank with
  full tie-breaks: occurrence count desc, `created_at` desc, `memory_id` asc.
- Output shape matches `recall_review_lessons_for_files` (`{count, lessons,
  message}`) via `format_review_lesson_guidance`, which gains a parameterized
  scope label ("matched lesson class" here; existing file-recall label
  unchanged).
- `list_check_keys` enumerates the **complete** distinct key set for the class
  from `check-key:` tags (project-scoped, deterministic pagination by key) —
  the identity-resolution surface recorders must consult before minting.
- Formatter routing: `_apply_effect` in `src/gobby/workflows/engine/effects.py`
  routes `("gobby-review-learning", "recall_review_lessons_by_class")` through
  `_format_review_lessons_result` (same result shape; `<review-guidance>`
  rendering and `injected_review_lesson_ids` dedupe come free).
- Project-scoped `include_global=False` throughout; fails closed on missing
  project.

**Acceptance:**

- 2.1.1 - `recall_review_lessons_by_class` rejects a missing/unknown
  `lesson_domain` and any source kind that maps to a different domain.
  test: `tests/review_learning/test_class_recall.py::test_domain_required_and_consistent`.
- 2.1.2 - The same `lesson_type` recorded in both domains recalls only the
  requested domain's lesson.
  test: `tests/review_learning/test_class_recall.py::test_cross_domain_same_lesson_type`.
- 2.1.3 - Pattern-dedupe precedes the cap and ranking is deterministic under
  input shuffling (occurrence desc, created_at desc, memory_id asc).
  test: `tests/review_learning/test_class_recall.py::test_dedupe_and_deterministic_ranking`.
- 2.1.4 - `list_check_keys` returns every distinct key for the class —
  including identities long enough that `pattern:` tags hash them — with
  project isolation and deterministic pagination.
  test: `tests/review_learning/test_class_recall.py::test_list_check_keys_completeness`.
- 2.1.5 - The effects routing renders class-recall results as
  `<review-guidance>` with the class scope label and id-dedupes across
  injections. test: `tests/workflows/test_review_learning_rules.py::test_class_recall_formatter_routing`.

### 2.2 Push injection rules for plan and QA agents [category: config] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml` (new)
Target: `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml` (new)
Target: `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml` (new)
Target: `src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml` (new)
Target: `tests/workflows/test_review_learning_rules.py`

Four bundled rules in `group: review-learning`, `event: turn_start` (the first
`before_agent` expands to `turn_start` for prompt-facing context; per-turn
refire is harmless via `injected_review_lesson_ids` dedupe), each an `mcp_call`
of `recall_review_lessons_by_class` with `background: false`,
`inject_result: true`, `limit: 3`, and `agent_scope` with **exact slugs**:

| Rule | agent_scope | lesson_domain | lesson_types |
|---|---|---|---|
| inject-plan-reviewer-lessons | `[plan-adversary, plan-adversary-taskless]` | plan | `[reviewer-miss]` |
| inject-planner-lessons | `[planner]` | plan | `[fixer-induced-defect]` |
| inject-plan-enhancer-lessons | `[plan-enhancer, plan-enhancer-taskless]` | plan | `[fixer-induced-defect]` |
| inject-qa-reviewer-lessons | `[qa-reviewer]` | code | `[qa-miss]` |

Enhancers consume fixer-induced lessons even though their suggestions never
record. The interactive coordinator has no `_agent_type` and is covered by the
skill contract in 3.2, not by rules.

**Acceptance:**

- 2.2.1 - inject-plan-reviewer-lessons loads with `group: review-learning`,
  `event: turn_start`, `agent_scope: [plan-adversary,
  plan-adversary-taskless]`, `lesson_domain: plan`, `lesson_types:
  [reviewer-miss]`, and `inject_result: true`. file:
  `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml`.
- 2.2.2 - inject-planner-lessons loads with the same group/event/injection
  contract, `agent_scope: [planner]`, `lesson_domain: plan`, `lesson_types:
  [fixer-induced-defect]`. file:
  `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml`.
- 2.2.3 - inject-plan-enhancer-lessons loads with the same
  group/event/injection contract, `agent_scope: [plan-enhancer,
  plan-enhancer-taskless]`, `lesson_domain: plan`, `lesson_types:
  [fixer-induced-defect]`. file:
  `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml`.
- 2.2.4 - inject-qa-reviewer-lessons loads with the same
  group/event/injection contract, `agent_scope: [qa-reviewer]`,
  `lesson_domain: code`, `lesson_types: [qa-miss]`. file:
  `src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml`.
- 2.2.5 - Scoped injection fires for each listed slug (including both
  taskless variants) and injects nothing for unscoped agent types, with empty
  and non-empty recall both exercised.
  test: `tests/workflows/test_review_learning_rules.py::test_class_injection_agent_scoping`.

## P3: Plan-Review Evidence and the Interactive Loop
`kind: framing`

**Goal**: Plan-review rounds produce trusted, immutable evidence, and the
interactive coordinator records proven lessons at final approval.

### 3.1 prepare_plan_review_round tool and plan_review_evidence store [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/plans/review_evidence.py` (new)
Target: `src/gobby/storage/postgres_baseline_schema.sql`
Target: `src/gobby/storage/migrations/337_plan_review_evidence.sql` (new)
Target: `src/gobby/mcp_proxy/tools/plans/__init__.py`
Target: `tests/plans/test_review_evidence.py` (new)

Trusted evidence producer for both plan flows. Agent-generated hash strings
prove nothing; hashes come from this tool or not at all.

- New daemon-owned table `plan_review_evidence`: `evidence_id` (uuid pk),
  `project_id`, `plan_path`, `plan_hash`, `section_manifest` (jsonb:
  `[{section_id, section_hash}]`), `snapshot` (immutable plan bytes),
  `round_number`, `created_at`, and the **lifecycle columns** `finalized_at`
  (nullable) and `expired_at` (nullable) with a CHECK that at most one is
  set — a row is exactly one of live-unfinalized, finalized, or expired,
  and both transitions are one-way. Attempt liveness for rows that have
  not yet reached a run bind — pre-bind rows on **either surface**; both
  surfaces bind (below) — is
  bounded by `lease_expires_at`, stamped at preparation as `created_at +
  EVIDENCE_LEASE_SECONDS` (module constant in `review_evidence.py`,
  default 7200) and cleared by a successful `bind_evidence_run`: a bound
  row's liveness is judged by its run's terminal state instead, never by
  the lease — an interactive adversary still reviewing past the lease
  window is live. Plus **attempt
  binding** columns: `session_id` (interactive parent session) and nullable
  `task_id`/`stage` (stage-native) populated at preparation — the stage-native
  attempt token is `(task_id, stage, round_number)`. `dispatch_run_id` is
  nullable and attached **after** spawn via `bind_evidence_run(evidence_id,
  run_id)` on **both surfaces** — the run id does not exist until the spawn
  returns, so preparation cannot carry it (two-phase binding): stage-native
  dispatch binds its dispatch run (6.1), and the interactive coordinator
  binds the run id `spawn_agent` returns for the taskless adversary (3.2).
  Spawn failure expires the
  just-prepared row. The row also carries: the **round-result payload**
  column `round_result` (jsonb — the canonical validated findings/verdict
  payload for the round: rejection rounds write it atomically with
  finalization (6.1), while approval rounds write it earlier, at manifest
  apply, as the **pre-finalization approval intent** — a non-null
  `round_result` on a still-unfinalized row, revocable only by 3.1.7's
  **pending-plus-drift invalidation transition**, which clears it back to
  null — so the complete verdict is
  durable before any V1 or finalization write; this durable payload,
  never a plan-file or task-description
  rendering, is the mint authority per 6.1/6.2); the **approval
  checkpoint** columns backing 6.2's fused approval commit and
  lost-response replay — `approval_result` (jsonb, the recorded approval
  response), `approved_at`, `lesson_mint_status`
  (`pending`/`minted`/`failed`/`none`; null until an approval commits on
  **either surface** — the stage-native fused approval commit stamps
  `pending` per 6.2, and interactive approval finalization stamps
  `pending` atomically with `finalized_at` on every path that finalizes
  an approval verdict: direct step 3, checkpoint reconciliation, and the
  intent drain), and `lesson_mint_detail` (jsonb: minted lesson ids or the
  recorder error). On interactive rounds both columns advance
  `pending`→`minted`/`failed`/`none` through the gobby-plans operation
  `checkpoint_plan_review_lesson_mint(evidence_id, status, detail)`,
  which accepts only finalized interactive approval rows and only that
  transition, and is idempotent at every terminal state (stage-native
  rounds advance through 6.2's backfill operation instead); and the **manifest-application
  checkpoint** columns backing 3.1.7's state machine: `manifest_digest`
  (canonical-payload digest), `manifest_payload` (jsonb, canonical form),
  `manifest_state` (`pending`/`applied`/`revoked`; null = never
  attempted),
  `manifest_result` (jsonb, the prior successful apply result), and
  `manifest_applied_at`. Every durable record this plan's lifecycle
  depends on — lease expiry, expiration, round results, approval replay,
  mint status — lives in these columns; no generic `task_stage_states`
  field is repurposed for any of them. Project-scoped.
  Schema ships through **both** existing paths: the next versioned migration
  (`337_plan_review_evidence.sql` — 335 and 336 are already taken by
  `335_memories_dream_due_version.sql` / `336_model_metadata_rename.sql`, and
  `MigrationRunner._discover_migrations` hard-errors on duplicate versions;
  applied by the existing migration runner to already-baselined databases)
  and the baseline schema for fresh bootstraps. No new schema path.
- Side-effecting gobby-plans MCP tool `prepare_plan_review_round(plan_path,
  round_number, ...binding)`: validates the path boundary first — normalized,
  project-contained, regular file; escaped or symlinked paths are rejected —
  then reads the plan **once**, computes the whole-plan hash and a canonical
  per-section hash manifest, inserts the row with its attempt binding, and
  returns `{evidence_id, plan_hash, sections}`. **Sectioning is the plan
  parser's fence-aware boundary model with total coverage** (symbol:
  `gobby.plans.parser` — every level-2..6 markdown heading outside fenced
  code blocks opens a section; `PLAN_HEADING_REGEX` alone is
  canonical-dotted-ID-only and must not define boundaries), and manifest
  identity is one exact key function `manifest_key(heading)`: a heading
  the parser recognizes as canonical keys by its dotted `section_id` —
  which includes `## M1 Task Manifest` → `M1` and `## V1 Plan Changelog`
  → `V1`, whose leading tokens match the dotted-ID grammar — while an
  ID-less heading (`## Task Mapping`, any other noncanonical heading)
  keys by normalized heading title; the reserved key `__preamble__`
  (unproducible by any heading) covers **every byte before the first
  level-2..6 heading** — the H1 title, the `**Plan ID:**` marker, and
  any other preamble bytes — so coverage is total: every snapshot byte
  belongs to exactly one manifest entry. Duplicate manifest keys —
  duplicate canonical IDs or duplicate normalized noncanonical titles,
  including a duplicated coordinator-owned heading — fail preparation
  with a deterministic error naming the key: identities are unique and
  order-independent, never occurrence-qualified. Hashing
  precedent symbol: `gobby.plans.coverage`. All hashes are defined over
  this **pre-review, pre-M1** snapshot.
- The adversary reviews the captured snapshot payload — never a fresh read of
  the live path (closes the TOCTOU gap; prompt transport reads the snapshot).
  Both flows call this tool: the interactive coordinator per 3.2, stage-native
  review dispatch per 6.1.
- **Evidence consumption has two modes.** *Current-attempt mutation
  authorization* (`reject_review` accepting a round's findings,
  `approve_review` consuming the approving round's evidence, and
  `apply_plan_review_manifest` — which the approval commit order runs
  **before** finalization, so it always operates on the still-unfinalized
  current row): resolves the referenced row and validates project, normalized
  plan path, round number, and the bound task/stage or parent session against
  the active attempt. While a stage-native row's `dispatch_run_id` is still
  null (bind pending), current-attempt mutations return a retryable
  `binding_pending` result — they never proceed unbound; once attached, the
  caller's run must match it. Already-finalized or expired rows presented as
  the current round's evidence (replay), wrong-plan, wrong-round, and
  unresolvable references are rejected — with one bounded exception: a
  repeat `reject_review` presenting the same evidence row under the same
  full attempt token `(task_id, stage, round_number, dispatch_run_id)` is
  answered as an **idempotent lost-response replay** (6.1): the recorded
  finalized rejection result for that token is returned read-only — no
  write, no re-finalization, recognized even though the atomic rejection
  commit already advanced the stage (no post-commit mutation window
  exists; changed findings require a new review round); cross-run,
  cross-round, and cross-plan references remain replay. *Historical proof
  reads* (lesson-mint classification over prior rounds): read-only
  resolution of **finalized** rows by `evidence_id`, validated for
  same-plan lineage — same project, same normalized plan path, and same
  task lineage (stage-native, regardless of dispatch run) or same parent
  session (interactive). Cross-plan and cross-lineage references are
  rejected; unfinalized rows are never proof (their round is incomplete).
- `finalize_plan_review_evidence(evidence_id, round_result)` writes the
  round's canonical result payload onto the row (a payload conflicting
  with a recorded pre-finalization approval intent is a deterministic
  error — finalization can only confirm the durable intent) and stamps
  `finalized_at`. For an **interactive** round, finalization first
  verifies the round's durable V1 checkpoint is present in the plan's
  changelog (the V1 checkpoint is the enforced durable step-2 marker —
  finalization refuses deterministically when the changelog lacks the
  round's checkpoint fence, so a finalized interactive round without its
  V1 entry cannot exist), and whenever the finalized payload is an
  approval verdict — on any finalization path: direct step 3, checkpoint
  reconciliation, or the intent drain — it stamps
  `lesson_mint_status=pending` in the same durable write, so an approved
  round whose lessons have not been minted always carries a durable
  marker. `finalized_at` is stamped
  **atomically with the round's durable result wherever both live in
  PostgreSQL**: the stage-native rejection fence and approval record commit
  in the **same transaction** as finalization (6.1, 6.2) — no crash window
  can leave a durable stage-native result pointing at an unfinalized row.
  The interactive changelog entry is a plan-file write and cannot join that
  transaction; the file/DB boundary is covered by a **reconciliation
  checkpoint** over a pinned wire format, the **V1 round checkpoint**:
  every interactive V1 round entry embeds one canonical fenced JSON block
  — rendered by the gobby-plans tool
  `render_v1_round_checkpoint(evidence_id, round_result=None)`, which
  validates
  the payload against the `round_result` schema — an omitted payload
  resolves from the row's durable approval intent, and a supplied payload
  conflicting with a recorded intent is a deterministic error, so the
  rendered bytes are a pure function of durable state after a process
  restart — and returns canonical
  bytes the coordinator persists verbatim — containing `{evidence_id,
  round_number, plan_hash, session_id, round_result}`, where
  `round_result` is the complete canonical payload (the same schema
  `finalize_plan_review_evidence` accepts: the full attestation findings
  and verdict, plus the typed manifest entries on approval); the prose
  bullets around it are a projection. Before any expiration,
  `prepare_plan_review_round` parses these checkpoints (parser counterpart
  in `review_evidence.py`; a malformed or schema-invalid checkpoint is a
  deterministic reconciliation error like any lineage mismatch) for
  entries referencing unfinalized
  evidence rows and finalizes them (a durable checkpoint *is* the round
  result — the crashed finalization is completed, never orphaned).
  Reconciliation is **lineage-validated** against the checkpoint's own
  fields: a referenced row is finalized
  only when it resolves in the same project, its normalized `plan_path`
  matches the plan being prepared, its `session_id` is the preparing
  parent session, its `round_number` equals the checkpoint's round, and
  the checkpoint's recorded `plan_hash` matches the server row;
  finalization here
  back-fills the row's `round_result` **losslessly** from the checkpoint
  so recovered
  interactive rounds remain mint-capable with their full attestations.
  A durable **pre-finalization approval intent** (the step-1 write in
  3.1.7's state machine) is reconciled the same way without any parsing —
  an interactive
  unfinalized row carrying an intent is completed or invalidated, never
  left wedged: with
  `manifest_state=applied` the preparation runs the **interactive
  approval recovery drain** — it renders the round's V1 checkpoint from
  the durable intent (`render_v1_round_checkpoint`, a pure function of
  durable state) and persists it into the changelog under the
  per-plan-path lock when absent (a coordinator-owned write, byte-equal
  to the rendered form), then finalizes the row losslessly from the
  intent, stamping `lesson_mint_status=pending`;
  with `manifest_state=pending` the apply first converges through its
  own state machine — a no-drift convergence checkpoints `applied` and
  the row then continues through the **same interactive approval
  recovery drain**: V1 checkpoint rendered from the durable intent,
  persisted under the per-plan-path lock, then a lossless finalize
  stamping `lesson_mint_status=pending` (never a bare finalize, which
  the V1 gate would refuse, wedging an unexpirable intent-carrying row) —
  and when that convergence finds
  reviewed-section drift, it executes the apply state machine's atomic
  **pending-plus-drift invalidation transition** (`manifest_state=revoked`
  + `round_result` cleared in one durable write, plan bytes untouched),
  revoking the stale intent and invalidating the round, so the row —
  now carrying no intent — is expirable under the standard predicate and
  the same preparation's orphan cleanup expires it once its attempt is
  provably dead, unblocking the next attempt's CAS. On
  any mismatch — stale,
  cross-plan, cross-session, wrong-round, or hash-divergent reference —
  the preparation itself fails with a deterministic reconciliation error
  naming the offending entry; nothing is finalized or expired while an
  unresolved mismatch exists. Only
  after reconciliation does orphan cleanup run, and it is **lease-guarded**
  (next bullet) — a row stays unfinalized-and-expirable only when no
  durable result references it and its attempt is provably dead.
  **Preparation is also the recovery owner for the mint tail**: while any
  same-lineage interactive approval row carries
  `lesson_mint_status=pending`, `prepare_plan_review_round` refuses to
  create or spawn a new round, returning the deterministic
  `pending_lesson_mint` result listing those rows and their durable
  `round_result` payloads; the coordinator completes the idempotent mint
  from those payloads and checkpoints `minted`/`failed`/`none` via
  `checkpoint_plan_review_lesson_mint` (the 3.2 drain contract), and only
  then does re-preparation proceed — so every incomplete interactive
  approval is drained before the next round exists, and an approved round
  can never be silently left unminted behind a newer one.
- **Preparation is serialized and lease-guarded** (the per-plan-path lock
  covers preparation, expiration, and manifest application — not manifest
  application alone). Bind-pending rows and rows owned by an actively
  reviewing agent are necessarily unfinalized, so unfinalized-row cleanup
  alone would let a duplicate dispatch or a concurrent
  interactive/stage-native preparation expire live evidence. Per-plan
  active-attempt CAS: `prepare_plan_review_round` with the **same attempt
  token** as an existing unfinalized row is idempotent — it returns the
  existing row (same-token retry, no new row, nothing expired). With a
  **different** token while a live unfinalized row exists, it **refuses**
  (active attempt in progress). Expiration of an unfinalized row requires
  the attempt to be provably dead: explicit spawn/bind failure (3.1.8), a
  bound run in a terminal state — for an interactive row additionally
  requiring that no durable V1 round checkpoint references it **and no
  durable pre-finalization approval intent is recorded on it** (a terminal
  run with a durable checkpoint or intent is reconciled, never expired;
  a `revoked` apply has had its intent cleared by the invalidation
  transition and expires normally) —
  or an
  expired `lease_expires_at`
  for rows that never reached a bind on either surface. An active bound
  run is never expirable regardless of lease age. Expiration stamps the
  durable `expired_at`
  marker, is one-way, and survives daemon restart.
- **Freshness surface**: a named constant `COORDINATOR_OWNED_SECTIONS =
  ("Task Mapping", "M1", "V1")` defines the
  sections the coordinator legitimately mutates after capture. Each
  identifier is the exact key `manifest_key` emits for that heading:
  `## Task Mapping` is ID-less and keys by normalized title, while
  `## M1 Task Manifest` and `## V1 Plan Changelog` are canonical headings
  keyed by their dotted IDs `M1`/`V1` — owned-section identifiers and
  manifest keys can never disagree because both come from the same key
  function. Every
  freshness guard compares the live plan's **reviewed-section manifest** —
  the per-section hashes of every manifest entry *not* in that constant,
  **including `__preamble__`** (the title, Plan ID, and other pre-heading
  bytes are reviewed content) — against
  the captured manifest. Coordinator-owned writes (changelog round entries,
  task-mapping updates, manifest application) never invalidate a round;
  any drift in a reviewed section refuses. The whole-plan `plan_hash`
  remains stored as snapshot identity only — it is never the comparison
  surface (a changelog write would self-invalidate every recorded round).
- Stale-write guard helper `verify_plan_unchanged(evidence_id, plan_path)`:
  applies the reviewed-section freshness surface. It is the comparison
  primitive `apply_plan_review_manifest` revalidates at compare-and-apply —
  the **single freshness gate** the approval commit order places before
  lifecycle approval, finalization, and mint on both surfaces. Reviewed-
  section drift refuses: the round is invalidated, its row stays
  unfinalized, and nothing mints — and when the refusing retry finds a
  recorded pending intent, the same refusal is the pending-plus-drift
  invalidation transition, so the row stays expirable, never deadlocked.
- Server-side compare-and-apply `apply_plan_review_manifest(evidence_id,
  round_result)` (gobby-plans tool): its input is the approval's complete
  validated `round_result`, whose typed
  `## M1 Task Manifest` entries are what gets applied — adversary
  contracts never write the plan
  file. The operation validates the evidence binding (current-attempt mode,
  still-unfinalized row), revalidates the live plan's reviewed-section
  manifest against the captured manifest via `verify_plan_unchanged`,
  pre-renders the manifest and runs expansion parsing **before** touching
  the file, then writes it atomically (single replacement) under the
  per-plan-path lock. Retry runs on the **durable manifest-application
  state machine** stored on the evidence row (checkpoint columns per the
  table definition) — never on live plan bytes, which the coordinator-owned
  `M1 Task Manifest` section may legitimately change after a successful
  apply: (i) first application (`manifest_state` null) persists the
  canonical payload + digest with `manifest_state=pending` **before** the
  atomic replacement — the canonical payload and digest cover the
  **complete validated approval `round_result`** (typed manifest entries
  included), and the same durable write records that payload in the
  row's `round_result` column as the pre-finalization approval intent —
  then stamps `manifest_state=applied` with the result
  and `manifest_applied_at` after it; (ii) an identical-digest re-invocation
  finding `applied` returns `manifest_result` without reading or writing
  the file — later M1 drift can never masquerade as a failed apply; (iii)
  an identical-digest re-invocation finding `pending` (crash between the
  intent write and the checkpoint, on either side of the file replacement)
  revalidates freshness and re-applies the replacement idempotently — the
  render is deterministic from the stored canonical payload, so re-applying
  converges to identical bytes whether or not the first replacement landed
  — then checkpoints `applied`, while reviewed-section drift found at a
  `pending` retry executes the **pending-plus-drift invalidation
  transition**: one atomic durable write stamps `manifest_state=revoked`
  and clears the row's `round_result` to null (`manifest_payload`/
  `manifest_digest` retained for forensics), revoking the pre-finalization
  approval intent and invalidating the round while leaving plan bytes
  untouched — the row then carries no intent, so it is expirable under the
  standard predicate and the active-attempt CAS is never blocked by the
  dead attempt; (iv) a **different**-digest invocation for
  the same evidence refuses in every state, and any invocation finding
  `revoked` refuses deterministically — re-review is the only path.
  Reviewed-section drift on a first application (`manifest_state` null —
  no intent yet recorded) and
  pre-render/parse failure refuse without writing — the plan file is left
  byte-unchanged and the checkpoint stays un-advanced.

**Acceptance:**

- 3.1.1 - `prepare_plan_review_round` persists an immutable snapshot +
  section manifest and returns `{evidence_id, plan_hash, sections}`.
  test: `tests/plans/test_review_evidence.py::test_prepare_round_snapshot`.
- 3.1.2 - Section hashing is canonical and mutation-sensitive: editing one
  section changes exactly that section's hash; hashing is stable across
  repeated runs on identical bytes; the emitted manifest keys are exact —
  `## 3.1 …` → `3.1`, `## M1 Task Manifest` → `M1`, `## V1 Plan
  Changelog` → `V1`, `## Task Mapping` → `Task Mapping`, pre-heading
  bytes → `__preamble__` — and coverage is total (every snapshot byte
  belongs to exactly one entry); duplicate keys fail closed across every
  key shape — a duplicate canonical section ID (two `## 3.1 …` headings),
  a duplicated `## M1 Task Manifest`, a duplicated `## V1 Plan Changelog`,
  a duplicate ordinary noncanonical title, and a duplicate `Task Mapping`
  heading each fail preparation with the deterministic duplicate-key error
  naming that exact key, and no occurrence-qualified identity is generated
  for any of them.
  test: `tests/plans/test_review_evidence.py::test_section_hash_canonicalization`.
- 3.1.3 - The adversary-facing payload is byte-identical to the stored
  snapshot even when the live file mutates after preparation.
  test: `tests/plans/test_review_evidence.py::test_toctou_snapshot_isolation`.
- 3.1.4 - `verify_plan_unchanged` refuses on reviewed-section drift and
  tolerates coordinator-owned section writes: a V1 changelog append, an M1
  manifest application, and a **Task Mapping mutation** each leave every
  reviewed-section hash unchanged and the guard passing under the exact
  owned-section keys (`Task Mapping`, `M1`, `V1`), while **preamble drift
  refuses**: an H1 title edit, a `**Plan ID:**` edit, and any other
  pre-heading byte change each fail the guard via the `__preamble__`
  entry; a finalized
  (recorded) round's row survives the next
  preparation; an unfinalized row referenced by a durable V1 round
  checkpoint is
  **finalized, not expired**, by the reconciliation checkpoint at the next
  preparation (interactive crash between checkpoint persist and finalize)
  with `round_result` recovered **losslessly** (the parsed payload equals
  the rendered input exactly), an unfinalized interactive row carrying a
  durable pre-finalization approval intent with `manifest_state=applied`
  (crash between approval steps 1 and 2 — no V1 checkpoint yet)
  undergoes the interactive approval recovery drain — its V1 checkpoint
  is persisted byte-equal to the rendered form, then the row is finalized
  losslessly from the intent with `lesson_mint_status=pending` stamped,
  and it is never expired,
  an unfinalized interactive row crashed while `manifest_state=pending`
  with **no** reviewed-section drift converges the apply to `applied`
  and continues through the same drain — its V1 checkpoint is persisted
  byte-equal to the rendered form, the row finalizes losslessly with
  `lesson_mint_status=pending`, the surfaced mint is completable via
  `checkpoint_plan_review_lesson_mint`, and the following re-preparation
  proceeds,
  an unfinalized interactive row carrying a `pending`-state intent whose
  reconciliation convergence finds reviewed-section drift undergoes the
  atomic pending-plus-drift invalidation transition
  (`manifest_state=revoked`, `round_result` cleared, plan bytes
  untouched), is expired by the same preparation's orphan cleanup once
  its bound run is terminal, and the preparation then proceeds — the
  active-attempt CAS is not blocked by the invalidated attempt,
  a malformed or schema-invalid checkpoint
  fails preparation with the deterministic reconciliation error,
  and reconciliation lineage validation is exercised negatively —
  wrong-project, wrong-plan, wrong-session, wrong-round, and
  hash-mismatched references each fail the preparation with a
  deterministic reconciliation error and leave the row unfinalized and
  unexpired;
  same-token re-preparation idempotently returns the existing unfinalized
  row; a different-token preparation against a live unfinalized attempt
  refuses; expiration happens only for provably dead attempts (spawn/bind
  failure, terminal bound run with — interactive — no durable V1
  checkpoint and no unrevoked approval intent, expired pre-bind
  `lease_expires_at`), stamps a
  durable `expired_at` that survives daemon restart (restart-recovery
  case); a bound interactive adversary run still active past
  `EVIDENCE_LEASE_SECONDS` survives the next preparation unexpired —
  concurrent
  interactive/stage-native preparation never expires live evidence.
  test: `tests/plans/test_review_evidence.py::test_stale_write_guard_and_lifecycle`.
- 3.1.5 - The `plan_review_evidence` table exists identically via both schema
  paths — fresh bootstrap (baseline) and upgrade-from-current-baseline
  (migration 337 through the existing runner, which discovers it without a
  duplicate-version collision) — with project scoping; a parity
  test compares the resulting table definitions.
  test: `tests/plans/test_review_evidence.py::test_schema_migration_baseline_parity`.
- 3.1.6 - Path-boundary validation rejects escaped, symlinked, and
  non-project paths at preparation; mutation-authorization consumption
  rejects replay, wrong-plan, wrong-round, wrong-attempt, and unresolvable
  references, returns the retryable `binding_pending` result while a
  run bind is pending, and answers a same-full-attempt-token repeat with
  the recorded finalized rejection result as an idempotent lost-response
  replay (read-only — no write, no re-finalization) while rejecting a
  wrong-run
  repeat as replay; historical proof reads accept finalized same-lineage
  rows from prior rounds and distinct dispatch runs while rejecting
  cross-plan and cross-lineage references and unfinalized rows.
  test: `tests/plans/test_review_evidence.py::test_path_boundary_and_binding_validation`.
- 3.1.7 - `apply_plan_review_manifest` refuses on reviewed-section drift,
  tolerates coordinator-owned section writes (prior V1 entries present),
  applies a domain-complete manifest exactly once with pre-render and
  expansion parsing before the atomic write, persists the complete
  approval `round_result` as the pre-finalization intent in the same
  durable write as its `pending` checkpoint (the row's `round_result` is
  non-null and equal to the validated payload while `finalized_at` stays
  null), and drives every retry from
  the durable checkpoint state machine: restart from a crash **before** the
  file replacement (`pending`, file untouched) completes the apply; restart
  from a crash **after** the replacement but before the checkpoint
  (`pending`, write landed) converges to identical bytes and checkpoints
  `applied`; an identical-digest retry after later coordinator M1 drift
  returns the stored prior result without reading or writing the file; a
  different-digest manifest for the same evidence refuses in every state;
  a first-application drift (`manifest_state` null) refuses with no
  checkpoint write and no intent recorded, while a `pending` retry that
  finds reviewed-section drift executes the pending-plus-drift
  invalidation transition in one atomic durable write
  (`manifest_state=revoked`, `round_result` cleared to null,
  `manifest_payload`/`manifest_digest` retained, plan bytes untouched),
  after which identical- and different-digest invocations both refuse
  deterministically and the row is expirable under the standard
  predicate;
  an invalid manifest leaves the plan byte-unchanged (no partial write, no
  duplicate section).
  test: `tests/plans/test_review_evidence.py::test_manifest_compare_and_apply`.
- 3.1.8 - `bind_evidence_run` attaches the run id to a prepared row exactly
  once **on both surfaces** (stage-native dispatch run and interactive
  `spawn_agent` run) and clears `lease_expires_at`; current-attempt
  consumption racing ahead of the bind receives the
  retryable `binding_pending` result and succeeds on retry after the bind
  (race/barrier test); a spawn failure expires the just-prepared row; a
  bind failure after a successful spawn expires the row and
  stops/invalidates the spawned run — spawn-failure and bind-failure
  cleanup are exercised for the interactive surface as well.
  test: `tests/plans/test_review_evidence.py::test_two_phase_run_binding`.
- 3.1.9 - Interactive mint-status lifecycle: finalizing an interactive
  round refuses deterministically while the changelog lacks the round's
  V1 checkpoint fence (no finalized interactive round without its V1
  entry can exist); finalizing an interactive approval verdict stamps
  `lesson_mint_status=pending` atomically with `finalized_at` on every
  finalization path — direct, checkpoint-reconciled, and intent-drained;
  reconciliation of an `applied`-intent row persists the missing V1
  checkpoint byte-equal to `render_v1_round_checkpoint` output **before**
  finalizing; while any same-lineage interactive approval row is
  `pending`, `prepare_plan_review_round` refuses new-round creation with
  the deterministic `pending_lesson_mint` result listing the rows and
  their durable `round_result` payloads, and proceeds once the status is
  checkpointed; `checkpoint_plan_review_lesson_mint` accepts only
  finalized interactive approval rows and only the
  `pending`→`minted`/`failed`/`none` transition (each recording
  `lesson_mint_detail`), is idempotent at every terminal state, and every
  marker survives daemon restart.
  test: `tests/plans/test_review_evidence.py::test_interactive_mint_status_lifecycle`.

### 3.2 Interactive plan-loop skill and agent contracts [category: config] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/review-learning/SKILL.md`
Target: `src/gobby/install/shared/skills/plan/SKILL.md`
Target: `src/gobby/install/shared/skills/plan-draft/SKILL.md`
Target: `src/gobby/install/shared/skills/plan-review/SKILL.md`
Target: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`
Target: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`
Target: `src/gobby/install/shared/workflows/agents/planner.yaml`
Target: `tests/skills/test_review_learning_skill.py`

Skill-side contracts for the interactive loop (phase 1 of plan-loop recording;
the stage-native deterministic mint is 6.x):

- **review-learning SKILL.md**: document the plan domain — reserved
  `lesson_type` vocabulary, class-scoped `pattern_id` forms with explicit
  `check_key` (consult `list_check_keys` and reuse before minting; seeded
  canonical starter keys per adversary category), per-class guardrail targets
  (plan classes → `checklist`), the reviser-records rule, and the per-class
  evidence-bundle requirements.
- **plan-review SKILL.md**: adversary findings extend to the full attestation
  schema — stable `finding_id`, `section_id` (primary anchor), `check_key`,
  at least one non-empty of `principle`/`root_cause` (both permitted; one
  wire field each), `prevention`, class-specific causal fields
  (`introduced_in_round`, and `causal_finding_id` — the distinct wire field
  naming the causal prior round's finding, never overloading the finding's
  own `finding_id`), and **validated section-set fields**: `participating_section_ids` (reviewer-miss: required
  non-empty — every section participating in the missed defect) and
  `causal_section_ids` (fixer-induced: required non-empty — every section the
  causal fix changed). Section sets carry ids only — hashes stay
  server-resolved; ids absent from the evidence manifest or class-required
  sets left empty are rejected. Recalled lesson classes are a mandatory extra
  review pass at review start. On approval the adversary returns the full
  typed `## M1 Task Manifest` entries in the verdict payload and never writes
  the plan file.
- **plan SKILL.md**: the coordinator calls `prepare_plan_review_round`
  immediately before every adversary spawn, binds the run id `spawn_agent`
  returns via `bind_evidence_run` immediately after the spawn (spawn or
  bind failure expires the row per 3.1 — the next attempt re-prepares),
  persists the **V1 round checkpoint** — the canonical fenced JSON from
  `render_v1_round_checkpoint`, pasted verbatim — carrying the returned
  round result (the incomplete-round rule — no proof, no mint — is
  scoped to rejection rounds and to approvals that crash **before**
  step 1 of the commit order records the durable intent; any approval
  crash after the intent write, including one that dies while the apply
  is still `pending`, is recovered from the durable intent per 3.1's
  drain, never abandoned), then finalizes the round's evidence
  (`finalize_plan_review_evidence` with the round's canonical result
  payload) once the checkpoint is durably
  persisted — a crash between those two writes is recovered by 3.1's
  reconciliation checkpoint (the durable V1 checkpoint finalizes the row
  at the
  next preparation with `round_result` recovered losslessly; it is never
  expired as an orphan); mandates
  `fixer-induced-defect` recall before every coordinator
  revision; records lessons at **final approval** using the changelog
  evidence: reviewer-miss requires every section in
  `participating_section_ids` hash-unchanged since an earlier reviewed round;
  fixer-induced requires changed hashes for every id in `causal_section_ids`
  + prior finding + introduction round; dual-class requires both complete
  bundles; every plan lesson carries the synthetic promotion anchor
  `finding.rule_id = "plan-review:<adversary-category>"` and never a
  plan-file path; cap ≤5 per plan with class-aware selection (≥1 slot per
  present class; reviewer-miss by rounds missed, fixer-induced by causal
  occurrences then severity; tie-breaks class-metric → severity → check_key
  asc → finding_id asc). The **approval commit order is fixed and
  crash-safe**, with the same logical order on both surfaces (interactive
  here, stage-native 6.2) and atomicity set by the storage boundary —
  stage-native fuses steps 2–3 with the stage transition into one
  PostgreSQL commit (6.2); interactive persists V1 (file) then finalizes
  (DB) with the 3.1 reconciliation checkpoint bridging a crash between
  them — every step idempotent and resumable: (1) apply the
  verdict's typed manifest entries via `apply_plan_review_manifest` — never
  a direct plan-file write — which revalidates reviewed-section freshness
  at compare-and-apply (`verify_plan_unchanged`) before any lifecycle
  effect and persists the complete validated approval `round_result` as
  the durable **pre-finalization approval intent** in the same durable
  write as its apply checkpoint (3.1); reviewed-section drift refuses
  here and invalidates the round
  (row unfinalized, nothing mints, plan untouched); (2) persist the V1
  approval round checkpoint (the rendered canonical fence carrying the
  complete `round_result` and lineage fields, rendered by
  `render_v1_round_checkpoint` **from the durable intent** — a pure
  function of durable state, so a process restart between steps 1 and 2
  reconstructs the exact checkpoint bytes); (3) finalize the
  round's evidence — finalization verifies the durable V1 checkpoint is
  present (the enforced step-2 marker) and stamps
  `lesson_mint_status=pending` atomically with `finalized_at`; (4) mint
  lessons per the rules above (historical proof
  reads over prior rounds' finalized evidence) and checkpoint the outcome
  via `checkpoint_plan_review_lesson_mint` — `minted` with lesson ids,
  `failed` with the recorder error, or `none` when nothing is mintable —
  so the approval is complete only when the status leaves `pending`.
  Steps 1 and 2 are
  coordinator-owned writes the freshness surface tolerates. A crash before
  (1) records its intent leaves an incomplete round (no proof, no mint,
  plan untouched) — the only approval crash that is incomplete; a
  crash after any step resumes at the failed step — a step-1 re-invocation
  with the identical payload returns its prior result as a no-op, and
  finalize and mint are idempotent. Drift arising after step 1 never blocks
  resume: steps 2–4 read only durable state (the immutable evidence row,
  the step-1 approval intent, and
  persisted entries — never coordinator memory), so the freshness gate
  sits solely at
  compare-and-apply, and a coordinator process death anywhere after step 1
  loses nothing — the resumed sequence re-runs from the failed step, and
  when no coordinator resumes, the next preparation's recovery drain
  (3.1) first converges a still-`pending` apply (no-drift convergence
  checkpoints `applied`; drift executes the invalidation transition),
  persists the missing V1 checkpoint, finalizes with
  `lesson_mint_status=pending`, and refuses any new round until the
  surfaced pending mints are completed and checkpointed — steps 2–4 are
  always either resumed or force-drained, never silently skipped.
- **plan-draft SKILL.md**: every changelog round entry embeds the pinned
  V1 round checkpoint fence — canonical JSON `{evidence_id, round_number,
  plan_hash, session_id, round_result}` rendered by
  `render_v1_round_checkpoint` — with the prose bullets around it a
  projection of that payload.
- **Agent YAMLs**: one instruction paragraph each pointing at the skill
  contracts (attestation schema for the adversary; recall-before-revision for
  the planner).

**Acceptance:**

- 3.2.1 - The review-learning skill documents plan-domain vocabulary,
  class-scoped identity with `check_key` reuse via `list_check_keys`,
  per-class guardrail targets, and the reviser-records rule.
  file: `src/gobby/install/shared/skills/review-learning/SKILL.md`.
- 3.2.2 - The plan skill mandates pre-spawn `prepare_plan_review_round`,
  post-spawn `bind_evidence_run` on the interactive run id, the rendered
  V1 round checkpoint persisted verbatim and finalized on durable
  persistence, the incomplete-round rule scoped to rejection rounds and
  pre-intent approval crashes (any approval crash after the step-1
  intent write — including a still-`pending` apply — recovers from the
  durable intent, never abandons the round),
  recall-before-revision, final-approval recording with per-class bundles and
  the synthetic `plan-review:<category>` rule_id anchor, the class-aware mint
  cap, and the fixed approval commit order (`apply_plan_review_manifest`
  with freshness revalidated at compare-and-apply and the durable
  pre-finalization approval intent persisted → V1 persist from the intent →
  finalize, V1-gated and `pending`-stamped →
  mint, then `checkpoint_plan_review_lesson_mint` to
  `minted`/`failed`/`none`).
  file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 3.2.3 - The plan-review skill carries the full finding/attestation schema
  including `causal_finding_id`, the at-least-one-of `principle`/
  `root_cause` rule, and the class-required `participating_section_ids` /
  `causal_section_ids` set fields, the approval-verdict manifest handoff (no
  direct plan-file writes), and the mandatory recalled-lesson review pass.
  file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 3.2.4 - Skill scenario: a seeded `reviewer-miss` lesson is recalled before
  findings; a simulated approval records `plan_review` lessons with the
  correct class, checklist target, synthetic `plan-review:<category>` rule_id
  anchor, and bundle evidence; an unproven
  classification and an A-changed/B-unchanged cross-section case record
  nothing / fixer-induced-only respectively.
  test: `tests/skills/test_review_learning_skill.py::test_plan_loop_recording_contract`.
- 3.2.5 - Interactive approval integration: from one captured round, the
  four operations run in commit order — `apply_plan_review_manifest`
  revalidates reviewed-section freshness at compare-and-apply (passing with
  prior V1 entries present), the approval entry persists, the evidence
  finalizes, and lessons mint from prior finalized rounds; an identical
  step-1 retry after a simulated crash returns the prior result without a
  second write. Drift is injected between every adjacent pair of steps:
  reviewed-section drift before step 1 refuses the round (row unfinalized,
  no intent recorded, nothing mints, plan untouched); drift injected after
  a **completed** step 1 (`manifest_state=applied`) never blocks
  resume — steps 2–4 complete from durable state; drift arriving while
  step 1 is still `pending` (crash inside the apply) invalidates the
  round on the resumed retry via the pending-plus-drift invalidation
  transition — the intent is revoked (`manifest_state=revoked`,
  `round_result` cleared), the row expires as a dead attempt once its
  run is terminal, the next preparation's CAS is unblocked, and
  re-review is the only path. A crash inside step 1 after the intent
  write (`manifest_state=pending`) with **no** reviewed-section drift is
  never wedged: the next preparation converges the apply to `applied`
  and runs the recovery drain — the V1 checkpoint is persisted
  byte-exactly, the row finalizes losslessly with
  `lesson_mint_status=pending`, the surfaced `pending_lesson_mint` mint
  completes and checkpoints `minted`, and re-preparation then
  proceeds. A process-restart crash
  between step 1 (manifest apply) and step 2 (V1 checkpoint persist)
  leaves the durable pre-finalization approval intent on the unfinalized
  row; the resumed sequence reconstructs the V1 checkpoint byte-exactly
  from the intent via `render_v1_round_checkpoint(evidence_id)`,
  finalizes exactly once, and mints from the recovered attestations —
  and when no coordinator resumes at all, the next preparation's recovery
  drain persists the V1 checkpoint byte-exactly, finalizes the row from
  the intent with `lesson_mint_status=pending`, and refuses new-round
  creation until the surfaced mint is completed and checkpointed, instead
  of expiring it. A crash between step 2
  (V1 checkpoint persist) and step 3 (finalize) leaves a durable
  checkpoint referencing an
  unfinalized row; the next preparation's reconciliation checkpoint
  finalizes it with `round_result` recovered exactly equal to the rendered
  payload and `lesson_mint_status=pending` stamped, and the resumed
  sequence mints from the recovered attestations. A crash between step 3
  (finalize) and step 4 (mint) leaves the finalized approval row at
  durable `lesson_mint_status=pending`; the next preparation refuses with
  `pending_lesson_mint` carrying the durable `round_result`, the drain
  mints from that payload **exactly once** — a re-run after a simulated
  mint crash adds no duplicate occurrence — checkpoints `minted` with the
  lesson ids, and re-preparation then proceeds. At every crash point
  recovery is complete only when the canonical V1 bytes exist in the
  changelog and the row's status has left `pending`.
  test: `tests/skills/test_review_learning_skill.py::test_interactive_approval_sequence`.

## P4: Validation-Gate Recording
`kind: framing`

**Goal**: Recurring validation failures become recordable lessons at the moment
proof exists — the successful close.

### 4.1 Structured validation issues end-to-end and success-path candidates [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/install/shared/prompts/validation/validate.md`
Target: `src/gobby/tasks/validation.py`
Target: `src/gobby/tasks/validation_verdict.py`
Target: `src/gobby/tasks/validation_history.py`
Target: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py`
Target: `tests/tasks/test_validation_issues.py` (new)

The live close path stores no structured issues today
(`validation_verdict.py` has no `issues`; `_record_validation_iteration`
stores `issues=None`; `group_similar_issues` groups only stored `Issue`
objects — recurrence detection never fires). Fix end-to-end with structured
issues (conversion from `blocking_reasons` rejected — it loses anchors):

- `validate.md` output contract gains `issues: [{title, type, severity,
  location}]` on invalid/pending verdicts — the wire key is the canonical
  `type` key that `Issue.to_dict`/`from_dict` already use, with the closed
  enum values enumerated in the prompt (`type`: `test_failure`,
  `lint_error`, `acceptance_gap`, `type_error`, `security`; `severity`:
  `blocker`, `major`, `minor`); `location` names a file/symbol anchor.
- `ValidationResult`, `_validation_result_from_data`, and
  `_record_validation_iteration` carry `issues: list[Issue]` through to
  `task_validation_history`. `_validation_result_from_data` parses the wire
  list defensively: a non-list `issues` value drops the whole payload, and
  a malformed item or unknown enum value drops that item — each with a
  logged warning; issue-parse problems never fail the validation verdict
  itself.
- On a **successful** close, recurrence is computed at the **configured**
  thresholds (`recurring_issue_threshold`, default 3, and
  `issue_similarity_threshold` from `TaskValidationConfig` — never
  hardcoded) with recurrence defined as **distinct failing iterations**,
  not raw issue occurrences: `ValidationHistoryManager` grouping retains
  iteration provenance (today `get_recurring_issue_summary` flattens every
  `Issue` across iterations and `group_similar_issues` thresholds on group
  length, so one iteration reporting the same issue three times would
  satisfy the default threshold without any recurrence), deduplicates
  equivalent issues **within** each iteration before grouping, and counts
  a group's occurrences as the number of distinct failing iteration IDs it
  spans. The close response includes `recurring_validation_candidates`: the
  recurring issue groups (titles, distinct-iteration counts, anchors) plus
  passing-iteration evidence. Candidates without a concrete location anchor
  are excluded. Infrastructure/pending failures produce no candidate.

**Acceptance:**

- 4.1.1 - Issues emitted by the validator persist through the real validation
  call path into `task_validation_history`.
  test: `tests/tasks/test_validation_issues.py::test_issue_persistence_real_path`.
- 4.1.2 - At the default threshold, two similar failing iterations then a
  pass yield no candidate; a third distinct failing iteration then a pass
  yields one; a custom threshold of 2 is honored; a single failing
  iteration containing the same issue three times followed by a pass yields
  **no** candidate (within-iteration duplicates never count as recurrence).
  test: `tests/tasks/test_validation_issues.py::test_configured_recurrence_thresholds`.
- 4.1.3 - Candidates without a concrete anchor are excluded;
  infrastructure/pending failures never produce candidates; clean-history
  closes return none.
  test: `tests/tasks/test_validation_issues.py::test_candidate_anchor_and_noise_gates`.
- 4.1.4 - The validator prompt documents the `issues` output contract with
  the canonical `type` key and the enumerated closed enum values.
  file: `src/gobby/install/shared/prompts/validation/validate.md`.
- 4.1.5 - `_validation_result_from_data` accepts the canonical wire payload
  and drops non-list, malformed, and unknown-enum `issues` payloads with a
  logged warning while the validation verdict still parses.
  test: `tests/tasks/test_validation_issues.py::test_issue_wire_schema_defensive_parse`.

### 4.2 Development-discipline recording instruction [category: config] (depends: 4.1, 2.1)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/development-discipline/SKILL.md`
Target: `tests/skills/test_development_discipline_skill.py`

When a successful close returns `recurring_validation_candidates`, the dev
agent records **one** `record_review_lesson` per task: `source_kind=
task_validation`, canonical `source="task-validation"`, and the
**task-scoped** `source_review="task-validation:<task_uuid>"` — the
occurrence identity is exactly `build_occurrence_key(source_review,
finding_fingerprint)`, so the task-scoped review id is what makes
byte-identical findings from two tasks distinct occurrences of one shared
pattern, while a same-task re-record dedupes as the same occurrence;
`lesson_type=recurring-validation-failure`, finding built
from the candidate group (check_key consulted via `list_check_keys`),
evidence citing the failed iterations + the passing close. The candidates in
the response are the prompt; the passing validation is the proof.

The recorded finding must carry the full actionable promotion signal the
existing gate requires: non-empty `prevention` and `principle`/`root_cause`
derived from the confirmed failure-and-pass evidence, and the issue location
normalized into a supported implementation anchor (file path or symbol on the
finding's anchor fields — never left as free prose). When that signal cannot
be established from the candidate, the agent records **nothing** — an
unpromotable lesson is noise.

When several groups recur, candidates are deterministically ordered
(recurrence count desc, then group title asc) and the **first** is recorded.
Class-scoped identity and target follow the plan-wide recorder rules:
`pattern_id = task-validation:recurring-validation-failure:<check-key>` and
`guardrail_target=validation` (strengthens the gate itself; without an
explicit target, occurrence 2 would default to a nonsensical `test` task).

**Acceptance:**

- 4.2.1 - The skill instructs exactly-one-lesson-per-task recording from
  close-response candidates with the correct source kind, the canonical
  `source` and task-scoped `source_review` values, class, class-scoped
  `task-validation:recurring-validation-failure:<check-key>` pattern_id,
  `guardrail_target=validation`, deterministic first-candidate selection,
  evidence shape, non-empty `prevention` and `principle`/`root_cause`, a
  normalized file/symbol anchor, and record-nothing when the actionable
  signal cannot be established.
  file: `src/gobby/install/shared/skills/development-discipline/SKILL.md`.
- 4.2.2 - Skill test asserts the recording contract text (source kind,
  canonical `source` and task-scoped `source_review`, class,
  pattern_id form, validation target, candidate ordering, one-per-task cap,
  candidate-driven, promotion-signal fields, normalized anchor,
  nothing-on-missing-signal).
  test: `tests/skills/test_development_discipline_skill.py::test_validation_lesson_contract`.

## P5: Epic QA Recording and Validator Injection
`kind: framing`

**Goal**: Epic QA mints leaf-QA-miss and validator-miss lessons with real
schema discipline, and the validator consumes its own miss lessons.

### 5.1 Epic finding schema and two-class recording [category: config] (depends: 2.1, 4.1)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/epic-review/SKILL.md`
Target: `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml`
Target: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`
Target: `tests/skills/test_epic_review_skill.py`

- `## Epic Findings` entries gain an explicit finding/confirmation schema:
  `check_key` (validated, consulted via `list_check_keys`), lesson class,
  `principle`/`root_cause`, `prevention`, concrete anchor (leaf task ref +
  file), and confirmed-fix evidence. Incomplete entries mint nothing.
- Two classes at the confirm point (fix confirmed on re-review), class-scoped
  identity `epic-qa:<lesson_type>:<check-key>`: `qa-miss` (leaf QA approved
  what epic QA caught; `guardrail_target=checklist`) and `validation-miss`
  (leaf validation passed what epic QA caught; `guardrail_target=validation`),
  both `source_kind=qa_rejection`, code domain, path tags from cited files.
- qa-reviewer.yaml notes the push-injected `qa-miss` lessons are a mandatory
  first-pass checklist at review start.

**Acceptance:**

- 5.1.1 - The epic-review skill defines the finding/confirmation schema, both
  classes with their guardrail targets, class-scoped identity, and the
  incomplete-entry rejection rule.
  file: `src/gobby/install/shared/skills/epic-review/SKILL.md`.
- 5.1.2 - Skill scenario: one epic finding minting both classes produces two
  lessons with separate pattern keys and occurrence counts; a schema-incomplete
  finding mints nothing.
  test: `tests/skills/test_epic_review_skill.py::test_two_class_epic_recording`.

### 5.2 Validator prompt lesson injection [category: code] (depends: 5.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py`
Target: `src/gobby/tasks/validation.py`
Target: `src/gobby/install/shared/prompts/validation/validate.md`
Target: `src/gobby/mcp_proxy/tools/tasks/_context.py`
Target: `src/gobby/mcp_proxy/tools/tasks/_factory.py`
Target: `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py`
Target: `src/gobby/mcp_proxy/tools/review_learning.py`
Target: `src/gobby/mcp_proxy/registries.py`
Target: `src/gobby/servers/http.py`
Target: `tests/mcp_proxy/tools/test_review_learning.py`
Target: `tests/tasks/test_validator_lesson_injection.py` (new)

- Wire one shared `ReviewLearningService` into `RegistryContext`: the
  instance is constructed once in `setup_internal_registries` and passed to
  the review-learning registry and both tasks registry factories. Today
  `create_review_learning_registry(memory_manager, task_manager)` constructs
  its **own** `ReviewLearningService` internally, so the factory signature
  changes to accept the shared instance
  (`create_review_learning_registry(service)`), with
  `tests/mcp_proxy/tools/test_review_learning.py` updated for the new
  construction.
- `validate_leaf_task_with_llm` fetches `validation-miss` lessons
  (`recall_review_lessons_by_class(lesson_domain="code",
  lesson_types=["validation-miss"], limit=3)`) into a `lessons_section`
  template slot in `validate.md`. The `lessons_section` slot renders empty
  for **both** a successful zero-result recall and a failed recall — the
  prompt never carries error text, so the rendered prompt is identical in
  the two cases. Recall failure — including unresolved project scope — stays
  non-fatal (validation proceeds without lessons) but is never silent: the
  validation result gains a `diagnostics: list[dict]` field, and recall
  failure appends exactly one entry `{code: "lesson-recall-failed",
  severity: "warning", detail: <reason>}` (stable code, also logged as a
  warning). A successful zero-result recall appends nothing. The serialized
  `diagnostics` entry is the **sole observable discriminator** between the
  error branch and a legitimate empty lesson set.

**Acceptance:**

- 5.2.1 - The validator prompt renders recalled lessons in `lessons_section`
  and renders identically to today when no lessons exist.
  test: `tests/tasks/test_validator_lesson_injection.py::test_lessons_section_empty_safe`.
- 5.2.2 - Injection preserves the 4.1 structured-issues output contract
  (integration: lessons present + issues emitted in one validation pass).
  test: `tests/tasks/test_validator_lesson_injection.py::test_issues_contract_preserved`.
- 5.2.3 - One shared `ReviewLearningService` instance serves the
  review-learning registry and both tasks registry contexts:
  `create_review_learning_registry` accepts the shared service instead of
  constructing its own, and an identity test asserts the object wired into
  `setup_internal_registries` is the same instance reachable from the
  review-learning registry and both tasks factories; the existing
  review-learning registry tests pass under the new signature.
  test: `tests/mcp_proxy/tools/test_review_learning.py::test_shared_service_identity`.
- 5.2.4 - A recall error (including unresolved project scope) serializes
  exactly one `{code: "lesson-recall-failed", severity: "warning"}` entry in
  the validation result's `diagnostics` field with `lessons_section`
  byte-identical to the zero-result rendering; a successful zero-result
  recall serializes no diagnostic.
  test: `tests/tasks/test_validator_lesson_injection.py::test_recall_failure_diagnostic`.

## P6: Stage-Native Deterministic Mint
`kind: framing`

**Goal**: The dispatch-pipeline plan loop mints lessons deterministically from
persisted structured findings, with a durable retry route.

### 6.1 Structured adversary findings on reject_review [category: code] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`
Target: `src/gobby/storage/tasks/_transitions_facade.py`
Target: `src/gobby/storage/tasks/_transitions.py`
Target: `src/gobby/dispatch/spawn.py`
Target: `src/gobby/dispatch/prompts.py`
Target: `tests/storage/test_stage_review_findings.py` (new)

- **Pre-spawn evidence binding (two-phase)**: the dispatch execution path
  (`src/gobby/dispatch/spawn.py`) — not the pure prompt formatter — calls
  `prepare_plan_review_round` before every adversary spawn, binding the row
  to the attempt token `(task_id, stage, round_number)`; the adversary
  prompt transport (`src/gobby/dispatch/prompts.py`) carries the stored
  snapshot payload and `evidence_id` — the adversary reviews the snapshot,
  never the live plan (same contract as the interactive flow in 3.1/3.2).
  Immediately after the spawn returns its run id, the same execution path
  attaches it via `bind_evidence_run(evidence_id, dispatch_run_id)`; a
  spawn failure expires the just-prepared row (cleanup, per 3.1).
- `reject_review` gains `findings: list[dict] | None` per the plan-review
  attestation schema (finding_id, section_id, check_key, severity, category,
  location, description, fix, `prevention`, at least one non-empty of
  `principle`/`root_cause` (both permitted; one wire field each),
  `introduced_in_round`, `causal_finding_id` — the distinct wire field for
  the causal prior round's finding — and the class-required
  `participating_section_ids` / `causal_section_ids` set fields — ids
  validated against the evidence manifest) **plus the round's
  `evidence_id`**, validated against the attempt binding per 3.1 (project,
  plan path, round, task/stage, bound dispatch run — a still-pending run
  bind returns the retryable `binding_pending` result; replayed, wrong-plan,
  and wrong-round references are rejected, while a repeat call presenting
  the same row under the same full attempt token
  is the idempotent lost-response replay defined below). Free-text
  `rejection_notes`
  remains a fallback; free-text-only rounds mint nothing later.
- Python renders the `## Adversary Findings — Round N` markdown from the
  structured list and appends a canonical fenced ```json block carrying the
  findings + `evidence_id`, `plan_hash`, and the section-hash manifest —
  **resolved server-side from the referenced `plan_review_evidence` record**;
  caller-supplied hash values are ignored. The rendered description is a
  **projection only**: the canonical validated findings payload is written
  to the evidence row's `round_result` column (3.1) in the same
  transaction, and that durable payload — never `task.description`, which
  `update_task` can rewrite at any time — is what classification and mint
  read later (6.2). The payload travels the **public task-manager path**:
  the MCP handler in `_stage_review.py` calls
  `TaskTransitionsMixin.reject_review` in
  `src/gobby/storage/tasks/_transitions_facade.py`, whose signature gains
  the `findings`/`evidence_id` parameters and forwards them to the
  stage-transition owner in `_transitions.py`; the stage-row update and
  the evidence writes share one ambient transaction. Fence persistence,
  `round_result`, evidence
  finalization, **and the stage transition itself** (`needs_review` →
  `ready`) commit in **one PostgreSQL transaction** (3.1) — no crash
  window can leave a durable fence pointing at an unfinalized row, and a
  failed transaction leaves neither. Because that commit advances the
  stage, no post-commit mutation window exists: a repeat `reject_review`
  under the same full attempt token is an **idempotent lost-response
  replay** — recognized from the finalized row and its `round_result` for
  that token (the rejection-side mirror of 6.2's approval-checkpoint
  replay), it returns the recorded rejection result read-only, never
  re-runs current-attempt authorization against the advanced stage, and
  never writes; changed findings require a new review round through the
  next preparation. A wrong-run repeat
  is replay.

**Acceptance:**

- 6.1.1 - Structured findings render + JSON-fence round-trip losslessly,
  including a root_cause-only finding (no
  `principle`) and a fixer-induced finding carrying `causal_finding_id`.
  test: `tests/storage/test_stage_review_findings.py::test_fence_round_trip`.
- 6.1.2 - Evidence in the fence resolves server-side from the
  `plan_review_evidence` record referenced by `evidence_id`; caller-supplied
  hash values are ignored; mutating the live plan after spawn does not change
  the fence's recorded evidence (snapshot authoritative); a replayed,
  wrong-plan, or wrong-round `evidence_id` is rejected against the attempt
  binding.
  test: `tests/storage/test_stage_review_findings.py::test_server_side_evidence_resolution`.
- 6.1.3 - Free-text-only rejection still works unchanged (fallback path),
  and non-planning review stages' `reject_review` contract is
  byte-for-byte unchanged — no evidence requirement, no findings schema
  (the activation boundary defined in 6.2 covers both operations; the
  full five-stage regression matrix — both operations, registry and
  facade, including epic QA — is asserted by 6.2.9).
  behavior: "free-text rejection fallback" in `tests/storage/test_stage_review_findings.py`.
- 6.1.4 - Stage-native dispatch prepares evidence bound to `(task_id, stage,
  round_number)` before the adversary spawn, the prompt payload is
  byte-identical to the stored snapshot, the run id is attached post-spawn
  via `bind_evidence_run`, a `reject_review`/`approve_review` racing ahead
  of the bind receives `binding_pending` and succeeds on retry after the
  bind (race/barrier test), a failed spawn expires the prepared row, and a
  failed bind after a successful spawn expires the row and
  stops/invalidates the spawned run.
  test: `tests/storage/test_stage_review_findings.py::test_pre_spawn_snapshot_transport`.
- 6.1.5 - Fence persistence, `round_result`, evidence finalization, and
  the stage transition are
  one atomic commit **exercised through the public task-manager facade
  path** (`TaskTransitionsMixin.reject_review`), not only a direct storage
  helper: a durably persisted rejection fence always has a finalized row
  (crash injection between the writes yields either all or nothing,
  never a durable fence with an unfinalized row), and the row survives the
  next round's preparation; a same-attempt repeat rejection after the
  commit returns the recorded rejection result as an idempotent
  lost-response replay with no second write and no re-finalization; a
  wrong-run repeat is rejected as replay; a wholly failed persistence
  leaves the row unfinalized and expirable.
  test: `tests/storage/test_stage_review_findings.py::test_rejection_finalizes_evidence`.

### 6.2 Idempotent mint with workflow-owned backfill [category: code] (depends: 6.1, 3.2)
`kind: deliverable`

Target: `src/gobby/review_learning/round_diff.py` (new)
Target: `src/gobby/review_learning/recorders.py` (new)
Target: `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`
Target: `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py`
Target: `src/gobby/storage/tasks/_transitions_facade.py`
Target: `src/gobby/storage/tasks/_transitions.py`
Target: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`
Target: `tests/review_learning/test_round_diff.py` (new)
Target: `tests/mcp_proxy/tools/test_plan_review_backfill.py` (new)

- `round_diff.py`: classification reads the **durable `round_result`
  payloads** on the task lineage's finalized `plan_review_evidence` rows
  (3.1) — never `task.description`, whose fences are a projection that
  `update_task` can rewrite after the fact — and classifies
  blocking findings per the per-class evidence bundles — reviewer-miss (every
  section in `participating_section_ids` hash-unchanged since an earlier
  reviewed round), fixer-induced (changed hashes for every id in
  `causal_section_ids` + `causal_finding_id` + introduction round),
  dual-class only with both complete bundles; attestation completeness —
  including the at-least-one-of `principle`/`root_cause` rule and
  `causal_finding_id` for fixer-induced — is validated identically to
  `reject_review`; unattested, incomplete, or free-text rounds yield
  nothing.
- `recorders.mint_plan_review_lessons(task_id, stage)`: idempotent (occurrence
  dedupe). The finalized `plan_review_evidence` rows bound to the task
  lineage are both the durable **index** and the **payload** of the mint
  input: rounds are discovered by `(task_id, stage)` lineage query and
  each row is consumed under
  the historical proof-read policy (3.1) — server-resolved hashes and the
  stored `round_result` are
  authoritative, spanning prior rounds and distinct dispatch runs within the
  same task lineage. Class-aware selection cap ≤5
  (≥1 slot per present class; deterministic tie-breaks); per-class
  `guardrail_target=checklist`; class-scoped identity + check_key from the
  attestation; every minted lesson carries the synthetic promotion anchor
  `finding.rule_id = "plan-review:<adversary-category>"` (never a plan-file
  path) so both plan classes pass the actionable-signal gate.
- **Activation boundary**: the evidence-bound review path — 6.1's
  structured-findings rejection and this approval commit order — activates
  only for **planning-stage reviews**, the stage whose registered reviewer
  attempt is the plan adversary and whose dispatch prepared and bound
  `plan_review_evidence` (6.1). Within that boundary approval is
  fail-closed: it must present the bound `evidence_id` and the verdict's
  typed manifest payload. `approve_review` and `reject_review` for every
  other review stage (development QA, expansion QA, document review,
  epic QA, PR/trajectory) keep their existing transitions byte-for-byte —
  no evidence resolution, no manifest application, no approval
  checkpoint, no mint.
- Planning-stage `approve_review` runs the same approval commit order as
  the interactive
  flow (3.2). It first resolves the approving round's `evidence_id` through
  the 3.1 binding validation — an unresolvable, replayed, wrong-plan, or
  wrong-round reference **refuses the approval itself** before any stage
  transition, finalization, manifest application, or mint (authorization
  failure is never a mint failure), and a still-pending run bind returns
  the retryable `binding_pending` result. It then (1) applies the verdict's
  typed manifest entries via `apply_plan_review_manifest` (3.1) —
  revalidating reviewed-section freshness at compare-and-apply and
  persisting the complete approval `round_result` as the durable
  pre-finalization intent (the shared tool contract with 3.2; the atomic
  commit below finalizes from a payload equal to that intent);
  reviewed-section drift refuses the approval with the row left unfinalized
  and nothing minted, coordinator-owned drift is tolerated, and the
  adversary never writes the plan file; (2+3) commits the **stage approval
  transition** — entered through `TaskTransitionsMixin.approve_review` in
  `src/gobby/storage/tasks/_transitions_facade.py`, whose signature gains
  the evidence/verdict payload and forwards it to the `_transitions.py`
  stage-transition owner inside one ambient transaction — together with
  the approval's evidence reference, the round's `round_result` payload,
  the evidence finalization
  (`finalize_plan_review_evidence` — final same-channel confirmation bound
  to the exact bytes the adversary reviewed), and the durable **approval
  checkpoint** on the approving evidence row (`approval_result`,
  `approved_at`, `lesson_mint_status=pending` — the 3.1 columns) in **one
  PostgreSQL transaction** — the stage never advances with the approving
  evidence unfinalized, so no crash can strand an approval whose attempt is
  no longer current while its row is still mutation-mode-only; (4) awaits
  the mint post-commit (errors caught + logged, never block the verdict)
  and returns `lesson_mint_status` (`minted`/`failed`/`none`) + retry
  info. A lost-response retry of `approve_review` presenting the same
  full attempt/evidence token **after** the atomic approval commit is
  recognized from the approval checkpoint columns on the approving
  evidence row and returns the recorded
  `approval_result` with the current `lesson_mint_status` — it never
  re-runs current-attempt authorization against the advanced stage and
  never fails as replay. `lesson_mint_status=failed` is reserved for
  recorder failures **after** evidence has resolved and been authorized;
  the crashed-before-mint case is the durable `pending` state, repaired by
  the same backfill. The backfill is the named gobby-tasks-ops MCP tool
  **`backfill_plan_review_lessons(task_id, stage)`** (registered in
  `_stage_ops.py`): it validates that the task/stage carries a durable
  approval checkpoint, reads only durable state (finalized evidence rows
  and their `round_result` payloads — never live task-description bytes),
  re-runs the idempotent mint, updates the row's `lesson_mint_status` /
  `lesson_mint_detail`, and returns `{lesson_mint_status,
  minted_lesson_ids, detail}`. Per-state behavior is pinned: `pending`
  and `failed` attempt the mint and checkpoint `minted` (with lesson ids)
  or `failed` (with the recorder error); `minted` returns the recorded
  result without re-minting; `none` (an approval with nothing mintable,
  e.g. free-text rounds) returns `none`; repeated calls at every state
  are idempotent in both status and result. No extra authorization token
  exists — the operation is project-scoped and only replays
  already-authorized durable state. `plan-adversary.yaml` contract: on
  `lesson_mint_status=failed` (or `pending` after a lost response),
  attempt `backfill_plan_review_lessons` **once**
  before `end_agent_run`, then relay remaining failure to the coordinator.
- Escalated/abandoned stages mint nothing.

**Acceptance:**

- 6.2.1 - Classification matrix: reviewer-miss only / fixer-induced only /
  dual-class with both bundles / unattested→none / nit→none /
  A-changed-B-unchanged→fixer-induced-only / multi-section reviewer miss
  (all participating sections unchanged) / multi-section causal fix (all
  causal sections changed; one unchanged causal id fails the bundle).
  test: `tests/review_learning/test_round_diff.py::test_classification_matrix`.
- 6.2.2 - Mint failure at approval never blocks the verdict, surfaces in
  `lesson_mint_status`, and the backfill operation then mints exactly one
  occurrence per lesson (failed-mint backfill idempotence).
  test: `tests/review_learning/test_round_diff.py::test_backfill_idempotence`.
- 6.2.3 - Class-aware selection reserves a slot per present class under the
  cap with deterministic tie-breaks (mixed-class + shuffled-input).
  test: `tests/review_learning/test_round_diff.py::test_class_aware_cap_selection`.
- 6.2.4 - Escalated stages mint nothing.
  test: `tests/review_learning/test_round_diff.py::test_escalation_mints_nothing`.
- 6.2.5 - Approval with post-spawn mutation confined to coordinator-owned
  sections confirms against the captured snapshot: the evidence record is
  resolved through the attempt binding, the manifest applies with freshness
  revalidated at compare-and-apply, and the stage transition, approval
  reference, `round_result`, finalization, and mint-pending checkpoint
  commit atomically **through the public task-manager facade path**
  (`TaskTransitionsMixin.approve_review`) before
  minting; post-spawn drift in a reviewed section refuses the approval with
  the row left unfinalized and nothing minted; a replayed, wrong-plan,
  wrong-round, or unresolvable reference refuses the approval before any
  stage transition, finalization, manifest application, or mint — never
  `lesson_mint_status=failed`. Crash/restart is exercised at every adjacent
  boundary: a crash between step 1 and the atomic commit resumes (step-1
  retry no-ops from its checkpoint, the commit then lands); crash injection
  inside the commit yields all-or-nothing (stage never advances with an
  unfinalized approving row); a crash after the commit before mint leaves
  durable `lesson_mint_status=pending`, and the backfill mints exactly one
  occurrence; a lost-response `approve_review` retry after stage
  advancement returns the recorded approval result with current mint
  status instead of failing authorization or replay.
  test: `tests/review_learning/test_round_diff.py::test_approval_evidence_finalization`.
- 6.2.6 - Classification and mint consume finalized evidence from at least
  two prior rounds with distinct dispatch runs (historical proof reads);
  cross-plan and cross-lineage evidence references and unfinalized rows are
  rejected from classification.
  test: `tests/review_learning/test_round_diff.py::test_historical_evidence_lineage`.
- 6.2.7 - Mint authority is durable: after a rejection or approval commits,
  replacing `task.description` wholesale via `update_task` changes neither
  round classification nor minted lessons — the description fence is a
  projection and `round_diff` reads only finalized evidence rows.
  test: `tests/review_learning/test_round_diff.py::test_description_mutation_immunity`.
- 6.2.8 - The registered `backfill_plan_review_lessons` operation is
  exercised end-to-end through the gobby-tasks-ops registry: `pending` and
  `failed` states mint exactly once and checkpoint `minted`; a `minted`
  state returns the recorded result without re-minting; `none` stays
  `none`; a task/stage without a durable approval checkpoint is refused;
  repeated calls at every state are idempotent in both status and result.
  test: `tests/mcp_proxy/tools/test_plan_review_backfill.py::test_backfill_wire_contract`.
- 6.2.9 - Non-planning review approvals and rejections are unaffected:
  **both** `approve_review` and `reject_review` are exercised for
  **every** non-planning review stage — development, expansion, document,
  **epic QA**, and PR/trajectory — through both the registry operations
  and the `TaskTransitionsMixin.approve_review`/`reject_review` facade
  paths; each succeeds without plan evidence or manifest payload,
  produces its existing stage-transition result unchanged, and performs
  no plan-evidence resolution, no manifest application, no approval
  checkpoint, and no lesson mint, while a planning-stage
  approval without a resolvable bound `evidence_id` and typed manifest
  payload is refused.
  test: `tests/mcp_proxy/tools/test_plan_review_backfill.py::test_non_plan_approval_unaffected`.

## P7: Retirement and End-to-End Verification
`kind: framing`

**Goal**: The loop has a correction valve for obsolete lessons, and the whole
system is proven end-to-end.

### 7.1 retire_review_lesson operation [category: code] (depends: 3.2, 5.2)
`kind: deliverable`

Target: `src/gobby/review_learning/class_recall.py`
Target: `src/gobby/mcp_proxy/tools/review_learning.py`
Target: `src/gobby/install/shared/skills/review-learning/SKILL.md`
Target: `tests/review_learning/test_retirement.py` (new)

Explicit `retire_review_lesson(pattern_id, evidence, session_id)` (overloading
`record(decision="stale")` was rejected — that path early-returns before scope
resolution): requires resolved project scope + non-empty evidence; retags
matching confirmed lessons `confirmed`→`stale` under the existing
`ReviewLearningPatternMutation` advisory lock; returns affected memory IDs
**and** any open `Guardrail:` task refs for the pattern (caller decides;
no auto-close). Skill line: any agent that verifies an injected lesson is
obsolete retires it with the injected `pattern_id`.

The 3.2 dependency serializes this task behind the canonical
review-learning `SKILL.md` edits; the 5.2 dependency serializes it behind
5.2's `create_review_learning_registry` factory-signature change, because
both tasks edit `src/gobby/mcp_proxy/tools/review_learning.py` and the
expansion compiler emits dependency edges only from explicit manifest
`depends_on` entries — phase numbering alone does not order them. The
`## M1 Task Manifest` entry for this section must carry both edges.

**Acceptance:**

- 7.1.1 - Retirement requires project scope + evidence, retags under the
  advisory lock, and returns affected IDs + open guardrail task refs without
  closing them. test: `tests/review_learning/test_retirement.py::test_retire_contract`.
- 7.1.2 - Retired lessons disappear from both deterministic recalls
  immediately. test: `tests/review_learning/test_retirement.py::test_retired_absent_from_recalls`.

### 7.2 End-to-end coexistence and promotion regression suite [category: test] (depends: 5.2, 6.2, 4.2)
`kind: deliverable`

Target: `tests/review_learning/test_feedback_loop_e2e.py` (new)

Behavior-pinning suite across the generalized loop:

- Per-class promotion: a reviewer-miss pattern and a fixer-induced pattern
  each reach two occurrences, hold independent occurrence counts, pass the
  actionable-signal gate via the synthetic `plan-review:<category>` rule_id
  anchor (no plan-file paths), and each mints its own checklist guardrail
  task via the existing explicit-target path; epic classes reach their
  declared targets (`checklist`/`validation`);
  the validation class (`recurring-validation-failure`) recorded across two
  tasks converges to one `task-validation:` pattern with distinct occurrences
  and promotes to its `validation` target.
- Coexistence: with plan lessons present (including one with a colliding code
  path), code-domain semantic and file recalls return zero plan lessons;
  legacy lessons behave byte-identically to before.
- Check-key convergence: equivalent findings resolve to one key with more
  than five competing class lessons in the store.

**Acceptance:**

- 7.2.1 - Per-class promotion cases pass for all five reserved classes
  (reviewer-miss, fixer-induced-defect, qa-miss, validation-miss,
  recurring-validation-failure) with independent occurrence counts; the
  recurring-validation-failure case spans two tasks recording
  **byte-identical** candidate findings under their task-scoped
  `source_review` values — one shared `task-validation:` pattern, two
  distinct occurrence keys, occurrence count 2 — carries non-empty
  `prevention` and `principle`/`root_cause` and a normalized file/symbol
  anchor per the 4.2 contract, and promotes to the `validation` target
  through the actionable-signal gate; both plan classes promote at
  occurrence 2 with the synthetic rule_id anchor satisfying the same gate.
  test: `tests/review_learning/test_feedback_loop_e2e.py::test_per_class_promotion`.
- 7.2.2 - Cross-domain coexistence regression passes (semantic + file recall,
  colliding path, legacy behavior).
  test: `tests/review_learning/test_feedback_loop_e2e.py::test_cross_domain_coexistence`.
- 7.2.3 - Check-key convergence holds beyond the recall cap window.
  test: `tests/review_learning/test_feedback_loop_e2e.py::test_check_key_convergence`.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 5b8fca6b-08b1-4832-82be-523dd5fae5c7
- enhancer_session: 75e14171-424f-4210-941b-91c577aaf749
- converged: false
- suggestions_presented: 4
- accepted:
  - E1 / better / versioned migration 335 for `plan_review_evidence` alongside
    the baseline definition, with upgrade/fresh parity tests (§ 3.1)
  - E2 / better / 1.1.6 compatibility redefined as exact-plus-domain-tag:
    legacy rows untouched, new legacy-path lessons gain exactly
    `lesson-domain:code` (§ 1.1)
  - E3 / better / stage-native flow bound to the evidence snapshot: pre-spawn
    prepare + snapshot transport, `evidence_id` resolved server-side on
    `reject_review`, approval finalizes evidence before mint; plans registry
    path corrected to the package `__init__` (§§ 3.1, 6.1, 6.2)
  - E4 / better / validation recorder gains deterministic candidate selection,
    `task-validation:recurring-validation-failure:<check-key>` pattern_id,
    `guardrail_target=validation`; § 7.2 extended to the fifth reserved class
    (§§ 4.2, 7.2)
- declined: none
- resolution_notes: All four suggestions folded into §§ 1.1, 3.1, 4.2, 6.1,
  6.2, 7.2. Enhancement cap (max_enhancement_rounds=1) reached; control
  proceeds to the adversary gate.

**Round 1** `kind: verification`

- reviewer_run: 41ae7588-1288-4d1f-b515-bbb528749eb2
- reviewer_session: 0e7f351c-162f-4131-a7ee-98be2d147986
- verdict: needs_review
- findings: 9 (8 blocking, 1 nit) — contract-target-coverage,
  contract-table-row-decomposition, plan-promotion-anchor,
  plan-adversary-yaml-collision, review-evidence-attempt-binding,
  review-evidence-finalization, manifest-compare-apply,
  validator-recall-scope-failure, unused-semantic-plan-opt-in
- accepted: all 9 (user-voted, item by item)
- declined: none
- resolution_notes: Target inventories completed (1.1, 2.1) and prose
  precedents converted to symbol references (1.1, 3.1); 2.2 acceptance split
  into one item per injection rule plus the integration item; synthetic
  `plan-review:<adversary-category>` rule_id promotion anchor restored across
  3.2, 6.2, and 7.2; 6.2 now depends on 3.2 to serialize the shared
  plan-adversary.yaml edit; evidence rows gain attempt binding and
  path-boundary validation with consumption-side replay / wrong-plan /
  wrong-round rejection (3.1, 6.1, 6.2); finalize-on-round-result lifecycle
  replaces finalize-at-approval-only (3.1, 3.2, 6.1); approval verdicts carry
  typed manifest entries applied via the new server-side
  `apply_plan_review_manifest` compare-and-apply under a per-plan-path lock
  (3.1, 3.2, 6.2) — adversary contracts no longer write the plan file; 5.2
  recall failures emit a structured diagnostic distinct from a successful
  empty recall; `recall_review_context` stays code-domain-only (1.2
  `lesson_domain` param removed as consumer-less). Remaining rounds run
  unattended per user directive (2026-07-22).

**Round 2** `kind: verification`

- reviewer_run: aef1ef80-8e15-43e6-bd45-1d583d8fa0cc
- reviewer_session: cf2e61b9-ce48-4747-abbe-639e62f17f19
- verdict: needs_review
- findings: 6 (6 blocking) — interactive-evidence-self-invalidation,
  historical-evidence-active-attempt-conflict,
  stage-native-pre-spawn-binding-gap, plan-attestation-section-set-missing,
  validation-lesson-promotion-signal-missing,
  validator-empty-recall-contract-contradiction
- accepted: all 6 (applied unattended per user directive)
- declined: none
- resolution_notes: Freshness redefined as the reviewed-section manifest —
  `COORDINATOR_OWNED_SECTIONS` (Task Mapping, M1 Task Manifest, V1 Plan
  Changelog) excluded — so coordinator V1/M1 writes never self-invalidate a
  round while reviewed-section drift refuses; the interactive approval
  sequence is fixed and crash-safe (freshness → V1 persist → finalize →
  mint → apply_plan_review_manifest) with new integration acceptance 3.2.5.
  Evidence consumption split into current-attempt mutation authorization vs
  read-only historical proof reads over finalized same-lineage rows (3.1,
  6.2, new 6.2.6). Stage-native binding is two-phase: preparation binds the
  attempt token `(task_id, stage, round_number)` in `dispatch/spawn.py`,
  `bind_evidence_run` attaches the run id post-spawn, spawn failure expires
  the row (3.1, new 3.1.8, 6.1). Attestation schema gains class-required
  `participating_section_ids`/`causal_section_ids` validated against the
  evidence manifest, with multi-section classification-matrix cases (3.2,
  6.1, 6.2). The 4.2 recorder must supply the full actionable promotion
  signal (prevention, principle/root_cause, normalized file/symbol anchor)
  or record nothing, asserted in 7.2.1. 5.2 prose reconciled with 5.2.4:
  `lessons_section` renders empty on both zero-result and failed recall;
  the structured diagnostic is the sole discriminator.

**Round 3** `kind: verification`

- reviewer_run: 85853d0d-00dc-47fd-9558-000ed62dc9e4
- reviewer_session: 7164eebb-753e-448f-a88d-7c7873b24e4e
- verdict: needs_review
- findings: 10 (10 blocking) — post-finalize-manifest-authorization,
  approval-freshness-commit-order, manifest-apply-retry-atomicity,
  finalized-round-replacement, stage-native-binding-pending-race,
  unresolvable-approval-evidence-semantics,
  attestation-wire-schema-ambiguity, validation-issue-wire-schema,
  validator-recall-diagnostic-schema, referenced-spike-invariant-drift
- accepted: all 10 (applied unattended per user directive)
- declined: none
- resolution_notes: The approval commit order is unified on both surfaces
  and reordered so `apply_plan_review_manifest` runs first — operating on
  the still-unfinalized current-attempt row and revalidating
  reviewed-section freshness at compare-and-apply before approval
  persistence, finalization, and mint (3.1, 3.2, 6.2) — resolving the
  post-finalize replay contradiction by ordering rather than a third
  authorization mode; the freshness gate sits solely at compare-and-apply,
  and drift after step 1 never blocks resume since later steps read only
  durable state (3.2.5, drift injected between every adjacent step pair).
  Manifest retry is a checkpointed no-op for an identical payload, with
  pre-render/expansion-parse before the atomic write and byte-unchanged
  refusal on invalid input (3.1, 3.1.7). Same-round fence replacement is a
  bounded authorization under the same full attempt token, re-finalizing
  the same row while wrong-run repeats stay replay (3.1, 3.1.6, 6.1,
  6.1.5). Stage-native mutations return retryable `binding_pending` while
  the run bind is pending; bind failure after a successful spawn expires
  the row and stops the spawned run (3.1, 3.1.8, 6.1, 6.1.4). Unresolvable
  approving evidence refuses the approval itself; `lesson_mint_status=
  failed` is reserved for post-authorization recorder failures (6.2,
  6.2.5). The attestation wire schema names `causal_finding_id` and
  requires at least one of `principle`/`root_cause`, validated identically
  in reject_review, fences, and round_diff classification (3.2, 3.2.3,
  6.1, 6.1.1, 6.2). The validator wire contract uses the canonical `type`
  key with enumerated closed enum values and defensive parsing that never
  fails the verdict (4.1, new 4.1.5). The 5.2 recall diagnostic is a
  concrete `{code: "lesson-recall-failed", severity: "warning"}` entry in
  a new `diagnostics` validation-result field (5.2, 5.2.4). The Overview
  carries a spike-supersession note naming the replaced invariants
  (whole-plan-hash freshness, finalize-after-approval, singular
  `evidence_section_id`) as superseded by 3.1/3.2/6.x.

**Round 4** `kind: verification`

- reviewer_run: 926515b6-32ba-49a3-874e-569d2bf3bb57
- reviewer_session: 5449bc2a-7f47-4be3-8c93-d7738081eedf
- verdict: needs_review
- findings: 7 (7 blocking) — migration-version-335-collision,
  shared-review-service-target-omission,
  manifest-retry-checkpoint-storage-missing,
  live-evidence-expired-by-concurrent-prepare,
  round-result-finalization-crash-window,
  stage-approval-post-transition-resume-gap,
  validation-recurrence-counts-duplicate-issues
- accepted: all 7 (applied unattended per user directive; the 335/336
  collision, the self-constructed `ReviewLearningService` in
  `create_review_learning_registry`, and the cross-iteration issue
  flattening in `get_recurring_issue_summary` were verified in the
  repository before applying)
- declined: none
- resolution_notes: The evidence migration is renumbered to
  `337_plan_review_evidence.sql` (335/336 exist;
  `MigrationRunner._discover_migrations` hard-errors on duplicates) across
  3.1 prose, targets, and 3.1.5. §5.2 gains
  `src/gobby/mcp_proxy/tools/review_learning.py` +
  `tests/mcp_proxy/tools/test_review_learning.py` as targets, the factory
  signature change to accept the shared service, and a 5.2.3 identity test
  across the review-learning registry and both tasks contexts. The
  `plan_review_evidence` row gains manifest-application checkpoint columns
  (digest, canonical payload, pending/applied state, prior result,
  applied-at) driving a durable state machine: applied+identical-digest
  returns the stored result without touching the file (later coordinator
  M1 drift can never masquerade as a failed apply), pending recovery
  re-applies idempotently from the stored canonical payload across both
  crash boundaries of the atomic replacement, different digest refuses in
  every state (3.1, 3.1.7). Preparation is serialized under the extended
  per-plan-path lock with a per-plan active-attempt CAS: same-token
  preparation is idempotent, a different token against a live unfinalized
  attempt refuses, and expiration requires a provably dead attempt —
  spawn/bind failure, terminal bound run, or expired pre-bind lease (3.1,
  3.1.4). Round-result persistence and evidence finalization commit in one
  PostgreSQL transaction on the stage-native surface (fences and
  approvals), with same-round replacement an atomic swap that preserves
  the original finalized fence on failure; the interactive file/DB
  boundary is bridged by a preparation-time reconciliation checkpoint that
  finalizes any unfinalized row referenced by a durable V1 entry before
  orphan cleanup (3.1, 3.1.4, 3.2, 3.2.5, 6.1, 6.1.5). Stage-native
  approval fuses the stage transition, approval evidence reference,
  finalization, and a durable mint-pending record into that same atomic
  commit, and a lost-response `approve_review` retry after stage
  advancement is recognized from the durable approval record instead of
  failing authorization; crash/restart acceptance covers every adjacent
  approval-step boundary (6.2, 6.2.5); the two surfaces now share the
  logical commit order with atomicity set by the storage boundary (3.2).
  Validation recurrence is redefined as distinct failing iteration IDs
  with within-iteration dedupe and iteration-provenance grouping —
  `src/gobby/tasks/validation_history.py` joins the 4.1 targets, and
  4.1.2 pins that three duplicates in one failing iteration never satisfy
  the threshold (4.1, 4.1.2).

**Round 5** `kind: verification`

- reviewer_run: 35b74526-ee9a-4401-ae91-4e8d9d52e032
- reviewer_session: 149b6a09-841f-458f-95b7-20544a1f9436
- verdict: needs_review
- findings: 8 (8 blocking) — review-evidence-lifecycle-checkpoints-incomplete,
  task-mapping-freshness-boundary-missing,
  stage-transition-transaction-owner-target-omission,
  validation-occurrence-source-review-undefined,
  validation-recorder-missing-class-recall-dependency,
  mutable-description-is-mint-authority,
  mint-backfill-wire-contract-missing,
  interactive-reconciliation-reference-validation-missing
- accepted: all 8 (applied unattended per user directive; the
  canonical-ID-only `PLAN_HEADING_REGEX` plus the parser's fence-aware
  all-heading machinery, the `_transitions_facade.py` signatures without
  payload parameters, `build_occurrence_key(source_review,
  finding_fingerprint)`, and unrestricted `update_task` description
  replacement were verified in the repository before applying)
- declined: none
- resolution_notes: The `plan_review_evidence` row now carries every
  durable lifecycle record — one-way `finalized_at`/`expired_at` states
  (CHECK: at most one), a `lease_expires_at` pre-bind/interactive
  liveness bound (`EVIDENCE_LEASE_SECONDS`, default 7200, cleared on run
  bind), the canonical `round_result` payload written atomically with
  finalization on both surfaces, and the approval checkpoint
  (`approval_result`, `approved_at`, `lesson_mint_status`
  pending/minted/failed/none, `lesson_mint_detail`) backing the fused
  approval commit and lost-response replay; no `task_stage_states` field
  is repurposed (3.1, 3.1.4, 6.2). Evidence sectioning is redefined on
  the plan parser's fence-aware all-heading boundary model — canonical
  headings key by `section_id`, named noncanonical headings (Task
  Mapping, M1, V1) key by normalized title — so `COORDINATOR_OWNED_SECTIONS`
  names match their own manifest entries and a Task Mapping mutation can
  never invalidate a reviewed-section hash (3.1, 3.1.4).
  `src/gobby/storage/tasks/_transitions_facade.py` joins the 6.1/6.2
  targets: `TaskTransitionsMixin.reject_review`/`approve_review` gain the
  findings/evidence payloads and forward them to the `_transitions.py`
  stage-transition owner inside one ambient transaction, with atomicity
  acceptance exercised through the public task-manager path (6.1, 6.1.5,
  6.2, 6.2.5). Validation recording pins canonical
  `source="task-validation"` and task-scoped
  `source_review="task-validation:<task_uuid>"` so byte-identical
  findings from two tasks form distinct occurrences of one pattern,
  asserted end-to-end in 7.2.1 (4.2, 7.2.1); 4.2 additionally depends on
  2.1 for `list_check_keys`. Mint authority moves off mutable
  `task.description`: the canonical findings payload persists in
  `round_result` at finalization, description fences are projections,
  `round_diff` reads only finalized evidence rows, and 6.2.7 pins
  description-mutation immunity (3.1, 6.1, 6.2). The backfill is the
  named gobby-tasks-ops tool `backfill_plan_review_lessons(task_id,
  stage)` with a pinned per-state wire contract and a registry-level test
  (6.2, 6.2.8). Interactive reconciliation is lineage-validated (project,
  normalized plan path, parent session, round number, recorded plan_hash)
  with deterministic failure on any mismatch and negative tests for all
  five mismatch axes (3.1, 3.1.4).

**Round 6** `kind: verification`

- reviewer_run: 0320c213-df53-4214-a113-5995bf6483aa
- reviewer_session: 605317ee-f90d-4cc9-b44f-41e746f4c312
- verdict: needs_review
- findings: 6 (6 blocking) — coordinator-owned-section-key-mismatch,
  reviewed-preamble-outside-section-manifest,
  noncanonical-section-key-collision, interactive-lease-expires-live-review,
  interactive-round-result-checkpoint-wire-undefined,
  finalized-rejection-replacement-unreachable
- accepted: all 6 (applied unattended per user directive; the dotted-ID
  grammar making `## M1 Task Manifest`/`## V1 Plan Changelog` canonical
  (`DOTTED_ID_PATTERN`), the level-2..6-only `_HEADING_LINE_RE`, and
  `_handle_noncanonical_heading` appending duplicate framing headings
  without dedupe were verified in the repository before applying)
- declined: none
- resolution_notes: Manifest identity is one exact `manifest_key` function —
  canonical headings key by dotted `section_id` (so `M1`/`V1`), ID-less
  headings by normalized title, and the reserved `__preamble__` entry
  covers every pre-heading byte for total coverage —
  `COORDINATOR_OWNED_SECTIONS` becomes ("Task Mapping", "M1", "V1") with
  exact-key assertions and preamble-drift refusal (title, Plan ID,
  pre-heading bytes) in 3.1.2/3.1.4; duplicate manifest keys, including a
  duplicated owned heading, fail preparation with a deterministic
  duplicate-key error (3.1, 3.1.2). Two-phase run binding extends to the
  interactive surface: the coordinator binds the `spawn_agent` run id via
  `bind_evidence_run`, the lease bounds only pre-bind rows, a bound
  interactive row expires only on a terminal run with no durable V1
  checkpoint, and an active run outliving the lease survives preparation
  (3.1, 3.1.4, 3.1.8, 3.2, 3.2.2). The interactive round checkpoint wire
  format is pinned: each V1 round entry embeds the canonical fenced JSON
  `{evidence_id, round_number, plan_hash, session_id, round_result}`
  rendered by the new `render_v1_round_checkpoint` gobby-plans tool and
  parsed by its `review_evidence.py` counterpart during reconciliation,
  with lossless `round_result` recovery and
  mint-from-recovered-attestations asserted (3.1, 3.1.4, 3.2, 3.2.5).
  Same-round replacement is removed as unreachable — the rejection commit
  atomically advances the stage, so a same-full-attempt-token repeat
  `reject_review` is now an idempotent lost-response replay returning the
  recorded finalized result read-only (mirroring 6.2's approval-checkpoint
  replay), and changed findings require a new review round (3.1, 3.1.6,
  6.1, 6.1.1, 6.1.5).

**Round 7** `kind: verification`

- reviewer_run: d9c6dc4f-a60c-4433-ae59-c360a7b5fa1b
- reviewer_session: 2ae28e23-bc29-4111-87c6-95281c0ef6be
- verdict: needs_review
- findings: 4 (4 blocking) — round6-duplicate-key-acceptance-gap,
  shared-approve-review-plan-evidence-scope,
  post-manifest-apply-round-result-recovery-gap,
  validation-e2e-dependency-missing
- accepted: all 4 (applied unattended per user directive; the shared use
  of `approve_review`/`reject_review` across all review stages and the
  7.2 dependency chains were re-verified against the plan and prior
  exploration before applying)
- declined: none
- resolution_notes: 3.1.2 now exercises duplicate-key failure across every
  key shape — duplicate canonical section ID, duplicated `## M1 Task
  Manifest`, duplicated `## V1 Plan Changelog`, duplicate ordinary
  noncanonical title, and duplicate `Task Mapping` — each failing
  preparation with the deterministic error naming that exact key and no
  occurrence-qualified identity generated (3.1.2). The evidence-bound
  review path gains an explicit activation boundary: it activates only
  for planning-stage reviews (fail-closed there — bound `evidence_id` +
  typed manifest payload required), while `approve_review` and
  `reject_review` for development, expansion, document, epic-QA, and PR
  review stages keep their existing transitions byte-for-byte, asserted
  through the registry and facade in new 6.2.9 and extended 6.1.3 (6.1.3,
  6.2, 6.2.9). The crash window between approval steps 1 and 2 is closed
  by the durable **pre-finalization approval intent**:
  `apply_plan_review_manifest` now takes the complete validated approval
  `round_result` and persists it into the row's `round_result` column in
  the same durable write as its `pending` checkpoint (digest covers the
  full payload), `render_v1_round_checkpoint` and
  `finalize_plan_review_evidence` resolve from / must equal the intent so
  steps 2–4 are pure functions of durable state, reconciliation completes
  intent-bearing rows instead of expiring them (`applied` → finalize
  losslessly; `pending` → converge the apply first, reviewed-drift
  refusal invalidating the round), interactive expiration additionally
  requires no durable intent, and 3.2.5 gains the process-restart case
  reconstructing the exact V1 checkpoint from the intent, finalizing
  once, and minting from recovered attestations (3.1, 3.1.4, 3.1.7, 3.2,
  3.2.2, 3.2.5, 6.2). 7.2 adds the missing dependency on 4.2 so the
  end-to-end suite is sequenced after the validation recorder contract it
  verifies (7.2).

**Round 8** `kind: verification`

- reviewer_run: 9af9d00e-9a36-420a-8241-f956b03afc6f
- reviewer_session: b416b96e-7f33-4f96-8d22-d3d4f4b30667
- verdict: needs_review
- findings: 2 (2 blocking) — round8-pending-intent-expiration-deadlock,
  round8-epic-qa-activation-regression-gap
- accepted: both (applied unattended per user directive; the deadlock was
  re-verified against the reconciliation, expiration, and CAS bullets —
  drift-invalidated pending-intent rows were declared expirable while the
  expiration predicate forbade expiring any intent-bearing row with no
  transition clearing the intent — and the 6.2.9 stage/operation gap was
  re-verified against the activation-boundary enumeration)
- declined: none
- resolution_notes: The pending-intent drift deadlock is closed by one
  atomic **pending-plus-drift invalidation transition** on the shared
  apply state machine: a `pending` retry (live or during reconciliation
  convergence) that finds reviewed-section drift stamps
  `manifest_state=revoked` and clears the row's `round_result` to null in
  a single durable write (`manifest_payload`/`manifest_digest` retained
  for forensics, plan bytes untouched), revoking the pre-finalization
  approval intent so the row falls back under the standard expiration
  predicate and the active-attempt CAS is never blocked; `revoked` is
  terminal — every later invocation refuses deterministically and
  re-review is the only path; first-application drift (`manifest_state`
  null) still refuses with no checkpoint write and no intent. Pinned in
  the column semantics, apply clauses (iii)/(iv), reconciliation and
  expiration bullets, the stale-write guard, 3.1.4 (reconciliation-drift
  invalidation + expiration + CAS-unblocked case), 3.1.7 (atomic
  revocation write + post-revocation refusals + expirability), and 3.2.5
  (crash-inside-step-1 plus drift restart scenario, distinguished from
  completed-step-1 resume) (3.1, 3.1.4, 3.1.7, 3.2.5). The non-planning
  regression matrix is completed: 6.2.9 now exercises **both**
  `approve_review` and `reject_review` for development, expansion,
  document, **epic QA**, and PR/trajectory through both the registry and
  the `TaskTransitionsMixin` facade paths, asserting unchanged
  stage-transition results and the absence of plan-evidence resolution,
  manifest application, approval checkpoint, and lesson mint, with the
  planning fail-closed cases retained; 6.1.3 references that matrix for
  the rejection side (6.1.3, 6.2.9).

**Round 9** `kind: verification`

- reviewer_run: a4f7b340-ea7b-46e3-84e5-fcebed2cb290
- reviewer_session: 019f8bfb-a908-7722-9355-b62407c58619
- verdict: needs_review
- findings: 1 (1 blocking) — round9-unserialized-review-learning-registry-edit
- accepted: yes (applied unattended per user directive; verified against
  the section headings — `src/gobby/mcp_proxy/tools/review_learning.py` is
  targeted by 2.1, 5.2, and 7.1; 2.1↔5.2 and 2.1↔7.1 are serialized
  transitively via 5.2←5.1←2.1 and 7.1←3.2←3.1←2.1, but no path connects
  5.2 and 7.1 in either direction, so they were parallel-eligible on the
  shared file)
- declined: none
- resolution_notes: §7.1's heading gains the explicit 5.2 dependency
  (`depends: 3.2, 5.2`), and its body records the rationale: 5.2 changes
  the `create_review_learning_registry` factory signature in the shared
  file, 7.1 registers `retire_review_lesson` on the resulting registry,
  and the expansion compiler emits dependency edges only from explicit
  manifest `depends_on` entries — phase numbering does not order them. The
  `## M1 Task Manifest` entry for 7.1 must carry both the 3.2 and 5.2
  edges when the manifest is authored at approval (§7.1).

**Round 10** `kind: verification`

- reviewer_run: 8d994b5e-785b-42e0-af63-dee696d28a8a
- reviewer_session: 019f8c09-4684-7693-99c2-bbfa7bffcd1a
- verdict: needs_review
- findings: 1 (1 blocking) — round10-interactive-approval-recovery-incomplete
- accepted: yes (applied unattended per user directive; verified against
  the plan text — 3.2 promised the next preparation's reconciliation
  re-derives approval steps 2–4 from the durable intent, but the 3.1
  reconciliation of an `applied`-intent row only finalized `round_result`
  with no V1 persistence and no mint, and `lesson_mint_status` was
  documented null until a stage-native approval commits, so a crash after
  step 1 with no resuming coordinator, or after step 3 before step 4,
  left an approved finalized round with no V1 entry and/or no lessons and
  no durable marker that recovery remained)
- declined: none
- resolution_notes: `lesson_mint_status`/`lesson_mint_detail` extend to
  interactive approvals — every finalization path recording an approval
  verdict (direct step 3, checkpoint reconciliation, intent drain) stamps
  `pending` atomically with `finalized_at`, and the new gobby-plans
  operation `checkpoint_plan_review_lesson_mint(evidence_id, status,
  detail)` advances `pending`→`minted`/`failed`/`none` for finalized
  interactive approval rows only, idempotently. The V1 checkpoint is the
  enforced durable step-2 marker: interactive finalization refuses while
  the changelog lacks the round's checkpoint fence, so a finalized
  interactive round without its V1 entry cannot exist. Preparation is the
  single recovery owner: the `applied`-intent reconciliation becomes the
  interactive approval recovery drain (persist the missing V1 checkpoint
  byte-equal to the rendered form before finalizing, finalize losslessly
  with `pending` stamped), and while any same-lineage interactive
  approval row is `pending`, `prepare_plan_review_round` refuses
  new-round creation with the deterministic `pending_lesson_mint` result
  carrying the rows' durable `round_result` payloads until the mint is
  completed and checkpointed — incomplete approvals drain before the next
  round exists. Acceptance: new 3.1.9 (mint-status lifecycle, V1 gate,
  drain, refuse-gate, checkpoint-op transitions, restart survival); 3.1.4
  `applied`-intent case now asserts V1 persistence + `pending` stamp;
  3.2.2 commit order gains the V1-gated `pending`-stamped finalize and
  the mint-status checkpoint; 3.2.5 gains the crash-after-step-3 case
  (durable `pending` → refuse-gate → exactly-once mint with a simulated
  mint-crash re-run adding no duplicate occurrence) and pins recovery
  completeness at every crash point to canonical V1 bytes present plus
  status having left `pending` (3.1, 3.1.4, 3.1.9, 3.2, 3.2.2, 3.2.5).

**Round 11** `kind: verification`

- reviewer_run: 67297413-70ad-4f40-8a01-1f30a248246e
- reviewer_session: 019f8c2f-c9b4-7862-a558-fefdc2a473ac
- verdict: needs_review
- findings: 2 (2 blocking) — round11-pending-apply-v1-finalize-deadlock,
  round11-pre-v1-crash-contract-contradiction
- accepted: yes, both (applied unattended per user directive; verified
  against the plan text — the `manifest_state=pending` no-drift
  reconciliation branch said "converges through its own state machine and
  then finalizes" while round 10's V1 gate refuses finalization without
  the checkpoint that a pre-step-2 crash never persisted, and the
  intent-carrying row is excluded from the expiration predicate, so it
  wedged the active-attempt CAS permanently; separately, 3.2's plan-skill
  bullet still stated "crash before persistence = incomplete round — no
  proof, no mint" unscoped, contradicting the durable-intent recovery the
  same section mandates for approvals)
- declined: none
- resolution_notes: the pending-intent no-drift convergence now routes
  through the same interactive approval recovery drain as an
  `applied`-intent row — convergence checkpoints `applied`, then the V1
  checkpoint is rendered from the durable intent and persisted under the
  per-plan-path lock, then a lossless finalize stamps
  `lesson_mint_status=pending` (never a bare finalize, which the V1 gate
  would refuse); the no-coordinator recovery sentence in 3.2 names the
  pending-convergence step explicitly. The incomplete-round rule is
  scoped to rejection rounds and approvals that crash **before** step 1
  records the durable intent; any approval crash after the intent write —
  including one that dies while the apply is still `pending` — recovers
  from the durable intent, never abandoned. Acceptance: 3.1.4 gains the
  no-drift `pending`-crash case (converge → drain → V1 byte-equal →
  lossless finalize with `pending` → mint checkpoint completable →
  re-preparation proceeds); 3.2.5 gains the same case end-to-end
  (`pending_lesson_mint` surfaced, mint checkpoints `minted`,
  re-preparation proceeds); 3.2.2 asserts the scoped incomplete-round
  rule (3.1, 3.1.4, 3.2, 3.2.2, 3.2.5).

**Round 12** `kind: verification`

- reviewer_run: 4c716a30-83ea-46ec-bf19-77afa75d9194
- reviewer_session: 019f8c37-84aa-7200-91ac-578019ece161
- verdict: approved
- findings: 0
- accepted: n/a
- declined: n/a
- resolution_notes: converged. The reviewer verified round 11's
  pending-apply drain routing and scoped incomplete-round rule against
  all standing crash/recovery, expiration, replay, and activation-boundary
  text, re-ran the every-pair shared-target-file check over the declared
  dependency graph, and found no contradiction, unhandled state, missing
  acceptance coverage, or unserialized shared-file edit. Plan approved
  after 12 adversarial rounds.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Extend lesson taxonomy with plan and validation source kinds, lesson-domain tagging, and class-scoped identity
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: New source kinds validate, domain derivation is total and fail-closed, and class-namespaced identity persists dual-class lessons with separate occurrence counts.
  labels:
  - covers:feedback-lesson-loop:1.1:1.1.1
  - covers:feedback-lesson-loop:1.1:1.1.2
  - covers:feedback-lesson-loop:1.1:1.1.3
  - covers:feedback-lesson-loop:1.1:1.1.4
  - covers:feedback-lesson-loop:1.1:1.1.5
  - covers:feedback-lesson-loop:1.1:1.1.6
  implementation_domain: backend
  tdd: true
  source_section: '1.1'
- title: Apply the domain partition to every code-domain recall path
  category: code
  task_type: feature
  depends_on: ['1.1']
  validation_criteria: Semantic recall and both file-recall paths exclude plan-domain lessons via the centralized constant, and plan-domain recall requires positive opt-in.
  labels:
  - covers:feedback-lesson-loop:1.2:1.2.1
  - covers:feedback-lesson-loop:1.2:1.2.2
  - covers:feedback-lesson-loop:1.2:1.2.3
  implementation_domain: backend
  tdd: true
  source_section: '1.2'
- title: Deterministic class recall tool and check-key enumeration
  category: code
  task_type: feature
  depends_on: ['1.2']
  validation_criteria: recall_review_lessons_by_class enforces domain-consistent predicates with deterministic ranking, and list_check_keys enumerates the complete per-class key set.
  labels:
  - covers:feedback-lesson-loop:2.1:2.1.1
  - covers:feedback-lesson-loop:2.1:2.1.2
  - covers:feedback-lesson-loop:2.1:2.1.3
  - covers:feedback-lesson-loop:2.1:2.1.4
  - covers:feedback-lesson-loop:2.1:2.1.5
  implementation_domain: backend
  tdd: true
  source_section: '2.1'
- title: Push injection rules for plan and QA agents
  category: config
  task_type: feature
  depends_on: ['2.1']
  validation_criteria: All four turn_start injection rules load and fire for every scoped agent slug with deduplicated review-guidance injection.
  labels:
  - covers:feedback-lesson-loop:2.2:2.2.1
  - covers:feedback-lesson-loop:2.2:2.2.2
  - covers:feedback-lesson-loop:2.2:2.2.3
  - covers:feedback-lesson-loop:2.2:2.2.4
  - covers:feedback-lesson-loop:2.2:2.2.5
  tdd: true
  source_section: '2.2'
- title: prepare_plan_review_round tool and plan_review_evidence store
  category: code
  task_type: feature
  depends_on: ['2.1']
  validation_criteria: Evidence rounds prepare, bind, apply, finalize, and recover through every documented crash point with the interactive mint-status lifecycle enforced.
  labels:
  - covers:feedback-lesson-loop:3.1:3.1.1
  - covers:feedback-lesson-loop:3.1:3.1.2
  - covers:feedback-lesson-loop:3.1:3.1.3
  - covers:feedback-lesson-loop:3.1:3.1.4
  - covers:feedback-lesson-loop:3.1:3.1.5
  - covers:feedback-lesson-loop:3.1:3.1.6
  - covers:feedback-lesson-loop:3.1:3.1.7
  - covers:feedback-lesson-loop:3.1:3.1.8
  - covers:feedback-lesson-loop:3.1:3.1.9
  implementation_domain: backend
  tdd: true
  source_section: '3.1'
- title: Interactive plan-loop skill and agent contracts
  category: config
  task_type: feature
  depends_on: ['3.1']
  validation_criteria: Plan-loop skills carry the evidence, recall, recording, and recovery contracts, and interactive approval integration mints exactly once per approved round.
  labels:
  - covers:feedback-lesson-loop:3.2:3.2.1
  - covers:feedback-lesson-loop:3.2:3.2.2
  - covers:feedback-lesson-loop:3.2:3.2.3
  - covers:feedback-lesson-loop:3.2:3.2.4
  - covers:feedback-lesson-loop:3.2:3.2.5
  tdd: true
  source_section: '3.2'
- title: Structured validation issues end-to-end and success-path candidates
  category: code
  task_type: feature
  depends_on: ['1.1']
  validation_criteria: Validator issues persist through the real validation call path, and recurring-issue candidates surface only at configured thresholds with concrete anchors.
  labels:
  - covers:feedback-lesson-loop:4.1:4.1.1
  - covers:feedback-lesson-loop:4.1:4.1.2
  - covers:feedback-lesson-loop:4.1:4.1.3
  - covers:feedback-lesson-loop:4.1:4.1.4
  - covers:feedback-lesson-loop:4.1:4.1.5
  implementation_domain: backend
  tdd: true
  source_section: '4.1'
- title: Development-discipline recording instruction
  category: config
  task_type: feature
  depends_on: ['4.1', '2.1']
  validation_criteria: The development-discipline skill instructs exactly one task_validation lesson per task and skill tests assert the recording contract text.
  labels:
  - covers:feedback-lesson-loop:4.2:4.2.1
  - covers:feedback-lesson-loop:4.2:4.2.2
  tdd: false
  source_section: '4.2'
- title: Epic finding schema and two-class recording
  category: config
  task_type: feature
  depends_on: ['2.1', '4.1']
  validation_criteria: The epic-review schema rejects incomplete findings, and one confirmed epic finding mints qa-miss and validation-miss lessons with separate identities and occurrence counts.
  labels:
  - covers:feedback-lesson-loop:5.1:5.1.1
  - covers:feedback-lesson-loop:5.1:5.1.2
  tdd: true
  source_section: '5.1'
- title: Validator prompt lesson injection
  category: code
  task_type: feature
  depends_on: ['5.1']
  validation_criteria: validation-miss lessons render empty-safe in the validator prompt via one shared ReviewLearningService without breaking the structured-issues output contract.
  labels:
  - covers:feedback-lesson-loop:5.2:5.2.1
  - covers:feedback-lesson-loop:5.2:5.2.2
  - covers:feedback-lesson-loop:5.2:5.2.3
  - covers:feedback-lesson-loop:5.2:5.2.4
  implementation_domain: backend
  tdd: true
  source_section: '5.2'
- title: Structured adversary findings on reject_review
  category: code
  task_type: feature
  depends_on: ['3.1']
  validation_criteria: Structured findings round-trip losslessly through the rejection fence with server-side evidence resolution and the free-text fallback unchanged.
  labels:
  - covers:feedback-lesson-loop:6.1:6.1.1
  - covers:feedback-lesson-loop:6.1:6.1.2
  - covers:feedback-lesson-loop:6.1:6.1.3
  - covers:feedback-lesson-loop:6.1:6.1.4
  - covers:feedback-lesson-loop:6.1:6.1.5
  implementation_domain: backend
  tdd: true
  source_section: '6.1'
- title: Idempotent mint with workflow-owned backfill
  category: code
  task_type: feature
  depends_on: ['6.1', '3.2']
  validation_criteria: Approval-time mint never blocks the verdict, backfill is idempotent and workflow-owned, and non-planning stage reviews are byte-for-byte unaffected.
  labels:
  - covers:feedback-lesson-loop:6.2:6.2.1
  - covers:feedback-lesson-loop:6.2:6.2.2
  - covers:feedback-lesson-loop:6.2:6.2.3
  - covers:feedback-lesson-loop:6.2:6.2.4
  - covers:feedback-lesson-loop:6.2:6.2.5
  - covers:feedback-lesson-loop:6.2:6.2.6
  - covers:feedback-lesson-loop:6.2:6.2.7
  - covers:feedback-lesson-loop:6.2:6.2.8
  - covers:feedback-lesson-loop:6.2:6.2.9
  implementation_domain: backend
  tdd: true
  source_section: '6.2'
- title: retire_review_lesson operation
  category: code
  task_type: feature
  depends_on: ['3.2', '5.2']
  validation_criteria: Retirement requires project scope and evidence, retags confirmed to stale under the advisory lock, and retired lessons vanish from both deterministic recalls.
  labels:
  - covers:feedback-lesson-loop:7.1:7.1.1
  - covers:feedback-lesson-loop:7.1:7.1.2
  implementation_domain: backend
  tdd: true
  source_section: '7.1'
- title: End-to-end coexistence and promotion regression suite
  category: test
  task_type: task
  depends_on: ['5.2', '6.2', '4.2']
  validation_criteria: Per-class promotion for all five reserved classes, cross-domain coexistence, and check-key convergence regressions all pass in one suite.
  labels:
  - covers:feedback-lesson-loop:7.2:7.2.1
  - covers:feedback-lesson-loop:7.2:7.2.2
  - covers:feedback-lesson-loop:7.2:7.2.3
  tdd: false
  source_section: '7.2'
```
