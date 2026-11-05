# Orchestrator: teams

Load before launching or relaunching a DevTeam, plan team or research team,
admitting an epic, or handling review, sign-off or a disposition close.

## Launching

- Clear the queue in parallel. Launch one DevTeam per ready task whose files do not conflict with work in flight; serialize tasks that touch the same files.
- DevTeams. Use the unified dev-team pipeline for a task or epic: developer, code-reviewer and dev-team-lead. Pass task, workspace, report_to=<Triage ref> and its declared provider/model/effort defaults. The daemon moves claims through pipeline claim_flow on attestations. The lead checks security, lands, validates the integrated SHA after land, closes and cleans up after seats end. Developers never land, merge, close or delete worktrees.
- Merge-level before staffing. A tree only falls behind its base through a work stoppage. Before relaunching onto an existing tree (team_id) or resuming a stopped epic, have its lead merge-level the tree first: base merged in, every conflict resolved in favor of the newer code.
- Epic admission. Use a pipeline that explicitly supports the intended task topology and team tree. Parent-team coordination comes from the installed definition's declared authority and task-bound run. Keep task ownership, dependencies and worktree ancestry intact.
- Plans. Launch the `plan-team` runbook with `task=<task to plan>`, `workspace=<workspace ref>` and `report_to=<Triage ref>`; it opens the `Plan Team - #<task>` tab and reuses or creates a planning child. The Plan Writer records an approved implementation attestation for the full final plan SHA, with validation and consensus evidence, before end_agent_run. You are the named planning-task closer: review those exact bytes, record independent-review and validation, preview closure, and close the child once the selected policy passes. Route corrections through Triage. Present the plan to the operator through the Assistant as a decision; planning closure does not approve expansion. Expand only after the operator approves. Old or implemented plans move to `.gobby/plans/completed/` without review.
- Research. Choose the configured research workflow and inspect its current input schema. A report author records implementation evidence; the independently authorized reviewer records a review against the exact artifact or commit reference. Keep research seats in the main checkout unless they are associated with a developer task.
- Research team. Launch the `research-team` runbook with `task=<research task>`, `workspace=<workspace ref>` and `report_to=<Triage ref>`; it opens the `Research Team - #<task>` tab and admits a treeless team working in the main checkout. The Researcher reports READY_TO_CLOSE after the Research Reviewer returns LAND; close once the selected policy accepts that evidence.

## Review and sign-off

- Cost. Spawn one fresh reviewer per candidate SHA and have it end its own run after its verdict; reuse it only for a delta review of the same task.
- Review fast path. Send a small, concrete finding straight to the developer; the same reviewer checks the delta, or a fresh reviewer reviews the new SHA. Run code and security review in parallel on one candidate; security review goes only to the standing Security Auditor. Every reviewer prompt carries the known sandbox issues: pytest can stall on teardown tests, and reviewers run `uv run --no-sync`.
- Sign-offs follow the selected workflow. Each reviewer records its own truthful attestation against the exact candidate reference and criteria evidence. Keep priority, ownership, contract decisions, activation and smoke tests with the responsible coordinator.
- Reviewer selection. Review requirements come from installed definitions, rules and pipelines. The bundled `dev-team` pairs `developer` with `code-reviewer` and accepts no routed reviewer or legacy manager inputs. Code grants no authority by definition names.
- Research report review. A researcher's report spike need not launch a DevTeam. Check the report against its cited primary evidence, then record your own `gobby-tasks:record_attestation` (kind `independent-review`, `approved` or `rejected`) on the exact candidate reference. The selected report policy requires `approve_report_review` on your installed definition. Close once that policy accepts the evidence.
- Disposition closes. An authorized closer records a truthful disposition attestation with the chosen reason and evidence, including the replacement reference for superseded work. Preserve linked work and provenance. Preview the selected policy, then close once its requirements pass. Reasons carry no built-in role ownership.

_Version: 1.0.0_

_Last verified: 2026-11-05_
