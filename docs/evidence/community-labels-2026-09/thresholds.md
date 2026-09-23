# File import partition: split thresholds and deterministic labels (plan 2.2, Q1.2)

Evidence for `.gobby/plans/gcode-import-communities.md` acceptance items 2.2.7, 2.2.11
and 2.2.12: the size distribution the partition produces on two corpora, the largest
community's share of visible files against the 25% target, whether any `community-N`
placeholder survives, and whether communities of five or more files carry deterministic
labels that name subsystems.

## Method

The experiment `partition_experiment_reports_distribution` in
`crates/gcode/src/communities/partition_tests.rs` loads a project's live import rows
through `communities::identity::load_project_imports`, then reports the partition at
three stages: one Leiden pass, plus the oversized split, plus the low-cohesion split.
Each stage prints the community count, the full size distribution, the largest
community's share of visible files, the singleton count, and the median cohesion over
communities of three or more members. The final stage also prints the deterministic
label of every community with five or more members.

```bash
GCODE_EXPERIMENT_DSN=<hub dsn> GCODE_EXPERIMENT_ROOT=<project root> \
  cargo nextest run -p gobby-code --no-capture \
  -E 'test(partition_experiment_reports_distribution)'
```

The test is gated on those two environment variables rather than `#[ignore]`d: an
ordinary suite run has no index to read and returns immediately, so the spike needs no
runner flag and the test-quality audit sees no unconditional skip.

Constants under test, all from Graphify 0.9.55 (`cluster.py:169-172, 298-316`):

| Constant | Value | Where |
| --- | --- | --- |
| Oversized ceiling | `max(10, file_count / 4)`, strict `>` | `OVERSIZED_FLOOR` |
| Low-cohesion member floor | 50 | `LOW_COHESION_MIN_MEMBERS` |
| Low-cohesion ratio | `cohesion < 0.05`, as `40 * E < n * (n - 1)` | `LOW_COHESION_NUMERATOR` |

The ratio is evaluated in `u128` rather than as a float. Graphify's own CHANGELOG
records a split threshold made unreachable by rounding, and the integer form has no
threshold that floating point can round past.

## Corpora

| Corpus | Root | Project id | Files indexed | Imports indexed |
| --- | --- | --- | ---: | ---: |
| Gobby | `/Users/josh/Projects/gobby` | `d45545c5-ded5-4335-b115-0245752edacf` | live | live |
| Game Goblins baseline (C1) | `/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/gcode/C1` | `c8e071af-e1f7-434f-b3c0-f31a1b1299c9` | 142 | 1,050 |

The frozen Game Goblins baseline is the sealed bakeoff artifact the September
scorecards used. It was not a registered Gobby project, so this leaf registered and
indexed it in place (`gobby init --name gcode-bakeoff-C1`, then the index run that
`init` triggers); the corpus tree itself is unchanged apart from the `.gobby/`
directory that registration writes, and it lives outside this repository.

Graphify's published partition of the same corpus is the comparison baseline, with one
difference that has to be stated before any number is read across: Graphify clusters a
heterogeneous graph of 2,308 nodes (1,470 functions, 293 classes, 105 file nodes, 76
rationale nodes, 363 imported-name nodes) and 7,677 edges, while this partition has one
node per visible file and one folded edge per importing pair. Community counts and
median sizes are therefore not comparable term for term; the largest community's
*share* of the clustered nodes is.

| Metric | Graphify C1 |
| --- | ---: |
| Nodes clustered | 2,308 |
| Communities | 116 |
| Largest share of nodes | 3.4% |
| Median size | 17.5 |
| Singletons | 27 |
| Cohesion median, 3+ members | 0.18 |

## Results

Both runs were made on 2026-09-19 from the lane worktree, against the live hub, with the
implementation at this leaf's commit.

### Gobby, project `d45545c5-ded5-4335-b115-0245752edacf`, 7,626 visible files

| Stage | Communities | Largest | Largest share | Median size | Singletons | Median cohesion, n≥3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Leiden only | 2,185 | 718 | 9.42% | 1 | 2,142 | 0.0592 |
| plus oversized | 2,185 | 718 | 9.42% | 1 | 2,142 | 0.0592 |
| plus low cohesion | 2,281 | 164 | **2.15%** | 1 | 2,142 | 0.1305 |

The oversized pass is a no-op here, and not by accident: `max_size` is
`max(10, 7626 / 4) = 1906`, and the largest community Leiden returns is 718. The
low-cohesion pass does all of the work — it splits 96 communities apart, takes the
largest from 718 files to 164, and more than doubles the median cohesion of the
communities big enough to have one.

Above the singletons the distribution is continuous rather than clumped: the non-singleton
sizes run 2, 3, 4, 5, 7, 8, 10 … 120, 128, 131, 152, 152, 164 with no gap, which is what a
partition looks like when no ceiling is binding.

### Game Goblins C1, project `c8e071af-e1f7-434f-b3c0-f31a1b1299c9`, 142 visible files

| Stage | Communities | Largest | Largest share | Median size | Singletons | Median cohesion, n≥3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Leiden only | 57 | 30 | 21.13% | 1 | 52 | 0.3333 |
| plus oversized | 57 | 30 | 21.13% | 1 | 52 | 0.3333 |
| plus low cohesion | 57 | 30 | **21.13%** | 1 | 52 | 0.3333 |

Neither pass fires. `max_size` is `max(10, 142 / 4) = 35`, above the largest community at
30; and the low-cohesion pass never looks at a community under 50 members, which every
community here is. The five non-singleton communities are 12, 15, 16, 17 and 30 files.

### Largest community against the 25% target

Met on both corpora: 2.15% on Gobby, 21.13% on C1. Neither is an exceedance, so no cause
needs recording. The C1 number is close to the target for a reason worth stating rather
than hiding: at 142 files a single cohesive package is a fifth of the corpus, and the
oversized ceiling of 35 sits above it. A corpus that small has no partition that both
respects modularity and keeps every community under a quarter of the files.

### No `community-N` placeholder

The experiment asserts that no derived label begins with `community-`, and both runs pass.
The assertion is a backstop rather than the argument: `derive_label` returns a member path,
a directory prefix, or the highest-in-degree member, and has no branch that can produce an
ordinal placeholder. Graphify's `Community N` fallback exists because its labels come from
node attributes that can be missing; gcode derives every label from paths it already has.

### Communities of five or more files carry subsystem labels

On Gobby, 116 communities have five or more members and every one is named by a path
prefix that identifies a subsystem. A sample across the tree:

| Size | Label |
| ---: | --- |
| 85 | `web/src/components/activity` |
| 65 | `src/gobby/install/shared/skills/impeccable/scripts` |
| 63 | `web/src/components/settings/sections` |
| 54 | `src/gobby/servers/websocket` |
| 52 | `web/src/hooks/useChat` |
| 34 | `src/gobby/memory/dream` |
| 33 | `src/gobby/mcp_proxy` |
| 30 | `crates/gcode/src/index/parser` |
| 27 | `crates/gcode/src/index/indexer` |
| 26 | `src/gobby/agents/watchdog` |
| 25 | `crates/gcode/src/index/import_resolution` |
| 20 | `crates/gcode/src/graph/code_graph` |
| 13 | `src/gobby/adapters/codex_impl` |
| 12 | `src/gobby/hooks/hook_types` |
| 8 | `crates/gcode/src/search/fts` |

On C1 the five labels are `src/game_goblins`, `src/game_goblins/replenishment`,
`src/game_goblins #2`, `src`, and `src/game_goblins/replenishment #2`.

The honest limit of the deterministic rule is visible in the same output: 37 Gobby
communities resolve to the bare prefix `src/gobby` and 26 to `tests`, so they run out to
`src/gobby #37` and `tests #26`. The dominant-directory rule names the deepest directory a
majority of members share, and for a community whose files are spread across sibling
packages that is the top of the tree. Those labels are correct and useless in the same
breath. They are the case plan section 6's model pass exists for, and they are why
`label_candidates` carries the busiest member's stem and the shared directory segment
alongside the deterministic label: on the 37 `src/gobby` communities those alternatives are
the only thing distinguishing them.

## Decision

All three constants are kept at Graphify's values. The evidence does not support moving any
of them:

- **Oversized ceiling `max(10, file_count / 4)`.** Inert on both corpora. Lowering it would
  have acted on Gobby's 718-member community, but the low-cohesion pass already reduces that
  community to at most 164 files, and it does so by splitting where the graph is actually
  sparse instead of wherever a size counter trips. There is no corpus in this leaf where a
  size ceiling beats a cohesion test, so there is no evidence to change the number on.
- **Low-cohesion member floor 50.** The only pass that fires on either corpus, and on Gobby
  it is the difference between a 9.42% largest community and a 2.15% one. Lowering it toward
  C1's scale is tempting and unsupported: C1's communities of 12 to 30 files have a median
  cohesion of 0.33, six times the 0.05 threshold, so a lower floor would not split them
  anyway. It would only add work.
- **Low-cohesion ratio `cohesion < 0.05` as `40 * E < n * (n - 1)`.** Unchanged, and the
  integer form is load-bearing: `cohesion_threshold_uses_integer_math` pins the exact
  boundary at `40 * 77 == 56 * 55`, where the float form rounds the threshold away.

Because no constant moved, `cohesion_threshold_uses_integer_math` and the Method table
above are unchanged from the values this leaf shipped.

### Against the Graphify baseline

| Metric | Graphify C1 | gcode C1 |
| --- | ---: | ---: |
| Nodes clustered | 2,308 | 142 |
| Communities | 116 | 57 |
| Largest, share of nodes | 79 (3.4%) | 30 (21.1%) |
| Median size | 17.5 | 1 |
| Singletons | 27 | 52 |
| Median cohesion, n≥3 | 0.18 | 0.33 |

Only the largest community's share compares term-for-term, and even that comparison favors
Graphify for a structural reason rather than a quality one: it clusters a heterogeneous
graph of 2,308 functions, classes, files, and imported names, where one package contributes
dozens of nodes, while gcode clusters 142 file nodes, where the same package contributes a
handful. A fifth of 142 files and 3.4% of 2,308 mixed nodes can describe the same subsystem.
Community counts, medians, and singleton counts are not comparable at all.

What does transfer is the label comparison, and it is the one that matters for this epic:
across its June, August, and September runs Graphify produced zero semantically named
communities and fell back to hub-node names such as `walker.rs`. gcode names every
community of five or more files after a directory that exists in the repository, on both
corpora, with no model in the loop.
