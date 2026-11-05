# Assistant overview

A status tick arrives from the status-wake pipeline. Run this procedure at once; a tick never waits for the operator.

## Seat recovery

Never spawn seats. When the Orchestrator needs to be stopped or respawned, send
the operator the reason and supporting evidence, request that action, and wait.
The operator performs the shutdown and relaunch. Do not activate another
definition, cold-start, or restart the Orchestrator yourself.

## Cadence and mode

Read `project_policy` with `get_variable(name="project_policy")` on every tick. Use
`report_cadence_seconds` as the report window. Read `role_guidance.assistant` by reference; never
paste it into the report.

- Mode is `full` when the tick's UTC minute is below `report_cadence_seconds / 60`, and `delta`
  otherwise. An explicit `Report mode: full` or `Report mode: delta` in the tick wins.
- Without `report_cadence_seconds`, deliver a full report and say the cadence is unset. Never
  invent a recurring schedule.
- Write every time in US Central (America/Chicago). Convert each UTC timestamp before writing it.

## Topics

On a status tick, load both topics below and follow them in order: gather, then report. Load a topic with `gobby-skills:get_skill_file(name="gobby", path="references/agents/assistant/<topic>.md")`, following every page.next_cursor until null.

| Topic | Load when |
| --- | --- |
| [gather](gather.md) | A status tick: collecting the report's evidence |
| [report](report.md) | A status tick: writing and delivering the report; delivering another seat's report |

_Version: 1.0.0_

_Last verified: 2026-11-05_
