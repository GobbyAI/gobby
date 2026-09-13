# Source-bound Ask and evidence

Load when starting or recovering a durable repository question, handling an Ask
worker assignment, or reading immutable evidence. Discover `gobby-ask` tools
with `list_tools`; fetch the selected schema before calling it. Native entrypoints
are `gcode ask --help` and `gcode evidence --help`.

## Run a repository question

Use `start_ask_run` for an authorized question; preserve the returned `run_id`.
The public MCP tool derives project and caller identity from session context.
Defaults are `timeout_seconds=600` and
`retrieval_mode="deterministic"`. An optional idempotency key belongs to start.
CLI equivalent:

```bash
gcode ask "Where is session authorization enforced?" --background --format json
```

The daemon binds the run to the caller's existing project index and records HEAD
commit/tree IDs as provenance, with snapshotted investigator and reviewer
definitions. Evidence reads working-tree bytes verified against indexed content
hashes, so indexed uncommitted edits are citable. Ask creates no checkout or index;
there is no commit selector. Admitted evidence and published answers are immutable.
Hybrid retrieval is explicit opt-in and requires matching healthy semantic
configuration; a failure does not silently switch to deterministic retrieval.
Starting Ask runs managed agents; loading this reference grants no authority to
start one. Inspect installed definitions before claiming profiles are active.

Use `get_ask_run` for a status check and `wait_for_ask_run` for event-driven
completion. A caller timeout or disconnect does not cancel the run. After a
disconnect, check status: wait for a running run; use `resume_ask_run` only for
an eligible interrupted run. Resume preserves the original binding and absolute
deadline, and rejects running, completed, cancelled, and expired runs. Do not
loop on the CLI's printed resume hint when the run remains active.

`gcode ask --status RUN_ID`, `--resume RUN_ID`, and `--cancel RUN_ID` expose the
same lifecycle. CLI start waits unless `--background`; CLI resume waits after
admission. Use `cancel_ask_run` to stop an authorized run and its active child;
the durable evidence and submissions remain available for audit.

Inspect `status`, `current_stage`, `answer_outcome`, `typed_error`, and
`deadline_at` separately. Answer outcomes `complete`, `partial`, and `unknown`
are successful publications, not interchangeable claims of completeness. The
CLI prints failed/cancelled records and exits 2; successful outcomes exit 0.

## Export and verify

`export_ask_run` verifies the completed immutable publication and returns its
manifest hash and authenticated download URL. CLI export requires a destination:

```bash
gcode ask --export RUN_ID --output ./ask-exports
```

Default calls create no output bundles. Foreground CLI completion prints cited
Markdown; `--format json` returns structured answer and run metadata. MCP
`wait_for_ask_run` and `read_answer` return answer content and provenance;
`read_citation` retrieves evidence by ID without local files. Large results use
existing tool-result paging. Explicit export creates a local archive. Replay verification checks the
publication bytes; a copied archive or an old answer is not proof about today's
checkout. A publication/hash failure needs artifact diagnosis, not manual
editing of recorded evidence or a replacement success flag.

## Assigned Ask workers

Only the active authorized investigator may `query_evidence`; use its exact
operation/selector contract and carry returned continuation tokens with the
same request through all pages. `read_evidence` reads an admitted record for
the active child. Reviewers read admitted evidence and cannot issue new queries.
`submit_answer` and `submit_review` require the assigned attempt and exact
submission/evidence hashes. Submission does not end the process: separately
complete the worker through the normal agent lifecycle.

Direct claims cite exact evidence. Inferences state cited premises and rationale;
unknown claims state the evidence limit. Negative or exhaustive claims include
the searched evidence scope: empty search alone proves no repository-wide absence.
Use the assigned profile's full submission contract, not an invented schema.

`prepare`, `seed`, `spawn`, `validate`, `admit_repair`, and `publish` belong to
the owning pipeline. Ordinary discovery hides them; do not impersonate a stage
or call them as a recovery shortcut. The normative Ask contract's statement that
stage names are discoverable is bounded by current pipeline authority, as
implemented by `stage_tool_is_discoverable`.

## Direct interactive evidence

Ordinary authenticated agents may call `evidence(operation, selector,
continuation=null)` without an Ask run. Search, read, graph, and commit-patch
selectors reuse native retrieval and return JSON. Project and checkout derive
from caller context. Managed Ask workers cannot use this public tool to bypass
evidence admission; reviewers remain limited to admitted records.

Retained Ask bodies live in PostgreSQL, with seven-day retention after terminal
completion by default (`GOBBY_ASK_RETENTION_DAYS`, 1–3650 days). Active and
recoverable runs are protected. Provenance includes observation time, checkout,
recorded HEAD, and source hashes. Replay verifies integrity, not present freshness.

CLI-only `--output-debug-files` on Ask or evidence writes diagnostics beneath the
calling machine’s Gobby home, reports the location, and cleans old bundles under
the same retention policy. Default file output is off. Debug-write failure does
not invalidate the result. MCP never writes output files or exposes this option.

## Native evidence adapter

`gcode evidence` is a model-free JSON adapter for exact source evidence. Supply
one complete versioned request through `--request-json`. Evidence schema v1
supports search, read, and graph operations against the resolved project index,
with recorded commit/tree provenance.
Use the generated request contract in `crates/gcode/src/evidence/contracts.rs`
for supported selectors; interactive requests may omit binding and resolve it
from the caller’s project. Do not invent hashes or IDs.

JSON output is required. `--allow-stale` is rejected.
Honor response `complete`, `completeness`, `bounds`, warnings, and
opaque continuation. Repair stale index facts without bypassing admission.
Native evidence retrieval alone does not admit evidence into an
Ask worker's durable manifest; use that worker's MCP tools for admission.

Guide: [Ask](../../../../../../../../docs/guides/ask.md).
Contract: [Evidence and provenance](../../../../../../../../docs/contracts/ask.md#evidence-and-provenance).

_Last verified: 2026-09-13_
