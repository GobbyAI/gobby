Plan artifact: `.gobby/plans/gcode-graphify-parity-delta.md`

# gcode communities and Graphify parity+: remaining work

**Plan ID:** gcode-graphify-parity-delta

## Overview
`kind: framing`

This plan specifies the work left under #22950 (Plan the remaining gcode
communities and Graphify parity+ work). It is a delta against the completed
plan `.gobby/plans/completed/gcode-import-communities.md`, written under
#22140 (Plan gcode import communities at parity+ with Graphify) and
implemented under #22581 (gcode project-level import communities at parity+
with Graphify), which closed on 2026-09-23. It does not repeat that work.

**What is already done.** The completed plan set parity+ with Graphify's
community subsystem, not with all of Graphify (lines 30-34). Every
implementation leaf of #22581 is closed, and the code is in
`crates/gcode/src/communities.rs`, `crates/gcode/src/commands/graph/view/`,
`crates/gcode/src/graph/report/` and `crates/gcode/src/evidence/communities.rs`.
The scorecard in
`.gobby/plans/research/gcode-graphify-parity-evidence-2026-09.md` (§1.2)
records, as of 2026-09-24:

| Capability | State |
| --- | --- |
| Cohesion scoring, oversized and low-cohesion splits | Matched (Graphify's constants kept by the threshold spike) |
| Cross-run identity | Exceeded: Jaccard remap and a per-project watermark, so a retired id is never reissued |
| Persisted memberships, signature-gated label reuse | Matched: computed at index time, read-only on every read path |
| Semantic labels | Exceeded on output: Graphify 0.9.55 produced no semantic names |
| Calibrated label gate | Missing: #22604 (Gate generated names through a Jev `choice` decision) is held |
| Community surfaces | Exceeded: `--view=communities`, `--community`, the report section, MCG subgraphs, the `communities` evidence operation |
| Incremental refresh cost | Over the +250 ms budget at Gobby scale, owned by #22862 |

Since then, #22889 (801a3d1f27) fixed the gcode CLI contract drift, and
#23055 (30d66635c1) retired Ask while keeping the evidence operations as
`gcode evidence`. gcode is now 1.9.9. The community code has not changed
since #22858 (Community refresh counts internal edges by scanning every
import edge per community, 386c7040d0), apart from the managed-scope fixes
of #23431 (gcode index refresh fails with an RLS error on
code_indexed_projects for a linked worktree, so it serves stale offsets).

**What remains.** Two things:

- Every head-to-head number is stale. The September bakeoff
  (`docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`) measured
  gcode 1.7.0 against Graphify 0.9.55, before the community work existed.
  Its gcode C7 (interruption and recovery) never ran. Nobody may claim
  parity+ in writing until a rerun records current numbers (1.1).
- The communities view breaks size ties on `members[0]`, while the report
  and the evidence operation break them on `community_id`
  (`docs/evidence/community-labels-2026-09/live-smoke.md`, lines 115-128).
  The view is the one surface that disagrees (1.2).

**When the leaves close:**
- A dated evidence report records C1 to C8, including C7 for both tools, for
  current gcode against the current Graphify release, with interleaved,
  order-balanced measurements and recorded load, on the
  frozen Game Goblins corpus. It also records the community-label facts that
  decide #22604 (1.1).
- Every gcode read surface orders communities by member count descending,
  then numeric `community_id` ascending (1.2).

## Decision Record
`kind: framing`

The Orchestrator gobby#14972 ruled on the scope questions on 2026-10-05 at
13:06 CT (Q1 to Q6 below). The Plan Writer gobby#15434 made the remaining
decisions on 2026-10-05 from the sources cited.

1. **Scope (Q1).** The parity+ target is Graphify's community subsystem, as
   the completed plan set it. A broader Graphify capability is built only
   when the plan names its consumer. No capability outside the community
   subsystem has one, so this plan adds none (Decisions 6 and 7).
2. **Expand two leaves, each blocked by batch 3 activation.** The
   Orchestrator's 2026-10-06 15:29 CT ruling supersedes the earlier
   expansion-after-close gate. Expand exactly the existing two fully
   specified leaves under #22948 (Code index: communities and Graphify
   parity), and give each a blocked-by dependency on #23692 (Land batch 3
   with one cutover). That carrier activates #22862 (gcode community refresh
   input-digest skip) and #23638 (avoid quadratic community remapping), with
   gcode 1.9.13 and schema 461. The benchmark uses that activated state;
   #22862's post-activation latency measurement remains its own close gate.
   Preserve both existing routes and no sibling dependency. The external
   task reference is a task dependency applied by the Orchestrator during
   expansion, never an M1 `depends_on` value, which names sibling sections.
   This explicit ruling governs this amendment's expansion; it does not
   change the plan-coverage contract for other plans or add a third leaf.
3. **The benchmark is the first leaf (Q2), and its protocol amends the frozen
   matrix.** `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md` stays the
   protocol for everything 1.1 does not amend. Leaf 1.1 carries each
   amendment in full, with its sources. In brief:
   - the rerun provisions a fresh runtime root, because the September
     provisioning refuses an existing root and asserts the September owner
     task;
   - every timed comparison uses at least five interleaved, order-balanced
     pairs, with the load recorded at each run, per the Orchestrator's
     2026-10-06 15:29 CT protocol amendment;
   - both tools run at their current identities;
   - gcode's C7 marker is a row count of its per-file state table, because
     gcode prints no progress under a pipe and keeps its state in
     PostgreSQL;
   - gcode's C7 passes only on queryable equivalence with a fresh clean C1
     state, because gcode syncs its projections after it commits facts;
   - each C2-C6 case is checked for community-id stability against its own
     starting state, because the matrix gives those cases separate
     lineages;
   - C0 runs as setup and preflight, and C9 (presentation exports) is not
     rerun: its missing surfaces are recorded dispositions, and no consumer
     needs them (Decision 7).
4. **Size ties break on `community_id` (Q3).** Every read surface orders
   communities by `member_count` descending, then numeric `community_id`
   ascending. This supersedes the PD ruling that the communities view breaks
   ties on `members[0]` (`live-smoke.md`, lines 121-122). The report
   (`crates/gcode/src/graph/report/types.rs::GraphReportCommunities::from_rows`)
   and the evidence operation (`crates/gcode/src/evidence/communities.rs`,
   line 59) already sort by member count descending, then numeric
   `community_id` ascending. The stored-row reader in
   `crates/gcode/src/db/communities.rs` orders by `community_id` alone; it
   supplies rows to those consumers and is unchanged. Only the view's output
   sort changes. Ids compare as integers, so `community:2` precedes
   `community:10`.
   The partition's construction order in `crates/gcode/src/communities/partition.rs`
   (line 58) is internal to the Leiden pass, reaches no reader, and stays.
5. **The labeler work queue keeps its order.**
   `src/gobby/code_index/_storage/communities.py::get_unlabeled_communities`
   selects unlabeled communities `ORDER BY member_count DESC LIMIT n`, with
   ties in arbitrary order. It is a work queue, not a read surface: every
   unlabeled row is selected on some later pass, and no reader sees the
   order. It is out of scope.
6. **Three report additions are dropped (Q4).** Cross-community
   connections, a corpus-level confidence distribution, and suggested
   questions in the Markdown report have no named consumer, so none is built.
7. **Five Graphify capabilities are excluded (Q5).**
   - **A `path` query over IMPORTS edges.** The communities view and
     `gcode impact` already answer import-reachability questions.
   - **Rationale and cites nodes.** gcode's exact-prose content retrieval
     passed C4, where Graphify truncated the rationale.
   - **New tree-sitter grammars.** gcode has 21 grammar crates, and no corpus
     in use needs another.
   - **Multimodal extraction.** No plan, corpus, or consumer needs images or
     other media indexed.
   - **A standalone graph MCP server.** The `gcode evidence` CLI, with its
     `communities` and `graph` operations (`directed_path`), replaces it.

   The decisions already recorded in research §1.3 (wiki renderer, HTML and
   other exports, Leiden tuning flags, hub exclusion and call edges) stand.
8. **#22604 stays held until the benchmark decides it (Q6).** #22604 (Gate
   generated names through a Jev `choice` decision) carries the
   `needs-decision` hold label. Today a generated label is admitted on schema
   validation alone, with `label_confidence` left empty
   (`src/gobby/code_index/community_labeler.py`, lines 86-188). 1.1 records
   whether current Graphify emits semantic community names and how gcode's
   labels are sourced. Josh decides from that evidence whether #22604 is
   unparked or retired; the plan neither unparks nor retires it. Parity+
   property 3 ("a generated label admitted only when a decision model is
   confident", completed plan lines 30-34) holds only if #22604 ships. If it
   is retired, that decision restates property 3, and this plan claims
   nothing for it.
9. **#22606 keeps its prerequisite.** #22606 (Memory community detection
   over memory_crossrefs) stays blocked by #21563 (Memory and search), as
   deferral D1 of the completed plan set it (lines 1722-1769). The Leiden
   kernel is reachable only in-process from the Rust memory crate. This plan
   neither pulls it forward nor edits it.
10. **#21572 is cross-referenced only.** #21572 (Parity ledger) is the
    whole-platform ledger of Rust public surfaces under 1.0. gcode community
    capabilities are not ledger rows by default, and nothing in this plan is
    a platform-parity obligation. The capabilities excluded here (Decisions 6
    and 7) are excluded from gcode, not from that ledger, which keeps its 1.0
    placement.
11. **Adjacent work stays separate.** #22958 (Plan code-index symbol-vector
    reconciliation and path-aware content retention) is unrelated to
    communities and is excluded. #22862's own measurement and any input
    digest it builds stay in #22862.

## Constraints
`kind: framing`

- Leaf 1.1 is a heavy, long-running measurement. The Lane Manager admits it,
  using Decision 3's comparative protocol and the shared heavy-work hold.
  It runs in the fresh isolated
  root `/Users/josh/Projects/gcode-graphify-rerun-2026-10/`, with its own
  compose services and its own Gobby home. It never touches the live daemon,
  the live database, or port 60891, and it never writes under the September
  root.
- The September harness is torn down: `docker ps -a` lists no
  `wiki-bakeoff` container as of 2026-10-05. The executor stops only the
  containers its own receipts record.
- Leaf 1.2 changes `gobby-code`. The change is live only after the
  workspace binary set is rebuilt and promoted through
  `promote_workspace_binary_set`, and the Orchestrator schedules that per
  the #22801 (Lane 5 - Rust) lane rule. The leaf closes on its tests.
- No daemon restart, push, or migration is needed.

## P1: Current evidence and one read order
`kind: framing`

**Goal**: parity+ claims rest on current measurements, and every read
surface lists communities in one order.

**Granularity:** two leaves. The rerun (1.1) is a measurement whose only
artifact is an evidence report. The tie order (1.2) is a code change with
unit tests. They share no target and need no edge between them; the
Orchestrator dispatches 1.1 first, per Q2.

### 1.1 Rerun the Graphify comparison on current gcode [category: test]
`kind: deliverable`

Targets:
- `docs/evidence/gcode-graphify-rerun-2026-10/report.md`

**Research context:**
- Prior run: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`
  (C0-C9 table at lines 62-75, C1-C6 measurements at lines 80-94, frozen
  identities at lines 42-48). Its runner scripts and results live in the
  September root, `/Users/josh/Projects/wiki-bakeoff-code-2026-09/`, under
  `results/index-comparison/`: `run_index_cases.py` (C1-C6), `run_c7.py`,
  `run_c8.py`, `run_c8_deterministic.py`, `measurements.json`,
  `run-records.normalized.jsonl`, and `C7-summary.json` (Graphify's clean
  and recovered signatures). They are outside the repo and are cited by
  path and SHA-256, not targeted.
- Protocol: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`. C2 to C6
  (lines 365-562) are separate one-step lineages, not one serial state: C2
  copies the sealed C1 state, C3 runs on an independent state built from
  the baseline, and C4 to C6 each start from a fresh baseline copy. C7 is
  at lines 563-696. Its wrapper accepts an output regex, a state-file glob,
  or a pinned fault-injection seam as the marker (lines 565-570), starts a
  separate process group, waits for the marker, sends one `SIGTERM`, and
  uses `SIGKILL` only after a 30 s deadline. Native recovery is at lines
  671-685, and the queryable-result requirement at lines 688-695.
- Provisioning scripts in the repo:
  `docs/evidence/wiki-bakeoff-code-2026-09/launch_services.py`,
  `provision_environment.py`, `prepare_daemon.py`, and
  `validate_environment.py`. They cannot provision the rerun in place:
  `provision_environment.py::init_runtime` (lines 365-369) refuses an
  existing root, and `launch_services.py` (lines 33-35) asserts the
  September owner task #21942. They also pin the September identities:
  `validate_environment.py` requires Graphify 0.9.55, gcode 1.7.0, contract
  v8, and gcode source `7394b97c`, and `provision_environment.py` and
  `prepare_daemon.py` archive and import that source. The corpora
  `corpora/gcode/C0` to `C8` and `C8-baseline`/`C8-change` survive in the
  September root.
- gcode facts: `gcode 1.9.9`. gcode prints no progress under a pipe: the
  bar renders only on a terminal
  (`crates/gcore/src/progress.rs::non_terminal_capture_suppresses_output`).
  It keeps its state in PostgreSQL, not in files, and commits each file in
  its own transaction (`crates/gcode/src/index/indexer/file.rs::index_file`,
  lines 47-64; the `code_indexed_file_states` writer is
  `crates/gcode/src/index/api/file_state.rs`), so the project's
  `code_indexed_file_states` row count rises during a cold index. It syncs
  the graph and vector projections only after the facts commit
  (`crates/gcode/src/commands/index.rs::run_index_locked`). The community
  refresh runs inside `gcode index` through
  `crates/gcode/src/communities.rs::refresh_project_communities`, which did
  not exist in the 09 run. So C2 to C6 now include it, and that cost is
  what #22862 (gcode community refresh: measure the unchanged-file cost
  against the +250 ms budget, add the input-digest skip if over) measures.
- Community ids: `crates/gcode/src/communities/remap.rs::assign_ids` keeps
  the id of the prior community that a new community matches by Jaccard
  overlap, and gives each unmatched community an id above both every prior
  id and the stored watermark
  `code_indexed_project_states.community_id_watermark`, which never falls.
  The unit tests in `crates/gcode/src/communities/remap/tests.rs` cover
  history across runs: `retired_ids_are_never_reissued`,
  `watermark_is_monotone_across_runs`, and
  `jaccard_beats_raw_overlap_on_swallow_case`.
- Community evidence: the `communities` operation of `gcode evidence`
  lists communities with empty `members`. With a `community_id` selector it
  returns that one community, whose `members` carry `path` and
  `content_hash`, cut to `max_members` (1 to 500, default 50) with
  `members_truncated` set when cut (`crates/gcode/src/evidence/communities.rs`).
  Items are atomic: `crates/gcode/src/evidence/mod.rs::EvidenceLibrary::paginate`
  returns `NarrowingRequired` when any item exceeds `max_bytes` (default
  16,384), and otherwise fills a page with whole items whose summed
  serialized bytes, reported as `bounds.serialized_item_bytes`, stay within
  `max_bytes`. The response envelope is not counted. A `read` request of
  `kind: range` returns the file's indexed `content_hash`, and fails when
  the file's bytes do not hash to it (`crates/gcode/src/evidence/source.rs`).
- 09 numbers to compare against: C1 gcode 35.789 s versus Graphify 2.430 s;
  C2 0.117 s versus 2.108 s; C3 3.121 s versus 2.533 s; C4-C6 0.42-0.98 s
  versus 2.43-2.49 s; C8 6/14 for both.
- Community counts are not comparable: Graphify clusters 2,308 mixed nodes,
  gcode clusters 142 files
  (`docs/evidence/community-labels-2026-09/thresholds.md`, lines 192-197).

**Implementation:**
1. **Fresh root.** The September root stays read-only input, and its
   scripts and results stay unchanged. Provision a new root,
   `/Users/josh/Projects/gcode-graphify-rerun-2026-10/`, from dated copies
   of the repo's provisioning and validation scripts and of the September
   runners, kept in that root. The copies may move only the runtime root,
   the owner task (this leaf's task), the compose project, container,
   volume and network names, and the tool, source and contract pins. Every
   isolation, corpus, service-boundary, containment, and receipt check
   stays. Copy the frozen corpora from the September root and verify them
   against its manifests' hashes. Archive the `0.5.0` source at the run's
   commit for schema setup, and provision a schema and an isolated native
   binary set compatible with the copied gcode. Fresh ownership and service
   receipts name this leaf's task. Results land in
   `/Users/josh/Projects/gcode-graphify-rerun-2026-10/results/index-comparison/`.
   The report records the copies' SHA-256 values, their exact invocations,
   and the source and binary identities.
2. **C0.** Copy the installed `~/.gobby/bin/gcode` into the harness tools
   and record its SHA-256, `gcode --version`, and `gcode contract`. Install
   the latest Graphify release from PyPI into a fresh venv, with no
   hardcoded version, and record the package version, executable SHA-256,
   and lock SHA-256, as the 09 report did. Rerun the embedding-identity
   preflight; the hybrid lane joins C8 only if it passes. Confirm an
   observable pre-completion Graphify marker for the chosen release; the
   September regex is reused only if it is observed again. Establish the
   gcode C7 marker: a sidecar poller counts the C7 project's
   `code_indexed_file_states` rows in the isolated PostgreSQL, and the
   marker is that count being above 0 and below the clean C1 file count.
   Run one cold C1 index under the poller and confirm such a count appears
   before the index completes. The marker reads tool state and changes
   neither the corpus nor the source, which is what the matrix's marker
   rules protect. No production seam is added.
3. **C1 to C6** for both tools, with the 09 argv shapes and corpora, each
   case from the starting state the matrix names. For each timed comparison,
   run at least five interleaved pairs of gcode and Graphify, alternating
   which tool runs first. Use an even number of pairs for equal numbers of
   both orders (six pairs minimum). Every repetition starts from an
   independent copy of the case's prescribed starting state; repetitions
   never share a mutated state. Record pair id, tool order, exact argv,
   raw timing, and the 1-, 5-, and 15-minute load averages immediately before
   each run. Report both tools' p50 timings and the p50 paired difference,
   alongside the raw samples. This comparative protocol replaces the
   5-minute load-under-5 preflight, per the Orchestrator's 2026-10-06
   15:29 CT ruling; the shared heavy-work hold still applies. Apply the same
   pairing and state-isolation rules to any timed C7 or C8 comparison.
   Keep each C7 repetition's one planned interruption and one native recovery.
   For gcode's C2 to
   C6, read the case state's `code_communities` rows and its
   `community_id_watermark` before the timed command starts and after it
   ends, outside the timed window.
4. **C7** for both tools, each from a fresh baseline copy and fresh
   isolated state, through the dated wrapper copy and the C0 markers.
   Preserve the partial state and logs after the one `SIGTERM`. Recover
   each tool exactly once with its native command:
   - Graphify: `update --no-cluster`, then `check-update`, against the
     interrupted output.
   - gcode: `index --sync-projections` without `--full`, then `status`.

   Build a separate fresh clean C1 state for each tool. Graphify's
   recovered state passes when it matches that state by the 09 signature
   (`C7-summary.json`). gcode's recovered state passes only when all of
   these hold between it and its clean state:
   - **Facts.** For each fact table the index writes for the project
     (`code_indexed_file_states`, `code_symbols`, `code_content_chunks`,
     `code_imports`, `code_inheritance`, `code_calls`, and
     `code_communities`), the sorted rows the project owns or references
     hash equal per table, after project id, root path, row UUIDs and
     timestamps are normalized.
   - **Projections.** The recovery's `index --sync-projections` response
     reports both projections synced. A fixed probe set, recorded in C0
     before either state is queried, returns byte-equal normalized output
     on both states: the C8 deterministic retrieval queries, and `gcode
     graph` reads (FalkorDB-backed, `crates/gcode/src/commands/graph/reads.rs`)
     for five corpus symbols named in advance.
   - **Vectors.** The C8 hybrid queries also match when the embedding
     preflight passed. When it failed, the vector projection is recorded
     `not-compared` with the preflight's reason.

   Equal counts never pass C7 on their own. A degraded or unsupported
   outcome is recorded as such. Record the exact interruption, recovery,
   and clean-build argv, and every protocol adaptation, with gcode's
   row-count marker recorded as an amendment to the matrix.
5. **C8**: the 14 frozen questions, scored as in 09.
6. **Labels**, each in its own untimed state, apart from the timed
   `--code-only --no-cluster` C1 state (`matrix.md`, lines 338-350):
   - Graphify deterministic names: `extract --code-only` with clustering on,
     into its own output directory.
   - Graphify semantic names: the matrix's C1 semantic stage
     (`extract --backend … --model …`) into its own output directory. It
     runs on its native provider path, or it is recorded as
     `blocked-preflight` with the reason; no substitute is used.
   - gcode: each community's label and `label_source` from
     `gcode graph view --view=communities --format json` on the clean C1
     state.
7. **Parity+ properties.** Each verdict names its evidence producer and its
   pass or absence condition, and says whether it was measured in this run
   or rests on earlier evidence:
   - **File-level projection, externals excluded.** Measured on the clean C1
     state: every member in `code_communities` is a file path that has a
     `code_indexed_file_states` row for the project, and none is an
     external module.
   - **Monotone ids with Jaccard remap.** Measured per case from step 3's
     reads, each C2-C6 case against its own starting state only. C2 passes
     when it leaves every `(community_id, member_signature)` pair and the
     watermark unchanged. Each of C3 to C6 passes when a community whose
     member set is unchanged keeps its id, every id absent from the
     starting state is above the starting watermark, and the watermark
     does not fall. An id absent from the starting state and at or below
     its watermark is a recycled id, and one is an absence. Each case
     records its counts of kept, remapped (id kept, members changed, with
     the Jaccard of the old and new member sets), retired, and new
     communities. A case whose remapped or retired count is 0 records that
     behavior `not-exercised`. No matrix case spans two refreshes of one
     state, so a retired id staying retired across runs rests on the
     `remap/tests.rs` unit tests above, as earlier evidence.
   - **Confident label admission.** Missing: #22604 (Gate generated names
     through a Jev `choice` decision) is held, and a generated label is
     admitted on schema validation alone, with `label_confidence` left
     empty (`src/gobby/code_index/community_labeler.py`, lines 86-188). The
     `label_source` counts are context only.
   - **Byte-bounded, replay-verifiable evidence.** Measured with the
     `communities` evidence operation on the clean C1 state, every request
     bound to that state's commit and tree. Each distinct request (binding,
     selector, and `max_bytes`) runs twice, and both runs return identical
     `items`, `bounds`, and `continuation`, or the identical error.
     - List: at the default `max_bytes`, and at a `max_bytes` no smaller
       than the largest list item and below the complete list's summed item
       bytes, so that the list spans at least two pages. Each continuation
       is followed to the end. Every page reports `bounds.serialized_item_bytes`
       within `bounds.max_bytes`, and the pages together return every
       community exactly once, matching `bounds.total_items`.
     - Detail: the first community in list order, with `max_members` at its
       member count (at most 500). A default-budget request gives its full
       item size: `bounds.serialized_item_bytes` when it succeeds, or the
       error's `item_bytes` when it returns `NarrowingRequired`. The same
       request at a `max_bytes` one byte below that size returns
       `NarrowingRequired` naming the community's `evidence_id`. At that
       `max_bytes`, a request with `max_members` low enough for the item to
       fit succeeds, with `members_truncated: true` and
       `bounds.serialized_item_bytes` within `max_bytes`.
     - Replay: for every member a successful detail returns, a one-line
       `range` read of its `path` against the same binding succeeds and
       returns the member's `content_hash`.

     It passes when all of these hold. Identical `evidence_id` lists alone
     show stable identity, not verifiable content.

   Index and retrieval scores never stand in for a property verdict.
8. Write `docs/evidence/gcode-graphify-rerun-2026-10/report.md`: identities,
   the load samples, the C1-C8 tables beside the 09 numbers (C9 is not
   rerun), the C7 method with the amendments, the label facts, the four
   property verdicts, and a decision-input section for #22604.

**Acceptance:**

- 1.1.1 - The report records the fresh runtime root and its ownership receipt, the dated script copies' SHA-256 values, both tools' identities (gcode version, contract, and SHA-256 of the copied installed binary; Graphify's PyPI version and executable and lock SHA-256), the corpus commits, and at least five interleaved, order-balanced pairs for each timed comparison (an even number, six minimum, with equal AB/BA orders), with the load recorded at each run, raw samples, both tools' p50 timings and the p50 paired difference. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.
- 1.1.2 - The report gives C1 to C6 for both tools in the 09 table shape (seconds, changed files, symbols and chunks for gcode; nodes, edges and uncached files for Graphify) beside the 09 numbers, and states that gcode's C2 to C6 include the community refresh. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.
- 1.1.3 - The report gives C7 for both tools: the marker each used, the one planned `SIGTERM`, any safety `SIGKILL`, the native recovery commands, and whether the recovered state equals a fresh clean C1 state, by the 09 signature for Graphify and by step 4's fact, projection and vector checks for gcode, with the gcode row-count marker recorded as an amendment to the matrix. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.
- 1.1.4 - The report scores C8's 14 frozen questions for both tools as in 09, and states whether the gcode hybrid lane was included or excluded by the embedding preflight. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.
- 1.1.5 - The report records Graphify's deterministic community names, whether current Graphify emits semantic community names (yes, no, or `blocked-preflight` with the reason), and gcode's count of communities by `label_source`, each from its own untimed state, without comparing community counts, and a section that states the evidence for Josh's #22604 decision without making it. behavior: "Decision input for #22604" in `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.
- 1.1.6 - The report gives a verdict for each of the four parity+ properties by step 7's producers and conditions, says whether each was measured in this run, records every remap or retirement behavior no case exercised as `not-exercised`, and records the label-admission property as missing while #22604 is held. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.

**Verification:** the dated copy of the environment validator passes
against the final C0 identities and schema before C1, and every number in
the report traces to a record under
`/Users/josh/Projects/gcode-graphify-rerun-2026-10/results/index-comparison/`
named in the report.

### 1.2 Order size ties by community id in the graph views [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/render.rs::ViewCommunity`
- `crates/gcode/src/commands/graph/view/render.rs::build_view_payload`
- `crates/gcode/src/commands/graph/view/communities.rs::view_community`
- `crates/gcode/src/commands/graph/view/mcg.rs::label_communities`
- `crates/gcode/src/commands/graph/view/communities/tests.rs::list_orders_communities_by_size_then_first_member`
- `crates/gcode/src/commands/graph/view/render_tests.rs::*` — scope-reason: the `community` fixture builds a `community_id`, and a new test pins the numeric tie order in `build_view_payload`
- `crates/gcode/src/commands/graph/view/mcg/tests.rs::*` — scope-reason: a new test pins the same tie order through `label_communities` and `build_view_payload`

Consumers unchanged:
- `crates/gcode/src/commands/graph/view/mod.rs` — no-edit-reason: re-exports `build_view_payload` and calls it with the signature unchanged; it builds no `ViewCommunity`
- `crates/gcode/src/commands/graph/view/fcg.rs` — no-edit-reason: calls `build_view_payload` with the signature unchanged and builds no `ViewCommunity`
- `crates/gcode/src/commands/graph/view/class_hierarchy.rs` — no-edit-reason: calls `build_view_payload` with the signature unchanged and builds no `ViewCommunity`
- `crates/gcode/src/commands/graph/view/class_hierarchy/fetch.rs` — no-edit-reason: calls `build_view_payload` with the signature unchanged and builds no `ViewCommunity`
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs` — no-edit-reason: calls `label_communities` and `build_view_payload` with their signatures unchanged; `label_communities` sets the new field internally
- `crates/gcode/src/commands/graph/view/tests.rs` — no-edit-reason: `view_typed_ids_keep_file_and_module_collision_distinct` calls `build_view_payload` with its signature unchanged and builds no `ViewCommunity`

**Research context:**
- `render.rs::ViewCommunity` carries `first_member: String` with
  `#[serde(skip)]` and the doc comment "The stored community's `members[0]`,
  which breaks size ties in partition order."
- `render.rs::build_view_payload` sorts with the comment
  `// Partition order: size desc, then members[0] asc.` and
  `right.size.cmp(&left.size).then_with(|| left.first_member.cmp(&right.first_member))`.
  JSON, text, and Mermaid output all read `payload.communities` in that
  order, so the Mermaid subgraphs follow it too.
- `ViewCommunity` is built in exactly three places:
  `communities.rs::view_community` (from `StoredCommunity`, whose
  `community_id` is `i32`), `mcg.rs::label_communities` (from the stored
  community), and the `render_tests.rs` fixture `community`, whose callers
  pass ids such as `"community:1"`.
- `communities/tests.rs::list_orders_communities_by_size_then_first_member`
  gives communities 2 and 10 the same size 3. It expects
  `["community:10", "community:2", "community:1"]`, because `src/a.rs`
  sorts before `src/c.rs`.
- `mcg/tests.rs::mcg_labels_nodes_from_stored_communities` already runs
  `label_communities` then `build_view_payload` through the `stored_community`
  helper. A new test can follow that path.
- The report (`crates/gcode/src/graph/report/types.rs::GraphReportCommunities::from_rows`)
  and the evidence operation (`crates/gcode/src/evidence/communities.rs`)
  already sort by member count descending, then numeric `community_id`
  ascending. The stored-row reader in `crates/gcode/src/db/communities.rs`
  orders by `community_id` alone, feeds those consumers, and stays as is.
- Rejected: parsing the integer back out of the `community:N` view id at sort
  time. The stored integer is already on hand at every construction site.

**Implementation:**
1. In `ViewCommunity`, replace `first_member: String` with
   `community_id: i32`, still `#[serde(skip)]`, so the JSON contract does
   not change. Its doc comment says it breaks size ties.
2. In `build_view_payload`, sort by `size` descending, then
   `community_id` ascending, and change the comment to match.
3. Set `community_id` from the stored row in `view_community` and in
   `label_communities`.
4. The `render_tests.rs` fixture `community` takes `community_id` as an
   `i32`, sets the hidden field directly, and builds `id` with
   `NodeKey::community(community_id.to_string()).canonical()`, as the
   production constructors do. Its callers pass integers. Add the render
   test and the MCG test, and rename the list test.

**Acceptance:**

- 1.2.1 - `build_view_payload` lists two communities of equal size with ids 10 and 2 as `community:2` then `community:10`, after any larger community. test: `crates/gcode/src/commands/graph/view/render_tests.rs::view_orders_size_ties_by_numeric_community_id`.
- 1.2.2 - The communities list view returns `["community:2", "community:10", "community:1"]` for the existing three-community fixture. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::list_orders_communities_by_size_then_community_id`.
- 1.2.3 - An MCG view over stored communities 10 and 2 of equal size lists `community:2` first, in the payload and in the Mermaid subgraph order. test: `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_orders_size_ties_by_community_id`.

**Verification:** `cargo test -p gobby-code --lib commands::graph::view`,
`cargo fmt --all --check`, and `cargo clippy -p gobby-code --all-targets -- -D warnings`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15434, on the
  Orchestrator gobby#14972's 13:06 CT rulings, with the Adversary
  gobby#15401. Targets and consumers were swept read-only on `0.5.0` at
  `a8b9514bea`. The draft is narrative only, with no M1.
- 2026-10-05: The single enhancer pass (run `e9fc1593`) proposed PE-01 to
  PE-05, and the Orchestrator gobby#14972 accepted all five at 13:25 CT:
  - 1.1 provisions from dated copies of the September scripts, with only
    their identity pins moved; the September scripts and results stay
    unchanged (PE-01);
  - C7 recovers each tool with its native command per `matrix.md`, against
    a separate fresh clean C1 state, and re-observes the Graphify marker
    (PE-02);
  - Decision 4 names the report and evidence sorts as the conforming layer,
    and the stored-row reader as unchanged (PE-03);
  - `view/tests.rs` joins 1.2's Consumers unchanged (PE-04);
  - the 1.2 fixture builds its id the way production does (PE-05).
- 2026-10-05: The Adversary gobby#15401's preliminary review raised GP-A1
  to GP-A3, and the Plan Writer accepted all three:
  - gcode's C7 passes only on queryable equivalence: normalized per-table
    fact hashes, synced projections with a fixed graph and retrieval probe
    set, and the vector lane when its preflight passes (GP-A1);
  - each parity+ property names its evidence producer and pass or absence
    condition, measured or not, and the label probes run in their own
    untimed states (GP-A2);
  - 1.1 provisions a fresh runtime root with its own ownership receipts,
    and the September root stays read-only input (GP-A3).
- 2026-10-05: The Adversary gobby#15401's review of `23e7d81790` raised
  GP-A4 to GP-A6, and the Plan Writer accepted all three:
  - 1.1 carries its whole protocol, with sources, and Decision 3 keeps a
    summary; 1.2 states the conforming sorts itself, so neither leaf
    depends on Decision text (GP-A4);
  - the evidence probe follows the operation's atomic-item contract:
    paginated list pages within `max_bytes`, a detail that returns
    `NarrowingRequired` and then fits once narrowed, each request run twice,
    and every member's `content_hash` replayed through a bound `read`
    (GP-A5);
  - community-id stability is checked per C2-C6 case against that case's
    own starting state and the stored watermark, with unexercised remap or
    retirement recorded as such and multi-run history resting on the
    `remap/tests.rs` unit tests (GP-A6).
- 2026-10-05 13:51 CT: V1 consensus between the Plan Writer gobby#15434
  and the Adversary gobby#15401 on `b84096cd6f` (SHA-256
  `2d344705b90c44d908830a2d4e269f94488c89c03fba8b36bdad166d7dc1b410`).
  GP-A1 to GP-A6 are resolved with no disagreement left open, and the
  Adversary's own base validation exits 0 on those bytes. Routing: 1.1 is
  category `test`, assigned `backend-developer`, TDD false, no sibling
  dependencies; 1.2 is category `code`, domain backend, assigned
  `backend-developer`, TDD true, no sibling dependencies. The whole plan
  stays gated on #22862 and on Josh's approval of the stamped plan.

- 2026-10-06: Lane 5 writer gobby#15405 applies the Orchestrator's 15:29 CT
  targeted amendment: comparative interleaved, order-balanced measurements
  replace the quiet-load preflight, and two leaves expand with blocked-by
  #23692 (batch 3 activation) instead of waiting for #22862 to close.
  The dated 2026-10-05 consensus above remains historical evidence; this
  amendment supersedes its expansion gate. Adversary gobby#15414's audit
  accepted the comparative protocol and identified A1 (old expansion gate),
  A2 (stale M1 criterion), and N1 (explicit even pair count in acceptance).
  On 2026-10-06 at 16:55 CT, writer gobby#15405 and Adversary gobby#15414
  reached technical consensus on revised SHA-256
  `25ee4cf1c087c5ae3fdeb853c61bd8dfdb7990a790e8e46e7e3e5a21b68f1a20`:
  A1, A2 and N1 are resolved with no blocking amendment findings. A2's
  service-derived M1 matches all nine narrative criteria. Both existing
  routes, Targets, other acceptance items, and the separate #22958 plan
  remain intact. This consensus entry receives a fresh service-derived M1;
  operational GO remains with Lane 7 and commit timing with the Merge Manager.

## V2: Verification
`kind: verification`

After both leaves pass:

1. Confirm the report's C1-C8 tables trace to
   `/Users/josh/Projects/gcode-graphify-rerun-2026-10/results/index-comparison/`.
2. Rerun `cargo test -p gobby-code --lib commands::graph::view`.
3. On a promoted binary, `gcode graph view --view=communities --format json`
   on the main checkout lists equal-size communities in ascending id order.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Rerun the Graphify comparison on current gcode
  category: test
  task_type: chore
  depends_on: []
  validation_criteria: '1.1.1: The report records the fresh runtime root and its ownership
    receipt, the dated script copies'' SHA-256 values, both tools'' identities (gcode
    version, contract, and SHA-256 of the copied installed binary; Graphify''s PyPI
    version and executable and lock SHA-256), the corpus commits, and at least five
    interleaved, order-balanced pairs for each timed comparison (an even number, six
    minimum, with equal AB/BA orders), with the load recorded at each run, raw samples,
    both tools'' p50 timings and the p50 paired difference. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.

    1.1.2: The report gives C1 to C6 for both tools in the 09 table shape (seconds,
    changed files, symbols and chunks for gcode; nodes, edges and uncached files for
    Graphify) beside the 09 numbers, and states that gcode''s C2 to C6 include the
    community refresh. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.

    1.1.3: The report gives C7 for both tools: the marker each used, the one planned
    `SIGTERM`, any safety `SIGKILL`, the native recovery commands, and whether the
    recovered state equals a fresh clean C1 state, by the 09 signature for Graphify
    and by step 4''s fact, projection and vector checks for gcode, with the gcode
    row-count marker recorded as an amendment to the matrix. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.

    1.1.4: The report scores C8''s 14 frozen questions for both tools as in 09, and
    states whether the gcode hybrid lane was included or excluded by the embedding
    preflight. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.

    1.1.5: The report records Graphify''s deterministic community names, whether current
    Graphify emits semantic community names (yes, no, or `blocked-preflight` with
    the reason), and gcode''s count of communities by `label_source`, each from its
    own untimed state, without comparing community counts, and a section that states
    the evidence for Josh''s #22604 decision without making it. behavior: "Decision
    input for #22604" in `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.

    1.1.6: The report gives a verdict for each of the four parity+ properties by step
    7''s producers and conditions, says whether each was measured in this run, records
    every remap or retirement behavior no case exercised as `not-exercised`, and records
    the label-admission property as missing while #22604 is held. file: `docs/evidence/gcode-graphify-rerun-2026-10/report.md`.'
  labels:
  - covers:gcode-graphify-parity-delta:1.1:1.1.1
  - covers:gcode-graphify-parity-delta:1.1:1.1.2
  - covers:gcode-graphify-parity-delta:1.1:1.1.3
  - covers:gcode-graphify-parity-delta:1.1:1.1.4
  - covers:gcode-graphify-parity-delta:1.1:1.1.5
  - covers:gcode-graphify-parity-delta:1.1:1.1.6
  tdd: false
  source_section: '1.1'
  assigned_agent: backend-developer
- title: Order size ties by community id in the graph views
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.2.1: `build_view_payload` lists two communities of equal
    size with ids 10 and 2 as `community:2` then `community:10`, after any larger
    community. test: `crates/gcode/src/commands/graph/view/render_tests.rs::view_orders_size_ties_by_numeric_community_id`.

    1.2.2: The communities list view returns `["community:2", "community:10", "community:1"]`
    for the existing three-community fixture. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::list_orders_communities_by_size_then_community_id`.

    1.2.3: An MCG view over stored communities 10 and 2 of equal size lists `community:2`
    first, in the payload and in the Mermaid subgraph order. test: `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_orders_size_ties_by_community_id`.'
  labels:
  - covers:gcode-graphify-parity-delta:1.2:1.2.1
  - covers:gcode-graphify-parity-delta:1.2:1.2.2
  - covers:gcode-graphify-parity-delta:1.2:1.2.3
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
```
