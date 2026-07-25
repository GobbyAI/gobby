# Daemon `INFO` Level Audit — 2026-07-24

## Policy

- `INFO`: lifecycle transitions, durable state changes, actual job execution,
  and nonzero recovery summaries.
- `DEBUG`: per-item success, no-ops, file-write acknowledgements, prompt sizing,
  polling, and progress details.
- `WARNING` and `ERROR`: unexpected degradation and operational failures.

Pipeline execution is automation. Its records belong to `automation.log`, even
when their level remains `INFO`.

## Direct `AFTER_TOOL` Ownership

Provider-native `PostToolUse` owns interactive completion for Codex, Claude,
Qwen, Droid, Grok, and AGY sessions. Direct MCP execution synthesizes
`AFTER_TOOL` only for pipeline and unknown-source sessions. Direct
`BEFORE_TOOL` enforcement remains synchronous for every source.

Codex transcript-derived `exec_command` verification receipts remain a separate
evidence path; they do not recreate generic MCP lifecycle events.

## Inventory

The audit snapshot is commit `4710917498f2456952f5fed381fcd8cfdbae854b`.
An AST inventory found 490 production `.info(...)` calls under `src/gobby`
that were routed to the daemon surface.

All 490 daemon-routed sites were reviewed against the policy. Sites retain
`INFO` unless listed in the demotion or routing exceptions below. This makes
the classification exhaustive while keeping the audit stable across line-number
changes.

| Classification | Sites |
| --- | ---: |
| Kept at daemon `INFO` | 470 |
| Demoted to `DEBUG` | 9 |
| Split by outcome between `INFO` and `DEBUG` | 1 |
| Rerouted to automation | 10 |
| **Total** | **490** |

The outcome-split site is the group-message responder skip. Authorization
denials remain `INFO`; the expected `mention_required` branch emits `DEBUG`.

## Volume-Weighted Follow-up

The original site-count inventory understated operational impact. In the
07:00–08:59 follow-up sample, `DoneEvent` usage telemetry and expected
`mention_required` skips produced 46 of 89 daemon `INFO` records: 52% after
rounding. Both are per-turn or expected-policy diagnostics, so both move to
`DEBUG`.

The follow-up confirmed routing was already correct. Websocket,
communications, session, storage, workflow, and agent records stay on the
daemon surface. Valid warnings continue to aggregate into `errors.log`.

## Demoted Sites

| File | Baseline message | Reason |
| --- | --- | --- |
| `src/gobby/sessions/processor_transcripts.py` | `Codex transcript verification receipt acknowledged` | Per-item success; replaced by one batch `DEBUG` record. |
| `src/gobby/sessions/processor_transcripts.py` | `Derived Codex transcript verification outcomes` | Batch progress duplicated receipt ingestion. |
| `src/gobby/tasks/validation.py` | `Validation prompt assembled ...` | Prompt-size diagnostic. Validation start remains `INFO`. |
| `src/gobby/sessions/session_wiki_file.py` | `Session wiki page written ...` | File-write acknowledgement. |
| `src/gobby/workflows/summary_actions.py` | `Session summary written` | File-write acknowledgement. |
| `src/gobby/servers/websocket/chat/_stream_events.py` | `DoneEvent context_window=...` | Per-turn usage telemetry. |
| `src/gobby/communications/inbound.py` | `Ignoring inbound message rejected by access policy ...` | Expected access-policy decision. |
| `src/gobby/storage/session_lifecycle.py` | `Skipped pruning ... retained references` | Protected-retention no-op. Nonzero pruning remains `INFO`. |
| `src/gobby/search/backends/embedding.py` | `Embedding index built ...` | Generic backend completion. Semantic `Skill search index built ...` readiness remains `INFO`. |

## Outcome-Split Site

| File | Baseline message | `DEBUG` outcome | Retained `INFO` outcomes |
| --- | --- | --- | --- |
| `src/gobby/communications/responder.py` | `Ignoring group message for conversation ...` | `mention_required` | Authorization and group-policy denials |

The session summarizer's existing generation site remains in the kept count,
with narrower semantics: actual full, delta, or deterministic-fallback
generation emits `INFO` with mode, reason, and output length; a source-hash
no-op emits `DEBUG`. The workflow summary action always performs generation,
so its `INFO` record now includes `reason=workflow_action` and output length.

## Rerouted Sites

The ten `INFO` sites in these modules move from the daemon surface to automation
through the `gobby.workflows.pipeline` and
`gobby.workflows.pipeline_executor` namespaces:

- `src/gobby/workflows/pipeline/handlers.py`: 2
- `src/gobby/workflows/pipeline_executor.py`: 6
- `src/gobby/workflows/pipeline_executor_steps.py`: 2

`gobby.workflows.pipeline_heartbeat` remains an explicit automation namespace.

## Retained Examples

The original retained 475 include task claims and auto-links, collaboration-mode
changes, workflow step transitions, actual validation and summary execution,
daemon and subsystem lifecycle, durable configuration/storage mutations,
agent state changes, and nonzero cleanup or recovery summaries.

After the follow-up corrections, the retained 470 still include those same
categories. Summary generation remains `INFO`. Two
`missing_summary_metadata` generations for one session within 27 seconds
exposed duplicate work; task #18893 tracks per-session generation coalescing.

Managed tmux termination now returns immediately after a successful managed
kill. This removes the stale post-kill PID identity warning at its source.
Direct-PID fallback still verifies process identity and warns for a live
mismatch.
