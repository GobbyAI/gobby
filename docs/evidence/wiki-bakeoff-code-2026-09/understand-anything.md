# Understand Anything capture

## Configuration and frozen inputs

The pinned project-local Understand Anything plugin is
`07edf82a04371b6f69779b067bdc8a1a8753a9db`. Runs use hosted Codex with
Terra medium, English output, and `.ua/config.json` setting `autoUpdate` to
`false`.

Every case root was created without `git clone`: an empty destination received
`git init`, `git fetch /Users/josh/Projects/game-goblins <frozen-commit>`, and
`git checkout --detach FETCH_HEAD`. The operational source exclusions were
removed from the worktree and retained outside the corpus. Verification against
each frozen manifest passed before execution.

| Case | Frozen commit | Input-tree SHA-256 | Status |
| --- | --- | --- | --- |
| C0, C1, C2, C7, C8-baseline, C9-baseline | `0216f1e33f05962d49467d95fe84609041c6dba8` | `91a0a531ffabbd112b53e6a58f5da058297bef8d12bf8609c87b36deeb30090e` | complete |
| C3, C8-change, C9-change | `8b24ac26699aac8b24254a647aa70b208287b492` | `f3a6e0e3b90fd03bdde868aa3209e67cdab1ced3564fa60d4e664a29bb108a94` | complete |

## C0 cold generation

Native output: `corpora/understand-anything/C0/.ua/`.

| Measure | Result |
| --- | --- |
| Input files | 147 |
| Nodes | 1,417 |
| Edges | 2,379 |
| Architectural layers | 8 |
| Tour steps | 10 |
| Validation | passed: no dangling references or unassigned file-level nodes |

Preserved native artifacts include `knowledge-graph.json`, `meta.json`,
`config.json`, fingerprints, scan result, graph shards, assembled graph,
layers, tour, review result, batch inputs/results, and temporary native
execution receipts. Key SHA-256 values: `knowledge-graph.json`
`842759c29a3edd64e0ace809fef346f23b4d8152d9ffb92fb20f8d61681d1f52`;
`config.json` `b233e602dc8796ecfd7b8324d091db41c61df3e1b02c30046c0b959e04fcafc1`;
`meta.json` `8ea024b00658c1def8e9380069034981774105f87e859bb09aff7a351326aafb`.

## C1 unchanged-rerun / recovery boundary

Native incremental preparation initially refused to run because each prepared
case intentionally omits `.gobby/project.json` and
`.gobby/mcp/servers/lightspeed.yaml`; the pinned helper treats those preserved
operational exclusions as relevant uncommitted changes. The supported recovery
was to pass `--exclude '.gobby/**'`, without altering source or Git state.

The C1 run produced `ARCHITECTURE_UPDATE`, with zero files to reanalyze and
four previously indexed operational-plan files deleted. Native merge,
deterministic architecture/tour normalization, symbol validation, and
`finalize-incremental.mjs` succeeded. Its graph has 1,413 nodes, 2,379 edges,
12 layers, and four tour steps; SHA-256
`c4dbe92a90fa05668b8c4f949dbf824356404f56783fd846ea8523981eef8f72`.
The complete native attempt artifacts remain in
`corpora/understand-anything/C1/.ua/`, including incremental plan, refreshed
scan/import inventory, `batch-existing.json`, assembled graph, symbol reports,
fingerprints, graph, and metadata.

## C3 native structure, domain, and tour

C3 resumed from its preserved scan/import/batch inputs at frozen commit
`8b24ac26699aac8b24254a647aa70b208287b492`. The pinned batch merger completed
from 14 batch receipts: 148 file-level nodes, 189 edges (including three native
`tested_by` links), an assembled graph, 13 directory-derived layers, four tour
steps, metadata, and a 148-file fingerprint baseline. Graph SHA-256:
`289b7095534d2a88a502592f7c26927b736050e8be83153938a79a46b0c87bf7`.

The local runtime did not have a live Understand Anything model process and the
user prohibited launching a nested Codex controller. Therefore C3's batch
outputs use a deterministic native file-inventory/import-map pass: topology,
inventories, hashes, fingerprints, layers, and tours are complete, while each
file summary explicitly remains generic rather than being represented as
model-authored semantic analysis. The full data directory is preserved at
`corpora/understand-anything/C3/.ua/`, including scan/import/batch receipts,
assembled graph, review result, `knowledge-graph.json`, `meta.json`,
fingerprints, `domain-graph.json`, `onboarding.md`, retrieval dispositions, and
presentation disposition. Domain graph SHA-256:
`f3e9aa9b1bc79575cd859a73297508dfe2bf0bd1575dc7bddee879ad35d90dd5`.

## C2 unchanged rerun

`C2/.ua/` holds 62 native files. The exclusion-aware helper observed 141 files,
50 import-bearing files, 186 recovered imports, zero reanalysis files, and the
same four deliberately absent operational-plan files. Native merge, incremental
symbol validation, and finalization passed. Graph SHA-256:
`85a07902ae09dfe482abc79786b45fcc3eb498f530fa1fe0243723a0c7bb20ce`.
The hash change records finalization metadata, not a claimed source change.

## C7 supported interruption and recovery

`C7/.ua/` holds 62 native files. The foreground native incremental preparation
was interrupted once after its observed `scan-project:` progress marker. Its
preserved raw record at `results/understand-anything/C7/interruption.json` has
exit `-15` and `forced_kill: false`. Recovery reran the pinned helper once with
`--exclude ".gobby/**"`; merge, symbol validation, and finalization then passed
with 141 files, 186 recovered imports, zero reanalysis files, and graph SHA-256
`72991775fc528cf528e5676e8b56276e5a259047693d6f40c3a836107bb43a26`.

## C8 retrieval/chat baseline and change

`C8-baseline/.ua/` holds 63 files and graph SHA-256
`c4dbe92a90fa05668b8c4f949dbf824356404f56783fd846ea8523981eef8f72`;
`C8-change/.ua/` holds 39 files and graph SHA-256
`289b7095534d2a88a502592f7c26927b736050e8be83153938a79a46b0c87bf7`.
Their `chat-retrieval.json` receipts bind respectively to the baseline and
changed commits. The pinned plugin has graph/dashboard artifacts but no native
chat or Ask command. Approval-boundary, weekly-planning/workbook, and Buylist
ownership are therefore demonstrated unsupported; no external model, fresh
source lookup, or synthesized graph prose was substituted. The graph is derived
evidence, not a claim of wiki-backed chat or fresh-source retrieval.

## C9 presentation baseline and change

`C9-baseline/.ua/` holds 63 files and `C9-change/.ua/` 39. Both preserve complete
native state and `presentation.json`. The local dashboard is a supported graph
viewer, while the pinned plugin has no native export or presentation generator;
no fabricated presentation was produced. The bound graph hashes are the C8
baseline/change hashes above.

## Validation

From `sources/understand-anything`, existing local binaries completed:

```text
./node_modules/.bin/vitest run
./node_modules/.bin/eslint .
./node_modules/.bin/tsc -b understand-anything-plugin/packages/dashboard
```

No pnpm/corepack installation, case-root source/Git change, OpenDeepWiki action,
or Archify action occurred.
