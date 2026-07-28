# Why the plan adversary under-finds and over-runs

**Status:** ideas log
**Tasks:** `#18959` (v1, single-round), `#18960` (v2, corpus rebuild)
**Companion:** `docs/research/plan-adversary-improvements.md` (task `#18957`) studies the
orchestration side — repair attestation, sweep records, lane telemetry — on the
`herdr-terminal-client` plan. This document is the measurement side. The two were written
independently and converge on the same primary cause; that agreement is noted where it
happens rather than restated.

## Corpus

Every completed `plan-adversary-taskless` run in the hub whose result blob contains a
parseable `round_result`. Reproduce with:

```sql
select id, tool_calls_count, turns_used,
       extract(epoch from (completed_at-started_at))::int as secs,
       started_at, prompt, result
from agent_runs
where agent_name ilike '%adversar%' and status='success'
order by started_at;
```

then extract the first JSON object in `result` containing a `findings` key, and group by
the plan filename in `prompt`. Finalized rounds also carry `round_result` directly in
`plan_review_evidence`, but that table retains only recent rows (10 at time of writing),
so `agent_runs.result` is the wider source.

**25 rounds parsed, 6 plans, 109 findings.** Two of the six have complete or near-complete
round sequences; the rest are single observed rounds.

| Plan | Rounds observed | Findings per round | Terminal verdict |
| --- | --- | --- | --- |
| `context-mode-borrowings.md` | 11 (rounds 11–22) | 2, 4, 3, 3, 3, 3, 1, 2, 1, 2, 2 | still `needs_review` at round 22 |
| `feedback-lesson-loop.md` | 10 (round 3 →) | 10, 7, 8, 6, 4, 2, 1, 1, 2, 0 | **`approved`** |
| `wiki-codewiki-restructure.md` | 1 (round 1) | 17 | `needs_review` |
| `subscription-sdk-integration.md` | 1 (round 2) | 12 | `needs_review` |
| `dream-stale-memory-reconciliation.md` | 1 (round 1) | 10 | `needs_review` |
| `task-12898-memory-recall-helper.md` | 1 (round 4) | 3 | `needs_review` |

Run cost across the corpus: 17–139 tool calls, 3–23 turns, 283–3389 s. The 2026-07-26
runs are 2–3× longer than the 2026-07-22/23 runs, consistent with the three-lane native
fanout landing in between.

**The single hardest number: 109 of 109 findings are `severity: blocking`.** Across six
plans, four weeks, and every round in the corpus, the adversary has never once emitted a
finding at any other severity.

## What the corpus retracts

The first version of this document was written from `wiki-codewiki-restructure` round 1
alone and argued that 131 tool calls for 17 findings (7.7 calls/finding) showed a
budget wasted on lookups. **The corpus contradicts that.** Calls-per-finding across the
25 rounds ranges from 4.3 to 62, and 7.7 sits near the *efficient* end. Worse for the
original claim, the ratio moves the wrong way for it: on the one plan that reached
`approved`, calls/finding climbed 4.6 → 8.7 → 13.7 → 15.5 → 62 as it converged. High
calls-per-finding is what a nearly-clean plan looks like, not what waste looks like.

That claim is withdrawn. Two things survive it, and they are better:

- **Calls-per-finding is a dryness metric.** It rises monotonically on the converging
  sequence. It is the closest thing in the existing telemetry to a "this plan is running
  out of real defects" signal, and nothing currently reads it.
- **The absolute call budget is the real ceiling.** A round gets 17–139 tool calls
  regardless of whether the plan has 20 target files or 126. Cheapening repo lookups buys
  *surface coverage per round*, which is a coverage argument, not an efficiency one.

## Why rounds repeat

### 1. Repairs fix the named instance, and the next round finds the next instance

This is the dominant driver in the only long sequence available, and it is visible in the
finding keys themselves. From `context-mode-borrowings`:

- Round 14: `live-migration-ordinal-uniqueness` — "planned migration version 338 collides
  with an existing live migration."
- Round 15: `live-migration-ordinal-uniqueness` — "the tool-results migration collides
  with live migration 339."

The same check key, twice, because the repair moved 338 to 339 and 339 was also taken.
The defect was never "338 is wrong"; it was "the plan picks migration ordinals without
consulting the live set." One instance got fixed.

The same shape recurs by class rather than by key:

- manifest-criteria fidelity: rounds 12 (`...criteria do not reflect their covered
  section contracts`), 13 (`manifest-validation-criteria-completeness`), 15
  (`manifest-criteria-save-retention`), 16 (`manifest-validation-criteria-fidelity`) —
  four rounds, four different M1 entries, one defect class.
- identity-bound transforms: round 14 (`identity-bound-normalization-closure`, off-by-one
  against the normalizer cap) → round 16 (`identity-bound-transform-consistency`, 146-char
  writer form vs 130-char canonicalizer).
- exhaustive target sweeps: round 16 (`constructor-sweep-target-and-line-cap`) → round 21
  (`exhaustive-struct-literal-target-sweep`).
- serialization/size bounds: rounds 11, 12 (twice), 19.

Round 22, the last one recorded, emitted `live-source-citation-accuracy` **twice in the
same round** — two instances of one class, which is the same failure surfacing inside a
single round instead of across two.

Roughly half the 27 findings in that tail belong to a class an earlier round already
raised. The exact-key repeat and the intra-round duplicate are hard evidence; the class
grouping above is my reading of the finding text, not a mechanical match, because nothing
in the schema makes classes comparable.

This is the same conclusion the companion log reached from the herdr plan (its P1 and P2),
by a completely different route: it saw round 2 immediately find consumers that round 1's
repairs left behind. Two independent plans, two independent analyses, one cause.

**The obligation already exists; only the evidence is missing.**
`_validate_dispositions()` (`src/gobby/plans/review_coverage.py:382`) already refuses a
round whose payload lacks `adjacent_variant_complete`, with the rejection message
"class-wide adjacent-variant sweep must be complete." It is a caller-supplied boolean with
no backing record — the reviewer asserts the sweep and the gate believes it.

Two scoping notes, because the timeline matters. That gate landed in `25b399922` at
2026-07-23T22:58Z, *after* the entire `context-mode-borrowings` sequence (15:57Z–22:18Z the
same day), so the 22-round tail is not evidence against the boolean. The wiki plan's round
1 did run under it and attested `adjacent_variant_complete: true` — and then spread 17
findings across eight defect classes at roughly two apiece, which is what an incomplete
class sweep produces. One honest attestation, still shallow per class.

The lane split on that same attestation is worth recording: `requirements_traceability` 10
candidates, `repository_blast_radius` 6, `runtime_invariants` 7 — 23 total, 17 emitted, 6
dismissed.

### 2. Severity carries no information

109/109 `blocking`. The coordinator must therefore ground-check every finding at equal
cost and has no basis for ordering work; the reviewer has no pressure to separate "this
plan will build the wrong thing" from "this section is under-specified." A field with one
observed value is not a field.

Note what this does to the tail. At round 20 the reviewer emitted one finding and called
it blocking. At round 22, two. If those were honestly `minor`, the plan approved eight
rounds earlier.

**Change:** `blocking` requires a concrete failure trace — preconditions, action, wrong
outcome. A finding that cannot produce one is `major` or `minor` **by construction**, not
by the reviewer's judgment, and is checkable at the schema layer in
`validate_review_coverage()`.

### 3. The exit condition has no fixed point

`context-mode-borrowings` ran **22 rounds** and never approved. Its findings-per-round
flattened at 1–3 by round 17 and stayed there — six consecutive rounds producing 1, 2, 1,
2, 2 findings, all blocking, none of which stopped the loop.

A floor, rather than a decay to zero, is what an unbounded generator looks like.
"Section X's validation criteria omit obligation Y" can be emitted against any plan of any
quality, so an exit condition of "no findings remain" is not reachable by construction.
The one plan that did approve got there because its findings genuinely ran out
(10 → 0 with calls/finding rising 13×), not because the rule let it stop.

**Change:** define `approved` on risk — *no remaining finding carries a concrete failure
trace* — in `docs/contracts/plan-coverage.md` and the agent definition, so it survives
prompt drift. Completeness findings without a failure trace are explicitly non-blocking.

### 4. Accepted remedies inflate the artifact, which manufactures next round's surface

The reviewer emits both a defect and a remedy. Defect-finding is bounded by the
repository; remedy *design* is not. On `wiki-codewiki-restructure` round 1, 8 of 17
proposed remedies were larger than the minimal correct fix — an absence-test matrix, four
new reinstall sections, a snapshot-and-restore drill, restructuring agents to return
staged content, absorbing every knowledge cron into the run queue. Two of the trims were
strictly better than the proposal rather than merely cheaper; the transactional row-lock
chosen over agent restructuring also removed agent vault write access entirely.

Accepted over-scoped remedies enlarge the plan; a larger plan has more sections, targets,
and acceptance items; more surface yields more findings. That round took the plan to 28
sections and the next round's snapshot hashed 38.

The corpus supports the mechanism but cannot size it — remedy scope is not recorded
anywhere, so 8/17 is a single-round observation and stays one. Recording it is the point
of the metric list below.

**Change:** split the finding schema into `defect` and `minimal_repair`, constraining the
repair to the smallest edit to existing sections that removes the failure trace; a new
deliverable requires naming the sections rejected as hosts and why.

### 5. Dismissed candidates die at the round boundary

`_validate_dispositions()` requires every emitted candidate to carry a disposition, so
dismissal reasoning exists — and then the round ends and the next reviewer never sees it.
Nothing prevents re-deriving a dismissed candidate and spending the budget arriving at the
same dismissal. I hand-wrote a do-not-reopen list into the wiki plan's round-2 prompt;
that should be mechanical.

### 6. One generalist samples many defect classes shallowly

`wiki-codewiki-restructure` round 1's 17 findings spread across eight classes at roughly
two apiece: durability/rollback (3), concurrency and fencing (2), schema/migration (2),
dependency ordering (3), API-contract-vs-reality (2), size and resource limits (1),
measurability (2), scope boundary (2). Uniform thin coverage is the signature of a
reviewer that reaches each lens and moves on before finding the second instance — which
is the same behavior that produces cause 1, seen from inside a single round rather than
across rounds.

The taskless contract already fans out three lanes (`requirements_traceability`,
`repository_blast_radius`, `runtime_invariants`). Those are *research* lanes, not
*defect-class* lanes.

## Ranked changes

Ordered by what the corpus shows drives repeat rounds. Each names the machinery it
extends.

1. **Back `adjacent_variant_complete` with a record instead of a boolean.** Directly
   targets cause 1, the only cause with an exact-key repeat in evidence, and adds no new
   obligation — `_validate_dispositions()` already requires the class-wide sweep, it just
   cannot tell a deep one from a shallow one. Require per-sweep entries (check key, seed
   candidate, query or index evidence, sites checked, resulting candidate IDs) and derive
   the boolean from them. Extend the same gate to accepted-finding *repairs* so
   `prepare_plan_review_round` can refuse a round whose prior repairs were not swept. The
   `live-migration-ordinal-uniqueness` pair is the acceptance test: a sweep that asks
   "which other ordinals does this plan pick?" catches round 15 during round 14's repair.
   Same conclusion as the companion log's P1/P2/P5, reached from the round sequence
   instead of the evidence schema.
2. **Failure-trace gate on `blocking`.** Extends the `round_result` finding schema and
   `validate_review_coverage()`. Turns a single-valued field into a real one and is the
   precondition for change 3.
3. **Risk-anchored `approved` in the review contract.** `docs/contracts/plan-coverage.md`
   plus the agent definition. Gives the loop a reachable exit; without it, changes 1 and 2
   make rounds cheaper without making them end.
4. **`defect` / `minimal_repair` split with host-section justification.** Same finding
   schema. Stops the artifact from growing under repair.
5. **Read calls-per-finding as a dryness signal.** Already recorded in
   `agent_runs.tool_calls_count`; nothing consumes it. A round with few findings and a
   high ratio is a converged plan; few findings at a low ratio means the reviewer stopped
   early. Cheapest item here — one query.
6. **Mechanical dismissal-ledger carry-forward.** Persist the dispositions
   `_validate_dispositions()` already validates, and inject them into the next round's
   prompt.
7. **Review facts pack in the round snapshot.** Extends
   `PlanReviewEvidenceService.snapshot_payload()` (`src/gobby/plans/review_evidence.py:185`),
   which already parses the plan document and emits the section manifest. Justified as
   surface coverage per fixed call budget, **not** as waste reduction — see the retraction
   above.
8. **Changed-section depth routing for round N>1.** Extends
   `_changed_sections_since_prior_round()`, which already computes the hashes. Companion
   log P6.
9. **Defect-class lens panel over a shared facts pack.** Re-scopes the existing three-lane
   fanout. The only item that costs materially more per round, and the only one whose
   payoff is speculative on this corpus.

Items 1–3 are where the measured problem lives. Items 5, 6, 8 are plumbing on machinery
that already exists.

## Metrics

Round count alone is the wrong target — it goes to 1 by weakening review. The corpus
gives a baseline for better ones:

| Metric | Baseline | Target |
| --- | --- | --- |
| Findings at `blocking` | 109 / 109 (100%) | a real distribution |
| Rounds to `approved` | 12+ observed; 22 without approving | single digits |
| Findings-per-round floor in the tail | 1–3, flat over 6 rounds | decays to 0 |
| Exact `check_key` repeats across rounds | ≥1 confirmed | 0 |
| Same-class findings in consecutive rounds | ~half the observed tail | 0 |
| Calls-per-finding at approval | 62 (n=1) | rises monotonically |
| Remedies accepted as written | 9 / 17 (n=1 round) | recorded at all, then rising |
| Artifact growth per round | not recorded | recorded, then → 0 |

Two of these are unrecorded today. Remedy scope and artifact growth are the ones that
would size cause 4, which is currently the least-evidenced cause in this document.

## What not to change

Citation grounding held on every round inspected, and it is why 17/17 findings were
accepted on the wiki plan. Keep the reviewer citing `file:line`, keep the coordinator
ground-checking before accepting, keep the byte-exact checkpoint fence. The reviewer is
not wrong. It is asked for completeness against a target that grows as it is satisfied,
and it repairs defects one instance at a time.
