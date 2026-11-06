# Operate runbooks

Load when launching, relaunching, recovering or stopping a runbook: a pipeline
tagged `runbook` whose steps place agent seats. Find one with
`gobby-workflows:list_pipelines(tag="runbook")` or
`gobby pipelines list --tag runbook`.

Launch from an operator surface (CLI, web, gclient), an authorized cron
`pipeline` job, or a definition whose installed rule selectors permit pipeline
launches. The selected launch rules cover `run_pipeline`, exposed
`pipeline:<name>` tools and shell `gobby pipelines run`; a refused seat sends
the request to its authorized coordinator. Pass every
project-specific value as an input. Seat models come from the project policy's
`agent_model_defaults` map and the definitions, so pass a `<seat>_model` input
only to override one.

Cold start is separate from runbooks. Bare commands declared in an installed
definition's `cold_start_commands` activate it and reuse or open its declared
`cold_start_counterpart`. The bundled example is `admin`; the Assistant is spawned by the Orchestrator, not cold started.

| Runbook | Seats | Required inputs |
| --- | --- | --- |
| `support` | release-manager, triage, archivist, recovery-steward, consolidator, security-auditor | `workspace`, `project_path`, `report_to` |
| `dev-team` | developer, code-reviewer, dev-team-lead | `task`, `workspace`, `report_to` |
| `plan-team` | plan-writer, plan-adversary, plan-advocate | `task`, `workspace`, `project_path`, `report_to` |
| `research-team` | researcher, research-reviewer | `task`, `workspace`, `project_path`, `report_to` |

## Support

The bundled recipient policy requires `message_restricted_seats`, which the
example Triage definition declares. Launch it first: `seats=triage` with
`report_to=<Orchestrator ref>`. Once Triage reports
its ref, run `support` again with `report_to=<Triage ref>`. Its guard skips each
seat whose agent already runs live in the project on this machine, so a re-run
launches only the missing seats into the one Support tab.

## DevTeam

Run the single dev-team pipeline for either a task or epic with task, workspace
and report_to. Seat models come from the project policy's `agent_model_defaults`
map, or from each definition, unless a `<seat>_model` input overrides them. It launches developer,
code-reviewer and dev-team-lead in Dev Team - #N. The pair uses the registered
task tree; the lead stays in main. admit_team chooses single-task or epic mode.
The bundled file is dev-team.yaml; there is no separate epic pipeline.

claim_flow is declared in the pipeline and applied atomically by record_attestation:
implement requires test-before, implementation, test-after, self-review and
validation; review requires independent-review and validation; a rejection returns
the claim to implement; completed review moves it to the lead's validate stage.
Each stage validates against task.validation_criteria with stage and a per-criterion
[{criterion, met, how}] list. Results follow each kind's allowed_results and guidance.

The lead checks attestations and security, lands, validates the integrated SHA
after landing, records closing and closes. Developers and reviewers never land,
merge into the integration branch, close or delete worktrees. When the lead asks,
a developer merges the base into its own tree under its claim. The lead deletes only clean merged worktrees
after associated seats end, without force. Superseded evidence remains in history.

For an epic retain the primary pair and shared tree; the lead schedules ready
leaves and parallel child teams through the same pipeline using parent_team_id.
An epic has one lead. A run with parent_team_id launches developer and reviewer
seats in its child tree but no lead: the guard maps its dev-team-lead seat to the
epic team's live lead, connect-epic-lead messages that lead the child's team, tree
and seats, and at validate the daemon hands the epic lead the leaf's claim. The
epic lead lands the leaf into the epic branch under clean-tree coordination,
validates, closes it and removes the clean child tree. Once every descendant is
closed, an epic team seat may claim the epic root for final validation. The lead's named
integration-leaf step provides a claimable task for implementation repairs and
final integration checks; the developer holding that leaf performs base merges
under its claim. Final review is independent
of all descendant code/test authors and binds the candidate and subtree_revision.

Relaunch missing seats with seats and team_id, the original admission execution,
preserving the task, tree and provider/model/effort defaults. Never duplicate a live
team. Report through the configured report target and use durable waits.

## Planning

The Orchestrator launches `plan-team` with `task` and `report_to=<Triage ref>`; it opens
the `Plan Team - #<task>` tab, and `seat_pane` adds seats to an existing pane. The operator
launches `plan-team` directly:
`gobby pipelines run plan-team -i workspace=<name> -i task=<#N> -i project_path=<path> -i report_to=<ref> -i seats=<names>`.

## Research

The Orchestrator launches `research-team` with `task` and `report_to=<Triage ref>`; it opens
the `Research - #<task>` tab, and `seat_pane` adds seats to an existing pane. The team is treeless:
its seats work in the main checkout with no branch or worktree. The operator launches
`research-team` directly:
`gobby pipelines run research-team -i workspace=<name> -i task=<#N> -i project_path=<path> -i report_to=<ref> -i seats=<names>`.

## Guard, restart and stop

The first step, `gobby-agents:check_runbook_seats(workspace, requested,
catalogue)`, refuses a bad seat name, another live execution of the same
runbook in the same project, workspace and machine (with a `task` input, only
one for the same task, so teams for different tasks launch in parallel), a
missing or disabled seat agent definition, a missing `report_to` where the
runbook requires one, and any
failed or truncated lookup. It checks no capacity and no title. Each later seat
step is one placed `spawn_agent` call with `reserved_run_id: ${{ invocation_id }}`;
its output carries `run_id` and `pane_ref`. Seats are identified by
`project#session_ref`, and titles never refuse a launch. Do not call
`wait_for_agent` on a standing seat.

After a restart, the execution resumes from its launch snapshot. A started seat
is adopted with its original `run_id`; `seat_launch_unsettled` and
`invocation_conflict` refuse without launching (see [recovery](recovery.md)).

A failed runbook is never resumed: `resume_pipeline` returns
`runbook_resume_refused`. Relaunch only the missing seats with a fresh run that
names them in the `seats` input; launched seats keep running.

To stop a seat, read its `run_id` from the step output in
`gobby pipelines runs show <execution-id> --json` or `get_pipeline_status`, then
call `gobby-agents:kill_agent` with that recorded run id. `cancel_pipeline` does
not reach seats once the pipeline child has ended.

Guides: [runbooks](../../../../../../../../docs/guides/runbooks.md) and
[pipelines](../../../../../../../../docs/guides/pipelines.md#runbooks).

_Version: 5.0.0_

_Last verified: 2026-11-05_
