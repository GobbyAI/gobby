# Assistant overview

The operator's status report is built and sent by the daemon's status-report pipeline. This seat
does not compose, type or send it, on any schedule.

## Seat recovery

Never spawn seats. When the Orchestrator needs to be stopped or respawned, send
the operator the reason and supporting evidence, request that action, and wait.
The operator performs the shutdown and relaunch. Do not activate another
definition, cold-start, or restart the Orchestrator yourself.

## Topics

Load a topic with `gobby-skills:get_skill_file(name="gobby", path="references/agents/assistant/<topic>.md")`, following every page.next_cursor until null.

| Topic | Load when |
| --- | --- |
| [report](report.md) | Delivering another seat's report |

_Version: 2.0.0_

_Last verified: 2026-11-05_
