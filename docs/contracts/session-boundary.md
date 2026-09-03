# Session Boundary Contract

## Default Boundary

Provider compact is an in-place context boundary. The same session row, external
identity, task claims, workflow state, and terminal ownership survive. Provider
`/clear` without a staged Gobby handoff creates an independent session with no parent.

`gobby-sessions:set_handoff` is the only operation that creates a recoverable handoff
marker. It accepts:

- required nonblank `current_state`;
- at least one nonblank `next_steps` entry;
- optional nonblank `what_was_accomplished`, `key_decisions`,
  `problems_encountered`, `what_didnt_work`, `blockers`, `notes`, and `references`
  entries;
- `clear_session=false` for in-place compact or `true` for a bound clear successor.

References are deduplicated in caller order. Validation completes before state mutation.

## Persisted State

Authored payloads live in `session_handoffs`; `sessions.handoff_markdown` remains the
current rendered copy used by delivery and UI reads. Sections render in this order:

1. Current State
2. numbered Next Steps
3. optional What Was Accomplished
4. optional Key Decisions
5. optional Problems Encountered
6. optional What Didn’t Work
7. optional Blockers
8. optional Notes
9. optional References

Each content row stores the normalized field arrays, exact rendered Markdown, payload
version, authored timestamp, and SHA-256 of the exact UTF-8 Markdown. Successful
compact and clear boundaries create one immutable `session_handoff_deliveries` receipt;
authorship alone is not delivery.

Feedback is a separate `gobby-sessions:feedback` operation and is excluded from the
handoff contract. Each observation becomes one `session_feedback` row
with session, source, kind, evidence, impact, frequency, optional suggestion and
disposition, `reviewed=false`, and a UTC creation timestamp. `kind` is an enum
(`friction`, `bug`, `noise`, `surprise`, `missing-affordance`, `useful`, `other`);
`frequency` is `once`, `repeated`, or `always`; `disposition`, when present, is
`worked-around`, `filed-task`, `fixed`, `escalated`, or `noted`. `kind` `other`
requires `kind_other_label` (a short label naming the unlisted kind, rejected when
it restates a listed kind); every other kind forbids it. Empty feedback writes no
rows. Both feedback entry points use the same transactional batch writer.

An actionable Gobby defect follows the Found Work ladder. `fixed` requires a `#N`
task the observing session claimed and closed or still has claimed in progress.
`escalated` requires the active owner session reference after `send_message`.
`filed-task` is rung 3 only: the observing session created the referenced `#N` task,
it carries `needs-decision` or `clean-window`, and its description explains why
rungs 1 and 2 do not apply. Unlabeled or unclaimed filings and every other defect
disposition are shirked found work; intake validation rejects invalid ladder claims,
the stop gate blocks unclaimed filings, and the nightly digest flags them.

Bundled ask-once survey gates prompt in-scope sessions to call
`gobby-sessions:feedback` before `set_handoff` and after completed work on stop.
Daemon config `session_feedback.survey` is `gobby` (default; only exact
`projects.name == "gobby"`), `all` (every project), or `off` (prompts off).
Projects outside the Gobby repository receive gates only after an operator explicitly
selects `all`. The manual feedback tool remains callable from every repository, and
capture stays on the local machine; email and form delivery are outside this contract.
The computed flag `_gobby_feedback_survey_active` is injected per event; epoch
acknowledgment lives in `_gobby_feedback_epoch_reviewed`. Only a context reset
re-arms it — SessionStart with source `clear` or `compact`, a `resume` carrying
`pending_context_reset`, or the equivalent Grok PostCompact closeout. Task closure
is not a context boundary, so one epoch is surveyed once however many tasks it
closes.

`summary_markdown` remains the transcript-generated archival summary. It never doubles
as a live handoff.

## Staging And Compensation

Before provider dispatch, Gobby atomically stages:

- an immutable authored `session_handoffs` row;
- the replacement `handoff_markdown`;
- a pending handoff marker containing attempt identity, handoff record identity, and
  compact/clear mode;
- the clear-attempt identity marker when `clear_session=true`.

Synchronous and queued dispatch failures restore the previous handoff, delete the
matching staged content row only when it has no delivery receipt, and compare-and-clear
its markers. A newer or delivered attempt is never overwritten by stale compensation.

## Compact Path

Compact dispatch uses the provider-specific command and continues on the same session
row. The continuation prompt instructs the agent to call `get_handoff()`. Compact
SessionStart/PostCompact handling resets context-epoch tracking and consumes only the
provider compact-identity marker; it leaves the `set_handoff` marker for retrieval.
Successful dispatch records a compact delivery receipt. If that receipt write is
interrupted, `get_handoff()` retries it idempotently while consuming the marker.

Manual or automatic provider compaction without `set_handoff` has no pending marker, so
`get_handoff()` returns an empty result.

## Clear Path

Clear dispatch stages a one-shot predecessor marker before `/clear`. A matching
successor atomically consumes that marker, records the clear delivery receipt, records
direct predecessor parentage, and expires the predecessor. Live task claims then move
through expected-owner compare-and-swap. Web chat performs successor insertion in the
same transaction; terminal hooks perform the equivalent binding after SessionStart.

Manual `/clear` has no marker. Its new session is independent and receives no handoff.

## Pull-Only Recovery

`get_handoff()` accepts no lookup arguments. It checks only:

1. the caller row for an in-place compact marker;
2. the caller's direct predecessor for a clear marker.

A successful read atomically removes the pending marker and returns persisted Markdown.
Subsequent reads are empty. Missing, expired, malformed, or manually created boundaries
fail open to the same empty result. No skill tier rides the handoff: the session-start
reset empties the loaded-skills ledger and the rule gates demand each skill again at its
first use.
The persisted Markdown remains available to UI/API session reads.

No handoff content is injected through provider `additionalContext`; no bounded copy,
summary pointer, stale-tail merge, or latest-project fallback participates in delivery.
Turn-start meta skill loads (`memory`, `loading-skills`, `brevity`) wait until
`get_handoff()` consumes the pending marker so Grok first-tool briefings cannot
run those reloads ahead of the pull.

## Titles

Persisted titles are deterministic:

- provisional: `(gobby): S#<session>`;
- successful claim: `(gobby): Task #<task> - <title>`;
- `set_title(title)`: sticky manual title.

Manual titles outrank all automatic sources. Clear successors inherit a manual title;
otherwise they select the latest still-open transferred claim or their own provisional
title. Closing the current claim recomputes the same rule. Tmux and UI surfaces display
the persisted title verbatim after terminal ownership checks.

## Archival Summaries And Memory

For an expired session, the newest validated handoff with a clear delivery receipt is
the archival narrative without an LLM call. Deterministic sections append Active Task,
task-linked or transcript-explicit Commits, session-attributed Files Changed, and exact
bounded Unresolved Errors with retrieval IDs. Evidence lookup failures are recorded as
metadata omissions and do not discard the handoff. The revision is `agent_authored` and
idempotent by its immutable handoff source hash.

Staged, compact-only, malformed, imported-without-receipt, and absent handoffs retain the
full-transcript/LLM fallback. Missing transcripts may therefore still leave
`summary_markdown` empty. Rolling digest state, digest watermarks, delta summaries, and
digest-derived titles do not exist.

Shadow-memory relevance judging runs from its own background `turn_end` rule through
`gobby-memory:judge_shadow_relevance`; it is independent of archival summaries and
handoffs.
