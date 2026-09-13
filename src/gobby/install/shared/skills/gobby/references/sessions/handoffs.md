# Author and consume handoffs

Load before authoring `gobby-sessions:set_handoff` or cooperative
`gobby-agents:end_agent_run` content, and when diagnosing handoff recovery.
Fetch the applicable schemas. Load this exact reference completely before
authoring the handoff; loading the router alone does not satisfy its gate.

## Shared budget and content

All sections together must fit 10,000 JSON-escaped characters, measured as
`len(json.dumps(rendered_markdown))`, including generated headings, list formatting,
escaping, and enclosing quotes. Aim below approximately 5,000 encoded characters
for ordinary handoffs to leave headroom; this is a recommendation, not a second cap.
Use readable sentences and ordinary technical terms. Remove low-value detail instead
of compressing prose into shorthand or invented abbreviations.

Derive continuation state from the implementation tracker and write fresh reflections
from the current context epoch. Do not copy previous handoffs, logs, or completed ledgers.

- `current_state`: current work and relevant validation status.
- `next_steps`: concrete immediate actions, including unfinished validation.
- `key_decisions`: active decisions and constraints; reference durable rationale in
  the owning task, design document, or Gobby memory, even when decided earlier.
- `blockers`: unresolved obstacles and the action or coordination needed.
- `what_was_accomplished`: meaningful outcomes from this epoch, briefly.
- `problems_encountered` and `what_didnt_work`: fresh friction observations, including
  resolved friction. Describe the attempt, obstacle, and consequence or workaround.
  No general lesson or improvement proposal is required.
- `notes`: other necessary live working context.
- `references`: sources needed to locate durable decisions, evidence, or working notes.

Never copy earlier reflections into a new handoff: this inflates apparent recurrence
for future daily friction synthesis. Record a new occurrence only when friction
actually recurs. An unresolved blocker may carry forward in continuation state
without repeating its historical narrative. Existing Gobby memory guidance still
applies; useful epoch observations do not require a memory write. A coordination
wait is not an acceptance result.

Use nonblank `current_state` and at least one nonblank `next_steps` entry.
Leave optional fields empty when unneeded; supplied entries must be nonblank.
References deduplicate in caller order.

## Necessary live detail

Prune cumulative history and superseded details first. If necessary live working
detail still cannot fit, create or update a session-scoped Markdown working-context
file and add its project-relative path to `references` before submitting. Keep
immediate orientation and next actions inline. Refresh the file's current state;
never append epoch histories or move discarded history and reflections into it.
Prepare notes while writes are permitted, before hard context-pressure gates block
them. If permissions prevent preserving necessary context, surface the conflict
before compaction; do not bypass permissions or silently truncate content.

For example, record "The schema lookup required a feedback submission; source
inspection let me finish diagnosis" as this epoch's resolved friction. Next epoch,
omit that observation unless it actually recurs. Carry "Integration validation is
blocked on the test service; rerun when available" in `blockers` while unresolved.
Reference an older active decision as "Keep the 10K cap; rationale in task #22309"
instead of recounting earlier epochs.

## Submit and continue

When the configured feedback survey applies, submit the epoch's feedback first.
If there is nothing to report, use `feedback(observations=[])`. Do not submit a
duplicate acknowledgment for the same epoch or call it human-reviewed. Actionable
defects follow the found-work ladder; feedback is not a substitute for fixing or
handing work to its active owner. Interactive sessions call `set_handoff` last.
Spawned agents submit feedback first and call `end_agent_run` as their last call.

Use `clear_session=false` during planning, review, or ongoing task work. Use
`true` only for a root/coordinator moving to another task after closing the
current task. A spawned worker finishes with structured `end_agent_run`, including
the coordinator's next actions and closed task refs in `references`; raw turn-end
does not end an agent run. An exit outside the exit step while the bound task is
open requires `blockers` and reports `incomplete` to the parent.

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
