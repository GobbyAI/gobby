# Why the plan adversary under-finds and over-runs

**Status:** ideas log, written during the `wiki-codewiki-restructure` review loop
**Task:** `#18959`
**Evidence base:** round 1 of `.gobby/plans/wiki-codewiki-restructure.md`, first-hand
**Companion:** `docs/research/plan-adversary-improvements.md` (task `#18957`) studies the
orchestration side — repair attestation, sweep records, lane telemetry — against the
`herdr-terminal-client` plan. This document covers the other half: **finding quality,
severity calibration, remedy scope, and why the loop has no fixed point.** Where the two
overlap it is noted inline; nothing here restates that log.

## Measured baseline

Round 1, run `2b388243-dfd2-4fc5-91d3-9b0c6099a256` (counters from its `agent_runs` row;
finding counts from the persisted round-1 checkpoint fence in the plan changelog):

| Metric | Value |
| --- | --- |
| Model / effort | `gpt-5.6-sol` / `xhigh` |
| Wall clock | 2045 s (34 m 5 s) |
| Turns | 7 |
| Tool calls | 131 |
| Candidates | 23 |
| Findings emitted | 17 |
| Dismissed | 6 |
| Severity distribution | 17 `blocking`, 0 anything else |
| Findings accepted | 17 / 17 |
| Remedies accepted **as written** | 9 / 17 |
| Remedies accepted **trimmed** | 8 / 17 |
| Citation accuracy | high — every load-bearing citation ground-checked, all held |

Two numbers carry most of the diagnosis: **severity variance is zero**, and **47% of
proposed remedies were larger than the minimal correct fix.**

Citation quality was not a problem. The reviewer correctly established that
`src/gobby/wiki/scheduled_jobs.py` is exactly 995 lines against a 1,000-line cap, that
`dispatch_batch` (`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py:592`) refuses
suggestions without a task ref and therefore cannot carry taskless queue rows, and that
`crates/gwiki/src/ingest/session/summarize.rs:39,74,77` uses `AiRouting::Direct` while
sitting outside every declared P1 target. Whatever is wrong is upstream of verification.

## Why it does not find more per round

### 1. The budget goes to lookups, not reasoning

131 tool calls for 17 findings is ~7.7 calls per finding, and the great majority were
file reads re-deriving facts that are cheap, deterministic, and already known to the
daemon: does this path exist, how many lines is it, what symbols does it export, what
enum variants does it have. The reviewer rebuilds a model of the repository from zero
every round.

Note what the three strongest round-1 findings have in common: each is *"the plan's claim
disagrees with the repository."* That class is mechanically discoverable. Making it cheap
converts it from lucky sampling into systematic coverage, and frees the reviewer's
context for the cross-section reasoning that nothing can precompute.

**Change:** `prepare_plan_review_round` emits a **review facts pack** alongside the
snapshot — for every path in every Target inventory: exists / line count / top-level
symbol names / last-touched commit; plus the plan's declared dependency DAG with the
cycle and unknown-dependency verdict already computed. Inject it as evidence. Highest
expected lift on findings-per-round, deterministic, no protocol change.

### 2. One generalist time-slices many lenses and finds the shallow instance of each

Round 1's findings cluster into eight recognizable defect classes: durability/rollback
(3), concurrency and fencing (2), schema/migration (2), dependency ordering (3),
API-contract-vs-reality (2), size and resource limits (1), measurability (2), scope
boundary (2). Roughly two per class, across eight classes, in 34 minutes. That is the
signature of uniform shallow sampling — the reviewer reaches each lens, finds the most
visible instance, and moves on before finding the second.

The taskless contract already fans out three native research lanes
(`requirements_traceability`, `repository_blast_radius`, `runtime_invariants`). Those are
*research* lanes, not *defect-class* lanes. Scoping lanes by defect class, over a shared
facts pack, should surface the second and third instance per class inside one round.
Costs N× tokens per round and should buy fewer rounds.

### 3. Dismissed candidates die at the round boundary

Six of 23 candidates were dismissed with reasoning the next round never sees, so nothing
stops round 2 from re-deriving them and spending its budget arriving at the same
dismissal. I hand-wrote a do-not-reopen list into the round-2 prompt; that should be
mechanical, not artisanal. Persist `dismissed_candidates` with reasons in the round
record and inject them into the next round's prompt. (Adjacent to, and cheaper than, the
structured sweep records proposed as P5 in the companion log.)

## Why it does not converge

### 4. Severity is a constant, so it carries no information

All 17 findings arrived `blocking`. The coordinator therefore has to ground-check all 17
at equal cost, and the reviewer has no pressure to distinguish "this plan will build the
wrong thing" from "this plan is under-specified here." Uniform severity is what a rubric
produces when severity has no forcing function.

**Change:** `blocking` requires a concrete failure trace — preconditions, the action, the
wrong outcome — in the same shape a bug report needs to be reproducible. A finding that
cannot produce one is `major` or `minor` **by construction**, not by the reviewer's
judgment. This is the same discipline that makes a code-review finding real, and it is
mechanically checkable at the schema layer.

### 5. Completeness findings are infinitely generable

"Section X has no acceptance item for Y" can be emitted against any plan of any quality,
including a perfect one, because acceptance coverage has no natural upper bound. A loop
whose exit condition is "no findings remain" therefore has **no fixed point**. It
terminates only when the reviewer decides to stop, which is exactly the decision the
rubric tells it not to make.

**Change:** define `approved` on risk instead of completeness — *no remaining finding
carries a concrete failure trace.* Put it in the review contract, not in each round's
prompt, so it survives prompt drift. Say explicitly that completeness findings without a
failure trace are non-blocking.

### 6. Accepted remedies inflate the artifact, which manufactures next round's surface

This is the structural engine, and it is the one that matters most.

The reviewer emits both a defect and a remedy. Defect-finding is bounded by the repo;
remedy *design* is unbounded, and round 1 shows where it goes: proposed fixes included an
absence-test matrix, four new reinstall sections, a snapshot-and-restore drill,
restructuring agents to return staged content, and absorbing every knowledge cron into
the run queue. Eight of seventeen had to be cut down to the minimal correct fix. Two of
those trims were strictly *better* than the proposal, not merely cheaper — the
transactional row-lock chosen over agent restructuring also removed agent vault write
access entirely.

The feedback loop: an over-scoped remedy, once accepted, enlarges the plan; a larger plan
has more sections, more targets, and more acceptance items; more surface yields more
findings next round. Round 1 took the plan to 28 sections, and round 2's snapshot hashes
38. The review loop feeds itself.

**Change:** split the finding schema into `defect` and `minimal_repair`, and constrain
the repair: *the smallest edit to existing sections that removes the failure trace.*
Propose a new deliverable only when no existing section can host it, and name the
sections rejected as hosts and why. This turns "did the reviewer over-scope" from a
judgment call by the coordinator into an artifact the reviewer must defend.

## Ranked changes

Each item names the machinery it extends.

1. **Review facts pack in the round snapshot.** Extends
   `PlanReviewEvidenceService.snapshot_payload()` (`src/gobby/plans/review_evidence.py:185`),
   which already parses the plan document and emits the section manifest — the Target
   inventories it needs are in that parsed document. Deterministic, no contract change.
   Biggest lift for the least mechanism.
2. **Failure-trace gate on `blocking`.** Extends the `round_result` finding schema and
   `validate_review_coverage()` in `gobby-plans`. Restores triage signal and kills
   completeness padding at the source.
3. **`defect` / `minimal_repair` split with host-section justification.** Same finding
   schema. Stops artifact inflation — the actual engine of non-convergence.
4. **Risk-anchored `approved` in the review contract.** Belongs in
   `docs/contracts/plan-coverage.md` and the plan-adversary agent definition, not in
   per-round prompt prose. Gives the loop a fixed point.
5. **Changed-section depth routing for round N>1.** Extends
   `_changed_sections_since_prior_round()`, which already computes the hashes and knows
   which sections changed; the prompt still asks for a full-plan review, so unchanged
   sections get re-derived. Present them as reviewed-and-accepted context and require
   depth on changed ∪ new ∪ dependents. (Same conclusion as P6 in the companion log,
   reached from the finding-rate side.)
6. **Mechanical dismissal-ledger carry-forward.** Persist the `dismissed_candidates`
   already produced by `_validate_dispositions()` into the round record and inject them
   into the next round's prompt.
7. **Defect-class lens panel over a shared facts pack.** Re-scopes the existing
   three-lane native fanout in the taskless review contract.

Items 1, 5, 6 are plumbing on machinery that already exists. Items 2, 3, 4 are schema and
contract changes to `gobby-plans`. Item 7 is the only one that costs materially more per
round.

## How to tell it worked

Round count alone is the wrong target — it can be driven to 1 by weakening review. Track
instead:

- findings per round **carrying a failure trace** (should rise, then fall to zero);
- share of remedies accepted **as written** (should rise from 53%);
- artifact growth per round in sections and bytes (should fall toward zero);
- repeat rate by `check_key` across rounds (should be ~0 once the dismissal ledger
  carries forward);
- tool calls per finding (should fall sharply once the facts pack lands);
- rounds to the first zero-failure-trace round.

## What not to change

Citation grounding held perfectly in round 1 and is the reason 17/17 findings were
accepted. Keep the reviewer citing `file:line`, keep the coordinator ground-checking
before accepting, and keep the byte-exact checkpoint fence. The problem is not that the
reviewer is wrong. The problem is that it is asked for completeness against a target that
grows as it is satisfied.
