# Herdr Terminal Recovery and Work Ordering Decision

Date: 2026-08-25  
Task: #20999

## Purpose

This document records the discussion about near-term work ordering, the state of the
Herdr terminal implementation, the recovery options considered, and the approach chosen.
It is a decision record, not an executable Gobby plan. The executable plan will be a
revised `.gobby/plans/herdr-terminal-client.md`.

## Starting priorities

The initial proposed order was:

1. Finish and land the Grok epic.
2. Integrate the Herdr worktree with current `0.5.0` and land it.
3. Complete AGY.
4. Complete #17435, the remote-session and project-identity epic.

The expected limit for the evening was Grok plus Herdr integration setup. AGY and #17435
were acknowledged as likely later work.

The authoritative active-task query was:

```bash
uv run gobby tasks list --active --limit 1000
```

It returned 177 active tasks. The relevant clusters were:

- **Grok — #20960, priority 1.** Five implementation leaves remain. #20966 and #20967
  are ready; #20968–#20970 follow through dependency edges. This is bounded enough to
  land first.
- **Herdr — #20255.** The epic and its children are closed in the task store, while its
  managed worktree remains active and unmerged. The work landed only on
  `wt-task-20255-m4`.
- **AGY — #18653, priority 2.** This remains a full provider campaign. #20783 is the
  open Gate 0 addendum, followed by the already-expanded P2–P7 implementation tree.
- **Remote stack — #17435 under #17488, priority 4.** This is an umbrella containing
  #19651 project-checkout identity, #19652 transcript-archive research, and later work.
  It is a campaign rather than a single short task.

## Why Grok comes first

The Herdr implementation was performed by Grok. Its QA exposed that Grok hooks and
context delivery were not behaving as assumed. The resulting Grok epic changes hook
capabilities, deferred session materialization, first-activity startup injection, pending
context delivery, and the isolated-daemon smoke contract.

Those changes affect some of the same session, hook, and agent-spawn surfaces involved
in Herdr integration. Landing Grok first gives the Herdr re-grounding a stable and
correct target rather than forcing another refresh immediately afterward.

## Current Herdr state

Relevant refs at the time of inspection:

- `0.5.0`: `0627e49166`
- Herdr branch: `wt-task-20255-m4` at `518cec5c41`
- Merge base: `b89f371a15bd73c8470669390a6dde80bd77add1`

Since the merge base:

- Herdr has 25 unique commits.
- `0.5.0` has 437 unique commits.
- Herdr changes 1,781 files, with approximately 495,465 insertions and 3,379 deletions;
  much of the insertion count is vendored terminal code.
- Approximately 94 real paths changed on both sides.
- A current `git merge-tree --write-tree --name-only --messages 0.5.0
  wt-task-20255-m4` probe reports 30 conflicted paths.

The conflicts are concentrated in meaningful integration surfaces: schema identity,
agent lifecycle and reconciliation, resume and spawn execution, application composition,
WebSocket/tmux handling, validation evidence, and their tests. The bulk vendored terminal
tree is not the main source of merge risk.

The old worktree also contains local dirt:

- modified `.gobby/project.json` with parent-project fields;
- untracked `.gobby-native-bin/`;
- untracked `web/node_modules`.

The old worktree and branch are disposable after useful work has been salvaged. Keeping
them for audit history is not a requirement.

## Architecture direction

`docs/architecture/evolution.md` still places the native terminal stack at Stage 0 and
makes it the prerequisite for the later Rust daemon and hub/node stages. Its core
direction remains current:

- own the Herdr-derived terminal core;
- keep `gterm` as a durable process separate from the daemon;
- make `gclient` the independent interactive client;
- preserve backend-neutral terminal and frame protocols;
- keep tmux as the shipped default until native parity is proven honestly.

The destination has changed little. The implementation and task topology have drifted
substantially.

## QA-plan assessment

`.gobby/plans/herdr-terminal-client-qa-fixes.md` was written to bring the closed Herdr
branch back into conformance with the original plan. Its intended flow was sound:

1. create an epic worktree from current `0.5.0`;
2. absorb `wt-task-20255-m4` in the first leaf;
3. repair the runtime and protocol findings;
4. build the missing Herdr-style gclient UI;
5. prove component and whole-screen parity;
6. run meaningful live end-to-end validation;
7. land only after the accumulated guard set passes.

The QA plan contains valuable requirements, especially:

- importing and carving the actual Herdr UI chrome;
- a real crossterm/ratatui client loop and daemon data plane;
- component render goldens derived from Herdr's `TestBackend` tests;
- whole-screen parity captures with an explicit divergence checklist;
- real gclient/gterm end-to-end execution;
- an honest tmux default and a separately evidence-gated native flip;
- removal of tautological or placebo acceptance tests.

Its execution snapshot is stale:

- it describes 69 intervening `0.5.0` commits and 12 merge conflicts;
- current measurements are 437 commits and 30 conflicts;
- standard plan validation passes with consumer-coverage warnings;
- expansion validation fails on stale consumer inventories in sections 1.1 and 4.1;
- the latest recorded adversarial result is round 15, `needs_review`;
- it has 33 deliverables and roughly 170 acceptance items tied to the old topology.

## Options considered

### 1. Merge current `0.5.0` into the old Herdr worktree and land it

This was the initial shorthand idea. The measured divergence made a direct landing too
risky. The old branch contains useful terminal work, an incomplete client, and stale
integration assumptions. A whole-branch merge would require resolving 30 textual
conflicts plus semantic drift across the overlapping paths.

### 2. Keep the QA plan, rebaseline it, and continue adversarial review

This remained viable. The necessary preparation would be:

- wait for Grok to land;
- refresh the merge base, conflict inventory, migration inventory, and Targets;
- repair current consumer-coverage failures;
- rerun standard and expansion validation;
- continue review at round 16 until approval;
- build from a fresh post-Grok `0.5.0` worktree.

This path preserves the QA plan's detailed work. It also continues carrying a large
delta plan whose organization was derived from a much older repository snapshot.

### 3. Discard the QA plan's execution topology and re-ground the original plan

The chosen approach uses the original Herdr plan as the behavioral source of truth,
incorporates the durable QA findings, and derives a new task graph from current code.
The old branch becomes a task-aligned source of salvageable commits instead of the base
that every correction must work around.

This preserves the expensive implementation while allowing the coordinator to accept,
adapt, or replace each prior task's output independently.

## Decision

Use the following sequence:

1. **Finish and land Grok #20960.** Re-run Herdr divergence checks against the resulting
   `0.5.0` tip.
2. **Reopen #20255.** Reuse the original epic as the active workstream. Existing closed
   leaf records remain historical task state; the new expansion is derived from the
   revised plan.
3. **Revise `.gobby/plans/herdr-terminal-client.md` against current reality.** Keep the
   architectural destination and rewrite Targets, dependencies, validation, and task
   boundaries for post-Grok `0.5.0`.
4. **Carry forward the QA plan's valuable requirements.** Preserve the UI, parity,
   protocol-safety, honest-default, and real-E2E obligations. Archive or supersede the
   QA plan after those requirements are traceably represented in the original plan.
5. **Add a salvage matrix for commits associated with tasks #20263–#20285.** Classify
   each prior commit as:
   - `reuse`: cleanly satisfies the revised acceptance contract;
   - `adapt`: useful implementation needing current-main repairs;
   - `replace`: inadequate implementation, including missing client/UI work or invalid
     evidence.
6. **Run fresh plan validation and multiple adversarial rounds.** Apply the existing
   restraint policy to every finding disposition so review improves coverage without
   rebuilding fixer-induced machinery indefinitely.
7. **Apply a new M1 manifest and reset/re-expand the original epic through the audited
   expansion path.** Generate a fresh task tree rather than manually rewriting closed
   leaf records.
8. **Build in a fresh isolation worktree from post-Grok `0.5.0`.** The coordinator
   cherry-picks clean predecessor commits into the owning new leaves, validates each
   against the revised criteria, adapts partial work, and implements replacements from
   the current base.
9. **Land through the managed worktree lifecycle.** After the revised epic is green and
   landed on `0.5.0`, mark and delete the old `wt-task-20255-m4` worktree and branch
   through Gobby's worktree tools.

## Expected effort and later ordering

The direct whole-branch merge was assessed around 7/10 difficulty. Mechanical conflict
resolution and compile triage alone would consume roughly one focused day. Full Herdr
correction, client construction, and validation remain a multi-day campaign.

A realistic near-term checkpoint is Grok landed plus the original Herdr plan re-grounded
and moving through review. The fresh build follows after approval.

After Herdr:

1. **AGY #18653.** Start with Gate 0 addendum #20783, then work through P2–P7. Herdr
   should land first because AGY overlaps terminal spawning, provider runtime, and web
   chat surfaces.
2. **#17435.** Treat it as a campaign. Execute #19651 project-checkout identity and
   #19652 transcript-archive research according to their dependencies; later
   multi-machine work remains gated by the broader #17488 architecture sequence and
   physical-machine availability.

## Settled outcome

The original Herdr plan will be modernized, reviewed again, and expanded fresh. The QA
plan supplies requirements and findings. The old worktree supplies cherry-pickable code.
The coordinator builds from current `0.5.0`, salvages clean work by leaf, replaces weak
work, lands the completed epic, and removes the old worktree.
