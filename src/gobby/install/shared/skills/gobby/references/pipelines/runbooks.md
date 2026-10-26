# Operate runbooks

Load when launching, relaunching, recovering or stopping a runbook: a pipeline
tagged `runbook` whose steps place agent seats. Find one with
`gobby-workflows:list_pipelines(tag="runbook")` or
`gobby pipelines list --tag runbook`.

Launch from an operator surface (CLI, web, gclient), an authorized cron
`pipeline` job, or the Orchestrator seat; the Assistant seat launches only
while the Orchestrator is down. Every other agent seat is blocked from
`run_pipeline`, the exposed `pipeline:<name>` tools and a shell
`gobby pipelines run`, and sends the request to Triage. Pass every
project-specific value as an input, plus the runbook's entry under the project
policy's `launch_defaults`.

Admin is not a runbook. `/gobby admin` (`$gobby admin` in Codex) makes the
typing pane the Orchestrator and opens the Assistant beside it in the same tab;
`/gobby assistant` does the same from the Assistant's side.

| Runbook | Seats | Required inputs |
| --- | --- | --- |
| `support` | release-manager, triage, archivist, recovery-steward, consolidator | `workspace`, `project_path`, `report_to` |
| `devteam` | test-writer, developer, code-reviewer, dev-manager | `task`, `workspace`, `report_to` |
| `team-planning` | plan-writer, plan-adversary, plan-enhancer | `workspace`, `project_path`, `report_to` |
| `planning` | writer, enhancer, adversary | `workspace` |

## Support

Only the Assistant and Triage can message the Orchestrator, so Triage goes
first: `seats=triage` with `report_to=<Orchestrator ref>`. Once Triage reports
its ref, run `support` again with `report_to=<Triage ref>`. Its guard skips each
seat whose agent already runs live in the project on this machine, so a re-run
launches only the missing seats into the one Support tab. The Orchestrator
spawns the monitors on the operator's go; neither runbook launches them.

## DevTeam

Run `devteam` once per task with `report_to=<Triage ref>`. Its `admit_team` step
creates the task's branch and worktree, both `task-<seq>-<title-slug>`, under the
landing lock and refuses `team_exists` while the task has a live team tree. The
seats then open in one tab titled with the branch. The Dev Manager hands the
claim between seats, lands the approved SHA and closes the task; the close ends
the seats and removes the worktree.

A replacement execution passes `team_id`, the admitting execution id, and the
same task. After the second rejection the Dev Manager's
`EVENT=REJECTION_2 TASK=#<seq> TEAM=<team id> COMMIT=<sha> REVIEWERS=<refs>`
line reaches the Orchestrator through Triage. Relaunch with `team_id=<TEAM>`,
`fresh_reviewer=true`, `exclude_reviewers=<REVIEWERS>` and
`report_to=<Dev Manager ref>`; only a fresh code reviewer launches, under the
`fresh_reviewer_*` identity.

## Planning

The Orchestrator launches `team-planning` with `report_to=<Triage ref>`; it opens
the `Team Planning` tab, and `seat_pane` adds seats to an existing pane. The operator
launches `planning` directly:
`gobby pipelines run planning -i workspace=<name> -i seats=<names>`.

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

_Last verified: 2026-10-23_
