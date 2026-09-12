# Author and consume handoffs

Load before authoring `gobby-sessions:set_handoff` or cooperative
`gobby-agents:end_agent_run` content, and when diagnosing handoff recovery.
Fetch the applicable schemas. Existing installed skill gates remain authoritative
until migration; load `handoff-discipline` if the active gate requires it.

Derive readable current state and concrete next actions from the implementation
tracker. Include decisions, blockers, commands, diagnostics, paths, impact, and
references only where needed for continuation. Write from the current context
epoch. Reference earlier evidence rather than pasting cumulative history,
previous handoffs, logs, or completed ledgers. Preserve active constraints and
unfinished validation; a coordination wait is not an acceptance result.

Use nonblank `current_state` and at least one nonblank `next_steps` entry.
Optional section entries must be nonblank. References deduplicate in caller
order. The rendered payload, including formatting, must fit 10,000 JSON-escaped
characters. Shorten excess history rather than compressing prose into shorthand.
An optional Markdown progress log can hold detail; reference its path.

When the configured feedback survey applies, submit the epoch's feedback first.
If there is nothing to report, use `feedback(observations=[])`. Do not submit a
duplicate acknowledgment for the same epoch or call it human-reviewed. Actionable
defects follow the found-work ladder; feedback is not a substitute for fixing or
handing work to its active owner. Call `set_handoff` last.

Use `clear_session=false` during planning, review, or ongoing task work. Use
`true` only for a root/coordinator moving to another task after closing the
current task. A spawned worker finishes with structured `end_agent_run`, including
the coordinator's next actions; raw turn-end does not end an agent run.

After a terminal result reports `handoff_staged` and `delivery_pending`, yield.
Delivery starts after successful tool completion; staged success is not proof
the provider boundary finished. A failed delivery restores the previous state
and provides retry guidance. Retry `set_handoff` after correcting that failure;
a staging retry does not require duplicate feedback.

On continuation, call `get_handoff()` without lookup arguments. It atomically
consumes the pending compact or direct-predecessor clear marker. A second read,
manual compact/clear, or absent marker returns empty. Do not pass `session_id`,
search for another session's summary, or create an archival summary to recover
it. Separately, a parent or its bound clear successor may idempotently read a
child's final handoff with `get_handoff(agent_run_id=...)`.

Guide: [Creating and reading handoffs](../../../../../../../../docs/guides/sessions.md#creating-and-reading-handoffs).

_Last verified: 2026-09-12_
