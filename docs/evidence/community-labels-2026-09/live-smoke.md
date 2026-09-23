# Import communities live smoke (Q1.8, #22605 acceptance 7.1.3)

The Q1.8 command list ran twice on 2026-09-23 (times CDT), in the Gobby main checkout
against the live hub. Run 1 found two defects. Both were fixed under #22605, and run 2
repeated the full list on the fixed binary.

| Run | When | Installed gcode | Main checkout HEAD |
| --- | --- | --- | --- |
| 1 | 16:3x-16:55, after the PD's migration-449 cutover (all-clear 16:33:55) | gcode 1.9.0, contract 11 | 1d9a45b3a7 |
| 2 | 18:10-18:18, after the PD re-promoted gcode with the fixes (installed 18:07:47) | gcode 1.9.0, contract 11 | c866167210, which moved to 640e4b7b6b during command 7 |

## Pre-change baseline

The baseline was not re-run live. gcode 1.9.0 (contract 11) was already installed when
this smoke ran, so no pre-change binary was left to run against the live hub. The only
older binary, `~/.gobby/bin/.gcode.bak-v435`, is gcode 1.7.0. Running it against the
migration-443 hub risks a stale-index refresh that writes with old code.

The baseline comes from source instead. At 4050b98ec8,
`crates/gcode/src/commands/graph/view/mcg.rs` `assign_leiden_communities` ran the
analytics `communities` pass inline over the view's own nodes and edges. Each MCG view
therefore partitioned only the subgraph it fetched. The plan's research note (Q1.8)
records the observed result: the same MCG call yielded one community per node.

## Run 1: two defects

Command 1 reported `skipped_unchanged true`. Commands 3 to 7 passed, and run 2 below
repeats them with only the labels changed. Command 2 found two defects.

1. `communities[]` was ordered by id string: `community:1` (123 members),
   `community:10` (121), `community:100` (19), `community:102` (14), and so on. Plan
   ordering is size desc, then `members[0]` asc.
2. Stored deterministic labels collided. Among the 129 communities with at least 5
   members, 33 were labeled `src/gobby` and 21 `tests`, across 8 duplicated labels.
   `dedupe_labels` existed but no production path called it.

## Fixes

Fix commit cce32ed777 (lane merge 89cf31657c, landed on 0.5.0 as c866167210):

- The communities view sorts by partition order: size desc, then `members[0]` asc.
- `assign_ids` dedupes the derived labels in current partition order. Every write of a
  deterministic label uses the deduped value.
- Refresh stores `LABEL_ALGORITHM_VERSION:partition_signature` (version 2). Each
  project's next `gcode index` therefore misses the old unversioned key and recomputes
  its labels once, with no manual step.

## Run 2: commands and outcomes

1. `gcode index`
   - First run: `communities 2326, changed 77, new_ids 0, retired_ids 0,
     skipped_unchanged false`. This is the one-time label recompute. No ids were added
     or retired, so only labels changed.
   - Second run: `changed 0, skipped_unchanged true`.
2. `gcode graph view --view communities --min-size 5 --format json`
   - Sizes descend: PASS, 129 communities. Communities 8 and 4 tie at 109 members, and
     8 comes first on `members[0]` (`gobby-annotate/...` sorts before `src/gobby/...`).
   - Labels name subsystems: PASS.
     - The 20 model labels include `Runner Lifecycle Management` (123 members) and
       `Workflow Rules and Tests` (121).
     - 109 deterministic labels have no duplicates; 57 carry an ordinal suffix, such
       as `src/gobby #9`.
     - `label_stale` is false everywhere.
   - `edges[]` join `community:` ids with counts: PASS. 1566 edges, all with both ends
     in the listed set, a positive count, and no self-loops.
   - Mermaid: PASS, a `flowchart TB` block.
3. `gcode graph view --view communities --community memory`
   - Exits 2 with a typed usage error, `community selector 'memory' is ambiguous`,
     listing 5 matches (66, 102, 111, 174, 189). This matches plan 4.3.10.
   - `--community 66` gives the detail view: `src/gobby/memory/dream`, 42 members,
     the 12-neighbor cap (13 community nodes, itself included), `incoming_truncated` true, and one Mermaid subgraph
     `community_66`.
4. `gcode graph view --view communities --community crates/gcode/src/commands/graph/view/mcg.rs`
   - Resolves by member path to `community:63`, `crates/gcode/src #2` (35 members).
     This is the community the MCG view labels.
5. `gcode graph view --view mcg --file crates/gcode/src/commands/graph/view/mcg.rs`
   - `mcg.rs`, `mcg/fetch.rs`, `mcg/identity.rs`, `mod.rs`, and `render.rs` all carry
     `community:63` and share one Mermaid subgraph.
   - Uniquely provided `module:` nodes carry the provider's community: 63 for
     `crate::commands::graph::view::*`, 110 (`crates/gcode/src/graph`) for
     `crate::communities::*`.
   - External modules (`std::*`, `gobby_core::*`) and unresolved ones (`fetch::run`)
     carry `null`.
   - Two communities form two subgraphs, against a baseline of one community per node.
6. `gcode graph report`
   - The markdown has `## Import communities`, with `2326 communities (2192 thin, below
     3 members)` and a table sorted by size desc, led by the ten model-labeled
     communities.
7. The `communities` evidence request
   - `gobby-ask` MCP `evidence` with `{operation: communities, selector: {community_id:
     66}}` bound to 640e4b7b6b. It returns fingerprint `8e7405753645...` and one item,
     `com:53ffee47345367c0...`, with size 42 and `member_signature a4cfa0217291bda8`.
   - `gcode evidence --request-json` with the same request (binding 640e4b7b6b,
     `max_bytes` 16384) returns the same fingerprint and `evidence_id`.
   - List mode (`min_size 5`, `limit 40`, binding c866167210) returns 15 of 40 items
     within the 64 KiB budget, marked `paginated`, in size order.
   - Run 1's `evidence_id` for community 66 was `com:e42565432f03...`, bound to
     1d9a45b3a7. `member_signature` is unchanged, so the id differs by binding only.

## Relabel check: `evidence_id` survives a label change

This check ran at 17:41, between the runs, bound to 1d9a45b3a7 (tree 3d7f9374db):

1. The list request was saved before the 17:33 label tick.
2. After the tick, the same request was re-run. It returned the same fingerprint
   (`83b1ccfe...`).
3. Six communities went from deterministic to model labels:
   - 42 `src/gobby` became `Code Index Maintenance`.
   - 14 `src/gobby` became `Session Lifecycle Hooks`.
   - 15 `src/gobby` became `AI Configuration and Services`.
   - 21 `tests` became `Configuration and Services`.
   - 16 `crates/gclient/src` became `Live Workspace Client`.
   - 19 `tests` became `Authentication and Runtime Grants`.
4. All 16 `evidence_id` values were unchanged. The id hashes the binding, the community
   id, and `member_signature`, never the label.

## Observation: tie order differs across surfaces

Sizes descend on all three read surfaces, but they break size ties differently:

- The communities view breaks ties on `members[0]`, per the PD ruling. Run 2 lists
  community 8 before community 4.
- `gcode graph report` (`GraphReportCommunities::from_rows`) and the `communities`
  evidence operation (`crates/gcode/src/evidence/communities.rs`) break ties on
  `community_id`, the plan's stored-row order (`member_count DESC, community_id`). Both
  list community 4 before community 8.

This is not a correctness failure, since each order is deterministic. Whether to unify
them is left to the PD.
