# Lane-Rename Historical Allowlist

Contract for the repo-wide `lane_a` / `lane_b` / `LaneB` sweep introduced by
plan `wiki-gap-closure` §2.1 (task #18564). The generation-lane codenames are
retired; current source, config, and docs must stay grep-clean of them. The
locations below are historical artifacts and are the only places the old
names may appear:

| Location | Reason |
| --- | --- |
| `.gobby/plans/**` | Plan records and work logs quote the pre-rename identifiers they changed |
| `.gobby/memories.jsonl` | Review-lesson evidence records reference pre-rename test names |
| `docs/contracts/lane-rename-allowlist.md` | This contract names the banned identifiers |

Kept names (already semantic, never part of the rename): the frontmatter
field `lane: tool_loop`, the `LANE_TOOL_LOOP` constant, and
`lane_observability_from_content`.
