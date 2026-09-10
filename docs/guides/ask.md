# Ask Guide

Ask answers repository questions from an immutable Git snapshot and leaves a
durable, reviewable run behind. It is appropriate when an answer needs citations
and replayable provenance rather than a best-effort conversational response.

## Start A Run

From a registered project checkout:

```bash
gcode ask "Where is session authorization enforced?"
```

The defaults are the current `HEAD`, a 600-second absolute deadline,
deterministic retrieval, and foreground waiting. Select another registered
project with the global option:

```bash
gcode --project /path/to/project ask "How are task dependencies validated?"
```

Pin a ref or opt into audited hybrid retrieval explicitly:

```bash
gcode ask "What calls the publication verifier?" \
  --commit release-candidate \
  --timeout-seconds 900 \
  --retrieval hybrid
```

Hybrid retrieval requires a healthy, identity-matched embedding and vector
configuration. It never silently falls back to deterministic retrieval.

Use `--background` when another process will inspect the run:

```bash
gcode ask "Summarize the cancellation path" --background --format json
```

Save the returned `run_id`. The same ID is used by the CLI, MCP, and HTTP APIs.

## Inspect And Recover

Read durable state without starting retrieval services locally:

```bash
gcode ask --status <RUN_ID>
gcode ask --status <RUN_ID> --format json
```

Resume an interrupted eligible run:

```bash
gcode ask --resume <RUN_ID>
```

Resume keeps the original source binding, profiles, evidence lineage, and
absolute deadline. It does not start a replacement run.

Foreground waiting is event-driven. If the client disconnects, the daemon run
continues and gcode reports `ask_wait_disconnected` with the durable ID and an
exact resume command. Do not assume a closed terminal or SSH connection cancelled
the run.

## Outcomes And Failures

A terminal answer outcome is one of:

- `complete` — the reviewed answer covers the question
- `partial` — the reviewed answer covers only part of it
- `unknown` — admitted evidence could not support an answer

All three are successful answer records and exit zero. Pipeline statuses
`failed` and `cancelled` are typed failures and exit 2 after printing the durable
record. In JSON output, inspect `status`, `current_stage`, `answer_outcome`,
`typed_error`, and `deadline_at` independently.

Cancel explicitly when the run should stop:

```bash
gcode ask --cancel <RUN_ID>
```

Cancellation stops the active native child but retains the run, source binding,
evidence, and submissions for audit.

## Export And Replay

Export is available only for a completed immutable publication and always
requires a destination:

```bash
gcode ask --export <RUN_ID> --output ./ask-exports
```

The CLI creates `ask-<RUN_ID>.tar` only after the daemon accepts the export.
Starting, waiting, status, resume, and cancel never create a local export. The
archive contains the content-addressed manifest, answer JSON, rendered Markdown,
and evidence manifest. Consumers should replay verification before trusting a
copied archive.

MCP clients can call `export_ask_run`; it verifies replay and returns the
manifest SHA-256 and authenticated HTTP download path. HTTP clients can stream
the same archive from:

```text
GET /api/ask/runs/<RUN_ID>/export?project_id=<PROJECT_ID>
```

## MCP And HTTP Clients

The public MCP surface is:

- `start_ask_run(question, commit_ref="HEAD", timeout_seconds=600,
  retrieval_mode="deterministic", idempotency_key=null)`
- `get_ask_run(run_id)`
- `wait_for_ask_run(run_id, timeout_seconds=null)`
- `resume_ask_run(run_id)`
- `cancel_ask_run(run_id)`
- `export_ask_run(run_id)`

Project identity comes from the active MCP registry and caller identity comes
from the verified session context. Do not add `project_id` to public calls.

HTTP start uses `POST /api/ask/runs` with `question`, `project_id`, and optional
`commit_ref`, `timeout_seconds`, `retrieval_mode`, and `idempotency_key` fields.
Status, wait, resume, cancel, and export use the routes documented in
[`../contracts/ask.md`](../contracts/ask.md). All calls use the existing local
daemon authentication transport.

## Evidence Expectations

Direct claims cite exact source evidence. Inferred claims cite premises and
explain the inference. Unknown claims describe the evidence limitation rather
than inventing support. Negative and exhaustive claims also record the searched
evidence scope; an empty search by itself is not proof that the repository lacks
something.

The answer is bound to the recorded commit, not the current working tree. If the
code changes afterward, start a new run against the desired ref instead of
reusing the old answer as current evidence.

## Installation Check

Before production use, verify all three runtime layers from the serving checkout:

1. The installed `gcode` binary emits contract version 10 and exposes `ask`.
2. The daemon uses the shared Ask service and the authenticated Ask routes and
   MCP registry are discoverable.
3. The database contains the synchronized `ask-investigator`, `ask-reviewer`,
   and Ask workflow definitions expected by the serving checkout.

Files under `src/gobby/install/shared/` are templates. Their presence does not
prove those definitions are installed or active; the database registry is the
runtime source of truth.
