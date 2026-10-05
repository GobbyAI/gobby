# Close-Review Retirement And Reviewer Landing (2026-10)

Task: #23513 "Investigate retiring task-close-reviewer: mechanical close gates
plus code-reviewer review" (Lane 7). Related: #23341 "Remove gobby build".
Research only. This file changes no product code.

Josh's question (Telegram, 2026-10-05, via the Assistant gobby#15070), verbatim:
"Why are tasks taking 4-6h, that seems excessive. What's the bottleneck? I think
I want to explore retiring task-close-reviewer along with Gobby build (we should
already have a task for the latter), or repurpose needs-review and
review-approved and just do the mechanical close gates until the code-reviewer
agent can review/send back/fix. Add the investigation to the planning lane."

## Answer In Brief

- Tasks take 4-6 hours from creation to close because of waiting:
  - #23441 took 5h57m and #23467 took 5h24m.
  - From claim to close they took 3h49m and 2h33m, and #23402 took 4h07m.
- The largest measured sink is the single Merge Manager landing queue. Its
  median is 101 minutes from landing-task creation to landed on 0.5.0. The merge
  itself takes 1-5 minutes once the Merge Manager claims the landing task.
- Activation holds come next: batched restarts and parking, 35-131 minutes.
- Close reviews take 6-10 minutes each and run twice per leaf, once on the
  source task and once on the "chore: land reviewed" landing task. That is under
  10% of the time from LAND to close.
- Retiring task-close-reviewer alone saves about 12-20 minutes per leaf.
  Retiring landing tasks and the central queue saves about 100 minutes.
- Recommendation:
  - Close option C, a receipt-gated mechanical close, now and independent of
    #23341.
  - Josh's landing decision (M3, the reviewer lands through `land_commit`, with
    a freeze flag and approval on exceptions) as specified in Section 3.

## 1. Measured Per-Stage Timings

All times are CT (UTC-5) on 2026-10-05 unless dated.

Sources by cell type:
- **[G]** git: `git log 0.5.0` commit times, re-run by this session. These are
  the source commit times and, for landing commits, the time each landed on
  0.5.0.
- **[T]** `gobby-tasks:get_task`: `created_at` and `closed_at`. This session
  re-checked #23402, #23441 and #23467.
- **[O]** the Orchestrator gobby#14972's seed measurement,
  `/private/tmp/claude-501/-Users-josh-Projects-gobby/c0c13516-4e18-4535-bb59-81f3637ba498/scratchpad/leaf-stage-timings.md`.
  It is built from get_task (brief=false), get_task_sessions (claim and close
  times) and inter-session messages (reviewer, LM, MM and Orchestrator inboxes).
  Claim, LAND, landing-claim and review-start times come from it.
- No tool exposes close-review history. A review's start is the earliest
  message saying it was running, so close-review durations are upper bounds.

### Requested pairs (source / landing task)

| Event | #23134 / #23481 | #23466 / #23480 | #23477 / #23483 | #23430 / #23485 |
|---|---|---|---|---|
| Source created [O] | 09-30 05:40 | 04:26 | 07:35 | 10-04 22:16 |
| Claimed, current claim [O] | 04:38 (first claim 09-30 13:25; 6 claimant sessions) | 04:39 | 08:26 | 08:47 |
| Final candidate commit [G] | 07:51 `6b66f49f6f` | 07:58 `c1e614b0bd` | 08:43 `dd5a13369d`; rev 10:04 `faa10b98cf` | 09:05 `e6597bd726`; replacement 09:15 `e5bbcb7592` |
| LAND [O] | 08:13 | 08:03 | 08:47; re-LAND 10:06 | replacement 09:18 |
| Landing task created [O] | 08:20 | 08:04 | 08:48 | 09:18 |
| Landing claimed by MM [O] | 10:02 | unknown | unknown | unknown |
| Landed on 0.5.0 [G] | 10:07 `a97893c1fe` | 09:51 `36fec32860` | 10:40 `94cb8fc8a1` | 10:57 `edca6fda59` |
| Landing close review [O] | start <=10:26, closed 10:34 | none ran (preview blocked, escalated 10:02) | not yet | not yet |
| Source close [O] | held for Restart B (10:42) | held for Restart B | held for Restart B | open |

### Closed leaves (source / landing task)

| Event | #23402 / #23476 | #23441 / #23478 | #23467 / #23479 |
|---|---|---|---|
| Source created [T] | 10-04 08:45 | 02:26 | 04:36 |
| Claimed [O] | 03:41 | 04:34 | 07:27 |
| Candidate commit [G] | 04:23 `908f8912b9`; fix 05:13 `2ecfc7ebd4` | 07:39 `c5dd0ca7ae` | 07:43 `f5b38bb57f` |
| LAND [O] | 05:19 (after one bounce) | 07:46 | 07:48 |
| Parked [O] | 05:21-07:25 | none | none |
| Landing task created [O] | 07:32 | 07:47 | 07:49 |
| Landing claimed by MM [O] | 07:35 | 08:03 | 09:22 |
| Landed on 0.5.0 [G] | 07:36 `7abafc30c8`; correction 08:24 `9e79ef1647` | 08:05 `1370a60e9b` | 09:24 `a5d51a9726` |
| Activation [O] | none | none | Cutover A 09:40-09:46 |
| Source close review start [O] | <=07:38 | <=08:14 | <=09:54 |
| Source closed [T] | 07:48 | 08:23 | 10:00 |
| Landing closed [O] | 08:42 (review 1 INVALID 08:00) | 08:15 | 09:36 |
| Created to closed | about 23h | 5h57m | 5h24m |
| Claimed to closed | 4h07m | 3h49m | 2h33m |

### Stage durations (minutes, from the rows above)

| Stage | Samples | Median |
|---|---|---|
| Claim to first commit | 15, 17, 18, 42, 185, 194, 200 | 42 |
| Commit to candidate sent | 1-3 | about 2 |
| Candidate to LAND, per round | 1, 3, 3, 4, 5, <=7, 18 | 4 |
| LAND to landing task created | 0.4-1.1, and 7.6 for #23134 | 0.9 |
| Landing created to landed | 4, 17, 95, 99, 107, 107, 112 | 99 |
| Actual merge after MM claim | 1-5 | about 2 |
| Landed to LM release (landed-tree validation) | #23481 18 (842 s pytest), #23479 5.4 | - |
| LM release to review running | <=0.4, <=0.8, <=6.8, <=7.7 | - |
| Close review run (upper bounds) | 6.3, 5.9, 8.3, 9.0, 9.7 | 8.3 |
| Landed to source close when activation-gated | #23467 36; Restart B holds for #23466, #23134 and #23477 of 51, 35 and 2 at 10:42 | - |
| LAND to source closed (closed leaves) | 36, 132, 149 | 132 |

The landing-created-to-landed column uses the seed's six values plus #23430,
which landed at 10:57 [G]. That gives 99 minutes from 09:18, so the median is 99
here; the seed's median of 101 predates #23430's landing.

### Where #23467's 153 minutes went (claim 07:27 to close 10:00)

| Stage | Minutes |
|---|---|
| Develop | 16 |
| Candidate and LAND | 5 |
| Waiting in the Merge Manager queue (07:49-09:22) | 93 |
| Merge | 2 |
| Landing-task close review | about 12 |
| Cutover A and source close review | about 25, overlapping the activation |

### Bounces and rework

- #23134: at least three bounce rounds, with six claimant sessions since 09-30.
- #23402: one bounce, then an INVALID landing-task review that needed a
  correction landing.
- #23477: re-LANDed 78 minutes later, after the Merge Manager found a failing
  foreign test.
- #23430: one replacement LAND, folding in a LOW finding.
- #23466, #23441, #23467: none.

### Top time sinks

1. **The single Merge Manager landing queue.** Four of seven landings waited
   95-112 minutes for 1-5 minutes of merge. One landing task at a time is
   claimed under the main reservation. #23481's claim hit
   `TASK_CLAIM_CONFLICT` while #23480 sat claimed. Each landing also runs a
   14-minute landed-tree pytest.
2. **Activation and parking holds:**
   - #23402 was parked for 124 minutes.
   - #23466 and #23134 landed at 09:51 and 10:07 but waited for Restart B at
     10:42.
   - The backlog cap of 5 held idle lanes.
3. **Rework loops:**
   - #23134's six claimants.
   - #23402's bounce plus its INVALID landing review.
   - #23477's re-LAND after a foreign failure.
4. **Close reviews.** At 6-10 minutes, two per leaf, they are under 10% of LAND
   to close. Every leaf also creates a second "chore: land reviewed" task, which
   gets its own close review. Retiring landing tasks halves the review count
   before any change to the reviewer.

## 2. Close Options

### What close checks today

`close_task` runs deterministic gates 1-12 and then gate 13 (`close_review`).
The gates are:
1. `task_exists`
2. `session_context`
3. `repository_path`
4. `children_closed`
5. `criteria_present`
6. `changes_summary_present`
7. `linked_commits`
8. `task_scope`
9. `uncommitted_task_edits`
10. `validation_commands`
11. `acceptance_artifacts`
12. `tdd_evidence`
13. `close_review`

Gate 13 runs for every real close of a task with work. It is skipped only for a
named no-work disposition. Evidence:
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:768-800`
(excerpt_hash `f4b34a82758319e627779ac8a1d4dcd742ad2ff3feb02f50a6d8c913c7cd5c09`):

```
768|     blocker = evaluation.checklist.first_failure
...
775|     if (
776|         reason in NO_WORK_CLOSE_REASONS
...
788|     if not run_close_review:
...
794|         evaluation.error = "close_review_required"
```

Gate 13 launches the task-close-reviewer agent
(`src/gobby/tasks/agentic_close_review.py`,
`src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml`). It checks
three things:
- **Criteria text:** it judges each stored validation criterion against the
  linked-commit net patch and the deterministic gate facts. It holds to the
  stored text and ignores waivers given in messages (memory 012dc49b, #23420).
- **Findings:** it reports structured findings at or above
  `close_review_min_severity` (`src/gobby/config/tasks.py:186`).
- **Receipts:** it weighs daemon-attested close receipts. These are
  `independent_review_approval` (a LAND of an exact commit) and `activation`.
  The prompt treats a LAND as evidence and still requires the reviewer's own
  code review.

Receipt kinds: `src/gobby/tasks/close_receipts.py:42-46`
(excerpt_hash `a6f47390593a2fc2511a6efa975171badcfb426ab270a60d2ecf5615e1f3c378`):

```
42| INDEPENDENT_REVIEW_APPROVAL = "independent_review_approval"
43| ACTIVATION = "activation"
44| CLOSE_RECEIPT_KINDS = frozenset({INDEPENDENT_REVIEW_APPROVAL, ACTIVATION})
46| _APPROVAL_VERDICT = "LAND"
```

`record_close_receipt` (same file, 68-158) does four things:
- authorizes against the locked task row;
- refuses the claimant and task-close reviewers as authors;
- accepts `activation` only from the task's creator or delegator;
- requires a full 40-character SHA that names a commit in the task repository.

The lane code reviewer's LAND is an interactive seat's verdict on the candidate
SHA, sent by message and, where used, recorded as an
`independent_review_approval` receipt. It covers code quality. Today it does
not systematically cover the stored criteria text, Live criteria or activation
evidence.

Closed-but-unlanded (memory 283e9a81): the close gate checks the linked commit
and never checks that it reached 0.5.0. The audit found 31 tasks closed VALID
whose commits never landed. The Lane Manager release line (close only after the
LM confirms landing) is the manual guard.

### Option A: retire task-close-reviewer with gobby build

**Change.** Delete gate 13 and the close-review runtime when #23341 removes
gobby build. Close becomes gates 1-12.

**Lost:**
- criterion-by-criterion judgment of the stored criteria text;
- judgment of Live and activation evidence;
- an independent findings pass over the net patch.

Nothing replaces them unless the lane reviewer's LAND is redefined, which
option A does not do.

**Surfaces:**
- `src/gobby/tasks/agentic_close_review.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_review_gate.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_orchestration.py` (review
  queue, finalization, stale-verdict handling)
- the gate 13 branch in `_lifecycle_close.py`
- `src/gobby/config/tasks.py`: `close_review_min_severity` and the
  validation-candidates failover list (memory 31a73e88)
- `task-close-reviewer.yaml`
- `references/tasks/closing.md`
- the tests for each of these

There is no stage-registry change: close review is not a stage.

**Closed-but-unlanded:** not fixed. A closes faster, so it strands faster
unless the LM line stays.

**Timing:** relief waits for #23341, a plan not yet drafted.

### Option B: repurpose needs-review and review-approved

**Change.** The lane reviewer's LAND becomes a stage transition
(`approve_review`, giving `review_approved`), and close runs gates 1-12 only.

**Blockers:**
- The bundled rule `block-needs-review-interactive` blocks `submit_for_review`,
  `approve_review` and `reject_review` for every non-spawned session. Lane
  reviewers are interactive seats. Evidence:
  `src/gobby/install/shared/workflows/rules/task-enforcement/block-needs-review-interactive.yaml:4-18`
  (excerpt_hash `a16262a0e9e4e23edde0fbce03b6f8e4e5323dd1be5729ed4472e9bd16fb7e37`):

  ```
  4| block-needs-review-interactive:
  ...
  10|       not variables.get('is_spawned_agent')
  ...
  14|           - "gobby-tasks-ops:submit_for_review"
  15|           - "gobby-tasks-ops:approve_review"
  16|           - "gobby-tasks-ops:reject_review"
  ```
- The development stage's reviewer selection (`qa-reviewer` / `doc-reviewer`)
  lives in `src/gobby/install/shared/registry/stages.yaml`, which #23341
  removes along with stage-manifest dispatch.

B therefore builds on machinery that #23341 deletes.

**Lost:** the same as A, unless the reviewer role adds criteria judgment.

**Surfaces:** everything A changes, plus:
- the interactive-block rule;
- `stages.yaml` and the crate seed rows that mirror it (#23341 notes `crates/gcore` `seed.manifest.json` and `baseline.sql`);
- the stage-state close gate wiring.

**Closed-but-unlanded:** fixed only if B also adds an ancestor-of-0.5.0 gate.

### Option C: receipt-gated mechanical close (the lighter variant)

**Change.** Gate 13 becomes deterministic. It passes when an
`independent_review_approval` receipt from a session other than the claimant
names the linked commit, or a commit with the same `git patch-id --stable`.
Two gates are added:
- **Landed:** the linked commit is an ancestor of 0.5.0. This is the gate
  #23513's Landing section names.
- **Activated:** when `land_commit` classified the landing as restart- or
  cutover-class (Section 3), an `activation` receipt naming a commit that
  contains the landing.

The agentic reviewer, its queue and its configuration are retired.

**Moves to the lane reviewer's LAND:**
- **Criteria judgment.** The reviewer checks the stored criteria text and
  records the result in the receipt's bounded `facts`, for example
  `criteria_checked: "1,2,3"` and `criteria_gaps: ""`. Receipts allow at most
  16 facts; keys are capped at 64 characters and values at 300.
- **Findings.** These are already the reviewer's job; LAND means no blocking
  findings.

**Moves to coordinators:**
- `Live:` criteria and activation evidence go to the activation receipt, which
  the creator or delegator records today.

**Kept mechanically:** gates 1-12 unchanged. Validation runs, acceptance
artifacts, TDD evidence, scope and clean tree stay deterministic.

**Lost:** an independent judgment made at close time over the final transcript.
A reviewer LAND precedes the close, so evidence produced after the LAND is
judged only mechanically (gates 10-12). For code leaves the LAND SHA is the
landed content (fast-forward), so nothing reviewable changes after it.

**Surfaces:**
- `_lifecycle_close.py`: gate 13 becomes a receipt match, plus the two new gates.
- `close_receipts.py`: patch-id matching, plus the new `landing_approval` kind
  for Section 3.
- Retire `agentic_close_review.py`, `_lifecycle_review_gate.py`, the review
  queue and finalization in `_lifecycle_close_orchestration.py`,
  `close_review_min_severity` and the validator-candidates config.
- Retire `task-close-reviewer.yaml`.
- The lane reviewer's agent definition and role file gain the criteria facts
  and the `land_commit` duty.
- `references/tasks/closing.md` changes.
- No stage-registry change.

**Closed-but-unlanded:** fixed mechanically by the ancestor gate. The LM
landing-confirmation release and the close-order rule's landing clauses
(memory 283e9a81) retire. The activation clause becomes the activation-receipt
gate.

**Tasks without a lane code reviewer** (planning and docs work, for example
Lane 7 plans) need an independent author for the receipt. For plans this is
the Adversary, which already verifies commits (as on #23498). Naming the author
per task category is a plan decision.

## 3. Landing: M3 With Freeze Flag And Approval On Exceptions

### Rulings of record (verbatim)

- Josh: "I don't like developers doing merges"
- Josh: "Lane Reviewer could do it after getting orchestrator approval."
- Josh (Telegram button, 2026-10-05): "landing: lighter variant, approval only
  for restart/freeze/overlap, freeze flag"

Developers may rebase their own candidate in their lane worktree before review.
Only the fast-forward onto 0.5.0 counts as a merge, and the lane reviewer
performs it.

### Today

The main-checkout reservation is a convention: a Merge Manager broadcast plus
the role doc. Git's `index.lock` is the only hard guard. Plan writers commit
directly on 0.5.0 in the main checkout.

### `land_commit(task, sha)`: one daemon tool, the only writer of 0.5.0 in the main checkout

1. **Serialize.** Take a DB advisory lock keyed on the project and the branch
   `0.5.0`. One landing proceeds at a time; each holds the lock for seconds.
2. **Verify the review.** An `independent_review_approval` receipt on `task`
   must name `sha`, or a commit with the same `git patch-id --stable`. Its
   author must be a session other than the claimant.
3. **Check the freeze.** If the landing freeze is on, require a
   `landing_approval` receipt for this `sha` whose reason includes `freeze`.
4. **Classify activation** (see below). Restart- and cutover-class landings
   require a `landing_approval` receipt with reason `restart`.
5. **Check overlap** (see below). An overlap requires a `landing_approval`
   receipt with reason `overlap`.
6. **Fast-forward.** Run `git merge --ff-only <sha>` in the main checkout. If
   the tip moved and fast-forward is impossible, refuse with `tip_moved` and
   return the moved-tip paths (rebase ownership below).
7. **Record.** Link `sha` to `task`, store the activation class on the landing
   record, and send the Orchestrator one notification per landing. Approval
   requests go straight from the reviewer to the Orchestrator; the Inbox
   Manager gets a copy only.

A bundled rule blocks every other raw git write that moves 0.5.0 in the main
checkout: commit, merge, cherry-pick, revert, reset and push from there. Plan
writers' commits go through the same tool. Their receipt author is the
Adversary, per Section 2.

### Restart-needed detection

Classify the candidate's changed paths, `git diff --name-only <merge-base>..<sha>`,
against a path-class table modelled on `cutover`'s schema-input list.
Evidence: `src/gobby/cli/cutover.py:39-43`
(excerpt_hash `3e64513499d1302b4b6eef1ccd26d4999c1d3e698634d9db2ce145fb3f2cc01b`):

```
39| _SCHEMA_INPUTS = (
40|     "crates/gcore/assets/schema",
41|     "crates/gcore/src/schema",
42|     str(_PIN_PATH),
43| )
```

| Class | Paths | Activation |
|---|---|---|
| cutover | `crates/**`, `Cargo.lock`, and the cutover schema inputs above | rebuild, promote the binary set, then cutover |
| restart | `src/gobby/**` (daemon-loaded Python and the `install/shared` templates that sync at startup), `pyproject.toml`, `uv.lock` | daemon restart |
| none | `docs/**`, `tests/**`, `.gobby/plans/**`, Markdown at the repository root | live on land |

The strongest class among the changed paths wins.

In the sample, every code leaf is restart- or cutover-class:
- #23134: `src/gobby/workflows`
- #23466: `src/gobby/mcp_proxy`
- #23477: `src/gobby/install`
- #23430: `src/gobby/mcp_proxy` and `src/gobby/storage`
- #23441: `src/gobby/cli`
- #23402: `src/gobby/tasks`
- #23467: `crates` and `Cargo.lock`

(`git show --name-only` on each candidate SHA.) So "approval only for restart"
means Orchestrator approval for nearly every code landing. Docs, tests and plans
land without approval.

Two classifications for the plan to decide:
- **`src/gobby/cli/**`.** The CLI runs as a fresh process per invocation, so a
  CLI-only change, such as #23441, is live on land.
- **`web/**`.** It needs a frontend build, not a daemon restart.

### Overlap detection

"Overlap" means the candidate's paths overlap another lane's in-flight
candidate. Define the in-flight set as the commits named by
`independent_review_approval` receipts on open tasks that are not ancestors of
0.5.0. These are LANDed but not yet landed, they are known to the daemon, and
they need no worktree scan. The test is:

- candidate paths: `git diff --name-only <merge-base(sha, 0.5.0)>..<sha>`;
- for each in-flight commit `x` on another task: the same path set for `x`;
- if the intersection is non-empty, require an `overlap` approval.

A candidate not yet LANDed is not in the set. Its own landing later meets this
one as a moved tip, which is handled below.

### Moved tip and rebase ownership

When 0.5.0 moved past the candidate's base:
- The **developer** rebases the candidate in the lane worktree (Josh permits
  developer rebases) and reruns its focused tests.
- The **LAND carries over** when the rebased commit's patch-id equals the LANDed
  commit's patch-id, and the candidate's paths do not intersect the paths
  changed on 0.5.0 since the old base.
- **Otherwise the reviewer re-reviews the delta** and records a new receipt.

The reviewer never rebases; it lands only commits it has a receipt for.

### Approval and freeze records

**Approval** is a new receipt kind, `landing_approval`, in
`CLOSE_RECEIPT_KINDS`:
- Its facts are `reason` (`restart` | `freeze` | `overlap`, comma-joined when
  several apply) and an optional `batch` id.
- `land_commit` requires one that names the exact `sha`.
- One approval may cover a batch. The Orchestrator approves N restart-class
  SHAs, they land, and one restart activates them all, followed by `activation`
  receipts.

This keeps restart batching as an Orchestrator policy without a separate queue.
The authority rule is a plan decision:
- reuse `activation`'s creator-or-delegator rule (the Orchestrator files or
  delegates most leaves), with `transfer_task_authority` as the fallback;
- or name the Orchestrator seat explicitly.

**Freeze** is a single project-scoped flag holding `on`, a `reason` and the
setter session. It is set and cleared only by the Orchestrator, for example
during pushes, restarts and cutovers. `land_commit` reads it under the advisory
lock, so a freeze set mid-queue stops the next landing. Its home is a plan
decision: a runtime key in `src/gobby/config/registry.py` `CONFIG_REGISTRY`
(the runtime config authority #23341 already names) or a hub row.

### Orchestrator latency per landing

- **Approval-free landings** (docs, tests, plans; the CLI if classified live):
  zero Orchestrator latency. The tool takes seconds after the LAND.
- **Restart-class landings:** one approval round-trip, which batch approval can
  amortize. The measured alternative is a 95-112 minute queue plus the restart
  wait.
- **Activation timing is unchanged.** It stays Orchestrator-owned. Landing no
  longer waits behind other lanes' landings.

### Seats and separation of duties

| | Seats | Handoff latency | Separation of duties | Who rebases |
|---|---|---|---|---|
| M1 central Merge Manager | 1 shared | queue; median 99-101 min measured | developer, reviewer and merger are three parties | MM or developer |
| M2 merge agent per lane | up to 7 more | per-lane handoff | three parties | developer |
| M3 reviewer lands (Josh's decision) | 0 more | seconds; approval round-trip only on exceptions | the developer cannot land, and the reviewer lands only what it LANDed | developer |

Under M3 the Merge Manager seat, the landing tasks ("chore: land reviewed") and
the landed-tree pytest per landing retire. The candidate's focused tests run
before the LAND, and fast-forward lands that exact tree.

## 4. Recommendation

**Close: option C, the receipt-gated mechanical close.** Reasons:
1. It keeps an independent semantic review, the lane reviewer's LAND, and gives
   it the stored-criteria check the close reviewer does today.
2. It removes both close reviews per leaf, about 12-20 minutes plus the queue
   and stale-verdict handling.
3. It closes the closed-but-unlanded gap mechanically with the ancestor gate,
   which retires the LM landing release.
4. It touches no stage machinery.

Relation to #23341 "Remove gobby build":
- C does not depend on #23341 and can land first.
- C removes close review from #23341's blast radius: #23341 no longer needs to
  carry close-gate changes.
- B is the option that conflicts with #23341, because it would repurpose stage
  states and `stages.yaml` rows that #23341 deletes.
- A, as written, waits for #23341.

**Landing: M3 as Josh decided**, with `land_commit` as specified in Section 3.
C and M3 share the receipt layer:
- `independent_review_approval` gates both landing and close;
- `landing_approval` gates exceptions;
- `activation` gates restart-class closes.

### Plan sections for the follow-up plan

1. Framing and rulings of record: Josh's three landing quotes and his question
   above.
2. As-is facts:
   - the gate list;
   - the receipt contract;
   - the interactive-block rule;
   - the convention-only reservation;
   - the timing evidence in this file.
3. Receipt layer:
   - patch-id matching;
   - the `landing_approval` kind and its authority rule;
   - the criteria facts carried in a LAND.
4. `land_commit` tool:
   - advisory lock, receipt check, freeze check;
   - the path-class table and restart classification;
   - the in-flight overlap check;
   - ff-only, `tip_moved` refusal, commit link, Orchestrator notification.
5. Freeze flag: its home, setter authority and enforcement point.
6. Main-checkout write guard: the rule blocking raw git writes that move 0.5.0
   in the main checkout, plus routing plan commits through `land_commit`.
7. Close gates:
   - gate 13 becomes a receipt match;
   - the new landed gate (ancestor of 0.5.0);
   - the new activated gate (activation receipt for restart and cutover
     classes).
8. Retire task-close-reviewer:
   - `agentic_close_review.py` and `_lifecycle_review_gate.py`;
   - the close-review queue and finalization;
   - its config fields and the agent definition.
9. Seat and role changes:
   - the lane reviewer lands and records criteria facts;
   - approval requests go to the Orchestrator, with the Inbox Manager copied;
   - the Merge Manager seat, landing tasks and LM close releases retire;
   - memory 283e9a81 is updated.
10. Receipt authors for tasks without a lane reviewer: the Adversary for plans,
    and a named reviewer for docs.
11. Documentation: `references/tasks/closing.md`, the guides and the role files.
12. Tests per section, including the #22903/#23498 plan-commit path and a
    closed-but-unlanded regression.
13. Rollout order: receipt layer first, then `land_commit` and the guard rule,
    then the gate changes, then retirement. Each step needs one activation
    restart.

Decisions left to the plan:
- the authority for `landing_approval`;
- the freeze flag's home;
- the class of `src/gobby/cli/**` and `web/**`;
- the receipt author per task category.

## 5. Scope Note

No product code changed under #23513. This file is the only artifact.
