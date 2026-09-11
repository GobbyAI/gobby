<!-- markdownlint-disable MD013 -->

# Why herdr-client-completion 3.2 became an oversized leaf

Task: #21854. Investigated September 11, 2026. Historical plan unchanged.

The author put several independently verifiable behaviors into section 3.2.
Review preserved that boundary, and expansion correctly compiled it into one
leaf, #21348. The missing safeguard was a concrete decomposition check before
manifest derivation. More implementation detail and acceptance coverage did
not make the section atomic.

## Where the size was fixed

1. **Drafting chose the unit of work.** The historical plan's section 3.2 is
   lines 1433–1694: 262 lines, 11 distinct Target paths, 14 acceptance items.
   Its title, “Build the app shell, event loop, and terminal views,” contains
   an encoder, renderer, attachment/fallback state machine, control lifecycle,
   reconnect supervisor, attention action, spawn/terminate action, and resize policy.
2. **The manifest froze that boundary.** The plan-coverage contract requires
   one entry per deliverable. `src/gobby/plans/manifest_parser.py:243`
   (`_validate_manifest_invariants`) rejects missing or multiple entries for
   a section; lines 322 onward derive the expected `covers:` labels from
   every acceptance item in that same section.
3. **Compilation preserved it.**
   `src/gobby/tasks/expansion/_contract.py:52` (`compile_plan_to_spec`),
   especially lines 99–109, calls `_build_contract_entry_work_task` once per
   entry. That helper at line 244 copies the section body, acceptance lines,
   and entry labels into one task. It does not partition them.

The recorded expansion run is `2bf827d4-ee6b-4e70-881d-222d882878f0`.
Its provenance label identifies 5 phase epics and the 16 leaves #21340–#21355.
`get_task(#21348)` confirms the section, title, and scope. No evidence suggests
that expansion combined several smaller planned leaves.

The `covers:` format can describe separate owners after narrative subdivision.
The limiting invariant is one manifest entry per source section. Split 3.2
into new deliverable sections, reassign each obligation to an acceptance ID
under its new section, and derive the manifest again. Emitting several leaves
for unchanged source section 3.2 would violate the current contract.

## What the guidance and gates checked before this change

The pre-expansion repository snapshot
`bccb62e9ede64a906187f0621e8b3ce211677112` contains the same granularity text
as the working tree before this fix:

> **Atomic** — completable in one AI session.

That sentence lived in
`src/gobby/install/shared/skills/plan-draft/references/task-structure.md:267`.
It supplied no decision procedure, acceptance/file/diff ceiling, or state-machine
budget. The verification checklist checked self-containment, dependencies,
Targets, consumers, and production file sizes; it did not check leaf granularity.
Installed `plan`, `plan-draft`, and `plan-review` content was read through
`gobby-skills` during this investigation and had the same gap.

The contract's 850-line decomposition trigger and 1,000-line production ceiling
apply to source files, not task size. The parser/compiler enforce coverage,
categories, and ordering, but measure no per-leaf projected diff, independent
behavior count, or implementation-session budget. Review's proportionality check
asks whether mechanisms are justified. Every mechanism can be justified while
the section still contains several tasks. Nothing in these facts establishes
which instructions the original drafting session actually read.

## Comparison of the 16 original leaves

AC = parsed acceptance items. Targets = distinct primary paths in the section's
Targets inventory, including tests, docs, and fixtures. Lines = inclusive parser
source span, excluding the following phase heading. These definitions avoid
counting backticked names inside scope reasons or phase prose as task work.

Minutes, turns, and calls sum all available `agent_runs` rows with that task ID,
including cancelled or failed attempts. They are elapsed run costs, not active
coding time. N/A means no task-linked run metadata; it does not mean zero work.
Commits are unique commits reachable from the sampled HEAD whose message contains
the literal `[gobby-#N]` tag, not the count of all edits, review commits, or merges.

| Task | Section | AC | Targets | Lines | Runs | Minutes | Turns | Calls | Tagged commits |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| #21340 | 1.1 | 8 | 17 | 139 | 1 | 85.3 | 56 | 387 | 1 |
| #21341 | 1.2 | 2 | 3 | 72 | 1 | 36.9 | 38 | 148 | 1 |
| #21342 | 1.3 | 4 | 9 | 81 | 1 | 102.8 | 85 | 446 | 1 |
| #21343 | 1.4 | 9 | 12 | 136 | 3 | 141.5 | 103 | 460 | 2 |
| #21344 | 1.5 | 3 | 75 | 140 | 1 | 139.3 | 34 | 223 | 2 |
| #21345 | 2.1 | 16 | 11 | 311 | 1 | 402.9 | 136 | 885 | 4 |
| #21346 | 2.2 | 10 | 10 | 218 | 1 | 196.7 | 141 | 809 | 2 |
| #21347 | 3.1 | 10 | 28 | 128 | N/A | N/A | N/A | N/A | 3 |
| #21348 | 3.2 | 14 | 11 | 262 | 5 | 779.2 | 431 | 2,486 | 20 |
| #21349 | 3.3 | 10 | 9 | 119 | 1 | 328.8 | 207 | 1,274 | 1 |
| #21350 | 3.4 | 5 | 3 | 111 | 1 | 82.8 | 59 | 320 | 1 |
| #21351 | 4.1 | 3 | 13 | 106 | N/A | N/A | N/A | N/A | 4 |
| #21352 | 4.2 | 1 | 5 | 39 | N/A | N/A | N/A | N/A | 5 |
| #21353 | 4.3 | 8 | 2 | 62 | 3 | 150.6 | 79 | 455 | 1 |
| #21354 | 4.4 | 2 | 2 | 46 | 1 | 10.9 | 8 | 37 | 1 |
| #21355 | 5.1 | 5 | 1 | 148 | 1 | 8.8 | 6 | 34 | 1 |

Snapshot plan SHA-256:
`6a0e36e29e6b7a71211c455ea2e5b65dca3b2e440e972f436c0bcc2d3a0ebc14`.
Commit-count HEAD: `8921c88c1fc2dad64f6e1375479f1fdebf14d0d2`.
The table describes the retained plan and metadata at investigation time, not
an assertion that all task descriptions remained byte-identical since expansion.

### Interpretation and limits

- #21348 accumulated about 13 elapsed run hours and 20 tagged commits. Three
  runs ended for lack of progress, one was cancelled before any recorded turn,
  and the last succeeded. Task closure ultimately succeeded September 6.
- #21345 finishing in one run did not make it small: that run took 402.9 minutes.
  #21349 bundled several user behaviors and took 328.8 minutes. These are
  adjacent examples of the same weak authoring boundary.
- #21344's 75 Target paths largely enumerate corpus work. It took 139.3 minutes,
  illustrating why a raw file-count hard cap would over-split mechanical work.
- Across the 13 leaves with task-linked runs, descriptive Pearson correlation
  with summed minutes is 0.80 for acceptance count, 0.72 for section lines,
  and 0.04 for Target count. Across all 16 leaves, correlation with tagged
  commits is 0.43, 0.50, and approximately 0.00 respectively. These small,
  selected, mixed-category samples and one dominant outlier cannot calibrate
  a safe numerical ceiling or establish causation.
- Run status is not a stall-rate proxy. #21343's first two runs failed during
  MCP startup; #21344 and #21345 report ending before their step workflow
  completed even though the tasks closed. #21348's successor also reported
  waiting for acceptance review. Metadata does not apportion those delays
  between implementation, coordination, review, and infrastructure.
- Three leaves have no task-linked run rows. No transcripts were opened to
  reconstruct missing costs. Additional epics would not remove these limits.

### Run metadata behind the totals

Prefixes identify the recorded run IDs; use `gobby-agents:get_agent_result`
to resolve one. Full timestamps were used for durations; displayed values are
rounded independently. Result bodies and transcript files are not inputs to
the totals.

| Task | Run prefix | Status | Minutes | Turns | Calls |
| --- | --- | --- | ---: | ---: | ---: |
| #21340 | `18428b55` | success | 85.3 | 56 | 387 |
| #21341 | `4ca4250f` | success | 36.9 | 38 | 148 |
| #21342 | `ea49e8ff` | success | 102.8 | 85 | 446 |
| #21343 | `b4893d24` | error | 18.0 | 22 | 21 |
| #21343 | `f8b246c0` | error | 7.1 | 17 | 9 |
| #21343 | `fd665422` | success | 116.5 | 64 | 430 |
| #21344 | `4c2bb8c8` | error | 139.3 | 34 | 223 |
| #21345 | `a9236705` | error | 402.9 | 136 | 885 |
| #21346 | `40f8462f` | success | 196.7 | 141 | 809 |
| #21348 | `489a8c6e` | error | 236.8 | 92 | 707 |
| #21348 | `b53c3480` | cancelled | 9.5 | 0 | 0 |
| #21348 | `14073e16` | error | 363.6 | 188 | 1,213 |
| #21348 | `122619f5` | error | 100.1 | 104 | 334 |
| #21348 | `491eb651` | success | 69.2 | 47 | 232 |
| #21349 | `cee3b3f3` | success | 328.8 | 207 | 1,274 |
| #21350 | `e63ee913` | success | 82.8 | 59 | 320 |
| #21353 | `cec79302` | success | 120.9 | 58 | 374 |
| #21353 | `400f58bd` | success | 13.2 | 12 | 41 |
| #21353 | `7787a5f0` | success | 16.5 | 9 | 40 |
| #21354 | `91250f5c` | success | 10.9 | 8 | 37 |
| #21355 | `f7fa43b1` | success | 8.8 | 6 | 34 |

To reproduce, parse the plan with `gobby.plans.parser.parse_plan`; count
`acceptance_items`, inclusive `source_span`, and unique primary Target paths.
Use `get_task` for section mapping and `list_agent_runs` for parent sessions
`be496b1a-87ca-48aa-bae0-e6f7f2802228` and
`42bea3b9-3ba5-4c38-b6b2-7429267c6e6b`, or a read-only join of
`agent_runs.task_id = tasks.id` restricted to this project's #21340–#21355.
Select IDs, status, timestamps, turns, and tool-call counters only.
Count tagged commits with
`git log <snapshot-HEAD> --format=%H --fixed-strings --grep='[gobby-#21348]'`,
substituting each task number. No task/storage mutation is needed.

## Decision: atomize before the manifest

Enforce **one independently verifiable outcome per deliverable** in `plan`
and `plan-draft`, then check it in qualitative review. A candidate part that
can implement, test, and commit its specified contract before the remaining
behaviors exist is another deliverable. Shared files imply ordering edges;
they do not erase the seam. Keep the behavior's own tests and consumer updates
with it, and assign its later integration checks to the consuming leaf.

Use **more than 6 acceptance items**, **more than 6 distinct hand-maintained
production Target files**, or **2 or more independently testable state machines
or lifecycle owners** as mandatory inspection triggers. Record a short
**Granularity:** decision for triggered sections, naming candidate seams and
either splitting them or giving a concrete atomicity reason. Inspect every
section for separable outcomes even below the triggers. Six is a conservative
review heuristic, not a statistically fitted safety limit; it prompts inspection
before the 10–16-item cases in this sample without banning necessary edge cases.

The changed authoring flow loads the rule before drafting, and its verification
checklist reruns it after revisions and consumer/acceptance growth. The review
guidance requires a concrete split, contracts, checks, and edges for a blocking
`bad-sequencing` finding. Required behavior remains intact. Base parser success
does not certify atomicity, and optional adversarial review does not remove
the author's obligation. No parser schema or compiler changes are warranted.

For 3.2, a counterexample to the original grouping is:

| Candidate leaf | Original acceptance obligations | Contract/check and ordering |
| --- | --- | --- |
| Encode input | 3.2.6 | Key event to bytes, table-driven protocol cases. |
| Render pane grids | Rendering part of 3.2.1 | Frame plus rectangle to rendered cells; scripted frames. |
| Supervise reconnect episodes | 3.2.7, 3.2.13 | Recovery intent to bounded episode result; paused-clock retries, cancellation, generation rollover. |
| Manage attachment and fallback | 3.2.3 | Pane attachment transitions, tombstones, detach deadlines; consumes completed supervisor contract. |
| Manage control and write outcomes | 3.2.2, 3.2.4, 3.2.12 | Lease/pending-input/write transitions; consumes attachment and encoder contracts. |
| Drive the loop through completed contracts | Remaining 3.2.1, 3.2.8, 3.2.9, 3.2.11 | Scripted loop proves routing, loss fan-out, read-only frames, handshake ordering, and exit latch; depends on preceding components. |
| Dispatch attention responses | 3.2.5 | Prompt answer reaches daemon; stale response displayed; depends on loop. |
| Converge spawn and termination | 3.2.10 | Action/reply/event permutations converge once; depends on loop. |
| Apply resize policy | 3.2.14 | Resize burst to geometry/viewport/native-resize effects; depends on loop. |

This is a retrospective boundary demonstration, not a replacement plan. It
accounts for all 14 original items, including compound rendering/routing checks.
Each candidate still needs the normal section specification and shared-file
ordering in a real draft. The final integration leaf must wire completed
contracts rather than inheriting their unfinished state machines. The historical
plan, task tree, and expansion code remain unchanged.
