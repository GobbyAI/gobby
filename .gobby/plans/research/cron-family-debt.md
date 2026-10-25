# Cron family debt pass — #24961

Research only; recommendations await disposition through Triage, the Plan Writer,
and Plan Adversary before operator approval. No implementation or canonical plan
was changed.

Started from `865ccb5227730d5282450d45d20e77724907b8bb`; final section references
use `2e7da824431fd49fa7161a5c197e4c2a2516e5e1`. Read both with `git show`, never
the working-tree plan. The latter removes the superseded M1; references below
therefore identify narrative sections and acceptance items. Source observations
are from the main `0.5.0` checkout at that revision, using gcode outlines,
symbol-at, literal sweeps, and one bound range read. Some navigations used
`--allow-stale` while an index refresh ran; source symbols were read from the
checkout and no source files were dirty in the initial status inventory.

## Debt not to port

This pass is necessary: the plan's broad “matches Python” requirements would
otherwise preserve several accidental behaviors. Existing native admission,
runtime cancellation, repositories, and corpus seams should carry the fixes;
there is no justification for another scheduler or transport.

**Disposition and parity rule for every recommendation below:** `DROP` means a
proposed behavior change, not an approved waiver. Keep all 28 Cron ledger surfaces
in scope. When approved, retain their eventual `delegated` status and cite a
decision-backed, case-specific waiver in their ledger owner notes, service
contract, characterization vectors, compare checks, and native replay assertions.
Each waiver must identify the decision task/ref, affected operation/input,
Python result, required native result/effect, and rationale. S1 below is a shared
mechanism gap: prose alone does not currently make the gates honor that waiver.
Until it is resolved, these changes cannot claim successful parity. No whole
surface is proposed for removal. If disposition removes one, update its source
group in `docs/contracts/parity-ledger-status.d/` and regenerate the ledger with
`dropped by decision #N`, under #21572; an owner reassignment is insufficient.

### D1. Client schedule errors become server errors on PATCH — DROP

- **Python behavior/evidence:** create catches `ValueError` and returns 400
  (`src/gobby/servers/routes/cron.py:175-202`); PATCH catches only HTTP and
  protection errors before its generic 500 handler (`:219-255`). Storage rejects
  an enabled job without a valid future schedule with `ValueError`
  (`src/gobby/storage/cron.py:425-441`). A PATCH such as an invalid `cron_expr`
  therefore reaches a generic 500, although the caller supplied the bad value.
- **Why/recommendation:** drop the accidental 500; map schedule/domain validation
  failures consistently to 400, preserving 403 protection and genuine internal
  failures. Do not expose database diagnostics as the new error body.
- **Amend:** §§1.1/1.3/3.2/3.4/3.5, especially 3.2.2 and its exact-error promise.
  Add invalid expression, invalid timestamp, and no-future-schedule cases with
  unchanged-row assertions.
- **Waiver:** cite the approved D1 decision for the affected PATCH rejection
  cases (500 to 400), with explicit expected native body and no write. See S1/S3.

### D2. PATCH loses presence information and rejects valid merged updates — DROP

- **Python behavior/evidence:** `UpdateCronJobRequest` uses `None` for every
  absent optional field and requires schedule data from the request alone when
  `schedule_type` is supplied (`src/gobby/servers/routes/cron.py:54-78`). The
  handler drops every `None` value (`:238-243`). Thus explicit
  `description:null` cannot clear a description, and sending the existing
  `schedule_type` without repeating its already-stored required field is rejected.
  Storage already merges a candidate with the stored job (`cron.py:425-441`)
  and has explicit display-name clearing (`:419-421`).
- **Why/recommendation:** drop the transport accident. Preserve absent versus
  explicit null; permit null only for nullable fields, and validate the merged
  candidate. An omitted field stays unchanged; null must not erase required
  schedule/action fields into an invalid enabled job. Preserve the current
  empty-string display-name reset as well.
- **Amend:** §§1.1/1.3/3.2/3.4; replace 3.2's blanket preservation of ignored
  empty fields with field-by-field semantics shared with MCP and CLI adapters.
- **Waiver:** cite approved D2 cases for nullable clears and a same-type partial
  schedule update; keep rejected-null cases explicit. Compare post-images,
  not just success envelopes. This changes behavior and needs disposition.

### D3. Hidden first-50 job window and post-limit retirement filtering — DROP

- **Python behavior/evidence:** HTTP list exposes only project/enabled filters,
  uses storage defaults, filters retired automation afterward, and calls page
  length `count` (`src/gobby/servers/routes/cron.py:149-173`). Storage defaults
  to 50, orders only by `created_at DESC`, and applies `LIMIT` before returning
  (`src/gobby/storage/cron.py:206-249`). It already supports filtering removed
  automation inside SQL. Retained retired rows are real: cleanup deliberately
  excludes `synthesis-reports` (`:694-698`).
- **Why/recommendation:** drop unreachable older jobs and underfilled pages
  caused by filtering after the limit. Reuse the SQL filter, add a bounded
  continuation contract with deterministic `(created_at,id)` order, default 50,
  and preserve `count` as returned-page length. Specify the request bound and
  continuation field before approval; do not silently remove all limits.
- **Amend:** §§1.1/1.3/3.1/3.4/3.5, especially 3.1.1. Test more than 50 jobs,
  retired rows inside the newest window, and equal creation timestamps.
- **Waiver:** cite approved D3 cases for changed page membership/order and
  continuation fields. CLI/MCP owner handoffs must state their traversal rules.
  This proposal needs a concrete pagination decision before plan approval.

### D4. Toggle can enable a job that has no future schedule — DROP

- **Python behavior/evidence:** create rejects enabled jobs whose computed
  schedule is absent (`src/gobby/storage/cron.py:134-137`); update applies the
  same invariant (`:425-441`). Toggle locks the row but writes
  `enabled=true,next_run_at=NULL` without checking the computed result
  (`:763-794`). `compute_next_run` returns None for an expired one-shot
  (`src/gobby/storage/cron_schedule.py:54-71`).
- **Why/recommendation:** reject toggle-on of an expired/invalid ordinary job
  with a typed validation error and leave it unchanged. Keep the intentional
  system park contract (`enabled=true,next_run_at=NULL`), which is a distinct
  protected service operation; do not prohibit parking globally.
- **Amend:** §§1.1/1.3/3.2/3.4, especially 1.3.1 and 3.2.4. Include expired
  ordinary once, invalid stored schedule, and protected park/wake vectors.
- **Waiver:** cite approved D4 for toggle's old success/new validation rejection
  and unchanged row; leave park/wake parity unwaived.

### D5. Schedule edits validate and commit outside one row lock — DROP

- **Python behavior/evidence:** `_update_job` reads the job, builds a candidate,
  computes `next_run_at`, then calls `_update_job_fields`
  (`src/gobby/storage/cron.py:386-445`). The latter executes an UPDATE by ID
  without a row-version condition (`:332-357`). Concurrent interval/timezone
  edits can each calculate against old fields, leaving a final combination of
  fields with a next instant computed from a different combination. Toggle's
  `FOR UPDATE` (`:769-772`) and scheduled admission's existing transactional CAS
  (`src/gobby/scheduler/scheduler.py:199-225`) show reusable local patterns.
- **Why/recommendation:** lock the job row while merging, checking protection,
  validating and writing its schedule, or use an equivalent checked version.
  Do not duplicate the scheduler's global admission lock for ordinary edits.
- **Amend:** §§1.3/2.1/3.2. Add a deterministic concurrent interval/timezone
  mutation test, plus mutation-versus-admission ordering. Each committed result
  must satisfy the validated candidate invariant.
- **Waiver:** cite approved D5 as a concurrency-contract correction; keep normal
  sequential fixtures equal. Add an explicit native interleaving expectation
  rather than encoding a race outcome as a Python golden response.

### D6. Shell timeout cleanup is weaker than cancellation cleanup — DROP

- **Python behavior/evidence:** cancellation kills and awaits the subprocess;
  timeout calls only `terminate()` and raises immediately
  (`src/gobby/scheduler/executor.py:739-755`). A child that ignores TERM can
  survive after the cron run is reported failed.
- **Why/recommendation:** keep §2.3's already-proposed kill-and-await guarantee.
  State that it deliberately corrects Python's timeout behavior. Test a temporary
  child that ignores TERM; verify no owned process remains on both timeout and
  cancellation. No live operator process needs to be touched for this proof.
- **Amend:** §§1.1/2.3/3.5, especially 2.3.2. The present safety requirement is
  sound, but universal lifecycle-vector equality needs this explicit exception.
- **Waiver:** cite approved D6 for cleanup effects, preserving the timeout
  failure envelope unless separately changed. Lifecycle gates must assert the
  stronger native cleanup rather than require the Python leak.

### D7. Full shell output is unbounded — DROP

- **Python behavior/evidence:** `process.communicate()` accumulates all combined
  stdout/stderr, decodes it in full, and keeps full output even when only the
  error preview is truncated (`src/gobby/scheduler/executor.py:719-738`).
  §2.3 explicitly says to store full output. The 2,000-character error tail is
  an error-message bound, not a memory or persisted-output bound.
- **Why/recommendation:** drop unlimited capture. Drain both process output and
  process lifetime safely while retaining a bounded output prefix/tail with
  explicit truncation metadata and total observed byte count. Recommend one
  concrete fixed byte budget selected in plan disposition; avoid a new generic
  output-storage service or extra configurable policy without a consumer.
- **Amend:** §§1.1/1.2/2.3/3.1/3.4, including output serialization and 2.3.2.
  Test output above the chosen bound and multibyte/invalid UTF-8 boundaries.
- **Waiver:** cite approved D7 for oversized-output vectors and new metadata;
  preserve ordinary output exactly. The bound is a resource-policy decision
  to resolve before approval, not an implementer choice.

### K1. Keep the concurrency, ownership, and durable-child contracts

- **Evidence/call:** KEEP scheduled CAS and insertion in one rollback boundary
  (`scheduler.py:199-225`), shared manual admission (§2.1), machine/owner-scoped
  recovery (§2.2), and child-link-before-dispatched semantics (§§2.4-2.5).
  These are correctness contracts, reinforced by memories `4c074c4e` and
  `ce35d69d`, rather than debt to simplify away. No waiver applies.
- **Amend:** retain §§1.1/2.1-2.5. Preserve the prune owner #24702 and existing
  mailbox scope ruling; this pass proposes no second prune binding and no
  dependency on parked #22340. No new architecture is needed for those duties.

## Systemic issues

### S1. Within-surface behavior waivers are not represented by shared parity gates

- **Evidence:** the ledger supports whole-surface `dropped by decision #N`
  (`docs/contracts/parity-ledger.md:3-12`). The corpus has only proxy/native
  parity plus python/gdaemon origin (`tests/contracts/http_corpus.py:25-28`),
  replays the recorded response (`:260-270`), and Rust replay asserts literal
  normalized equality (`crates/gdaemon/tests/http_contracts.rs:403-435`,
  specifically `:423`). Live comparison compares status/headers/body directly
  (`crates/gdaemon/src/front_door/routes.rs:333-361`); its outcome enum and
  record contain no decision-backed waiver result
  (`crates/gdaemon/src/front_door/compare.rs:18-40`). No waiver references were
  found in the scoped front-door/corpus literal sweeps.
- **Cron implication/shared fix:** D1-D7 need a single shared approved-exception
  contract consumed by recording/replay and live compare. Preserve Python
  evidence, require the approved native response/effect, report the decision
  ref separately, and fail on any difference outside that exact case. Never
  hide it with broad masks, `Skipped`, or rewriting origin as gdaemon. Reuse
  #21572's ledger/exit machinery; coordinate with Skills debt pass #24959 rather
  than inventing a Cron waiver format. A prose note is presently insufficient.
- **Affected families:** every family using this shared HTTP parity path:
  Runtime/auth, health, terminal, API keys/nodes, sessions, tasks, agents,
  memory, rules/workflows, pipelines, plus every #21568 family: Skills, Plans,
  Communications, source control/GitHub/sync, Projects/files/hub documents,
  wiki/code-index, AI providers/LLM/local models, Voice, admin/metrics/traces/
  telemetry, Web UI serving, install/setup, and WS chat's HTTP handshakes.
  This identifies mechanism exposure; it does not assert every family needs a
  waiver. Non-HTTP adapters need the same decision references in their own gates.
- **Plan amendment:** §§1.1/3.5/4.1 and parity ownership; make the common
  mechanism a prerequisite before counting approved differences as parity.

### S2. Shared compare “passes” can consist entirely of skipped evidence

- **Evidence:** `Report::passes` requires `total > 0` and absence of Diff,
  NativeError and Mismatch; Skipped is accepted
  (`crates/gdaemon/src/front_door/compare.rs:97-105`). No per-route or per-case
  coverage requirement appears in that predicate. Cron's §3.5.3 correctly asks
  for nonzero stable evidence per case, but the shared predicate alone cannot
  establish it. The planned race-aware skips make this material for Cron.
- **Shared fix:** add a shared explicit required-case coverage assertion,
  counting Match/Verified evidence only. Keep Skipped for diagnostic races;
  zero stable evidence for any required case blocks takeover. This is evidence
  accounting, not another scheduler or family-specific soak engine.
- **Affected families:** the same shared HTTP consumer set named in S1; most
  exposed are mutable sessions, tasks, agents, pipelines, memory, Plans,
  Projects, Communications and Cron. Those names indicate concurrent-state
  consumers, not separately verified defects in their native implementations.
- **Plan amendment:** §§3.5/4.1; require the common coverage proof in addition
  to zero mismatches. An intentional waiver must count its explicit expected
  native assertion, never an unexamined skip.

### S3. Transport error policy is fragmented across adapters

- **Evidence:** Cron's create/PATCH split is D1. Projects independently maps
  checkout exceptions to nested `{error,message}` detail at
  `src/gobby/servers/routes/projects.py:108-112`; Cron uses string detail.
  Authentication middleware emits `{error,code}` directly
  (`src/gobby/servers/middleware/auth.py:177-188`). Existing shapes differ;
  this is evidence of policy fragmentation, not proof that every shape is wrong.
- **Shared fix:** define one native domain-error classification and HTTP
  translation policy before copying catch-all patterns. Keep security-safe
  internal errors, validation, missing, forbidden, conflict, capacity and
  unavailable distinct. Preserve intentional existing envelopes through explicit
  adapters; require a decision-backed S1 waiver for a changed wire contract.
  Reuse the runtime family's error facilities where sufficient, rather than
  adding per-family exception frameworks. Cron contributes its D1 vectors.
- **Affected families:** directly evidenced Cron, Projects and shared
  authentication; all native HTTP handlers named in S1 consume the common
  policy. No unrelated error-shape migration is authorized by this report.
- **Plan amendment:** §§1.1/3.1/3.2/3.4/3.5. Route through one shared owner;
  avoid a separate Cron-only convention.

### S4. Credentialed CORS needs an explicit native boundary owner

- **Evidence:** the adversary reported CRON-005 during this pass; independently
  checked with gcode. Python constructs configured exact/glob origin handling
  and an outer CORS middleware with credentials, all methods and all headers
  (`src/gobby/servers/app_factory.py:99-130`). Its explicit purpose is to answer
  preflights without authentication and decorate auth refusals (`:121-122`).
  Native `FrontDoor::handle` has authentication early returns and dispatches
  directly without an equivalent stage
  (`crates/gdaemon/src/front_door/mod.rs:111-247`); a scoped `CorsLayer` sweep
  in gdaemon/gruntime found none. Cron §4.1b cannot preserve that Python outer
  layer after proving operation with Python absent.
- **Call/shared fix:** KEEP the existing allowed/disallowed-origin,
  credential/preflight contract. Assign one native front-door CORS owner,
  ordered outside authentication/refusal responses, using the startup-config
  origin semantics. Coordinate its dependency with #24659's shared verifier;
  add no Cron-only CORS policy or browser route widening. Test preflight and
  denied-auth headers with Python absent. Any deliberate policy tightening
  needs a separately approved, case-specific S1 waiver; none is proposed here.
- **Affected families:** all browser-facing HTTP families in S1, particularly
  auth, Skills, Plans, Projects/files/hub documents, Communications, memory,
  tasks, agents, pipelines, admin and WS chat's HTTP/auth handshakes. Browser
  cookies are not required for ordinary cross-origin API-key requests to need
  CORS either. This report establishes the missing shared owner, not a live
  browser exploit or a proof of every caller's current origin requirements.
- **Plan amendment:** §§1.1/3.1/4.1b and the shared front-door/verifier plan.
  Track once through the adversary's CRON-005 rather than duplicate fix work.

Validation: source inspection and deterministic artifact checks only; no pytest,
cargo build, daemon HTTP/DB access, secret reads, restarts, or process termination.
The proposed failure/interleaving tests have not run. Browser authorization and
database row-level policy were not exhaustively audited; this report makes no
cross-project-access finding. The Plan Writer must resolve D3/D7's concrete
policy choices, disposition all drops, and obtain fresh adversarial review.
