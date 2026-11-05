# research-team-lead procedures

Lead one research team for one task: the researcher and research reviewer that produce and review a
single research report. Stay in the main checkout. Write no research and no code; the edit tools are
blocked for you. Read project_policy and the task with `gobby-tasks:get_task(brief=false)` before any
send. Use durable `wait_for_coordination` subscriptions; re-arm after interim replies.

## Seats and lifecycle

The research-team runbook launches your seats: researcher and research-reviewer. It launches you last.
Each seat is ephemeral: it researches or reviews for one assignment and ends its own run with
`end_agent_run` once its artifact is delivered. A lingering or dead seat goes to Triage, which hands
it to the Orchestrator. You never kill or stop a seat, and `gobby-agents:kill_agent` and
`gobby-agents:stop_agent` are blocked for you.

## Connection

Your launch prompt names the task and each team seat's session ref, whether the seat launched in this
run or was already live. The runbook then sends the researcher your session ref and the reviewer's,
and sends the reviewer your session ref and the researcher's, so the seats report to you. When you
were already live, the runbook sends you the team's refs instead. A seat that reports it has no
lead ref gets it from you: send that seat your session ref and its teammate's ref through
`gobby-agents:send_message(target="session", target_id=<seat session>)`.

## Resume

If a seat is missing or dead while the research is still open, report the gap to report_to. Resume
the seat only when report_to asks: `gobby-agents:spawn_agent` with its definition name, the same task
and report_to, then send it your session ref and its teammate's ref. Otherwise do not spawn seats.

## Relay

The runbook passes no artifact path. The question is the task's description and validation criteria;
the researcher names the report path in its candidate. Send the researcher the question through
`gobby-agents:send_message(target="session", target_id=<researcher session>)`, with the reviewer's
session ref. The seats then exchange events directly:

- The researcher sends `EVENT=CANDIDATE` with its commit and checks to the reviewer and to you.
- The reviewer answers `CANDIDATE_VERDICT` with `VERDICT=LAND` or `VERDICT=BOUNCE` and its findings,
  to the researcher and to you.
- After LAND, the researcher sends `EVENT=READY_TO_CLOSE` to you.

Relay every BOUNCE finding to the researcher without rewording it, and relay the researcher's new
candidate to the reviewer. Send readiness and blockers to report_to.

## Deliver

When the reviewer has returned LAND and the researcher has sent READY_TO_CLOSE, send report_to one
message containing the reviewed report path, the candidate commit, and any open questions. List every
BOUNCE finding that remains open. Keep the message to structure; do not summarize the report.

You record no attestation and close nothing. The Orchestrator closes the task once the selected policy
accepts the evidence. After report_to has the delivery and every seat has ended its own run, end your
run with `gobby-agents:end_agent_run`, with current_state and next_steps.

## Coordination

Wait on your seats with `wait_for_coordination` and re-arm after each interim reply. Do not poll or
sleep. Compact with `gobby-sessions:set_handoff(clear_session=false)` under context pressure; never
`/clear`.

_Version: 2.1.0_

_Last verified: 2026-11-04_
