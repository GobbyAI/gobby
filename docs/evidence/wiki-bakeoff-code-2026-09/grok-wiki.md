# Grok Wiki native wiki-generation evidence

- Date: 2026-09-08
- Task: Gobby #21947
- Runtime root: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`
- Corpus boundary: `/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/grok-wiki/<case>`
- Native output/state boundary: `/Users/josh/Projects/wiki-bakeoff-code-2026-09/state/grok-wiki-21947`
- Raw evidence boundary: `/Users/josh/Projects/wiki-bakeoff-code-2026-09/results/grok-wiki-21947/raw`

## Scope and outcome

This is a direct capture against the pinned Grok Wiki application. No different
release, server, agent, or export implementation was substituted. The frozen corpus
was never used as Grok Wiki state: each native invocation used the task-specific
`GROK_WIKI_ROOT` above and a distinct frozen corpus copy.

The required native local-CLI sidecar is absent from the pinned bundle. Every native
generation and Ask invocation therefore stopped before model selection, corpus
analysis, persistence, page creation, source retrieval, or export. The disposition
for every required case is **unsupported: pinned release incomplete**. This is not a
claim that the corresponding Grok Wiki feature does not exist in another release.

The authoritative task requires this leaf to stay open when its required native
baseline is incomplete. Consequently, there are no native records, pages, sources,
Markdown exports, Obsidian exports, inventories of those objects, model-effective
identity, or generation measurements to report.

## Frozen identity and execution controls

The pinned executable was invoked directly as:

```text
<bundle>/Contents/Resources/bun/bun <bundle>/Contents/Resources/server/rlm-wiki.js
```

| Item | Observed value |
| --- | --- |
| Pinned application release | `Grok-Wiki 0.0.38` (frozen installation receipt) |
| `bun` SHA-256 | `e0c90ec15d33363e6b70713d56bc3b2c7585c17f40a0fe0f8fd9305901d4e233` |
| `rlm-wiki.js` SHA-256 | `5bf0abc8612f23aab6db915db01c9e967ef9b45697ebee5f95f65588dcb5eef2` |
| Requested agent/model/reasoning | `codex` / `gpt-5.6-terra` / `medium` |
| Requested language/concurrency | `en` / `1` |
| Requested native page behavior | `--style first-30 --page-count-mode auto --pages 12` (automatic selection, ceiling 12) |
| Effective model identity | unavailable: sidecar fails before it can enumerate or select a local agent |
| Native fallback | unavailable: no fallback response or run record was emitted |

The release attempts to load
`Contents/Resources/bin/grok-wiki.ts`, but that file is absent from the pinned
application resources. The direct native error was:

```text
[local-cli-sidecar] error: Module not found ".../Contents/Resources/bin/grok-wiki.ts"
Error: Local CLI mode is unavailable: Local CLI sidecar exited before ready (1).
  Open Grok-Wiki on localhost or install and authenticate a local CLI agent.
```

The prompt's verified Codex Terra/medium profile was supplied to all generation and
Ask calls. Because the failure is before sidecar startup, it cannot verify an
effective provider/model or distinguish a provider fallback without substituting a
different release; no such substitution was made.

## C0-C9 result table

| Case | Native invocation and corpus | Result | Measurement / evidence |
| --- | --- | --- | --- |
| C0 | `agents --rescan` | unsupported | Required sidecar missing; direct preflight stderr is recorded by C7 below. |
| C1 cold | `generate corpora/grok-wiki/C1` with the controls above | unsupported | 1.2 s wall time; `raw/C1-cold.stderr`; no native output. |
| C2 unchanged | `generate corpora/grok-wiki/C2` with identical controls | unsupported | 1.6 s wall time; `raw/C2-unchanged.stderr`; no native output. |
| C3 committed change | `generate corpora/grok-wiki/C3` with identical controls | unsupported | 1.0 s wall time; `raw/C3-change.stderr`; no native output. |
| Incremental | CLI help exposes no incremental/update operation, and C1 could not create a baseline run | unsupported | No invented invalidate/rebuild substitute. |
| C7 recovery | `agents --rescan` after the failed generation attempts | unsupported | 0.9 s wall time; `raw/C7-recovery.stderr`; recovery cannot begin with no native run/queue record. |
| C8 Ask | Three native Ask calls against `corpora/grok-wiki/C8-baseline` | unsupported | Each fails before retrieval/source selection; raw evidence below. |
| C9 export | Native Markdown/Obsidian export requires a generated saved wiki | unsupported | No saved wiki was created, so no export route was available. |

The launcher's CLI usage includes only `generate`, `ask`, `agents`, `list`, `serve`,
`worker`, and `sidecar`; it contains no incremental operation. The missing sidecar
means neither `generate` nor `ask` could establish the prerequisite saved record.

## C8 native Ask evidence

All three questions were sent as direct native `ask` calls with
`--agent codex --model gpt-5.6-terra --reasoning medium --mode deep` and the frozen
`C8-baseline` corpus. Each returned the same pre-model sidecar failure, so the
retrieval mode is **neither wiki nor fresh source nor both: no answer emitted**.

| Ask case | Question | Result | Raw stderr SHA-256 |
| --- | --- | --- | --- |
| P3-A1 | What prevents an unreviewed run from mutating Lightspeed, and which explicit steps precede a live write? | unsupported before retrieval | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| P3-A2 | What is the weekly planning lane order, why does that order matter, what sheets are in the vendor workbook, and when is the workbook uploaded? | unsupported before retrieval | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| P3-A3 | Is Buylist part of the shared platform today? Describe its current outputs and publication targets, and distinguish implemented ownership from proposed integration. | unsupported before retrieval | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |

## Evidence inventory and reproduction

All raw files are retained outside the repository under the task-specific evidence
root. Stdout was empty and stderr contained the native failure shown above.

| File | SHA-256 |
| --- | --- |
| `raw/C1-cold.stderr` | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| `raw/C2-unchanged.stderr` | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| `raw/C3-change.stderr` | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| `raw/C7-recovery.stderr` | `12168872c6aa31c3420e22d15acaecc33536fababaad37a0ceee64a244ca1912` |
| `raw/P3-A1.stderr` | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| `raw/P3-A2.stderr` | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |
| `raw/P3-A3.stderr` | `afa795dafa09ecbbfb9b44a60eed08ca29763a899391f7254d12fffed470edb7` |

The task-specific state directory has no files. Thus the native record/page/source
inventory is empty by observed precondition failure, rather than omitted. Reproduce
C1 with the following direct command (changing only the frozen case path for C2/C3):

```text
GROK_WIKI_ROOT=/Users/josh/Projects/wiki-bakeoff-code-2026-09/state/grok-wiki-21947 \
  <bundle>/Contents/Resources/bun/bun <bundle>/Contents/Resources/server/rlm-wiki.js \
  generate /Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/grok-wiki/C1 \
  --agent codex --model gpt-5.6-terra --reasoning medium --pages 12 \
  --page-count-mode auto --style first-30 --language en --concurrency 1
```

No path under `results/opendeepwiki` or `state/opendeepwiki` was read or written.
