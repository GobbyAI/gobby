# Code-wiki bakeoff evidence index

Date: 2026-09-10
Epic: Gobby #21926
Evidence root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`

This index is the reproducible handoff for the completed native code-wiki bakeoff. It links the
frozen contract, every comparator report, native artifacts, hashes, measurements, reproduction
entry points, limitations, and failed attempts. It does not choose a product, define a canonical
Gobby wiki format, repair native output, or turn a missing capability into a zero-quality
implementation. The comparator reports and sealed native records remain authoritative when this
summary is less detailed.

## Start here

- [Matrix and casebook](matrix.md): frozen inputs, Q01-Q14 answer key, C0-C9 procedures, and scoring.
- [Environment](environment.md): corpus isolation, installation receipts, services, and teardown.
- [Provider compatibility](compatibility.md): effective provider/model checks and unsupported lanes.
- [Graphify and gcode](graphify-gcode.md): deterministic index, retrieval, graph, and export evidence.
- [CodeWiki](codewiki.md): native full-wiki generation, update, recovery, and HTML evidence.
- [OpenDeepWiki](opendeepwiki.md): local-model generation, update, recovery, Ask, MCP, and export.
- [Grok Wiki](grok-wiki.md): source-backed generation/Ask and native Obsidian/HTML export.
- [Understand Anything](understand-anything.md): native graph, domains, tours, and dashboard evidence.
- [Archify](archify.md): five validated diagram types plus change and last-good evidence.

The external evidence root is intentionally outside Git. It contains frozen corpus copies, isolated
state, native output, raw attempts, sanitized run records, and inventories. Do not move native files
into this documentation directory or edit them to improve presentation.

## Frozen contract and effective identities

The read-only Game Goblins baseline is
`0216f1e33f05962d49467d95fe84609041c6dba8`; the committed change is
`8b24ac26699aac8b24254a647aa70b208287b492`. Their sealed input-tree SHA-256 values are
`91a0a531ffabbd112b53e6a58f5da058297bef8d12bf8609c87b36deeb30090e` and
`f3a6e0e3b90fd03bdde868aa3209e67cdab1ced3564fa60d4e664a29bb108a94`.
The answer key never entered a comparator corpus.

| Comparator | Frozen identity | Effective model or deterministic lane |
| --- | --- | --- |
| Graphify | source `c9f99018774e2e0380e9f65b3959944559a0d5f6`; package 0.9.55; executable `4274ea286caa55a83dd6f693056cce24b5ec10bcf91633573dcd6e8efb1f892f` | Deterministic AST/graph/export only; the required Codex semantic backend is unsupported |
| gcode | source `7394b97c1d88c82f685e788e798de2cfd728ad15`; 1.7.0; contract v8; executable `1f64d7400001a890ab6630ea7a823d8dbaf7e6b18513d3bb3bed6a07075f7652` | Deterministic AST, BM25/content, graph, JSON/text, and Mermaid; semantic results excluded because endpoint identity was not verifiable |
| CodeWiki | source `2584854d7538dc3e3e8e6839cf8590b0cd12a431`; CLI 1.0.1; executable `bfbd257897dc32c10f7e6cb36fe7036de3e51d1fa3422536f68a9fce67b87761` | Native CAW Codex backend; `gpt-5.6-terra`; medium; serial |
| OpenDeepWiki | source `75840e5e86213ca40ace9d5036b1f52603f8d038`; executable `af7d67135527bd490d271a54d4711a74feab2921994ca9e9365d1cb6b3ff0b5c`; image `sha256:4beef5b8919dcaa2dc924233bd069257e883cc7a061e09088a97d152d6a48510` | LM Studio OpenAI-compatible endpoint; `qwen/qwen3.8-27b`; xhigh loaded-model default; serial |
| Grok Wiki | release 0.0.38; official archive `d2f50dcf9a6160234a8cbb00020b59fd9ccc8b008d8dd972f62c42efb4c905f8`; signed DMG `bbcccef258102a1f32e36947b1a6da059a828bf1cea552dfdb58ca35ab562e09` | Native Codex adapter; `gpt-5.6-terra`; medium; native concurrency 1 |
| Understand Anything | project-local plugin source `07edf82a04371b6f69779b067bdc8a1a8753a9db` | Native graph pipeline; Terra/medium requested where available; C3 semantic summaries explicitly remain generic |
| Archify | project-local source `c6519401f7b91b9d43011657880893b0a8955548` | Native deterministic validator/renderer; candidate authoring used the frozen Terra/medium profile |

Operator work is separately identified in the reports as `gpt-5.6-sol` at xhigh. Comparator token
counts never include operator usage. Unknown provider quota or missing telemetry remains the literal
value `unknown` rather than an estimate.

## Complete C0-C9 disposition index

`N/A` means the frozen comparator profile does not own that case. `Unsupported` means the frozen
native contract was inspected and the surface is absent; it does not mean the tool attempted a bad
implementation.

| Comparator | C0-C1 | C2-C3 | C4-C6 | C7 | C8 | C9 |
| --- | --- | --- | --- | --- | --- | --- |
| Graphify | Compatibility retry then pass; C1 2,308 nodes / 9,054 edges | C2 fail: unchanged rebuild; C3 over-rebuild | C4 partial, C5 pass, C6 partial | Recovery command exited 0 but graph-equivalence failed | Deterministic retrieval 6/14; native Ask unsupported | Wiki/HTML/Obsidian/SVG/GraphML/tree/call-flow emitted; call-flow ordering drifted |
| gcode | Deterministic preflight and C1 pass: 142 files / 2,499 symbols / 711 chunks | Exact no-op C2 and exact nine-file C3 pass | C4-C6 exact scoped updates pass | Blocked-preflight: PIPE mode has no safe native interruption marker or seam | Deterministic retrieval 6/14; native Ask unsupported; semantic lane excluded | JSON/text/Mermaid/report emitted and stable modulo timestamp; complete wiki unsupported |
| CodeWiki | C0 pass; C1 complete 20-file wiki | C2 exact no-op; C3 useful output but incomplete Q14 and one stale graph | N/A | Stable-scope recovery pass after one diagnosed wrong-argv retry; generative paths differ | Retrieval and Ask demonstrated unsupported | Renderer deterministic; absolute machine paths, no line citations, incomplete changed-policy coverage |
| OpenDeepWiki | C0 pass; C1 complete 22-leaf wiki, mind map, references, diagrams, and export | C2 native no-op; C3 fail: changed snapshot accepted but content stayed stale | N/A | Native restart/recovery pass; recovered catalog varied to 23 leaves | Execution pass, quality fail: searches empty/weak, source reads unavailable, Ask 1 pass / 1 fail / 1 partial | Member-tree determinism pass; stale changed content and 13 unresolved relative references |
| Grok Wiki | C0 pass after supported launcher override; C1 9-page wiki | C2 full regeneration with new identity/content; C3 generation pass but Q14 content fail | N/A | Interruption captured; resume/update demonstrated unsupported; second generate was a full rebuild | Fresh-source Ask execution pass; answer quality pass / fail / pass | Normalized ZIP members deterministic; portable Obsidian structure, but source citations have empty hrefs |
| Understand Anything | C0 graph pass; C1 exclusion-aware native update | C2 zero-reanalysis native update; C3 topology complete with generic, non-model-authored summaries | N/A | Planned interruption and supported rerun pass | Native chat/Ask demonstrated unsupported | Dashboard graph viewer supported; native export/presentation generator unsupported |
| Archify | C0 identity/preflight; C1 five showcase deliveries pass | No unchanged case; C3 changed architecture delivery passes while native compare rejects missing stable IDs | N/A | Invalid candidate rejected and last-good HTML preserved byte-for-byte | Retrieval/Ask unsupported | Static HTML delivered; browser visual-check skipped because Chrome/Chromium was unavailable; standalone CLI export unsupported |

Every failed, interrupted, blocked-preflight, and diagnosed retry named above remains preserved. A
successful command is not silently promoted to a capability pass: Graphify C7 and Grok C7 are the
important examples. Conversely, unsupported surfaces were not replaced by an external LLM or a
hand-written converter.

## Artifact map and hash anchors

All paths below are relative to `/Users/josh/Projects/wiki-bakeoff-code-2026-09`.

| Evidence | Canonical entry points | Hash or inventory anchor |
| --- | --- | --- |
| Frozen environment | `receipts/`, `logs/`, `templates/`, `corpora/*/corpus-manifest.json` | Installation, service, source/archive, and input-tree hashes are in `receipts/installations.json` and the environment report |
| Graphify + gcode | `results/index-comparison/artifact-inventory.json`; `results/index-comparison/run-records.normalized.jsonl`; `results/index-comparison/measurements.json` | Inventory file `036aa8804dee83d59be9d9af58b5ea39aabe8628e18663da7541a6fb001f384f`; tree `b1bf82542dc46a6eb55a20562b15676fce4c31415b659cfee143674f71ae33b6` |
| CodeWiki | `results/codewiki/full-inventory.json`; `run-records.jsonl`; `command-manifest.json`; `case-analysis.json` | 390 files / 42,873,788 bytes; tree `1155c109fa5863f1a4a5f6152ca6fe19b21a2c7c622a9a71617f70f71f750ac2` |
| OpenDeepWiki | `results/opendeepwiki/command-manifest.json`; per-case run records and inventories under C0, C1, C2, C3, C7, C8, and C9 | C1 inventory `ac3880821f909c0833df6d0d5f9001be5a374779d4baacabb015495712985e50`; the report lists all ten qualifying case hashes |
| Grok Wiki | `results/grok-wiki-21947/evidence-inventory.json`; `run-records.jsonl`; `command-manifest.json`; C1/C2/C3/C7/C8/C9 records | 383 files / 14,959,195 bytes; inventory `ff493c929de80ae32be1b695c19a44e5e75b5765d70614c1ccb3e0e164d81161` |
| Understand Anything | native `.ua/` directories under `corpora/understand-anything/{C0,C1,C2,C3,C7,C8-*,C9-*}`; interruption receipt under `results/understand-anything/C7/` | C0 `842759c29a3edd64e0ace809fef346f23b4d8152d9ffb92fb20f8d61681d1f52`; C1/C8 baseline/C9 baseline `c4dbe92a90fa05668b8c4f949dbf824356404f56783fd846ea8523981eef8f72`; C2 `85a07902ae09dfe482abc79786b45fcc3eb498f530fa1fe0243723a0c7bb20ce`; C3/C8 change/C9 change `289b7095534d2a88a502592f7c26927b736050e8be83153938a79a46b0c87bf7`; C7 `72991775fc528cf528e5676e8b56276e5a259047693d6f40c3a836107bb43a26` |
| Archify | `results/archify/inventory.txt`; `inventory.sha256`; C1 five candidate/HTML pairs; C3 change; C7 invalid candidate | Relative manifest `828b38bb1e47b1ef555a6f3fd2df88b7e1420710c23b437826dfddffc671102b`; every delivered candidate and HTML hash is in the Archify report |

The inventories exclude themselves where required to avoid recursive hashing. OpenDeepWiki's
qualifying inventory hashes are:

- C0: `232d8b4f73899d3a4cb88e3cf3aeec196fbc3f2e5a76cc30c9d1a6a87166b430`
- C1: `ac3880821f909c0833df6d0d5f9001be5a374779d4baacabb015495712985e50`
- C2: `ef57343eac326fc149027ceed99935ed7d3744076fea2d6311f80365a2600617`
- C3: `2be0f0d1551d09a50aa7675c57fb3c859d25246955c528177af1cbaf21ff9346`
- C7 interruption: `5f1404f3276bbdf40442bd2f9f4403c206dcd8f5d858e02b9119df4867fe24b6`
- C7 recovery: `bca8b2ca9315a1ee688b40441757db92b7af618e545d31689af1951e69ee355f`
- C8 baseline: `aff523daf3f9c3598abe2ed87b3e90727bb59cd27ffa5a6424c26b4ce969578f`
- C8 change: `d74a511e0e14f5e60467b8e8503c58453f4efa40aaaa4c0e072c0b18e3fd7136`
- C9 baseline: `8df88dedca074b6e4ec141a5c5c95d8aaf98440fa1b73b65075e0ffc9250be36`
- C9 change: `15eab0cb26ec8b72dba6846a6f6dcebcb9bbc76449905a2d565ee32ea4588396`

## Aggregate measurements and attempt accounting

| Comparator | Baseline / unchanged / changed | Recovery and retrieval | Model accounting |
| --- | --- | --- | --- |
| Graphify | C1 2.430 s; C2 2.108 s; C3 2.533 s | C8 14 queries in 3.235 s | Zero comparator model calls; token telemetry `unknown` for retrieval-only commands |
| gcode | C1 35.789 s; C2 0.117 s; C3 3.121 s | C8 280 deterministic spans in 4.950 s | Zero comparator model calls; semantic contribution excluded |
| CodeWiki | C1 1,924.047 s / 17 calls; C2 1.523 s / 0 calls; C3 469.495 s / 6 calls | Qualifying C7 interruption 187.083 s / 2 calls plus recovery 1,371.068 s / 15 calls | C1 3,462,983 input / 75,014 output; C3 1,130,082 / 21,983; full retry accounting remains in the report |
| OpenDeepWiki | C1 19,308.889 s / 204 calls; C2 10.971 s / 0; C3 13.124 s / 0 | C7 82.219 s interruption + 21,054.451 s recovery / 198 calls; C8 1,145.404 s / 23 calls | Qualifying C1+C7+C8: 425 successful calls, 9,672,873 input / 639,996 output tokens |
| Grok Wiki | C1 810.168 s / 10 calls; C2 648.589 s / 9; C3 603.420 s / 8 | C7 70.311 s interruption + 631.700 s rebuild; three Ask calls total 136.111 s | Successful model-bearing cases: 7,183,620 input, 5,524,224 cached, 135,194 output; 42 recorded starts include the failed-login start |
| Understand Anything | Per-case file/node/edge/layer/tour counts and graph hashes are in native receipts | C7 interruption exit -15 with no forced kill; recovery produced 141 files / 186 imports / 0 reanalysis files | Native task did not expose a complete comparable token ledger; no total is inferred |
| Archify | Five C1 HTML deliveries total 3,529,222 bytes; C3 changed HTML hash is recorded separately | Every final candidate passed 9/9 showcase checks; C7 last-good hash stayed equal | Renderer/validator model calls are zero; candidate-authoring usage is operator work |

Superseded attempts are not folded into qualifying case totals. They remain visible where they
explain a retry: Graphify's missing SQL/SciPy dependencies, CodeWiki's wrong cold recovery argv,
OpenDeepWiki's zero-call summary and interrupted/timeout attempts, Grok Wiki's revoked credential
snapshot and live-JSONL controller race, and native export integration attempts. Reports state
whether a retry is a harness failure, comparator failure, or admissible diagnosed retry.

## Browse the native output

Static files require no listener. Open these directly:

- Graphify: `results/index-comparison/native-artifacts/graphify/C9/C1/run-1/` and `C3/run-1/`.
- CodeWiki: `results/codewiki/C1/generation/attempt-1/docs/index.html` and the corresponding C3
  incremental `docs/index.html`.
- OpenDeepWiki: `results/opendeepwiki/C9-baseline/export-1-content/` and
  `results/opendeepwiki/C9-change/export-1-content/`.
- Grok Wiki: `results/grok-wiki-21947/C9-{baseline,change}/native-canonical/print.html` and each
  `export-1/` directory.
- Understand Anything: the native graph/dashboard state is under each case's `.ua/` directory.
- Archify: the five C1 HTML files and C3 changed HTML under `results/archify/`.

For a disposable Obsidian review, copy native Markdown without rewriting it. Run the following from
the external evidence root; open any generated comparator/case directory as its own vault. The
temporary root is deliberately outside the sealed evidence tree and can be discarded afterward.

```bash
BAKEOFF_ROOT=/Users/josh/Projects/wiki-bakeoff-code-2026-09
BAKEOFF_VAULT=$(mktemp -d /tmp/wiki-bakeoff-vault.XXXXXX)
mkdir -p "$BAKEOFF_VAULT"/{graphify-baseline,graphify-change,codewiki-baseline,codewiki-change,opendeepwiki-baseline,opendeepwiki-change,grok-baseline,grok-change}
rsync -a "$BAKEOFF_ROOT/results/index-comparison/native-artifacts/graphify/C9/C1/run-1/obsidian/" "$BAKEOFF_VAULT/graphify-baseline/"
rsync -a "$BAKEOFF_ROOT/results/index-comparison/native-artifacts/graphify/C9/C3/run-1/obsidian/" "$BAKEOFF_VAULT/graphify-change/"
rsync -a "$BAKEOFF_ROOT/results/codewiki/C1/generation/attempt-1/docs/" "$BAKEOFF_VAULT/codewiki-baseline/"
rsync -a "$BAKEOFF_ROOT/results/codewiki/C3/incremental/attempt-1/docs/" "$BAKEOFF_VAULT/codewiki-change/"
rsync -a "$BAKEOFF_ROOT/results/opendeepwiki/C9-baseline/export-1-content/" "$BAKEOFF_VAULT/opendeepwiki-baseline/"
rsync -a "$BAKEOFF_ROOT/results/opendeepwiki/C9-change/export-1-content/" "$BAKEOFF_VAULT/opendeepwiki-change/"
rsync -a "$BAKEOFF_ROOT/results/grok-wiki-21947/C9-baseline/native-canonical/export-1/local-c1-wiki/" "$BAKEOFF_VAULT/grok-baseline/"
rsync -a "$BAKEOFF_ROOT/results/grok-wiki-21947/C9-change/native-canonical/export-1/local-c3-wiki/" "$BAKEOFF_VAULT/grok-change/"
```

This copies native bytes as-is. It does not merge link namespaces, fix links, add citations, or
convert outputs into a Gobby format. Graphify and Grok Wiki already include native `.obsidian`
configuration. Obsidian may add local workspace metadata only to the disposable copy.

If a static HTML directory is served rather than opened directly, bind only to loopback, for
example `uv run python -m http.server --bind 127.0.0.1 8010`. Do not expose corpus-derived output on
`0.0.0.0` or a public host.

## Reproduction entry points

Run Python through `uv` from the external evidence root unless a report says otherwise.

| Surface | Principal entry point |
| --- | --- |
| Contract/environment | `uv run python /Users/josh/Projects/gobby/docs/evidence/wiki-bakeoff-code-2026-09/validate_matrix.py`; `uv run python /Users/josh/Projects/gobby/docs/evidence/wiki-bakeoff-code-2026-09/validate_environment.py --live` |
| Graphify + gcode | `uv run python results/index-comparison/run_index_cases.py`; focused C7/C8/C9 and inventory scripts are listed in the report |
| CodeWiki | `uv run python results/codewiki/run_codewiki_cases.py <prepare|c0|c1|c2|c3|c7|c8|c9|finalize>` |
| OpenDeepWiki | `uv run python results/opendeepwiki/run_opendeepwiki_cases.py <case>`; exact API, proxy, and export commands are in `command-manifest.json` |
| Grok Wiki | `GROK_WIKI_SERVER_ENTRY=<bundle>/Contents/Resources/server/rlm-wiki.js` plus the exact native command in the report; harness and validator are under `results/grok-wiki-21947/` |
| Understand Anything | Pinned project-local plugin helpers; validate with the existing Vitest, ESLint, and dashboard TypeScript commands listed in the report |
| Archify | `node bin/archify.mjs validate|deliver|visual-check ... --quality showcase --json` from `sources/archify/archify` |

The OpenDeepWiki proxy is direct to LM Studio and independent of Gobby daemon restarts. Its request
and response bodies remain sealed; only hashes, sizes, timing, status, model identity, and token
metadata are admissible evidence. Grok Wiki's temporary authenticated Codex home was removed after
the last model call and is absent from the canonical inventory.

## Observations for the separate analysis epic

These are evidence summaries, not adoption decisions:

- gcode produced the most exact deterministic incremental accounting in C2-C6 and stable native
  graph/text presentation, but it has no native full-wiki or sourced-answer surface.
- Graphify produced the broadest deterministic graph/export set, including a native Obsidian vault,
  but unchanged and recovery runs over-rebuilt and the required semantic backend was unavailable.
- CodeWiki produced a complete, diagram-rich wiki with efficient unchanged detection and native
  stable-scope recovery, while retrieval/Ask is absent and changed output retained stale/incomplete
  evidence.
- OpenDeepWiki produced the largest complete local-model wiki and recovered without manual repair,
  while copied-directory incremental detection, native retrieval/source reads, and export
  portability all showed material failures.
- Grok Wiki produced compact native Markdown/Obsidian output and strong fresh-source answers for two
  of three prompts, while unchanged and recovery paths regenerate from scratch and C3 missed exact
  changed-policy facts.
- Understand Anything preserved the richest native structural graph/state surface, but no native
  Ask/export exists and C3 semantic summaries are intentionally generic.
- Archify delivered the strongest validated diagram-specific HTML artifacts, but it is a renderer,
  not a repository wiki/retrieval system, and browser visual checks were unavailable on the host.

Inputs ready for later file-by-file analysis are the native Markdown/HTML/JSON/graph artifacts,
Q01-Q14 result records, source-line anchors, inventories, and effective-identity receipts. That
later analysis may select capabilities, specify canonical examples, and assess gcode gaps; this
evidence package deliberately does none of those things.

## Unsupported capabilities and unresolved result failures

Demonstrated unsupported capabilities:

- Graphify: required Codex semantic generation and native sourced Ask.
- gcode: native sourced Ask and complete wiki generation; C7 has no safe PIPE interruption marker.
- CodeWiki: native post-generation retrieval/Ask and standalone export command.
- Grok Wiki: incremental update and interrupted-run resume/recovery.
- Understand Anything: native chat/Ask and native export/presentation generator.
- Archify: native corpus retrieval/Ask and standalone CLI export.

Observed comparator failures that remain intentionally unresolved in native output:

- Graphify over-rebuilds unchanged input, fails C7 clean equivalence, and has nondeterministic
  equal-degree call-flow ordering.
- CodeWiki C3 omits exact policy values and retains a stale dependency graph; its presentation has
  machine-local links and no source-line citations.
- OpenDeepWiki C3 records a changed commit without regenerating content; C8 retrieval/source reads
  fail quality; C9 has stale changed content and 13 unresolved references.
- Grok Wiki C2 is a fresh nondeterministic rebuild, C3 misses exact Q14 policy facts, C7 does not
  resume, P3-A2 misses the planning-stage order, and exported citation hrefs are empty.
- Understand Anything's C3 semantic descriptions are generic rather than model-authored, and its
  absent Ask/export surfaces stay absent.
- Archify's Chromium-dependent visual check remains skipped; native compare rejects the original
  connections because they lack stable IDs.

Required baselines are nevertheless complete: every comparator has either the native output its
frozen profile requires or direct demonstrated-unsupported evidence, and every applicable case has
a measurement or a bounded preflight disposition. There is no remaining task-level execution
blocker. The two separate Gobby security issues filed during coordination remain outside this
bakeoff: #22027 covers direct provider-agent sandbox bypass, and #22028 covers propagation of
approved external write roots into managed-agent SRT. This run used the built-in collaboration
agent path and did not repeat the direct-provider bypass.

## Verification checklist

The delivery boundary must keep all of the following green:

- every relative Markdown link in this directory resolves;
- every referenced report and external entry point exists;
- Graphify/gcode, CodeWiki, OpenDeepWiki, Grok Wiki, and Archify inventories reproduce their stored
  file sizes and SHA-256 values;
- Understand Anything case counts and bound graph hashes match its report;
- no credential, cookie, cache, or private response body entered the committed docs or canonical
  Grok/OpenDeepWiki inventories;
- optional viewers use direct files or loopback only;
- the Game Goblins checkout is still read-only and unchanged by the bakeoff.
