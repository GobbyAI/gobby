# OpenDeepWiki native wiki-generation evidence

- Date: 2026-09-08
- Task: Gobby #21946
- Runtime root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`
- Result root: `results/opendeepwiki`

## Scope and outcome

This report executes the OpenDeepWiki-applicable C0-C3 and C7-C9 cases from the frozen
[matrix](matrix.md) against the sealed Game Goblins baseline
`0216f1e33f05962d49467d95fe84609041c6dba8` and changed commit
`8b24ac26699aac8b24254a647aa70b208287b492`. It uses OpenDeepWiki's native local-directory
ingestion, wiki generation, update, recovery, retrieval, Ask, mind-map, and export surfaces. It
does not substitute a hosted model for the specified local generator.

The completed results establish that:

- C1 indexed all 149 source files and produced a 22-leaf English wiki, mind map, and native ZIP
  export. The serial local run made 204 successful model calls in 19,308.889 seconds, with
  4,526,243 input tokens and 301,877 output tokens.
- C2 correctly treated an identical local-directory snapshot as unchanged: it completed in
  10.971 seconds with zero model calls and no regenerated article content.
- C3 exposes a material incremental-update defect for copied local directories. OpenDeepWiki
  accepted the changed snapshot, scanned all 150 files, and advanced its stored commit identity,
  but reported zero changed files, made zero model calls, and retained the prior documentation.
  It is therefore supported but ineffective for this committed-change case.
- C7 sent one planned `SIGTERM` after the first successful generation request, restarted the same
  container and state without repair, and completed the baseline wiki in 21,054.451 recovery
  seconds. Its 198 total calls all succeeded. Recovery preserved source scope and native surfaces,
  while the model-derived catalog varied from 22 to 23 leaves.
- C8 executed every native MCP and Ask surface, but quality failed: Q01-Q14 natural-language
  searches returned zero matches, only four of eight structural searches returned results, and
  every source-file read failed because the server could not locate the repository workspace.
  The three wiki-backed Ask answers scored one pass, one fail, and one partial result.
- C9 reproduced both baseline and changed native export member trees exactly. The unchanged
  changed-state export confirms C3's stale-content defect. Raw ZIP bytes vary only through member
  timestamps; portable presentation is incomplete because 13 relative references do not resolve
  within the bundle.

## Frozen identity and execution controls

| Surface | Frozen/effective identity | Disposition |
| --- | --- | --- |
| OpenDeepWiki | Source `75840e5e86213ca40ace9d5036b1f52603f8d038`; executable SHA-256 `af7d67135527bd490d271a54d4711a74feab2921994ca9e9365d1cb6b3ff0b5c`; container image `sha256:4beef5b8919dcaa2dc924233bd069257e883cc7a061e09088a97d152d6a48510` | Native local-directory ingestion, catalog, articles, mind map, retrieval/Ask, MCP, and export are in scope. |
| Local adapter | OpenAI-compatible endpoint via LM Studio; `qwen/qwen3.8-27b`; xhigh loaded-model default | Effective provider, model, and reasoning were verified from endpoint metadata; no request reasoning field was injected. |
| Operator | `gpt-5.6-sol`; xhigh reasoning | Recorded separately from comparator model work. |

The harness binds OpenDeepWiki to case-specific corpus and state directories, disables scheduled
incremental scans, and records native API/database state without changing the frozen source. Model
traffic is serial and recorded by a standalone localhost proxy directly in front of LM Studio, so
Gobby daemon restarts do not interrupt generation. Request and response bodies remain sealed; the
evidence exposes only hashes, sizes, timing, status, model identity, and token metadata.

The matrix originally assigned a 120-minute local deadline. C1 demonstrated that the specified
27-billion-parameter model needs about 5.36 hours for this corpus. Subsequent full-generation and
recovery waits therefore use a six-hour measured deadline while preserving the prescribed model,
reasoning level, serial concurrency, and source identity.

## C0-C9 result table

| Case | Disposition/result | Principal observation |
| --- | --- | --- |
| C0 | Pass: 0.134 s, zero calls | Frozen source/binary/image and native API compatibility established; clean state contained zero repositories or prior usage. |
| C1 | Pass: 453 files, 144,421,158 bytes, 19,308.889 s, 204 calls | All 149 files indexed; 22-leaf wiki, mind map, references, diagrams, API captures, database snapshot, and native export preserved. |
| C2 | Pass: 30 files, 878,014 bytes, 10.971 s, zero calls | Identical snapshot completed as a native no-op with no regenerated documentation. |
| C3 | Fail: 30 files, 878,014 bytes, 13.124 s, zero calls | Local-directory update scanned 150 files but detected no changes, advanced state, and retained stale documentation. |
| C7 | Recovery pass: 82.219 s interruption plus 21,054.451 s recovery, 198 calls | One planned stop after two calls; same native state restarted and completed with 196 more calls. Recovered catalog has one additional model-derived leaf. |
| C8 baseline | Execution pass; quality fail: 1,144.715 s, 23 calls | Q01-Q13 searches all empty; 4/8 structural searches nonempty; source reads unavailable; Ask quality 1 pass / 1 fail / 1 partial. |
| C8 change | Execution pass; quality fail: 0.689 s, zero calls | Q14 search empty and all four Q14 source reads failed with `Repository workspace not found on server`. |
| C9 | Member determinism pass; portability/content fail: 4.547 s total, zero calls | Both states exported identical 23-member content; changed state stayed stale; 13 relative references are unresolved in the portable bundle. |

Raw records, proxy metadata, stdout/stderr, database snapshots, API captures, and inventories remain
under the corresponding result directories. Failed and interrupted setup attempts are separately
named and excluded from final measurements.

## Cold generation and incremental behavior

| Case | Seconds | Model calls | Input tokens | Output tokens | Output files/bytes | Inventory SHA-256 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| C1 | 19,308.889 | 204 | 4,526,243 | 301,877 | 453 / 144,421,158 | `ac388082...12985e50` |
| C2 | 10.971 | 0 | 0 | 0 | 30 / 878,014 | `ef57343e...2600617` |
| C3 | 13.124 | 0 | 0 | 0 | 30 / 878,014 | `2be0f0d1...1ff9346` |

C1's native catalog contains Overview and Getting Started plus Platform Core, Lightspeed Data
Synchronization, Replenishment Domain, Legacy Standalone Automations, and Operations and
Governance sections. Twenty-two leaves have generated content. The captured mind map binds those
sections to source paths, and the native export endpoint returned a 255,696-byte ZIP. All 453
inventoried artifact hashes were verified.

C2's repository task retained the same target commit identifier as C1. C3's task instead recorded
a new target identifier while still reporting no changed files and producing no model traffic.
That distinction matters: C3 is not an unsupported operation or a harness omission; the native
operation completed successfully while failing to refresh content for the changed source tree.

## C7 planned interruption and native recovery

The event-driven harness watched the model recorder and stopped the native container immediately
after its first successful `POST /v1/chat/completions`. By the interruption boundary, two calls had
completed successfully. The container then restarted against the same case state; no database or
file repair occurred.

| Stage | Seconds | Calls | Input/output tokens | Files/bytes | Inventory SHA-256 |
| --- | ---: | ---: | ---: | ---: | --- |
| Planned interruption | 82.219 | 2 | 16,295 / 768 | 5 / 223,691 | `5f1404f3...67fe24b6` |
| Native recovery | 21,054.451 | 196 | 4,256,154 / 314,400 | 31 / 963,632 | `bca8b2ca...9ee355f` |
| Combined model work | 21,136.669 | 198 | 4,272,449 / 315,168 | Not applicable | Not applicable |

All 198 recorded calls returned HTTP 200 without proxy error, and both canonical inventories
recompute exactly. Recovery indexed the same 149 files from the same baseline commit, reached a
completed repository state, and reproduced the catalog, articles, mind map, references, diagrams,
and export surfaces. Its generated catalog has 23 content leaves versus C1's 22 and different
catalog/mind-map hashes. The recovery criterion passes stable native scope; exact generative output
does not reproduce.

Three earlier C7 attempts remain separately preserved and excluded from the final result. Two were
operator relocation interruptions. The third exposed the original 120-minute recovery deadline as
too short after 65 completed calls. The clean final run uses a 900-second marker deadline and one
21,600-second recovery budget derived from C1's measured duration.

## C8 native retrieval and Ask

C8 baseline cloned C1 state and exercised all 13 common questions through native MCP
`search_doc`, all eight structural probes, repository structure, five direct source reads, and the
three exact P3 Ask prompts. C8 change cloned the C3 state and exercised Q14 plus four direct changed-
source reads. API execution completed, but retrieval quality did not:

- Q01-Q13 and Q14 each returned `matchCount: 0`.
- R03, R05, R06, and R08 returned 3, 20, 13, and 16 matches respectively; R01, R02, R04, and R07
  returned zero.
- `get_repo_structure` and all nine `read_file` probes returned
  `Repository workspace not found on server`.
- Baseline used 23 successful model calls, 874,181 input tokens, and 22,951 output tokens in
  1,144.715 seconds. Changed-state retrieval used zero model calls in 0.689 seconds.

The native chat endpoint completed all three exact prompts with HTTP 200 and no streamed error.
These answers are wiki-backed only: the response stream reports no tool calls, and native direct
source retrieval failed.

| Prompt | Quality result | Observation |
| --- | --- | --- |
| P3-A1 | Pass | Correctly identifies shadow defaults, immutable reviewed runs, scope preflight, publish lock, readback verification, and the explicit steps before live writes. |
| P3-A2 | Fail | Substitutes the transfer CLI's four selectable lanes for the gold weekly planning-stage order and cannot name the required Orders, Transfers, Sales, Demand, Bands, and Exceptions sheets. Upload ordering is correct. |
| P3-A3 | Partial | Correctly separates standalone Buylist from proposed shared-package integration and identifies CSV/Sheets/Slack publication, but omits the exact four games plus operational/Unpriced and internal/public distinctions. |

Execution-pass records are not represented as answer-quality passes. The baseline inventory has
188 files / 23,234,117 bytes with tree SHA-256
`aff523daf3f9c3598abe2ed87b3e90727bb59cd27ffa5a6424c26b4ce969578f`; the changed inventory has
65 files / 2,589,755 bytes with tree SHA-256
`d74a511e0e14f5e60467b8e8503c58453f4efa40aaaa4c0e072c0b18e3fd7136`. Both recompute exactly.

## C9 native export and presentation

OpenDeepWiki returns its native export as JSON containing base64 `fileContents`, rather than a raw
`application/zip` body. The first attempt preserved that observed response and failed before
inspection. The repaired capture helper accepts both documented raw ZIP and observed JSON/base64
shapes, records the response encoding, and writes only the decoded unmodified archive. Its boundary
checks cover raw ZIP, JSON/base64 ZIP, malformed JSON, missing content, invalid base64, and
unsupported content types.

For each source state, C9 captured an export, restarted the native service, and captured it again:

| Check | Baseline | Changed state |
| --- | ---: | ---: |
| Native execution seconds | 2.291 | 2.256 |
| Model calls | 0 | 0 |
| Archive bytes | 191,632 | 191,632 |
| Members | 23 | 23 |
| Member-tree SHA-256 | `274823fd...cf49528` | `274823fd...cf49528` |
| Member trees equal across restart | Pass | Pass |
| Raw archive bytes equal across restart | No: timestamp variance | No: timestamp variance |
| Result inventory files/bytes | 92 / 4,058,378 | 92 / 4,058,254 |
| Result inventory SHA-256 | `8df88ded...9250be36` | `15eab0cb...a4588396` |

Each native bundle contains `SKILL.md` plus 22 generated reference documents. Presentation includes
58 Mermaid blocks, 290 source-line anchors, no machine-local absolute paths, and 21 resolvable
article links from `SKILL.md`. Thirteen relative references inside generated articles do not resolve
within the portable bundle: 12 point to source/doc paths that were not exported and one points to a
bare `outlets` target. Presentation is therefore rich and navigable from the skill index but not
self-contained at every citation.

Baseline and changed-state member trees are also identical to each other. That is deterministic
evidence of C3's stale update, not evidence that the changed policy was documented.

## Evidence inventory and reproduction

The principal completed-case records are:

- `results/opendeepwiki/C0/run-record-compatibility.json`
- `results/opendeepwiki/C1/run-record-cold-generation.json`
- `results/opendeepwiki/C2/run-record-unchanged-update.json`
- `results/opendeepwiki/C3/run-record-committed-change-update.json`
- `results/opendeepwiki/C7/run-record-planned-interruption.json`
- `results/opendeepwiki/C7/run-record-native-recovery.json`
- `results/opendeepwiki/C8-baseline/run-record-native-retrieval-and-ask.json`
- `results/opendeepwiki/C8-change/run-record-changed-state-native-retrieval.json`
- `results/opendeepwiki/C9-baseline/run-record-native-export-and-presentation.json`
- `results/opendeepwiki/C9-change/run-record-native-export-and-presentation.json`
- `results/opendeepwiki/command-manifest.json`

The corresponding per-record inventory hashes are:

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

The harness entry point is `results/opendeepwiki/run_opendeepwiki_cases.py`, supported by
`opendeepwiki_common.py`, `opendeepwiki_native.py`, `lm_proxy.py`, and the focused export-response
regression in `test_opendeepwiki_native.py`. Every hand-maintained Python file remains below the
1,000-line ceiling and is validated from the Gobby environment with Ruff.
