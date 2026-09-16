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

References are deduplicated in caller order. Rendered content is limited to 10,000
JSON-escaped characters, including formatting; oversized handoffs are rejected with
shorten-and-retry guidance before state mutation. Required feedback must be submitted
separately before calling `set_handoff` last.

Planning, review, and ongoing task work use `clear_session=false`. The
`clear_session=true` boundary is reserved for a root or coordinator that has closed the
current task and is moving to another task or epic child. Before-tool rules enforce the
compact boundary for autonomous sessions and whenever resolved `plan_mode` is true.

## Handoff Content And Incidental History

A handoff records current continuation state plus fresh reflections from the ending
context epoch. Current state, next steps, active decisions, blockers, and relevant
validation orient the successor. Carry forward earlier constraints only while they
affect the work; reference their authoritative task, design document, or Gobby memory.

What Was Accomplished records meaningful outcomes from this epoch. Problems
Encountered and What Didn't Work preserve concrete friction, including resolved
friction: the attempt, obstacle, and consequence or workaround. Agents need not infer
a general lesson. These observations support future daily friction synthesis.
Never copy earlier reflections into a new handoff: that inflates apparent recurrence.
Record another occurrence only when friction actually recurs. An unresolved blocker
may remain in continuation state without repeating its historical narrative. Existing
memory guidance applies independently; an observation need not become a memory.

Omit superseded intermediate test counts and historical run labels from the handoff
narrative unless needed to explain an active blocker, next action, or fresh friction
observation. Keep actionable failure diagnostics and distinguish completed
validation from acceptance work that is still required. A coordination wait is not
evidence that acceptance ran or passed.

All sections together must fit 10,000 JSON-escaped characters, measured as
`len(json.dumps(rendered_markdown))`, including generated formatting, escaping, and
enclosing quotes. Aim below approximately 5,000 encoded characters for ordinary
handoffs to leave headroom. Use readable sentences; remove low-value detail instead
of inventing shorthand. Leave optional fields empty when unneeded.

After pruning, necessary live detail may go in a session-scoped Markdown
working-context file. Create or update it while writes are permitted and include
its project-relative path in `references`; keep immediate orientation and next actions
inline. Refresh current state in the file; never append epoch histories or move
discarded history and reflections into it. Prepare it before hard context-pressure
gates block writes; surface preservation conflicts before compaction rather than
bypassing permissions or truncating content. Existing task/session records and
transcripts retain history. This policy does not delete or rewrite historical records.

Task #21887 records the inclusion decision accepted on 2026-09-05. Its generated
recovery/combination examples and "Source Records" terminology predate the current
persisted-Markdown delivery contract. Today, `get_handoff()` returns authored content;
it does not regenerate or merge summaries. The policy guides authors and bounded
review, not a runtime telemetry filter or a guarantee of automatic semantic checking.
The historical cases and their expected treatment are documented in
[Creating And Reading Handoffs](../guides/sessions.md#creating-and-reading-handoffs).

No additional model calls or whole-session replays are required by this policy. An
audit-grade event ledger, source-position ordering, run-type classification, and
semantic verification are outside this contract. Generated archival-summary behavior
is unchanged.

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

Feedback enters through `gobby-sessions:feedback`.
Each observation becomes one `session_feedback` row
with session, source, kind, evidence, impact, frequency, optional suggestion and
disposition, `reviewed=false`, and a UTC creation timestamp. `kind` is an enum
(`friction`, `bug`, `noise`, `surprise`, `missing-affordance`, `useful`, `other`);
`frequency` is `once`, `repeated`, or `always`; `disposition`, when present, is
`worked-around`, `filed-task`, `fixed`, `escalated`, or `noted`. `kind` `other`
requires `kind_other_label` (a short label naming the unlisted kind, rejected when
it restates a listed kind); every other kind forbids it. Empty feedback writes no
rows. A successful submission satisfies the epoch gate; it does not mark any
observation human-reviewed.
`source` must name a Gobby surface as `gobby-<server>:<tool>`;
`<surface>:<name>` where surface is `rule`, `hook`, `skill`, `workflow`, `agent`,
`pipeline`, `prompt`, `cli`, `binary`, `daemon`, `ui`, `docs`, or `config`; or a
repository path starting with `src/gobby/`, `crates/`, `web/src/`, or `docs/`.
Existing rows are unchanged.

An actionable Gobby defect follows the Found Work ladder. `fixed` requires a `#N`
task the observing session claimed and closed or still has claimed in progress.
`escalated` requires the active owner session reference after `send_message`.
`filed-task` is rung 3 only: the observing session created the referenced `#N` task,
it carries `needs-decision`, `needs-planning`, or `clean-window`, and its description explains why
rungs 1 and 2 do not apply. Unlabeled or unclaimed filings and every other defect
disposition are shirked found work; intake validation rejects invalid ladder claims,
the stop gate blocks unclaimed filings, and the nightly digest flags them.

The stop gate prompts unanswered in-scope sessions after completed work: a queued
task closure that no successful submission has covered. Submission alone satisfies
the stop gate; it never calls for a handoff. `set_handoff` separately checks
successful survey submission before staging, so a session that is handing off
submits feedback first, then calls `set_handoff` last. The context-pressure gate
permits feedback and its schema discovery. Merely receiving a survey prompt does not
satisfy the gate.
Daemon config `session_feedback.survey` is `gobby` (default; only exact
`projects.name == "gobby"`), `all` (every project), or `off` (prompts off).
Projects outside the Gobby repository receive gates only after an operator explicitly
selects `all`. The manual feedback tool remains callable from every repository, and
capture stays on the local machine; email and form delivery are outside this contract.
The computed flag `_gobby_feedback_survey_active` is injected per event; epoch
acknowledgment lives in `_gobby_feedback_epoch_submitted`. Only a context reset
re-arms it — SessionStart with source `clear` or `compact` (Grok PostCompact
evaluates those same `session_start(compact)` rules), or a `resume` carrying
`pending_context_reset`. Task closure
is not a context boundary, so one epoch is surveyed once however many tasks it
closes. A successful submission also records the pending closure identities in
`_gobby_feedback_surveyed_closures`, which context resets keep. A re-armed epoch whose
pending closures were all covered is not surveyed again, so a handoff compaction
cannot re-trigger the stop gate; a closure queued after the submission is surveyed
in a later epoch. The re-armed epoch flag still gates `set_handoff`.

Context-pressure enforcement reads live `context_handoff.*` config. Windows
strictly below `small_window_tokens` use `small_window_warn_ratio` and
`small_window_block_ratio`; windows at or above that cutoff and below
`extended_window_tokens`, plus unknown or invalid windows, use `warn_tokens` and
`block_tokens`; windows at or above the extended cutoff use `extended_warn_tokens`
and `extended_block_tokens`. Warnings repeat every turn and every
`warn_every_tool_calls` tool calls. Plan mode and pipelines are exempt.
At block pressure, handoff prerequisites and schema discovery remain callable. A
non-retryable missing terminal compaction path caps the epoch at warning pressure;
a background delivery failure remains blocked until `set_handoff` is retried.

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
For terminal sessions, `set_handoff` returns a staged-success result before any provider
input is sent. The normalized successful `after_tool` event persists the old-epoch tool
gate, atomically claims the attempt, and schedules one background delivery. Duplicate,
failed, and malformed completion events cannot claim an attempt. A delivery failure
restores the staged state and exposes guidance to retry `set_handoff`. Web-chat
compaction and clear remain synchronous because they do not replace a terminal composer.

## Compact Path

Before compact staging, `set_handoff` validates bounded handoff content and checks
the separate feedback submission. A staging failure can be retried without
resubmitting feedback. Compact dispatch interrupts the provider, clears its composer, submits `/compact` for
Claude, Codex, and Grok or `/compress` for Qwen and Droid, and continues on the same
session row. The continuation prompt instructs the agent to call `get_handoff()`. Compact
SessionStart handling — including Grok PostCompact, which evaluates the
`session_start(compact)` rules — resets context-epoch tracking and consumes only the
provider compact-identity marker; it leaves the `set_handoff` marker for retrieval.
Successful dispatch records a compact delivery receipt. If that receipt write is
interrupted, `get_handoff()` retries it idempotently while consuming the marker.

Manual or automatic provider compaction without `set_handoff` has no pending marker, so
`get_handoff()` returns an empty result.

## Clear Path

Clear dispatch stages a one-shot predecessor marker before the post-result worker
interrupts the provider, clears its composer, and submits `/clear`. A matching
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
reset empties the loaded-skills and completed-reference ledgers and the rule gates demand each skill again at its
first use.
The persisted Markdown remains available to UI/API session reads.

No handoff content is injected through provider `additionalContext`; no bounded copy,
summary pointer, stale-tail merge, or latest-project fallback participates in delivery.
Turn-start bootstrap loads (`gobby:references/skills/loading.md`,
`gobby:references/memory/overview.md`, `brevity`, `restraint`) wait until
`get_handoff()` consumes the pending marker so Grok first-tool briefings cannot
run those reloads ahead of the pull.

## Titles

Persisted titles are deterministic:

- provisional: `<project>#<session>: <Provider>`;
- successful claim: `<project>#<session>: Task #<task> - <title>`;
- `set_title(title)`: sticky manual title.

Manual titles outrank all automatic sources. Clear successors inherit a manual title;
otherwise they select the latest still-open transferred claim or their own provisional
title. Closing the current claim recomputes the same rule. Tmux displays the persisted automatic title after terminal ownership checks.
Terminal and Sessions activity panels suppress the provisional provider suffix
while preserving task titles, per the completed #22163 presentation contract.

## Archival Summaries And Memory

For an expired session, the newest validated handoff with a clear delivery receipt is
the archival narrative without an LLM call. Deterministic sections append Active Task,
task-linked or transcript-explicit Commits, session-attributed Files Changed, and exact
bounded Unresolved Errors with retrieval IDs. Evidence lookup failures leave that section
empty and do not discard the handoff. The summary is `agent_authored`; regeneration is a
no-op only while the current summary still matches the handoff's immutable source hash
and rendered markdown.

Staged, compact-only, malformed, imported-without-receipt, and absent handoffs retain the
full-transcript/LLM fallback. Missing transcripts may therefore still leave
`summary_markdown` empty. Rolling digest state, digest watermarks, delta summaries, and
digest-derived titles do not exist.

Shadow-memory relevance judging runs from its own background `turn_end` rule through
`gobby-memory:judge_shadow_relevance`; it is independent of archival summaries and
handoffs.
