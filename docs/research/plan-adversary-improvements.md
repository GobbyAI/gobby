# Plan-Adversary Convergence Improvements

**Research task:** `#18957`
**Started:** 2026-07-26
**Status:** complete point-in-time research log

## Goal

Reduce the number of adversarial review rounds needed for an interactive plan to
converge while preserving review quality. This log starts with plan-adversary
sessions created after the latest daemon restart because older sessions used the
known-bad researcher spawn path.

## Scope Boundary

The current daemon process is PID `57163`.

- Process start: `2026-07-26 14:55:58 CDT`
- UTC cutoff: `2026-07-26T19:55:58Z`
- Daemon initialization logged: `2026-07-26 14:56:04 CDT`
- Restart source: `cli_restart`

The first authoritative project session listing contained three sessions
created on or after the cutoff. Three more plan-adversary sessions were spawned
while this research was running, so the cohort inventory is intentionally
time-stamped rather than treated as a one-time count:

| Session | Created (UTC) | Classification |
| --- | --- | --- |
| `#9639` | `2026-07-26T20:08:58.726735Z` | Unrelated implementation work |
| `#9640` | `2026-07-26T22:33:26.349533Z` | Plan adversary, round 2 |
| `#9641` | `2026-07-26T22:42:38.549908Z` | This research session |
| `#9642` | `2026-07-26T22:45:56.201683Z` | Plan adversary, round 3 |
| `#9643` | `2026-07-26T22:49:30.071101Z` | Plan adversary, round 3 |
| `#9644` | `2026-07-26T22:52:30.828863Z` | Plan adversary, round 2 |

There are four in-scope plan-adversary sessions. Their parent coordinators and
prior review rounds predate the cutoff. Prior-round findings are used to explain
the current repair history, but their researcher execution is excluded from the
post-restart operational baseline.

Durable project memory records one older feedback-lesson-loop plan reaching
approval after 12 adversarial rounds. It confirms that serial convergence is a
real historical problem, while its pre-restart execution is excluded from this
operational comparison.

### Corroborating behavioral corpus

The independently maintained
`docs/plans/adversary-convergence.md` analyzed 25 completed adversary rounds
across 6 plans and 109 findings. Those older runs remain excluded from timing
and researcher-reliability claims because they cross the known-bad spawn era.
They are useful behavioral evidence:

- all 109 findings were classified `blocking`, so severity currently carries no
  ordering or exit information;
- `feedback-lesson-loop.md` converged from 10 findings to 0, while
  `context-mode-borrowings.md` remained `needs_review` at round 22 with a
  six-round floor of 1–3 findings;
- the exact `live-migration-ordinal-uniqueness` check key repeated in consecutive
  rounds because the repair moved one colliding ordinal to another colliding
  ordinal;
- related manifest-fidelity, identity-transform, exhaustive-target, and
  serialization-bound classes recurred across several rounds;
- calls per finding rose from 4.6 to 62 as the successful sequence dried up.

The last point corrects a tempting but false optimization: high calls per
finding is not necessarily waste. Near convergence, it is evidence that the
reviewer searched broadly and found little. Absolute wall time and duplicated
discovery work remain costs; calls per finding is better treated as a dryness
signal alongside concrete failure-trace coverage.

## In-Scope Runs

| Session | Run | Parent | Artifact | Round | Evidence | State at inspection |
| --- | --- | --- | --- | ---: | --- | --- |
| `#9640` | `58cb9f21` | `#9600` | `herdr-terminal-client.md` | 2 | `afb81121` | success, 91 calls / 9 turns |
| `#9642` | `67e91aba` | `#9601` | `split-workflow-definition-storage.md` | 3 | `2220a657` | running, 126 calls / 11 turns |
| `#9643` | `a1cc3881` | `#9595` | `subscription-sdk-integration.md` | 3 | `3aac4c93` | running, 113 calls / 7 turns |
| `#9644` | `b26091e4` | `#9602` | `wiki-codewiki-restructure.md` | 2 | `809a3131` | running, 81 calls / 6 turns |

The inspection counts are snapshots, not terminal metrics except for `#9640`.
The other three remained active at the final cohort query.

### `#9640` detail

| Field | Value |
| --- | --- |
| Session | `#9640` / `b17a58f2-07e3-4f43-9919-d55a09a09a73` |
| Run | `58cb9f21-da65-4cab-93be-db73b92081a4` |
| Agent | `plan-adversary-taskless` |
| Parent | `#9600` / `bba3ce3c-8613-408a-b74d-e7bfa07466bb` |
| Artifact | `.gobby/plans/herdr-terminal-client.md` |
| Display round | 2 |
| Evidence | `afb81121-3da5-4d7f-9254-64f6046efbe0` |
| Plan hash | `1b94991fb37df79282844c35ed690d8747ebd8c44ebbb8dc8f1f1d6e5f038bf2` |
| Started | `2026-07-26T22:33:41.872879Z` |
| State at first inspection | running; 58 tool calls, 5 turns |
| Completed | `2026-07-26T23:08:13.350727Z`; success; 91 calls, 9 turns |
| Verdict | `needs_review`; 25 blocking findings |

The immutable snapshot forced parallel review: 23 deliverables, 113 acceptance
items, 126 target files, and 17 changed sections. The adversary launched the
three required native read-only lanes:

1. `requirements_traceability`
2. `repository_blast_radius`
3. `runtime_invariants`

## Evidence Log

### 2026-07-26 — Scope and restart verification

- `uv run gobby status` reported PID `57163`, uptime consistent with the process
  start, and the preceding shutdown as a CLI restart.
- `ps` gave the exact process start time.
- `daemon.log` recorded startup initialization at `14:56:04 CDT`.
- The full 300-row project session result was paged and parsed by `created_at`;
  titles and activity timestamps were not used as the scope filter.

### 2026-07-26 — Round-2 intake

The round-2 prompt says round 1 produced 19 blocking findings, all accepted and
applied. Round 1 used evidence `cf7c438f-6bfd-4974-b75d-34002d88d2a4`.
The coordinator preserved the canonical checkpoint and revalidated the revised
draft before spawning round 2.

The repair batch changed 17 of 23 deliverable sections, grew acceptance coverage
from 87 to 113 items, and grew the target inventory from 109 to 126 files.
Seventy-four percent of the plan leaves were therefore revised before the next
full review, but the only pre-spawn validation was structural plan validation;
no finding-specific counterexample or overlap verifier ran.

The round-2 adversary reported at `2026-07-26T22:34:18.827Z` that its first
completed lane had already exposed two concrete adjacent-site omissions:

- an omitted `task_recovery.py` reader;
- a missing dispatch constructor target.

The adversary described both as sites left behind by round-1 consumer-sweep
fixes. Classification is pending full evidence review:

- incomplete application of accepted findings;
- fixer-induced defects;
- round-1 reviewer misses;
- or a mix of these classes.

This is the strongest early signal for round inflation. A repair that changes a
contract or consumer set needs a mandatory class-wide causal sweep before the
next adversary spawn. Waiting for the next full three-lane review to discover
adjacent consumers guarantees at least one avoidable round.

The round-2 adversary later completed a causal sweep across all 19 round-1
findings and verified that all three deliberately simplified round-1 repairs
fail their original invariants:

1. **R1-F14 — bounded operation outcome map.** A 32-entry map cannot
   distinguish a retry whose ID was evicted from a genuinely new operation ID.
   The proposed `unknown_operation` behavior is unreachable without separate
   knowledge that the ID previously existed.
2. **R1-F12 — immediate embed teardown.** Teardown on the last detach leaves a
   create-to-attach gap in which a newly created consumer can race the teardown.
3. **R1-F19 / grouped tmux behavior.** Group members share window active-pane
   and geometry state. The adversary reproduced the geometry failure: attaching
   a second 80x19 client changed the owner's 120x39 window to 80x19.

These are repair failures, not evidence that the reviewer needed more breadth.
The round-2 prompt explicitly named the three deviations and asked for concrete
counterexamples. A finding-specific repair proof before respawn would have
caught each one without another full plan review.

### 2026-07-26 — Existing orchestration context

The parent session summary records two older round-1 operational problems:

- completion notifications propagated to the full ancestor lineage, waking the
  interactive root once per researcher grandchild;
- one researcher timed out after 78 tool calls, eliminating a lane result and
  forcing recovery in the parent.

These events predate the restart and belong to the known-bad spawn era, so they
are not counted as post-restart performance evidence. They remain useful
regression checks. The current contract uses provider-native internal subagents
for lane research, which should avoid Gobby descendant-completion broadcasts.

### 2026-07-26 — Evidence-service and lint inspection

Current implementation confirms that the round boundary loses causal repair
information:

- `PlanReviewEvidenceService.snapshot_payload()` returns the current section
  manifest, full snapshot, `changed_section_ids`, and `review_complexity`.
- `_changed_sections_since_prior_round()` compares section hashes and returns
  IDs only.
- The snapshot does not contain prior finding IDs, check keys, accepted
  resolutions, finding-to-section causality, changed symbols, or the sites
  swept while repairing them.

The parent currently reconstructs round history as prompt prose. The evidence
service cannot verify that a claimed repair handled the complete failure class.

The deterministic consumer lint is useful and narrower than the review contract:

- `run_consumer_sweep()` checks indexed direct consumers.
- `_sweep_section()` activates from explicit symbol references in section text
  or destructive file intents.
- It checks direct symbol/file consumers and target-file membership.
- It does not derive changed contracts from the plan revision, follow indirect
  consumers, or prove constructor/reader/registry completeness for changes that
  lack a recognized trigger.

The round-2 `task_recovery.py` reader and dispatch-constructor omissions are
consistent with these blind spots. Draft validation passing proves the current
lint contract passed; it does not prove a class-wide repair sweep passed.

Coverage validation also relies on self-attested completion:

- `_validate_lanes()` requires exactly three completed lanes and requires every
  lane to list every deliverable section.
- `_validate_dispositions()` requires the caller to set
  `cross_lane_interaction_complete: true` and
  `adjacent_variant_complete: true`, then checks every emitted candidate has one
  disposition.
- `validate_review_coverage()` rehashes citations and emits both completion
  fields as `true`.

No structured interaction record or adjacent-variant inventory backs those two
booleans. The gate proves result shape, full section-ID enumeration, candidate
disposition completeness, citation freshness, and manifest parity. It does not
prove the reviewer performed a complete causal or adjacent-site sweep.

### 2026-07-26 — Live round-2 lane timing

The three provider-native lane subagents all returned candidate results. Launch
and return timing:

| Lane launch order | Launched (UTC) | Returned (UTC) | Elapsed | Candidates |
| --- | --- | --- | --- | --- |
| 1 | `22:35:29.487` | `22:47:50.306` | 12m 20.8s | 9 |
| 2 | `22:35:40.190` | `22:51:11.198` | 15m 19.0s | 9 |
| 3 | `22:35:52.169` | `22:53:49.626` | 18m 09.4s | 7 |

The launches were issued serially with 22.7 seconds between the first and
third. All three then ran concurrently. The 25 candidates contain clear
cross-lane duplicates and collapse to approximately 21 issue families before
final verification. Candidate ownership was:

- requirements traceability: 9;
- repository blast radius: 7;
- runtime invariants: 9.

Canonical coverage accepted all three lanes: 25 candidates produced 19 emitted
findings and 6 dismissed or merged dispositions. The 23-entry shadow manifest
passed with attestation digest
`1a423368d766c955b1e84be0f6563ec080a8151229608ef5d61d7a729e24f80c`.
The server-side coverage check took 49.58 milliseconds; the surrounding caller
spent minutes recovering, paging, and echoing the 42 KB manifest.

The terminal result then violated that attestation: it returned 25 blocking
findings, `R2-F01` through `R2-F25`. `R2-F05` and `R2-F21` through `R2-F25`
were added during the parent causal/cross-lane sweep after dispositions had
already been validated. They have no coverage-backed candidate mapping. The
validator persists no canonical result, generic parent messaging can write
arbitrary content into `agent_runs.result`, and `end_agent_run` accepts no
result digest. Coverage, delivery, persistence, and termination are therefore
four unbound operations.

Observed parent-adversary overhead before and during lane execution:

- four sequential skill loads: 13.9 seconds;
- memory-tool discovery followed by a blocked call: approximately 7 seconds;
- the 140 KB snapshot result was offloaded and fetched twice;
- the first code-index freshness call hung, was interrupted, hit a blocked
  `rg` fallback, and then succeeded with `--no-freshness`: 60.25 seconds to the
  useful retry;
- concurrent code-index refresh contention caused several 9–19 second searches;
- the 42 KB derived manifest result was offloaded and derived twice;
- the coverage tool schema exposes generic objects, so the adversary searched
  implementation code to discover the required candidate-disposition shape;
- the first composed coverage call failed on a nested result-envelope
  `TypeError`, requiring payload reconstruction;
- the first parent delivery failed because `from_session` was not inferred as
  the messaging schema promised;
- context compacted after delivery but before shutdown, forcing tool
  rediscovery.

Native lane execution fixed terminal-result loss: all three results arrived.
It did not make the round cheap. The current bottleneck is repeated context
recovery plus three repository-wide searches contending on shared code-index
freshness.

Coverage-to-terminal tail latency was 5m11s. The terminal reviewer session also
reported 93 tool calls while the raw transcript and agent run reported 91,
showing that current per-run telemetry is not yet a stable convergence metric.

### 2026-07-26 — Concurrent cohort saturation

The other three post-restart runs started 12m30s, 16m04s, and 19m04s after
`#9640`. Each crossed the current parallel-review threshold and launched three
native lanes. The live system therefore ran four adversary parents plus 12
repository-wide research lanes without a global admission budget.

The operational symptoms are cohort-wide:

- snapshots for `#9642`, `#9643`, and `#9644` were also offloaded at roughly
  109–125 KB;
- code-index searches reported a refresh already running or waited behind a
  shared refresh;
- `#9643` temporarily lost the control plane while requesting the shadow
  manifest schema;
- `#9644` received a transient daemon-unreachable result during live evidence
  inspection;
- installed native lane definitions expose no per-run deadline, and the
  taskless parent contract treats lane deadlines as optional when the provider
  does not expose one.

This is distinct from the convergence defect. Repair-induced findings explain
why another round is needed; unbounded concurrent fanout explains why each
round's wall-clock time and tool recovery cost increase when several plans are
reviewed together.

The cohort also reinforces the repair-quality pattern:

- `#9642` entered round 3 after 14 accepted round-1 blockers and 16 accepted
  round-2 blockers. Seven round-2 findings were explicitly introduced by the
  round-1 repairs. Its current run found another missing target
  (`src/gobby/agents/spawn.py`) and a transaction/lock contract conflict.
- `#9643` entered round 3 after 12 accepted round-2 blockers; its prompt says the
  second review proved some round-1 fixes were not correct.
- `#9644` entered round 2 after 17 accepted round-1 findings and explicitly asks
  the reviewer to detect defects introduced while applying them.

These prompts independently point at the same serial loop: adversary finds a
defect, coordinator applies a local repair, and the next full adversary
discovers the repair's adjacent consumers or cross-finding interaction.

### 2026-07-26 — This research session's tmux title

The title has not failed at the tmux rename boundary. Both durable session state
and tmux still agree on the provisional title:

- stored title: `#9641 Codex`;
- stored source: `provisional`;
- tmux window name: `#9641 Codex`;
- tmux automatic rename and client rename: both disabled, as expected for a
  Gobby-managed window.

The session did generate a compaction summary at `23:06:57Z`. The user then
interrupted the response twice. Codex correctly recorded two `turn_aborted`
events and started two new tasks, proving that the interruptions ended their
active turns. Gobby still left durable digest fields empty
(`digest_markdown`, `last_turn_markdown`, and `last_digest_input_hash` are null;
`last_digested_pair_index` is zero).

The installed DB rules confirm the boundary:

- `digest-on-response` runs at `turn_end`;
- `digest-on-plan-turn-end` runs only after provider plan-boundary tools such as
  Codex `request_user_input`.

An interrupt is therefore a provider turn boundary that Gobby does not currently
translate into its `turn_end` lifecycle. Context compaction also writes only
`summary_markdown`; it does not run digest-owned title synthesis. The fix should
preserve the aborted outcome while emitting a distinct end boundary, such as
`turn_end(outcome=aborted)`, so digest/title rules can consume the partial
exchange without treating it as a successful agent completion or triggering
completed-turn recovery.

Canonical round-history classification found 15 fixer-induced, incomplete, or
repair-interaction findings and 10 pre-existing round-1 reviewer misses. No
finding came from new user scope. The dominant repair-related families are:

- R1-F02 × R1-F09: locator non-nullability before spawn;
- R1-F14 × R1-F18: spawn retry and reconciliation;
- R1-F11/R1-F12/R1-F13/R1-F19: embed pane, lease, lifecycle, and geometry;
- R1-F06: `task_recovery.py` consumer still omitted;
- R1-F07: `_planning_enhancement.py` consumer still omitted;
- R1-F04: cadence/flip gate weaker than the accepted invariant;
- R1-F15: title/sequence reconciliation remains incomplete.

The 10 reviewer misses included:

- full scrollback versus bounded JSON-line transport;
- a host-protocol/supervision dependency cycle;
- §3.5 lacks the dependency edge into §3.6 for version-pin work;
- omitted service composition roots and Cargo workspace membership;
- a missing control-socket authentication handshake;
- the non-agent `terminal_create` ingress bypassing sole row ownership;
- self-attach detection using the daemon's identity instead of the client
  identity;
- flip dependencies that omit packaging and web parity.

Only the version-pin finding lived wholly in unchanged leaves; the other misses
were original behavior inside sections later touched by repairs. Improving
reviewer breadth alone addresses this smaller class. Repair-time synthesis and
proof address the dominant repeat and interaction class.

## Completed Mitigations Under Evaluation

Three relevant fixes landed before or shortly after the restart:

| Task | Commit | Change |
| --- | --- | --- |
| `#18928` | `bff20be87` | Scope completion wakes to the direct spawner and explicit coordinator instead of the full session lineage |
| `#18934` | `1c7d5e2a9` | Remove the Gobby-managed plan-review researcher and use provider-native internal lane subagents |
| `#18932` | `316ce1955` | Fail open for code search when the exact skill-load MCP call has an unresolved proxy error |

`#9640` is the first in-scope adversary run exercising all three changes
together. It completed successfully after 34m31s, 91 recorded calls, and 9
turns. All three native lanes returned usable results without the retired
Gobby-agent timeout failure. The review still needed a 60.25-second manual
search recovery loop, repeated offloaded-result retrieval, and more than five
minutes from coverage success to terminal completion. The native-lane change
fixed result loss; it did not bound review latency or finish the canonical
result transactionally.

Two infrastructure risks remain outside those completed fixes:

- workflow evaluation has a 15-second `before_tool` budget under concurrent DB
  load; open research task `#18940` owns this investigation;
- the former researcher had a 900-second run deadline for a repository-wide
  sweep.

The native lane path no longer uses the former Gobby researcher deadline, so
increasing that retired timeout would treat obsolete machinery. Measure native
lane completion first. The workflow-evaluation budget remains relevant if
`#9640` shows slow or failed MCP calls.

## Findings

### P0. Coverage does not bind the terminal review result

**Evidence:** `#9640` validated 19 emitted findings and 6 dismissed candidates,
then delivered 25 blockers. Six post-attestation findings bypassed candidate
dispositions, citations, adjacent-site evidence, and source-hash coverage.
Current validation is pure and persists no canonical result; generic agent
messaging writes arbitrary parent-directed content into `agent_runs.result`;
termination accepts no result or digest.

**Likely effect:** coverage can report success while the parent receives a
different finding set. Repairs then lose the strongest lane evidence and the
next round cannot prove which candidate obligations were closed.

**Candidate improvement:** make the service materialize and persist the final
result from validated evidence. Return only `result_id` and digest. A
plan-review-specific atomic finalizer must verify the active run, evidence and
source hashes; write the canonical result; finalize evidence; notify the
parent; and complete the run. Enforce exact finding-ID/count correspondence,
candidate mapping for every finding, no dismissed candidate emission, and
idempotent re-finalization.

### P1. Repairs lack a hard causal-sweep gate

**Evidence:** round 2 immediately found adjacent consumers omitted by round-1
repairs and disproved all three deliberate simplifications of accepted
findings.

**Likely effect:** each accepted finding can introduce or expose another defect
class, producing serial discovery across rounds.

**Candidate improvement:** before preparing the next review snapshot, require
the coordinator to submit a repair attestation per accepted finding:

- changed section IDs;
- the finding's `check_key`;
- accepted resolution and any intentional deviation from the suggested fix;
- changed symbols or contracts;
- repository consumer/constructor sites swept;
- adjacent variants swept;
- validation evidence;
- unresolved or deliberately deferred sites.

The evidence service can then reject a next-round preparation whose causal sweep
is incomplete. This turns the sweep into a repair-time gate instead of another
adversary round.

Any repair that deviates from the suggested fix needs an additional proof:

- restate the violated invariant and original counterexample;
- show how the alternative closes that counterexample;
- test the alternative under the original race, failure, or boundary condition;
- request another user decision when the alternative accepts new risk.

“Least mechanism” is an implementation constraint after correctness is proven.
It cannot justify replacing an accepted fix with a weaker invariant.

### P2. Findings are repaired individually without a class-wide synthesis pass

**Evidence:** 19 round-1 findings were all accepted and folded into one large
revision touching 17 sections. Round 2 still found consumer-set gaps related to
those repairs.

**Likely effect:** local edits satisfy finding text while missing interactions
among findings and shared consumers.

**Candidate improvement:** group accepted findings by `check_key`, affected
contract, and shared target surface before editing. After edits, run one
cross-finding interaction pass across the complete changed-section set. For
stateful cross-stack plans, rebuild the relevant end-to-end invariant bundle
before resubmission: state-transition table, retry/outcome matrix, protocol
request/result/event matrix, ownership/authentication matrix, and
client/backend matrix.

### P3. The adversary prompt optimizes rigor without an explicit convergence
mechanism

**Evidence:** the round-2 prompt sets `max_review_rounds: 99` and emphasizes that
another round is always cheaper than a bad plan. The prompt correctly forbids
manufactured findings, yet it supplies no mechanism that moves likely future
findings into the current repair pass.

**Likely effect:** quality remains high while discovery stays serial.

**Candidate improvement:** retain unlimited review authority and add a
convergence obligation:

- every emitted finding triggers an adjacent-variant candidate sweep in the
  same round;
- every repair triggers the same `check_key` sweep before respawn;
- the next adversary receives explicit repair attestations and tests them first;
- repeated check keys or affected surfaces are classified as convergence
  failures with a named cause.

### P4. Large repair batches need machine-derived blast-radius hints

**Evidence:** the reviewed plan targets 126 files and round-1 repairs changed 17
sections. Manual target lists missed at least two consumers.

**Likely effect:** prompt-only consumer sweeps become brittle as the plan and
repair batch grow.

**Candidate improvement:** derive changed symbols/contracts from the revision,
query the code index for their consumers, and include the resulting candidate
site inventory in the next-round evidence snapshot. The coordinator must
disposition every candidate before spawn.

### P5. Coverage completion is asserted at too low a resolution

**Evidence:** coverage validation checks two caller-provided booleans for the
cross-lane and adjacent-variant passes. It records no pass inputs, checked sites,
check keys, or dispositions produced specifically by those passes.

**Likely effect:** a reviewer can honestly believe it completed the pass while
using a narrower interpretation than the next reviewer. The attestation cannot
distinguish a deep sweep from a shallow one.

**Candidate improvement:** replace the two booleans with structured pass
records:

- `cross_lane_interactions`: participating candidate IDs, affected sections,
  interaction checked, and disposition;
- `adjacent_variant_sweeps`: check key, seed finding/candidate, query or index
  evidence, sites checked, and resulting candidate IDs;
- `causal_repair_sweeps`: prior finding ID, changed sections/contracts, sites
  checked, and disposition.

Coverage validation should derive completion from those records and reject
unreferenced candidates or changed repair surfaces.

### P6. Section hashes are insufficient repair routing

**Evidence:** round preparation knows that 17 sections changed, while the
snapshot gives no semantic reason for each change.

**Likely effect:** every lane re-reviews all 23 deliverables and must rediscover
which changes are causally related to prior findings.

**Candidate improvement:** retain full-plan coverage and add focused routing:
prior finding ID, check key, changed acceptance items, changed target inventory,
and changed symbols/contracts. Lanes start with causal surfaces, then complete
their normal exhaustive pass.

### P7. Parallel lanes contend on shared discovery work

**Evidence:** three launches were staggered by 22.7 seconds; lane durations grew
from 12m21s to 18m09s; concurrent code-index refreshes added repeated 9–19
second waits.

**Likely effect:** parallel review lowers ideal compute time while duplicating
snapshot, target inventory, and index-refresh work. The slowest lane sets round
latency.

**Candidate improvement:** prepare a shared read-only research bundle once:

- current index generation/freshness token;
- parsed plan and deliverable inventory;
- changed-section and prior-finding routing;
- target files and precomputed symbol/consumer seeds;
- immutable snapshot result inline or by stable result reference.

Refresh the code index once before fanout. Require every lane to use the same
generation with freshness checks disabled for its read-only searches.

### P8. Oversized tool results cause duplicate expensive calls

**Evidence:** snapshot and manifest payloads were offloaded; each operation was
repeated instead of consuming a stable result reference.

**Likely effect:** extra DB work, extra context/tool calls, and more opportunity
for source drift.

**Candidate improvement:** return compact metadata plus a durable result ID from
the first call and make downstream tools accept that ID directly. The
adversary should never re-derive an immutable snapshot or manifest solely to
recover an offloaded payload.

The shadow-manifest round trip has an even smaller fix. Coverage validation
currently receives the full caller-supplied shadow object, extracts its routing
decisions, re-derives the expected shadow on the server, and compares both
objects. The caller is therefore paging 42 KB only to echo server-owned data
back to the same service. Accept `routing_decisions` or a durable derivation
token instead; derive the canonical shadow once inside coverage validation.

### P9. Generic schemas force runtime source archaeology

**Evidence:** the coverage validator's generic object schema did not expose the
candidate-disposition record shape; the adversary searched implementation code
after all lanes returned.

**Likely effect:** predictable per-round tool calls and review latency, plus
risk of malformed first attempts.

**Candidate improvement:** publish strict nested schemas for lanes, candidates,
dispositions, cross-lane interactions, adjacent sweeps, and shadow manifest
status. Include a compact example in the tool description.

### P10. Parallel review has no cohort-level admission or deadline control

**Evidence:** four parents simultaneously launched 12 repository-wide lanes.
Shared index refreshes queued, two runs observed transient control-plane
failures, and the native lane path has no enforced elapsed-time deadline.

**Likely effect:** a burst of planning sessions multiplies the slowest-lane
problem and makes infrastructure recovery part of every active review. A stuck
lane can hold its parent indefinitely even after the useful candidate yield has
flattened.

**Candidate improvement:** add one global plan-review admission budget measured
in active lanes, not active parents. Prepare and pin one code-index generation
before granting lane permits. Queue excess lanes or run their required pass
serially in the parent. Enforce:

- a per-lane elapsed watchdog that interrupts and records a fallback reason;
- an outer review deadline that changes execution strategy without weakening
  the required three-lane coverage;
- a compact partial-result checkpoint so a timed-out lane does not lose
  validated candidates already produced.

### P11. Canonical requirements are not part of the immutable snapshot

**Evidence:** `#9642` needed the parent epic's requirements, but its taskless
allowlist blocked read-only `get_task(#18879)` and the adversary prompt did not
include the epic description.

**Likely effect:** a lane can prove plan-to-prompt traceability while silently
missing the user-owned requirement source referenced by that prompt.

**Candidate improvement:** snapshot and hash canonical parent-task and named
requirements documents during evidence preparation. Include their compact
requirement IDs in each lane projection. Expanding the taskless allowlist is a
weaker fallback because it reintroduces source drift during review.

### P12. Compaction context is not isolated to the reviewer

**Evidence:** `#9640` compacted after parent delivery. Its generated session
summary included unrelated commits, plan artifacts, `survey.json`, and this
research document even though the reviewer made no edits.

**Likely effect:** a mid-review compaction can inject false repository ownership
or completion state into the reviewer and distort later dispositions.

**Candidate improvement:** generate reviewer compaction only from that child
session's transcript, immutable evidence IDs, and run-owned tool results.
Exclude ambient workspace summaries and other active-session activity. Add an
isolation test with concurrent editors and overlapping plan artifacts.

### P13. `blocking` has no failure-trace gate or reachable risk fixed point

**Evidence:** the independent 25-round corpus contains 109 findings and every
one is `blocking`. One plan remained unapproved through round 22 with a stable
tail of 1–3 blockers, while the plan that approved actually decayed to zero.

**Likely effect:** the coordinator must investigate every under-specification at
equal priority, and approval requires an unbounded generator to produce exactly
zero critiques. That exit condition can remain unreachable even after material
implementation risk has converged.

**Candidate improvement:** require every `blocking` finding to carry a concrete
failure trace: preconditions, action, wrong outcome, and cited violated
obligation. Findings without such a trace are `major` or `minor` by schema, not
reviewer discretion. Define approval as no remaining concrete failure trace,
while continuing to report non-blocking quality improvements.

### P14. Reviewer remedies can manufacture the next round's surface

**Evidence:** the companion analysis found 8 of 17 suggested remedies in one
wiki-plan round larger than the minimal correct repair. The repaired artifact
grew to 28 sections and the next snapshot observed 38.

**Likely effect:** accepting an expansive remedy adds deliverables, targets, and
acceptance items that create new review surface. Round count can grow even while
the original defect set shrinks.

**Candidate improvement:** split `defect` from `minimal_repair`. The repair must
name the smallest edit to existing sections that closes the failure trace. A new
deliverable requires explicit proof that existing host sections cannot own the
obligation. Record artifact-growth and remedy-scope telemetry per round.

### P15. Dismissed candidates are forgotten between rounds

**Evidence:** coverage validates a dismissal disposition, then the next reviewer
receives no mechanical dismissal ledger.

**Likely effect:** later rounds can spend calls re-deriving and re-dismissing the
same candidate, or oscillate when the prior rationale is unavailable.

**Candidate improvement:** persist dismissed candidate identity, check key,
source hash, and rationale. Carry it into the next snapshot as a
do-not-reopen record unless named source or plan evidence changed.

## Candidate Changes, Ranked

1. **Finalize only a server-materialized validated result.** This is the
   evidence-integrity prerequisite: bind candidate dispositions, final
   findings, persistence, parent delivery, and run completion in one operation.
2. **Gate respawn on repair attestations and causal sweeps.** Highest expected
   reduction in serial defect discovery. Require counterexample proof for every
   deviation from an accepted suggested fix.
3. **Replace sweep-complete booleans with structured sweep records.** Make
   interactions and adjacent variants auditable and mechanically enforceable.
4. **Gate `blocking` on a concrete failure trace and approve on residual risk.**
   Give severity information content and the loop a reachable fixed point.
5. **Generate a changed-contract consumer inventory from the code index.**
   Cover semantic users, constructors, protocol implementors, composition
   roots, exhaustive handlers, fixtures, fakes, and tests.
6. **Synthesize accepted findings before editing.** Build an overlap graph by
   shared contract, check key, target surface, and resource; require a recorded
   interaction disposition for every edge.
7. **Constrain suggested remedies to the minimal repair.** Require host-section
   justification before adding deliverables, and track artifact growth.
8. **Carry dismissed candidates across rounds.** Reopen them only when named
   source or plan evidence changes.
9. **Add a global lane admission budget and elapsed watchdogs.** Prevent four
   planning sessions from turning 12 independent sweeps into shared index and
   control-plane saturation.
10. **Snapshot canonical requirements with the plan.** Hash parent-task and
    named requirement sources so taskless lanes cannot lose user-owned scope.
11. **Use focused later-round lanes without dropping full-plan coverage.** Keep
    one full-plan lane for old-scope misses; route the other two first through
    changed sections, repair obligations, and semantic adjacency.
12. **Precompute one shared research bundle and pin one code-index generation.**
    Start all lanes from the same snapshot and use `--no-freshness` for
    immutable searches.
13. **Make offloaded evidence results first-class inputs.** Pass snapshot,
    manifest, lane output, and final result by stable handle rather than model
    echo.
14. **Isolate reviewer compaction context.** Build summaries only from the child
    transcript and run-owned immutable evidence.
15. **Publish strict coverage/disposition schemas.** Remove per-round source
    archaeology and malformed-call recovery.
16. **Track convergence telemetry by finding class.** Persist repeated check
    keys, reviewer misses, fixer-induced defects, artifact growth, remedies,
    calls per finding, lane duration, and wall time.
17. **Keep native lane research and assert no ancestor wake amplification.**
    Treat any root wake per lane completion or loss of a terminal lane result as
    a regression.

## Metrics Needed

Per plan and round:

- wall-clock duration;
- parent turns spent waiting or processing descendant notifications;
- adversary turns and tool calls;
- per-lane duration, tool calls, fallback reason, and candidate count;
- findings emitted and dismissed by `check_key`;
- severity distribution and concrete failure-trace completeness;
- changed section count between rounds;
- artifact growth and remedy scope;
- calls per finding as a dryness signal, not an efficiency target;
- dismissed candidates reopened without source change;
- repeated findings by check key and affected surface;
- reviewer-miss and fixer-induced-defect counts;
- findings attributable to incomplete accepted-finding repairs;
- rounds to approval.

The useful convergence target is fewer avoidable rounds, measured by repeated
or repair-induced finding classes. Raw round count alone would encourage weaker
review.

## Point-in-Time Limits and Next Measurements

`#9642`, `#9643`, and `#9644` were still running at the final query. Their
partial evidence is sufficient to establish cohort saturation and repeated
repair defects, but their terminal finding counts and wall times are not
reported here as completed metrics.

The next implementation experiment should compare two otherwise similar
revisions:

1. current repair-and-respawn behavior;
2. structured repair obligations, overlap synthesis, deterministic consumer
   inventory, and focused verifier proof before respawn.

Measure avoidable findings in the next full round, especially
fixer-induced/repeated findings. Separately load-test the same reviews under a
global lane budget to measure orchestration latency without conflating it with
plan convergence.
