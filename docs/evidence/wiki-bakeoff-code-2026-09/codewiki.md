# CodeWiki native wiki-generation evidence

- Date: 2026-09-07
- Task: Gobby #21945
- Runtime root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`
- Result root: `results/codewiki`

## Scope and outcome

This report executes the CodeWiki-applicable C0-C3 and C7-C9 cases from the frozen
[matrix](matrix.md) against the sealed Game Goblins baseline
`0216f1e33f05962d49467d95fe84609041c6dba8` and changed commit
`8b24ac26699aac8b24254a647aa70b208287b492`. It uses CodeWiki's native generation,
incremental update, recovery, and GitHub Pages renderer. It does not implement missing retrieval,
Ask, or standalone export features.

The principal results are:

- C1 produced a complete 20-file wiki in 1,924.047 seconds with 17 native model calls. The output
  covers the shared platform, Lightspeed, replenishment, reporting, Restocks, and Buylist, and
  includes 15 Markdown documents, module hierarchies, metadata, 82 Mermaid blocks, and native HTML.
- C2 correctly detected the unchanged commit in 1.523 seconds, made zero model calls, and preserved
  C1's exact output inventory hash.
- C3 detected the exact nine-file Git change and regenerated six Markdown layers plus metadata and
  HTML in 469.495 seconds with six model calls. It captured longest-prefix precedence but omitted
  the tracked `Hobby Supplies = 2` and `Sleeves: = 4` values, and retained C1's stale dependency-
  graph file beside the new C3 graph. C3 therefore produced useful changed-state documentation but
  failed the complete Q14/stale-artifact criteria.
- C7 sent exactly one planned `SIGTERM` after an observed native Markdown state marker. The first
  recovery attempt used the wrong cold argv and timed out at an overwrite prompt; the diagnosed
  retry used native `--update`, completed with 15 calls after reusing two completed pre-interruption
  calls, and matched C1's frozen source, analyzer statistics, top-level module count, required
  files, and domain coverage. Its LLM-derived tree and filenames differed and it emitted one additional
  Markdown module; that variance is preserved rather than normalized away.
- The exhaustive C0 inventory demonstrates that CodeWiki 1.0.1 has no native retrieval/search,
  sourced Ask, or standalone post-generation export command. C8 records all three exact P3 prompts
  and the R01-R08 battery as `demonstrated-unsupported` with zero substitute calls.
- C9 reproduced both native HTML files and complete output inventories byte-for-byte. Presentation
  quality nevertheless failed: each overview contains five machine-local absolute links, no
  generated page has a source-line citation, and the changed presentation does not contain the
  complete Q14 evidence.

## Frozen identity and execution controls

| Surface | Frozen/effective identity | Disposition |
| --- | --- | --- |
| CodeWiki | Source `2584854d7538dc3e3e8e6839cf8590b0cd12a431`; CLI 1.0.1; executable SHA-256 `bfbd257897dc32c10f7e6cb36fe7036de3e51d1fa3422536f68a9fce67b87761`; lock SHA-256 `a8f6be5716150dac5012172fc66a2dc613ffe62a7ef7876f91b37747983f66fa` | Native config, generation, incremental update, recovery, MCP generation, and GitHub Pages presentation are valid. Native retrieval/Ask and standalone export are demonstrated unsupported. |
| Hosted adapter | Native CAW `codex` backend; `gpt-5.6-terra`; medium reasoning | Effective provider/model/reasoning matched every completed hosted trajectory. |
| Operator | `gpt-5.6-sol`; xhigh reasoning | Recorded separately from comparator model work. |

The adapter redirects CodeWiki's three configuration constants into per-attempt state and sets an
isolated `CAW_HOME`; it never repurposes `HOME` or changes global CodeWiki configuration. Hosted
runs were serial, native model-call concurrency was one, the deadline was 60 minutes, and only C7
used the one permitted diagnosed whole-run retry. Unknown quota and counters remain the literal
string `"unknown"`. Provider eligibility and the native child argv are established by
[compatibility.md](compatibility.md); corpus and daemon boundaries are in
[environment.md](environment.md).

CAW emitted a warning during hosted runs that its Codex adapter cannot enforce individual
PARALLEL/WEB/INTERACTION restrictions and instead applies sandbox-level mappings. The run records
now derive this warning from preserved stderr. It is not a provider/model fallback; no fallback or
degraded source was observed.

## C0-C9 result table

| Case | Disposition/result | Principal observation |
| --- | --- | --- |
| C0 | Pass: five native version/help/config/MCP commands, 4.340 s total | Pinned command inventory established native generation/update/presentation and absent retrieval/Ask/export commands. |
| C1 | Pass: 20 files, 2,896,639 bytes, 1,924.047 s, 17 calls | Complete cold wiki with hierarchy, metadata, diagrams, and HTML; inventory `6fc73c3a...d095db4e0`. |
| C2 | Pass: 1.523 s, zero calls/tokens | Native `--update` detected identical HEAD and preserved the exact C1 inventory. |
| C3 | Partial/fail: 21 files, 5,447,148 bytes, 469.495 s, six calls | Exact nine-path change detected; generic longest-prefix behavior present, exact tracked values absent, stale C1 graph retained. |
| C7 | Pass after one diagnosed retry | Two completed calls survived interruption; native `--update` completed 15 remaining calls and restored equivalent stable baseline scope. Exact model-derived tree/path equality is false. |
| C8 | Demonstrated unsupported | Three exact P3 Ask prompts plus R01-R08 retrieval recorded; zero native or substitute model calls. |
| C9 | Determinism pass; presentation-quality fail | Native HTML and complete inventories reproduced exactly; five absolute links/state, zero source-line citations, and incomplete Q14 coverage. |

Raw records are in `results/codewiki/run-records.jsonl`; per-attempt records, stdout/stderr, exact
prompts, normalized trajectories, summaries, and output inventories remain under each case.

## Cold and incremental measurements

| Case | Seconds | Model calls | Input tokens | Output tokens | Cache tokens | Output files/bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| C1 | 1,924.047 | 17 | 3,462,983 | 75,014 | 2,870,784 | 20 / 2,896,639 |
| C2 | 1.523 | 0 | 0 | 0 | 0 | 20 / 2,896,639 |
| C3 | 469.495 | 6 | 1,130,082 | 21,983 | 951,808 | 21 / 5,447,148 |

C1 and C2 have the identical output inventory SHA-256
`6fc73c3a1f59bde87af2e6359428bd9461f4767ab9c49e8d54158c6d095db4e0`.
C3's inventory is `29cd99dde5e16baca4897dddc8f81b3a50ba437b0368e93a1b1a1a3eaf3edd58`.
The C3 source diff was verified before execution as eight modified files and one added test file.

C3 changed these existing artifacts:

- `application_platform_and_integrations.md`
- `demand_intelligence_and_replenishment_planning.md`
- `overview.md`
- `replenishment_execution_and_catalog_operations.md`
- `replenishment_overrides_and_daily_operations.md`
- `replenishment_planning.md`
- `metadata.json`
- `index.html`

It added `temp/dependency_graphs/C3_dependency_graph.json` without removing
`temp/dependency_graphs/C1_dependency_graph.json`. The new Markdown explains that a longest
matching name prefix overrides category/default minima and connects that behavior to
`compute_store_targets`, but neither `Hobby Supplies` nor `Sleeves: ` appears. Those observations
are sealed in `results/codewiki/case-analysis.json`.

## C7 interruption and recovery

The event-driven wrapper watched the C0-recorded `docs/*.md` state glob, ran CodeWiki in its own
process group, and sent one `SIGTERM` after the first Markdown document appeared. Neither attempt
required safety `SIGKILL`.

| Stage | Attempt | Seconds | Exit | Calls | Input/output/cache tokens | Files/bytes at boundary |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Interruption | 1 | 311.998 | -15 | 2 | 121,589 / 4,781 / 100,096 | 4 / 2,625,970 |
| Recovery | 1 | 3,600.096 | 124 | 0 | 0 / 0 / 0 | 4 / 2,625,970 |
| Interruption | 2 | 187.083 | -15 | 2 | 94,062 / 4,885 / 63,488 | 4 / 2,619,891 |
| Recovery | 2 | 1,371.068 | 0 | 15 | 3,325,687 / 60,195 / 2,720,000 | 21 / 2,904,407 |

Attempt 1 is a harness invocation failure, not a CodeWiki recovery conclusion: cold generation
encountered CodeWiki's overwrite confirmation and waited until timeout. Attempt 2 supplied native
`--update` with EOF-protected stdin. Because interrupted output lacked final metadata, CodeWiki
loaded the cached module tree, reused the completed document, and generated the missing documents.

Stable comparison against sealed C1 passed:

- source commit, `total_components=1256`, `leaf_nodes=233`, `max_depth=2`, and the top-level
  module count of five match;
- overview, first/final module trees, metadata, HTML, and all baseline domain probes are present;
- interrupted plus recovery calls equal C1's 17 calls; and
- no manual file/database repair occurred.

Exact semantic output did not reproduce: the module tree, module filenames, inventory hash, and
prose differ, with 16 recovered Markdown documents versus C1's 15. The matrix's stable recovery
scope passes, while this model-derived variance remains explicit in
`results/codewiki/C7/recovery-comparison.json`.

## C8 native retrieval and Ask

Frozen `--help`, `generate --help`, `config show`, and `mcp --help` output plus pinned source show no
native query, search, or sourced-answer command. The MCP surface assists generation; it is not a
post-generation retrieval or Ask surface. No general LLM was wrapped around generated Markdown and
reported as native Ask.

The three exact prompts are preserved with these SHA-256 values:

| Question | Prompt SHA-256 | Disposition |
| --- | --- | --- |
| P3-A1 | `e9313e57f4b06cd25dc44a29028737f56e8e8ee93777fbdf076373b30d234ddc` | `demonstrated-unsupported` |
| P3-A2 | `e9126aba01cf12adfeabf7ed355ad43c2d42bdc8204f717c9c3655bba6932b3e` | `demonstrated-unsupported` |
| P3-A3 | `343ab93e60500c96a24337e700505ee3b561d059b50588eabb8773216e285036` | `demonstrated-unsupported` |
| R01-R08 | No prompt/model invocation | `demonstrated-unsupported` |

This is a capability disposition, not an answer-accuracy score. The generated C1 presentation does
contain content sufficient for deterministic keyword coverage of all three P3 topics, but that
content was not retrieved or synthesized through a native question surface.

## C9 native presentation and reproducibility

The frozen native `HTMLGenerator` was invoked twice from unchanged copies of C1 and C3. This tests
the same bundled GitHub Pages renderer used by `generate --github-pages`; it does not claim that a
standalone export command exists.

| Check | C1 | C3 |
| --- | ---: | ---: |
| Markdown documents | 15 | 15 |
| Mermaid blocks | 82 | 84 |
| Output inventory exact across two renders | Pass | Pass |
| Native `index.html` exact across source/two renders | Pass | Pass |
| Broken relative Markdown links | 0 | 0 |
| Machine-local absolute overview links | 5 | 5 |
| Source-line citations / resolvable | 0 / 0 | 0 / 0 |
| P3-A1/A2/A3 topic coverage | 3 / 3 | 3 / 3 |
| Q14 complete changed-policy coverage | Not applicable | Fail |

The HTML renderer is deterministic and the Markdown link graph has no missing relative target, but
the absolute links make each overview non-portable outside the bakeoff machine. Generated prose
names source symbols and files but supplies no source-line citation span, so citation precision
cannot be credited. C3 lacks the complete Q14 policy values. These are presentation/content
failures, separate from renderer determinism, in
`results/codewiki/C9/presentation-validation.json`.

## Evidence inventory and reproduction

The result root contains 20 normalized run records and 390 inventoried files totaling 42,873,788
bytes. `full-inventory.json` excludes itself to avoid recursive hashing.

- Full inventory: `results/codewiki/full-inventory.json`
- Inventoried tree SHA-256: `1155c109fa5863f1a4a5f6152ca6fe19b21a2c7c622a9a71617f70f71f750ac2`
- Run ledger: `results/codewiki/run-records.jsonl`
- Command manifest: `results/codewiki/command-manifest.json`
- Case analysis: `results/codewiki/case-analysis.json`
- Secret scan: pass; one deduplicated candidate hash, 24 derived occurrences, all traced to frozen
  `Buylist/test_buylist_catalog.py`; disposition `frozen-source-test-fixture`; zero unresolved
  candidates. Values are never emitted by the scan.

The principal invocations were:

```bash
uv run python results/codewiki/run_codewiki_cases.py prepare
uv run python results/codewiki/run_codewiki_cases.py c0
uv run python results/codewiki/run_codewiki_cases.py c1
uv run python results/codewiki/run_codewiki_cases.py c2
uv run python results/codewiki/run_codewiki_cases.py c3
uv run python results/codewiki/run_codewiki_cases.py c7
uv run python results/codewiki/run_codewiki_cases.py c8
uv run python results/codewiki/run_codewiki_cases.py c9
PYTHONDONTWRITEBYTECODE=1 uv run python results/codewiki/run_codewiki_cases.py finalize
```

The external harness files are `results/codewiki/codewiki_runner.py`,
`results/codewiki/codewiki_common.py`, `results/codewiki/codewiki_unsupported.py`,
`results/codewiki/codewiki_secret_scan.py`, and `results/codewiki/run_codewiki_cases.py`. Every
Python file is below the 1,000-line ceiling and passes Ruff formatting and lint.
