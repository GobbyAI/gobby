# Freshness and recovery

Load for missing indices, stale source, projection drift, or runtime failures.
Start with `gcode status`, `gcode projects`, the failed command's error/recovery
fields, and `gcode embeddings doctor` for embedding configuration diagnostics.
Use `gcode contract` and `gcode schema-identity --json` for compatibility checks.
Never print runtime grant credentials.

Registered checkout setup precedes `gcode init`; standalone `gcode.json` is
rejected. Operator installation uses `gobby install`, project registration uses
`gobby init`, and hub migration uses the normal admin workflow. Do not create
schema or substitute a client DSN to bypass a grant failure.

For authorized indexing, `gcode index` is incremental; `--files <paths>...`
limits files, including deleted paths. After bulk checkout changes run indexing
explicitly. `--full` bypasses content-hash shortcuts; `--sync-projections`
includes graph/vector sync and reports each outcome. C/C++ work requiring
semantic guarantees can use `--require-cpp-semantics`; missing clangd or compile
commands then fails instead of accepting weaker extraction.

Read-time freshness attempts incremental refresh and can retain results with a
busy or degraded warning. Inspect it before asserting freshness. `--allow-stale`
is the explicit per-call bypass. `GCODE_FRESHNESS_INFLIGHT` is an internal
recursion guard, not an agent configuration workaround. A missing edited symbol
ID requires re-resolution, not a full rebuild.

`gcode repair` promotes stranded local imports and marks drift for resync. A
changed indexer version causes full reindex with projection reports; steady
state repair does not itself guarantee the queued graph work has finished.
Inspect `marked_for_resync`, `graph_reconcile`, and `full_reindex` as applicable.
An extractor change without a Cargo version bump needs an explicit full index.
The daemon retries busy/failed post-edit work with backoff and delegates queued
projection sync to gcode. Check installed schedules and config before assuming
maintenance is active.

For BM25 corruption, operator `gobby postgres status --json` reports verification
under `code_index`; inspect its health payload (the status command does not fail
solely because BM25 is unhealthy). Operator `gobby postgres repair-code-index
--json` verifies the two required indexes in the active schema, selectively
reindexes damaged ones under an advisory lock, then verifies again. It exits 1
if still unhealthy. It reads the bootstrap DSN and uses
`code_index.maintenance_index_timeout_seconds`; it has no project/DSN override.
Missing indexes require normal PostgreSQL setup/migrations, not REINDEX.
If startup marked `code_index_bm25` degraded, repair first, then coordinate a
restart so maintenance and sync workers can start. Do not substitute `gcode
repair`, invalidate source facts, or treat database errors as empty search.
See [BM25 recovery](../../../../../../../../docs/guides/code-index.md#postgresql-bm25-recovery).

Operator-only cleanup: `invalidate` removes the selected project's index and
prompts unless `--force`; `prune` without `--project` requests global daemon
maintenance. Explicit `--project` scopes pruning; `--retention-days` is at least
1, and `--max-seconds` requires explicit project scope. Budget exhaustion can
defer versions; partial failures are not a clean sweep. Preserve task/memory
data and other projects. Prefer projection-only repair when facts are sound.

Exact retirement is operator-only: `retire-files --manifest <private-file>`
validates an inventory-bound set; applying also requires `--apply --receipt
<private-file>`. Preserve the manifest and durable receipt on interruption.
Retry the same admitted inventory only after resolving its error; changed
identity or newly present files require fresh inventory, never wider deletion.

On `payload_skew` or `api_contract_mismatch`, stop retrying gcode, report its
recovery directive, and continue with fallback tools. Coordinate rebuilds and
new-inode installation with active owners; never replace their newer binary
with another checkout's reduced surface. Destructive examples and recovery
probes belong in isolated fixtures, not the running user's state.

Navigation rules fail open without being asked. A typed outage from grant or
checkout resolution (`schema_mismatch`, `daemon_required`, `io`,
`checkout_required`, and the like) allows raw navigation of that checkout for
the rest of the turn. A gcode call without verified output reopens the read it
targeted. Retry the blocked raw command rather than repeating the failing call.

Guide: [Recovery and maintenance](../../../../../../../../docs/guides/gcode-user-guide.md#project-management).

_Last verified: 2026-09-14_
