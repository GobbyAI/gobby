# gcode and Graphify: parity, evidence guidance, naming

- **Status:** research, read-only. Nothing in the repo, the tasks or the config was changed.
- **Author:** Researcher gobby#14435, 2026-09-24.
- **Requested by:** #14069, relaying Josh (message f6e6d589).
- **Scope:** set by the PD gobby#14018 (message fb016ca4).
- **Checked at:** 0.5.0 HEAD `7095381d6e`. The installed `gcode` is 1.9.0, CLI contract 11.

> **Update 2026-09-24, late: #22858 changed the refresh findings.** #22858 ("Community
> refresh counts internal edges by scanning every import edge per community") closed at
> `386c7040d0` and was promoted at 15:04.
> - **Cause:** the cost was `internal_edges`, which scanned every import edge once per
>   community, O(communities × edges). It was not the Leiden rebuild before the
>   signature compare, and the input-digest fix was dropped (`refresh-latency.md:27-48`).
> - **Live path, one unchanged file:** 17.37 s wall before, 0.99-2.47 s after
>   (`refresh-latency.md:43-45`). That is still over the +250 ms budget.
> - **Evidence limits:** two runs, under machine load. The harness corpus is gone, so
>   the 89 s harness figure can't be re-run like for like.
> - **Timeouts:** the daemon's 30 s index timeout fired 100 times on 09-24, the last at
>   14:59 local, and not once from the 15:04 promotion to 16:29 (`~/.gobby/logs/daemon.log`).
> - The rows in Answers 1, §1.2, §1.3, §1.4 and §4 item 1 are corrected. The original
>   89 s numbers are kept where they describe the harness.

## Request

Josh, verbatim: "Has gcode achieved parity+ with graphify? We need to give agents
instructions on how/when to use gcode evidence (is this the right semantic naming we
should use? Would gcode investigate, gcode delve, gcode find work better?"

PD constraints (fb016ca4):

- The parity work does not overlap #22405.
- A rename or a rewording of the evidence instructions must land **before cohort 3
  launches**, or else be recorded as a treatment change.
- No cohort launches. No edits to test-cohort-a.

## Answers

1. **Parity+.** It depends on what you compare against.
   - **The community subsystem**, which is what the #22581 plan set as the parity+
     goal:
     - **Yes** for partition, splitting, identity, persistence, surfaces and names.
       Graphify produced no semantic community names in three runs; gcode names every
       community of 5+ files after a real directory.
     - **Not yet** on one of the four properties the plan rests parity+ on (plan:30-34):
       the calibrated label gate is parked (#22604), so generated labels are admitted
       on schema validation alone.
     - **Over budget** on incremental refresh at Gobby scale, against the +250 ms
       budget from the plan's leaf 3.2. A one-file index on the live path now takes
       0.99-2.47 s wall, down from 17.37 s, since #22858
       (`refresh-latency.md:43-45`).
   - **Graphify as a whole product: no.** That was never the goal. Most of the gaps
     are recorded decisions: wiki and exports, and CLI tuning knobs.
   - **What's left** splits three ways: gaps nobody has decided on, one regression,
     and head-to-head numbers that are now stale (§1.3).
   - **The headline open gap is indexing: over budget, no longer timing out.**
2. **Guidance.** Nothing anywhere tells an agent *when* to use gcode evidence instead
   of search, grep, outline or symbol-at. A draft is in §2.3.
   - Its one job: pin a `path:line` citation that someone else will check.
   - The draft is a **#22405 treatment change.** It must land before cohort 3 launches,
     or else be recorded as a treatment change.
   - My recommendation: land it before cohort 3 and choose a fresh cohort 3 over the
     2b replacement, because a 2b replacement would pool runs with and without the
     guidance.
3. **Naming.** Keep `evidence`. The gap is guidance, not the name.
   - `investigate`, `delve` and `find` each point agents toward the wrong use.
   - If Josh wants a verb anyway, `cite` is the only one that fits.

## 1. Parity

### 1.1 What "parity+" meant

The completed plan (`.gobby/plans/completed/gcode-import-communities.md:20-34`) set
the target as parity+ with **Graphify 0.9.55's community subsystem**, not with all of
Graphify.

- It lists seven Graphify capabilities to close:
  - cohesion scoring;
  - oversized splitting;
  - low-cohesion splitting;
  - cross-run identity;
  - signature-gated label reuse;
  - persisted memberships;
  - semantic labels.
- It lists three things to add that Graphify lacks:
  - ids that are never recycled;
  - a calibrated admission gate for generated labels;
  - a `communities` evidence operation.
- Parity+ "rests on four properties" (:30-34):
  - a file-level projection that excludes externals;
  - monotone ids with Jaccard remap;
  - a label admitted only when a decision model is confident;
  - byte-bounded, replay-verifiable evidence.

### 1.2 Community subsystem scorecard

| Capability | gcode now | Verdict |
| --- | --- | --- |
| Cohesion scoring and both splits | Graphify's constants in integer math; the threshold spike kept them all (`docs/evidence/community-labels-2026-09/thresholds.md:175-179`) | Matched |
| Cross-run identity | `code_communities` table, greedy Jaccard remap, and a per-project watermark, so a retired id is never reissued. Graphify recycles ids (plan:44-46). The churn runs show `changed=0` on unchanged input (`churn.md:27,77`). | Exceeded |
| Persisted memberships, signature-gated label reuse | Computed at index time, read-only on every read path (plan:40-43, 56-58) | Matched |
| Semantic labels | Graphify produced zero semantic names in its June, August and September runs and fell back to hub names such as `walker.rs`. gcode names every community of 5+ files after a real directory, with no model involved (`thresholds.md:199-203`). Model labels are live: 6 replaced deterministic ones in the smoke run (`live-smoke.md:107`). | Exceeded on output |
| Calibrated label gate | #22604 is parked by Josh, so labels are admitted on schema validation alone (plan:55-56) | **Missing** (parked) |
| Community surfaces | `--view=communities`, `--community`, the `## Import communities` report section, MCG subgraphs, and the `communities` evidence operation (`crates/gcode/src/evidence/communities.rs`; gobby-ask `evidence`) | Exceeded |
| Incremental refresh cost | Harness, Gobby checkout, one file: 487 ms p50 before #22595 and **89,170 ms** after (`refresh-latency.md:16-19`). Live path since #22858: 17.37 s wall before, **0.99-2.47 s** after, over two runs (`refresh-latency.md:43-45`). Game Goblins: 379 ms to 477 ms, a pass. | **Over budget** (+250 ms), no longer timing out (§4) |

Two smaller rough edges from the smoke run (`live-smoke.md`):

- 57 of 109 deterministic labels need an ordinal suffix to stay unique (:61).
- Size ties break differently on the three read surfaces (:117-121).

Neither is a parity gap.

Don't compare community counts. `thresholds.md:192-197` says counts, medians and
singleton counts "are not comparable at all": Graphify clusters 2,308 mixed nodes,
while gcode clusters 142 files.

### 1.3 Beyond the community subsystem

**Gaps by decision.** These are recorded, and I don't recommend reopening any of them.

- **Wiki renderer.** Out of scope by user direction; gwiki was deleted on 2026-09-05
  in `0a4d3cb3c3` (plan:65-66).
- **HTML, Obsidian, SVG, GraphML and call-flow exports.** Recorded as "explicit
  dispositions, not substitutions" (`docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md:75`).
- **Leiden tuning flags.** "No CLI knobs; `DEFAULT_GAMMA` stays 1.0" (plan:50).
- **Hub exclusion and call edges in the partition.** Import edges only; externals are
  excluded by construction (plan:63-64).
- **Memory communities.** Deferred to D1 (plan:66). The follow-up is #22606
  (needs-planning, blocked by #21563).

**Open gaps.** Nobody has decided on these, and no task tracks them.

| Gap | Evidence | Weight |
| --- | --- | --- |
| **Incremental index cost** | §4 below. The trigger's 30 s timeout fired 2 times on 09-20, 48 on 09-23 and 100 on 09-24, the last at 14:59. It has not fired since the #22858 promotion at 15:04 (`~/.gobby/logs/daemon.log`). A one-file index now takes 0.99-2.47 s. | **Headline.** gcode's clearest lead over Graphify was incremental cost. At Gobby scale it is still over budget, but no longer timing out. |
| Language breadth | Graphify: 37 tree-sitter grammars plus regex extractors. gcode: 19 code grammars plus JSON and YAML (`crates/gcode/Cargo.toml:70-93`). | Partial. Matters only for corpora in the missing languages. |
| `path` query | Follows CALLS edges only | Partial |
| Surprising connections | Graphify reports them; there are 0 hits in `crates/gcode/src` | Missing. Enhancement, not a bug. |
| Rationale and cites nodes | Graphify extracts rationale comments as nodes (bakeoff C4); gcode indexes the text as content only | Missing. gcode's exact-prose retrieval passed C4, where Graphify truncated the text. |
| Confidence tags | Per edge only, with no corpus-level distribution | Partial |
| Suggested questions | A JSON template field (`summary.rs:307-336`), not in the Markdown report | Partial |
| MCP shortest path | gobby-ask `graph` has `directed_path`, but there is no standalone graph MCP server | Partial |
| Interrupted-run recovery (C7) | Never measured for gcode; blocked at preflight (`graphify-gcode.md:73`) | Unknown |
| Image and other multimodal extraction | Graphify extracts from non-code media; gcode has no extractor, and no plan or bakeoff case covers it | Not evaluated. Plain document text is a gcode win: Graphify's code-only C1 run skipped 20 documents that gcode content-indexed (`graphify-gcode.md:67`). |

**Matched** (from the sweep):

- git hooks;
- god-node hotspots (`render.rs:38-61`);
- impact and explain commands;
- question retrieval, 6/14 gold spans each (C8).

**Exceeded** (from the sweep):

- `--token-budget` on six commands;
- sourced answers through `gcode ask`: 14/14 supported after retries
  (`docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md:395-414`). Graphify has no
  Ask, so this has no comparison.
- per-symbol summaries.

**Stale measurements.** Every head-to-head number predates the community work.

- The September bakeoff ran gcode 1.7.0, CLI contract v8, against Graphify 0.9.55
  (`graphify-gcode.md:46-47`). The installed gcode is 1.9.0, contract 11. Graphify's
  current release is 0.9.67.
- Graphify has since closed its py3.13 Leiden gap (0.9.60, plan:30-31). Its changelog
  claims deterministic graph output from 0.9.66 on. That comes from an untrusted web
  source and I have not verified it. If true, it narrows the reproducibility lead that
  C9 showed: in C9, 0.9.55's call-flow HTML order drifted.
- The June scorecard is older still.

### 1.4 Benchmarks

From `graphify-gcode.md:87-94`, on Game Goblins with gcode 1.7.0 and Graphify 0.9.55:

| Case | Graphify | gcode | Read |
| --- | ---: | ---: | --- |
| C1 full build | 2.430 s | 35.789 s | About 15× slower, but not like for like. gcode content-indexed 142 files including non-AST formats (711 chunks) and synced graph and vector projections. Graphify's code-only run skipped 20 documents. Never re-measured, and no task tracks it. |
| C2 unchanged rerun | 2.108 s (changed the index) | 0.117 s (no-op) | gcode was correct and 18× faster. This predates `refresh_project_communities`. |
| C3 nine-file change | 2.533 s (26 files rebuilt) | 3.121 s (exactly 9) | Graphify was faster; only gcode kept to the changed-file scope. |
| C4-C6 one to four files | 2.43-2.49 s | 0.42-0.98 s | gcode was faster. On a small corpus that still holds after the regression (477 ms), but not on Gobby. |
| C8 retrieval | 6/14 | 6/14 | Tie. gcode's hybrid lane was excluded because its embedding identity failed preflight. |

Don't cite Graphify's LOCOMO or recall numbers from PyPI. They measure memory, not
code indexing.

Before anyone claims parity+ in writing, **re-run C1-C3 and C8 on gcode 1.9.0 against
Graphify 0.9.67.** The #22858 fix has landed, so C2 and C4-C6 should hold. The
Gobby-scale number, 0.99-2.47 s, is still over budget.

## 2. Agent guidance for gcode evidence

### 2.1 What it is

`gcode evidence --request-json` is a model-free JSON contract ("Read indexed
working-tree evidence without agent or model orchestration", from `--help`).

- **Operations:** search, read, graph and communities
  (`crates/gcode/src/evidence/contracts.rs:99-207`). `--help` shows examples for search
  and read only.
- **Search lanes:** symbol, lexical_symbol, literal, regex, content, hybrid.
- **Read kinds:** range, symbol, commit_metadata.
- **Graph queries:** callers, callees, usages, imports, directed_path, scoped_view.
- **Each item carries:** an `evidence_id`, `content_hash` and `excerpt_hash`, line and
  byte bounds, `excerpt`, and `numbered_excerpt` (lines prefixed `N| `, added in
  #22400, `c9324608bf`).
- **Where the bytes come from:** the working tree, checked against the indexed content
  hash. A mismatch returns `stale_range`.
  - `crates/gcode/src/commands/evidence.rs:134` roots the library at the caller's
    checkout.
  - `crates/gcode/src/evidence/source.rs:29-67` (`read_file`) opens the file at :45,
    hashes it at :56-57, and returns `stale_range` at :58-63.
- **MCP twins:**
  - `gobby-ask:evidence` needs no Ask run (`src/gobby/mcp_proxy/tools/ask.py:100-114`).
  - `query_evidence` and `read_evidence` are for assigned Ask workers (:284-314).

**Cost.** Cohort 1 measured about 28% of each response as metadata, and the source text
appears twice (`excerpt` and `numbered_excerpt`). Today a 3-line range read returned
1,859 bytes of JSON for 128 bytes of source.

**What `symbol-at` lacks.** It prints a body with **no line numbers**. `outline` gives
only symbol ranges.

### 2.2 Where agents learn about it today

| Place | What it says | When |
| --- | --- | --- |
| Skill `code-index/ask.md:3-6, 86-93, 105-120` | What evidence is, its operations, and that `communities` is not citable | Loaded "when … reading immutable evidence". That is circular: an agent must already want evidence to find the text that says when to want it. |
| Skill `code-index/overview.md:5-6, 29` | Routes to ask.md for "handling immutable evidence" | Same circularity |
| Skill `search.md`, `retrieval.md` | Nothing | This is where lane choice is actually taught |
| gobby-ask `evidence` description (`ask.py:103-107`) | Operations; "communities orients you; it is not citable" | Nothing about when |
| gobby-ask `query_evidence` description (`ask.py:288-289`) | "Prefer content for docs and literal for exact identifiers" | The only lane hint anywhere |
| `docs/guides/code-index.md:48-55`, `docs/guides/ask.md:153-167` | Human docs | Not agent-routed |
| AGENTS.md, rules, hooks | Nothing. `require-code-index-skill` redirects long reads to `outline` and `symbol-at` only. | — |
| Agent definitions `ask-investigator.yaml:55-56`, `ask-reviewer.yaml:55` | Tool lists only | — |
| #22405 arm A block (`docs/research/gcode-evidence-cohort-sonnet.md:95-103`) | CLI form and request shapes. No graph operation, no when. | Cohort agents only |

No text anywhere says when to use evidence.

The read-redirect hook sends agents to `symbol-at`, which has no line numbers. That is
the path by which citations drift.

### 2.3 Draft guidance

**Proposed placement:**

- a new section in `code-index/ask.md`;
- a routing change in `overview.md`;
- one sentence in the gobby-ask `evidence` tool description.

Wording follows the house style.

> ## When to use source evidence
>
> Find code with `gcode search`, `gcode grep` and `gcode search-content`. Read it with
> `gcode outline` and `gcode symbol-at`. Use source evidence (`gobby-ask:evidence`, or
> `gcode evidence --request-json` from a shell) for one job: pinning a `path:line`
> claim that someone else will check, such as an Ask answer, a close summary, a review
> finding or a research report.
>
> - Before writing the claim, read that range as evidence and copy line numbers from
>   `numbered_excerpt`. `symbol-at` prints no line numbers, and counting lines by hand
>   is how citations drift.
> - Don't browse with evidence. Each item carries hashes and bounds and repeats the
>   source text, so it costs more per line than `symbol-at`.
> - Prefer the MCP tool: it avoids quoting JSON in a shell.
> - `stale_range` means the file changed after it was indexed. Don't cite the old
>   range; re-read once the index catches up.
> - `communities` orients you in an unfamiliar area. It is not citable; cite `read`
>   items from its members.
> - Assigned Ask workers use `query_evidence` (see Assigned Ask workers).

- **overview.md:29 routing row**, from "Running durable questions or handling immutable
  evidence" to "Running durable questions, or citing exact `path:line` ranges with
  source evidence".
- **`ask.py:103` evidence description**, adding: "Use it to verify a range before
  citing path:line; navigate with gcode search, outline and symbol-at."

**How the draft maps to the cohort data** (from `gcode-evidence-cohort-sonnet.md` at
`0e0a6b0a71^`; the answer key is not reproduced here):

- **Scores.** Cohort 2 (Sonnet) scored arm A 11 and 12 strict, arm B 11 and 10. The
  conclusion was "Evidence availability made no measurable accuracy difference …
  Evidence did not reduce citation errors" (L350-352).
- **Bad citations.** A1 4, A2 5, B1 2, B2 7. All five of A2's were in files read only
  through evidence, "a JSON-escaped string with no per-line numbers, so the agent had to
  count lines". B2's seven came from ordinary reads (L326-329). #22400's
  `numbered_excerpt` removes the first cause. The draft's cite-from-`numbered_excerpt`
  rule targets both.
- **Format errors.** Cohort 1 (Haiku) lost 3 of 16 calls to request-format errors,
  which is why the draft says to prefer the MCP tool.
- **Cost.** Cohort 1's ~28% metadata is why the draft says don't browse.

Treat the rule as provisional until cohort 3 reports. The only measured result so far
is "no difference".

### 2.4 Interaction with #22405 (fb016ca4)

Every proposed landing place is reachable by isolation-none cohort sessions in both
arms: the skill files, the tool description, and a synced skill after `gobby sync`.
**So the guidance is a treatment change.** It must land before cohort 3 launches, or
else be recorded in #22405 as a treatment change.

The effects:

- **Arm A** would test "evidence plus when-to-use guidance", which is the product we
  would ship. Cohort 2 already showed that evidence without guidance does nothing
  measurable.
- **Arm B's block** says evidence is unavailable. If arm B loads `ask.md`, it will read
  guidance for a tool it can't use. The effect is small, but it should be recorded.
- **#22405's open decision** (fresh cohort 3 at 2 per arm, or a 2b replacement):
  landing the guidance first rules out 2b, because 2b pools cohort 2 runs that had no
  guidance.
- **"Prefer the MCP tool"** also changes arm A. Arm A's block names only the CLI form.

The recommendation is in Answers 2. I did not touch test-cohort-a (a database row) or
any cohort.

Separately, #22405 already requires one pre-launch edit to test-cohort-a: arm A's
"(end_line must not exceed the file's length)" at `gcode-evidence-cohort-sonnet.md:100`
must become the clamp text from #22400.

## 3. Naming

**Recommendation: keep `evidence`.**

**Why:**

- **It fits the CLI convention.** Noun commands return an artifact (`outline`, `symbol`,
  `tree`, `callers`, `contract`); verb commands act (`search`, `grep`, `index`, `ask`).
  `evidence` returns a hash-verified artifact.
- **`find`** repeats what the five search commands already do. It hides that evidence
  also reads, walks graphs and lists communities, and it would pull agents toward
  exactly the browsing use the guidance warns against.
- **`investigate` and `delve`** promise orchestration. `--help` explicitly rules that
  out ("without agent or model orchestration"). Orchestration is `gcode ask`, whose
  worker is the `ask-investigator` agent, so `investigate` would collide with it
  directly.
- **The name caused no measured misuse.** A2's errors were line counting, now fixed.
  Cohort 1's were JSON format errors.
- **Agent-facing names are already qualified:** `gcode evidence` and
  `gobby-ask:evidence`. The ~197 Python files that mention "evidence" (Ask admission,
  plan evidence, close evidence) are internal. The guidance should still always say
  "source evidence" or `gcode evidence`, never a bare "evidence".

**What a rename would cost:**

- the contract key lists in three places (`crates/gcode/src/contract/schema.rs`,
  `crates/gcode/contract/gcode.contract.json`, `tests/contracts/gcode.contract.json`);
- the four gobby-ask tool names and their modules;
- the skills and docs;
- a restart window for the forbid-extra promotion (memory cba73c37);
- a reset of #22405's baseline, because arm A's text names `gcode evidence` literally.

A rename would also be a treatment change under fb016ca4.

**If Josh wants a verb anyway,** `cite` is the only candidate that names the job. I
don't recommend it: the command also serves Ask admission and graph reads, which are
not citing.

## 4. Found work (escalated to the PD, message c8339665)

1. **Incremental index regression (P1). Fixed by #22858 at `386c7040d0`; still over
   budget.**
   - **Cause, corrected:** `internal_edges` in `communities/partition.rs` scanned every
     folded import edge once per community, O(communities × edges) over 2,353
     communities. A live `sample` put about 100% of the main thread there, with Leiden,
     the import load and the database near zero (`refresh-latency.md:27-35`).
     - #22858 counts each member's edges with a `BTreeMap` range instead.
     - My original cause was wrong: I blamed the Leiden `build_partition` running
       before the signature compare in `refresh_project_communities`
       (`communities.rs:77`). The code order was real, but that step wasn't where the
       time went.
   - **Callers:** `pipeline.rs:253` and `:433`, and `overlay.rs:337` via
     `lifecycle.rs:61`.
   - **Why it's hidden:** it runs after `refresh_project_stats`, so `gcode status`
     `index_duration_ms` leaves it out.
   - **Impact before the fix:** one file on Gobby took 89 s p50 in the harness and
     17.37 s on the live path. The daemon trigger's 30 s timeout
     (`src/gobby/code_index/trigger.py:310`) fired 150 times through 14:59 on 09-24.
     The per-day counts are in §1.3.
   - **After the fix:** 0.99-2.47 s over two live runs, and no timeouts from the 15:04
     promotion to 16:29.
     - By checkout: epic-rust-gclient 49, epic-stability 36, main 18, epic-overflow 5,
       epic-runbooks 3, others 1-2.
     - The first post-09-20 timeout was 09-23 07:48, after `a37c885639` (#22595)
       landed on 09-22 10:20. That is consistent with #22595 as the cause, but the
       promotion time isn't pinned (the installed binary's mtime is 09-24 09:35).
   - **The input-digest fix was dropped.** The rebuild is now cheap, and the existing
     output-signature skip avoids the write (`refresh-latency.md:47-48`).
   - **Open:** the remaining gap to the +250 ms budget. No task tracks it.
2. **Contract doc drift.**
   - `docs/contracts/gcode-cli.md:29` ("exact-commit read surface") and :203-211 say the
     source bytes come from the bound Git blob.
   - Since #22021 (`469ee29d23`), `evidence/source.rs` reads the working tree, checked
     against the index hash.
     - The commit deleted `evidence/snapshot.rs`, which read blobs through
       `git cat-file`, and added `source.rs:29-67`.
     - `RepositoryBinding.commit_oid` and `tree_oid` are shape-checked only
       (`evidence/mod.rs:36-45`).
     - Commit metadata alone still comes from Git (`read.rs:336-337`).
   - :201 ("verified snapshot binding") and :203 ("blob … hashes") are stale for the
     same reason. Source items carry `content_hash` and `excerpt_hash` and no blob OID
     (`contracts.rs:334-335`).
   - The skill (`ask.md:22-23`) is already correct.
3. **#22604** is parked by Josh, but the task carries no parked marker.

The PD is filing these for a lane. Everything else in §1.3 is an enhancement gap, not
found work.

## Sources

- **Plans and evidence:**
  - `.gobby/plans/completed/gcode-import-communities.md:20-67`
  - `docs/evidence/community-labels-2026-09/`: `refresh-latency.md`, `thresholds.md`,
    `live-smoke.md`, `churn.md`
  - `docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md:46-94`, `ask-pipeline.md:395-414`
- **Cohorts:**
  - `docs/research/gcode-evidence-cohort-sonnet.md`: the working tree for the treatment
    blocks, and `0e0a6b0a71^` for the cohort 1 and 2 results (#22640 moved the answer
    key out)
  - #22400 (`c9324608bf`); #22405
- **Code:**
  - `crates/gcode/src/evidence/{contracts,source,communities}.rs`
  - `crates/gcode/src/communities.rs:77`
  - `src/gobby/mcp_proxy/tools/ask.py:100-114, 284-314`
  - `src/gobby/code_index/trigger.py:310`
- **Skills:** `src/gobby/install/shared/skills/gobby/references/code-index/{ask,overview}.md`
- **Logs:** `~/.gobby/logs/daemon.log` (timeout counts through 2026-09-24)
- **Web** (treated as untrusted): Graphify releases 0.9.58-0.9.67 on PyPI and GitHub
