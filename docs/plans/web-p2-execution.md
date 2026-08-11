# Web Styling Phase 2 — Execution Plan

Epic: **#19148** · Plan artifact: `.gobby/plans/web-styling-consolidation-phase-2.md`
· Coverage manifest: `.gobby/plans/coverage/d45545c5-ded5-4335-b115-0245752edacf/19148/web-styling-consolidation-phase-2.coverage.yaml`

Execution mode is **fully interactive** — every leaf runs in an interactive
session the operator watches, in dependency order. No `gobby build`
automation for this epic. Providers: **Fable 5 xhigh** (Claude Code) and
**GPT-5.6-Sol xhigh** (Codex).

## Login credentials for the web-ui test

User: <josh@gobby.ai> (ignore the angle brackets)
Pass: gobby

## Assignment principle

Four adversarial review rounds (55 repaired findings) made the sweep and
retirement sections prescriptive: exact target inventories,
selector-by-selector dispositions, named test seams, and same-commit ratchet
updates. Those leaves are plan-following with objective gates — Sol's
strength. Fable takes the leaves that *create* — the capture-evidence
infrastructure every later gate rests on, the primitives designed once to
the `.impeccable.md` meta, the judgment-heavy cascade flip, and the design
contract itself — plus phase-boundary reviews and the final gate.

Design-facing leaves (1.3, 1.4, 3.1–3.4, 6.1, 7.1–7.4, 8.2) carry
`additional_skills: ["impeccable"]`; every session touching them loads the
impeccable skill and reads `.impeccable.md` before design-bearing edits.

## Per-leaf loop (both providers)

1. Claim the leaf → implement per its plan section.
2. `cd web && npm test && npm run type-check && npm run lint && npm run lint:tokens`.
3. Ratchet allowlist shrinks in the same commit; capture-matrix runs at the
   plan's named checkpoints.
4. Commit with the leaf's `covers:` label → close with linked commit.

## Task assignments

| Order | Leaf | Task | Title | Provider | Rationale |
| --- | --- | --- | --- | --- | --- |
| 1 | 1.3 | #19913 | Add the Playwright surface-capture spec | **Fable 5 xhigh** | Evidence infrastructure for every later parity gate; the finalizer contract (runner-final attestation, exact key-set equality, activation gating) took two review rounds to harden — build it right once |
| 2 | 1.1 | #19911 | Split the chat/styles.css barrel | GPT-5.6-Sol xhigh | Mechanical import-seam split, fully enumerated |
| 3 | 1.2 | #19912 | Delete dead session CSS | GPT-5.6-Sol xhigh | Census-verified dead-code deletion |
| 4 | 1.4 | #19914 | Hoist the responsive tier into the theme layer | GPT-5.6-Sol xhigh | Pure code with pinned boundary tests; the sole parity-exempt leaf, re-baselined by the 1.3 matrix |
| 5 | 2.1 | #19915 | Retire legacy Settings.tsx onto SettingsOverlay | GPT-5.6-Sol xhigh | Full control/test disposition map already written; presence-preserving normalization spec is exact |
| 6 | 3.1 | #19916 | ui/Chip primitive | **Fable 5 xhigh** | New primitive designed once to the meta (tone ladder, deutan constraints) |
| 7 | 3.2 | #19917 | ui/Card primitive | **Fable 5 xhigh** | New primitive designed to the meta |
| 8 | 3.3 | #19918 | ui/FormField primitive and fields consolidation | **Fable 5 xhigh** | Largest design surface: FormField/NativeSelect/Textarea APIs, a11y wiring, 44×44 coarse-pointer contract |
| 9 | 3.4 | #19919 | Promote TabBar into ui/ | GPT-5.6-Sol xhigh | Promotion of an existing component; SidebarPanel retirement fully specified. Fable reviews in the P3 boundary check |
| 10 | 4.1 | #19920 | Agents editors sweep | GPT-5.6-Sol xhigh | Census-driven sweep (76 raw elements, enumerated per file) |
| 11 | 4.2 | #19921 | Agents cards and portfolio sweep | GPT-5.6-Sol xhigh | Census-driven sweep incl. SidebarPanel a11y port (4.2.4 pins it) |
| 12 | 4.3 | #19922 | Pipelines sweep | GPT-5.6-Sol xhigh | Census-driven sweep |
| 13 | 4.4 | #19923 | Wiki sweep | GPT-5.6-Sol xhigh | Census-driven sweep; 4.11 deferral boundary already typed |
| 14 | 4.5 | #19924 | Graph explorers sweep | GPT-5.6-Sol xhigh | Census-driven sweep, canvas logic untouched |
| 15 | 4.6 | #19925 | FilesPage sweep | GPT-5.6-Sol xhigh | Census-driven sweep incl. FilesTab nested controls |
| 16 | 4.7 | #19926 | Tasks sweep | GPT-5.6-Sol xhigh | Census-driven sweep |
| 17 | 4.8 | #19927 | Activity lists and detail panels sweep | GPT-5.6-Sol xhigh | Census-driven long-tail sweep |
| 18 | 4.9 | #19928 | Activity chrome sweep | GPT-5.6-Sol xhigh | Census-driven sweep with named controller tests |
| 19 | 4.10 | #19929 | Chat, command-browser, and app-shell sweep | GPT-5.6-Sol xhigh | Census-driven sweep; composer moat pinned |
| — | — | — | **Fable review checkpoint: P4 batch** | **Fable 5 xhigh** | Verify all ten sweeps against the live allowlist census, ratchet state, and named tests before P5 deletions begin |
| 20 | 5.1 | #19930 | Retire message.css and empty-state.css | GPT-5.6-Sol xhigh | Wrapper-neutral MarkdownBody utility spec is exact; nine hosts enumerated |
| 21 | 5.2 | #19931 | Retire the chat input family | GPT-5.6-Sol xhigh | 1,022 lines onto twelve enumerated consumers |
| 22 | 5.3 | #19932 | Retire layout.css, variables.css, and the chat barrel | GPT-5.6-Sol xhigh | Selector-by-selector migrate/dead classification already done |
| 23 | 5.4 | #19933 | Retire sessions-tab.css and activity-panel.css | GPT-5.6-Sol xhigh | Consumer dispositions incl. McpDetailPanel split stated on both sides |
| 24 | 5.5 | #19934 | Retire the small activity tab sheets | GPT-5.6-Sol xhigh | InlineFilterPanel implementation named and test-pinned |
| 25 | 5.6 | #19935 | Retire task-execution.css and task-detail.css | GPT-5.6-Sol xhigh | Thirteen consumers enumerated with earlier-owner statements |
| — | — | — | **Fable review checkpoint: P5 batch** | **Fable 5 xhigh** | Verify sheet deletions left no orphaned consumers (gcode selector census against zero) before the cascade flip |
| 26 | 6.1 | #19936 | Remove important:true behind the screenshot gate | **Fable 5 xhigh** | Judgment-heavy: before/after capture pairs reviewed with the operator; exact-parity acceptance |
| 27 | 7.1 | #19937 | Retire segmented-control.css and dropdown-caret.css | GPT-5.6-Sol xhigh | Small enumerated retirements |
| 28 | 7.2 | #19938 | Retire app-shell.css | GPT-5.6-Sol xhigh | Enumerated retirement incl. app-header contract update |
| 29 | 7.3 | #19939 | Retire settings-overlay.css | GPT-5.6-Sol xhigh | 13 sections + shared renderers enumerated; registry-derived capture cells gate parity |
| 30 | 7.4 | #19940 | Load-order rationalization | GPT-5.6-Sol xhigh | Deterministic import-order work with guard pins |
| — | — | — | **Fable review checkpoint: P7 batch** | **Fable 5 xhigh** | Verify retirements and load order against mobileChromeCss/typography guards before the endgame |
| 31 | 8.1 | #19941 | Simplify the ratchet to pure bans | GPT-5.6-Sol xhigh | Exact-pin endgame spec: sanctioned floor = composer moat + 4.11 deferral |
| 32 | 8.2 | #19942 | Update the style guide and design contract | **Fable 5 xhigh** | `.impeccable.md` teach-mode update — the design contract is Fable-owned |

**Totals:** Fable 5 xhigh — 6 leaves (1.3, 3.1, 3.2, 3.3, 6.1, 8.2) + 3 review
checkpoints + final gate. GPT-5.6-Sol xhigh — 26 leaves.

## Dependency structure (serialized chains)

- **P1:** 1.3 → 1.1 → 1.2 → 1.4
- **P2→P3:** 2.1 → 3.1 → 3.2 → 3.3 → 3.4
- **P4:** 4.1 → 4.2 → … → 4.10 (strict chain)
- **P5:** 5.1 → 5.2 → … → 5.6 (strict chain; 5.4 also gates on 4.2/4.3/4.9)
- **P6:** 6.1 (after P5)
- **P7:** 7.1 → 7.2 → 7.3 → 7.4 (after 6.1)
- **P8:** 8.1, 8.2 (after P7)

The chains were made deterministic by review round 3 (shared-writer
serialization): every link is an exact-census ledger or shared guard-pin
writer. Do not parallelize across a chain — the ratchet allowlist and guard
tests are single-writer files.

## Provider handoff points

Provider switches happen at leaf boundaries only, with the prior leaf
closed and committed:

1. Fable finishes 1.3 → Sol runs 1.1–1.4, 2.1.
2. Sol finishes 2.1 → Fable runs 3.1–3.3.
3. Fable finishes 3.3 → Sol runs 3.4 and all of P4, P5 (Fable review
   checkpoints after P4 and P5).
4. Fable runs 6.1 → Sol runs P7 (Fable review checkpoint), then 8.1.
5. Fable runs 8.2 last — the contract update reflects the shipped end state.

## End-of-phase cross-check and final gate

After all 32 leaves close:

1. **Sol checks Fable's work** — an xhigh review session over the six
   Fable leaves (1.3, 3.1–3.3, 6.1, 8.2): acceptance items against shipped
   code, finalizer edge cases re-exercised, primitive API/test coverage
   audited. Findings filed as tasks under #19148, fixed before the gate.
2. **Fable checks Sol's work** — an xhigh review session over the 26 Sol
   leaves, anchored on the mechanical gates: live raw-element census is
   zero outside the sanctioned floor (composer moat 05198494 + 4.11
   deferral), `*_CLS` constants gone, all retired sheets deleted with the
   `CSS_TOTAL_LINES` exact pin in place, guard tests re-pointed, capture
   matrix full-pass parity at the final checkpoint.
3. **Fable is the final gate** — a last full pass: `cd web && npm test &&
   npm run type-check && npm run lint && npm run lint:tokens`, the complete
   capture-matrix run reviewed with the operator, V2 checklist walked item
   by item, then the epic-close recommendation to the operator. The
   operator closes #19148.

## Out of scope

- Wiki Ask/Research surfaces — typed deferral 4.11 → #19672.
- Design elevation (subjective polish, type-ladder collapse, signature
  moments) — successor epic per the Phase 2 plan's out-of-scope note.
- `gobby build` smoke-testing — explicitly not this epic.
