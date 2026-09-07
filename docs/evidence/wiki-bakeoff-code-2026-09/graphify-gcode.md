# Graphify versus gcode: native evidence-index comparison

- Date: 2026-09-07
- Task: Gobby #21944
- Runtime root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`
- Result root: `results/index-comparison`

## Scope and outcome

This report executes C0-C9 from the frozen [matrix](matrix.md) against the sealed Game Goblins
baseline `0216f1e33f05962d49467d95fe84609041c6dba8` and change
`8b24ac26699aac8b24254a647aa70b208287b492`. It compares native evidence indexing, retrieval,
graph inspection, and presentation. It does not choose a product, define a canonical wiki format,
or treat a missing capability as a zero-quality implementation.

The valid comparison is deterministic. Graphify semantic extraction and community naming were
demonstrated unsupported by the frozen compatibility probes. gcode's embedding doctor could not
verify the required effective endpoint because the scoped identity lacked direct `config_store`
read permission. gcode did create vector projections and hybrid search reported semantic
contributors, but those results are retained only as raw observations and excluded from the valid
retrieval score. No provider, model, execution path, corpus, or answer-synthesis substitute was
used.

The principal observed differences were:

- gcode tracked unchanged and changed files exactly in C2-C6. Graphify's unchanged update rebuilt
  19 uncached files and changed the raw graph from 2,308/9,054 to 2,587/9,321 nodes/edges.
- gcode preserved exact C4 prose, all 12 C5 replacements, and C6 scoped deletion evidence.
  Graphify represented the C4 rationale as a truncated rationale label, represented C5 as two
  delete/add identities with no stale old name, and removed the intended C6 node while an unscoped
  `_ceil` query returned an unrelated helper.
- Both native question surfaces retrieved a frozen gold span for 6 of 14 exact questions. Neither
  tool has native sourced answer synthesis, so no D/I/A answer score is claimed.
- Graphify emitted a complete native wiki/export set after two diagnosed dependency/CLI failures.
  All local wiki links resolved. Its call-flow HTML was not reproducible after normalizing the run
  directory and timestamp because equal-degree neighbors changed order. gcode's graph views and
  text outputs reproduced exactly; its graph report reproduced after removing `generated_at`.
- Graphify's C7 recovery command succeeded but produced the same 2,587/9,321 incremental expansion
  seen in C2, not the clean 2,308/9,054 graph. gcode C7 was blocked before execution because the
  exact PIPE invocation exposes no pre-completion marker or pinned fault-injection seam.

## Frozen identities and validity boundaries

| Surface | Frozen/effective identity | Disposition |
| --- | --- | --- |
| Graphify | package `0.9.55`; executable SHA-256 `4274ea286caa55a83dd6f693056cce24b5ec10bcf91633573dcd6e8efb1f892f`; lock SHA-256 `cd9619377edfc6aea4941e6ed37302b9f5624a52fc005df6e6d10f22ec0a537c` | Deterministic AST, graph, cluster-without-labels, retrieval, and exports valid. Semantic generation demonstrated unsupported. |
| gcode | `1.7.0`, CLI contract v8; executable SHA-256 `1f64d7400001a890ab6630ea7a823d8dbaf7e6b18513d3bb3bed6a07075f7652`; source `7394b97c1d88c82f685e788e798de2cfd728ad15`; lock/image SHA-256 `b9fe159f671596a34c4f25232182e7442d5745b864760c9eb6f27074601ab8b5` | AST, BM25/content, graph, JSON/text, and Mermaid valid. Embedding identity blocked-preflight; hybrid results excluded. Native Ask and complete wiki demonstrated unsupported. |
| Operator | `gpt-5.6-sol`, xhigh | Recorded separately from comparator work. All reported comparator runs used zero model calls and zero generation tokens. |

Unknown telemetry is recorded as the literal string `"unknown"`. The compatibility and service
boundary are documented in [compatibility.md](compatibility.md) and
[environment.md](environment.md). C0 receipts are under
`results/index-comparison/setup/graphify-sql` and
`results/index-comparison/setup/graphify-scipy`.

Graphify's installed SVG path initially failed because its `svg` extra did not install SciPy even
though NetworkX required it for this graph size. The single retry installed exact locked SciPy
`1.17.1`. The initial SQL extraction likewise omitted 11 SQL files until exact locked
`tree-sitter-sql==0.3.11` was installed. Both failed attempts and repair receipts remain in the
inventory.

## C0-C9 result table

| Case | Graphify disposition/result | gcode disposition/result | Cross-tool observation |
| --- | --- | --- | --- |
| C0 | Pass after one diagnosed SQL-parser retry: 2,308 nodes, 9,054 edges, 2.961 s | Deterministic contract/schema/status pass; embedding identity blocked-preflight | Deterministic lanes valid; semantic lanes not comparable |
| C1 | Pass: 119 code files, 2,308 nodes, 9,054 edges, 2.430 s | Pass: 142 files, 2,499 symbols, 711 chunks, 35.789 s | Graphify code-only skipped 20 documents; gcode content-indexed non-AST formats |
| C2 | Fail incrementality/equivalence: 19 uncached files, 2,587 nodes, 9,321 edges, 2.108 s | Pass: 0 files/symbols/chunks, 0.117 s | Graphify changed an unchanged index; gcode was a no-op |
| C3 | Result produced with over-rebuild: 26 uncached files, 2,601 nodes, 9,396 edges, 2.533 s | Pass: exactly 9 files, 298 symbols, 82 chunks, 3.121 s | Both exposed the changed state; only gcode matched changed-file scope |
| C4 | Partial/fail exact-rationale criterion: one rationale node changed, but its label ended in an ellipsis | Pass: 1 changed file; new exact sentence 1 hit, old sentence 0 hits | Graphify captured rationale identity but not the complete exact prose |
| C5 | Pass stale-removal; delete/add identity: 2 nodes and 11 edges removed, 2 nodes and 11 edges added; old name 0 | Pass: 4 files; 12 new-name prefix hits, old name 0; new definition/callers/usages resolved | Neither result retained a stale old name; Graphify did not preserve rename identity |
| C6 | Partial: intended helper node removed; net -1 node/-3 edges versus C2, but unscoped query returned another `_ceil` | Pass: 1 file; scoped `_ceil` symbol/grep both 0, `ROUND_CEILING` 2 | Extraction deletion succeeded in both; Graphify query scope caused a false-positive retrieval |
| C7 | Fail equivalence: recovery 2,587/9,321 versus clean 2,308/9,054 | Blocked-preflight: exact PIPE command exposes no safe marker/seam | No manual repair, corpus inflation, PTY substitution, or fabricated marker |
| C8 | Retrieval 6/14; all exact-question traversals exceeded or were truncated at the 2,000-token budget; Ask unsupported | Deterministic content retrieval 6/14; hybrid captured but excluded; Ask unsupported | These are retrieval scores, not answer-accuracy scores |
| C9 | Native wiki/HTML/Obsidian/SVG/GraphML/tree/call-flow emitted; wiki links valid; call-flow order drift | Native JSON/text/Mermaid/report emitted; report stable modulo timestamp; complete wiki unsupported | Missing presentation surfaces are explicit dispositions, not substitutions |

Raw per-case measurements are in `results/index-comparison/measurements.json`; every command's
normalized record is in `results/index-comparison/run-records.normalized.jsonl`.

## C1-C6 deterministic measurements

Graphify `uncached` is the final native progress count. gcode counts are the fields emitted by its
native JSON index response. gcode reported successful graph and vector projection sync for every
changed file, but the vector lane is not treated as valid semantic evidence because endpoint
identity was not established in C0.

| Case | Graphify seconds | Graphify nodes/edges | Uncached files | gcode seconds | gcode files | Symbols/chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| C1 | 2.430 | 2,308 / 9,054 | 119 | 35.789 | 142 | 2,499 / 711 |
| C2 | 2.108 | 2,587 / 9,321 | 19 | 0.117 | 0 | 0 / 0 |
| C3 | 2.533 | 2,601 / 9,396 | 26 | 3.121 | 9 | 298 / 82 |
| C4 | 2.485 | 2,587 / 9,321 | 20 | 0.428 | 1 | 13 / 4 |
| C5 | 2.430 | 2,587 / 9,321 | 23 | 0.981 | 4 | 79 / 21 |
| C6 | 2.431 | 2,586 / 9,318 | 20 | 0.421 | 1 | 12 / 4 |

The Graphify C4-C6 deltas use C2's post-update graph as the reference. This separates the fixture
effect from the repeatable 279-node/267-edge expansion that occurred during its first update:

- C4 changed only `src_game_goblins_replenishment_product_types_rationale_80`; node and edge sets
  were otherwise identical to C2. The new label contained “set names do not mask…” but not the
  complete source sentence. The old sentence was absent.
- C5 removed the old production/test node IDs and added corresponding new IDs. Eleven incident
  edge records changed on each side. This is coherent delete/add rename evidence, not identity
  continuity.
- C6 removed `src_game_goblins_replenishment_product_types_ceil`. Compared with C2, 16 exact edge
  records were removed and 13 added; the net graph reduction was the expected one node and three
  edges. Source-location movement accounts for part of the exact-record churn.

The focused evidence is in `results/index-comparison/C4-C6-result-summary.json`; raw native queries
remain under `graphify/C4` through `graphify/C6` and `gcode/C4` through `gcode/C6`.

## C7 interruption and recovery

Graphify was launched through the matrix's event-driven PIPE wrapper. After the previously observed
`AST extraction: 100/119 uncached files (84%)` marker, the wrapper sent exactly one `SIGTERM` to the
separate process group. The process exited nonzero without safety `SIGKILL`. Native `update` and
`check-update` then completed once, followed by one clean comparison extraction.

The recovered graph contained 2,587 nodes and 9,321 edges; the clean graph contained 2,308 nodes
and 9,054 edges. Node and edge multiset hashes also differed. Therefore native recovery did not
meet the matrix's clean-build equivalence criterion. Evidence:

- `results/index-comparison/graphify/C7/interruption.json`
- `results/index-comparison/graphify/C7/interrupted.log`
- `results/index-comparison/C7-summary.json`
- `results/index-comparison/native-artifacts/graphify/C7`

For gcode, the exact C1 JSON invocation writes progress only when stderr is an interactive terminal.
Its normal PIPE output has no pre-completion marker, no qualifying state-file glob was found, and
contract v8 exposes no pinned fault-injection seam. The matrix forbids a PTY/corpus surrogate, so
C7 is `blocked-preflight`, not pass, fail, or demonstrated unsupported. Source anchors and the
expanded command are recorded in `results/index-comparison/command-manifest.json` and
`results/index-comparison/gcode/C7/disposition.json`.

## C8 native retrieval

The score requires at least one returned native node/span to overlap a frozen gold source range.
It deliberately does not infer that a relevant filename proves the complete answer. Graphify was
scored from native `query --budget 2000`; gcode was scored from native `search-content` with a
2,000-token page budget and complete top-20 collection. gcode hybrid `search` output is preserved
but excluded because the semantic contributor's effective endpoint was unverified.

| Question | Expected D/I/A | Graphify gold span (rank) | gcode deterministic gold span (rank) |
| --- | --- | --- | --- |
| Q01 | D | No | Yes (1) |
| Q02 | D | No | No |
| Q03 | D | No | No |
| Q04 | D | Yes (34) | No |
| Q05 | I | No | No |
| Q06 | D | No | No |
| Q07 | D | Yes (17) | Yes (14) |
| Q08 | A | Yes (15) | No |
| Q09 | D | Yes (26) | No |
| Q10 | D | Yes (24) | Yes (9) |
| Q11 | D | Yes (18) | Yes (10) |
| Q12 | D | No | Yes (1) |
| Q13 | D | No | Yes (16) |
| Q14 | D | No | No |

Graphify returned 6/14 gold-supporting traversals in 3.235 aggregate seconds; every traversal
reported truncation or a complete result over the requested budget. gcode returned 280 deterministic
content spans, 6/14 with gold overlap, in 4.950 aggregate seconds. Token usage is `"unknown"` for
both native retrieval-only surfaces.

The structural battery showed:

- R01/R02: both resolved `derive_demand_type`. gcode returned 5 direct callers, 7 usages, and 11
  whole-word text references; Graphify returned explanation and reverse-affected evidence.
- R03: neither tool found a directed path between the exact `derive_demand_type` and
  `load_vendor_workbook_data` endpoints. Graphify's native undirected diagnostic found
  `derive_demand_type <--imports-- queries.py --contains--> load_vendor_workbook_data` in two hops.
  gcode returned `found:false`. The initial overly broad destination probe and its corrected exact
  endpoint are both retained.
- R04/R05: gcode returned 9 imports, a 35-node MCG, and a project report. Graphify returned query,
  explanation, and 20 native god nodes from deterministic no-label communities.
- R06-R08: both exposed native retrieval evidence; gcode additionally preserved content spans,
  exact grep lines, and `symbol-at` bodies.

`results/index-comparison/C8-scores.json` contains per-query latency, rank, bound commit, expected
classification, budget disposition, and top supporting span. Structural measurements are in
`C8-structural-summary.json`. Neither exhaustive frozen command inventory contains a native
sourced-answer command, so both Ask dispositions are `demonstrated-unsupported`.

## C9 native presentation and reproducibility

Graphify exports started from byte-identical copies of deterministic no-label clustered graphs:
C1 had 2,308 nodes/7,677 edges and C3 had 2,601 nodes/8,000 edges. Semantic community labels were
not fabricated. The native outputs were wiki Markdown, interactive HTML, Obsidian, SVG, GraphML,
tree HTML, and call-flow HTML.

| Check | C1 | C3 |
| --- | ---: | ---: |
| Files per completed export run | 2,683 | 3,025 |
| Exact hash matches | 2,681 | 3,023 |
| Wiki Markdown files | 127 | 140 |
| Local wiki links checked | 942 | 973 |
| Missing local wiki links | 0 | 0 |
| HTML documents parsed | 3 | 3 |
| SVG/GraphML XML parsed | Pass | Pass |

Graphify SVG reproduced after normalizing its declared timestamp and Matplotlib-generated clip-path
ID. Call-flow HTML did not: after normalizing the run-directory title and timestamp, equal-degree
neighbors appeared in a different order. Wiki, Obsidian, GraphML, tree, graph JSON, reports, and
the remaining files matched exactly. Wiki pages cite source files and EXTRACTED/INFERRED labels,
but do not preserve source line spans.

The matrix's absolute `tree --root "$CASE_ROOT"` invocation failed in all four attempts because
Graphify compared that absolute path with repo-relative `source_file` values. One bounded retry per
state/run omitted the incompatible root and emitted the native tree; the failed commands remain
preserved. The SVG dependency failure and successful exact-lock retry are likewise retained.

gcode emitted MCG, FCG, class-hierarchy JSON plus Mermaid, project graph report JSON/Markdown,
tree text, and outline text for both C1 and C3, twice. Every surface reproduced byte-for-byte except
the project report, which reproduced after normalizing its `generated_at` field and matching
Markdown line. Contract v8 has no complete wiki renderer, recorded as `demonstrated-unsupported`.

Raw and normalized comparisons are in:

- `results/index-comparison/C9-graphify-comparison.json`
- `results/index-comparison/C9-gcode-comparison.json`
- `results/index-comparison/C9-normalized-comparison.json`
- `results/index-comparison/C9-presentation-validation.json`
- `results/index-comparison/native-artifacts/graphify/C9`

## Extraction gaps, retrieval failures, and interpretation

These categories are kept separate:

| Category | Observed evidence |
| --- | --- |
| Extraction/index gap | Graphify's initial missing SQL parser omitted 11 SQL files; repaired from the frozen lock. Its code-only lane intentionally skipped 20 documents and 10 unclassified files. gcode marked non-AST extensions unsupported while still indexing their safe text chunks. |
| Incremental/state defect | Graphify C2 changed an unchanged graph; C7 recovery reproduced that expansion rather than the clean graph. These are state/equivalence failures, not retrieval failures. |
| Retrieval failure | Both exact-question surfaces found a gold range for only 6/14 prompts. Graphify's unscoped C6 query returned an unrelated `_ceil`; both directed R03 paths were absent. The underlying scoped C6 deletion was nevertheless correct. |
| Interpretation boundary | Neither tool synthesized sourced answers or assigned D/I/A labels. Relevant evidence is not promoted to a correct answer, and an absent gold span is not described as a false factual assertion. |
| Provenance observation | Graphify deterministic edges carry top-level string confidence such as `EXTRACTED` or `INFERRED` and numeric `weight`; native wiki pages preserve the labels. No unsupported numeric-confidence interpretation was imported from memory. |

## Evidence inventory and reproduction

The result root contains 242 normalized native run records. Five raw command records are expected
failures: the first SVG dependency failure and the four absolute-root tree failures. C7 recovery
equivalence is a failed case-level assessment even though its native recovery commands exited
successfully. The final result inventory contains 15,028 files and 326,639,668 bytes.

- Artifact inventory: `results/index-comparison/artifact-inventory.json`
- Inventory SHA-256: `036aa8804dee83d59be9d9af58b5ea39aabe8628e18663da7541a6fb001f384f`
- Inventoried tree SHA-256: `b1bf82542dc46a6eb55a20562b15676fce4c31415b659cfee143674f71ae33b6`
- State tree SHA-256: `bec21c2416586be3bc47630f1b4f0d25160c50b74dfe9a33576837353a69963a`
- Workspace tree SHA-256: `07d674a890a2003c5f7604a972561147199fddbcca9b384b4adfb0f90e0d880e`
- Secret-pattern scan: 0 matches

The external harness is part of that inventory. The principal invocations were:

```bash
uv run python results/index-comparison/casebook.py c0
uv run python results/index-comparison/casebook.py repair-graphify-sql
uv run python results/index-comparison/run_index_cases.py
uv run python results/index-comparison/rerun_graphify_preserve.py
uv run python results/index-comparison/run_c7.py
uv run python results/index-comparison/run_c8.py
uv run python results/index-comparison/run_c8_supplement.py
uv run python results/index-comparison/run_c8_deterministic.py
uv run python results/index-comparison/run_fixture_queries.py
uv run python results/index-comparison/run_fixture_supplement.py
uv run python results/index-comparison/run_c9.py
uv run python results/index-comparison/resume_c9.py
uv run python results/index-comparison/retry_c9_tree.py
uv run python results/index-comparison/analyze_evidence.py
uv run python results/index-comparison/refresh_inventory.py
```

Run these from `/Users/josh/Projects/wiki-bakeoff-code-2026-09`, except the matrix/environment
validators, whose paths are fixed in this repository. The scripts refuse to overwrite their native
attempt artifacts. They operate only on sealed copies, isolated state, and isolated services; the
Game Goblins checkout and frozen corpora were not modified.
