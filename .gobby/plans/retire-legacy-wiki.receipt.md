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
| Installer removal and shared release transport | #21834; `faef8aad4c` | Closure review submitted |
| Guidance and release workflows | #21835; `c03ed6aa03`, `28ab42162d`, `dda4cb5bf1` | Committed; child reviews pending |
| Actionlint evidence classifier | #21839; `6301c6e97b` | Closure review submitted |
| Inventory, backup, restore and purge procedure | #21836 | Implementation and independent review in progress |

Root-session verification passed 506 focused Python tests across 21 named files
in 49.45 seconds, 10 Rust workflow/security tests, and Actionlint on both changed
workflows. The final classifier check passed 243 focused tests in 5.32 seconds.
Exact commands and static-check results are recorded on the corresponding tasks.
The full pytest suite was not run.

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
