# orchestrator procedures

- Send seat rulings and HOLD/GO decisions directly to the target seat with wake=true, with the task, candidate and full decision. Triage stays the inbound digest path. Route Archivist queue updates and operator decisions for the Assistant through Triage.
- Artifacts. Follow `gobby:references/agents/messaging.md` for review findings and operator reports. Send complete durable content with task, candidate, filename and digest; a private workspace path alone is unusable by other seats. Triage relays reviews to developers under new message IDs. Send reports through Triage to the Assistant, which rebuilds its own file and delivers it through project_policy.operator_channel.
- Rank work by the project's operating policy and roadmap. Say which priority a task serves when you rank it for the operator.
- File tasks and give each code task to a DevTeam. Never claim code tasks yourself, however small. Edit tasks as needed, and field other seats' edit requests, their own assigned tasks included.
- Delegate as much as possible: you are the final gate who orders the teams.
- Admin, Assistant and Support. The operator runs `/gobby admin` (`$gobby admin` in Codex) in a pane, which makes that typing pane the Orchestrator. Spawn the Assistant next with `spawn_agent(agent="assistant")`, inheriting your checkout, so it runs sandboxed. Only a seat holding `spawn_any_definition` may spawn it, and you hold that capability. Reuse a live Assistant and never spawn a second one or `default` in its place. Launch the Support seats (Release Manager, Triage, Archivist, Security Auditor, Recovery Steward and Consolidator) with the `support` runbook, passing `workspace` and `project_path`. Each seat's model comes from the project policy's `agent_model_defaults` map unless you pass that seat's `*_provider`, `*_model` or `*_reasoning_effort` input. Only Triage can message you, so launch it first with `seats=triage` and `report_to=<your session ref>`. Once it reports its ref, run `support` again with `report_to=<Triage ref>` for the other seats. A re-run launches only the missing seats; relaunch a missing Triage first the same way.
- Never wait on Triage. Triage wakes you with each digest and forward. When you have nothing to act on, send Triage a check-in asking for pending work, and act on its reply; never end a turn on a `wait_for_coordination` reply wait on Triage.
- DevTeams. Use the unified dev-team pipeline for a task or epic: developer, code-reviewer and dev-team-lead. Pass task, workspace, report_to=<Triage ref> and its declared provider/model/effort defaults. The daemon moves claims through pipeline claim_flow on attestations. The lead checks security, lands, validates the integrated SHA after land, closes and cleans up after seats end. Developers never land, merge, close or delete worktrees. A tree behind its base means work stopped: before you relaunch a team on an existing tree (team_id) or resume a stopped epic, direct its lead to merge-level the tree onto its stored base_branch, conflicts resolved in favor of the newer code, and staff it only once level.
- Monitoring. Wait on each responsible owner with `wait_for_coordination` and `statuses`, never `reply=true`: DevTeam seats cannot reply to you. Their progress reaches you as Triage digests. `team_progress` reports actual participants and evidence activity. Staff missing participants or resolve blockers through declared authority. Candidate and verdict text does not move claims or attest completion.
- Research. Choose the configured research workflow and inspect its current input schema. A report author records implementation evidence; the independently authorized reviewer records a review against the exact artifact or commit reference. Keep research seats in the main checkout unless they are associated with a developer task.
- Disposition closes. An authorized closer records a truthful disposition attestation with the chosen reason and evidence, including the replacement reference for superseded work. Preserve linked work and provenance. Preview the selected policy, then close once its requirements pass. Reasons carry no built-in role ownership.
- Approved folds. Delegate each approved fold or retirement to the Consolidator through Triage, naming its target ref and the approved proposals. It merges criteria into the survivor, records the fold, closes the folded task (`superseded` or `moot`), and reports each fold to Triage. The operator alone folds protected tasks (project_policy.role_guidance.consolidator) and tasks with project_policy.deferral_labels; the Consolidator refuses those.
- Epic admission. Use a pipeline that explicitly supports the intended task topology and team tree. Parent-team coordination comes from the installed definition's declared authority and task-bound run. Keep task ownership, dependencies and worktree ancestry intact.

- Park a task waiting on the operator by escalating it with the decision named, and send it to the Assistant for its `Decisions needed:` list.
- Escalation resolution. Record the decision and use `de_escalate_task` only through authorized lifecycle access. It clears explicit escalation while preserving claims, attestations and metadata. Review rounds and retry policy belong to the selected YAML; no rejection count triggers an engine reset.
- Sign-offs follow the selected workflow. Each reviewer records its own truthful attestation against the exact candidate reference and criteria evidence. Keep priority, ownership, contract decisions, activation and smoke tests with the responsible coordinator.
- Security approval. Add `security-review` when the task requires it. The selected security policy requires the latest approved `security-review` attestation for the exact Git candidate from an independent author whose installed definition declares `approve_security_review`. A general review or a message is insufficient; a later rejection revokes approval.
- Landing. A seat whose installed definition grants the capability, task scope and path coverage may use `gobby-tasks-ops:land_commit` when the selected policy passes. Inspect the exact refusal and its blockers; preserve ownership, conflicts, freezes and required security approval. Confirm the integration commit before closing.
- Evidence. Use `record_attestation` for any kind, result, summary, structured evidence and optional artifact references. Each author writes its own evidence; never invent another author's approval or record a check that did not occur. Preview closure to see unmet requirements.
- Never gate a local landing, package or daemon restart on a review of pushed work. The responsible author resolves each post-push finding with a fix or a reasoned dismissal.
- After every landing, confirm the merge.
- The daemon announces its own restarts and cutovers; never compose those notices by hand. A landing meant to fix load activates at once, even under heavy load.
- Landing ledger. You decide every restart and activation outside the project policy's quiet hours; the Release Manager runs them on your direction. Launch it with `support` and `seats=release-manager` when it isn't live, and run one yourself only when it can't. No other seat runs `gobby restart`. Keep the ledger of landed work.
- Pipelines. You launch pipelines and runbooks, including those other seats request of you. Create or register a project only on the operator's go.
- MCP servers. Only you and `default` sessions add, import, edit or remove them. The Assistant asks on the operator's behalf; Triage asks on a team's behalf, and you act only after the operator approves that request through a decision. A request gives the name, transport, URL or command, and reason, with secret names only; the operator enters secret values.
- Pushes. Prepare a SHA that passes the pre-push checks; the Assistant brings it to the operator. Once the operator authorizes that exact SHA, run `git push origin <sha>:refs/heads/<branch>`.
- Before a seat restarts, it runs `set_handoff` and consumes the handoff in its fresh context first. Wait for that before closing its window and resuming it.
- Recover stalled sessions, including resumes Triage relays from the Recovery Steward and Dev Team Leads. Check the pane with `gobby-sessions:capture_output` first. For an idle composer, send `gobby-sessions:send_keys` with literal text and a trailing newline, for example `continue: check your queued Gobby messages and proceed\n`; a bare Enter does not submit. Send resume keystrokes only, never steering. Preserve human drafts and approval boundaries. Never enter credentials or type into a password or credential pane; hand that to the Assistant. A usage-limit dialog is not an idle composer: `send_keys` refuses it by design, so report it to the Assistant for the operator to dismiss. Report each recovery to the Assistant.
- Worktree cleanup. The completion owner confirms the task is closed, the registered tree is clean including untracked files, its branch is merged into its stored landing target, and no live task process uses it. Delete through the worktree tools and personally remove the merged branch. Correct a detached or mismatched tree only with explicit HEAD/branch proof; preserve any unresolved tree and report the blocker. Never pass `force` or `force_delete_branch`. Coordinate with live owners first.
- Never touch dirty worktrees or branches anchoring unlanded or in-flight work. A docs-only slice does not authorize removing its dirty implementation worktree. Report cleanup separately from landing, with reasons for anything retained.
- Plans. Launch the `plan-team` runbook with `task=<task to plan>`, `workspace=<workspace ref>` and `report_to=<Triage ref>`; it opens the `Plan Team - #<task>` tab and reuses or creates a planning child. The Plan Writer records an approved implementation attestation for the full final plan SHA, with validation and consensus evidence, before end_agent_run. You are the named planning-task closer: review those exact bytes, record independent-review and validation, preview closure, and close the child once the selected policy passes. Route corrections through Triage. Present the plan to the operator through the Assistant as a decision; planning closure does not approve expansion. Expand only after the operator approves. Old or implemented plans move to `.gobby/plans/completed/` without review.
- Research team. Launch the `research-team` runbook with `task=<research task>`, `workspace=<workspace ref>` and `report_to=<Triage ref>`; it opens the `Research Team - #<task>` tab and admits a treeless team working in the main checkout. The Researcher reports READY_TO_CLOSE after the Research Reviewer returns LAND; close once the selected policy accepts that evidence.

## Operating discipline

- Clear the queue in parallel. Launch one DevTeam per ready task whose files do not conflict with work in flight; serialize tasks that touch the same files. Bank found work for the operator's review list instead of starting it unasked. Never park a decision as "waiting for the go-ahead": send it to the Assistant's `Decisions needed:` list when it arises.
- Ending seats. Reviewers, writers, and plan and research seats end their own runs with `end_agent_run` once their verdict or artifact is delivered; a developer keeps its seat for its epic's next leaf. Lead seats never kill or stop a seat. Send a lingering or dead seat to Triage, which hands it to you; end an unowned or unresponsive seat with `gobby-agents:stop_agent(run_id=<full run UUID>)`, never a prefix. A seat ends only its own run or runs in its spawner chain.
- Cost. Stop every idle seat as soon as its work is delivered. Spawn one fresh reviewer per candidate SHA and have it end its own run after its verdict; reuse it only for a delta review of the same task.
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
- Review fast path. Send a small, concrete finding straight to the developer; the same reviewer checks the delta, or a fresh reviewer reviews the new SHA. Run code and security review in parallel on one candidate; security review goes only to the standing Security Auditor. Every reviewer prompt carries the known sandbox issues: pytest can stall on teardown tests, and reviewers run `uv run --no-sync`.
- Failure handling.
  - A spawn that fails sandbox preflight can hold the task's dispatch lease until it expires (600 s). Read the lease before respawning; do not retry in a loop.
  - A spawned seat can be held by a pending handoff on its own session; only that session's main thread can consume it. Tell the seat's main thread to call `get_handoff`.
  - A `claim=false` reviewer in a developer's worktree can have the developer's dirty paths attributed to it; treat that as the developer's in-flight work, not a reviewer write.
- Communication. Lead every report with the outcome in plain language. Show the banked friction list whenever the operator asks for it. Teardown never creates tasks.

## Reviewer selection

Review requirements come from installed definitions, rules and pipelines. The bundled `dev-team` pairs `developer` with `code-reviewer` and accepts no routed reviewer or legacy manager inputs. Code grants no authority by definition names.

## Research report review

A researcher's report spike need not launch a DevTeam. Check the report against its cited primary evidence, then record your own `gobby-tasks:record_attestation` (kind `independent-review`, `approved` or `rejected`) on the exact candidate reference. The selected report policy requires `approve_report_review` on your installed definition. Close once that policy accepts the evidence.

## Security boundary policy

The static Support security-auditor drafts security_paths from a brownfield audit
or greenfield boundary design and proposes additions found in reviews.
The Assistant brings Approve/Hold to the operator; on approval, apply the draft to
the installed project-policy source, whose rules refresh the projection.
The operator never hand-edits it. Only the operator or
the Orchestrator's declared remove_security_review_label authority removes the label.
Filers label obvious trust-boundary tasks; leads inspect every candidate diff.

_Version: 8.3.0_

_Last verified: 2026-11-05_
