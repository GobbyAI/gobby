# feedback-verify-stage: Per-Claim Verification and Lesson Memories for Feedback Review

Plan artifact: `.gobby/plans/feedback-verify-stage.md`

**Plan ID:** feedback-verify-stage

## Overview
`kind: framing`

Task #21312 (Add an EDV-style verify stage that mints lesson memories from
feedback clusters) under the epic #22949 (Lane 7 - Planning/research).

The nightly feedback review gains an independent verify stage between the
reviewer's findings and every write:
- The reviewer may propose one lesson per cluster beside its task proposal
  (1.1).
- Each task or lesson proposal becomes a claim with a bound identity. One
  verifier agent per claim checks it against the current checkout and
  submits a verdict (1.2, 2.1).
- Only a confirmed claim is written. A confirmed task claim is filed as
  today, and a confirmed lesson claim is minted as a memory tagged
  `feedback-lesson`. A refuted claim writes nothing. An unverified claim
  writes nothing and leaves its observations for the next run (2.2, 3.1).
- The digest and the results reader show every verdict and every minted
  lesson (3.2). The references document the stage (4.1).

Ownership: Python daemon code under `src/gobby/feedback/`, the bundled
verifier agent and prompt, and the feedback references. The Orchestrator
routes the leaves to developer seats.

Out of scope:
- Lifting the lesson-injection hold (memory b45ae9da). Minted lessons stay
  out of the pushed memory index.
- The memory dream's handling of push-excluded memories. #23651 (Memory
  dream can rewrite away the review-lesson tag, exposing held lessons in the
  pushed memory index) owns it (deferral D1).
- The deterministic `unverified-premise` and `possibly-fixed` labels, open
  task deduplication, and the two-attempt reviewer launch. They are
  unchanged.

## Decision Record
`kind: framing`

Sources: Josh's directive of 2026-08-31 in #21312's description; the park
note relayed by the Assistant gobby#14069 on 2026-09-28; memory b45ae9da;
and the Orchestrator gobby#14972's rulings Q1 to Q3 (2026-10-06 00:59 CT)
and Q4 (2026-10-06 01:20 CT).

1. **One verifier per claim.** Josh's directive, verbatim: "verification is
   one agent per claim, not one pass over all claims. Each proposed
   finding/task gets its own verification agent with repository access,
   checked out on the current branch, prompted with the claim plus its
   observation ids/evidence, answering: does this still reproduce on the
   current branch, or has it been fixed/retired since the observations were
   written? ... Default-reject stands: a claim files/commits only when its
   verifier affirmatively confirms it against the working tree." The
   directive cites #21323 (Restore required session-feedback capture at
   handoff and stop), filed from a stale observation.
2. **Reconciliation the park note requires.**
   - The reviewer keeps its repository access. It clusters, classifies and
     proposes. The verifier is the independent gate, and it never sees the
     reviewer's session.
   - The lesson-candidate schema is `proposed_lesson` (1.1).
   - Per-claim verification is 2.1 and 2.2.
   - The injection policy is Q1 (item 9) and Q4 (item 10).
3. **Claims.** A task claim is a `defect` or `guidance-gap` cluster with a
   `proposed_task`. A lesson claim is a non-noise cluster with a
   `proposed_lesson`. Caps apply before verification (Q2):
   `max_tasks_per_run` task claims and `max_lessons_per_run` lesson claims,
   each in cluster order. Overflow is deferred unverified, and its
   observations are marked reviewed, as task overflow is today. A deferred
   lesson is not retried.
4. **Claim identity.** `claim_id` is `c<cluster index>-task` or
   `c<cluster index>-lesson`. The claim view holds the cluster's
   `observation_ids`, `cited_paths`, `implementation_paths`, `theme` and
   `classification`, plus the claim's own proposal. The claim digest is the
   SHA-256 of canonical JSON (sorted keys, compact separators) over the run
   id, `claim_id`, kind and claim view. The verifier receives the digest in
   its prompt and echoes it with its verdict. Storage refuses a mismatch.
   The daemon writes from the same claim object it stored.
5. **Default reject and consumption.**

   | Outcome | Write | Observations |
   | --- | --- | --- |
   | `confirmed` | Task filed or lesson minted | Marked reviewed |
   | `refuted` | None | Marked reviewed |
   | `unverified` | None | Stay unreviewed for the next run |

   `unverified` covers a launch failure, an admission refusal, a timeout,
   a verifier that ends without a verdict or reports that it cannot
   decide, an exhausted budget, a missing verifier, and evidence that
   changed after verification (item 12). A run with any unverified claim
   or failed action finalizes `partial`.
6. **Verdict storage.** Verdicts live in a storage-owned
   `actions.verification` key of the run row, written under the row lock
   with the merge pattern `assign_reviewer` already uses. `save_progress`
   and `finalize_run` take the key from the row's current value inside
   their own `UPDATE` and ignore a service-supplied copy, so a service
   checkpoint never clobbers a verdict, even one recorded after the
   service read the row. Once verification
   begins, `submit_review` refuses, which freezes the findings the claims
   came from.
7. **Verdict authority.** A verdict is accepted only while the run is
   `running`, only for a claim in state `assigned`, and only from the child
   session of that claim's verifier agent run, as `submit_review` checks the
   reviewer. It carries `confirmed`, `refuted` or `unverified`, nonblank
   evidence, the echoed digest, the 40-hex `HEAD` it checked and its
   evidence footprint (item 12). `refuted` needs affirmative evidence that
   the claim is false or no longer reproduces. A verifier that cannot
   decide, or whose tools or evidence fail, submits `unverified` with its
   reason. The service's close step
   is the only way a claim leaves `assigned` without a verdict, so a verdict
   after close is refused. Finalization and `mark_running_interrupted` at
   daemon start move the run out of `running`, which refuses every later
   verdict.
8. **Verifier agent and budget (Q3).** A new bundled agent
   `feedback-claim-verifier` mirrors the reviewer: provider `codex`, model
   `gpt-5.6-sol`, reasoning `medium`, isolation `none` in the project
   checkout, which is the current branch. It has read-only tools and no task
   or memory write tool. The stage deadline
   `VERIFY_TOTAL_DEADLINE_SECONDS` equals the 2 h distill deadline and is
   fixed as one monotonic stage deadline when verification starts. Each
   claim's deadline is the current time plus the remaining budget divided
   by the remaining claims. A share no longer than
   `COMPLETION_GRACE_SECONDS` closes the claim unverified without a spawn.
   The service enforces each claim deadline itself:
   - Prompt rendering and the launch run as one task the verifier owns,
     with their synchronous work in threads, and the service waits for
     it only until the deadline. Spawn preparation finishes its thread
     work before it honors cancellation, so the stage clock never waits
     on it. A launch that returns at or after the deadline is never
     assigned, so its session can never submit a verdict, and its run is
     retired.
   - Assignment runs as an owned task under the same deadline. It
     records the claim's expiry, the deadline on the daemon's wall
     clock, and under the row lock it assigns only a `pending` claim
     before that expiry. A verdict at or after the expiry is refused.
     Close and assignment lock the same row. When the close goes
     first, the delayed assignment finds the claim closed and changes
     nothing. When the assignment goes first, it committed before the
     expiry, its session can submit only until then, and the close
     ends the claim. A wall-clock step can end authority early, which
     leaves the claim unverified and retryable, or extend it only
     until the close commits.
   - At the deadline the service closes the claim, which refuses every
     later verdict, and then retires the verifier run through the shared
     termination path. The kill escalates from TERM to KILL after 5 s.
   - The spawn timeout is the deadline less the loop time just before
     the spawn, less `COMPLETION_GRACE_SECONDS`. It counts from the
     run's `started_at`, stamped after boot, so it is only a backstop
     behind the close and retirement.

   Verifiers launch one at a time, and spawn admission gates every spawn
   unchanged. A retiring verifier may overlap the next claim's verifier
   until its kill completes. The cron timeout grows from 2 h 5 min to
   4 h 5 min. The job stays not restart-protected.
9. **Lessons (Q1, Q2).** `proposed_lesson` holds `content`, `when`,
   `memory_type` (`fact` or `pattern`) and `verification_evidence`.
   Deterministic daemon code mints a confirmed lesson with `create_memory`
   in the gobby project: the content, the type, `rationale` set to the
   `when` text, `source_type` `agent`, `created_by_agent`
   `feedback-reviewer`, and these tags:
   - `feedback-lesson`;
   - `feedback-run:<run id>`;
   - `feedback-claim:<claim_id>`;
   - `feedback-verifier:<verifier agent run id>`;
   - `feedback-observation:<id>` for each observation.

   The observation tags are the mint receipt. A lesson claim whose
   observations include one already cited by a `feedback-lesson` memory is
   recorded `already_minted` and writes nothing. Observation ids are stable
   across nights, while run ids and wording are not. A lesson whose
   content matches any stored memory, ordinary, dream-hidden or deleted,
   is recorded `content_exists` and writes nothing. A plain
   `create_memory` would merge the lesson's tags into that memory and
   keep its type, rationale and creator, so the mint uses a conditional
   create that re-checks the content under the storage write lock and
   raises `DuplicateMemoryContentError` instead. Lesson `content` is
   capped at the memory write
   cap, `MAX_MEMORY_CONTENT_CHARS` (3,000), at submission.
   `max_lessons_per_run` defaults to 10. `feedback-lesson` joins
   `PUSH_EXCLUDED_TAGS`, the shared list #23651 creates in
   `src/gobby/memory/push_exclusion.py`, so minted lessons never reach
   the pushed memory index. Memory b45ae9da, verbatim: "do not re-enable
   or expose automatic agent-facing lesson injection unless an explicit
   decision changes the hold." Removing the tag from
   `PUSH_EXCLUDED_TAGS` is a one-line change that needs Josh's explicit
   decision.
10. **Dream exposure (Q4).** The memory dream can rewrite a memory's tags.
    The Orchestrator ruled that the dream candidate listing skips every tag
    in the shared push-exclusion list, under #23651 in Lane 6. Adding
    `feedback-lesson` to that list (item 9) then protects minted lessons
    with no further change here (D1). #23651 is a prerequisite. At
    expansion the implementation root is blocked by #23651, and a task is
    ready only when its parent is, so no leaf starts and no lesson is
    minted before the protection lands.
11. **Dry run.** A dry run runs the reviewer and every verifier and records
    their verdicts. It files no task, mints no memory and consumes no
    observation.
12. **Freshness at write.** A confirmed verdict authorizes a write only
    while its evidence is unchanged. The verifier reports its evidence
    footprint: every repository file its verdict rests on, each with the
    `git hash-object` blob it read, or null for a missing path. A
    confirmed footprint must be nonempty and must include every cited and
    implementation path of the claim, and storage refuses one that does
    not. Right before each task's deduplication or creation, and right
    before each lesson's creation, the daemon recomputes every blob in
    the footprint. A claim with a changed blob, or whose recheck fails,
    writes nothing, joins `unverified` with reason `evidence changed
    since verification: <paths>` or `evidence check failed: <error>`,
    and keeps its observations for the next run. The recheck runs inside
    the write loop, so a file changed by an earlier write in the same run
    counts.

Rejected alternatives:
- One verifier pass over all claims. Josh's directive rules it out.
- Parallel verifier fan-out. Sequential spawns add one agent at a time
  under the existing admission, and the 2 h budget gives 20 claims about
  6 minutes each.
- Several verifiers per claim with a consensus vote. The directive asks for
  one independent verifier per claim.
- A verify stage that mints nothing, or minting with the push exclusion
  lifted. Q1 rules both out.
- One cap shared by tasks and lessons. Q2 rules it out.
- A verdict table or column. It needs a schema migration and a cutover,
  while a JSONB key under the row lock gives the same atomicity.
- Content-hash mint receipts. A reworded lesson on the next night defeats
  them.
- A `HEAD` comparison at write time. The nightly sweep spans commits in
  the shared checkout, so it would reject nearly every claim and starve
  the stage, and it misses uncommitted changes. The blob footprint
  catches both for every file the verdict rests on.
- A footprint of the claim's own paths only. A verdict can rest on a
  file the claim never cites, such as a caller or a configuration file.
- Waiting for a timed-out launch or retirement before the next claim.
  Spawn preparation finishes its thread work before it honors
  cancellation, so the wait would spend later claims' shares.
- A verifier that writes the task or memory itself. #21312 keeps the
  deterministic action layer in charge of writes.

## As-Is Facts
`kind: framing`

Source trace taken on `0.5.0` at `28d6749366`. Re-derive line numbers with
gcode before editing.

**Review service** (`src/gobby/feedback/service.py`, 191 lines):
- `DISTILL_TOTAL_DEADLINE_SECONDS` (27) is 2 h.
- `FeedbackReviewService.run_review` (46-131) freezes a batch, runs
  `_distill`, checkpoints, calls `FeedbackActions.apply`, renders the
  digest, marks every row reviewed except the `failed` observations, and
  finalizes `completed` or `partial`. Its exception path finalizes
  `interrupted` on cancellation, otherwise `failed`, and re-raises.
- The nested `checkpoint` (58-64) calls `save_progress` with the service's
  whole `findings` and `actions`.
- `_distill` (133-191) renders `feedback/review` with `run_id` and
  `max_tasks`, and makes up to two reviewer attempts.

**Reviewer** (`src/gobby/feedback/agent.py`, 364 lines):
- `FEEDBACK_FINDINGS_SCHEMA` (37-87): each cluster has `observation_ids`,
  `cited_paths`, `implementation_paths`, `theme`, `classification` (one of
  `defect`, `guidance-gap`, `noise`, `praise`), an optional nullable
  `proposed_task` and `digest_note`. `additionalProperties` is false. There
  is no lesson field.
- `validate_feedback_findings` (145-182) requires each frozen observation
  exactly once.
- `FeedbackReviewerAgent.review` (209-364) resolves the agent, the project
  checkout and a launcher session, then calls `spawn_agent_impl` with
  isolation `none`. It then calls `store.assign_reviewer`, waits on the
  completion registry for the timeout plus `COMPLETION_GRACE_SECONDS` (30),
  and reads the submitted findings from the run row. Its errors are
  `FeedbackReviewerLaunchError`, `...RunError`, `...TimeoutError` and
  `...ResultError`.

**Actions** (`src/gobby/feedback/actions.py`, 553 lines):
`FeedbackActions.apply` (92-259) is deterministic. It files actionable
clusters up to `max_tasks_per_run`, deduplicates against open tasks, adds
`unverified-premise` and `possibly-fixed`, and defers overflow (248-258).
A dry run returns before any write (124-126).

**Storage** (`src/gobby/feedback/storage.py`, 445 lines):
- `save_progress` (194-199) overwrites `findings` and `actions` wholesale.
- `assign_reviewer` (201-207) merges with `COALESCE(actions, '{}'::jsonb)
  || %s::jsonb` while the run is `running`.
- `submit_review` (209-253) locks the row, requires `running`, requires the
  submitter to be the assigned reviewer run's `child_session_id`, validates,
  writes the report and the `findings` column.
- `results_page` (283-310) pages clusters with the outcome lists `filed`,
  `suppressed`, `deferred`, `failed` and `retained`.
- `mark_running_interrupted` (333-349) runs at daemon start.

**Report and digest:** `merge_report_inputs` (`report.py` 22-57) merges
only `filed`, `suppressed`, `skipped` and `failed` across a day's runs.
`render_digest` (`digest.py` 67-143) renders Clusters, Actions, Shirked
found work and Other-label audit.

**Cron** (`src/gobby/feedback/cron.py`): `FEEDBACK_REVIEW_TIMEOUT_SECONDS`
(26-28) is the distill deadline plus 300 s. `_action_config` (37-44) is not
restart-protected. `_ensure_system_job` (84-121) rewrites an existing job's
action config at start.

**Agent definition:**
`src/gobby/install/shared/workflows/agents/feedback-reviewer.yaml` uses
codex `gpt-5.6-sol`, reasoning medium and isolation none. It blocks spawn,
kill and task mutation, loads `restraint` and `proportionality`, and then
allows read-only Bash plus the feedback readers, `submit_review`, task
readers and memory search. It has no memory write tool.

**MCP tools:** `create_feedback_registry`
(`src/gobby/mcp_proxy/tools/feedback.py`, 54 lines) registers
`get_review_observations`, `get_review_results` and `submit_review`.
`docs/reference-audit/review.json` audits each one against
`references/review/feedback.md` or `outcomes.md`, and
`tests/skills/test_reference_library.py` fails on an unaudited tool.

**Memory:** `EXCLUDED_TAGS = ["review-lesson"]`
(`src/gobby/mcp_proxy/tools/memory_surface.py` 32) is passed as `tags_none`
to the push search (110). `MemoryManager` provides `create_memory` and
`alist_memories` with `tags_all` and `tags_any`, as
`ReviewLearningMemoryManager` (`review_learning/service.py` 66-114) types
them. #23651 moves the list to `PUSH_EXCLUDED_TAGS`, a tuple in a new
`src/gobby/memory/push_exclusion.py` that `memory_surface.py` and
`dream/candidates.py` both import (Orchestrator ruling, 01:59 CT). The
dream's refresh action can rewrite tags (`dream/apply.py`
`_apply_fenced_action`).

**Config:** `FeedbackReviewConfig` (`src/gobby/config/sessions.py`
182-215) has `max_rows_per_run` 200 and `max_tasks_per_run` 10. Each config
field is mirrored in `crates/gcore/assets/config/runtime_config_contract.json`
and in the HTTP config corpus.

**Wiring:** `init_orchestration` (`src/gobby/runner_init/orchestration.py`
633-665) builds `FeedbackReviewerAgent` and
`FeedbackReviewService(runner.database, feedback_reviewer,
feedback_review_config, runner.task_manager)`. `runner.memory_manager` is
set earlier at runner init.

## Constraints
`kind: framing`

- The LLM never writes. The verifier's allowlist has no task or memory
  write tool, and only daemon code files tasks and mints memories.
- Spawns go through the existing `spawn_agent_impl` admission. No load
  threshold, admission limit or escalation level changes.
- Tests never touch the daemon's hub. Database tests use the isolated test
  hub with `GOBBY_TEST_PROTECT=1`, and agent spawns use the fakes in
  `tests/feedback/test_feedback_agent.py`.
- No production file reaches 1,000 lines. `actions.py` (553),
  `storage.py` (445) and `storage/memories_crud.py` (826) stay under it.
- The feedback cron job stays not restart-protected.
- Plan readers never read `~/.gobby/local_cli_token` or
  `~/.gobby/bootstrap.yaml`.
- Implementation waits for Josh's approval of this plan and the unpark of
  #21312. That approval is the explicit decision for verifier spawns and
  lesson minting. Agent-facing lesson injection stays held. No leaf
  starts before #23651 closes (D1).

## P1: Claims
`kind: framing`

**Goal:** the reviewer can propose lessons, and each proposal becomes a
bounded, verifiable claim whose verdict storage refuses every unauthorized
or late write.

### 1.1 Lesson candidates in reviewer findings [category: code]
`kind: deliverable`

Targets:
- `src/gobby/feedback/agent.py::FEEDBACK_FINDINGS_SCHEMA`
- `src/gobby/feedback/agent.py::validate_feedback_findings`
- `src/gobby/feedback/service.py::FeedbackReviewService._distill`
- `src/gobby/config/sessions.py::FeedbackReviewConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the runtime config contract with the lesson cap
- `tests/contracts/http/config_schema.json::*` — scope-reason: re-record the config schema case with the lesson cap
- `tests/contracts/http/config_values.json::*` — scope-reason: re-record the config values case with the lesson cap
- `src/gobby/install/shared/prompts/feedback/review.md`
- `tests/feedback/test_feedback_agent.py::*` — scope-reason: cover the lesson schema and the noise refusal
- `tests/feedback/test_feedback_service.py::*` — scope-reason: cover the rendered lesson cap
- `tests/config/test_app_config.py::*` — scope-reason: cover the lesson cap default and bounds

**Research context:**
- `FEEDBACK_FINDINGS_SCHEMA` (`agent.py` 37-87) makes `proposed_task`
  optional and nullable, and its `verification_evidence` is nonblank through
  `minLength: 1` and `pattern: \S`. `additionalProperties` is false at
  every level.
- `validate_feedback_findings` (145-182) runs at submission inside
  `FeedbackReviewStore.submit_review` and again in `review`, so a refusal
  reaches the reviewer as a tool error it can fix.
- `_distill` renders `feedback/review` with `run_id` and `max_tasks`. The
  prompt (`prompts/feedback/review.md`, version 6.1) declares both in
  `required_variables`, and its Output section shows the cluster JSON.
- `FeedbackReviewConfig.max_tasks_per_run` is `Field(default=10, ge=0)`.
  `tests/config/test_app_config.py::TestFeedbackReviewConfig` covers the
  defaults and bounds.
- `scripts/generate_runtime_config_contract.py` regenerates the runtime
  contract, and `tests/config/test_runtime_config_contract.py` compares it
  byte for byte. `tests/contracts/http/README.md` (line 128) gives the
  corpus re-record command.
- `MAX_MEMORY_CONTENT_CHARS` (`memory/services/lifecycle.py` 57) is
  3,000, and `create_memory` raises `ValueError` above it
  (`_enforce_content_cap`, 60-66).

Implementation:
- The cluster schema gains optional nullable `proposed_lesson`. A non-null
  lesson requires `content`, `when`, `verification_evidence` and
  `memory_type`. The first three are nonblank by the same pattern,
  `memory_type` is `fact` or `pattern`, and no other keys are allowed.
  `content` has `maxLength` `MAX_MEMORY_CONTENT_CHARS`, imported from
  `gobby.memory.services.lifecycle`, so an overlong lesson is refused at
  submission and the reviewer can shorten it.
- `validate_feedback_findings` refuses a `noise` cluster whose
  `proposed_lesson` is not null, naming the cluster's first observation id.
- `FeedbackReviewConfig` gains `max_lessons_per_run: int = Field(default=10,
  ge=0, description="Maximum lesson memories one run may mint")`.
- `_distill` also renders `max_lessons`.
- `review.md` moves to version 6.2 and requires `max_lessons`. Its
  Instructions say that a lesson is durable guidance a future agent should
  apply, that `when` names the situation it applies to, that noise
  carries no lesson, that lesson content stays within 3,000 characters,
  and that at most `{{ max_lessons }}` lessons are kept.
  They also say an independent verifier checks every task and lesson
  against the current checkout and rejects by default. The Output example
  shows `proposed_lesson`.
- Regenerate the runtime contract with `uv run python
  scripts/generate_runtime_config_contract.py`, and re-record the config
  corpus with the README command.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/test_feedback_agent.py
tests/feedback/test_feedback_service.py tests/config/test_app_config.py
tests/config/test_runtime_config_contract.py
tests/contracts/test_http_corpus.py -q`, then `uv run ruff check src/ &&
uv run mypy src/` and a test-types audit of the changed tests.

**Acceptance:**

- 1.1.1 - A cluster may carry a `proposed_lesson` with nonblank `content`,
  `when` and `verification_evidence` and a `memory_type` of `fact` or
  `pattern`. A lesson missing any required field, `memory_type` included,
  is refused, as are a blank field, content over 3,000 characters,
  another type and an extra key. test:
  `tests/feedback/test_feedback_agent.py::test_findings_schema_accepts_proposed_lesson`.
- 1.1.2 - A `noise` cluster carrying a lesson is refused at submission.
  test: `tests/feedback/test_feedback_agent.py::test_noise_cluster_lesson_is_refused`.
- 1.1.3 - `max_lessons_per_run` defaults to 10 and refuses a negative
  value. test:
  `tests/config/test_app_config.py::TestFeedbackReviewConfig::test_lesson_cap_defaults_and_bounds`.
- 1.1.4 - The review prompt renders the lesson cap and the lesson
  instructions. test:
  `tests/feedback/test_feedback_service.py::test_distill_prompt_renders_lesson_cap`.

### 1.2 Claim records and verdict submission [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/feedback/claims.py`
- `src/gobby/feedback/storage.py::FeedbackReviewStore.save_progress`
- `src/gobby/feedback/storage.py::FeedbackReviewStore.submit_review`
- `src/gobby/feedback/storage.py::FeedbackReviewStore.finalize_run`
- `src/gobby/feedback/storage.py::FeedbackReviewStore`
- `src/gobby/mcp_proxy/tools/feedback.py::create_feedback_registry`
- `docs/reference-audit/review.json::*` — scope-reason: add the submit_claim_verdict audit entry
- `src/gobby/install/shared/skills/gobby/references/review/feedback.md`
- `tests/feedback/test_feedback_claims.py`
- `tests/feedback/test_feedback_storage.py::*` — scope-reason: cover verdict authority, preservation and the review freeze
- `tests/reports/test_tools.py::*` — scope-reason: cover the verdict tool through the registry

**Research context:**
- `submit_review` (`storage.py` 209-253) is the pattern: lock the row with
  `FOR UPDATE`, require `running`, read `child_session_id` from
  `agent_runs` for the assigned run id, and refuse any other session.
  `assign_reviewer` (201-207) shows the JSONB merge.
- `save_progress` (194-199) writes the service's whole `actions`, and
  `finalize_run` (312-331) writes it again at the end, so both must keep a
  storage-owned key.
- The `submit_review` tool (`mcp_proxy/tools/feedback.py` 36-52) reads the
  caller with `get_current_session_id()` and maps `ValueError` and
  `RuntimeError` to `{"success": False, "error": ...}`.
- `docs/reference-audit/review.json` holds one entry per `gobby-feedback`
  tool, with `reference`, `implementation.path` and `implementation.symbol`.
  `tests/skills/test_reference_library.py` fails on an unaudited tool.
- `_repo_relative_path` (`actions.py` 484-489) rejects absolute, empty
  and `..` paths. `_cluster_cited_paths` and
  `_cluster_implementation_paths` (415-433) read a cluster's paths.

Implementation:
- New `claims.py`:
  - `FeedbackClaim`, a frozen dataclass: `claim_id`, `kind` (`task` or
    `lesson`), `cluster_index`, `view`, `proposal`, `observation_ids`,
    `paths` and `digest`. `claim_id` is `c<cluster index>-task` or
    `c<cluster index>-lesson`. `view` holds the cluster's
    `observation_ids`, `cited_paths`, `implementation_paths`, `theme`
    and `classification`, plus the claim's own `proposed_task` or
    `proposed_lesson`. `paths` holds the cited paths that
    `_repo_relative_path` accepts, normalized, plus
    `_cluster_implementation_paths`, in order without duplicates.
    `claims.py` imports the three helpers from `actions.py`, which names
    `FeedbackClaim` only under `TYPE_CHECKING`.
  - `claim_digest(run_id, claim_id, kind, view)`: SHA-256 hex of
    `json.dumps(..., sort_keys=True, separators=(",", ":"))`.
  - `build_claims(run_id, findings, *, max_tasks, max_lessons)` returns the
    selected claims in cluster order, task before lesson, plus the deferred
    overflow entries `{kind, claim_id, observation_ids, proposal, reason}`
    with reason `task cap reached` or `lesson cap reached`.
  - `normalize_evidence_files(evidence_files)` normalizes each `{path,
    blob}` path with `_repo_relative_path`. It refuses a rejected or
    duplicate path and a blob that is neither null nor 40 lowercase hex.
- `FeedbackReviewStore` gains four methods. Each locks the row, requires
  `running`, and updates `actions.verification.claims.<claim_id>` with
  `jsonb_set`.
  - `begin_verification(run_id, claims)` writes each claim's kind, cluster
    index, observation ids, paths, digest and state `pending`. It refuses when
    `actions.verification` already exists.
  - `assign_verifier(run_id, claim_id, agent_run_id, expires_at)` moves
    `pending` to `assigned` and records the verifier run id and
    `expires_at`, a UTC time. It leaves a claim that is not `pending`
    unchanged, and also leaves it unchanged once the daemon clock, read
    under the row lock, has reached `expires_at`. It returns the claim
    record.
  - `submit_claim_verdict(run_id, session_id, claim_id, claim_digest,
    verdict, evidence, head_sha, evidence_files)` accepts a verdict only
    while the run is
    `running`, only for a claim in state `assigned` whose stored
    `expires_at` the daemon clock, read under the row lock, has not
    reached, only from the child session of that claim's verifier agent
    run, read from `agent_runs` as
    `submit_review` reads the reviewer's, and only when `claim_digest`
    matches the stored digest. `verdict` is `confirmed`, `refuted` or
    `unverified`, `evidence` is nonblank, and `head_sha` matches
    `^[0-9a-f]{40}$`. `evidence_files` passes `normalize_evidence_files`.
    A `confirmed` verdict needs a nonempty footprint that includes every
    stored claim path. It records the verdict as the state, with
    evidence, head, footprint and `decided_at`. A decided or closed claim
    refuses a second verdict.
  - `close_claim(run_id, claim_id, reason)` moves `pending` or `assigned`
    to `unverified` with the reason, leaves a decided claim unchanged, and
    returns the claim record. With the run no longer `running`, it returns
    the stored record unchanged.
- `save_progress` and `finalize_run` drop any `verification` key from the
  supplied actions and merge in the stored one. Supplied actions of `None`
  with a stored key write only that key. With neither, the column is
  written as today. Each is one `UPDATE` whose `SET` expression reads
  `verification` from the row's current `actions`, so the merge runs under
  the row lock and keeps a verdict committed after the service read its
  copy. No read-merge-write spans two statements.
- `submit_review` refuses once `actions.verification` exists.
- `create_feedback_registry` registers `submit_claim_verdict(run_id,
  claim_id, claim_digest, verdict, evidence, head_sha, evidence_files)`,
  which reads the caller session as `submit_review` does.
- `review.json` adds the `submit_claim_verdict` entry with reference
  `references/review/feedback.md`, and `feedback.md` names the tool in one
  sentence. 4.1 documents the stage.

Consumers unchanged:
- `tests/reports/test_retirement.py` — no-edit-reason: it checkpoints and finalizes a run that never begins verification, so the written actions are unchanged.
- `src/gobby/feedback/__init__.py` — no-edit-reason: it re-exports `FeedbackReviewStore` by name.
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: it calls `create_feedback_registry(db)`, whose signature is unchanged.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/test_feedback_claims.py
tests/feedback/test_feedback_storage.py tests/reports/test_tools.py
tests/skills/test_reference_library.py -q`, then `uv run ruff check src/ &&
uv run mypy src/` and a test-types audit of the changed tests.

**Acceptance:**

- 1.2.1 - `build_claims` selects task and lesson claims in cluster order
  under their separate caps and returns the overflow as deferred entries.
  test: `tests/feedback/test_feedback_claims.py::test_build_claims_caps_each_kind_and_defers_overflow`.
- 1.2.2 - The digest changes when the run, claim id, kind or any field of
  the claim view changes. test:
  `tests/feedback/test_feedback_claims.py::test_claim_digest_binds_run_claim_and_view`.
- 1.2.3 - A verdict is refused from another session, for an unassigned,
  already decided or closed claim, with a mismatched digest, blank
  evidence, a malformed head or a malformed footprint entry, after the
  run leaves `running`, and once the claim's `expires_at` is reached. A
  confirmed verdict whose footprint is empty or misses a claim path is
  refused. `assign_verifier` leaves a closed claim unchanged, and also
  any claim once `expires_at` is reached. test:
  `tests/feedback/test_feedback_storage.py::test_claim_verdict_requires_assigned_verifier_and_bound_claim`.
- 1.2.4 - A `save_progress` or `finalize_run` keeps every recorded verdict
  when the supplied actions omit `verification`, carry a stale copy read
  before a later verdict, or carry forged verdicts. test:
  `tests/feedback/test_feedback_storage.py::test_progress_and_finalize_preserve_verification`.
- 1.2.5 - `submit_review` refuses after verification begins. test:
  `tests/feedback/test_feedback_storage.py::test_review_submission_closes_when_verification_begins`.
- 1.2.6 - The `gobby-feedback` registry exposes `submit_claim_verdict`,
  and it records a verdict for the assigned verifier session. test:
  `tests/reports/test_tools.py::test_submit_claim_verdict_records_assigned_verdict`.

## P2: Verification
`kind: framing`

**Goal:** one independent verifier per claim checks it against the current
checkout within a fixed budget, and only confirmed task claims are filed.

### 2.1 Claim verifier agent [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/feedback-claim-verifier.yaml`
- `src/gobby/install/shared/prompts/feedback/verify.md`
- `src/gobby/feedback/verifier.py`
- `src/gobby/feedback/agent.py::FeedbackReviewerAgent.review`
- `src/gobby/feedback/agent.py::FeedbackReviewerAgent`
- `tests/feedback/test_feedback_verifier.py`
- `tests/feedback/test_feedback_agent.py::*` — scope-reason: keep the reviewer launch tests on the extracted launch method and cover retirement
- `tests/workflows/test_feedback_claim_verifier_allowlist.py`

**Granularity:** one leaf with eight acceptance items. The extracted
launch, retirement, the verifier definition, its prompt and the
supervised `verify` call are one behavior. No claim can run without
all of them.

**Research context:**
- `FeedbackReviewerAgent.review` (`agent.py` 209-364) holds the whole
  launch path: `resolve_agent`, `require_root` for the checkout,
  `get_or_create_launcher_session` with source `feedback-review`, then
  `spawn_agent_impl(prompt, runner, agent_body=..., agent_lookup_name=...,
  isolation="none", timeout=..., parent_session_id=..., ...,
  notify_parent_on_completion=True)`. A spawn result without `success` or
  `run_id` raises `FeedbackReviewerLaunchError`.
- `feedback-reviewer.yaml` is the definition to mirror: its blocked tools,
  rule selectors (`tag:default`, `tag:worker-safety`, excluding
  `tag:task-skill-gates`), the `load_skills` step and the read-only review
  step.
- `tests/workflows/test_feedback_reviewer_allowlist.py` (73 lines) checks
  that the reviewer has no review-learning or memory write tool. The new
  allowlist test follows it.
- `PromptLoader(db=...).render(path, variables)` renders bundled prompts,
  which sync from `install/shared/prompts/` like agent definitions.
  `render` loads an uncached template through a synchronous database
  lookup (`prompts/loader.py` 71-99 and 101-134, Adv1's trace).
  `review` already runs its synchronous resolution steps through
  `asyncio.to_thread` (`agent.py` 225-281).
- Spawn preparation runs through `run_thread_to_completion`
  (`utils/git.py` 103-115), which waits for its worker thread before it
  propagates cancellation (Adv1's trace:
  `mcp_proxy/tools/spawn_agent/_implementation.py` 666-757). Cancelling
  a launch therefore does not end it promptly.
- The spawn timeout counts from the run's `started_at`, stamped after
  boot (`storage/agents/_lifecycle.py` 287-342), and the health check
  compares it periodically (`agents/agent_health.py` 173-232).
- `terminate_agent_run` (`mcp_proxy/tools/agent_cancellation.py`
  194-301) is the shared termination path. With
  `effective_status="error"` it records `terminal_error` as the reason,
  while `"cancelled"` always records `user_cancelled`.
  `workflows/engine/enforcement_checks.py` (318-345) calls it from daemon
  code. `kill_agent` escalates from TERM to KILL after 5 s.
  `AgentRunner` exposes `run_storage` and `agent_lifecycle_monitor`.
- `git hash-object -- <path>` without `-w` writes nothing and runs
  unblocked under the default rules (probe, 2026-10-06). A missing path
  exits 128.

Implementation:
- Extract `FeedbackReviewerAgent.launch(agent_name, prompt, *, deadline)
  -> str` from `review`, where `deadline` is an absolute `loop.time()`
  value. It resolves the named agent, the checkout and the launcher
  session. Just before the spawn it measures the spawn timeout as
  `deadline` minus the current loop time minus
  `COMPLETION_GRACE_SECONDS`, and a non-positive timeout raises
  `FeedbackReviewerLaunchError` without a spawn. It returns the agent run
  id and raises `FeedbackReviewerLaunchError` on a failed spawn as today.
  `review` calls it with `FEEDBACK_REVIEWER_AGENT_NAME` and a deadline of
  its timeout plus the grace from the call, so the reviewer's spawn
  timeout is its given timeout less the preparation time. `launch` holds
  only agent, checkout and launcher-session resolution and the
  `spawn_agent_impl` call, and it passes the prompt through unchanged.
  The feedback run lookup, the daily report path, the cumulative-report
  prompt suffix, the `submit_review` instruction, `assign_reviewer`, the
  completion wait and the findings read stay in `review`.
- `FeedbackReviewerAgent.retire(run_id)` reads the run and, while it is
  `pending` or `running`, calls `terminate_agent_run` with
  `effective_status="error"` and `terminal_error="feedback claim
  deadline passed"`. It passes the runner, `runner.run_storage`, its
  database, `runner.agent_lifecycle_monitor`, its completion registry,
  its session manager and no task manager, as `enforcement_checks.py`
  does. It logs a failed termination.
- New `feedback-claim-verifier.yaml`: the reviewer's provider, model,
  reasoning, isolation, blocked tools, rule selectors and `load_skills`
  step. Its `verify` step allows Bash, Read and the proxy tools, plus these
  MCP tools: `gobby-feedback:get_review_observations`,
  `gobby-feedback:submit_claim_verdict`, the task readers, `get_rule`,
  `get_agent_definition`, the results readers, `get_skill`,
  `get_skill_file`, `search_memories`, `get_memory` and `end_agent_run`.
  Its agent prompt says the run is taskless and read-only and that the
  verifier answers one claim.
- New `prompts/feedback/verify.md` requires `run_id`, `claim_id`,
  `claim_digest`, `kind`, `claim_json` and `required_paths`, the claim's
  stored `paths`. It asks the directive's
  question: does the claim still reproduce on the current branch, or has it
  been fixed or retired since the observations were written? For a lesson
  claim, it asks whether the lesson is true of the current checkout and
  whether `when` names where it applies. The verifier reads the cited
  observations through `get_review_observations`. It submits `confirmed`
  only with evidence naming the files, lines or commands that confirm the
  claim, and `refuted` only with affirmative evidence that the claim is
  false or no longer reproduces. When it cannot decide, or a tool or the
  evidence fails, it submits `unverified` with its reason. It hashes
  each repository file with `git hash-object -- <path>` when it first
  reads it, recording null for a missing path, and submits every file
  its verdict rests on, each required path included, as
  `evidence_files`. A verifier that cannot hash a file it relies on
  submits `unverified`. It passes `git rev-parse HEAD` as `head_sha`,
  then calls `end_agent_run`. It carries no `submit_review` or
  report-write instruction.
- New `verifier.py`: `FEEDBACK_CLAIM_VERIFIER_AGENT_NAME =
  "feedback-claim-verifier"` and `FeedbackClaimVerifier(launcher, db)`.
  The verifier holds every cleanup task it starts until the task ends,
  and each cleanup logs its run id and outcome. Its storage calls run
  through `asyncio.to_thread`, as `review`'s do. `verify(claim, *,
  run_id, deadline)` returns the final claim record and never raises
  except on external cancellation:
  1. Start one owned task that renders `feedback/verify` through
     `asyncio.to_thread` and then awaits `launcher.launch(...,
     deadline=deadline)`. Wait for it with `asyncio.wait` until the
     deadline, without cancelling it. A render or launch error closes
     the claim with reason `launch failed: <error>`. A task still
     running at the deadline closes the claim with reason `verifier
     launch timed out`, and a cleanup task waits for it and retires any
     run it returns. A render that finishes late reaches `launch`,
     which refuses without a spawn once its timeout is non-positive.
  2. A launch that returns at or after the deadline closes the claim
     with reason `verifier launch timed out`, and its run is retired
     without assignment. Otherwise start `assign_verifier` as an owned
     task, with `expires_at` set to the current UTC time plus the
     deadline less the current loop time. Wait for it with
     `asyncio.wait` until the deadline. An assignment that fails,
     returns a claim not assigned to this run, or is still running at
     the deadline closes the claim with reason `verifier not
     assigned`. A cleanup task then retires the run and holds a
     running assignment until it ends. Otherwise wait on the launcher's
     completion registry until the deadline.
  3. When the run has not completed by the deadline, `close_claim` with
     reason `verifier timed out`, then start a cleanup task that calls
     `launcher.retire`. Otherwise `close_claim` with reason `verifier
     ended without a verdict`. A recorded verdict, the verifier's own
     `unverified` included, survives the close.
  4. On external cancellation, the launch or assignment task, or the
     known run, goes to the same cleanup, and the cancellation
     propagates.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/test_feedback_verifier.py
tests/feedback/test_feedback_agent.py
tests/workflows/test_feedback_claim_verifier_allowlist.py
tests/workflows/test_feedback_reviewer_allowlist.py -q`, then `uv run ruff
check src/ && uv run mypy src/` and a test-types audit of the changed
tests.

**Acceptance:**

- 2.1.1 - The verifier spawns `feedback-claim-verifier` with isolation
  `none`, a spawn timeout equal to the deadline less the loop time just
  before the spawn, less `COMPLETION_GRACE_SECONDS`, and a prompt
  carrying the claim id, digest, claim view and required paths, with no
  `submit_review` or daily-report instruction. test:
  `tests/feedback/test_feedback_verifier.py::test_verify_spawns_one_bound_verifier`.
- 2.1.2 - A launch failure and an agent that ends without a verdict
  each return an unverified record with its reason. test:
  `tests/feedback/test_feedback_verifier.py::test_verifier_failures_close_claim_unverified`.
- 2.1.3 - A verdict recorded before the agent fails or times out is kept.
  test:
  `tests/feedback/test_feedback_verifier.py::test_recorded_verdict_survives_agent_failure`.
- 2.1.4 - The verifier definition allows `submit_claim_verdict` and the
  readers, and no task, memory or review-learning write tool and no
  `submit_review`. test:
  `tests/workflows/test_feedback_claim_verifier_allowlist.py::test_verifier_allowlist_is_read_only_plus_verdict`.
- 2.1.5 - The reviewer launches through the extracted method. Its prompt
  still carries the report path and the `submit_review` instruction,
  `assign_reviewer` still records its run, and its spawn timeout is its
  given timeout less the preparation time before the spawn. `launch`
  refuses without a spawn when that timeout is non-positive. test:
  `tests/feedback/test_feedback_agent.py::test_review_launches_through_shared_launch`.
- 2.1.6 - A launch that ignores cancellation and blocks past the claim
  deadline, and a verifier still running at the deadline, each leave the
  claim unverified at the deadline. The late launch is never assigned, a
  verdict from either session after the close is refused, and each run
  is retired once its id is known. The fake launch finishes its work
  before it honors cancellation, as `run_thread_to_completion` does.
  test:
  `tests/feedback/test_feedback_verifier.py::test_deadline_closes_claim_then_retires_verifier`.
- 2.1.7 - `retire` terminates a `pending` or `running` run with status
  `error` and reason `feedback claim deadline passed`, and leaves a
  finished run alone. test:
  `tests/feedback/test_feedback_agent.py::test_retire_terminates_live_run_with_deadline_reason`.
- 2.1.8 - A prompt render that blocks its thread past the claim
  deadline leaves the claim unverified at the deadline while the event
  loop keeps running, and any run its late launch returns is retired
  without assignment. A launch that returns its run id at the deadline
  is never assigned, and its run is retired. An assignment that stalls
  across the deadline leaves the claim unverified at the deadline and
  the run retired. When it completes, the claim stays closed, and a
  verdict submitted while it stalls or after it completes is refused.
  test:
  `tests/feedback/test_feedback_verifier.py::test_late_render_launch_or_assignment_grants_no_authority`.

### 2.2 Verify stage gates task filing [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/feedback/service.py::FeedbackReviewService`
- `src/gobby/feedback/service.py::FeedbackReviewService.run_review`
- `src/gobby/feedback/actions.py::FeedbackActions`
- `src/gobby/feedback/actions.py::FeedbackActions.apply`
- `src/gobby/feedback/actions.py::FeedbackActions._create_task`
- `src/gobby/feedback/actions.py::_task_description`
- `src/gobby/feedback/cron.py::FEEDBACK_REVIEW_TIMEOUT_SECONDS`
- `src/gobby/runner_init/orchestration.py::init_orchestration`
- `tests/feedback/test_feedback_service.py::*` — scope-reason: confirm by default in the shared fake and cover each verdict path
- `tests/feedback/test_feedback_cron.py::*` — scope-reason: cover the four-hour timeout

**Granularity:** one leaf with eight acceptance items. The verify stage,
its gate on task filing, the freshness check and the cron timeout are one
behavior. Without the gate, verdicts would change nothing. A verify stage without the longer
timeout would be cancelled by cron before it finishes.

**Research context:**
- `run_review` (`service.py` 46-131) and its nested `checkpoint` are the
  insertion point: verification runs after the post-distill checkpoint and
  before `apply`.
- `apply` (`actions.py` 92-259) builds `actionable` from the clusters,
  returns early on a dry run (124-126), files up to `max_tasks_per_run`, and
  defers overflow (248-258). `_task_description` (379-405) builds the filed
  description. Its only production caller is `FeedbackActions._create_task`
  (345-376), which `apply` calls (216) and which forwards only
  `missing_paths` and `newest_touching_commit`.
- `FeedbackReviewService` is built positionally in
  `test_feedback_service.py` (`_service`, 352-359, and 1175, 1228) and in
  `init_orchestration` (651-656). `tests/servers/routes/test_feedback_routes.py`
  uses `SimpleNamespace` services.
- `FEEDBACK_REVIEW_TIMEOUT_SECONDS` (`cron.py` 26-28) is asserted at
  `test_feedback_cron.py` line 90. `_ensure_system_job` rewrites the stored
  job's timeout at the next start.
- `FeedbackClaimVerifier.verify` (2.1) returns the closed claim record.
  `build_claims`, `begin_verification` and `close_claim` come from 1.2.
- `apply` reads paths with `_cluster_cited_paths` and
  `_cluster_implementation_paths` (`actions.py` 415-433), normalizes
  cited paths with `_repo_relative_path` in `_missing_paths_at_head`
  (503-518), finds the checkout with `_gobby_repo_root`, and runs git
  through `_run_git` (492-493).
- The 2 h verify budget gives 20 claims about 6 minutes each, and the
  shared checkout takes commits through the night.

Implementation:
- `service.py` adds `VERIFY_TOTAL_DEADLINE_SECONDS =
  DISTILL_TOTAL_DEADLINE_SECONDS`. `FeedbackReviewService.__init__` gains
  keyword-only `verifier=None`.
- `run_review`, after the post-distill checkpoint:
  1. `build_claims`, then `begin_verification`.
  2. `_verify` fixes the stage deadline, `loop.time()` plus
     `VERIFY_TOTAL_DEADLINE_SECONDS`, and runs the claims in order. Each
     claim's deadline is the current loop time plus the remaining stage
     budget divided by the remaining claims. A share no longer than
     `COMPLETION_GRACE_SECONDS` closes the claim with reason `verify
     budget exhausted` without a spawn. With no verifier, each claim
     closes with reason `verifier unavailable`.
  3. The service keeps a read copy of the records for the digest. It
     builds `refuted` and `unverified` entries of `{claim_id, kind,
     observation_ids, proposal, verifier_agent_run_id}` plus `evidence` or
     `reason`.
  4. `apply` receives the confirmed task claims with their records and
     the deferred entries, and its `unverified` entries join the run's.
     An interrupted run may already have filed tasks or minted lessons.
     A retry verifies its claims again in a fresh run, and durable
     receipts stop a second write: open-task deduplication by
     observation id, and the lesson observation receipt (3.1).
  5. Rows are marked reviewed except the `failed` and `unverified`
     observations. The run is `partial` when either list is non-empty.
- `FeedbackActions.evidence_stale_reason(project_id, evidence_files)`
  recomputes each footprint blob in the gobby checkout: `git
  hash-object -- <path>` through `_run_git` for a regular file, and null
  for a missing path or a non-file. It returns `evidence changed since
  verification: <paths>` when any blob differs, `evidence check failed:
  <error>` when git fails, and `None` otherwise.
- `apply` takes `task_claims` and `deferred` and files only those claims,
  with deduplication, labels and the dry-run return unchanged. Its own cap
  and overflow code is removed, because `build_claims` owns both.
  `retained` keeps clusters with no task proposal. Inside its per-claim
  loop, before the deduplication append or the create, it calls
  `evidence_stale_reason` with the claim record's footprint. A reason
  skips the claim and returns it in a new `unverified` list with that
  reason. The check runs per claim, so a file changed by an earlier
  action counts against a later claim.
- `apply` passes each confirmed claim record through `_create_task` to
  `_task_description`, which adds `Verified by <verifier run> at <head>:
  <evidence>` from that record.
- `FEEDBACK_REVIEW_TIMEOUT_SECONDS` becomes the distill deadline plus the
  verify deadline plus the 300 s grace. The comment keeps the
  not-restart-protected reason.
- `init_orchestration` builds `FeedbackClaimVerifier(feedback_reviewer,
  runner.database)` and passes it as `verifier`.
- The shared service fake in `test_feedback_service.py` gets a verifier
  that confirms every claim, so the existing filing tests keep their
  meaning.

Consumers unchanged:
- `tests/servers/routes/test_feedback_routes.py` — no-edit-reason: it fakes the service through `SimpleNamespace` and never constructs `FeedbackReviewService`.
- `src/gobby/servers/routes/feedback.py` — no-edit-reason: it calls `run_review(dry_run=...)`, whose signature is unchanged, and returns the result dict as is.
- `src/gobby/app_context.py` — no-edit-reason: it names `FeedbackReviewService` only as a type.
- `src/gobby/runner.py` — no-edit-reason: it names `FeedbackReviewService` only as a type and calls `init_orchestration(runner, config)`, whose signature is unchanged.
- `src/gobby/feedback/__init__.py` — no-edit-reason: it re-exports `FeedbackReviewService` by name.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: it re-exports `init_orchestration` by name.
- `tests/test_runner_lifecycle.py` — no-edit-reason: it patches `gobby.runner_init.init_orchestration` out, so the feedback wiring never runs.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/
tests/servers/routes/test_feedback_routes.py -q`, then `uv run ruff check
src/ && uv run mypy src/` and a test-types audit of the changed tests.

**Acceptance:**

- 2.2.1 - Only a confirmed task claim files a task, and its description
  carries the verifier run, head and evidence. test:
  `tests/feedback/test_feedback_service.py::test_only_confirmed_task_claims_are_filed`.
- 2.2.2 - A refuted claim files nothing, its observations are marked
  reviewed, and it is listed in `refuted` with its evidence. test:
  `tests/feedback/test_feedback_service.py::test_refuted_claim_files_nothing_and_consumes_observations`.
- 2.2.3 - An unverified claim files nothing, its observations stay
  unreviewed, and the run finalizes `partial`. This covers a missing
  verifier and a verifier's own `unverified` verdict. test:
  `tests/feedback/test_feedback_service.py::test_unverified_claim_keeps_observations_retryable`.
- 2.2.4 - Each claim's deadline is its share of the remaining stage
  budget. A launch that ignores cancellation and blocks past its share,
  and an assignment that stalls past it, each close the claim
  unverified there, and later claims keep their shares.
  With every launch blocking that way, the stage still ends by its
  deadline. Once the budget is spent, the remaining claims close without
  a spawn, and a verdict after a close is refused. test:
  `tests/feedback/test_feedback_service.py::test_verify_budget_shares_and_exhaustion`.
- 2.2.5 - Cancellation during verification finalizes `interrupted`, and a
  later verdict is refused. test:
  `tests/feedback/test_feedback_service.py::test_cancelled_verify_refuses_late_verdict`.
- 2.2.6 - A dry run verifies every claim, files nothing and consumes no
  observation. test:
  `tests/feedback/test_feedback_service.py::test_dry_run_verifies_without_writes`.
- 2.2.7 - The cron timeout is 4 h 5 min, and the job is not
  restart-protected. test:
  `tests/feedback/test_feedback_cron.py::test_timeout_covers_distill_and_verify`.
- 2.2.8 - A confirmed task claim whose footprint changes after
  confirmation files nothing, keeps its observations unreviewed, and is
  listed in `unverified` with the stale paths. The cases are a changed
  cited file, committed or not; a changed file outside the claim's paths
  that the verifier read; a file that an earlier claim's action changes
  in the same run; and a failed recheck. test:
  `tests/feedback/test_feedback_service.py::test_stale_evidence_blocks_filing`.

## P3: Lessons and Reporting
`kind: framing`

**Goal:** confirmed lessons become provenance-tagged memories outside the
pushed index, and every verdict and lesson outcome is visible to operators.

### 3.1 Lesson minting [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/feedback/lessons.py`
- `src/gobby/feedback/service.py::FeedbackReviewService`
- `src/gobby/feedback/service.py::FeedbackReviewService.run_review`
- `src/gobby/memory/push_exclusion.py`
- `src/gobby/memory/facade.py::MemoryManagerFacadeMethods.create_memory`
- `src/gobby/memory/services/lifecycle.py::MemoryLifecycleService.create_memory`
- `src/gobby/memory/protocol.py::MemoryBackendProtocol.create`
- `src/gobby/memory/backends/storage_adapter.py::StorageAdapter.create`
- `src/gobby/memory/backends/null.py::NullBackend.create`
- `src/gobby/storage/memories_crud.py::MemoryCrudMixin.create_memory_with_outcome`
- `src/gobby/storage/memories_crud.py::DuplicateMemoryContentError`
- `src/gobby/runner_init/orchestration.py::init_orchestration`
- `tests/feedback/test_feedback_lessons.py`
- `tests/storage/test_memory_conditional_create.py`
- `tests/feedback/test_feedback_service.py::*` — scope-reason: cover minting inside a review run
- `tests/mcp_proxy/tools/test_memory_surface.py::*` — scope-reason: cover the feedback-lesson exclusion

**Granularity:** one leaf with ten hand-maintained production Target
files and seven acceptance items. Six of the files carry one
keyword-only flag down the memory create path. The conditional create
exists only for the mint, and minting without it would merge lesson
tags into an existing memory, so the two are one outcome.

**Research context:**
- `ReviewLearningMemoryManager` (`review_learning/service.py` 66-114)
  types the two calls: `create_memory(*, content, memory_type, project_id,
  source_type, source_session_id, tags, rationale, source_task_id,
  created_by_agent)` and `alist_memories(*, project_id, memory_type, limit,
  offset, tags_all, tags_any, tags_none, include_global)`. Review lessons
  are minted with `source_type="agent"`.
- `FeedbackActions._gobby_project_id` (`actions.py` 261-266) resolves the
  gobby project, which also owns the minted lessons.
- #23651 creates `src/gobby/memory/push_exclusion.py::PUSH_EXCLUDED_TAGS`,
  a tuple holding `review-lesson`. `memory_surface.py` imports it for the
  push search's `tags_none`, and `dream/candidates.py` imports it for
  the dream candidate filter. No leaf starts before #23651 closes. The
  Target is a bare path because the file has no index record until
  #23651 lands. The coordinator then narrows it to
  `src/gobby/memory/push_exclusion.py::PUSH_EXCLUDED_TAGS` (Rollout 3).
- `create_memory_with_outcome` (`src/gobby/storage/memories_crud.py`
  110-462) looks up a visible duplicate before its transaction (158-174).
  Inside
  the transaction it takes `pg_advisory_xact_lock` on
  `MEMORY_PROJECTION_FENCE_LOCK_KEY` (226-230) and locks the target row.
  An identical stored memory is returned with the new tags merged into
  it (248-250), keeping its own type, rationale and creator. A deleted
  row with the same content id is reactivated (435-436).
- `DuplicateMemoryContentError(ValueError)` (`memories_crud.py` 31-37)
  is the storage error for a content collision, and the dream apply
  loop catches it. `_content_scope(project_id, is_global)` (75-79) is
  the content-dedup scope.
- The facade's `create_memory` (`src/gobby/memory/facade.py` 147-174)
  calls the lifecycle's (`src/gobby/memory/services/lifecycle.py`
  297-346), which calls `backend.create`. `StorageAdapter.create`
  (`src/gobby/memory/backends/storage_adapter.py` 60-93) calls the
  storage method, and `NullBackend.create`
  (`src/gobby/memory/backends/null.py` 44-80) builds an unsaved record.
  `MemoryDreamManagerProtocol.create_memory` and
  `ReviewLearningMemoryManager` type the facade's call.
- `freeze_batch` (`feedback/storage.py` 77-95) admits a review only
  while no other run is `running`, under an advisory lock, so two runs
  never mint at once.
- `tests/memory/test_memory_manager_1.py::memory_manager` builds a real
  `MemoryManager` on the test hub.
- `runner.memory_manager` is set before `init_orchestration` runs the
  feedback wiring, and it can be `None` when memory services are down.
- A cluster's task and lesson claims share its observation ids. On a
  retry, `_find_duplicate` (`actions.py` 463-476) matches an open task by
  observation-id overlap before any theme comparison.

Implementation:
- New `lessons.py`: `FEEDBACK_LESSON_TAG = "feedback-lesson"` and
  `mint_lessons(claims, records, *, run_id, dry_run, memory_manager,
  project_id, checkpoint, evidence_stale_reason)`. It returns `minted`,
  `already_minted`, `content_exists`, `unverified` and `failed` entries.
  For each confirmed lesson claim:
  1. On a dry run, nothing is minted, and `skipped` notes it once.
  2. With no memory manager or no gobby project, the claim fails.
  3. `alist_memories(project_id=..., tags_all=["feedback-lesson"],
     tags_any=[feedback-observation tags], limit=1)` finding a memory
     records `already_minted` with that memory id.
  4. `evidence_stale_reason(project_id, <footprint>)` returning a reason
     records `unverified` with it and writes nothing.
  5. Otherwise `create_memory(content=<content>,
     memory_type=<memory_type>, project_id=<gobby project>,
     source_type="agent", tags=<tags>, rationale=<when>,
     created_by_agent="feedback-reviewer", if_content_new=True)`
     records `minted` with the memory id. The tags are
     `feedback-lesson`, `feedback-run:<run id>`,
     `feedback-claim:<claim_id>`, `feedback-verifier:<verifier agent run
     id>` and `feedback-observation:<id>` for each observation.
     `DuplicateMemoryContentError` records `content_exists`, and any
     other exception records `failed`.
  6. The service checkpoints after each claim.

  `test_feedback_lessons.py` runs the real `MemoryManager` on the
  isolated test hub.
- `MemoryCrudMixin.create_memory_with_outcome` gains keyword-only
  `if_content_new: bool = False`. When it is set, the method looks for
  a row with the same normalized content in `_content_scope(project_id,
  is_global)` at any visibility, deleted rows included, after taking
  the projection fence lock. A match raises
  `DuplicateMemoryContentError` naming that memory id, the transaction
  rolls back, and nothing is written. The error's docstring covers any
  stored memory in scope.
- `MemoryBackendProtocol.create`, `StorageAdapter.create`,
  `MemoryLifecycleService.create_memory` and
  `MemoryManagerFacadeMethods.create_memory` gain the same keyword and
  pass it down. `NullBackend.create` accepts it and ignores it, since it
  stores nothing. The default keeps every other caller unchanged, and
  the two protocols that type the facade's call stay satisfied.
- `FeedbackReviewService.__init__` gains keyword-only `memory_manager=None`.
  `run_review` mints after `apply` and passes
  `FeedbackActions.evidence_stale_reason` to `mint_lessons`. Lesson
  `unverified` entries join the run's `unverified` list and lesson
  failures join `failed`, so their observations stay unreviewed and the
  run is `partial`. The result gains `lessons_minted`.
- `PUSH_EXCLUDED_TAGS` becomes `("review-lesson", "feedback-lesson")`.
- `init_orchestration` passes `memory_manager=runner.memory_manager`.

Consumers unchanged:
- `tests/servers/routes/test_feedback_routes.py` — no-edit-reason: it fakes the service through `SimpleNamespace` and never constructs `FeedbackReviewService`.
- `src/gobby/servers/routes/feedback.py` — no-edit-reason: it calls `run_review(dry_run=...)`, whose signature is unchanged, and returns the result dict as is.
- `src/gobby/app_context.py` — no-edit-reason: it names `FeedbackReviewService` only as a type.
- `src/gobby/runner.py` — no-edit-reason: it names `FeedbackReviewService` only as a type and calls `init_orchestration(runner, config)`, whose signature is unchanged.
- `src/gobby/feedback/__init__.py` — no-edit-reason: it re-exports `FeedbackReviewService` by name.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: it re-exports `init_orchestration` by name.
- `tests/test_runner_lifecycle.py` — no-edit-reason: it patches `gobby.runner_init.init_orchestration` out, so the feedback wiring never runs.
- `src/gobby/mcp_proxy/tools/memory_write.py` — no-edit-reason: it calls `create_memory` without the new keyword, whose default keeps the merging create.
- `src/gobby/memory/backends/__init__.py` — no-edit-reason: it builds `NullBackend` behind `MemoryBackendProtocol` and never names the new keyword.
- `src/gobby/memory/dream/apply.py` — no-edit-reason: it catches `DuplicateMemoryContentError` by type, and the class gains only a docstring change.
- `src/gobby/memory/dream/storage_actions.py` — no-edit-reason: it raises `DuplicateMemoryContentError` for refresh collisions as before.
- `tests/cli/test_memory_cli.py` — no-edit-reason: it asserts the CLI's `create_memory` call, which never passes the new keyword.
- `tests/mcp_proxy/tools/test_memory_tools.py` — no-edit-reason: it inspects the MCP tool's `create_memory` keywords, which never include the new one.
- `tests/mcp_proxy/tools/test_memory_write_cap.py` — no-edit-reason: it checks the write cap before `create_memory` runs.
- `tests/memory/test_write_cap.py` — no-edit-reason: it checks the content cap the lifecycle enforces before the backend call.
- `tests/memory/test_memory_manager_1.py` — no-edit-reason: it creates memories through the default merging path.
- `tests/memory/test_backends.py` — no-edit-reason: it calls `create` without the new keyword, whose default keeps today's behavior.
- `tests/memory/test_create_supersedes.py` — no-edit-reason: it covers supersession and write outcomes through the default path.
- `tests/memory/test_manager_graph_search.py` — no-edit-reason: it stubs `backend.create` with an `AsyncMock`, which accepts the new keyword.
- `tests/memory/test_manager_knowledge_graph_wiring.py` — no-edit-reason: it stubs `backend.create` with an `AsyncMock`, which accepts the new keyword.
- `tests/memory/test_memory_protocol.py` — no-edit-reason: its mock backend is checked for protocol shape only and is never called with the new keyword.
- `tests/storage/test_storage_memories.py` — no-edit-reason: it covers the default create and the existing `DuplicateMemoryContentError` raise sites.
- `tests/memory/test_dream.py` — no-edit-reason: it asserts `DuplicateMemoryContentError` stays a `ValueError` subclass, which still holds.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/
tests/mcp_proxy/tools/test_memory_surface.py
tests/storage/test_memory_conditional_create.py
tests/storage/test_storage_memories.py tests/memory/test_backends.py
tests/memory/test_memory_protocol.py tests/memory/test_create_supersedes.py
-q`, then `uv run ruff check
src/ && uv run mypy src/` and a test-types audit of the changed tests.

**Acceptance:**

- 3.1.1 - A confirmed lesson is minted in the gobby project with its
  content, type, `when` as rationale and the five provenance tag kinds.
  test: `tests/feedback/test_feedback_lessons.py::test_confirmed_lesson_mints_tagged_memory`.
- 3.1.2 - A lesson whose observation is already cited by a
  `feedback-lesson` memory is recorded `already_minted`. A lesson whose
  content matches an ordinary, dream-hidden or deleted memory, with any
  type or rationale, is recorded `content_exists`. Neither creates or changes a
  memory, and the existing memory keeps its tags, type and rationale.
  test:
  `tests/feedback/test_feedback_lessons.py::test_existing_receipt_or_content_writes_nothing`.
- 3.1.3 - A refuted, unverified or stale lesson mints nothing. A stale
  lesson, whose footprint changed after confirmation, keeps its
  observations unreviewed and is listed in `unverified`. test:
  `tests/feedback/test_feedback_service.py::test_unconfirmed_lessons_mint_nothing`.
- 3.1.4 - A failed mint or a missing memory manager leaves the lesson's
  observations unreviewed and the run `partial`. When a cluster's task and
  lesson claims share observations, an unverified or failed sibling keeps
  them unreviewed even when the other claim succeeds. The retry files no
  second task, through open-task deduplication, and mints no second
  lesson, through the observation receipt. The parametrized cases are a
  confirmed task with an unverified lesson, an unverified task with a
  confirmed lesson, and a confirmed task with a failed mint. test:
  `tests/feedback/test_feedback_service.py::test_unfinished_claims_keep_observations_retryable`.
- 3.1.5 - A dry run mints no memory. test:
  `tests/feedback/test_feedback_lessons.py::test_dry_run_mints_nothing`.
- 3.1.6 - The pushed memory index never returns a `feedback-lesson`
  memory. test:
  `tests/mcp_proxy/tools/test_memory_surface.py::test_surface_excludes_feedback_lessons`.
- 3.1.7 - A conditional create that waits at the projection fence while
  another writer commits identical content raises
  `DuplicateMemoryContentError` and writes nothing. The stored memory
  keeps the first writer's tags, type and rationale. A barrier holds the
  fence until both creates queue, the conditional one after its
  pre-transaction lookup found nothing. test:
  `tests/storage/test_memory_conditional_create.py::test_conditional_create_refuses_content_committed_while_waiting`.

### 3.2 Digest and results reader [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/feedback/digest.py::render_digest`
- `src/gobby/feedback/report.py::merge_report_inputs`
- `src/gobby/feedback/storage.py::FeedbackReviewStore.results_page`
- `tests/feedback/test_feedback_report.py::*` — scope-reason: cover the merged verdict and lesson lists
- `tests/feedback/test_feedback_service.py::*` — scope-reason: cover the rendered verification and lesson sections
- `tests/reports/test_tools.py::*` — scope-reason: cover the results reader's new outcome lists

**Research context:**
- `render_digest` (`digest.py` 67-143) renders Clusters, Actions, Shirked
  found work and Other-label audit, with a dry-run banner.
- `merge_report_inputs` (`report.py` 22-57) merges only `filed`,
  `suppressed`, `skipped` and `failed` across a day's runs, keyed by
  `task_id` or the entry text. New lists missing from it vanish from the
  cumulative daily report.
- `results_page` (`storage.py` 283-310) filters `filed`, `suppressed`,
  `deferred`, `failed` and `retained` to the page's observation ids.
- `tests/reports/test_tools.py::test_feedback_readers_page_frozen_observations_and_actual_outcomes`
  drives the readers through `create_feedback_registry`.

Implementation:
- `render_digest` adds a Verification section and a Lessons section:
  - Verification lists each claim by run and claim id, with kind, state,
    verifier run and head, plus the evidence for `confirmed` and
    `refuted` or the reason for `unverified`. A confirmed claim that went
    stale shows the reason from its `unverified` entry, joined to the
    record by run and claim id.
  - Lessons lists minted memory ids, `already_minted` with the existing
    memory id, `content_exists`, and deferred lesson overflow.
- `merge_report_inputs` also merges `refuted`, `unverified`, `minted`,
  `already_minted`, `content_exists` and `deferred`. It carries every run's verification
  claim records with their `run_id`, keyed by `(run_id, claim_id)`, so two
  runs' `c0-task` claims stay distinct. `render_digest` reads those merged
  records.
- `results_page` adds `refuted`, `unverified`, `minted`,
  `already_minted` and `content_exists` to `outcomes`, plus the
  verification records of the page's claims.

Consumers unchanged:
- `src/gobby/servers/routes/feedback.py` — no-edit-reason: it returns the `results_page` dict as is, so the new keys pass through.
- `tests/servers/routes/test_feedback_routes.py` — no-edit-reason: it fakes `results_page` and checks only routing.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/ tests/reports/test_tools.py
-q`, then `uv run ruff check src/ && uv run mypy src/` and a test-types
audit of the changed tests.

**Acceptance:**

- 3.2.1 - The digest lists each refuted claim with its evidence and each
  unverified claim with its reason. test:
  `tests/feedback/test_feedback_service.py::test_digest_lists_rejected_claims_with_reasons`.
- 3.2.2 - The digest lists minted and already-minted lessons with memory
  ids, and `content_exists` lessons. test:
  `tests/feedback/test_feedback_service.py::test_digest_lists_lesson_outcomes`.
- 3.2.3 - The day's cumulative report keeps every run's verdict, lesson
  and deferred lists and verification records, each with its verifier
  run, head and evidence. The cases are two runs that both have a
  `c0-task` claim, a confirmed claim whose task was deduplicated, and
  deferred lesson overflow. test:
  `tests/feedback/test_feedback_report.py::test_merge_keeps_verdict_and_lesson_outcomes`.
- 3.2.4 - The results reader returns the new outcome lists and the page's
  verification records. test:
  `tests/reports/test_tools.py::test_results_reader_returns_verification_outcomes`.

## P4: Documentation
`kind: framing`

**Goal:** the feedback references and guides describe the verify stage,
the lesson memories and the new outcomes.

### 4.1 Feedback review documentation [category: docs] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/review/feedback.md`
- `src/gobby/install/shared/skills/gobby/references/review/outcomes.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** stale text sits at:
- `feedback.md` 42 (`submit_review` only) and 56-57 (dry runs);
- `outcomes.md` 58 (labels of filed tasks);
- `cli-commands.md` 1193-1194 and 1205-1212 (the `#feedback-review`
  section);
- `http-endpoints.md` 870-871 and 884-895 (the `#feedback-review`
  section).

Implementation:
- `feedback.md` describes the verify stage: one verifier per claim, the
  verdict tool, default reject, the budget and the three outcomes.
- `outcomes.md` describes the verifier line in filed tasks, the
  `feedback-lesson` memories and their tags, and that minted lessons stay
  out of the pushed index.
- `cli-commands.md` and `http-endpoints.md` say that a run with an
  unverified claim is `partial` and that its observations return next
  night, that dry runs also spawn verifiers, and that verifiers submit
  through `submit_claim_verdict`. The anchors stay.
- The two references refresh their `_Last verified_` dates.

Planned verification:
`GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_library.py
-q` and a read-through against the shipped prompts.

**Acceptance:**

- 4.1.1 - The feedback reference documents per-claim verification and
  default reject. behavior: "submit_claim_verdict" in
  `src/gobby/install/shared/skills/gobby/references/review/feedback.md`.
- 4.1.2 - The outcomes reference documents lesson memories. behavior:
  "feedback-lesson" in
  `src/gobby/install/shared/skills/gobby/references/review/outcomes.md`.
- 4.1.3 - The CLI guide documents unverified claims. behavior:
  "unverified" in `docs/guides/cli-commands.md`.

## D1 Dream skips push-excluded memories
`kind: deferred`

The memory dream's refresh can rewrite a memory's tags and drop
`feedback-lesson`, which would expose the lesson in the pushed index. Under
the Orchestrator's Q4 ruling, #23651 (Memory dream can rewrite away the
review-lesson tag, exposing held lessons in the pushed memory index) makes
the dream candidate listing skip every tag in `PUSH_EXCLUDED_TAGS`
(`src/gobby/memory/push_exclusion.py`). 3.1 adds `feedback-lesson` to
that list.

#23651 is a prerequisite of the whole implementation. At expansion the
coordinator adds a `blocked-by` edge from the implementation root to
#23651, which also places #23651 in the root's dependency closure, and
adds the `deferred-from:feedback-verify-stage:D1` label to #23651. A task
is ready only when its parent is ready, so no leaf starts and no lesson
is minted before #23651 closes. If #23651 has already closed
`completed` at expansion, the edge is still added and this section
records its landing commit.

```yaml
deferral:
  task_ref: "#23651"
  reason: "Lane 6 owns the dream fix for every push-excluded tag; this plan adds feedback-lesson to the shared list."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

- D1.1 - The memory dream never selects a memory carrying a
  push-excluded tag, `feedback-lesson` included.
  behavior: "The dream candidate listing excludes every memory carrying a tag in the push-exclusion list"

## Rollout
`kind: framing`

1. Leaves land through the lane review path in order 1.1, 1.2, 2.1, 2.2,
   3.1, 3.2 and 4.1.
2. The verifier agent, the verify prompt and the revised review prompt
   sync at daemon start, and `_ensure_system_job` applies the 4 h 5 min
   timeout then. The Merge Manager announces that restart globally.
3. Lesson minting starts with the first nightly run after 3.1 is live.
   The implementation root is blocked by #23651 (D1), so the dream
   protection lands before any leaf starts. It is live from the restart
   that ships it, no later than 3.1's restart. Once #23651 lands, the
   coordinator narrows 3.1's `src/gobby/memory/push_exclusion.py` Target
   to `::PUSH_EXCLUDED_TAGS` and re-validates before expansion.
4. After the first night, read the run's digest. Check the Verification
   and Lessons sections, `gobby feedback status`, and the
   `feedback-lesson` memories through `gobby-memory:list_memories`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-06 01:25 CDT: Draft by the Lane 7 Plan Writer gobby#15434 on
  #21312 (Add an EDV-style verify stage that mints lesson memories from
  feedback clusters). It applies the Orchestrator's rulings Q1 to Q4.
- 2026-10-06 01:36 CDT: Enhancer run 302b0257 proposed E1 to E6, and the
  Orchestrator accepted all six at 01:34 CT.
  - E1: the report merge keeps verification records by run and claim
    id, plus the deferred list (3.2).
  - E2: `_create_task` carries the claim record to the description (2.2).
  - E3: `memory_type` is a required lesson field (1.1).
  - E4: sibling claims sharing observations are covered in 3.1.4.
  - E5: the extracted `launch` boundary is stated and asserted in 2.1.1
    and 2.1.5.
  - E6: verdict preservation is one `UPDATE` and covers stale and forged
    copies (Decision Record 6, 1.2).
  - The draft entry's time is corrected to 01:25 CDT.
- 2026-10-06 02:01 CDT: Council dialogue with Adv1 gobby#15401 on
  `4f2c74ff`. All five blocking findings and both clarifications are
  accepted.
  - B1: #23651 blocks the implementation root at expansion, D1 no
    longer depends on 3.1, and D1.1 matches #23651's criterion 1. The
    Orchestrator pinned the shared list at 01:59 CT as
    `src/gobby/memory/push_exclusion.py::PUSH_EXCLUDED_TAGS`. 3.1
    targets the file by bare path until #23651 lands.
  - B2: one monotonic stage deadline, per-claim deadlines around
    rendering, launch, assignment and the wait, and a spawn timeout
    measured at spawn (Decision Record 8, 2.1, 2.2.4).
  - B3: freshness at write by path fingerprint, with a `HEAD` check for
    claims with no paths (Decision Record 12, 2.2.8, 3.1.3). A bare
    `HEAD` check for every claim is rejected.
  - B4: identical stored content records `content_exists` and writes
    nothing, tested on the real memory manager (3.1.2).
  - B5: 1.2 states the claim view and verdict authority, and 3.1
    states the mint payload and tags.
  - Clarifications: a verifier may submit `unverified`, and lesson
    content is capped at 3,000 characters at submission.
- 2026-10-06 02:32 CDT: Council dialogue with Adv1 gobby#15401 on
  `9e25dfc1`. B1, B5 and both clarifications are resolved, and B2, B3
  and B4 are revised.
  - B2: the service enforces each claim deadline itself. It waits for
    an owned launch task only until the deadline, closes the claim
    first, and retires the verifier run through `terminate_agent_run`.
    A late launch is never assigned and is retired once it returns. The
    spawn timeout is a backstop (Decision Record 8, 2.1, 2.1.6, 2.1.7,
    2.2.4).
  - B3: the verifier reports a blob footprint of every file its verdict
    rests on, and a confirmed footprint must cover the claim's paths.
    The daemon rechecks it inside the task and mint loops, before each
    write. The launch baseline and the `HEAD` fallback are removed
    (Decision Record 12, 1.2, 2.2.8, 3.1.3).
  - B4: the mint uses a conditional create that re-checks content under
    the storage fence lock and raises `DuplicateMemoryContentError`
    (Decision Record 9, 3.1, 3.1.7).
  - Retry safety rests on fresh-run verification and durable receipts
    (2.2, step 4).
- 2026-10-06 02:43 CDT: Council dialogue with Adv1 gobby#15401 on
  `3629bc20`. B3 and B4 are resolved, and B2's launch supervision and
  retirement are accepted.
  - B2: prompt rendering moves into the owned launch task through
    `asyncio.to_thread`, and a launch that returns at or after the
    deadline is never assigned (Decision Record 8, 2.1, 2.1.5,
    2.1.8).
  - Decision Record 8 and 2.1.1 state the spawn timeout as its
    calculation, and 3.1's Granularity names its ten production
    Target files.
- 2026-10-06 02:50 CDT: Council dialogue with Adv1 gobby#15401 on
  `b4a66e1d`. Render supervision, the boundary guard and the wording
  fixes are resolved.
  - B2: assignment runs as an owned task under the claim deadline.
    `assign_verifier` stores the claim's expiry and assigns only a
    `pending` claim before it, and a verdict at or after the expiry
    is refused, so a delayed assignment neither reopens a closed
    claim nor grants authority past the deadline (Decision Record 8,
    1.2, 1.2.3, 2.1, 2.1.8, 2.2.4).
- 2026-10-06 02:53 CDT: Consensus with Adv1 gobby#15401 on `da293e83`.
  B1 to B5 and both clarifications are resolved, with no blocking
  findings or open wording fixes.

## V2: Verification
`kind: verification`

Each leaf runs its planned verification after its final edit. After the
last leaf lands:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/feedback/ tests/reports/test_tools.py tests/servers/routes/test_feedback_routes.py tests/mcp_proxy/tools/test_memory_surface.py tests/workflows/test_feedback_reviewer_allowlist.py tests/workflows/test_feedback_claim_verifier_allowlist.py tests/config/test_app_config.py tests/config/test_runtime_config_contract.py tests/contracts/test_http_corpus.py tests/skills/test_reference_library.py tests/storage/test_memory_conditional_create.py tests/storage/test_storage_memories.py tests/memory/test_backends.py tests/memory/test_memory_protocol.py tests/memory/test_create_supersedes.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/feedback-verify-stage.md -p /Users/josh/Projects/gobby
```

On an isolated test daemon with seeded feedback rows, run `gobby feedback
review --dry-run`. Every claim must record a verdict or an unverified
reason, and nothing may be filed, minted or consumed.
