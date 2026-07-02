# Wiki bake-off evidence — 2026-06 (task #734, WP4/WP5)

Lightweight evidence for the **live competitor bake-off** written up in
[`fable-repo-analysis.md` → Phase 4](../../../fable-repo-analysis.md). Seven competitor tools run
head-to-head against gobby's three wiki/code surfaces, same inputs, scored per track.

Heavy raw outputs (generated wikis, graphs, synth pages, ~46 MB) are **not** committed — they live
at `~/Projects/wiki-bakeoff/outputs/` as the **before-baseline for a re-run** after the C1–C10
adoption work lands. This dir holds only the scorecards, setup repros, and the adoption analysis.

## Tracks

- **A1 — code intelligence for agents** · gobby `gcode` ↔ **Graphify**
- **A2 — code → documentation** · gobby `gcode codewiki` ↔ **DeepWiki-Open**, **CodeWiki**, **OpenDeepWiki**
- **B — sources → knowledge vault** · gobby `gwiki` ↔ **llm-wiki**

UI/UX out of scope (deliberate later phase). Fairness: configure endpoint + key + model id only,
tool defaults otherwise; competitors needing an LLM ran on local gemma-4 (same class gobby uses).

## Files

| File | Track | Tool | Verdict (short) |
| --- | --- | --- | --- |
| `graphify-{SCORECARD,SETUP}.md` | A1 | Graphify | gcode wins overall; Graphify → C5/C6/C7/C8 |
| `deepwiki-open-{SCORECARD,SETUP}.md` | A2 | DeepWiki-Open | → C1/C2/C3; W1 (empty citation hrefs) |
| `codewiki-rust-{SCORECARD,SETUP}.md` | A2 | CodeWiki (Rust) | honest FAIL — Rust unsupported |
| `codewiki-python-{SCORECARD,SETUP}.md` | A2 | CodeWiki (Python, 65-file scope) | no complete wiki on local gemma; → C5 |
| `opendeepwiki-{SCORECARD,SETUP}.md` | A2 | OpenDeepWiki | success on Rust; → C1/C4/C9 |
| `llm-wiki-{SCORECARD,SETUP}.md` | B | llm-wiki | gap analysis → C10 |
| `ADOPTION-CANDIDATES.md` | — | all | the C1–C10 + W1 running log (source for the WP5 epic) |
| `bakeoff-STATUS.md` | — | all | per-tool run status / resume breadcrumb |

gobby's own A2 baseline output (same 65-file Python scope, the codewiki side of the head-to-head)
is at `~/Projects/wiki-bakeoff/outputs/gobby-codewiki-agents/` — not copied here (it is gobby's
own generated wiki, regenerable via `gcode codewiki`).

## Outcome → WP5

Ten deduped adoption candidates (C1–C10) became one gobby-cli epic, one leaf task per candidate.
The parity-plus verdict held against running tools: gobby is ahead on every structurally-hard
dimension (coverage, resolvable citations, search-over-own-output, incremental self-heal); the
competitor wins are additive presentation/primitive/input-domain gaps, not correctness gaps.
