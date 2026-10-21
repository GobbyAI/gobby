# Open tasks made moot by Astra's work

Task #24782. Researcher gobby#17246, 2026-10-21. Integration tip `ac3182b54f` on `0.5.0`.

## Summary

There are 530 open tasks. Josh's subtrees, #22978 Triage and #22225 Annotate, were listed but not changed.

- **Moot at tip `ac3182b54f`:** no open task is wholly moot. The landed remove-gobby-build leaves (P1, 2.1–2.4, ending with #23850 in `d8dd689d8d`, `ea8b712d92` and `ac3182b54f`) already closed their own tasks. Five open tasks still carry scope or text that is moot now. They are listed in the first table and only need trimming.
- **Moot once runbook cohort v4 lands** (`.gobby/plans/runbook-devteam-redesign.md`): 37 tasks across 29 rows.
  - P1 removes the close relay. That covers 25 tasks: gate 8, 10, 12 and close-review defects, the #23640 subtree, Gate 10 reuse under #24538, the #24617 host harness, and three post-0.5.0 or Triage items.
  - P2 and P3 change landing authority, receipts, reporting and seat lifecycle. That covers 11 tasks, including the whole #23542 reviewer-landing epic (4 tasks).
  - #23817 is superseded by Decision 7.
  - Nine rows carry a *verify* caveat.
- **Related but not moot:** 23 rows, chiefly:
  - The #23561 content-ownership tree. Gate 9 survives as invariant 3.
  - Restart and cutover-hold defects. Release restarts remain.
  - #23433 and #23902, which the plan keeps.
  - Tasks whose scope only needs trimming.
- **Six open questions for Josh**, listed at the end.

## Method

1. I paged `gobby-tasks:list_tasks(closed=false, view=compact)` to completion: 530 rows, following `next_offset` exactly. Compact pages return fewer than 25 rows when rows are large (#24743). One row came back elided, #23359; I read it with `get_task`.
2. I read every open title and path against these sources:
   - `.gobby/plans/remove-gobby-build.md`: open leaves #23851–#23861 and #24062.
   - `.gobby/plans/runbook-devteam-redesign.md`: Decisions 1–13 and §§1.1–6.1.
   - The landed commits `d8dd689d8d`, `ea8b712d92` and `ac3182b54f`.
3. I read full cards wherever a title alone left the class ambiguous: #23433, #23817, #22643, #23678, #22660, #23756, #24149, #24398, #24485, #24617 and #23359.
4. Classes:
   - **Moot at tip:** the code it targets is already gone at `ac3182b54f`.
   - **Moot once v4 lands:** a named v4 section deletes or replaces the code or role it targets.
   - **Related:** v4 or remove-gobby-build touches it, but the work keeps a consumer.
5. Limit: most tasks were classified from title and path, not full descriptions. Rows marked *verify* need one check against the landed section before closing.

## Already moot at tip ac3182b54f

No whole task. These open tasks carry stale scope or stale instructions only:

| Task | Title | Why | Evidence |
|---|---|---|---|
| #24149 | S2.7 hub half: machine-scoped attention, roster, response routing and dispatch gate in Rust | The "dispatcher's machine-eligibility gate" scope ports a dispatcher that no longer exists. The roster, attention and routing scope stays. | `d8dd689d8d` retires the dispatcher drivers |
| #21564 | Attention, agents, dispatch, and worktrees with machine scoping | Same dispatch scope as #24149, its child. | `d8dd689d8d` |
| #21567 | Workflows, rules, pipelines, build, and validation | The "build" part of this Rust port has nothing left to port. Its "validation" part shrinks further under v4 P1. | #23849 (2.3) removed the build CLI, MCP and HTTP routes; `d8dd689d8d` |
| #22643 | Handoff stop gates and pull order | The card says "Expand through `gobby build`", which no longer exists. The plan content is unaffected. | #23849 |
| #17436, #17769, #17438 | Node mode; Per-user auth; Fleet management | Each still carries a stage row (`development`/`ready`). The rows are cleared when stages are dropped. | #23858 and #23861 drop stages |

## Moot once runbook cohort v4 lands

| Task | Title | Why | Evidence |
|---|---|---|---|
| #24494 | TDD gate 12 does not credit a passing Rust integration-binary test | The transcript RED gate is deleted. | §1.2, Decision 3 |
| #24748 | Gate 12 evidence cleanup | Same. | §1.2 |
| #24498 | Gate 12 TDD confirmation that survives evidence eviction | Same. | §1.2 |
| #24779 | enforce-tdd-block roots write authorization at the hook project_path | `enforce-tdd-block.yaml` is deleted. | §1.2 targets |
| #24777 | safe_evaluator: `_tdd_authorization` evaluates twice | `tdd_write_authorization.py` and its `condition_helpers.py` bindings are deleted. | §1.2 targets |
| #23868 | Close preview gate 10 drops pytest credit after a rebase | Close keeps four invariants; validation-command evaluation is removed. | §1.1, §1.4, Decision 4 |
| #24497 | Gate 10 npx -c and make -f recognition | Same. *Verify:* this recognizer must not be the found-work validation detection that §1.4 preserves. | §1.1, §1.4 |
| #24085 | Recover #23252: close gate extracts command-shaped prose from artifact references | Close-only acceptance-artifact resolution is retired. | §1.4 |
| #24509 | close_task preview of a no-commit manual chore exceeds the 300 s limit | Preview calls the same four-invariant evaluator with no evidence pass. *Verify:* re-time after §1.1. | §1.1 |
| #24736 | Close-preview negative control and lambda formatting (#24650 LOWs) | The preview contract is replaced. | §1.1 |
| #23640, with #23642, #23644, #23645 | Partial-deletion proof exemption: size-proof check in close gate 8; recheck close gate inputs | Close gate 8 is gone. The edit-time monolith hook is unaffected. | §1.1 |
| #24295 | INVALID close-review wake gives the author no blocking_reasons | Automatic close review is retired. | §1.3, Decision 12 |
| #24723 | Close-reviewer SRT denials | Same. *Verify:* a DevTeam reviewer running tests may hit the same rc and bootstrap denials. | §1.3 |
| #24727 | Close-review sandbox: settings.json rewrite and uv `.venv` lock | Same, with the same *verify* caveat. | §1.3 |
| #24546, #24555, #24556 | P3: Gate 10 reuse (under #24538) | Gate 10 is removed. | §1.1, §1.4 |
| #24552 | Transcript gates credit launcher runs | Transcript gates are removed. | §1.2, §1.4 |
| #24617 | Host e2e harness with single-use admission and a daemon run receipt | Its acceptance is Gate 10 credit, and admission comes from the RM heavy-slot GO. Both are removed. See open question 2. | §1.1, §1.4, §4.1, Decision 9 |
| #24403 | Land-gate classifier: crate test-only paths should be no-activation | The restart-approval and activation receipt prerequisite is removed from landing. *Verify:* release still classifies activation. | §2.4, Decision 8 |
| #24031 | delegate_task refuses a session that took authority through transfer_task_authority | Creator/delegator authority-transfer relays retire. | §2.4, Decision 8 |
| #24646 | record_close_receipt has no supersede path for a mistaken receipt fact | Verdicts become atomic: a replay returns the original and a contradictory submission fails. Activation and landing-approval receipt kinds are removed. *Verify.* | §2.3, §2.4 |
| #23817 | Task lifecycle state model after gobby build | Decision 7 fixes five states (open, blocked, in_progress, closed, escalated). The plan says to reconcile it. See open question 1. | §2.2, Decision 7, §Existing work |
| #23542, with #23707, #23544, #23550 | Reviewer landing through land_commit (M3): role files, docs | The Dev Manager lands, using another reviewer's exact-SHA approval. Lane-manager roles are rewritten. | §2.4, §3.5, Decisions 8 and 12 |
| #24656 | One reporter per event to the Orchestrator | Only the Assistant and Triage message the Orchestrator. | §3.4, Decision 1 |
| #22660 | Automatically clear persistent role sessions between tasks | DevTeam seats serve one task and collapse; there is no recycling. See open question 4 for standalone seats. | §3.1–3.3, Decision 2 |
| #24135 | Developer step stays in the claim step after reclaiming a handed-off task | Claims pass within one team; definitions are rewritten. *Verify* against the new developer definition. | §3.2, §3.5 |
| #24398 | code-reviewer park check compares candidate_task verbatim | The candidate/park relay is replaced by verdicts and the team review loop. *Verify* that `code-reviewer.yaml` no longer has a park block. | §2.3, §3.2, §3.5 |
| #18784 | Mechanical close-gate receipt check for the test-types ratchet | No close evidence gates remain. | §1.1, Decision 4 |
| #21528 | Qwen Code subagent transcripts the close gate must fold in | No transcript close gates remain. | §1.2, §1.4, Decision 3 |
| #21972 (Josh's #22978; listed only) | Make close validation policies configurable and language-aware | Close validation policies are removed. | §1.1, §1.4 |

## Related but not moot

| Task | Title | Relation |
|---|---|---|
| #23561, with #23565–#23569 | Content-based ownership for close gates and edit hooks | Gate 9 survives as invariant 3: no task-attributed edits left uncommitted (Decision 4). Still needed. |
| #24590 | Gate 9 live-interval attribution | Same: invariant 3. |
| #24493 | Rule 3 operation exemption can't tell stale ownership from a foreign write | Ownership attribution feeds invariant 3. |
| #24197 | land_commit gates: approvals survive card changes; overlap counts superseded candidates | §2.4 rewrites landing authority. The exact-SHA and overlap rules continue (2.3.2, 2.4.2). Re-scope against §2.4. |
| #24623 | Review follow-ups: claim release history; stale close-evaluation comments; developer submit wording | The close-evaluation part goes with §1.4. The claim-release history stays. |
| #24399 | Claim-release path for a seat that claimed only to attribute a commit | Claim transfer is reworked in §3.2. Check for overlap. |
| #24304 | Reviewer proof friction: nextest JUnit write, scratchpad writes | Reviewers still run the tests themselves (Decision 3). |
| #24485 | Daemon-side descendant inventory for stopped validation runs | Still useful. Its heavy-grant motivation goes in §4.1. |
| #24538 P2: #24545, #24550, #24551, #24553; #24534; #24557 | Bounded-run launcher, its research and docs | Loses its Gate 10 and transcript-credit consumers. See open question 3. |
| #23370 | Retire the transcript evidence pickle codec | Transcript evidence stays for found-work (§1.4). |
| #24169, #24725, #24618 | Restart-hold and cutover defects | Releases still restart and announce (Decision 10). |
| #23902 | Concurrent cargo and test-hub load research | Explicitly kept (Decision 11). |
| #23433, with #23826, #23827 | Worktree spawn code-index time | Plan: verify snapshot-only worktree indexing before changing status. DevTeam worktrees keep it relevant. |
| #23756 | require-dart-skill gate fires on `.gobby/build.yaml` | Gobby's trigger file goes with #23851 (2.5 deletes `gobby.config.build`). The basename matcher bug remains for other repositories. |
| #23678 | Batch spawn retirement | Planned deletion of `_dispatch_batch.py` after #23852. Remains real work. |
| #22643, #23669 | Handoff stop gates and pull order; staleness check | Unaffected except for the stale "gobby build" text. |
| #24773, #24722 | seat-no-spawn exemption; protocol-line rejection | Definitions are rewritten in §3.5. Re-check against the new roles. |
| #24720, #24776 | Plan Adversary CONSENSUS latch reads only the first line | Planning roles are kept. These two are duplicates of each other. |
| #24062 | Web leftovers after the Stages tab removal | A remove-gobby-build leaf. |
| #24732, #24743 | list_tasks compact paging and row elision | Unaffected. Same root area; candidates to combine. |
| #18580 | Recorded acknowledgement for edits outside declared scope | Decision 4 assigns scope judgment to the reviewer and the Dev Manager. See open question 5. |
| #21312 (Josh's #22978; listed only) | EDV-style verify stage | A feedback "stage", unrelated to task stages. Parked on jev. |
| #22225 (Josh's) | Gobby Annotate | Unaffected. |

## Open questions for Josh

1. **#23817:** Decision 7 fixes five task states. Your list also had deferred with a follow-up date, human review distinct from escalation, and type-specific review states. Close #23817 as superseded, or keep it for the states Decision 7 omits?
2. **#24617, host e2e harness:** with Gate 10 and heavy grants gone, should reviewers still get a host route to run the 14 SRT-spawning e2e tests? macOS refuses nested Seatbelt, so they can't run in a seat. Or drop the harness?
3. **#24538 P2, bounded-run launcher** (#24550, #24551, #24553, research #24534): its consumers were Gate 10 reuse and transcript credit. Keep it as a general bounded-run tool, or drop it with P3?
4. **#22660:** apply automatic clear between tasks to the standalone researcher and to Admin seats, or drop it?
5. **#18580:** scope acknowledgement moves to reviewer judgment under Decision 4. Drop it?
6. **Closing the "Moot once v4" rows:** should each close with a no-work disposition citing the landing commit of its v4 section, or should they all close at once at §6.1? Rows marked *verify* need that one check first.
