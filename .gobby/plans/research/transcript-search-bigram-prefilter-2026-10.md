# Transcript search bigram prefilter: measured and retired (#23267)

## Question

#23117 landed a per-call render budget (2000 groups) and a resume cursor for
`search_session_messages`. The prior #23117 branch also carried a bigram-signature
candidate prefilter: a764731691 (lookup) and e6f56851fc (incremental signing in the
index appender, sidecar schema v2). Does that prefilter, on top of the landed
bound, cut enough work to justify the sidecar schema change?

## Verdict

**Not material. Retire the prefilter.** It removes rendering for miss queries but
makes every individual call slower, and it adds a large signing cost to every
index build.

The bar was fixed before measuring: a miss query's full-pagination wall time must
drop at least 5x, **and** no query's per-call latency may regress by more than
10%. The prefilter passes the first condition and fails the second by 2.2x to 40x.

## Method

Harness: `tests/sessions/bench_transcript_search_prefilter.py`, run on 2026-10-03
from 0.5.0 at b25b1b4c05, under LM heavy key `l3-23267-bench-v1`. Load (1/5/15 min)
was 9.00/9.20/10.49 at start and 8.83/10.50/10.82 at end.

- **Fixture.** Synthetic, 200,000 rendered groups (100,000 turns), 743 MB, the
  #23117 mix shape: a prompt, then assistant text plus tool_use, then a
  tool_result. One distinct needle sits at turn 75,000. It is generated in
  temporary state; no daemon, database or real transcript is touched.
- **Arms.** Each runs in a fresh interpreter with its own temporary GOBBY_HOME.
  - *baseline* calls the public `search_session_messages` boundary with limit 20
    and follows `next_cursor` to exhaustion.
  - *prefilter* signs every group with the prior branch's `GroupSigner`. Each call
    then re-runs `candidate_group_indices`, as the prior design did, and renders
    only candidate runs from one resolved snapshot, under the same budget and
    limit.
- **Assumption.** The budget counts rendered groups only; the signature scan is
  outside it, as in the prior design.
- **Provenance.** The 200k numbers come from the harness's draft revision. The
  committed file keeps the same measurement logic. It changes only lint fixes,
  how the match digest is computed, and how the prior module is loaded (now a
  `--prior-module` argument).
- **Match identity.** The draft compared snippets only. The committed harness
  identifies each match by the canonical JSON of its full rendered message (id
  included). It compares count and sha256 per query across arms, and exits 1 on
  any mismatch. Its 3k-group run (2026-10-03) printed `match sets identical for
  all 5 queries`.

## Results

| Query | Baseline first call | Baseline full pagination | Prefilter call (= full pagination) | Candidates | Scan per call |
| --- | --- | --- | --- | --- | --- |
| miss, distinct grams | 0.317 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 27.99 s | 0.687 s, 0 groups, 1 resolution | 0 | 0.521 s |
| miss, common grams (`please check item 2000000`) | 0.233 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 27.33 s | 3.765 s, 185 groups, 1 resolution | 185 | 4.914 s |
| rare hit, common grams (`please check item 99999`) | 0.282 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 40.20 s | 6.186 s, 1296 groups, 1 resolution | 1296 | 4.629 s |
| rare hit, distinct needle | 0.187 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 25.81 s | 0.380 s, 1 group, 1 resolution | 1 | 0.392 s |
| common hit (`lorem ipsum`) | 0.072 s, 200 groups, 1 resolution | first page only | 2.913 s, 20 groups, 1 resolution | 100,000 | 2.634 s |

Every prefilter query finished in a single call, so its full pagination is that
one call: one resolution and the groups shown. The draft emitted only
first-call resolutions. It recorded no full-pagination resolution total for the
baseline; the committed-harness rerun below records it (one per call: 100 for
each miss and rare hit, 5000 for the common hit). At 200k, the snippet sequences
were identical across both arms for all five queries. The full-message identity
check above reproduces that equality at 3k, which supports the superset
guarantee on this fixture.

Index-side cost:

- Boundary index cold build: 41.0 s (baseline) and 27.5 s (prefilter); these are
  the same code, so the gap is run-to-run noise.
- Signing 200k groups: **166.6 s CPU (203.5 s wall)**, on top of the index build.
  In the prior design this work happens in the index appender.
- Signature bitmap: 7,985,566 bytes, 95% of the 8 MiB `MAX_SIGNATURE_BITS` cap.
  Groups signed past the cap are admitted unconditionally, so a longer or more
  varied history than this lorem-ipsum fixture would lose filtering.

### Committed-harness 200k rerun (baseline arm only)

On 2026-10-03 the committed harness (85be31e0eb) was rerun at 200k on the same
fixture with `--shards 6`, under LM heavy key `l3-bench-v2`, from 05:03:49 CDT.
The baseline arm finished uncontended and reproduces the draft numbers:

| Query | First call | Full pagination | Matches |
| --- | --- | --- | --- |
| miss, distinct grams | 0.312 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 100 resolutions, 29.21 s | 0 |
| miss, common grams | 0.303 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 100 resolutions, 27.03 s | 0 |
| rare hit, common grams | 0.252 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 100 resolutions, 24.75 s | 1 |
| rare hit, distinct needle | 0.272 s, 2000 groups, 1 resolution | 100 calls, 200k groups, 100 resolutions, 25.15 s | 1 |
| common hit | 0.034 s, 200 groups, 1 resolution | 5000 calls, 999,600 groups, 5000 resolutions, 140.38 s | 100,000 |

The index cold build took 29.2 s. The common-term query now paginates to
exhaustion (503d724862) and yields all 100,000 matches; its sha256 is
`0f0fc811…ed24`.

The six prefilter shards never reported. Each loads the 743 MB fixture
and signs all 200k groups in its own interpreter; sharding splits only the
measured calls (call index modulo shard count). On a memory-starved host (about
140 MB free, 5-minute load 20-27) each shard showed 15-22% of a CPU in
instantaneous samples, but averaged only about 2m45s CPU over 2h41m (under 2%). The Orchestrator stopped the run gracefully at about
09:00 CDT so that queued work could proceed. Prefilter timings from any sharded run
are contended by design and are not evidence; the verdict above rests on the
single-process draft measurements. Full-message identity across both arms is
shown at 3k groups; at 200k only the snippet sequences were compared.

**Common-hit full pagination with the prefilter: lower bound, not executed.**
The PD waived this arm for a retire disposition on 2026-10-03 at 11:19 CDT
(the amendment is recorded on criterion 1). Running it single-process would take
about 3.7 CPU-hours of the serial heavy slot. Every prefilter call re-runs
the signature scan, measured at 2.634 s for this query. The baseline needs 5000
calls to exhaust it, so the prefilter arm would take at least 5000 × 2.634 s,
about 13,200 s. That is about 94x the 140.38 s baseline before any rendering.
This is arithmetic from measured inputs; the arm was not executed.

## Why it fails

- **The scan is a linear Python loop.** `candidate_group_indices` checks the query
  grams against all 200k signatures on every call. With common query grams it
  takes 2.6-4.9 s, which is 10-60x the cost of a budgeted render call (0.07-0.32 s).
  Queries with a rare gram exit early (0.4-0.5 s), but even that exceeds a whole
  baseline call.
- **The fixture favours the filter.** Lorem-ipsum text has very few distinct
  bigrams. Real transcripts have far more, which means wider signatures, an earlier
  cap and more candidates.
- **The landed bound already caps per-call work.** Full pagination of a miss
  (about 28 s across 100 calls) is the only case the prefilter improves, and an
  agent rarely paginates a miss to exhaustion.

A filter that wins per call would need sublinear candidate lookup, such as an
inverted gram index, not a per-group signature scan. That would be a new design,
not a rebase of this branch.

## Disposition

#23267 closes under criterion 3. Commits a764731691, e6f56851fc and 0c28dd3d39 are
unlinked from it. The retained worktree
`~/.gobby/worktrees/gobby/fix-23117-transcript-search-bound` (branch
`fix/23117-transcript-search-bound`) can be retired by its owner. Retiring it is
outside this task: no cleanup is authorized here.
