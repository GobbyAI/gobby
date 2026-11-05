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
`gcode evidence`. gcode is now 1.9.103. Two scorecard rows have moved. #22862
(gcode community refresh: measure the unchanged-file cost against the +250 ms
budget, add the input-digest skip if over) and #23638 (Daemon gcode index times
out after 30s almost only on the lane-6-everything-else worktree) brought the
refresh to +120.495 ms p50, within budget on the activated implementation
(`docs/evidence/community-labels-2026-09/refresh-latency.md`, lines 109-110).
#22604 landed the label gate with every consumer `off` (Decision 8).

**What remains.** P1 is closed: 1.2 delivered the read order, and 1.1 closed
without delivering its measurement (Decision 2). Three things are left, and Josh
decided all three on 2026-11-05 (Decision 12):

- Every head-to-head number is stale. The September bakeoff
  (`docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md`) measured
  gcode 1.7.0 against Graphify 0.9.55, before the community work existed.
  Its gcode C7 (interruption and recovery) never ran. Four October roots
  stopped before any timed run. Nobody may claim parity+ in writing until a
  rerun records current numbers (2.1 and 2.2).
- The label gate has no quality evidence. #22604 closed without its study, so
  `docs/evidence/community-labels-2026-09/label-quality.md` does not exist and
  the ship decision is open (2.3).
- Box has no live consumer evidence. Its 2026-10-26 OpenRouter attempt returned
  HTTP 400 on a 64x64 image, under Box's 280 px minimum, and the Orchestrator's
  2026-11-05 rerun on a 512x512 image returned 200. The capture script's Box
  request bypasses the registered `gobby-decisions:evaluate` tool (2.4), and the
  wire note names no consumer per decision shape (2.5). The service already
  refuses an image outside 280 to 1568 px per side before dialing, but its error
  names neither the image's size nor the bounds (2.6, which the Orchestrator
  filed as #25788).

**When the leaves close:**
- A provisioning record shows a fresh isolated harness, the Orchestrator's
  exact Docker invocations and service receipts, a live isolated daemon, and
  C0 passed for both tools (2.1).
- A dated evidence report records C1 to C8, including C7 for both tools, for
  current gcode against the current Graphify release, with interleaved,
  order-balanced measurements and recorded load, on the frozen Game Goblins
  corpus. It also records the community-label facts and a verdict for each
  parity+ property (2.2).
- A label-quality report records Josh's ratings of 30 sampled communities and
  the ship rule they select, with every decision consumer `off` (2.3).
- The capture script sends its one Box request through the registered
  `gobby-decisions:evaluate` tool and keeps the raw exchange (2.4).
- A dated `clef:27b` capture on local Ollama records Box's round trip through that
  tool or its finding, and the wire note names a consumer and live-path test per
  shape (2.5).
- A Box image outside 280 to 1568 px per side is refused before dialing with an
  error naming its size and the bounds (2.6).

## Decision Record
`kind: framing`

The Orchestrator gobby#14972 ruled on the scope questions on 2026-10-05 at
13:06 CT (Q1 to Q6 below). The Plan Writer gobby#15434 made the remaining
decisions on 2026-10-05 from the sources cited. Decisions 12 to 18 are the
2026-11-05 refresh: Josh's buttons, relayed by the Orchestrator gobby#19309,
and the Plan Writer gobby#19935's decisions from the sources cited.

1. **Scope (Q1).** The parity+ target is Graphify's community subsystem, as
   the completed plan set it. A broader Graphify capability is built only
   when the plan names its consumer. No capability outside the community
   subsystem has one, so this plan adds none (Decisions 6 and 7).
2. **Both original leaves are expanded and closed, and #24222 did not deliver
   1.1.** The Orchestrator's 2026-10-06 ruling expanded #24222 (Rerun the
   Graphify comparison on current gcode) and #24223 (Order size ties by
   community id in the graph views), each blocked by #23692 (Land batch 3 on
   0.5.0 with one cutover), which closed on 2026-10-07. That gate no longer
   applies. #24223 delivered 1.2, and its three acceptance tests exist.
   #24222 closed `done` on 2026-10-31 at `4babd15570` under the operator's
   "land everything" directive, without delivering acceptance 1.1:
   `docs/evidence/gcode-graphify-rerun-2026-10/report.md` reads "Status:
   partial" and "No timed run has executed", and every C1-C8, label and
   verdict value is TBD. Its `covered` coverage rows matched the report's
   path, so they are not evidence of measurement. The Orchestrator ruled on
   2026-11-05 that this closure does not stand as delivery and that #24222 is
   not reopened. Section 1.1 stays frozen as that leaf's record. Re-expansion
   preserves a closed `done` leaf whose section remains, so any measurement
   work takes a new section id.
3. **The rerun's protocol amends the frozen matrix.**
   `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md` stays the protocol for
   everything leaves 2.1 and 2.2 do not amend. They carry each amendment in
   full, with its sources. In brief:
   - the rerun provisions a fresh runtime root, because the September
     provisioning refuses an existing root and asserts the September owner
     task;
   - Docker, dependency builds and project registration run in the
     Orchestrator's root pane (Decision 13), and the isolated daemon runs in
     the developer's sandbox (Decision 14);
   - each gcode repetition has its own registered project (Decision 16);
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
8. **#22604 closed independently, and the label gate is not live.** #22604
   (Gate generated names through a Jev `choice` decision) did not wait for the
   benchmark. It closed on 2026-10-26 with the Choice gate, Noul carry and
   Score sufficiency landed in `src/gobby/code_index/community_labeler.py`,
   each defaulting to mode `off`. It closed without its label-quality study:
   `docs/evidence/community-labels-2026-09/label-quality.md` does not exist,
   `docs/guides/llm-features.md` says the study and ship decision are pending,
   and Activation Gate item 4 remains unmet on both wires
   (`docs/evidence/decisions/item-4-checklist-2026-10-26.md`). So a generated
   label is still admitted on schema validation alone, and parity+ property 3
   ("a generated label admitted only when a decision model is confident",
   completed plan lines 30-34) does not hold in live configuration.
9. **#22606 keeps its prerequisite.** #22606 (Memory community detection
   over memory_crossrefs) stays blocked by #21563 (Memory and search), as
   deferral D1 of the completed plan set it (lines 1722-1769). The Leiden
   kernel is reachable only in-process from the Rust memory crate. This plan
   neither pulls it forward nor edits it.
10. **#21572 is cross-referenced only.** #21572 (Parity ledger), the
    whole-platform ledger of Rust public surfaces under 1.0, closed on
    2026-10-26. gcode community capabilities were not ledger rows by default,
    and nothing in this plan is a platform-parity obligation. The capabilities
    excluded here (Decisions 6 and 7) are excluded from gcode only, and this
    plan makes no claim about that ledger.
11. **Adjacent work stays separate.** #22958 (Plan code-index symbol-vector
    reconciliation and path-aware content retention) is unrelated to
    communities and is excluded. #22862 closed on 2026-10-07, and its
    measurement stands.
12. **Josh decided the remaining gaps on 2026-11-05.**
    - D1, button "#22948 D1 Graphify comparison: Run it". Leaves 2.1 and 2.2
      run the comparison. The button is also the operator's go for project
      registration in the isolated hub only (Decision 13).
    - D2, button "#22948 D2 label study: A, run the no-decisions study now
      (Recommended)". Leaf 2.3 runs it. This supersedes the 2026-10-26
      selection of completing Activation Gate item 4 first, recorded in
      `docs/evidence/community-labels-2026-09/study-rating-instructions-2026-10-26.md`,
      lines 3-6. The button is also the explicit go that
      `.gobby/plans/community-label-decisions.md` D4 requires before C1 is
      re-registered.
    - D3, the owner of real Box consumer evidence and of the V2 checks that
      `docs/evidence/decisions/item-4-checklist-2026-10-26.md` lists as
      outstanding. Josh answered on 2026-11-05: "On 22948, tell the
      orchestrator to load Clef (not Clef flash) with Ollama and use that".
      The Orchestrator scoped it: "capture the Box evidence on Clef (full, not
      Flash) served locally by Ollama", with model `clef:27b`, and drop the
      OpenRouter dependency. Leaf 2.4 sends the capture's Box request through the
      registered `gobby-decisions:evaluate` tool, leaf 2.5 owns the evidence, and
      the Orchestrator owns its capture step. LM Studio's `clef-flash` is not used. On
      2026-11-05 the Orchestrator reported that its OpenRouter Box rerun on a
      512x512 red-square image returned 200 with edges 0.25/0.74/0.25/0.76,
      that the 2026-10-26 HTTP 400 came from the 64x64 test image, and that
      OpenRouter's catalog lists `cloudflare/clef`, `clef-flash` and
      `clef-omni`. The local `clef:27b` capture stays as ruled. The V2
      checks gate enforcement, and this plan enables none: the
      `--no-decisions` branch ships every consumer `off`, so no leaf needs
      them, parity+ property 3 stays missing, and 2.2 records it so.
13. **The Orchestrator is the only Docker-capable session, and the executor is
    a sandboxed developer.** Spawned seats always run under SRT, and
    `spawn_agent` has no unsandboxed fallback.
    `src/gobby/agents/srt_settings.py::render_srt_settings` sets
    `allowAllUnixSockets` false, so the Docker socket is unreachable. Josh
    ruled on 2026-10-14 (decision `b02931d3`): "Seats cannot run Docker under
    SRT, and the sandbox stays unchanged." The Orchestrator's root pane is
    deliberately unsandboxed (`src/gobby/install/shared/workflows/agents/orchestrator.yaml`,
    line 13). No seat gets Docker, and no sandbox grant is added.

    That ruling has the Orchestrator run only the compose launch. The rerun
    needs three more things from the root pane, and each is refused to a seat:
    - **External-state snapshots** query `docker ps`, `docker volume ls` and
      `lsof`, and the isolation check asserts their exit codes
      (`provision_environment.py::external_state_snapshot`).
    - **Dependency builds.** `worker-safety/no-package-install.yaml` reasons
      that the user manages dependencies. Its pattern does not match `uv sync`
      or `uv venv`, and this plan does not rely on that gap.
    - **Project registration.** `roles/seat-spawn-policy.yaml::seat-no-project-create`
      blocks `gobby init` for seats, because creating a project needs the
      operator's go through the Orchestrator. A `gobby init` hidden inside a
      runner script would evade that rule, so the harness has none.

    All of this runs through three actions of one committed, dated script:
    `launch_services.py provision`, `observe` and `teardown`. Every other step
    stays with the developer. The Orchestrator confirmed this scope on
    2026-11-05: it runs each action from the root pane after reading the
    committed script and checking its SHA-256, and Josh's D1 button is the go
    for registrations in the isolated hub only.
14. **The isolated daemon is mandatory and runs in the developer's sandbox.**
    The October report's "launches no daemon" scope cannot stand. gcode's
    credentials come only through a signed v2 daemon grant, so no gcode case
    runs without one. `python -m gobby.runner` under
    `prepare_daemon.py::isolated_environment` strips every `GOBBY_*` and
    credential variable and sets its own `GOBBY_HOME`. AGENTS.md directs
    anything needing daemon behavior to an isolated test daemon with temporary
    state and ports, and `worker-safety/no-daemon-management.yaml` covers only
    the production daemon's commands. SRT permits loopback binds
    (`allowLocalBinding`) and confines the daemon's writes to the epic folder,
    which matters: September's early daemon probe wrote trust entries into
    global Qwen stores (`environment.md`, lines 48-53). Rejected: the
    Orchestrator launches it, which would run that daemon unsandboxed. The
    Orchestrator confirmed on 2026-11-05 that the developer runs it, with an
    isolated `GOBBY_HOME` and ports 59480-59482.
15. **The runtime root lives in the epic folder.** The root is
    `/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11/`.
    The October roots sat in seat session folders, and a relaunched seat lost
    read access to them. `src/gobby/agents/agent_workspaces.py::spawn_workspace_write_paths`
    grants the epic folder to every seat of a live epic-mode team. `build/` is
    excluded from the workspace's auto-commits, so corpora and
    `config/services.env` stay out of them. The workspace's Git directory is
    separate, so no `.git` sits above the root and the file walkers see the same
    conditions as the September root. The worktree's `var/` path stays rejected:
    the sandbox denied dotfile names there. The scripts themselves are committed
    under `docs/evidence/gcode-graphify-rerun-2026-11/`, because the October
    copies existed only in folders no later seat could read. The Orchestrator
    confirmed this root on 2026-11-05.
16. **Each gcode repetition has its own registered project, created empty.**
    `crates/gcode/src/index/indexer/lifecycle.rs::invalidate` deletes only
    machine state and retains the facts (`code_symbols`, `code_content_chunks`,
    `code_imports`, `code_calls`, `code_inheritance`), as
    `crates/gcode/src/index/indexer/tests/api_contract.rs::invalidate_postgres_deletes_only_machine_state`
    pins. A rebuild after `gcode invalidate` would hit conflict paths, so it is
    not cold. Fact rows are written per `project_id`
    (`crates/gcode/src/index/api.rs`), so a new project starts cold. Registration
    runs with the isolated daemon down, because `src/gobby/cli/init.py::init`
    runs `gcode index` after it registers. Without a daemon that index fails its
    reachability check with `GrantError::DaemonRequired`
    (`crates/gcore/src/grant/acquisition.rs`), `init` reports the failure and
    continues, and the project stays empty; `provision` asserts zero
    `code_indexed_file_states` rows per project.
17. **The Orchestrator runs the study's `prepare`.** `.gobby/plans/community-label-decisions.md`
    has Josh running it. `scripts/community_label_study.py::_load_communities`
    and `::_load_generation` read the live hub and its stored text-generation
    configuration, so `prepare` cannot run under SRT. `--no-decisions` builds
    no decision client and resolves no decision secret, and no key appears on
    its command line. No seat enters or reads a credential. Josh's one manual
    step is rating `sheet.csv`, delivered through the Assistant. The
    Orchestrator confirmed on 2026-11-05 that it runs `prepare` from the root
    pane into the epic folder, and that D2 counts as D4's go for re-registering
    C1 under its existing id.
18. **#24701 (Symbol summaries and community labels) waits on leaf 2.3.** Its
    coordination line reads "Start only after #22604 closes, its Python study
    included". #22604 closed without the study, so the line no longer holds
    the order it names. By the Orchestrator's 2026-11-05 ruling, it edits #24701
    at expansion to be blocked by the 2.3 task. Seats do not edit it.
19. **#25788 (Box decisions reject images under 280 px per side before sending,
    with a clear error) becomes leaf 2.6.** The Orchestrator filed it under
    #22948 on 2026-11-05 and directed it into P2 with no edges. Leaf 2.6
    carries its four validation criteria. At expansion the Orchestrator closes
    #25788 as superseded by the 2.6 leaf, so the section has one implementation
    owner and its coverage labels sit on an open leaf. Seats do not edit it.

## Constraints
`kind: framing`

- The Orchestrator launches all six leaves in a Dev Team in epic mode for
  #22948, the only mode that grants the epic folder. That launch waits on Josh's
  freeze: no new teams until #25765 and #25653 are proven live.
- Leaves 2.1 and 2.2 are heavy measurement work. The fleet's load hold in
  `project_policy` applies, and no timed run happens during
  `project_policy.quiet_hours`. Decision 3's comparative protocol replaces any
  quiet-load preflight.
- Leaves 2.1 and 2.2 use only the fresh root named in Decision 15, with its own
  compose services and Gobby home. They never touch the live daemon, the live
  database, or port 60891, and they never write under the September root. The
  root named in the frozen 1.1 text, under `/Users/josh/Projects/`, was never
  created.
- Only the Orchestrator calls Docker, and it stops only the containers its own
  receipts record. The September harness is torn down: `docker ps -a` listed
  no `wiki-bakeoff` container as of 2026-10-05.
- Operator-gated steps go to the Orchestrator through the Dev Team Lead and
  Triage, never to Josh directly. No seat enters a credential. Josh's only
  manual step in this plan is rating the study sheet, delivered by the
  Assistant.
- A daemon hold pauses a leaf between pairs. It never voids a pair already
  recorded, because the isolated services do not depend on the live daemon.
- No live-daemon restart, push, or live migration is needed.

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
- `crates/gcode/src/commands/graph/view/communities/tests.rs::list_orders_communities_by_size_then_community_id`
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

## P2: The measurement and the label study
`kind: framing`

**Goal**: parity+ claims rest on numbers measured on current gcode, and the
label gate has its quality evidence and ship rule.

**Granularity:** six leaves. Provisioning (2.1) and measurement (2.2) split
at the boundary where all four October roots stopped, so provisioning closes
with its own durable record and a relaunched seat resumes measurement from it.
Each lists more than six files, and the count does not call for a further
split: 2.1's are dated copies of one harness that only runs together, and
2.2's are the runner copies and the records its report traces to. Each leaf
keeps to six acceptance items. The study (2.3) shares no target or service with
the other two, so it has no edge to them. The Box route (2.4) and the Box
capture (2.5) share no target with any of them, so they have no edge to them.
The route is a script change with its own tests and closes on them. The capture
waits on operator steps, and it depends on 2.4 because it runs the script from
the main checkout after 2.4 lands. The two share no target. The Box refusal
detail (2.6) is a product change in `src/gobby/ai/decisions.py` with its own
tests, outside 2.4's script-only scope. It shares no target with any leaf, so it
has no edge (Decision 19).

### 2.1 Provision the rerun harness and pass C0 [category: test]
`kind: deliverable`

Targets:
- `docs/evidence/gcode-graphify-rerun-2026-11/provisioning.md`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_services.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/provision_environment.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/prepare_daemon.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/prepare_identity.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/launch_daemon.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/record_installations.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/validate_environment.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/runtime_boundary.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/service_safety.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/verify_corpora.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/test_runtime_boundary.py`

**Research context:**
- Runtime root, written `<root>` below:
  `/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11/`.
  `src/gobby/agents/agent_workspaces.py::spawn_workspace_write_paths` grants the
  epic folder to the seats of a live epic-mode team, so this leaf runs in one.
  This leaf never touches the live daemon, the live database or port 60891, and
  never writes under the September root.
- Why the earlier attempts stopped:
  `docs/evidence/gcode-graphify-rerun-2026-10/report.md`, section 7. Docker
  denial on `docker image inspect` under SRT; write-once `receipts/gcode.json`
  and `receipts/installations.json` written before validation failed; the
  source manifest's one symlink record,
  `crates/gterminal/vendor/libghostty-vt/CLAUDE.md` (still mode 120000),
  rejected by `runtime_boundary.py::contained_file`; a missing
  `isolation/external-state.after.json`; and a no-daemon scope. Its corpus
  result stands as a method: `init` built the trees from the pinned source
  (base `0216f1e3`, change `8b24ac26`), and 20 cases and 2,984 files matched
  the September manifests with 0 mismatches. Its staged copies are not reused.
- Harness to copy: `docs/evidence/wiki-bakeoff-code-2026-09/`.
  `launch_services.py` has actions `check`, `start` and `observe`.
  `provision_environment.py` has `init`, `refresh-manifest`, `write-compose`
  and `snapshot-after`, and `init_runtime` refuses an existing root. Docker is
  confined to `launch_services.py`, `provision_environment.py::external_state_snapshot`
  and `validate_environment.py` (lines 227-229, 240-242, 496 and 594); the
  September runners contain no Docker call. `validate_external_changes`
  (lines 189-221) carries September's Codex trust-path attribution.
  `runtime_boundary.py::assert_owned_runtime` asserts the owner task and owner
  session. Ports: PostgreSQL 61234, Qdrant 61235/61236, FalkorDB 61237/61238,
  daemon 59480/59481, UI 59482.
- Daemon: `launch_daemon.py::launch` runs `python -m gobby.runner --config
  gobby-home/bootstrap.yaml`, and `src/gobby/runner.py` still takes `--config`.
  `prepare_daemon.py::prepare` writes the bootstrap, applies the schema through
  `gobby.storage.schema_contract.apply_schema`, and patches configuration with
  `ConfigMutations`, both of which still exist. It disables tmux, terminal
  hosting, cron, memory dreams, task expansion and validation, and automatic
  code indexing. `src/gobby/utils/dev.py::worktree_daemon_refusal` refuses only
  a linked-worktree source tree, and an archive is not one.
- Binary set: `src/gobby/storage/schema_divergence.py::binary_set_apply_refusal`
  reports a set as mixed when `.gdaemon-schema-identity.json` is unreadable or
  a member disagrees with it, and `gobby start` consults it. October copied
  `gdaemon` alone, with no stamp.
- Protocol: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`, C0 at lines
  293-336, `command-manifest.json` immutability at lines 287-291, and C7's
  marker rules at lines 565-570. `gobby init --no-linear-setup`, the September
  argv in `casebook.py::initialize_gcode_project`, is gone: the option no
  longer exists on `src/gobby/cli/init.py::init`.
- gcode markers: gcode prints no progress under a pipe
  (`crates/gcore/src/progress.rs::non_terminal_capture_suppresses_output`). It
  keeps state in PostgreSQL and commits each file in its own transaction
  (`crates/gcode/src/index/indexer/file.rs::index_file`), so the project's
  `code_indexed_file_states` row count rises during a cold index. September
  blocked its gcode C7 for lack of a marker
  (`graphify-gcode.md`; `run_c7.py::record_gcode_disposition`).

**Implementation:**
1. **Dated copies.** Copy the harness files into
   `docs/evidence/gcode-graphify-rerun-2026-11/harness/` and commit them. The
   September scripts and results stay unchanged. Every isolation, corpus,
   service-boundary, containment and receipt check stays. The copies may
   change only:
   - the runtime root to `<root>`, the owner task to `#22948`, the compose
     project to `gcode-graphify-rerun-2026-11` (containers, volumes and network
     derive from it), the database name and account email, and the tool,
     source and contract pins. Graphify is unpinned: the PyPI `graphifyy`
     package, recorded by version, executable SHA-256 and lock SHA-256.
     Comparators narrow to gcode and Graphify (Orchestrator ruling 3,
     2026-10-28). Ports stay. Images stay at the September digests unless
     `apply_schema` fails on one; then that image moves to the digest the
     Orchestrator reads from the live `gobby-postgres-local:18-pgsearch`, and
     the record says so.
   - `launch_services.py`, which gains the three actions in step 3 and becomes
     the only file that calls Docker, builds a dependency environment, or runs
     `gobby init`. `provision_environment.py::init_runtime` and
     `::snapshot_after` no longer take snapshots themselves.
   - `validate_environment.py`, which reads the `observe` receipt in place of
     its Docker probes, drops the `opendeepwiki` assertions, and replaces the
     September trust-path attribution with the same reconstruction for this
     run: a Codex configuration difference passes only when removing that
     run's observed worktree trust entries reproduces the before bytes. Any
     other difference fails and is listed.
   - `record_installations.py`, which validates before it writes its
     write-once receipts, so a failed validation leaves none behind.
   - source-manifest symlink entries, recorded as `{path, symlink, target}` and
     verified with `os.readlink`. `contained_file` keeps refusing symlinks for
     content reads.
   - `prepare_daemon.py`'s configuration patch, which drops any key the current
     configuration schema no longer has, and the record lists each one dropped.
   - a new read-only `verify_corpora.py`, which checks each case against its
     September manifest by hash, size and mode and reports extra files.
2. **Developer setup.** Run `provision_environment.py init` with the developer's
   session as owner, install the committed copies byte-identical into
   `<root>/scripts/`, then run `write-compose` and `verify_corpora.py`. `init`
   builds the corpora from the pinned commits and archives `0.5.0` at the run's
   commit into `sources/gobby`. Copy `gcode`, `gdaemon`, `ghook` and
   `.gdaemon-schema-identity.json` from `~/.gobby/bin/` into
   `<root>/gobby-home/bin/` as one set, by new inode, then confirm
   `binary_set_apply_refusal(<root>/gobby-home/bin)` returns `None`. Record each
   SHA-256 from the isolated directory. `gdaemon schema version --json` and the
   archived checkout's schema head must agree, and `crates/gcode` must carry the
   installed version; a mismatch stops the leaf and asks the Orchestrator for its
   next cutover, and the leaf builds no binaries. Write
   `config/project-manifest.json`, one entry per gcode project (name, case,
   repetition, workspace path), and materialize each workspace from its sealed
   corpus: six repetitions and two spares for each of C1 to C6 (48), plus the
   C7 interrupted and clean states, C8-baseline and C8-change, the clean C1 state
   for labels and properties, and the C0 probe (6), for 54 projects.
3. **Orchestrator actions.** Send the Orchestrator, through the Dev Team Lead
   and Triage, each exact invocation and the SHA-256 of
   `<root>/scripts/launch_services.py`. It reads that copy, then runs it from the
   main checkout with `/Users/josh/Projects/gobby/.venv/bin/python`. No seat
   calls Docker. Josh's D1 button, "#22948 D1 Graphify comparison: Run it", is
   the operator's go for the registrations in `provision`, in the isolated hub
   only.
   - `provision --owner-session <session>`: writes
     `isolation/external-state.before.json`; runs the existing `check`, `start`
     and `observe`, which write the compose and service receipts; builds
     `tools/graphify/venv` with the latest `graphifyy` and
     `tools/gobby/venv` with `uv sync --frozen` from `sources/gobby`; runs
     `prepare_daemon.py` and `prepare_identity.py`; runs `gobby init --name
     <name>` in each manifest workspace under `isolated_environment`, with the
     daemon down; and asserts zero `code_indexed_file_states` rows per project. A
     step whose receipt exists and verifies is skipped, so it is safe to rerun
     after a corrected copy.
   - `observe --owner-session <session>`: writes the Docker, volume and listener
     observation and `isolation/external-state.after.json`.
   - `teardown --owner-session <session>`: writes the final snapshot and stops
     only the containers and volumes the receipts record. Leaf 2.2 sends this.

   The Orchestrator returns the exit code and the receipt paths. Endpoints and
   connection details come from those receipts and `config/services.env`.
4. **Isolated daemon.** Run `launch_daemon.py <attempt>` under SRT, with no
   `GOBBY_ALLOW_WORKTREE_DAEMON`. It writes `receipts/daemon-launch-<attempt>.json`.
   `isolated_environment` already removes provider CLIs from `PATH`.
5. **C0.** Save stdout, stderr, exit code and environment diff for the matrix's
   commands: Graphify `--version`, `--help`, and `extract <workspace> --code-only
   --no-cluster --out <state>`; gcode `--version`, `--format json contract`,
   `schema-identity --json`, `--format json embeddings doctor`, and `--format json
   status`. Then:
   - Capture Graphify's command inventory and select
     `GRAPHIFY_NATIVE_CONCURRENCY`.
   - Rerun the embedding-identity preflight. The hybrid lane joins C8 only if it
     passes.
   - Observe a pre-completion Graphify marker for this release; the September regex
     is reused only if it is observed again.
   - Establish gcode's C7 marker: a sidecar poller counts the C0 project's
     `code_indexed_file_states` rows in the isolated PostgreSQL, and the marker is
     that count above 0 and below the clean file count. Run one cold index under
     the poller and confirm such a count appears before it completes. The marker
     changes neither corpus nor source. No production seam is added.
   - Name in advance the C7 probe set: the C8 deterministic retrieval queries and
     `gcode graph` reads for five corpus symbols.
   - Freeze `command-manifest.json` with each exact argv and any replacement, then
     treat it as immutable. A missing command is demonstrated unsupported, with no
     hand-authored substitute.
6. **Validate.** Ask the Orchestrator for `observe`, then run the dated
   `validate_environment.py --live`. It refreshes the native grant through the
   copied gcode, checks grant version 2, expiry, and project and machine binding,
   presents the signed grant to `/api/runtime/config` (200, and 403
   `invalid_signature` for a changed signature), and connects to PostgreSQL, Qdrant
   and FalkorDB, printing no credential. Stop the daemon. Compose stays up for 2.2.
7. Write `provisioning.md`, including the deviation Josh's ruling requires: who
   launched, each exact command, the copy's SHA-256, and the service receipts.

**Acceptance:**

- 2.1.1 - The record gives the runtime root and its ownership receipt with owner task `#22948`, the commit and SHA-256 of every dated copy, and each edit against its September copy, with no check removed beyond the Docker probes replaced by the `observe` receipt and the `opendeepwiki` assertions. file: `docs/evidence/gcode-graphify-rerun-2026-11/provisioning.md`.
- 2.1.2 - The record states the deviation: the Orchestrator ran `provision` and `observe` from the main checkout, with each exact invocation, the `launch_services.py` SHA-256 and the service receipts, and no seat called Docker. file: `docs/evidence/gcode-graphify-rerun-2026-11/provisioning.md`.
- 2.1.3 - The record shows the corpus commits and the verification of cases C0 to C7, C8-baseline and C8-change against the September manifests with 0 mismatches, and all 54 manifest projects registered with zero `code_indexed_file_states` rows before the daemon started. file: `docs/evidence/gcode-graphify-rerun-2026-11/provisioning.md`.
- 2.1.4 - The record shows the isolated binary set's SHA-256 values and stamp, `binary_set_apply_refusal` returning `None`, the daemon launch receipt under SRT with no worktree override, and a passing `validate_environment.py --live` covering the signed grant, its rejected tampered copy and all three services. file: `docs/evidence/gcode-graphify-rerun-2026-11/provisioning.md`.
- 2.1.5 - The record gives C0 for both tools: gcode version, contract and schema identity; Graphify's PyPI version and executable and lock SHA-256; the embedding preflight's outcome; both C7 markers as observed before completion; the probe set; and the SHA-256 of the frozen `command-manifest.json`. file: `docs/evidence/gcode-graphify-rerun-2026-11/provisioning.md`.
- 2.1.6 - A source-manifest symlink record passes only when its stored target equals `os.readlink`, and `contained_file` still refuses that path for a content read. test: `docs/evidence/gcode-graphify-rerun-2026-11/harness/test_runtime_boundary.py::test_symlink_record_verifies_target_without_following`.

**Verification:** `uv run pytest docs/evidence/gcode-graphify-rerun-2026-11/harness/test_runtime_boundary.py`
with `GOBBY_TEST_PROTECT=1`, and the dated validator exits 0 against the final C0
identities and schema.

### 2.2 Measure C1 to C8 and report the verdicts (depends: 2.1) [category: test]
`kind: deliverable`

Targets:
- `docs/evidence/gcode-graphify-rerun-2026-11/report.md`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/casebook.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/run_index_cases.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/run_c7.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/run_c8.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/run_c8_deterministic.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/harness/analyze_evidence.py`
- `docs/evidence/gcode-graphify-rerun-2026-11/results/measurements.json`
- `docs/evidence/gcode-graphify-rerun-2026-11/results/run-records.normalized.jsonl`
- `docs/evidence/gcode-graphify-rerun-2026-11/results/C7-summary.json`
- `docs/evidence/gcode-graphify-rerun-2026-11/results/C8-scores.json`

**Research context:**
- Runtime root, written `<root>` below:
  `/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/gcode-graphify-rerun-2026-11/`.
  `src/gobby/agents/agent_workspaces.py::spawn_workspace_write_paths` grants the
  epic folder to the seats of a live epic-mode team, so this leaf runs in one. It
  holds `ownership.json`, `config/project-manifest.json` and the results.
- Run constraints: this leaf never touches the live daemon, the live database or
  port 60891, and never writes under the September root. The fleet's load hold in
  `project_policy` applies, and no timed run happens during
  `project_policy.quiet_hours`. A daemon hold pauses the leaf between pairs and
  never voids a pair already recorded, because the isolated services do not depend
  on the live daemon.
- Prior run: `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md` (C0-C9
  table at lines 62-75, C1-C6 measurements at lines 80-94, frozen identities at
  lines 42-48). Runners and results sit in
  `/Users/josh/Projects/wiki-bakeoff-code-2026-09/results/index-comparison/`:
  `casebook.py`, `run_index_cases.py` (C1-C6), `run_c7.py`, `run_c8.py`,
  `run_c8_deterministic.py`, `analyze_evidence.py`, `measurements.json`,
  `run-records.normalized.jsonl`, `C7-summary.json` (Graphify's clean and recovered
  signatures) and `C8-questions.json`. That root is read-only input.
- September's commands: gcode `gcode --project <workspace> --format json index
  <workspace> [--full] --sync-projections`, then `status`, under
  `casebook.py::isolated_gcode_environment`. Graphify `update <workspace>
  --no-cluster` (`--force` for C6), `check-update <workspace>`, and `cluster-only
  <state> --no-label`, with `GRAPHIFY_OUT` and `GRAPHIFY_NATIVE_CONCURRENCY`. Current
  `gcode index --help` still lists `--full`, `--sync-projections` and `--project`.
  `run_index_cases.py::run_cases` uses `casebook.py::initialize_gcode_project`, which
  this leaf removes, because `roles/seat-spawn-policy.yaml::seat-no-project-create`
  blocks `gobby init` for seats: 2.1 registered the projects.
- Protocol: `docs/evidence/wiki-bakeoff-code-2026-09/matrix.md`. C2 to C6 (lines
  365-562) are separate one-step lineages, not one serial state: C2 copies the
  sealed C1 state, C3 runs on an independent state built from the baseline, and C4
  to C6 each start from a fresh baseline copy. C7 is at lines 563-696. Its wrapper
  starts a separate process group, waits for the marker, sends one `SIGTERM`, and
  uses `SIGKILL` only after a 30 s deadline. Native recovery is at lines 671-685,
  and the queryable-result requirement at lines 688-695.
- gcode facts: gcode syncs the graph and vector projections only after the facts
  commit (`crates/gcode/src/commands/index.rs::run_index_locked`). The community
  refresh runs inside `gcode index` through
  `crates/gcode/src/communities.rs::refresh_project_communities`, which did not
  exist in the September run, so C2 to C6 now include it.
- Community ids: `crates/gcode/src/communities/remap.rs::assign_ids` keeps the id of
  the prior community that a new community matches by Jaccard overlap, and gives each
  unmatched community an id above both every prior id and the stored watermark
  `code_indexed_project_states.community_id_watermark`, which never falls. Unit tests
  in `crates/gcode/src/communities/remap/tests.rs` cover history across runs:
  `retired_ids_are_never_reissued`, `watermark_is_monotone_across_runs`, and
  `jaccard_beats_raw_overlap_on_swallow_case`.
- Community evidence: the `communities` operation of `gcode evidence` lists communities
  with empty `members`. With a `community_id` selector it returns that one community,
  whose `members` carry `path` and `content_hash`, cut to `max_members` (1 to 500,
  default 50) with `members_truncated` set when cut
  (`crates/gcode/src/evidence/communities.rs`). Items are atomic:
  `crates/gcode/src/evidence/mod.rs::EvidenceLibrary::paginate` returns
  `NarrowingRequired` when any item exceeds `max_bytes` (default 16,384), and otherwise
  fills a page with whole items whose summed serialized bytes, reported as
  `bounds.serialized_item_bytes`, stay within `max_bytes`. The response envelope is not
  counted. A `read` request of `kind: range` returns the file's indexed `content_hash`,
  and fails when the file's bytes do not hash to it
  (`crates/gcode/src/evidence/source.rs`).
- Label gate: #22604 landed the Choice gate, Noul carry and Score sufficiency in
  `src/gobby/code_index/community_labeler.py`, each defaulting to mode `off`
  (`docs/guides/llm-features.md`, lines 177-192). So a generated label is admitted on
  schema validation alone. The isolated daemon runs no labeler, because
  `prepare_daemon.py` disables automatic code indexing, so its `label_source` counts
  are context.
- September numbers to compare against: C1 gcode 35.789 s versus Graphify 2.430 s; C2
  0.117 s versus 2.108 s; C3 3.121 s versus 2.533 s; C4-C6 0.42-0.98 s versus 2.43-2.49
  s; C8 6/14 for both. Community counts are not comparable: Graphify clusters 2,308
  mixed nodes, gcode clusters 142 files
  (`docs/evidence/community-labels-2026-09/thresholds.md`, lines 192-197).

**Implementation:**
1. **Runner copies.** Copy the runners into
   `docs/evidence/gcode-graphify-rerun-2026-11/harness/` and commit them. The copies may
   change only the root and tool paths, the executable identities, the registration
   (each repetition reads its project from `config/project-manifest.json` instead of
   running `gobby init`), and the pair and repetition loop in step 3. Results land in
   `<root>/results/index-comparison/`. Start a fresh isolated daemon with
   `launch_daemon.py <attempt>`, and rerun `validate_environment.py --live` against the
   latest `observe` receipt. A relaunched seat restarts here under a new attempt id and
   passes the owner session recorded in `ownership.json`. Before continuing, it inventories
   completed valid pairs and consumed projects from
   `<root>/results/index-comparison/run-records.normalized.jsonl` and
   `config/project-manifest.json`. Each completed pair stays unchanged and counts once. An
   interrupted or incomplete pair is recorded void, and the whole pair is replaced on an unused
   spare in the same AB or BA slot. With no spare left, the seat stops, adds the manifest
   entries, stops the isolated daemon, and asks the Orchestrator through the Dev Team Lead and
   Triage to rerun `provision`. New projects have no cached grant
   (`crates/gcore/src/grant/cache.rs::interactive_cache_path` keys on the project id), so their
   registration stays empty as in 2.1. The runner loop and its records carry all of this, with no
   separate checkpoint store.
2. **Starting states.** Each case starts from the state the matrix names, in that
   repetition's own project and Graphify state directory, with the starting state built
   untimed. For C3, replace the workspace from its sealed change corpus and check the
   hash; for C4 to C6, apply the frozen fixture; otherwise verify the workspace.
3. **C1 to C6** for both tools, with the frozen argv. For each timed comparison, run six
   interleaved pairs of gcode and Graphify, three in each tool order. Every repetition
   starts from an independent copy of the case's starting state and never shares a
   mutated state. A failed repetition voids its pair, is recorded with its reason, and
   reruns on a spare project. Record pair id, tool order, exact argv, raw timing, and
   the 1-, 5- and 15-minute load averages immediately before each run. Report both tools'
   p50 timings and the p50 paired difference, with the raw samples. This comparative
   protocol replaces the 5-minute load-under-5 preflight, per the Orchestrator's
   2026-10-06 15:29 CT ruling. Apply the same pairing and isolation to any timed C7 or C8
   comparison. For gcode's C2 to C6, read the state's `code_communities` rows and its
   `community_id_watermark` before the timed command starts and after it ends, outside the
   timed window.
4. **C7** for both tools, each from a fresh baseline copy and fresh isolated state, through
   the dated wrapper and the C0 markers. Preserve the partial state and logs after the one
   `SIGTERM`. Recover each tool exactly once with its native command:
   - Graphify: `update --no-cluster`, then `check-update`, against the interrupted output.
   - gcode: `index --sync-projections` without `--full`, then `status`.

   Build a separate fresh clean C1 state for each tool. Graphify's recovered state passes
   when it matches that state by the September signature (`C7-summary.json`). gcode's
   recovered state passes only when all of these hold between it and its clean state:
   - **Facts.** For each fact table the index writes for the project
     (`code_indexed_file_states`, `code_symbols`, `code_content_chunks`, `code_imports`,
     `code_inheritance`, `code_calls`, and `code_communities`), the sorted rows the project
     owns or references hash equal per table, after project id, root path, row UUIDs and
     timestamps are normalized.
   - **Projections.** The recovery's `index --sync-projections` response reports both
     projections synced. The C0 probe set returns byte-equal normalized output on both
     states, including `gcode graph` reads (FalkorDB-backed,
     `crates/gcode/src/commands/graph/reads.rs`).
   - **Vectors.** The C8 hybrid queries also match when the embedding preflight passed. When
     it failed, the vector projection is recorded `not-compared` with the preflight's reason.

   Equal counts never pass C7 on their own. A degraded or unsupported outcome is recorded as
   such. Record the exact interruption, recovery and clean-build argv, and every adaptation,
   with gcode's row-count marker recorded as an amendment to the matrix.
5. **C8**: the 14 frozen questions, scored as in September.
6. **Labels**, each in its own untimed state, apart from the timed `--code-only --no-cluster`
   C1 state (`matrix.md`, lines 338-350):
   - Graphify deterministic names: `extract --code-only` with clustering on, into its own
     output directory.
   - Graphify semantic names: the matrix's C1 semantic stage (`extract --backend … --model …`)
     into its own output directory. It runs on its native provider path, or it is recorded
     as `blocked-preflight` with the reason; no substitute is used.
   - gcode: each community's label and `label_source` from `gcode graph view
     --view=communities --format json` on the clean C1 state.
7. **Parity+ properties.** Each verdict names its evidence producer and its pass or absence
   condition, and says whether it was measured in this run or rests on earlier evidence:
   - **File-level projection, externals excluded.** Measured on the clean C1 state: every
     member in `code_communities` is a file path that has a `code_indexed_file_states` row
     for the project, and none is an external module.
   - **Monotone ids with Jaccard remap.** Measured per case from step 3's reads, each C2-C6
     case against its own starting state only. C2 passes when it leaves every
     `(community_id, member_signature)` pair and the watermark unchanged. Each of C3 to C6
     passes when a community whose member set is unchanged keeps its id, every id absent from
     the starting state is above the starting watermark, and the watermark does not fall. An
     id absent from the starting state and at or below its watermark is a recycled id, and one
     is an absence. Each case records its counts of kept, remapped (id kept, members changed,
     with the Jaccard of the old and new member sets), retired, and new communities. A case
     whose remapped or retired count is 0 records that behavior `not-exercised`. No matrix case
     spans two refreshes of one state, so a retired id staying retired across runs rests on the
     `remap/tests.rs` unit tests above, as earlier evidence.
   - **Confident label admission.** Missing in live configuration: the gates landed with every
     consumer `off`, so a generated label is admitted on schema validation alone with
     `label_confidence` empty, and leaf 2.3's `--no-decisions` branch keeps them `off`. The
     `label_source` counts are context only.
   - **Byte-bounded, replay-verifiable evidence.** Measured with the `communities` evidence
     operation on the clean C1 state, every request bound to that state's commit and tree. Each
     distinct request (binding, selector, and `max_bytes`) runs twice, and both runs return
     identical `items`, `bounds`, and `continuation`, or the identical error.
     - List: at the default `max_bytes`, and at a `max_bytes` no smaller than the largest list
       item and below the complete list's summed item bytes, so that the list spans at least two
       pages. Each continuation is followed to the end. Every page reports
       `bounds.serialized_item_bytes` within `bounds.max_bytes`, and the pages together return
       every community exactly once, matching `bounds.total_items`.
     - Detail: the first community in list order, with `max_members` at its member count (at
       most 500). A default-budget request gives its full item size:
       `bounds.serialized_item_bytes` when it succeeds, or the error's `item_bytes` when it returns
       `NarrowingRequired`. The same request at a `max_bytes` one byte below that size returns
       `NarrowingRequired` naming the community's `evidence_id`. At that `max_bytes`, a request with
       `max_members` low enough for the item to fit succeeds, with `members_truncated: true` and
       `bounds.serialized_item_bytes` within `max_bytes`.
     - Replay: for every member a successful detail returns, a one-line `range` read of its `path`
       against the same binding succeeds and returns the member's `content_hash`.

     It passes when all of these hold. Identical `evidence_id` lists alone show stable identity,
     not verifiable content.

   Index and retrieval scores never stand in for a property verdict.
8. **Close the harness.** Stop the isolated daemon. Ask the Orchestrator, through the Dev Team
   Lead and Triage, for `observe`, then `teardown`. Commit the normalized records under
   `docs/evidence/gcode-graphify-rerun-2026-11/results/`, because `build/` is not committed.
9. Write `docs/evidence/gcode-graphify-rerun-2026-11/report.md`: identities (citing
   `provisioning.md`), the load samples, the C1-C8 tables beside the September numbers (C9 is
   not rerun), the C7 method with the amendments, the label facts, and the four property
   verdicts.

**Acceptance:**

- 2.2.1 - The report gives both tools' identities from `provisioning.md`, the corpus commits, and six interleaved pairs for each timed comparison with equal AB and BA orders, with the load recorded at each run, raw samples, both tools' p50 timings, the p50 paired difference, and each voided pair with its reason. file: `docs/evidence/gcode-graphify-rerun-2026-11/report.md`.
- 2.2.2 - The report gives C1 to C6 for both tools in the September table shape (seconds, changed files, symbols and chunks for gcode; nodes, edges and uncached files for Graphify) beside the September numbers, and states that gcode's C2 to C6 include the community refresh. file: `docs/evidence/gcode-graphify-rerun-2026-11/report.md`.
- 2.2.3 - The report gives C7 for both tools: the marker each used, the one planned `SIGTERM`, any safety `SIGKILL`, the native recovery commands, and whether the recovered state equals a fresh clean C1 state, by the September signature for Graphify and by step 4's fact, projection and vector checks for gcode, with the gcode row-count marker recorded as an amendment to the matrix. file: `docs/evidence/gcode-graphify-rerun-2026-11/report.md`.
- 2.2.4 - The report scores C8's 14 frozen questions for both tools as in September, and states whether the gcode hybrid lane was included or excluded by the embedding preflight. file: `docs/evidence/gcode-graphify-rerun-2026-11/report.md`.
- 2.2.5 - The report records Graphify's deterministic community names, whether current Graphify emits semantic community names (yes, no, or `blocked-preflight` with the reason), and gcode's count of communities by `label_source`, each from its own untimed state, without comparing community counts. file: `docs/evidence/gcode-graphify-rerun-2026-11/report.md`.
- 2.2.6 - The report gives a verdict for each of the four parity+ properties by step 7's producers and conditions, says whether each was measured in this run, records every remap or retirement behavior no case exercised as `not-exercised`, and records the label-admission property as missing in live configuration. file: `docs/evidence/gcode-graphify-rerun-2026-11/report.md`.

**Verification:** every number in the report traces to a committed record under
`docs/evidence/gcode-graphify-rerun-2026-11/results/` named in the report, and the Orchestrator's
`teardown` receipt lists the containers and volumes it stopped.

### 2.3 Run the no-decisions label study and record the ship rule [category: test]
`kind: deliverable`

Targets:
- `docs/evidence/community-labels-2026-09/label-quality.md`
- `docs/evidence/community-labels-2026-09/study-2026-11/sheet.csv`
- `docs/evidence/community-labels-2026-09/study-2026-11/sheet.rated.csv`
- `docs/evidence/community-labels-2026-09/study-2026-11/key.json`
- `docs/evidence/community-labels-2026-09/study-2026-11/run.json`
- `docs/evidence/community-labels-2026-09/study-2026-11/label-quality-results.md`
- `docs/evidence/community-labels-2026-09/study-rating-instructions-2026-10-26.md`
- `docs/guides/llm-features.md`

**Research context:**
- Execution context: the output directory in step 2 sits in the epic folder.
  `src/gobby/agents/agent_workspaces.py::spawn_workspace_write_paths` grants that folder only to the
  seats of a live epic-mode team, so this leaf's developer and reviewer run in the #22948 epic-mode team.
- Package: `docs/evidence/community-labels-2026-09/study-rating-instructions-2026-10-26.md`. Its
  temporary directory no longer exists, so this run needs new output paths. The scripts are
  tracked: `scripts/community_label_study.py`, SHA-256
  `07a77db40dac6ac83cb388fc05a10e0aa81c64c0aab0f0a5acf7780079784552`, and
  `scripts/decisions_live_capture.py`, SHA-256
  `1df0ba09baf98b052842cbe186cc8c245d4ad66865631af4ce01d0be6accd0b7`, both verified on 2026-11-05.
  Their offline test is `tests/scripts/test_community_label_study.py`.
- Sampling (`scripts/community_label_study.py::_sample`, `::CORPORA`): communities with at least 3
  members, split into size terciles, five per tercile, seed 22604, 15 per corpus across `gobby`
  and C1, so 30 in total. Fewer than 15 in either corpus exits nonzero. Identical deterministic and
  generated names share one row, so `--no-decisions` produces 30 to 60 rows with two ratings each.
- `_prepare`: `_load_communities` reads structural rows from the live hub in a read-only transaction,
  `_load_generation` reads the hub's stored text-generation configuration, and label attempts and
  writes go to an in-memory `RecordingStorage`. It performs inference, and it changes no live label
  or configuration. With `--no-decisions` it builds no decision client. Outputs: `sheet.csv`,
  `key.json` and `run.json`.
- C1: `.gobby/plans/community-label-decisions.md`, D4 (lines 109-118 and 238-244). Corpus
  `/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/gcode/C1`, project id
  `c8e071af-e1f7-434f-b3c0-f31a1b1299c9`, name `gcode-bakeoff-C1`. Josh's D2 button, "#22948 D2 label
  study: A, run the no-decisions study now (Recommended)", is the explicit go D4 requires. The
  Orchestrator re-registers and re-indexes that existing id, never a fresh init. After this leaf lands
  it soft-deletes the registration with `gobby projects delete`; the folder and marker stay.
- Ship rules (same plan, lines 973-987): rule 1, G regresses on at most 1 of 30, and the gate ships
  `off`; rule 2, enforce if J passes its bars; rule 3, deterministic names ship with
  `code_index.community_label.enabled: false` and every decision consumer `off`. "A `--no-decisions`
  run has no J, carry or sufficiency data, so rule 2 is unreachable and every consumer ships `mode:
  off`. Rule 1 or rule 3 then decides `enabled`." `score` proposes the configuration without applying
  it, and live configuration is operator-side.
- `docs/guides/llm-features.md` says the live study and ship decision are pending, and places the
  report at `docs/evidence/community-labels-2026-09/label-quality.md`.

**Implementation:**
1. Run the offline test and confirm both script hashes. If either script has changed, record the new
   hash; the test passing is what admits it.
2. **Orchestrator steps.** Send these through the Dev Team Lead and Triage, with the exact commands.
   The output directory is
   `/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/label-study-2026-11/`,
   and it must not already exist.
   - Re-register and re-index C1 under its existing id, and confirm by read-only count at least 15 C1
     communities and 15 `gobby` communities with 3 or more members.
   - Run, from the main checkout: `PYTHONPATH=/Users/josh/Projects/gobby/src uv run --project
     /Users/josh/Projects/gobby python /Users/josh/Projects/gobby/scripts/community_label_study.py prepare
     --wire-api systemone --api-base http://127.0.0.1:11434 --model clef:27b --identity-contract
     response_version --no-decisions --out <dir>`. The parser requires the decision flags, and
     `_prepare` builds no decision configuration under `--no-decisions`, so they are inert.
3. **Check the sample.** Read `run.json`, and report the generation-failure count and the exact CSV row
   count. A failed generation collapses an item to its deterministic name, which is not evidence of
   generation quality. The sheet goes out only when the count is 0, or when the Orchestrator accepts the
   count after reading it.
4. **Manual step: Josh rates 30 to 60 rows.** Through the Dev Team Lead and Triage, the Orchestrator asks
   the Assistant to deliver `sheet.csv` to Josh as an attachment, with the row count and the rating text at
   lines 103-111 of the instructions. Only `sheet.csv` goes to Josh; `key.json` and `run.json` stay from him
   until his ratings are final. Only Josh supplies ratings, and no seat fills or suggests one. The Assistant
   hands his returned file to the Orchestrator, which writes it as `<dir>/sheet.rated.csv` and gives the
   developer the path and SHA-256.
5. **Score.** Run `score --sheet <dir>/sheet.rated.csv --key <dir>/key.json --capture /dev/null --out
   <dir>/label-quality-results.md`. It reads only those files, and the capture is unused for a `no_decisions`
   run.
6. **Publish.** Blinding is over, so commit the five files under
   `docs/evidence/community-labels-2026-09/study-2026-11/`. Write `label-quality.md` with the results, the sample,
   the provenance and the rule selected. Update `docs/guides/llm-features.md` to state the outcome and link the
   report, and add a line under the instructions' heading naming the 2026-11-05 selection and the new outputs.
   Send the proposed configuration through the Dev Team Lead and Triage. Applying it is operator-side, and this
   leaf does not.

**Acceptance:**

- 2.3.1 - The report records the `--no-decisions` branch and Josh's 2026-11-05 selection, both script SHA-256 values, the C1 readiness counts, the 30 sampled communities (15 per corpus), the exact row count, and the generation-failure count. file: `docs/evidence/community-labels-2026-09/label-quality.md`.
- 2.3.2 - The report gives the regression count for G against D over the 30 communities and the ship rule it selects, 1 or 3, with the proposed `code_index.community_label.enabled` value and every decision consumer `off`, and states that this branch has no J, carry or sufficiency data, so rule 2 is unreachable. file: `docs/evidence/community-labels-2026-09/label-quality.md`.
- 2.3.3 - The committed rated sheet holds Josh's ratings for every row of the delivered sheet, its item ids, paths and names unchanged, and `score` accepted it. file: `docs/evidence/community-labels-2026-09/study-2026-11/sheet.rated.csv`.
- 2.3.4 - The guide no longer says the study and ship decision are pending, and states the outcome with a link to the report. file: `docs/guides/llm-features.md`.
- 2.3.5 - The instructions name the 2026-11-05 selection and the new outputs, and keep their earlier text as history. file: `docs/evidence/community-labels-2026-09/study-rating-instructions-2026-10-26.md`.

**Verification:** `GOBBY_TEST_PROTECT=1 uv run pytest tests/scripts/test_community_label_study.py`, with
`DATABASE_URL` at the isolated test hub, and every number in `label-quality.md` traces to the committed
`label-quality-results.md` or `run.json`.

### 2.4 Route the capture's Box request through the registered evaluate tool [category: code]
`kind: deliverable`

Targets:
- `scripts/decisions_live_capture.py::_capture`
- `tests/scripts/test_decisions_live_capture.py::*` — scope-reason: two new tests pin the registered-tool route, and four existing tests pin the Box finding, redaction, exchange and shape records the route must keep

Consumers unchanged:
- `scripts/community_label_study.py` — no-edit-reason: `_cost` reads only `wire_api`, `measured_chars_per_token` and `probes` from a capture, never a shape record
- `tests/scripts/test_community_label_study.py` — no-edit-reason: its `_capture` helper passes a PNG `--box-image` to `decisions_live_capture.main` and reads the capture only through `_cost`

**Research context:**
- The Adversary gobby#19936's GP-20261105-04: 2.5 names `gobby-decisions:evaluate` as Box's
  consumer, but `_capture` calls `service.box("live_capture", ...)` on its own `DecisionService`,
  so a live run never crosses that consumer's boundary. The repair replaces that request with the
  registered tool's path. It adds no second Box request, no product consumer, no daemon and no
  live configuration.
- The tool: `src/gobby/mcp_proxy/tools/decisions.py::create_decisions_registry(config_resolver)`
  registers `evaluate(state, questions, timeout_seconds=None)`. `_parse` validates the request, and
  `_box` requires an absolute `image_path`, reads the file, and accepts PNG or JPEG by signature,
  else `invalid_request` with detail "image must be a PNG or JPEG file". The handler then reads
  `config.ai.decisions`, takes its service from `src/gobby/ai/decisions.py::get_decision_service`,
  and `_decide` calls `service.box(CONSUMER, ...)` with `CONSUMER = "mcp_evaluate"`. Success
  returns `_answered`: `success` true, `type`, `answers`, `response_model`, `backend_identity`.
  Failure returns `_failure`: `success` false, `reason`, `detail`, `error`, including every
  `DecisionsUnavailable` reason.
- In-memory configuration precedent:
  `tests/mcp_proxy/tools/test_decisions_tools.py::_registry` calls
  `create_decisions_registry(lambda: DaemonConfig(ai=AIConfig(decisions=config)))`, importing
  `gobby.config.app` and `gobby.config.ai`, and `::_evaluate` awaits
  `registry.call("evaluate", {...})`. Neither module is in the capture test's
  `LIVE_CONFIG_MODULES`, which `::test_capture_output_omits_key_and_image` checks against the
  script's imports.
- Raw exchange: `get_decision_service` caches one `DecisionService(config)` per
  `_fingerprint(config)` in the module global `gobby.ai.decisions._service_cache`, built with no
  `exchange_observer`. `_capture` already builds an observed service for each shape
  (`exchange_observer=partial(_observe_exchange, config, record, redactions=redactions)`).
  Setting the cache to `(decisions._fingerprint(config), service)` before the call makes the tool
  use that observed service, so the Box record keeps its `exchanges` and parse diagnostics.
  Rejected: a service-factory parameter on `create_decisions_registry`, a product change whose
  only caller would be this script.
- Redaction: the tool sends `base64.b64encode(data)` of the file's raw bytes, so the capture
  redacts that encoding. `prepare_image_data` is no longer on the Box path.
- Behavior the existing tests pin, on the fake wire of
  `tests/scripts/conftest.py::openrouter_wire`, which answers Box and also resets
  `decisions._service_cache`:
  `::test_capture_box_failure_does_not_block_raw_probes` expects `box["finding"] ==
  {"reason": "http_status", "detail": "HTTP 400"}` on both wires;
  `::test_capture_output_omits_key_and_image` expects the image's base64 absent;
  `::test_capture_records_bounded_redacted_http_errors` reads the Box record's `exchanges`;
  `::test_capture_records_every_shape_and_probe` expects all four kinds sent.
- `main` calls `_capture(config, args)` and writes its dict as JSON, so it needs no edit.
  `_parse_diagnostics` treats only Score questions specially, so the Box `questions` shape below
  is safe. 2.5 runs this script from the main checkout, so this leaf lands first.

**Implementation:**
1. **Tests first,** in `tests/scripts/test_decisions_live_capture.py`:
   - `test_capture_box_runs_through_registered_evaluate_tool`: on the fake wire, the Box record has
     `consumer` `gobby-decisions:evaluate`; its `questions` equal the tool argument
     `{"square": {"type": "box", "instructions": "Locate the red square.", "image_path": <the
     resolved image path>}}`; `image_sha256` equals the file's SHA-256; `tool_output` has
     `success` true, `type` `box`, the fake wire's Box answer and `response_model`; `exchanges`
     holds one 200 exchange; exactly one Box request reached the wire; and
     `gobby.ai.decisions._service_cache` equals its value before the run.
   - `test_capture_box_rejects_unsupported_image_through_the_tool`: a GIF file as `--box-image`
     gives `finding` `{"reason": "invalid_request", "detail": "image must be a PNG or JPEG
     file"}`, sends no Box request, and the six probes still run.
2. **Route.** In `_capture`'s Box branch, replace the `prepare_image_data`, `BoxImage` and
   `service.box` steps:
   - Read the bytes of `args.box_image.resolve()`, append `base64.b64encode(data).decode("ascii")`
     to `redactions`, and set `record["image_sha256"]`.
   - Set `record["consumer"] = "gobby-decisions:evaluate"` and `record["questions"]` to the tool's
     `questions` argument above.
   - Save `decisions._service_cache`, set it to `(decisions._fingerprint(config), service)`, and
     await `create_decisions_registry(lambda: DaemonConfig(ai=AIConfig(decisions=config))).call(
     "evaluate", {"state": _STATE, "questions": record["questions"]})` once. Restore the saved
     value in `finally`.
   - Store the result as `record["tool_output"]`. When `success` is false, set
     `record["finding"]` to its `reason` and `detail`.
   - An `OSError` reading the image stays an `image_input` finding. Drop the imports the change
     leaves unused.

**Acceptance:**

- 2.4.1 - The Box record names `consumer` `gobby-decisions:evaluate`, carries the tool's `questions` argument with the absolute `image_path`, the image's `image_sha256`, the tool's typed output and one recorded exchange; one Box request reaches the wire; and the service cache is restored. test: `tests/scripts/test_decisions_live_capture.py::test_capture_box_runs_through_registered_evaluate_tool`.
- 2.4.2 - A GIF `--box-image` yields the tool's `invalid_request` finding with detail "image must be a PNG or JPEG file", sends no Box request, and the six probes still run. test: `tests/scripts/test_decisions_live_capture.py::test_capture_box_rejects_unsupported_image_through_the_tool`.
- 2.4.3 - The tool's HTTP 400 Box failure is recorded as `finding` `{"reason": "http_status", "detail": "HTTP 400"}` on both wires and does not block the probes. test: `tests/scripts/test_decisions_live_capture.py::test_capture_box_failure_does_not_block_raw_probes`.
- 2.4.4 - The capture section omits the key and the base64 of the image file's bytes, and the script imports none of `LIVE_CONFIG_MODULES`. test: `tests/scripts/test_decisions_live_capture.py::test_capture_output_omits_key_and_image`.
- 2.4.5 - The Box record's failed exchanges stay bounded and redacted through the tool route. test: `tests/scripts/test_decisions_live_capture.py::test_capture_records_bounded_redacted_http_errors`.
- 2.4.6 - All four kinds still reach the wire, and the probe and cost records are unchanged. test: `tests/scripts/test_decisions_live_capture.py::test_capture_records_every_shape_and_probe`.

**Verification:** `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/scripts/test_decisions_live_capture.py
tests/scripts/test_community_label_study.py -v`, then `uv run ruff check
scripts/decisions_live_capture.py tests/scripts/test_decisions_live_capture.py` and
`uv run ruff format --check` on the same paths.

### 2.5 Capture Box on local Clef and record the consumer evidence (depends: 2.4) [category: docs]
`kind: deliverable`

Targets:
- `docs/evidence/decisions/systemone-live.md`
- `docs/evidence/decisions/wire-contracts.md`
- `docs/evidence/decisions/box-red-square-2026-11.png`

**Research context:**
- Josh's D3 answer, 2026-11-05, verbatim: "On 22948, tell the orchestrator to load Clef (not
  Clef flash) with Ollama and use that". The Orchestrator scoped it: "capture the Box evidence on
  Clef (full, not Flash) served locally by Ollama", model `clef:27b` (18 GB), installed in local
  Ollama and not loaded. OpenRouter is dropped by that ruling. Its 2026-10-26 Box attempt
  returned HTTP 400 (`docs/evidence/decisions/item-4-checklist-2026-10-26.md`, lines 104-109),
  which the Orchestrator traced on 2026-11-05 to the 64x64 test image, under Box's 280 px
  minimum. Its rerun on a 512x512 red-square image returned 200 with edges
  0.25/0.74/0.25/0.76. OpenRouter's catalog lists `cloudflare/clef`, `clef-flash` and
  `clef-omni`. The local `clef:27b` capture stays as ruled. LM Studio's `clef-flash` is not
  used.
- What is owed: `docs/evidence/decisions/item-4-checklist-2026-10-26.json` lists "Resolve the Box
  failure and obtain #24248's required real consumer round-trip evidence."
  `.gobby/plans/community-label-decisions.md` 3.5 specified a "Decision shape consumers" section
  in the wire note. That leaf closed with #22604, and `wire-contracts.md` has no such section.
- Consumers and live-path tests, all present: Choice,
  `tests/code_index/test_community_labeler.py::test_gate_admits_at_threshold_and_rejects_below`;
  Noul, `::test_carry_keeps_name_above_threshold_without_generation`; Score,
  `::test_sufficient_deterministic_label_skips_generation`; Box, #24248's
  `gobby-decisions:evaluate` tool,
  `tests/mcp_proxy/tools/test_decisions_tools.py::test_evaluate_round_trips_every_decision_shape`,
  which posts all four shapes, and this leaf's live capture through that tool. #24248's closing
  commit is `fe0c921cbe2780838fcb0c339d4e3f8a23e943b6`.
- Capture tool: `scripts/decisions_live_capture.py::main` runs Choice, Noul and Score through
  `DecisionService`. After 2.4 it sends Box through the registered `gobby-decisions:evaluate` tool
  and records `consumer`, `image_sha256`, the tool's `questions` argument and its typed
  `tool_output`. It then runs the raw probes and appends one dated section to `--out`. It requires
  `--alias`, `--box-image` and `--out`. `build_decisions_config` fixes the service timeout at 30 s,
  and `--probe-timeout-seconds` sets the probes'. A failed shape is recorded as a `finding`.
- Order: the Orchestrator runs the capture from the main checkout, so it needs 2.4's commit on
  0.5.0. 2.4 closes only after it lands, and this leaf's dependency on 2.4 orders the capture
  after the route change.
- Ground truth (the Plan Advocate gobby#19939's PA-22948-02, which the Orchestrator accepted): the
  red square covers pixels [128, 384) on both axes of the 512 by 512 white image, so its expected
  normalized edges are top and left 0.25, bottom and right 0.75. The capture adds no tolerance,
  retry or second request.
- Ollama precedent: `systemone-live.md` Run 8 ran `clef:27b` at `http://127.0.0.1:11434` on the
  `systemone` wire with `response_version` identity, for Choice, Noul and Score only. Box on a local
  `systemone` surface is unverified (`wire-contracts.md`, section 9), and LM Studio's route rejected
  Box with HTTP 400, "Expected 'choice' | 'score' | 'noul'" (`systemone-live.md`, 2026-10-25).
- Box input bounds (`wire-contracts.md`, section 9): PNG or JPEG, 280 to 1568 pixels per side, so a
  512 by 512 image fits. Pillow is in `uv.lock`.
- Execution context: the work directory is in the epic folder, which
  `src/gobby/agents/agent_workspaces.py::spawn_workspace_write_paths` grants only to the seats of a
  live epic-mode team, so this leaf runs in the #22948 epic-mode team. Every decision consumer
  stays `off`, and this leaf changes no configuration.

**Implementation:**
1. **Image.** With Pillow through `uv run`, make a 512 by 512 white RGB image, draw a red filled
   rectangle with corners (128, 128) and (383, 383), which Pillow draws inclusive so the square
   covers pixels [128, 384) on both axes, and save it as a PNG to `<dir>/red-square-512.png`,
   where `<dir>` is
   `/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-22948/build/box-capture-2026-11/`.
   Record its SHA-256.
2. **Orchestrator steps.** Send these through the Dev Team Lead and Triage with the exact commands
   and the image's SHA-256, for a time outside `project_policy.quiet_hours`:
   - Load `clef:27b` in local Ollama, so the 30 s service timeout does not include the cold load,
     and report the context length `ollama ps` shows for it.
   - Once, from the main checkout, with no retries: `PYTHONPATH=/Users/josh/Projects/gobby/src uv run
     --project /Users/josh/Projects/gobby python
     /Users/josh/Projects/gobby/scripts/decisions_live_capture.py --wire-api systemone --api-base
     http://127.0.0.1:11434 --model clef:27b --identity-contract response_version
     --backend-max-state-tokens <that context length> --alias clef:27b --box-image
     <dir>/red-square-512.png --probe-timeout-seconds 600 --out <dir>/clef-capture.md`.
3. **Record.** Append `<dir>/clef-capture.md` unchanged to `systemone-live.md`, and commit the image
   as `docs/evidence/decisions/box-red-square-2026-11.png`.
4. **Wire note.** Add a "Decision shape consumers" section to `wire-contracts.md` that names each
   shape's consumer and live-path test above, #24248's closing commit, and the Clef Box result.
   When Box parsed, it gives the captured edges and confidence beside the expected edges (top and
   left 0.25, bottom and right 0.75) and states each difference. When Box did not parse, it gives
   the finding's reason and detail verbatim. In section 9, replace "Box runtime support remains
   unverified there" with the observed Ollama result. A Box finding stays a finding: the section
   says Box live evidence on Clef is unmet, and no consumer leaves `off`.

**Acceptance:**

- 2.5.1 - The dated capture section records `wire_api` `systemone`, `api_base` `http://127.0.0.1:11434`, `request_model` `clef:27b`, all four shapes, and a Box record with `consumer` `gobby-decisions:evaluate`, its `image_sha256`, and the tool's parsed answer or its finding. file: `docs/evidence/decisions/systemone-live.md`.
- 2.5.2 - The wire note's "Decision shape consumers" section names a product consumer and live-path test for each of Choice, Noul, Score and Box, with #24248's closing commit and the Clef Box result, and when Box parsed it sets the expected edges (top and left 0.25, bottom and right 0.75) beside the captured ones. file: `docs/evidence/decisions/wire-contracts.md`.
- 2.5.3 - Section 9 of the wire note states the observed Ollama Box result in place of "remains unverified". file: `docs/evidence/decisions/wire-contracts.md`.
- 2.5.4 - The committed image is 512 by 512 and white except for red pixels covering exactly [128, 384) on both axes, and its SHA-256 equals both the one sent with the Orchestrator's command and the Box record's `image_sha256`. file: `docs/evidence/decisions/box-red-square-2026-11.png`.

**Verification:** the committed capture section is byte-equal to `<dir>/clef-capture.md`, the image
hash matches, a Pillow read of the committed image confirms the red pixel bounds, and the diff
changes no configuration file.

### 2.6 Name the image's size and the bounds when Box refuses its dimensions [category: code]
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py::_check_images`
- `tests/ai/test_decisions_service.py::*` — scope-reason: a new test pins the refusal detail for both formats and both bounds, the 512 by 512 pass and the cooldown, and `test_box_rejects_invalid_images_before_dialing` keeps its existing checks

**Research context:**
- #25788, filed by the Orchestrator under #22948 on 2026-11-05 and directed into P2 with no edges
  (Decision 19). Its fix: "read the PNG/JPEG dimensions from the header and refuse an image
  outside 280-1568 px per side with a typed invalid-input error naming the size and the bounds,
  before dialing. Invalid input must not open the service cooldown." Its validation criteria:
  "1. A Box request with a 64x64 image is refused before any HTTP call with an error naming the
  image size and the 280-1568 px bounds. 2. A 512x512 image passes validation unchanged. 3. The
  refusal does not open the service cooldown. 4. Focused tests in tests/ai pass against the
  isolated test hub."
- What already exists. #25788's description says the service does not check pixel dimensions.
  The current checkout does check them, since `4e5b4ef809` (2026-10-24, #22604):
  - `src/gobby/ai/decisions.py::_check_images` measures each image with `_image_dimensions`
    (PNG IHDR or JPEG SOF). Outside 280 to 1568 px per side, or 78,400 to 2,458,624 px in all,
    it raises `DecisionsUnavailable("invalid_request", "image dimensions exceed Box limits")`.
  - `_check_request` calls it, and `DecisionService._request` runs `_check_request` before the
    cooldown check and `_dial`. A refusal therefore sends nothing and opens no cooldown.
  - `tests/ai/test_decisions_service.py::test_box_rejects_invalid_images_before_dialing` pins
    refusal of 279 and 1569 px PNG and JPEG images before dialing with `invalid_request`. It also
    pins the inclusive 280 and 1568 px boundaries and that no refusal opened the cooldown, on
    `systemone` and `openrouter-decisions`.
- So criteria 2 and 3 already hold, and criterion 1 holds except for the error's content. The
  detail names neither the size nor the bounds, and says "exceed" for an undersized image. This
  leaf changes that detail only. It keeps the bounds, the area check and the order of checks.
- Fixture sweep, for the Adversary gobby#19936: no fixture starts failing, because the guard
  already exists and only its detail changes. The Box fixtures are
  `tests/scripts/conftest.py::png` (280 by 280), `tests/mcp_proxy/tools/test_decisions_tools.py::_png`
  (280 by 280), the 2.5 image (512 by 512), and the service test's
  `tests/ai/test_decisions_service.py::_png` and `::_jpeg` at chosen sizes. A repository search
  for "dimensions exceed" and "exceed Box" finds only `decisions.py` line 506, so no test or
  document quotes the old detail.
- Consumers: the `gobby-decisions:evaluate` tool returns the detail through
  `src/gobby/mcp_proxy/tools/decisions.py::_failure` as `detail` and in `error`, with its shape
  unchanged. `_check_request` is `_check_images`' only caller.
- Test helpers already in the service test: `::_png(width, height)`, `::_jpeg(width, height)`,
  `::_image`, `::_wire_config`, `::_must_not_dial`, `::_shape_responder`, `BOX_MODEL` and `STATE`.
- `src/gobby/ai/decisions.py` is 805 lines, under the 850-line heuristic.

**Implementation:**
1. **Test first.** Add `test_box_dimension_refusal_names_size_and_bounds` to
   `tests/ai/test_decisions_service.py`, parametrized on `systemone` and `openrouter-decisions`.
   It builds one `DecisionService` with `failure_cooldown_seconds=60`.
   - With `_must_not_dial`, a 64 by 64 PNG raises `invalid_request` with detail exactly
     `image is 64x64 px; Box takes 280 to 1568 px per side`, and a 300 by 1569 JPEG gives
     `image is 300x1569 px; Box takes 280 to 1568 px per side`. No request is sent.
   - With `_shape_responder`, a 512 by 512 PNG Box request dials once and returns its answer,
     which shows the refusals opened no cooldown.
2. **Detail.** In `_check_images`, raise
   `DecisionsUnavailable("invalid_request", f"image is {width}x{height} px; Box takes 280 to 1568
   px per side")`. Keep the bounds and the area check unchanged.

**Acceptance:**

- 2.6.1 - A 64 by 64 PNG and a 300 by 1569 JPEG are refused with `invalid_request` before any HTTP call, each with a detail naming its size and the 280 to 1568 px bounds. test: `tests/ai/test_decisions_service.py::test_box_dimension_refusal_names_size_and_bounds`.
- 2.6.2 - After those refusals, a 512 by 512 PNG passes validation and dials once, so the refusals opened no cooldown. test: `tests/ai/test_decisions_service.py::test_box_dimension_refusal_names_size_and_bounds`.
- 2.6.3 - The 279 and 1569 px refusals, the inclusive 280 and 1568 px boundaries and the no-cooldown check still pass on both wires. test: `tests/ai/test_decisions_service.py::test_box_rejects_invalid_images_before_dialing`.

**Verification:** `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_decisions_service.py
tests/mcp_proxy/tools/test_decisions_tools.py -v`, then `uv run ruff check src/gobby/ai/decisions.py
tests/ai/test_decisions_service.py`, `uv run ruff format --check` on the same paths, and
`uv run mypy src/gobby/ai/decisions.py`.

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
- 2026-11-05: Plan Writer gobby#19935 refreshes the plan on the Orchestrator
  gobby#19309's ruling that #24222's closure does not stand as delivery of 1.1,
  and on Josh's D1 and D2 buttons (Decisions 2 and 12). Sections 1.1 and 1.2 keep
  their closed leaves, and new work takes new section ids. Leaves 2.1 and 2.2
  replace the undelivered measurement and carry its protocol, so they stand alone.
  Leaf 2.3 adds the label study. The prior M1 is removed for re-derivation.
- 2026-11-05: The Orchestrator gobby#19309's rulings, relayed through the lead
  gobby#19945. It confirms the three `launch_services.py` invocations, the
  developer-run isolated daemon on ports 59480-59482, the runtime root and an
  epic-mode launch, its own run of `prepare`, and D2 as D4's go for C1. The launch
  waits on Josh's freeze until #25765 and #25653 are proven live. #24701 is edited
  at expansion to be blocked by the 2.3 task. It accepts the Plan Advocate
  gobby#19939's PA-22948-01 as written, adding the resume rule to 2.2 step 1 with
  no new store. D3 was still open.
- 2026-11-05: Josh answered D3, relayed by the Orchestrator gobby#19309: "On
  22948, tell the orchestrator to load Clef (not Clef flash) with Ollama and use
  that". Decision 12 records it, leaf 2.4 captures Box on `clef:27b` in local
  Ollama and records the consumer evidence, and 2.3's inert decision flags name
  the same backend, so the plan has no OpenRouter dependency. This resolves the
  Adversary gobby#19936's GP-20261105-01.
- 2026-11-05: The Orchestrator gobby#19309 accepted the Plan Advocate
  gobby#19939's PA-22948-02 as suggested: the capture image's red square covers
  pixels [128, 384) on both axes of a 512 by 512 white image, and the wire note
  sets the expected edges 0.25 and 0.75 beside the captured ones, with no
  tolerance, retry or extra request. For the Adversary's GP-20261105-04, new leaf
  2.4 replaces the capture script's direct Box request with the registered
  `gobby-decisions:evaluate` tool path, keeping the raw exchange through the
  observed service, and the capture becomes 2.5, which depends on 2.4.
- 2026-11-05: Consensus. The Plan Writer gobby#19935 and the Adversary
  gobby#19936 reached technical consensus on plan commit
  `3b46dab4be4014970fc4be31d2652cec70c5d95f` (plan SHA-256
  `6d388ab8a97b68f7a75e585bbf3593993e81bebfd446306c705e78f1ce83ba2e`), with base
  validation passing for both. GP-20261105-01 through GP-20261105-04 are resolved.
  The Adversary verified PA-22948-02 and found the temporary service-cache seam in
  2.4 consistent with `get_decision_service`'s fingerprint contract, so no product
  factory parameter is needed. The Plan Advocate gobby#19939 converged on the same
  bytes with no further suggestions. No open decision or blocking finding remains.
- 2026-11-05: After that consensus, the Orchestrator gobby#19309 directed a scope
  change, relayed by the lead gobby#19945. The Adversary withdrew consensus on
  `dc44dc4db4fa0482655e9afff4c1a482a23f7417` before deriving any M1.
  - The Orchestrator's #25788 joins P2 as leaf 2.6 with no edges. Decision 19
    records that #25788 is closed at expansion as superseded by the leaf.
  - The source shows the dimension guard already exists, so 2.6 changes only the
    refusal's detail to name the image's size and the bounds.
  - The Orchestrator's OpenRouter Box rerun returned 200 on a 512x512 image. It
    traced the 2026-10-26 HTTP 400 to the 64x64 test image, and the plan notes
    OpenRouter's catalog listing of `cloudflare/clef`, `clef-flash` and
    `clef-omni`. The local `clef:27b` capture stays as ruled.

## V2: Verification
`kind: verification`

After the leaves pass:

1. Confirm the report's C1-C8 tables trace to the committed records under
   `docs/evidence/gcode-graphify-rerun-2026-11/results/`.
2. Confirm `provisioning.md` states the Orchestrator's exact invocations, and that
   the Orchestrator's `docker ps -a` lists no `gcode-graphify-rerun-2026-11` container.
3. Rerun `cargo test -p gobby-code --lib commands::graph::view`.
4. On the installed binary, `gcode graph view --view=communities --format json` on
   the main checkout lists equal-size communities in ascending id order.
5. Confirm `label-quality.md` exists and `docs/guides/llm-features.md` no longer calls
   the study pending. The Orchestrator then soft-deletes the C1 registration, and
   confirms #24701's `blocked-by` names the closed study leaf.
6. Rerun `tests/scripts/test_decisions_live_capture.py` under
   `GOBBY_TEST_PROTECT=1` against the isolated test hub. Confirm `wire-contracts.md`
   has the "Decision shape consumers" section and `systemone-live.md` the dated
   `clef:27b` capture, whose Box record names `consumer` `gobby-decisions:evaluate`.
7. Rerun `tests/ai/test_decisions_service.py` under `GOBBY_TEST_PROTECT=1` against the
   isolated test hub, and confirm #25788 is closed as superseded by the 2.6 leaf.
