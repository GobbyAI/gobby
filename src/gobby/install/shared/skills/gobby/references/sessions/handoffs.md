# Author and consume handoffs

Load before authoring `gobby-sessions:set_handoff` or cooperative
`gobby-agents:end_agent_run` content, and when diagnosing handoff recovery.
Fetch the applicable schemas. Load this exact reference completely before
authoring the handoff; loading the router alone does not satisfy its gate.

## Handoff, standing directives, and memory

Keep three stores separate. Write each seat's content only to the store it belongs in.

- Handoff: this seat's ephemeral work in progress, consumed once. It holds current
  state, next steps and pending replies owed, recent decisions and problems relevant
  to the next few hours, and a pointer to `standing-directives.md`, never a copy of it.
- `standing-directives.md`: a persistent seat's scratchpad in its agent workspace
  session folder, durable until each directive is done or withdrawn. It holds operator
  directives with triggers (after X lands, launch Y), holds and their release
  conditions, and operator standing rules specific to this seat. Each entry carries its
  date, source, trigger, status (waiting, fired or done) and done time.
- Memory: shared by all agents and durable. It holds project facts, observed runtime
  behavior, preferences that apply to every seat, and decision rationale written for
  future sessions to match. It never holds one seat's instructions, task progress or
  temporary state.

Gray area: an operator preference starts in `standing-directives.md` and moves to
memory only when the operator says it applies to everyone. Project-wide rules go in
`project_policy`.

A persistent seat re-reads `standing-directives.md` after every compaction and every
coordination digest, checks it on every status report, and lists its path in the
handoff's `references` as a pointer.

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
  A persistent seat lists its `standing-directives.md` path here as a pointer.
- `found_work`: findings this epoch placed on the found-work ladder, as
  `{finding, disposition, ref}` entries. `fixed` refs the `#N` task this session or
  a spawned descendant claimed or closed; `escalated` refs the active owner session
  after `send_message`; `filed-task` refs the `#N` rung-3 task this session created
  with `needs-decision`, `needs-planning`, or `clean-window`. Entries render under
  Notes. A consumed handoff with an entry not marked `fixed` arms the continuing
  session's found-work gate. A finding with no disposition belongs in the ladder,
  not the handoff.

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
file in your agent workspace folder (named in your Agent Workspace context) and add
its path to `references` before submitting. `set_handoff` already appends that folder,
so the file stays findable from the handoff. Keep immediate orientation and next
actions inline. Never keep working notes in the checkout, `.gobby/` included: an
untracked note there is an attributed edit that blocks task close.
Refresh the file's current state;
never append epoch histories or move discarded history and reflections into it.
This file holds live detail for the current epoch only. It is not
`standing-directives.md`, which persists across epochs.
Prepare notes while writes are permitted, before hard context-pressure gates block
them. If permissions prevent preserving necessary context, surface the conflict
before compaction; do not bypass permissions or silently truncate content.

For example, record "The schema lookup required a feedback submission; source
inspection let me finish diagnosis" as this epoch's resolved friction. Next epoch,
omit that observation unless it actually recurs. Carry "Integration validation is
blocked on the test service; rerun when available" in `blockers` while unresolved.
Reference an older active decision as "Keep the 10K cap; rationale in task #22309"
instead of recounting earlier epochs.

## Workspace folder and other seats

The agent workspace folder holds working artifacts only. A handoff holds continuation
state and points to files; it does not carry another seat's inputs. Anything another
seat needs goes in a message, a `send_attachment` (a document up to the attachment cap,
readable by its sender and recipients), or a task. Durable outcomes go to tasks, memory,
git, or plans.

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
consumes the pending compact or direct-predecessor clear marker. This call is
allowed and required during a restart or cutover hold: do not wait for DAEMON BACK,
because shutdown waits for the acknowledgment. Keep other held work paused. A second read,
manual compact/clear, or absent marker returns empty. Do not pass `session_id`,
search for another session's summary, or create an archival summary to recover
it. Separately, a parent or its bound clear successor may idempotently read a
child's final handoff with `get_handoff(agent_run_id=...)`.
If terminal delivery fails, use the returned attempt ID with
`get_handoff(failed_attempt_id=...)` to explicitly recover the authored payload.
That read neither delivers the handoff nor creates a receipt; clear any held
provider command before retrying `set_handoff`. A new staged attempt replaces the
failed recovery marker.

Guide: [Creating and reading handoffs](../../../../../../../../docs/guides/sessions.md#creating-and-reading-handoffs).

_Version: 1.2.0_

_Last verified: 2026-11-02_
