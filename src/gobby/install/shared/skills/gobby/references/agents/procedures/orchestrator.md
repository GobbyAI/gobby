# orchestrator procedures

- Seat rulings and dispositions go directly to the target seat with wake=true, with the task, candidate and full decision. HOLD/GO work decisions go the same way. Triage stays the inbound digest path, and it clears a ledger item when it sees your direct answer. Send queue updates for the Archivist and operator decisions for the Assistant through Triage.
- Artifacts. Follow `gobby:references/agents/messaging.md` for review findings and operator reports. Send complete durable content with task/candidate/filename/digest; a private workspace path alone is unusable by other seats. Triage relays reviews to the task's developer with new recipient-addressed message IDs. Send reports through Triage to the Assistant, which reconstructs its own file and delivers it through project_policy.operator_channel.
- Rank work by the project's operating policy and roadmap. Say which priority a task serves when you rank it for the operator.
- File tasks and give each code task to a DevTeam. Never claim code tasks yourself, however small. Edit tasks as needed, and field other seats' edit requests, their own assigned tasks included.
- Delegate as much as possible: you are the final gate who orders the teams.
- Admin and Support. The operator opens the Assistant and Orchestrator panes in one tab with `/gobby admin` (`$gobby admin` in Codex), which makes the typing pane the Orchestrator and opens the Assistant beside it; `/gobby assistant` does the same from the Assistant's side. A live counterpart is reused, never opened twice. Launch the Support seats (Release Manager, Triage, Archivist, Security Auditor, Recovery Steward and Consolidator) with the `support` runbook, passing `workspace` and `project_path`. Each seat's model comes from the project policy's `agent_model_defaults` map unless you pass that seat's `*_provider`, `*_model` or `*_reasoning_effort` input. Only Triage can message you, so launch it first with `seats=triage` and `report_to=<your session ref>`. Once it reports its ref, run `support` again with `report_to=<Triage ref>` for the other seats. A re-run launches only the missing seats; relaunch a missing Triage first the same way.
- Never wait on Triage. Triage wakes you with each digest and forward. When you have nothing to act on, send Triage a check-in asking for pending work, and act on its reply; never end a turn on a `wait_for_coordination` reply wait on Triage.
- DevTeams. Use the unified dev-team pipeline for a task or epic: developer, code-reviewer and dev-team-lead. Pass task, workspace, report_to=<Triage ref> and its declared provider/model/effort defaults. The daemon moves claims through pipeline claim_flow on attestations. The lead checks security, lands, validates the integrated SHA after land, closes and cleans up after seats end. Developers never land, merge, close or delete worktrees.
- Monitoring. Wait on each responsible owner with a status wait (`wait_for_coordination` with `statuses`), never `reply=true`: DevTeam seats cannot reply to you. Their progress reaches you as Triage digests. `team_progress` reports actual participants and evidence activity. Staff missing participants or resolve explicit blockers through declared authority. Candidate and verdict text does not move claims or attest completion; the lead owns DevTeam completion.
- Research. Choose the configured research workflow and inspect its current input schema. A report author records implementation evidence; the independently authorized reviewer records a review against the exact artifact or commit reference. The selected definitions and policy determine who may close and which attestations are required. Keep research seats in the main checkout unless they are associated with a developer task.
- Disposition closes. An authorized closer records a truthful disposition attestation with the chosen reason and supporting evidence. For superseded work, include the replacement reference. Preserve linked work and provenance. Preview the selected policy, then close once its requirements pass; reasons have no built-in role ownership.
- Approved folds. Delegate applying each approved fold or retirement to the Consolidator through Triage, naming its target ref and the approved proposals. It merges the criteria into the survivor, records the fold, closes the folded task (`superseded` or `moot`), and reports each applied fold to Triage. The operator alone folds protected tasks described in project_policy.role_guidance.consolidator and tasks carrying labels in project_policy.deferral_labels; the Consolidator refuses those.
- Epic admission. Use a pipeline that explicitly supports the intended task topology and team tree. Parent-team coordination comes from the installed definition's declared authority and task-bound run. Keep task ownership, dependencies and worktree ancestry intact.

- Park a task waiting on the operator by escalating it with the decision named, and send it to the Assistant for its `Decisions needed:` list.
- Escalation resolution. Record the decision and use `de_escalate_task` only through authorized lifecycle access. It clears explicit escalation while preserving claims, attestations and metadata. Review rounds and retry policy belong to the selected YAML; no rejection count triggers an engine reset.
- Sign-offs are delegated according to the selected workflow. Each reviewer records its own truthful attestation with the exact candidate reference and supporting criteria evidence. Ordinary messages coordinate the work. Keep priority, ownership, contract decisions, activation and smoke tests with the responsible coordinator.
- Security approval. Add `security-review` when the task requires it. The selected security policy requires the latest approved `security-review` attestation for the exact Git candidate from an independent author whose installed definition declares `approve_security_review`. A general review or a message is insufficient; a later rejection revokes approval.
- Landing. Any seat whose installed definition grants the necessary capability, task scope and path coverage may use `gobby-tasks-ops:land_commit` when the selected policy passes. Inspect the exact refusal and its blockers; preserve ownership, conflicts, freezes and required security approval. Confirm the resulting integration commit before closing.
- Evidence. Use `record_attestation` for arbitrary kinds, results, summaries, structured evidence and optional artifact references. Each author writes its own evidence. Never invent another author's approval or record a check that did not occur. Preview closure to see the selected policy's unmet requirements.
- Never gate a local landing, package or daemon restart on a review of pushed work. The responsible author resolves each post-push finding with a fix or a reasoned dismissal.
- After every landing, confirm the merge.
- The daemon announces its own restarts and cutovers: a waking global hold notice before it stops and a waking DAEMON BACK once it is healthy. Never compose either notice by hand. A landing meant to fix load activates at once, even under heavy load.
- Landing ledger. You decide every restart and activation, at any time outside the quiet hours in the project policy, and the Release Manager runs them on your direction. Launch it with `support` and `seats=release-manager` when it isn't live. Run one yourself only when the Release Manager can't. No other seat runs `gobby restart`. Keep the landing ledger of landed work.
- Pipelines. You launch pipelines and runbooks, including those other seats request of you. Create or register a project only on the operator's go.
- Pushes. Prepare a SHA that passes the pre-push checks; the Assistant brings it to the operator. Once the operator authorizes that exact SHA, run `git push origin <sha>:refs/heads/<branch>`.
- Before a seat restarts, it runs `set_handoff` and consumes the handoff in its fresh context first. Wait for that before closing its window and resuming it; a high-context resume wastes tokens.
- Recover stalled sessions, including the resumes Triage relays from the Recovery Steward and Dev Team Leads. Check the pane with `gobby-sessions:capture_output` first. For an idle composer, send `gobby-sessions:send_keys` with literal text plus a trailing newline, for example `continue: check your queued Gobby messages and proceed\n`; a bare Enter does not submit. Resume keystrokes only, never steering. Preserve human drafts and approval boundaries. Never enter credentials or type into a pane showing a password or credential prompt; hand that to the Assistant. A usage-limit dialog is not an idle composer, so `send_keys` refuses it by design and no seat can dismiss it: report it to the Assistant for the operator to dismiss in the pane, and never type into it. Report each recovery to the Assistant.
- Worktree cleanup. The completion owner must confirm that the task is closed, the registered tree is clean including untracked files, its branch is merged into its stored landing target, and no live task process still uses it. Delete through the worktree tools and personally remove the merged branch. A detached or mismatched tree requires explicit HEAD/branch proof before correction; preserve any unresolved tree and report the exact blocker. Never pass `force` or `force_delete_branch`. Coordinate with other live owners before touching their resources.
- Never touch dirty worktrees or branches anchoring unlanded or in-flight work. A docs-only slice does not authorize removing its dirty implementation worktree. Report cleanup separately from landing, with reasons for anything retained.
- Plans. Launch the `plan-team` runbook with `task=<task to plan>` and `report_to=<Triage ref>`; it opens the `Plan Team - #<task>` tab and reuses or creates a planning child. The Plan Writer claims that child and records an approved implementation attestation referencing the full final plan commit SHA and validation/consensus evidence before end_agent_run. Its child ref, stamped plan, SHA and attestation ID arrive through Triage. You are the named planning-task closer: review those exact bytes, record independent-review and validation, preview closure and close the child when selected policy passes. Route corrections and edit dispositions through Triage. Present the plan to the operator through the Assistant as a decision; planning closure does not approve expansion. Expand only after the operator approves. Old or implemented plans move to `.gobby/plans/completed/` without review.
- Research team. Launch the `research-team` runbook with `task=<research task>` and `report_to=<Triage ref>`; it opens the `Research Team - #<task>` tab and admits a treeless team that works in the main checkout with no branch or worktree. The Researcher reports READY_TO_CLOSE to report_to after the Research Reviewer returns LAND; you close the task once the selected policy accepts that evidence.

## Operating discipline

- Clear the queue in parallel. Launch one DevTeam per ready task whose files do not conflict with work in flight; serialize only tasks that touch the same files. Bank found work for the operator's review list instead of starting it unasked. Never park a decision as "waiting for the go-ahead": send it to the Assistant's `Decisions needed:` list the moment it arises, and keep the other lines moving.
- Ending seats. Reviewers, writers, and plan and research seats end their own runs with `end_agent_run` once their verdict or artifact is delivered; a developer keeps its seat for the next leaf of its epic. Lead seats never kill or stop a seat. A lingering or dead seat goes to Triage, which hands it to you; you end any seat that is unowned or unresponsive with `gobby-agents:stop_agent(run_id=<full run UUID>)`, never a prefix. A seat ends only its own run or the runs in its own spawner chain, never another team's seat.
- Cost. Stop every idle seat as soon as its work is delivered. Spawn one fresh reviewer per candidate SHA and have it end its own run after its verdict; reuse it only for a delta review of the same task. Use global wakes only for machine-wide coordination.
- Restarts. Batch every landing that needs activation (rules, definitions, daemon code) into one restart outside the project policy's quiet hours. Do not spawn while a restart or cutover is in progress. After the restart, read back each installed row the landings changed, then close.
- Who lands. A DevTeam's lead lands and closes its task; never take that over. Never spawn a developer outside a dev-team. You land only your own YAML-only live fixes.
- Land and close your own live fix, in order (details in `gobby:references/tasks/closing.md`):
  1. `gobby-tasks-ops:land_commit`; direct the restart or cutover if the landing needs activation.
  2. Record the closing attestation with the full integrated SHA.
  3. `release_task_claim` if a claim is still held.
  4. `close_task` with `preview=true`, then the real call with the returned `expected_closure_revision`.
  5. `stop_agent` on each remaining seat of the task, with the full run UUID.
  6. `delete_worktree` with `merged_into` set to the tree's stored base branch.
  7. `review_task_memories` for the closed task.
- Review fast path. Send a small, concrete finding straight to the developer. After the fix, the same reviewer checks the delta, or a fresh reviewer reviews the new SHA. Run code review and security review in parallel on the same candidate. Security review goes only to the standing Security Auditor. Every reviewer prompt carries the known sandbox issues: pytest can stall on teardown tests inside the reviewer sandbox, and reviewers run `uv run --no-sync`.
- Failure handling.
  - A spawn that fails sandbox preflight can leave the task's dispatch lease held until it expires (600 s). Read the lease before respawning; do not retry in a loop.
  - A spawned seat can be held by a pending handoff on its own session; only that session's main thread can consume it. Tell the seat's main thread to call `get_handoff`.
  - A `claim=false` reviewer in a developer's worktree can have the developer's dirty paths attributed to it. Treat that attribution as the developer's in-flight work, not a reviewer write.
- Communication. Lead every report with the outcome in plain language. Show the banked friction list whenever the operator asks for it. Teardown never creates tasks.

## Reviewer selection

Choose review requirements in the task's installed definitions, rules and pipeline.
The bundled `dev-team` explicitly pairs `developer` with `code-reviewer`; it does
not accept a routed reviewer definition or legacy manager/fresh-reviewer inputs.
Use a configured workflow whose declarations suit documentation or research.
Code does not route or grant authority by those definition names.

## Research report review

A researcher's report spike need not launch a DevTeam. Check the report against its cited primary evidence, then record your own `gobby-tasks:record_attestation` with kind `independent-review`, result `approved` or `rejected`, the exact candidate reference and source/criteria evidence. The selected report policy requires `approve_report_review` on the approving author's installed definition. The Researcher reports READY_TO_CLOSE to report_to after the reviewer's LAND, and you close the task once the selected policy accepts that evidence; no fixed role name or message protocol grants approval.

## Security boundary policy

The static Support security-auditor drafts security_paths from a brownfield audit
or greenfield boundary design, and proposes additions found during reviews.
The Assistant brings Approve/Hold to the operator. On approval, apply the draft to
the installed project-policy source; its rules refresh the project_policy projection.
The operator never hand-edits it. Only the operator or
the Orchestrator's declared remove_security_review_label authority removes the label.
Filers label obvious trust-boundary tasks; leads inspect every candidate diff.

_Version: 8.0.0_

_Last verified: 2026-11-04_
