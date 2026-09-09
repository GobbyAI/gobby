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

## C0-C3 result table

| Case | Disposition/result | Principal observation |
| --- | --- | --- |
| C0 | Pass: 0.134 s, zero calls | Frozen source/binary/image and native API compatibility established; clean state contained zero repositories or prior usage. |
| C1 | Pass: 453 files, 144,421,158 bytes, 19,308.889 s, 204 calls | All 149 files indexed; 22-leaf wiki, mind map, references, diagrams, API captures, database snapshot, and native export preserved. |
| C2 | Pass: 30 files, 878,014 bytes, 10.971 s, zero calls | Identical snapshot completed as a native no-op with no regenerated documentation. |
| C3 | Fail: 30 files, 878,014 bytes, 13.124 s, zero calls | Local-directory update scanned 150 files but detected no changes, advanced state, and retained stale documentation. |

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

## Evidence inventory and reproduction

The principal completed-case records are:

- `results/opendeepwiki/C0/run-record-compatibility.json`
- `results/opendeepwiki/C1/run-record-cold-generation.json`
- `results/opendeepwiki/C2/run-record-unchanged-update.json`
- `results/opendeepwiki/C3/run-record-committed-change-update.json`
- `results/opendeepwiki/command-manifest.json`

The corresponding per-record inventory hashes are:

- C0: `232d8b4f73899d3a4cb88e3cf3aeec196fbc3f2e5a76cc30c9d1a6a87166b430`
- C1: `ac3880821f909c0833df6d0d5f9001be5a374779d4baacabb015495712985e50`
- C2: `ef57343eac326fc149027ceed99935ed7d3744076fea2d6311f80365a2600617`
- C3: `2be0f0d1551d09a50aa7675c57fb3c859d25246955c528177af1cbaf21ff9346`

The harness entry point is `results/opendeepwiki/run_opendeepwiki_cases.py`, supported by
`opendeepwiki_common.py`, `opendeepwiki_native.py`, and `lm_proxy.py`. Every hand-maintained Python
file remains below the 1,000-line ceiling and is validated from the Gobby environment with Ruff.
