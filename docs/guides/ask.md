# Ask Guide

Ask answers repository questions from the caller’s existing code index and leaves a
durable, reviewable run behind. It is appropriate when an answer needs citations
and replayable provenance rather than a best-effort conversational response.

## Start A Run

From a registered project checkout:

```bash
gcode ask "Where is session authorization enforced?"
```

The defaults are the current checkout’s live index, a 600-second absolute deadline,
deterministic retrieval, and foreground waiting. Select another registered
project with the global option:

```bash
gcode --project /path/to/project ask "How are task dependencies validated?"
```

Opt into audited hybrid retrieval explicitly:

```bash
gcode ask "What calls the publication verifier?" \
  --timeout-seconds 900 \
  --retrieval hybrid
```

To ask about a historical commit, create and register a worktree at that commit,
index it once with ordinary `gcode index`, then run Ask from that worktree. Ask
has no `--commit` flag and does not create worktrees or rebuild indexes.

Hybrid retrieval requires a healthy, identity-matched embedding and vector
configuration. It never silently falls back to deterministic retrieval.

Use `--background` when another process will inspect the run:

```bash
gcode ask "Summarize the cancellation path" --background --format json
```

Foreground calls print the final cited Markdown. Add `--format json` for the
structured answer, run metadata, and provenance. Default calls create no output
bundles. Save the returned `run_id` for background calls. The same ID is used by the CLI, MCP, and HTTP APIs.

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
the run. Check status first after a disconnect: if it is still running, wait
through `wait_for_ask_run` rather than repeatedly resuming it. Resume rejects
already-running, completed, cancelled, and expired runs; its printed recovery
command is applicable only after the run becomes eligible for recovery.

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

- `start_ask_run(question, project_path=null, timeout_seconds=600,
  retrieval_mode="deterministic", idempotency_key=null)`
- `get_ask_run(run_id)`
- `wait_for_ask_run(run_id, timeout_seconds=null)`
- `resume_ask_run(run_id)`
- `cancel_ask_run(run_id)`
- `export_ask_run(run_id)`
- `read_answer(run_id)`
- `read_citation(run_id, evidence_id)`
- `evidence(operation, selector, continuation=null)`

Project identity comes from the active MCP registry and caller identity comes
from the verified session context. Do not add `project_id` to public calls.

Ordinary discovery also exposes the four guarded worker tools `query_evidence`,
`read_evidence`, `submit_answer`, and `submit_review`; they require the active
Ask child principal and its assigned stage. Reviewers cannot issue new evidence
queries. The six pipeline stage operations in the contract are hidden from
ordinary discovery and require the owning pipeline's live authority. Their
names are not a public recovery interface.

HTTP start uses `POST /api/ask/runs` with `question`, `project_id`, and optional
`project_path`, `timeout_seconds`, `retrieval_mode`, and `idempotency_key` fields.
An explicit path must be the project’s primary checkout or a registered worktree
or clone on this machine; a foreign or unregistered path is denied. Requests
carrying `commit_ref` are rejected.
Status, wait, resume, cancel, and export use the routes documented in
[`../contracts/ask.md`](../contracts/ask.md). All calls use the existing local
daemon authentication transport.

## Interactive Evidence And Diagnostics

Use the public `evidence` MCP tool for a direct native JSON search, read, graph,
or commit-patch retrieval without starting Ask. Project and checkout are derived
from your authenticated context. CLI requests may omit the binding:

```bash
gcode evidence --request-json '{"schema_version":1,"operation":"read","read":{"kind":"range","path":"src/example.py","start_line":1,"end_line":20}}'
```

Consult `gcode evidence --help` and its request schema for supported selectors.
Carry opaque continuation tokens through every page. Managed Ask workers retain
their assigned evidence tools; reviewers cannot use public retrieval to bypass
admission.

Ask results live in PostgreSQL and survive daemon restart without publication
files. Retention defaults to seven days after terminal completion; set
`GOBBY_ASK_RETENTION_DAYS` to 1–3650 days to change it. Active and recoverable runs
are protected. Historical exported bundles are preserved.

For local diagnostics, add `--output-debug-files` to Ask or evidence. The CLI
writes a private bundle under your local Gobby home’s `ask-debug` directory and
prints its location on stderr. Old debug bundles are cleaned under the same
retention setting when diagnostics are written. Debug failures are separate from
successful results. MCP has no file-output or debug-files option.

## Evidence Expectations

Direct claims cite exact source evidence. Inferred claims cite premises and
explain the inference. Unknown claims describe the evidence limitation rather
than inventing support. Negative and exhaustive claims also record the searched
evidence scope; an empty search by itself is not proof that the repository lacks
something.

Citations use working-tree bytes verified against their indexed content hashes.
Dirty files and untracked nonignored files can be cited after ordinary indexing.
Observation time, checkout identity, and the recorded HEAD commit and tree identify
admission-time provenance, not an
immutable source snapshot. If a cited file changes before validation, refresh the
index and start a new run; do not treat an older answer as current evidence.

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

_Last verified: 2026-09-13_
