# Graphs and projections

Load when inspecting structural graphs, reporting graph state, or maintaining
code projections. Discover nested commands with `gcode graph --help` and
`gcode vector --help`. Gcode owns graph and vector operations; daemon HTTP
routes delegate UI operations to it.

Inspect before mutation: `graph overview`, `graph file --file <path>`,
`graph neighbors --symbol-id <uuid>`, and `graph blast-radius` with exactly one
`--symbol-id` or `--file`. `graph report --top-n 10` produces a project report;
required graph failures fail, optional memory-bridge failures are degradation.

`graph view --view mcg --file <path>` (or `--module`) shows IMPORTS;
`--view fcg --symbol <query>` shows CALLS, and `--view class-hierarchy --symbol
<query>` shows heritage. Views emit complete JSON and a complete Mermaid fence.
Class hierarchy defaults to depth 8 and has no row limit within depth; FCG/MCG
default to depth 1 and report incoming/outgoing truncation. Do not clip or hide
these diagnostics. A nullable node `file` is not an ownership fact.

For MCG, a uniquely resolving file and its module aliases identify the same
provider neighborhood. Incoming imports identify consumers. Do not invent a
persisted provider-file ownership column; inspect ambiguous aliases and use
concrete seeds.

MCG community labels come from the import partition that `gcode index` persists
per machine and project; community ids stay stable across runs. Views never
compute a partition inline: with none stored, node `community` stays null and
the `hint` says to run `gcode index`. MCG Mermaid output groups nodes into one
subgraph per community.

- `graph view --view communities [--min-size N] [--community ID|LABEL|PATH]`
  answers "Which files form a subsystem, and how subsystems depend on each
  other?" With no seed it lists communities of at least N files (default 2)
  and the import edges between them. `--community` selects one community by
  id, label, or member path and shows its members and neighbor communities.
- `label_stale: true` means the model label predates the current membership,
  so the view shows the deterministic label instead.

For authorized projection repair, `graph sync-file --file <path>` replays one
indexed file, `graph rebuild` replays project facts, and `graph cleanup-orphans`
removes projections absent from PostgreSQL. Vector counterparts are `vector
sync-file`, `vector rebuild`, and `vector cleanup-orphans`. Vector cleanup
does not need embeddings. Clear operations have no confirmation prompt:
`graph clear` and `vector clear` remove project projections; only operator
purge work uses `--project-id` without a checkout or vector `--drop-collection`.

Code graph labels and `code_symbols_{project_id}` vectors remain separate from
memory stores. Memory owns `RELATES_TO_CODE` hints. Preserve the graph-core
ownership and degradation contract; never mark a degraded report as complete.
`--allow-missing-indexed-file` is daemon/background-worker stale-work tolerance,
not a way to hide a wrong human path. An indexed file with no graph facts can
legitimately return skipped `no_graph_facts` after clearing old projection.

On a missing symbol, re-resolve it. On unavailable services, use
[recovery](recovery.md); don't clear the index to compensate for connectivity.
HTTP routes and their project scopes are documented in the guide below.

Guides: [Graph operations](../../../../../../../../docs/guides/gcode-user-guide.md#dependency-graph),
[ownership contract](../../../../../../../../docs/guides/gcode-graph-core.md#ownership-boundaries),
[HTTP routes](../../../../../../../../docs/guides/code-index.md#http-endpoints).

_Last verified: 2026-09-23_
