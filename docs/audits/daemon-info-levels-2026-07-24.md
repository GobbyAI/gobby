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
| Kept at daemon `INFO` | 475 |
| Demoted to `DEBUG` | 5 |
| Rerouted to automation | 10 |
| **Total** | **490** |

## Demoted Sites

| File | Baseline message | Reason |
| --- | --- | --- |
| `src/gobby/sessions/processor_transcripts.py` | `Codex transcript verification receipt acknowledged` | Per-item success; replaced by one batch `DEBUG` record. |
| `src/gobby/sessions/processor_transcripts.py` | `Derived Codex transcript verification outcomes` | Batch progress duplicated receipt ingestion. |
| `src/gobby/tasks/validation.py` | `Validation prompt assembled ...` | Prompt-size diagnostic. Validation start remains `INFO`. |
| `src/gobby/sessions/session_wiki_file.py` | `Session wiki page written ...` | File-write acknowledgement. |
| `src/gobby/workflows/summary_actions.py` | `Session summary written` | File-write acknowledgement. |

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

The retained 475 include task claims and auto-links, collaboration-mode
changes, workflow step transitions, actual validation and summary execution,
daemon and subsystem lifecycle, durable configuration/storage mutations,
agent state changes, and nonzero cleanup or recovery summaries.
