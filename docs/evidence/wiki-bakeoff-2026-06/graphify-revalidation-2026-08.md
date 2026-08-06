# Graphify revalidation — 2026-08-06 addendum

Graphify shipped ~45 releases between the June bakeoff pin (**0.8.39**, 2026-06-14)
and this re-run (**0.9.34**, released 2026-08-05). This addendum revalidates the
bakeoff conclusions that cited Graphify (C5, C6, C7, C8) against current output.

## Procedure (fairness preserved)

Identical to `graphify-SETUP.md`: same frozen corpus
(`inputs/gobby-cli/crates`, June `git archive` of `ea6a26c`, 435 code files),
same LM Studio model (`google/gemma-4-26b-a4b-qat`), same
`~/.graphify/providers.json` endpoint-only config, no tuning.

```
uv tool install --upgrade "graphifyy[openai]"          # → 0.9.34
graphify extract inputs/gobby-cli/crates --backend lmstudio \
  --out outputs/graphify-2026-08
graphify cluster-only outputs/graphify-2026-08 --backend=lmstudio
```

One deviation: the June run passed `--cargo`. In 0.9.34 that flag hard-requires
`Cargo.toml` at the scan root and aborts the run on this layout
(`error: [Errno 2] No such file or directory: …/crates/Cargo.toml`) — 0.8.39
handled the same layout. Re-ran without `--cargo`; the August graph therefore
lacks the crate→crate dep edges, which is immaterial to the C5–C8 questions.

Outputs: `wiki-bakeoff/outputs/graphify-2026-08/` (June evidence untouched at
`outputs/graphify/`). Logs: `outputs/graphify-2026-08-extract2.log`,
`outputs/graphify-2026-08-cluster.log`.

## Graph delta

| | June 0.8.39 | Aug 0.9.34 |
| --- | --- | --- |
| Nodes | 10,842 | 9,628 (−11%) |
| Edges | 25,859 | 26,567 (+3%) |
| Communities | 404 (393 shown) | 345 (330 shown) |
| Confidence | 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS, avg 0.8 | identical |

Fewer nodes with more edges matches the release notes: exact dedup,
excluded-file pruning, false-`indirect_call` elimination, cross-file edge
preservation. Extraction hygiene improved: zero-node files are reported instead
of silently cached; cross-file node-name collisions (`gobby` minted by two crate
READMEs) now warn with a `merge-graphs` workaround instead of silently dropping
a node.

## Per-candidate verdicts

- **C5 (semantic concept clustering) — STANDS, reinforced.** Community labeling
  failed again on the fair local-model config:
  `[graphify label] warning: community labeling failed (label response is not
  parseable JSON: ''); using Community N placeholders.` The report fallback
  improved — community hubs now display hub-node names (`walker.rs`,
  `SearchScope`, `exports.rs`) instead of `Community 0` — but those are
  file/symbol names, exactly what C5 scored as inadequate versus semantic names
  like `monitoring_and_detection`, and 345 communities remains ~25× too many
  for navigation. Two months of releases did not crack LLM-fragile naming;
  the #19664 design (deterministic membership, 8–15 clusters, daemon-routed
  naming with schema validation, cached names) remains differentiated, and
  Graphify's failure mode is the one daemon-lane schema validation prevents.
- **C6 (two-endpoint path) — improved on their side.** Directed-by-default with
  an honest `No directed path found … Re-run with --undirected` refusal,
  per-hop stored relations with `[EXTRACTED]` tags and direction arrows, and an
  ambiguous-target warning on tie scores. `gcode path` already shipped; their
  per-hop relation + confidence rendering is worth borrowing for gcode output
  formatting.
- **C7 (edge confidence) — unchanged.** Identical tagging and ratios.
- **C8 (token-budget retrieval) — improved ergonomics.** Truncation banner now
  counts cut nodes (`showing 41 of 356 nodes (~1200-token budget)`) and names
  both widening and narrowing moves; each node row carries `src/loc/community`.
  `gcode --token-budget` already adopted the pattern.

## New surfaces since June

- `god-nodes` command (top hubs by degree: `WikiError` 489 edges, `Client` 232,
  `Context` 214, `Symbol` 128 on this corpus) — independent convergence on the
  insight-report god-nodes section in the #19664 output design; Graphify prints
  bare counts, the Gobby design cites and wikilinks.
- Q&A memory loop (`save-result`, `reflect` → `LESSONS.md`) and cross-repo
  `merge-graphs` — adjacent to existing Gobby subsystems (memory, multi-project
  index), not wiki output.
- Claude Code PreToolUse hook that blocks raw file reads until a graph query
  runs — aggressive agent-axis integration.
- `graph.html` now suppressed above 5,000 nodes (aggregated view retained per
  release notes; this corpus exceeds the cap).

## Conclusion

No 0.9.34 change invalidates a bakeoff adoption decision or exposes a new gap
in the wiki output design. The one candidate the design bets against (C5
semantic naming as competitors implement it) regressed-in-place: still unusable
on a fair local config. C6/C8 polish converges on patterns gcode already
adopted, and `god-nodes` independently validates the insights-page design.
