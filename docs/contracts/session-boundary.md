# Session Boundary Contract

## Default Boundary

Provider compact is an in-place context boundary. The same session row, external
identity, task claims, workflow state, and terminal ownership survive. Provider
`/clear` without a staged Gobby handoff creates an independent session with no parent.

`gobby-sessions:set_handoff` is the only operation that creates a recoverable handoff
marker. It accepts:

- required nonblank `current_state`;
- at least one nonblank `next_steps` entry;
- optional nonblank `key_decisions`, `blockers`, `notes`, and `references` entries;
- optional structured `gobby_feedback` observations;
- `clear_session=false` for in-place compact or `true` for a bound clear successor.

References are deduplicated in caller order. Validation completes before state mutation.

## Persisted State

Rendered Markdown lives in `sessions.handoff_markdown` with sections in this order:

1. Current State
2. numbered Next Steps
3. optional Key Decisions
4. optional Blockers
5. optional Notes
6. optional References

Feedback is excluded from Markdown. Each observation becomes one `session_feedback` row
with session, source, kind, evidence, impact, frequency, optional suggestion and
disposition, `reviewed=false`, and a UTC creation timestamp. Empty feedback writes no
rows. Both feedback entry points use the same transactional batch writer.

Bundled ask-once survey gates prompt in-scope sessions to call
`gobby-sessions:feedback` before `set_handoff` and after completed work on stop.
Daemon config `session_feedback.survey` is `all` (default), `gobby` (only
`projects.name == "gobby"`), or `off` (prompts off; the manual tool still works).
The computed flag `_gobby_feedback_survey_active` is injected per event; epoch
acknowledgment lives in `_gobby_feedback_epoch_reviewed`.

`summary_markdown` remains the transcript-generated archival summary. It never doubles
as a live handoff.

## Staging And Compensation

Before provider dispatch, Gobby atomically stages:

- the replacement `handoff_markdown`;
- feedback rows for this attempt;
- a pending handoff marker containing attempt identity and compact/clear mode;
- the clear-attempt identity marker when `clear_session=true`.

Synchronous and queued dispatch failures restore the previous handoff, delete only the
attempt's feedback rows, and compare-and-clear its markers. A newer attempt is never
overwritten by stale compensation.

## Compact Path

Compact dispatch uses the provider-specific command and continues on the same session
row. The continuation prompt instructs the agent to call `get_handoff()`. Compact
SessionStart/PostCompact handling resets context-epoch tracking and consumes only the
provider compact-identity marker; it leaves the `set_handoff` marker for retrieval.

Manual or automatic provider compaction without `set_handoff` has no pending marker, so
`get_handoff()` returns an empty result.

## Clear Path

Clear dispatch stages a one-shot predecessor marker before `/clear`. A matching
successor atomically consumes that marker, records direct predecessor parentage, and
inherits live task claims through expected-owner compare-and-swap. Web chat performs
predecessor expiry and successor insertion in one transaction; terminal hooks perform
the equivalent binding after SessionStart.

Manual `/clear` has no marker. Its new session is independent and receives no handoff.

## Pull-Only Recovery

`get_handoff()` accepts no lookup arguments. It checks only:

1. the caller row for an in-place compact marker;
2. the caller's direct predecessor for a clear marker.

A successful read atomically removes the pending marker and returns persisted Markdown
plus required and advisory resume-skill tiers. Subsequent reads are empty. Missing,
expired, malformed, or manually created boundaries fail open to the same empty result.
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

Session-end summaries read the full available transcript and append a summary revision.
Missing transcripts may leave `summary_markdown` empty. Rolling digest state, digest
watermarks, delta summaries, and digest-derived titles do not exist.

Shadow-memory relevance judging runs from its own background `turn_end` rule through
`gobby-memory:judge_shadow_relevance`; it is independent of archival summaries and
handoffs.
