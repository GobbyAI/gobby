# Grok Wiki native wiki-generation evidence

- Date: 2026-09-10
- Task: Gobby #21947
- Runtime root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`
- Corpus boundary: `/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/grok-wiki/<case>`
- Native state boundary: `/Users/josh/Projects/wiki-bakeoff-code-2026-09/state/grok-wiki-21947`
- Raw evidence boundary: `/Users/josh/Projects/wiki-bakeoff-code-2026-09/results/grok-wiki-21947`

## Scope and outcome

The required baseline, unchanged rerun, committed-change run, planned interruption, recovery
probe, three native Ask questions, saved records, Obsidian/Markdown ZIPs, and print-HTML exports
were captured against the pinned **Grok-Wiki 0.0.38** application. C1, C2, C3, native Ask, and
native export completed. Incremental update and interrupted-run resume are demonstrated unsupported
by the frozen native command inventory; no substitute operation is credited.

The earlier report classified the release as incomplete because the packaged launcher points to
`Contents/Resources/bin/grok-wiki.ts`, which is absent. The user authorized a provenance-preserving
repair or authoritative replacement. The official 0.0.38 app archive was downloaded from the
[upstream release](https://github.com/AsyncFuncAI/grok-wiki/releases/tag/0.0.38), and both it and the
original DMG have the upstream-published SHA-256 values. Both omit the same TypeScript path. No
bundle byte was changed: `GROK_WIKI_SERVER_ENTRY` was set to the same bundled `rlm-wiki.js`, whose
compiled CLI contains the native `sidecar` implementation and explicitly honors that variable.
This is a launcher-path repair around a packaging defect, not a replacement runtime or model
adapter.

The signed extracted app passes `codesign --verify --deep --strict`, satisfies its designated
requirement, and identifies Developer ID team `9RSXBGZ65V` (ASYNCFUNC LLC). The official app archive,
signature asset, release API snapshot, original failure evidence, repaired attempts, and exact
identity hashes remain in the external workspace.

## Frozen identity and controls

| Item | Observed value |
| --- | --- |
| Release / publication | `Grok-Wiki 0.0.38`; 2026-08-18T01:37:34Z |
| Official app archive SHA-256 | `d2f50dcf9a6160234a8cbb00020b59fd9ccc8b008d8dd972f62c42efb4c905f8` |
| Official archive signature SHA-256 | `82ed9bb01af8f016dc750282fc92d60412921af765bc51255cc0c6c04692456a` |
| Original / official DMG SHA-256 | `bbcccef258102a1f32e36947b1a6da059a828bf1cea552dfdb58ca35ab562e09` |
| Bundled `bun` SHA-256 | `e0c90ec15d33363e6b70713d56bc3b2c7585c17f40a0fe0f8fd9305901d4e233` |
| Bundled `rlm-wiki.js` SHA-256 | `5bf0abc8612f23aab6db915db01c9e967ef9b45697ebee5f95f65588dcb5eef2` |
| Code-signing identity / CDHash | `ai.grokwiki.desktop`; `2f548098401c2cead4babcb05e2f9f1f1038870e` |
| Requested and effective generator | Codex / `gpt-5.6-terra` / `medium` |
| Operator | `gpt-5.6-sol` / `xhigh` |
| Language / native concurrency | `en` / `1` |
| Native page behavior | `first-30`; automatic selection; ceiling `12` |
| Hosted deadline / scheduling | 3,600 seconds; at most two independent runs active |
| Embedding configuration | not applicable: Grok Wiki used source-backed local CLI generation |

The capture wrapper records every native child argv, prompt, event stream, and aggregate token
usage. Across 42 recorded `codex exec` starts, every invocation requested exact
`gpt-5.6-terra` and `model_reasoning_effort="medium"`; generated pages used `workspace-write`, and
Ask used `read-only`. There was no `model-fallback` or default-model retry event. The first C1
attempt reached one Codex invocation but returned no usage because its isolated copied login
snapshot had been revoked. The snapshot was refreshed from the already authenticated operator
installation for the one diagnosed retry.

All successful model-bearing cases together used 7,183,620 input tokens, including 5,524,224
cached input tokens, and 135,194 output tokens, including 7,354 reasoning tokens. Provider quota
before/after/delta remains `unknown`; token counts are not misrepresented as subscription quota.

## C0-C9 result matrix

| Case | Native result | Wall time | Model work | Output / disposition |
| --- | --- | ---: | ---: | --- |
| C0 | `agents --rescan` and `--help` passed | 0.415 s / 0.172 s | 0 calls | Codex ready; frozen command inventory captured |
| C1 cold | attempt 1 blocked on revoked isolated login; diagnosed attempt 2 passed | 3.963 s / 810.168 s | 1 failed start; then 10 calls | 9/9 pages; 30 source files; 5 state files / 2,980,322 bytes |
| C2 unchanged | passed as another full native generation | 648.589 s | 9 calls | New wiki ID, 8/8 pages; 10 state files / 5,332,505 bytes |
| C3 committed change | passed as a fresh full generation | 603.420 s | 8 calls | 7/7 pages; 42 source files; 5 state files / 2,475,973 bytes |
| C4-C6 | not applicable | 0 s | 0 calls | Index-only fixtures outside the Grok Wiki P3 profile |
| C7 interruption | planned SIGTERM after first completed call passed | 70.311 s | 2 calls started | No wiki; partial structure persisted; no forced kill |
| C7 recovery | a second `generate` completed but did not resume | 631.700 s | 9 fresh calls | Demonstrated unsupported as recovery; original run remains `running` |
| C8 Ask | three exact native Ask calls passed execution | 45.948 / 46.469 / 43.694 s | 1 call each | Fresh-source mode; answer quality pass / fail / pass |
| C9 baseline export | native ZIP twice + print HTML | 0.278 s | 0 calls | 13 ZIP members; 43 evidence files / 646,223 bytes |
| C9 changed export | native ZIP twice + print HTML | 0.261 s | 0 calls | 11 ZIP members; 39 evidence files / 515,051 bytes |

Execution success is not conflated with semantic quality or capability support. In particular, C7's
second process exited zero but is a failed recovery result, and C8 P3-A2 exited zero but does not
answer the required planning-stage-order portion correctly.

## Saved native records and unchanged/change behavior

| Case | Saved wiki ID | Pages | Distinct source files | Content SHA-256 |
| --- | --- | ---: | ---: | --- |
| C1 | `wiki-local-c1-b975abdd206f` | 9 | 30 | `0fa2b9367f79c75fb4358abc7c735c003cb592061b17cceb79d2922d40dda61d` |
| C2 newest | `wiki-local-c1-d087d99b45d2` | 8 | 42 | `d5a0027d6a4eae5d641df869f142a0efd99e323202aef57a151803aed7eac040` |
| C3 | `wiki-local-c3-d7e5af8f4664` | 7 | 42 | `8f690b604b1e7cdf4a0807b8c9bfbe0cae9b8244727852c6d83f3f91546cf5cd` |
| C7 second run | `wiki-local-c7-fac573eb7499` | 8 | 47 | `61ac6ddc39fcaa2ccd81d3b6aeec2318a99ddd2866d7abdbfbbe9fc5f9565999` |

Every record says `Codex CLI · gpt-5.6-terra`, `first-30`, `auto`, page ceiling 12, and English. The
C1 and C2 corpus manifests are byte-identical (`91a0a531...eb30090e`), but C2 performed nine fresh
model calls, created a second identity beside the first, selected eight pages instead of nine, and
changed page content. Therefore unchanged rerun is supported only as full regeneration; it provides
no incremental efficiency, stable identity, or deterministic-content guarantee.

C3 has source commit `8b24ac26699aac8b24254a647aa70b208287b492`, input tree
`f3a6e0e3b90fd03bdde868aa3209e67cdab1ced3564fa60d4e664a29bb108a94`, 150 files, and the expected
eight modified files plus one added test. Its wiki generally notes that automatic settings may use
category or name-prefix minimums, but it omits the actual committed policy values (`Hobby Supplies`
minimum 2 and `Sleeves: ` minimum 4), case-sensitive longest-prefix precedence, and the exact
changed-file set. C3 generation passes; C3 change awareness fails the common Q14 content bar.

## Incremental and recovery support

Frozen CLI help exposes only `generate`, `ask`, `agents`, `list`, `serve`, `worker`, and `sidecar`.
There is no native update, incremental, resume, or recovery operation. The C2 full rerun is not
renamed as incremental support.

For C7, the task harness sent SIGTERM to the owned process group immediately after one Codex call
completed; a second page call had begun, and no SIGKILL was needed. Native state retained a
`wiki_generate` run with structure and a page-start event but no saved wiki. Running the only
available operation, `generate`, against the same state created a different run, repeated a full
structure/page sequence with nine calls, and left the interrupted run marked `running`. This is
direct demonstrated-unsupported evidence for interrupted-run recovery, not a claim that the later
full rebuild failed.

The first interruption-controller attempt is also preserved separately: the controller encountered
an incomplete live JSONL tail, was repaired to ignore an unterminated tail, and the owned native
process group was terminated before the clean planned-interruption rerun. The focused regression
test covers this live-write race.

## C8 native Ask: fresh source, not saved wiki

Each exact prompt was passed to native `ask` with `--agent codex --model gpt-5.6-terra
--reasoning medium --mode deep`. A C1 wiki copy was present in the isolated C8 state, but the CLI
record references the frozen C8 source path, and all three trajectories use direct native Search and
Read tools. No wiki record is selected as context. The observed retrieval mode is therefore
**fresh source only**, not wiki or both.

| Ask | Answer quality | Observation |
| --- | --- | --- |
| P3-A1 approval boundary | Pass | Correctly identifies shadow default, explicit publish/baseline behavior, scope preflight, source-version/review steps, and the important nuance that documented human approval is not an approval token enforced by code. |
| P3-A2 planning/workbook | Fail | Correctly names Orders, Transfers, Sales, Demand, Bands, and Exceptions and the post-transfer-verification upload gate, but substitutes the four transfer routes for the required planning-stage order. |
| P3-A3 Buylist ownership | Pass | Correctly keeps Buylist standalone; names operational and Unpriced CSVs for Magic, Pokémon, One Piece, and Riftbound; distinguishes internal/public Sheets and aggregate Slack reporting; and separates current shared database support from proposed package ownership. |

Ask usage was respectively 287,411 / 324,175 / 206,608 input tokens (231,680 / 275,456 / 165,376
cached) and 1,861 / 1,603 / 1,798 output tokens. Exact post-redaction prompts and SHA-256 values are
stored in each run record.

## C9 native export and presentation

The server's authenticated API stores records under a per-user root, while the CLI writes at the
direct `GROK_WIKI_ROOT`. The first HTTP attempt therefore returned 404 for the CLI-root wiki and is
preserved. The canonical capture starts an isolated auth-off native server, establishes its
in-memory anonymous session, copies the unmodified saved native wiki/product records into that
server's isolated per-user state, and then calls the native wiki, ZIP, and print-HTML routes. No
cookie or credential artifact is persisted.

| Check | Baseline | Changed state |
| --- | ---: | ---: |
| Export workflow wall time | 0.278 s | 0.261 s |
| ZIP bytes | 103,341 | 82,511 |
| ZIP members | 13 | 11 |
| Normalized member-tree SHA-256 | `a4a446895af79c4f82863c5f1f9e7a179b53e80c07a3b52a7148f993ffaa8d71` | `0863b97454a0a1daa368db12b760e6b35586b8d398bc799dae0bafc702c1c05a` |
| Page Markdown files | 9 | 7 |
| All Markdown files / bytes | 11 / 95,005 | 9 / 75,058 |
| Print HTML bytes | 138,364 | 111,663 |
| Mermaid blocks | 1 | 1 |
| Source-line anchors | 198 | 109 |
| Obsidian wiki links resolved | 44/44 | 53/53 |
| Machine-local absolute paths | 0 | 0 |
| Result inventory SHA-256 | `08e075b9a489f151080fde27abfb585cb2d0087b59b01b708b4c3fd0f0a8b491` | `11acf13ea6b845bd5f6d9bf0bf12f2d33807cfdb3910e0c739d62db5ff94ae44` |

All health, wiki, list, ZIP, and print requests returned HTTP 200. Each native bundle contains
`README.md`, `sources.md`, `manifest.json`, `.obsidian/app.json`, and one Markdown file per page.
Repeated raw ZIP bytes differ because `manifest.json.exportedAt` changes; every other member is
equal, and the member tree is identical after normalizing only that timestamp.

The export is portable as an Obsidian vault and its index links resolve, but source-line links are
rendered with empty hrefs: 198 baseline and 109 changed-state citations look like
`[path:line-span]()`. Source paths and spans survive as text, but they are not clickable or
self-contained links. The native print HTML has one article per generated page and no machine-local
absolute path.

## Evidence inventory and reproduction

The canonical evidence entry points are:

- `results/grok-wiki-21947/command-manifest.json`
- `results/grok-wiki-21947/run-records.jsonl`
- `results/grok-wiki-21947/evidence-inventory.json`
- `results/grok-wiki-21947/C1/cold/attempt-2/run-record.json`
- `results/grok-wiki-21947/C2/unchanged/attempt-1/run-record.json`
- `results/grok-wiki-21947/C3/changed/attempt-1/run-record.json`
- `results/grok-wiki-21947/C7/interrupted/attempt-2/run-record.json`
- `results/grok-wiki-21947/C7/recovery/attempt-1/run-record.json`
- `results/grok-wiki-21947/C8-baseline/ask-P3-A*/attempt-1/run-record.json`
- `results/grok-wiki-21947/C9-{baseline,change}/native-canonical/`
- `state/grok-wiki-21947/{C1,C2,C3,C7,C8-baseline}/`

The sealed result inventory contains 383 files / 14,959,195 bytes with inventory SHA-256
`ff493c929de80ae32be1b695c19a44e5e75b5765d70614c1ccb3e0e164d81161` (the inventory excludes itself).
`command-manifest.json` hashes to
`5d826c321677aeb8cbcf6f15a20ebfdca8ce4a003a918e92dd9db1ec5df9d6ed` at the validation boundary.
The harness, export capture, export audit, focused tests, and evidence validator are preserved under
the same result root.

The repaired native invocation is:

```text
GROK_WIKI_ROOT=<isolated-case-state> \
GROK_WIKI_SERVER_ENTRY=<bundle>/Contents/Resources/server/rlm-wiki.js \
CODEX_HOME=<temporary-isolated-authenticated-home> \
<bundle>/Contents/Resources/bun/bun \
  <bundle>/Contents/Resources/server/rlm-wiki.js \
  generate <frozen-case-corpus> \
  --agent codex --model gpt-5.6-terra --reasoning medium \
  --pages 12 --page-count-mode auto --style first-30 --language en --concurrency 1
```

After the last model call, the task's isolated credential home—including `auth.json` and transient
Codex databases/caches—was permanently removed and verified absent. Native export cookies were
kept in memory or deleted after use. No credential value, credential hash, or sensitive request or
response body appears in the report or canonical inventory.
