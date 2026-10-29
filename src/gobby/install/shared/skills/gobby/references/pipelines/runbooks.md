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
project-specific value as an input, plus the runbook's entry under the project
policy's `launch_defaults`.

Cold start is separate from runbooks. Bare commands declared in an installed
definition's `cold_start_commands` activate it and reuse or open its declared
`cold_start_counterpart`. The bundled examples use `admin` and `assistant`.

| Runbook | Seats | Required inputs |
| --- | --- | --- |
| `support` | release-manager, triage, archivist, recovery-steward, consolidator | `workspace`, `project_path`, `report_to` |
| `devteam` | test-writer, developer, code-reviewer, dev-manager | `task`, `workspace`, `report_to` |
| `team-planning` | plan-writer, plan-adversary, plan-enhancer | `task`, `workspace`, `project_path`, `report_to` |

## Support

The bundled recipient policy requires `message_restricted_seats`, which the
example Triage definition declares. Launch it first: `seats=triage` with
`report_to=<Orchestrator ref>`. Once Triage reports
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

For an epic, the four primary seats persist until the epic closes. Its bound Dev
Manager may launch `devteam` for an open parallel leaf with
`parent_team_id=<epic team UUID>` and `report_to=<Dev Manager ref>`. This creates a
single-task Dev Pod with its own Test Writer, Developer and Code Reviewer on a
sub-branch of epic main, in `Dev Pod - #<leaf>`. Do not pass the epic as `team_id`:
that joins the primary team instead. Single-task teams cannot launch pods.

Every leaf follows Dev Manager, Test Writer, Code Reviewer, Developer, Code
Reviewer, Dev Manager, using the existing single-task review rounds. A pod's
fresh reviewer joins its own `team_id`; a primary leaf's joins the epic team.
The Dev Manager lands a leaf on epic main and closes it immediately. The daemon
ends that pod's seats and removes its worktree; the epic's primary seats persist.
The epic tip lands on the integration branch before the epic itself closes.

## Planning

The Orchestrator launches `team-planning` with `task` and `report_to=<Triage ref>`; it opens
the `Team Planning - #<task>` tab, and `seat_pane` adds seats to an existing pane. The operator
launches `team-planning` directly:
`gobby pipelines run team-planning -i workspace=<name> -i task=<#N> -i project_path=<path> -i report_to=<ref> -i seats=<names>`.

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
