# Coordinating and validating builds

Load only when the user assigns this session the coordinator role, or when
validating/debugging unattended automation. Reading this topic does not launch
a separate coordinator agent or create a new epic.

For an explicitly assigned build-coordination run, keep product work and
coordination work separate: a target tree and a claimed coordination epic
outside it. Use an existing coordination epic when resuming. Load task and
source-control procedures. Inspect target dependencies/manifests and preserve
the requested agent/provider, stage route, isolation, scope, and criteria.
Implementation leaves normally use `development`; change stages only through
supported lifecycle operations and applicable authorization.

Launch once with the coordinator session identified and without quick mode for
unattended validation. Check project automation state before judging a stalled
run. Observe daemon-owned dispatch; repeated launch/tick calls change the test
and can hide a broken heartbeat. Keep test targets separate from tracking work,
and use an isolated test daemon/state for mutation probes.

Each coordinator iteration checks target status, dispatch explanation, stages,
active agents, history, workspace health, and coordination blockers. Fix the
highest-priority actionable automation issue while product agents own their
work. Intervention is evidence of an automation gap, not successful unattended
operation. Keep found work in the current tracker unless an authorized task
split or the repository's edge-case found-work policy applies. Coordinate with
active owners without editing their uncommitted files.

When no actionable work remains, subscribe once to a running agent with
`gobby-agents:wait_for_agent` and yield. Retrieve the terminal snapshot on wake,
then do a full status/health sweep. Use structured session handoff with
`clear_session=false` before context degrades and after a coordination fix before
the next loop/wait. Do not close, unclaim, detach, or reparent unfinished work
to satisfy a stop hook.

Completion requires the target's real delivery/merge evidence, all required
reviews/tests, no running build agents or accidental claims, closed automation
bugs, and workspace cleanup (or an explicit explanation for preserved dirty or
conflicted work). A daemon fix must be running before its path is released;
follow recovery's restart coordination. Do not run the full pytest suite without
an explicit request. Leave escalation only for a real user decision after
practical fixes have been exhausted.

For terminal/sandbox validation record worker OS/provider, run/session IDs,
checkout/commit, generated policy and canonical run temp path, exact commands,
pass/fail/skip counts, and fixture process sets before/after. Required spawned
agent/platform evidence cannot be replaced by a parent-shell pass. Unix-socket
skips/denials remain unvalidated. Managed macOS SRT socket grants are limited
to the current canonical run temp directory with `allowAllUnixSockets=false`;
preserve separate operator grants. Linux/WSL2 keep socket restrictions. Verify
bind/listen/connect and denial boundaries when changing policy; web-chat grants
are separate. Consult the [terminal guide](../../../../../../../../docs/guides/gterminal-development-guide.md#sandboxed-validation-by-operating-system).

Guide: [Agent work](../../../../../../../../docs/guides/orchestration.md#agent-work).

_Last verified: 2026-09-12_
