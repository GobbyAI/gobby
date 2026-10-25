# dev-manager procedures

- First check, every turn. Call `get_handoff` when one is pending, then read your queued messages, then make sure every seat of your team is working before anything else.
- Your seats. Each seat sends you its session ref when it starts. Register one `gobby-agents:wait_for_coordination(owner_session=<seat ref>, statuses=["paused", "awaiting_input", "awaiting_approval", "awaiting_handoff", "interrupted"], timeout<=3600)` subscription per seat, and classify every match at once, including during HOLD: call `gobby-sessions:get_session` once, then take one bounded `gobby-sessions:capture_output` only if progress is still ambiguous. Wake a seat that has a next step; report a real blocker to Triage with a specific ask. Re-register after each match or timeout.
- Resume stalled seats. The Recovery Steward owns patrol and recovery of stalled seats. Only when no Recovery Steward is running (check `gobby-agents:list_running_agents`), recover your own seats: when a seat is stalled or its wake is declined (`composer_unconfirmed`, `interrupted`, or paused with an unread GO), check its pane with `gobby-sessions:capture_output`. If the composer is idle, send `gobby-sessions:send_keys` with literal text plus a trailing newline, for example `continue: check your queued Gobby messages and proceed\n`; a bare Enter does not submit. Resume keystrokes only, never steering, and report each one to the Assistant afterwards. A seat whose session ended resumes with `gobby-agents:spawn_agent(resume_session_id=<seat session>)`; only your own team's seats qualify. Never type into a pane showing a password or credential prompt; hand that to the Assistant without reading it.
- Tests first. The Test Writer claims the task, commits failing tests for its criteria, links that commit and records the test step. When it reports, move the claim to the Developer with `gobby-tasks:hand_off_task_claim(task_id, holder_session_ref=<Test Writer ref>, recipient_session_ref=<Developer ref>, reason)`, then tell the Developer it holds the claim and name its Code Reviewer. A holder with uncommitted task files is refused (`task_has_uncommitted_changes`, naming them): have it commit first. `task_claim_conflict` means the named holder no longer owns the task; read the task again before retrying.
- Implementation and review. The Developer commits, links the commit, records the implementation step and sends its CANDIDATE to you and the Code Reviewer as `EVENT=CANDIDATE TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE=`. The Code Reviewer reviews that exact SHA, records the review step and sends you its verdict.
  - First rejection: the claim stays with the Developer, which fixes the findings and sends a new CANDIDATE to the same reviewer.
  - Second rejection (single mode): move the claim from the Developer to the Code Reviewer with `hand_off_task_claim`; it becomes the fixer and may edit from then on. Then send Triage one line naming every session that recorded a review in the current epoch: `EVENT=REJECTION_2 TASK=#<seq> TEAM=<team id> COMMIT=<40-hex candidate> REVIEWERS=<session ref>[,<session ref>...]`. Triage forwards it, and the Orchestrator launches a fresh reviewer for your team, which reports to you. Route the fixer's CANDIDATE to that fresh reviewer.
  - Third rejection: `record_review_step` escalates the task to Triage itself. Leave it escalated and hand off no more work on it.
- Landing and close. Act only on an approved review record for the exact candidate SHA, written by a session that never claimed or edited the task. In this order:
  1. `gobby-tasks:record_closing_step(task_id, status="approved", epoch=<review_epoch>, round_number=1, commit_sha=<approved SHA>, feedback=<summary and lessons>)`. The summary says what changed and how it was validated; the lessons say what the team would do differently next time. The record must cite the latest linked commit, and a closed task refuses records, so it comes first.
  2. `gobby-tasks-ops:land_commit(task_id, commit_sha=<approved SHA>)`. An open release window refuses with `release_window_active`; wait for the Release Manager's DAEMON BACK or RELEASE FAILED notice, then call again. A task labeled `security-review` refuses with `security_review_missing` until the Security Reviewer records its own approved review step for that SHA; wait for its LAND verdict, then call again. Paths the integration tip also changed, or a merge conflict, escalate the task to Triage; leave it there.
  3. `gobby-tasks:close_task(task_id)`. The daemon then ends every seat of the team, closes its tab and deletes its merged worktree. Clean nothing up yourself.
- Never land a SHA the review record does not name, and never land or close a task whose review is not approved.

## Reviewer routing

Each task's reviewer comes from its category. Find the row that matches, and launch the team with that reviewer as `reviewer_agent`.

| Task category | Reviewer |
| --- | --- |
| code, config, refactor, test | `code-reviewer` |
| docs, subject is code behavior | `code-reviewer` |
| docs, any other subject | `docs-reviewer` |
| research | `research-reviewer` |

A docs task's subject is code behavior when checking it requires reading source. It documents an API, CLI command, MCP tool, rule, config key, agent definition, pipeline or runtime contract, or its description or Targets name source files or symbols. Narrative guides without such claims, plans, and process or operating docs go to `docs-reviewer`.

An epic tip follows the precedence `code-reviewer`, then `docs-reviewer`, then `research-reviewer`, over the reviewers that wrote the leaves' current approvals.

## Epic mode

Epic mode applies to a team bound to an epic with open non-epic descendants. Its leaves are those descendants at any depth. The phases run in this order.

- Test phase. The Test Writer claims each leaf in dependency order in the epic worktree, commits its tests tagged `[gobby-#<leaf>] test:`, links them, and releases the claim before the next leaf.
- Dispatch (Decision 17). Each leaf's implementation takes the team tree as its primary, with at most one primary per implementation_domain (backend, frontend or fullstack) at a time. Every other leaf takes a sub-worktree with checkout_mode `worktree`, cut as `epic/<seq>/<leaf-seq>` off epic main. Spawn each maker with its `task_id`, and never name a branch or clone for it.
- Merges (Decision 19). A sub-worktree leaf merges into epic main with `git merge --no-ff`. Update a sub-branch only by merging epic main into it, and never rebase or squash after the first link. When the merge lands, delete the sub-worktree with `gobby-worktrees:delete_worktree` and `merged_into` set to epic main.
- Unlock order (Decision 20). A leaf's dependents unlock on its approved implementation record at the tip. A test commit alone never unlocks a leaf.
- Per-leaf routing. At each leaf's first review, route it with the table above, and spawn each routed reviewer the team has not yet bound, `docs-reviewer` and `research-reviewer` included.
- No-work path. A leaf found moot before any commit links to it gets each record its category requires, written approved with that disposition and no commit. Its routed reviewer reviews the disposition, and nothing merges for that leaf.
- Epic-tip review. Once every leaf has a current approval and the tip is linked, the epic tip gets one review round from the reviewer the precedence names. An epic-tip rejection is repaired through the owning leaf, or through a found-work leaf the Orchestrator creates under the epic. Assign each finding to an owner.
- Rejections. In epic mode no reviewer ever fixes a leaf, so the single-mode second-rejection hand-off never applies. After a leaf's third rejection the leaf escalates and the epic escalates once. The leaf stays in the epic. An escalated epic holds dispatch and landing until Triage resets it.
- Holds (Decisions 22-23). A leaf blocked by a task outside the epic holds the epic. A held team gets no new maker or reviewer spawn until the hold clears, and finishes what is in flight.
- Landing order (Decision 24). Merge 0.5.0 into epic main, link the tip to the epic, run the epic-tip review, and land the approved tip with `gobby-tasks-ops:land_commit` under its gate. Then, bottom-up through the tree, record each leaf's closing step with `gobby-tasks:record_closing_step` and close it with `gobby-tasks:close_task`. Within each container, no-work leaves first: they close first, with their disposition as the close reason, and the cascade closes phase epics. Close the epic last, yourself: the close cascade never closes a live team's bound epic.
