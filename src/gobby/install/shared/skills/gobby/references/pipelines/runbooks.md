# Operate runbooks

Load when launching, relaunching, recovering or stopping a runbook: a pipeline
tagged `runbook` whose steps place agent seats. Find one with
`gobby-workflows:list_pipelines(tag="runbook")` or
`gobby pipelines list --tag runbook`.

Launch only from an operator surface (CLI, web, gclient) or an authorized cron
`pipeline` job. Agent seats are blocked from `run_pipeline`, the exposed
`pipeline:<name>` tools and a shell `gobby pipelines run`. Pass every
project-specific value as an input. The bundled `planning` runbook takes
`workspace` and a `seats` string such as `writer,enhancer,adversary`:
`gobby pipelines run planning -i workspace=<name> -i seats=<names>`.

The first step, `gobby-agents:check_runbook_seats(workspace, requested,
catalogue)`, refuses a bad seat name, another live execution of the same
runbook in the same project, workspace and machine, a missing or disabled seat
agent definition, and any failed or truncated lookup. It checks no capacity and
no title. Each later seat step is one placed `spawn_agent` call with
`reserved_run_id: ${{ invocation_id }}`; its output carries `run_id` and
`pane_ref`. Seats are identified by `project#session_ref`, and titles never
refuse a launch. Do not call `wait_for_agent` on a standing seat.

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

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#runbooks).

_Last verified: 2026-10-04_
