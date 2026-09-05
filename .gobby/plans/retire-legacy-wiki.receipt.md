# Legacy wiki retirement receipt

Retirement is **in progress**. Live backup, purge, registry archive, landing,
installation, synchronization and daemon restart remain pending.

## Execution identity

Epic #21771 uses `/Users/josh/.gobby/worktrees/gobby/wt-epic-21771` and branch
`wt-epic-21771`. Its base is `0.5.0` at
`53e862d53d40027173ba2d74d86858d44fbc88f4`. The annotated archive tag
`legacy-wiki-before-retirement-21771` was verified at that base.

The approved plan is `retire-legacy-wiki.md`. Task #21840 owns operational
verification; #21772 owns backlog retirement and the plan-registry archive.
Preserve canonical summaries and revisions, handoffs, transcripts, memories,
projects, original files outside wiki storage, gcode services and explicit grants.

## Implementation evidence

| Work | Task and commit evidence | Current disposition |
| --- | --- | --- |
| Shared utilities, Python, Rust and schema retirement | #21783, #21784, #21787, #21778, #21796, #21803 | Committed and validated |
| Wiki UI and saved-tab fallback | #21816, #21828; `200d7aa050`, `3312725c36`, `ae8dfd7999` | Closed and memory-reviewed |
| Installer removal and shared release transport | #21834; `faef8aad4c` | Closed and memory-reviewed |
| Guidance and release workflows | #21835; `c03ed6aa03`, `28ab42162d`, `dda4cb5bf1` | Committed; child reviews pending |
| Actionlint evidence classifier | #21839; `6301c6e97b` | Closed and memory-reviewed |
| Inventory, backup, restore and purge procedure | #21836 | Implementation and independent review in progress |
| Real daemon retirement verification | #21846; `5920dcefdd` | Committed and tested; attribution cleanup pending |

Root-session verification passed 506 focused Python tests across 21 named files
in 49.45 seconds, 10 Rust workflow/security tests, and Actionlint on both changed
workflows. The final classifier check passed 243 focused tests in 5.32 seconds.
Exact commands and static-check results are recorded on the corresponding tasks.
The full pytest suite was not run.

Additional root verification passed 182 focused preservation and real-daemon tests
in 47.01 seconds, then 57 procedure, daemon, skill-parity and manifest tests in
71.94 seconds. Summary revisions, handoffs, transcript archives, attachment
forwarding and redaction remain covered. The procedure's Falkor fingerprint fix
will receive another focused run after its final edit.

A read-only merge rehearsal against `0.5.0` at `88e5cd8b66` found no conflicts and
preserved main's transcript, gzip, task-review and terminal changes. Final
integration verification will use the actual landed source identity.

## Isolated rehearsal resources

The following containers use the installed services' image IDs, without host data
mounts. Each has labels `gobby.retirement.epic=21771` and
`gobby.retirement.role=rehearsal`.

| Container | Loopback port | Initial check |
| --- | --- | --- |
| `gobby-wiki-retirement-21771-postgres` | 60893 | `pg_isready` passed |
| `gobby-wiki-retirement-21771-qdrant` | 6338 | `/readyz` passed |
| `gobby-wiki-retirement-21771-falkordb` | 16389 | `PING` passed |

The worker's isolated Falkor DUMP/RESTORE probe preserved nodes, an edge and
properties. Complete inventory-bound deletion and restoration remain pending.
The coordinator will remove these containers after their evidence is accepted.

## Outstanding operational evidence

The exact inventory, scoped backup, restoration receipt, shutdown record,
installed-runtime proof, guarded migration 426 result and final preservation
comparisons have not yet been produced. Main still runs schema 425 and the wiki.

Task #21772 records all 38 old-task dispositions. Shared #21577 and #21586 retain
their gcode obligations. Old-task closure retries and #21837's Actionlint review
await deployment of committed validator fixes. The old plan will be archived
through the registry after landing; replacement planning is a separate session.

## Inventory and rehearsal checkpoint

The first complete inventory has digest
`e6c4ea9667fa8b54f01a62fd6ee51432093ad93826a22868730e3a43e4e295ff`.
Its private record is under `~/.gobby/retirement/<digest>/inventory.json`, outside
repository indexing and wiki discovery. It found 21 filesystem roots, including
the Cargo-owned Gobby wiki binary, and 26 datastore/registry targets: the five-table
wiki schema (78,390 rows), five Qdrant collections, the dedicated Falkor graph,
one rule, one variable and 17 jobs. No writer process was detected.

The backup attempt refused on a changed Falkor fingerprint and did not complete.
Consecutive read-only captures showed nondeterministic raw DUMP bytes. Task #21836
is separating deterministic semantic comparison from the exact recovery artifact;
the corrected procedure must produce a new inventory. No content was deleted.

Task #21853 owns the discovered canonical-maintenance rehearsal gap: admission
currently supports only production and the shared test hub. A dedicated verified
Docker profile will retain the existing epoch, backup and schema guards while
operating only the isolated retirement services.

Task #21772 now records 12 old tasks closed obsolete and 26 still open. All 38
are unclaimed and unavailable for automation. Its readable record contains the
remaining leaves-first closure order and the exact later plan-archive operation.
Shared #21577 and #21586 remain open with gcode-only obligations.

Main is `b5f7d2fa23`. Session #11789 owns the preceding restart and observation
window; retirement landing and schema apply wait for that coordination. Session
#11776 confirmed that its later migrations 427/428 will remain unlanded until
retirement 426 and the preservation receipt are complete.
