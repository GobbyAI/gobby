# Rule events

Load when selecting trigger timing or diagnosing a rule that did not fire.
Inspect the installed `get_rule` event and actual normalized hook payload.
Prefer `turn_start` for portable guidance/reset, `turn_end` for stop gates,
`before_tool` for prevention, and `after_tool` for results. Use raw events only
when their particular provider timing or payload is required.

`RuleTriggerEvent` owns accepted names. Model acceptance does not mean every
provider emits an event. The guide lists lifecycle, tool/model, notification,
permission, file/worktree, and elicitation events. `session_start` includes
resume/clear/compaction re-entry; distinguish source for one-time initialization.

Raw `before_agent` resolves alongside `turn_start`; `after_agent`, `stop`, and
`stop_failure` resolve alongside `turn_end`. Do not assume once-only delivery
across separate incoming raw events. Manual compaction arms a one-shot bypass of
the next semantic turn end; raw stop rules still run. Turn start also clears the
pending bypass.

Turn end does not terminate an agent. Follow its definition and call
`gobby-agents:end_agent_run` for cooperative lifecycle completion. Durable
agent/task waits do not consume ordinary stop-attempt counts. A user interrupt
suppresses configurable turn-end block delivery while keeping non-block effects
live; it does not discard task ownership.

Recover timing errors by reproducing the normalized payload in an isolated
engine fixture and inspecting resolved events. Do not move a portable stop gate
to raw `stop` to evade its semantic boundary.

Guides: [Events](../../../../../../../../docs/guides/rules.md#events) and
[semantic boundaries](../../../../../../../../docs/guides/workflow-rules.md#author-against-semantic-turn-events).

_Last verified: 2026-09-12_
