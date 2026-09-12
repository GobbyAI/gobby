# Operate memory dream

Load before a requested memory hygiene run or when diagnosing its outcome.
Discover `gobby-memory:memory_dream`, `memory_dream_status`,
`memory_dream_decisions`, and `memory_dream_revert` through schemas. Inspect the
intended scope and configuration first. Scheduled enablement is installed state,
not proven by bundled defaults.

`memory_dream` starts asynchronous work and returns a run ID. `dry_run` defaults
false; use true for a report-only memory-action preview. `full_sweep` broadens
selection and `skip_consolidation` changes processing. Current-project context
starts a scoped run; absent context admits all due project scopes, each with its
own truth digest. Do not launch another run merely because the first is ongoing.
Equivalent active work can coalesce; a conflicting active run can be rejected.

Read `memory_dream_status` for the durable checkpoint, terminal outcome, errors,
and publication summary. Use `memory_dream_decisions` to inspect proposed versus
effective actions, snapshots, and outcomes; follow its offset/limit pagination to
completion when the review requires all decisions. A proposal is not an applied
mutation, and a dry run still records run diagnostics.

There is no dedicated Dream MCP wait primitive. Preserve the agent event-driven
wait contract: use a
status read for bounded diagnostics and do independent work or yield rather than
run an unbounded polling loop. Do not invent a wait tool or promise an automatic
wake without a registered event. Operator CLI `gobby memory dream` watches the
run; `--timeout` bounds the client wait only. Stopping that observer leaves the
daemon run active; `gobby memory dream status RUN_ID` reads its current checkpoint.

Before `memory_dream_revert`, inspect the run and snapshot availability. Revert
uses durable snapshots and reconciles secondary stores according to configuration;
it is not a substitute for a full backup. History retention limits recovery.
Read `conflicts` and `secondary_sync_failures` even on success. Conflicted
action-owned columns can remain unchanged; snapshot failures return
`revert_failed`, while forfeited snapshots reject revert. Do not claim every
memory was restored from a successful envelope alone.
For dependency failure, inspect the checkpoint and restore the missing service;
for active-run conflict, resolve the existing run before retrying. Coordinate
daemon stop/restart with protected cron work; do not interrupt nightly maintenance
just to shorten an observation wait.

Guide: [Dream operations](../../../../../../../../docs/guides/memory.md#dream-operations)
and [Configuration](../../../../../../../../docs/guides/memory.md#configuration).

_Last verified: 2026-09-12_
