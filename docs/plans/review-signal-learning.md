# Review Signal Learning

## Context

Gobby has several surfaces that produce verified findings — CodeRabbit triage, adversarial
`code-reviewer`, `holistic-review`/`qa-reviewer` rejections, and the `nightly-fixes`
linter/test-fixer — and today every one of them throws the knowledge away after the fix.
The same class of problem gets re-discovered on the next PR with no memory, no sibling
sweep, and no path to enforcement.

This adds a **source-agnostic review-signal learning loop**. A *confirmed* signal becomes a
durable lesson (a memory), a *repeated* lesson auto-files a tracked work item to **build** a
guardrail, and the existing memory recall resurfaces relevant lessons in future turns.
CI/static-analysis/test signals feed it too — not just human review comments.

### The model

```text
verified finding / fixed CI failure  -> record review-signal lesson memory  (evidence)
threshold crossed                     -> auto-create/update guardrail IMPLEMENTATION task
task completed by an engineer          -> the actual guardrail ships
existing memory recall                 -> resurfaces relevant lessons on later turns
```

A **task is not the guardrail.** It is the durable, evidence-backed work item that says
"build or update the guardrail." The task's `guardrail_target` names what to build; the
guardrail ships by completing the task. Tasks are the queue + audit trail; memories are the
evidence.

### Four separate identities (the conceptual backbone)

Keep these distinct or the system gets noisy:

- `source_review` — **provenance**: which run/review produced the signal.
- `finding_fingerprint` — **finding identity**: stable, line-number-agnostic id of *this
  finding* (producer-supplied per SARIF/reviewdog, else derived).
- `occurrence_key` = `source_review` + `finding_fingerprint` — **dedupe + promotion identity**:
  one real occurrence.
- `pattern_id` — **reusable lesson bucket**: the generalized class a guardrail is built for;
  spans many fingerprints.

### Design inspirations (prior art)

- **SARIF / GitHub code scanning** — `partialFingerprints` prevent duplicate alerts across
  runs and edits → `finding_fingerprint`.
- **reviewdog RDFormat** — tool-agnostic diagnostic shape (location, severity, rule code, URL,
  range, suggestion) → the `finding` payload.
- **SonarQube issue lifecycle** — `Accepted`/`False positive` states → a `no-fix-policy`
  decision tunes the rule/profile, it is not a product defect.
- **Danger JS** — codifies team norms as CI checks → `rule`/`workflow`/`pipeline`/`validation`
  guardrail targets ("project norm check").
- **Checklist-promotion skills** (update-review-checklist / review-memory-promotion / upskill)
  → `checklist` is a first-class `guardrail_target`.

### Build on existing rails — one new tool, deterministic core

- **Lesson storage** → `gobby-memory`, `memory_type="pattern"` + tags
  (`src/gobby/memory/manager.py`, `src/gobby/storage/memories.py`).
- **Recall/injection** → `MemoryRecallRunner` already injects relevant memories at `turn_start`
  through an LLM relevance gate (`src/gobby/memory/recall.py`, rule
  `memory-recall-on-prompt.yaml`). Lessons-as-memories ride this. **No new injection rule in v1.**
- **Search** → reuse `gobby-memory.search_memories` with a tag filter. No new search tool.
- **Guardrail work items** → `gobby-tasks` with labels (`src/gobby/storage/tasks/_models.py`).
  No new task type.

The only new code is **one deterministic tool**, `record_review_lesson`, plus the
service/fingerprint/promotion logic. `search_review_lessons`, `promote_review_lesson`, and
`summarize_review_batch` are **dropped from v1**. **The core recorder takes no `llm_service`**;
producers/agents do any generalizing/summarizing while they still hold full context.

### Two key constraints

1. `create_memory` (the MCP tool) only exposes `content`/`memory_type`/`tags`/`session_id`,
   not the `metadata` dict — so a lesson is a **markdown content template + tags**. The service
   writes via `MemoryManager` **directly** (not the MCP `create_memory` tool) to bypass that
   tool's "speculative-memory redirection" (`src/gobby/mcp_proxy/tools/memory.py`).
2. **A CI/test/static-analysis failure alone must NOT create a lesson** — only after the root
   cause is fixed (verified) or a reviewer rejects with a concrete reusable pattern.

---

## Tool surface

Collapsed signature — diagnostic/pattern detail lives inside `finding` so the API ages well:

```python
record_review_lesson(
    source_kind,                # validated: review_comment|ci_check|agent_review|qa_rejection|static_analysis|test_failure
    source,                     # free-form: coderabbit|copilot|claude|github-actions|qa-reviewer|ruff|mypy|pytest|bandit|...
    source_review,              # stable run/review id (provenance)
    decision,                   # validated: confirmed|no-fix-policy|stale|invalid
    finding,                    # dict, see below
    evidence,                   # dict: verified-fix ref (commit/changes id) + supporting detail
    session_id=None,            # resolves project_id; preserved as source_session_id
    repo=None,                  # descriptive provenance only, NOT storage scope
    language=None,
    risk="medium",              # low|medium|high
)
```

**`finding` payload** (producer owns generalization — it has the context):

| Field | Required | Meaning |
| --- | --- | --- |
| `title` / `message` | yes | human-readable finding |
| `pattern_id` | preferred | reusable lesson bucket. If omitted, derive **only** from normalized `lesson_type` + `principle`; if still underivable, store **non-promotable** (won't count toward thresholds) |
| `principle`, `root_cause`, `prevention` | preferred | the reusable lesson content |
| `query_hints` | optional | terms for the gcode sibling sweep |
| `lesson_type` | optional | free-form (e.g. `durable-writes`); seed examples in the skill, **no enum** |
| `finding_fingerprint` | optional | native id (SARIF/reviewdog); else derived line-agnostically |
| `guardrail_target` | optional | producer recommendation; else ladder picks a deterministic default |
| `rule_id`, `rule_url`, `severity`, `path`, `start_line`, `end_line`, `symbol`, `suggestion`, `diagnostic_format` | optional | reviewdog/SARIF diagnostic core; diagnostic format is `raw`, `sarif`, `rdjson`, or `review_comment` |

**`session_id` / project resolution:** follow the memory-tool pattern — resolve `project_id`
from `session_id` when present, else current project context; pass `source_session_id` through
to `MemoryManager`. `repo` is a provenance tag, not a scope.

---

## Tagging & identity rules

- **Bounded, filter-safe tags only.** High-cardinality values are slugged/short-hashed in tags;
  **full values live in markdown content**: `pattern:<pattern_key>`, `fingerprint:<short_hash>`,
  `occurrence:<short_hash>`, `rule:<slug>`, `repo:<slug>`. `pattern_key` is a bounded
  slug/short-hash of `pattern_id`, used identically in the memory tag **and** the guardrail-task
  label so lookups join cleanly.
- **Occurrence idempotency via tag preflight, not content-hash.** Before writing, preflight
  `list_memories(tags_all=["review-lesson", "occurrence:<short_hash>"])`; if present, skip and
  return the existing lesson. (Content-hash dedup is brittle to evidence wording/format/timestamp
  drift.)
- **Promotion counting:** distinct `occurrence_key`s sharing a `pattern_id`. Non-promotable
  lessons never count.

Stable tags also applied: `review-lesson`, `source-kind:<kind>`, `source:<src>`,
`lang:<language>`, `lesson-type:<value>`, `severity:<sev>`, `guardrail:<status>`, plus
`confirmed` or `no-fix-policy`, and `non-promotable` when applicable.

---

## Decision routing & promotion ladder (deterministic, decision-aware)

`guardrail_target` ∈ `helper | test | checklist | rule | workflow | pipeline | validation | tool-config`.
Producer may recommend one in `finding`; otherwise the ladder assigns the default shown.

| Decision | Occurrences | `guardrail_target` (default) → task |
| --- | --- | --- |
| confirmed | 1 | lesson memory only (no task) |
| confirmed | 2 | `test` (or producer-recommended `helper`/`checklist`) |
| confirmed | 3+ | `validation` (or `rule`/`workflow`/`pipeline`) |
| confirmed | `risk=high` | `rule` — immediate, regardless of count |
| no-fix-policy | 1 | policy lesson memory only |
| no-fix-policy | 2+ | `checklist` or `tool-config` **only** (tune rule/profile) |
| stale / invalid | — | skipped (no-op) |

**Task category derives from `guardrail_target`** (not `research`):

| target | category |
| --- | --- |
| `helper`, `validation` | `code` |
| `test` | `test` |
| `rule`, `workflow`, `pipeline`, `tool-config` | `config` |
| `checklist`, skill-only | `docs` (or `manual`) |

**Idempotent task create/update — exact behavior (no audit-comment API exists):**
locate the open task via `list_tasks(project_id=..., label="pattern:<pattern_key>", closed=False)`,
filter to those also labeled `review-learning` + `guardrail`. If none, `create_task`. If one
exists and the tier/target escalates, **`update_task`** its `description`, `validation_criteria`,
and `labels` (bump `target:<…>`, append the new evidence memory ID) — never spawn a duplicate.

**Guardrail task fields** (via `LocalTaskManager`): `claim=False`; title
`Guardrail: <lesson-type> — <pattern_id> (<N>×, target=<guardrail_target>)`; description carries
the full pattern_id/principle/root_cause/prevention, diagnostic locations, and **evidence memory
IDs**; `validation_criteria` = guardrail acceptance; labels `guardrail`, `review-learning`,
`pattern:<pattern_key>`, `lesson-type:<value>`, `target:<guardrail_target>`, `source:<src>`,
provenance `review-lesson:<source_review>`.

---

## Components

### 1. `review_learning` service package (new, deterministic core — no LLM)

Split to stay under the 1,000-line monolith rule:

- `src/gobby/review_learning/fingerprint.py` — native-fingerprint passthrough + deterministic
  line-agnostic derivation (`rule_id`/principle + `path` + `symbol`); `occurrence_key` + short-hash
  builders.
- `src/gobby/review_learning/lessons.py` — `finding` payload schema, `pattern_id` derivation +
  non-promotable fallback, content-template renderer, **bounded tag/slug builder**. Validated
  closed sets: `source_kind`, `decision`, `guardrail_target`. Free-form: `source`, `lesson_type`.
- `src/gobby/review_learning/promotion.py` — count distinct `occurrence_key`s per `pattern_id`,
  branch on `decision`, resolve target+tier, category mapping, `list_tasks` lookup, create-vs-update.
- `src/gobby/review_learning/service.py` — `ReviewLearningService(memory_manager, task_manager)`
  `.record(...)`: resolve project from `session_id` → normalize → fingerprint → occurrence
  preflight → decision/CI guards → write pattern memory via `MemoryManager` (with `project_id` +
  `source_session_id`) → promote → return `{lesson_id?, pattern_id, finding_fingerprint,
  occurrence_key, decision, promotable, tier, guardrail_target?, task_ref?, skipped_reason?}`.

### 2. `gobby-review-learning` MCP registry (new, one tool)

`src/gobby/mcp_proxy/tools/review_learning.py` —
`create_review_learning_registry(memory_manager, task_manager)` returning an
`InternalToolRegistry` named `gobby-review-learning` (pattern: `tools/metrics.py`), exposing the
collapsed `record_review_lesson` above. Registered in `setup_internal_registries()`
(`src/gobby/mcp_proxy/registries.py`) with `memory_manager` + `task_manager` (**no `llm_service`**).

### 3. `review-learning` skill (new — owns generic source policy)

`src/gobby/install/shared/skills/review-learning/SKILL.md` owns: the `finding` payload contract
(incl. producer-supplied `pattern_id`/`principle`/`root_cause`/`prevention`/`query_hints`), seed
`lesson_type` examples, decision/CI-guard record-vs-skip rules, the promotion ladder +
`guardrail_target` semantics, guardrail task wording, the instruction to record via
`gobby-review-learning`, and the **gcode sibling sweep** using `query_hints`. `metadata.gobby`
frontmatter for bundled-skill sync (`src/gobby/skills/sync.py`).

### 4. Wire v1 producers — inline calls while the producer has full context

Reviewers/agents call `record_review_lesson` **inline during their flow** (they hold the finding
context and can generalize `pattern_id`). No post-parsing of `reject_review` notes.

| Producer | File | Hook |
| --- | --- | --- |
| coderabbit | `skills/coderabbit/SKILL.md` | `REQUIRED SKILL: review-learning` + post-triage step: record each confirmed / no-fix-policy pattern |
| code-reviewer | `skills/code-reviewer/SKILL.md` | on confirming a material finding, record inline (`source_kind=agent_review`) |
| holistic-review | `skills/holistic-review/SKILL.md` | when a `request_changes` finding is a reusable pattern, record inline **before** the verdict (`source_kind=qa_rejection`) |
| qa-reviewer | `workflows/agents/qa-reviewer.yaml` | record the reusable pattern inline **before** `reject_review` (`source_kind=qa_rejection`) |
| nightly-linter | `workflows/agents/nightly-linter.yaml` | after a **verified** ruff/mypy/bandit fix, record (`source_kind=static_analysis`, fix commit in evidence) |
| nightly-test-fixer | `workflows/agents/nightly-test-fixer.yaml` | after a **verified** pytest fix (never the raw failure), record (`source_kind=test_failure`, fix commit in evidence) |

**Copilot** enters via pasted / GitHub review comments through the coderabbit-style flow
(`source=copilot`, `source_kind=review_comment`); a direct connector is deferred.

---

## Files

**New**
- `src/gobby/review_learning/{__init__,fingerprint,lessons,promotion,service}.py`
- `src/gobby/mcp_proxy/tools/review_learning.py`
- `src/gobby/install/shared/skills/review-learning/SKILL.md`

**Modified**
- `src/gobby/mcp_proxy/registries.py` — register `gobby-review-learning`
- `src/gobby/install/shared/skills/{coderabbit,code-reviewer,holistic-review}/SKILL.md` — hooks
- `src/gobby/install/shared/workflows/agents/{qa-reviewer,nightly-linter,nightly-test-fixer}.yaml` — hooks

No new injection rule (existing recall covers resurfacing in v1).

---

## Test Plan

Prefix every run with `GOBBY_TEST_PROTECT=1`; never run the full suite.

- `tests/review_learning/test_fingerprint.py` — same logical finding across line edits → same
  fingerprint; native passthrough; `occurrence_key` + short-hash determinism.
- `tests/review_learning/test_lessons.py` — `finding` dict parsing across diagnostic_formats;
  `pattern_id` derivation from `lesson_type`+`principle`; **non-promotable** fallback when
  underivable; bounded tag slugging for fingerprint/rule/repo/pattern; full values in content.
- `tests/review_learning/test_promotion.py` — distinct `occurrence_key` counting per `pattern_id`;
  confirmed ladder + high-risk; **no-fix-policy → checklist/tool-config only**; category mapping
  by target; **occurrence-tag preflight idempotency**; create-vs-`update_task` (description/
  validation_criteria/labels) on an existing open `pattern:<key>` task; non-promotable never counts.
- `tests/review_learning/test_ci_guard.py` — `ci_check`/`static_analysis`/`test_failure` without a
  verified-fix ref records nothing; with it, records.
- `tests/mcp_proxy/tools/test_review_learning.py` — collapsed signature (`finding` dict);
  `session_id`→`project_id` resolution + `source_session_id` preserved; `stale`/`invalid` no-ops;
  writes via `MemoryManager` directly (speculative-redirect bypass); **constructed without `llm_service`**.
- `tests/skills/test_review_learning_skill.py` — skill contract (payload, seed lesson-types,
  record/skip rules, ladder, target/category semantics).
- `tests/skills/test_coderabbit_skill.py` (extend) — `REQUIRED SKILL: review-learning` + post-triage step.
- `tests/skills/scenarios/review-learning/` — fake-source proof: confirmed → memory;
  stale/invalid → none; raw CI failure → none; verified-fix → memory; same finding across 2 reviews
  → 2 occurrences → promotes; same finding twice in 1 run → 1 (occurrence preflight).
- Producer hook contract tests — inline-record instruction present in code-reviewer /
  holistic-review skills and qa-reviewer / nightly agent YAMLs.

---

## Verification (end-to-end)

1. `uv sync`; `uv run ruff check src/`; `uv run mypy src/`.
2. `GOBBY_TEST_PROTECT=1 uv run pytest tests/review_learning/ tests/mcp_proxy/tools/test_review_learning.py tests/skills/test_review_learning_skill.py tests/skills/test_coderabbit_skill.py -v`.
3. Isolated test daemon: `gobby-review-learning` + `record_review_lesson` appear via
   `list_mcp_servers()` / `list_tools`.
4. Record the same `confirmed` finding (same fingerprint) from two reviews → 2 occurrences → a
   `test`-category guardrail task with evidence memory IDs; a third → escalates the **same** task
   via `update_task`; re-record same run → occurrence preflight skips, no dup.
5. Record a `no-fix-policy` pattern twice → only a `checklist`/`tool-config` task.
6. Record a raw `test_failure` with no fix ref → nothing; again with a verified fix ref → stored.
7. Pass `session_id` → lesson lands in the resolved project with `source_session_id` set.
8. `search_memories(tags_all=["review-lesson"])` returns lessons; a fresh `turn_start` matching the
   pattern injects via existing recall; unrelated prompt does not.

## Notes / Risks

- **Four identities stay separate** — provenance / finding / occurrence / pattern.
- **No direct rule mutation** — enforcement ships only by completing a guardrail task.
- **Load-bearing, pin with tests**: CI-flake guard, occurrence-tag idempotency, fingerprint
  stability, non-promotable fallback, deterministic core (no `llm_service`), `MemoryManager`-direct
  write.
- Implementation claims/creates a Gobby task per file edit (CLAUDE.md rule 3); each new `.py` stays
  under 1,000 lines.
