# Web Styling Consolidation — Phase 2

**Plan ID:** web-styling-consolidation-phase-2

## Overview

`kind: framing`

Phase 1 (#19128–#19146) consolidated primitives in `web/src/components/ui/`, shipped the cva `Button` at `.btn` parity, migrated ~106 call sites, deleted `buttons.css`, and installed the style-debt ratchet. Phase 2 takes the remaining debt to its end state: **all styling in Tailwind utilities + cva recipes + `ui/` primitives, with only token-infrastructure stylesheets surviving** (`index.css`, `tokens.css`, `tailwind-theme.css`, `base.css`, `markdown.css`, `accessibility.css`). Hook sheets (`app-shell.css`, `segmented-control.css`, `dropdown-caret.css`) and `settings-overlay.css` retire too, sequenced last. `important: true` leaves `tailwind.config.ts` behind a fixed Playwright screenshot gate. The ratchet ends as a pure ban plus a pinned sanctioned-exception floor.

Current debt (live inventory 2026-08-08, re-read `web/src/__tests__/styleRatchet.allowlist.ts` at execution time — attrition shrinks it): 358 raw interactive elements (210 button / 93 input / 37 select / 18 textarea) across 164 file-entries; 332 `*_CLS` constants across 12 files; 35 recorded stylesheets, 7,251 scanner-counted CSS lines under ceiling 7,274; 6 `!important` in 3 files.

Design elevation (type-ladder collapse to ~5–6 steps at ≥1.25×, per-surface impeccable audit/polish, signature moments) is **out of scope** — it runs as a follow-up epic on the consolidated primitives this plan produces.

## Constraints

`kind: framing`

requirement-source: docs/guides/frontend-style-guide.md

- **Ratchet discipline.** Every migration commit edits `styleRatchet.allowlist.ts` in the same commit (stale entries fail). Entries only shrink. New `.css` files are banned. When a batch deletes >200 CSS lines, lower `CSS_TOTAL_LINE_CEILING` in the same commit (tighten-slack floor = ceiling − 200).
- **Allowlist parsing is textual** (`parseAllowlistSnapshot`): keep single-quoted keys and `export const NAME` markers; do not reformat the file.
- **CI-only gate:** `STYLE_RATCHET_TARGET_REF` makes the target-branch check bite only in CI (`.github/workflows/ci.yml:225`). Green locally does not prove green in CI.
- **Guard tests are updated deliberately, never loosened**: `mobileChromeCss.test.ts` (most brittle — pins selectors in named sheets + `main.tsx` import relation), `coarsePointerTouchTargets.test.ts` (44px harness; Button size×dense ladder `{sm:28, md:32, lg:40, icon:32}` must survive unchanged), `typographyLadder.test.ts`, `cssTokenIntegrity.test.ts` (token-shaped, safest), `inputFocusAdoption.test.ts` (entries deleted as files migrate), `planApprovalDesign.test.tsx`, `ActivityPanelEmpty.test.tsx`, `ActivityRowStatusDot.test.tsx`. `settingsSliderFocus.test.ts` and `coarsePointerTouchTargets` do `readFileSync` at module scope — deleting their source sheets without updating the test throws at import time.
- **Visual parity is the bar** for every `refactor` deliverable: rendered output identical before/after (the Playwright matrix from 1.3 is the evidence tool). **One deliverable is exempt: 1.4**, which intentionally changes responsive behavior to bring the code into conformance with the settled `.impeccable.md` tier contract. Its post-change output becomes the parity baseline for every deliverable after it; no other deliverable may change rendered output.
- **Validation per step:** `npm run test` (vitest), `npm run type-check`, `npm run lint` (eslint + stylelint), `npm run lint:tokens` — all from `web/`. Small, bisectable commits.
- **After any `npm uninstall` in `web/`, run `npm install`** (postinstall reapplies `patch-jsx-a11y-minimatch.mjs`; #19146 finding).
- **Canonical component standards** live in `.impeccable.md` (Canonical Components); it is edited only via the impeccable skill's teach mode. Sanctioned *composite semantics* are preserved — the FilesTab composite tree rows and the ToolCallCard expandable header keep their div-plus-keyboard-guard structure — but composite containers do not exempt the native controls nested inside them: those migrate to ui primitives like any other. The raw-element endgame floor is exactly the chat composer icon buttons (moat 05198494) plus the typed 4.11 Wiki deferral.
- Non-test `.ts`/`.tsx`/`.css` files stay under 1,000 lines; cohesive decomposition, not line-count gaming.
- Corrections to the epic description discovered in exploration and honored here: the `input-responsive.css` `!important` hatch no longer exists (stale comment only); the `chat/styles.css:31` `.tool-code-surface` `!important` overrides an *inline style* from react-syntax-highlighter and must **survive** the `important: true` flip (it relocates to an infra sheet when `chat/styles.css` retires); `styles/settings.css` was not unreferenced — the legacy `Settings.tsx` surface is live and is retired by 2.1.

## P1: Foundations

`kind: framing`

**Goal**: Structural prerequisites and early wins — CSS ownership made explicit, dead CSS deleted, the responsive tier hoisted to one token, the screenshot harness in place. After 1.3, the phase runs serialized (1.1 → 1.2 → 1.4): all three rewrite the same mobile-chrome guard pins (and 1.2 the ratchet ledger), and exact-census pin files admit no concurrent writers.

### 1.1 Split the chat/styles.css barrel along the chat/activity seam [category: refactor] (depends: 1.3)

`kind: deliverable`

Targets:
- `web/src/components/activity/ActivityMcpTab.tsx::*` — scope-reason: gains the mcp-tab.css side-effect import as its owner
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: gains the activity-panel.css side-effect import as its owner
- `web/src/components/activity/CronTab.tsx::*` — scope-reason: gains the cron-tab.css side-effect import as its owner
- `web/src/components/activity/FilesTab.tsx::*` — scope-reason: gains the files-tab.css side-effect import as its owner
- `web/src/components/activity/PipelinesTab.tsx::*` — scope-reason: gains the pipelines-tab.css side-effect import as its owner
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: gains the rules-tab.css side-effect import as its owner
- `web/src/components/activity/SessionsTab.tsx::*` — scope-reason: gains the sessions-tab.css side-effect import as its owner
- `web/src/components/activity/TracesTab.tsx::*` — scope-reason: gains the traces-tab.css side-effect import as its owner
- `web/src/components/activity/ActivityPanelEmpty.tsx::*` — scope-reason: gains its own empty-state.css side-effect import for standalone style ownership
- `web/src/components/activity/SkillsTab.tsx::*` — scope-reason: gains its own rules-tab.css side-effect import for the filter-panel rules it consumes
- `web/src/components/activity/integrations/IntegrationsFilterPanel.tsx::*` — scope-reason: gains its own rules-tab.css side-effect import for the filter-panel rules it consumes
- `web/src/components/chat/styles.css`
- `web/src/components/chat/styles/input-responsive.css`
- `web/src/components/chat/styles/activity-panel.css`
- `web/src/components/chat/styles/cron-tab.css`
- `web/src/components/chat/styles/files-tab.css`
- `web/src/components/chat/styles/mcp-tab.css`
- `web/src/components/chat/styles/pipelines-tab.css`
- `web/src/components/chat/styles/rules-tab.css`
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/chat/styles/traces-tab.css`
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: gains standalone style-ownership pins for the multi-owner sheet imports

`ChatPage.tsx:1` imports `chat/styles.css`, whose 13-sheet `@import` chain loads 8 sheets that style `components/activity/` surfaces (48% of the barrel's lines): `activity-panel.css`, `sessions-tab.css`, `mcp-tab.css`, `rules-tab.css`, `files-tab.css`, `cron-tab.css`, `traces-tab.css`, `pipelines-tab.css`. Any activity surface rendered without `ChatPage` mounted is unstyled, and per-sheet retirement is impossible to bisect.

- Move each activity sheet's import to its owning component (side-effect import at the consumer, the pattern the task sheets already use): `activity-panel.css` → `ActivityPanel.tsx`; `sessions-tab.css` → `SessionsTab.tsx`; `mcp-tab.css` → `ActivityMcpTab.tsx`; `rules-tab.css` → `RulesTab.tsx`; `files-tab.css` → `FilesTab.tsx`; `cron-tab.css` → `CronTab.tsx`; `traces-tab.css` → `TracesTab.tsx`; `pipelines-tab.css` → `PipelinesTab.tsx`.
- `chat/styles.css` keeps: `variables.css`, `layout.css`, `message.css`, `input.css`, `empty-state.css` imports plus the `.tool-code-surface` rule. `empty-state.css` serves both chat and activity empty states, so it gets **two explicit owners**: it stays in the chat barrel and `ActivityPanelEmpty.tsx` gains its own side-effect import — a lazy or standalone activity mount renders styled without `ChatPage`. Duplicate side-effect imports of one sheet are deduped by the bundler; both owners are removed together in 5.1.
- `.activity-filter-panel` rules currently in `rules-tab.css` are consumed by `SkillsTab.tsx:338` and `IntegrationsFilterPanel.tsx:18` too — both consumers gain their own `rules-tab.css` side-effect import so Skills and Integrations render styled without the Rules tab mounted. All three owner imports are removed together in 5.5.
- Delete the stale `!important` comment paragraph in `web/src/components/chat/styles/input-responsive.css` (~lines 100–102) — the hatch it documents was already removed.
- No selector, rule, or emitted-bundle change beyond import relocation; visual parity exact.

**Acceptance:**

- 1.1.1 - The 8 activity sheets are imported by their owning activity components and removed from the chat barrel. file: `web/src/components/chat/styles.css`.
- 1.1.2 - Activity surfaces render styled without `ChatPage` mounted. behavior: "activity sheets load with their owning components" in `web/src/components/activity/ActivityPanel.tsx`.
- 1.1.3 - The stale hatch comment is gone. file: `web/src/components/chat/styles/input-responsive.css`.
- 1.1.4 - ActivityPanelEmpty, SkillsTab, and IntegrationsFilterPanel each carry their own sheet import and render styled standalone (without ChatPage or the Rules tab mounted), with import-relation pins updated. test: `web/src/__tests__/mobileChromeCss.test.ts`.

### 1.2 Delete dead session CSS [category: refactor] (depends: 1.1, 1.3)

`kind: deliverable`

Targets:
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/shared/executions/execution-utils.tsx::*` — scope-reason: workflow-trace icon utilities and the execution card/badge/button styling both migrate onto ui primitives
- `web/src/styles/index.css`
- `web/src/styles/session-primitives.css`

`session-primitives.css` (229 lines, imported from `web/src/styles/index.css`) has exactly one live consumer: `.workflow-trace-icon` (line 226), used at `web/src/components/shared/executions/execution-utils.tsx:310`. The other ~215 lines (`.session-item*`, `.session-dot*`, `.session-name*`, `.session-badge`, `.session-kill-btn`, `.session-delete-btn`, `.terminals-*`, `.session-group*`) have zero component consumers anywhere in `web/src`. `.session-kill-btn` is also defined in `sessions-tab.css:236-253` — both definitions are dead.

- Convert the `execution-utils.tsx:310` call site to Tailwind utilities equivalent to the `.workflow-trace-icon` rule; delete `session-primitives.css` entirely; remove its `@import` from `web/src/styles/index.css`.
- Delete the dead `.session-kill-btn` block from `web/src/components/chat/styles/sessions-tab.css`.
- Update `web/src/__tests__/mobileChromeCss.test.ts`: remove the `session-primitives.css` assertions (`.session-delete-btn` pins at line ~506 — the class is dead).
- Ratchet: drop the `CSS_FILE_ALLOWLIST` entry for `src/styles/session-primitives.css`; this batch deletes >200 lines, so lower `CSS_TOTAL_LINE_CEILING` accordingly in the same commit.

**Acceptance:**

- 1.2.1 - `session-primitives.css` is deleted and its index import removed. file: `web/src/styles/index.css`.
- 1.2.2 - The workflow trace icon renders via utilities. file: `web/src/components/shared/executions/execution-utils.tsx`.
- 1.2.3 - Both dead `.session-kill-btn` definitions are gone. file: `web/src/components/chat/styles/sessions-tab.css`.
- 1.2.4 - Allowlist entry dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

### 1.3 Add the Playwright surface-capture spec [category: test]

`kind: deliverable`

Targets:
- `web/tests/style-surfaces.spec.ts`
- `web/tests/support/captureRunFinalizer.ts`
- `web/tests/support/captureRunFinalizer.spec.ts`
- `web/playwright.config.ts::*` — scope-reason: capture project/device descriptors, opt-in spec tagging, and the globalTeardown finalizer registration land in the shared config

A checked-in capture harness producing the fixed screenshot matrix used as before/after evidence for every risky step (especially 6.1). Chrome DevTools stays the ad-hoc debugging tool; this spec is the repeatable gate.

- **Surface-scenario manifest, not a surface list.** Every entry declares its route, its seeded state, and one visible checkpoint the run asserts before capturing. A tab that renders empty or still-loading produces a stable screenshot while showing none of the controls a migration touches — the checkpoint is what makes the capture meaningful. The manifest is built from the **live** `ACTIVITY_PANEL_TABS` registry (`ActivityPanelTabs.tsx`), currently 16 tabs: sessions, terminal, tasks, mcp, agents, stages, skills, memory, integrations, wiki, rules, plans, changes, files, pipelines, cron. Full entry list (24 base scenarios): chat; composer; the 16 tabs; agents tab with the editor panel open (heaviest sweep, 4.1); memory tab in graph view (KnowledgeGraph, 4.5); `FilesPage` (4.6); settings overlay; login; mobile toolbar state. The spec asserts its scenario count against the manifest so a registry change fails loudly. **The settings-overlay scenario expands into one cell per live `SETTINGS_SECTIONS` registry entry** (13 sections today, derived from the registry the same way the tab scenarios derive from `ACTIVITY_PANEL_TABS`), each with deterministic seed data for its specialty editors — variables, endpoint, hubs, prompt-row — so the overlay renders every section the 7.3 migration touches, and every section cell participates in finalizer completeness.
- **Explicit representative mappings** for surfaces the matrix cannot photograph, each with a recorded equivalence rationale in the spec: Traces is deliberately hidden from the tab strip (moat 66e919e3, #19152) — `TracesTab` migration (4.8) is covered by its component tests; `CodeGraphExplorer` and `AgentPortfolioPage` have zero production mounts (test-only imports) — their sweeps (4.5, 4.2) are covered by component tests and the KnowledgeGraph capture as the graph-chrome representative. Wiki Ask mode is excluded (4.11 deferral).
- **Determinism per entry:** fixed seed data via the existing API/WebSocket mock patterns in `web/tests/`, frozen clock, `document.fonts.ready` awaited, and an asserted `matchMedia` state for the pointer axis so a mis-emulated descriptor fails loudly instead of capturing the wrong tier.
- **State coverage where the surface owns it:** entries for focused/open (dropdown, dialog, filter panel) and long-content/overflow states on the surfaces that own those affordances, so migrations to those code paths are actually photographed.
- Matrix: dark and light theme × fine and coarse pointer (`hasTouch` + touch descriptor) × reference viewports 1440×900 (desktop), 440×956 (portrait), 932×430 (landscape — exercises the height≤500px mobile-tier clause, which only becomes a real tier after 1.4).
- **Grayscale subset:** state-bearing rows (task/session/pipeline status, error and success surfaces) captured desaturated in both themes — the repeatable form of the deutan contract check, scoped to a subset rather than doubling the full matrix.
- **Reduced-motion subset:** the animation families (voice recording, speaking/listening, loading, streaming) captured under `prefers-reduced-motion: reduce` plus a no-preference control, with computed-style assertions that the suppression actually holds — the executable form of the reduced-motion contract exercised at 5.2 and 6.1.
- **Immutable, pairable, recoverable runs:** each run is named by label (git SHA + `before`/`after`) under a gitignored root. Every matrix cell — and every retry attempt — writes into its own attempt-scoped staging directory and emits an immutable per-cell manifest fragment (scenario, git SHA, plan section under test, SHA-256 per PNG); a single deterministic finalizer assembles the staged cells into the labeled run directory with a merged run-manifest JSON in one atomic rename. Overwrite refusal applies to **finalized** runs only — a failed or partial attempt never occupies the label, so CI retries and Playwright's parallel workers cannot collide on the label or corrupt the manifest, while a finalized baseline can never be silently replaced. Stable file names inside a run (`<surface>--<state>--<theme>--<pointer>--<viewport>.png`) pair across runs by name; no committed baselines and no pixel-diff gate (per decision — human review of pairs).
- **Exactly-once finalization under `fullyParallel` + retries:** the finalizer is a run-level coordinator module (`web/tests/support/captureRunFinalizer.ts`) registered as Playwright `globalTeardown` in `playwright.config.ts` — never spec- or worker-scoped, so parallel workers cannot each publish. **Canonical cell roster:** a pure matrix-expansion function exported from `captureRunFinalizer.ts` derives the full expected cell-key set — scenarios × themes × pointers × viewports plus the grayscale, reduced-motion, state-coverage, and settings-section cells — and is the single source consumed by both the spec (to enumerate captures) and the finalizer (to check completeness), so the two can never disagree. The finalizer publishes only on **exact expected-key-set equality**: a missing cell aborts publication with a diagnostic naming it, an unknown staged key aborts as foreign work, duplicate successful fragments for one cell (CI retries) resolve deterministically to the highest attempt index; then the single atomic rename publishes the labeled run. **Runner-final success attestation:** cell success is recorded through a reporter `onTestEnd` seam keyed to the runner's final `passed` result — after `afterEach` hooks and fixture teardown have settled — never by a write inside the test body, so a body-passes/teardown-fails attempt can never leave a success marker. **Explicit activation:** both the capture spec and the teardown are gated on an explicit capture-run id (environment variable set by the documented opt-in command in the spec header); an ordinary Playwright run — the existing specs, the 3.3 coarse-pointer spec — sees no active run id and the teardown exits without touching staging, including stale staging left by an earlier aborted capture run. `captureRunFinalizer.spec.ts` (node-side under the Playwright runner, temp staging trees, no browser) covers: missing cell, duplicate success, failed-then-successful retry, interrupted finalization, concurrent cell completion, matrix-expansion correctness, inactive-run no-op with staged files present, stale-staging refusal, and foreign-key rejection; integration coverage proves a body-pass/hook-fail and a body-pass/fixture-teardown-fail attempt each yield no success attestation.
- **Per-scenario readiness:** each manifest entry declares a readiness callback (beyond the visible checkpoint) that settles asynchronous descendants — lazy content, streaming placeholders, animation-driven layout — before capture; fresh browser context per matrix cell.
- Reuse existing `playwright.config.ts` (web server auto-start, `PLAYWRIGHT_BASE_URL` override) and existing fixture patterns from `web/tests/`.
- Tag the spec so it is excluded from any default CI test run (manual/opt-in execution), matching how existing live specs are handled.

**Acceptance:**

- 1.3.1 - The capture spec exists and produces the full named matrix in one run. test: `web/tests/style-surfaces.spec.ts`.
- 1.3.2 - A documented two-run before/after workflow (run, flip, run, compare by name) is described in the spec's header comment. behavior: "before/after capture workflow" in `web/tests/style-surfaces.spec.ts`.
- 1.3.3 - Every manifest entry asserts its visible checkpoint and readiness callback before capturing, the run fails if a checkpoint is absent, and the entry count is asserted against the live tab registry. behavior: "surface checkpoint assertion" in `web/tests/style-surfaces.spec.ts`.
- 1.3.4 - The grayscale subset covers state-bearing rows in both themes. behavior: "grayscale state subset" in `web/tests/style-surfaces.spec.ts`.
- 1.3.5 - Runs are immutable, pairable, and recoverable: attempt-scoped staging, per-cell manifest fragments, one run-level atomic finalizer registered via Playwright globalTeardown, overwrite refusal against finalized runs only, and a merged run-manifest JSON with git SHA and per-PNG hashes — a failed attempt or parallel cell never blocks a retry or corrupts a manifest. behavior: "immutable capture runs" in `web/tests/style-surfaces.spec.ts`.
- 1.3.6 - The reduced-motion subset captures the animation families under both preference states with computed-style suppression assertions. behavior: "reduced-motion subset" in `web/tests/style-surfaces.spec.ts`.
- 1.3.7 - Unphotographable surfaces (Traces, CodeGraphExplorer, AgentPortfolioPage) carry recorded representative mappings with equivalence rationales in the spec. behavior: "representative mappings" in `web/tests/style-surfaces.spec.ts`.
- 1.3.8 - The finalizer selects exactly one successful fragment per expected cell against the shared matrix-expansion roster with exact key-set equality — missing cells abort publication, unknown keys reject as foreign, retry duplicates resolve to the highest attempt, success attestation comes from the runner-final reporter seam, and an inactive run id makes the teardown a no-op even with stale staging present — with coverage for missing, duplicate, retry, interruption, parallel completion, matrix expansion, inactive-run, stale-staging, foreign-key, and body-pass/teardown-fail outcomes. test: `web/tests/support/captureRunFinalizer.spec.ts`.

### 1.4 Hoist the responsive tier into the theme layer [category: code] (depends: 1.2, 1.3)

`kind: deliverable`

Targets:
- `web/src/styles/tailwind-theme.css`
- `web/src/hooks/useIsMobile.ts::useIsMobile`
- `web/src/utils/platform.ts::*` — scope-reason: the hardcoded 768 viewport check folds onto the shared tier-token read; the file's indexed symbols are types only
- `web/src/utils/__tests__/platform.test.ts::*` — scope-reason: the device-capability rename and tier-token read update the module-reset import harness
- `web/src/components/activity/memory/KnowledgeGraph.tsx::*` — scope-reason: imports of the renamed device-capability exports update
- `web/src/components/code-graph/CodeGraphExplorer.tsx::*` — scope-reason: imports of the renamed device-capability exports update
- `web/src/styles/app-shell.css`
- `web/src/components/chat/styles/files-tab.css`
- `web/src/__tests__/cssTokenIntegrity.test.ts::*` — scope-reason: gains the compiled-variant-vs-emitted-property drift guard for the tier tokens
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/hooks/__tests__/useIsMobile.test.ts`
- `.impeccable.md`

**The one deliverable exempt from visual parity** (see Constraints). `.impeccable.md` "Responsive Tiers (Product UI)" already settles the model: mobile tier is viewport width ≤ 767px **or** height ≤ 500px, desktop is everything else (768px exact is desktop), the threshold "lives in a single **token** consumed by both CSS media queries and `useIsMobile`" with identical inclusivity on both sides, and the legacy 480px/430px one-offs collapse into the tier model. The code does not implement that contract, so every sheet migrated in P5–P7 would carry the drift into the end state and the 932×430 capture would keep exercising a tier clause that does not exist.

Verified drift (2026-07-29):

- `useIsMobile.ts:7-9` uses `< breakpoint` / `matchMedia("(max-width: 767px)")` — correct, and the only correct consumer.
- `platform.ts:13` independently hardcodes `window.innerWidth < 768`.
- Eight sheets use inclusive `@media (max-width: 768px)` — `activity-panel.css:323,374`, `layout.css:94,273`, `sessions-tab.css:172,179`, `input-responsive.css:78`, `app-shell.css:124`, `segmented-control.css:15`, `settings-overlay.css:450`. At exactly 768px the JS says desktop and the CSS says mobile.
- `app-shell.css:145` keeps `@media (max-width: 430px)`; `files-tab.css:97` keeps `@media (max-width: 480px)`.
- No `height ≤ 500px` media query exists anywhere in `web/src` — the landscape-phone clause has never been implemented.

Work:

- Author the tier **once** in `web/src/styles/tailwind-theme.css` as a `@theme static` block declaring the desktop breakpoint plus the mobile max-width and max-height values. Tailwind is `^4.3.0`, so the CSS-first theme layer supports this; the file currently declares no `--breakpoint-*` value and no custom variant. `static` is load-bearing: it forces the theme values to be emitted as `:root` custom properties even when no utility consumes them, which is what gives JS a runtime handle on the same declaration.
- **A CSS media query cannot evaluate `var()`** — `@media (max-width: var(--x))` is invalid and silently never matches. So the single `@custom-variant` encoding the width-OR-height mobile condition must be written with the theme values substituted at **build time**, producing literal pixel numbers in the compiled `@media` conditions. The custom properties are for JS only; nothing in a media-query condition may reference them.
- Point `useIsMobile.ts` at the emitted `:root` custom properties (read once off the document element via `getComputedStyle`, compose the `matchMedia` query) and collapse `platform.ts`'s independent 768 onto the same read. This gives one authoring site with two derivations — build-time literals for CSS, runtime property reads for JS — rather than one shared runtime value, which CSS cannot support.
- **One reactive geometry predicate; device heuristics stay out of it.** A single validated width-or-height query builder is the only source of layout-tier truth in JS, and it is reactive (`matchMedia` change listeners), matching the CSS variant exactly. `platform.ts`'s cached touch/user-agent classification is a *device-capability* signal: it must not feed geometry tokens or tier decisions, and its exports are renamed so they cannot be mistaken for layout signals (both graph explorers and the platform tests import `IS_MOBILE`/`IS_IOS` — their import sites update with the rename). The builder validates the token values it reads and falls back to the authored defaults (with a console warning) on a malformed or missing token rather than composing a query that never matches.
- **Boundary tests** pin the predicate: 767 vs 768px width, 500 vs 501px height, landscape (932×430 with fine pointer — layout is mobile-tier regardless of pointer), live resize across the boundary, a malformed tier token, `matchMedia`-absent environments (jsdom without the stub — the hook degrades without crashing), and change-listener cleanup on unmount.
- Convert every migrated `@media (max-width: 768px)` block to the generated variant as its sheet retires in P5–P7; correct the inclusivity at the point of conversion.
- Delete the `430px` and `480px` one-offs outright — the tier model replaces them, and 430px hardcodes miss 440pt phones (iPhone 16 Pro Max).
- `pointer: coarse` continues to govern target sizing only. Confirm no coarse rule changes layout; the Button size×dense ladder stays untouched.
- Guard: because the two derivations are separate compilation paths, the drift check is the whole safety net. Add a `cssTokenIntegrity` assertion that parses the literal numbers out of the **compiled** custom-variant media conditions and compares them against the emitted `:root` custom-property values the hook reads, failing if either side moves alone. Update `mobileChromeCss.test.ts` where it pins `(max-width: 430px)` (lines ~296, ~302) and `(max-width: 768px)` (line ~344).
- Intentional behavior deltas to record and re-baseline against: landscape phones (932×430 and peers) flip to single-pane mobile layout; a viewport of exactly 768px wide flips to desktop layout in CSS.

**Acceptance:**

- 1.4.1 - The mobile tier is authored once in a `@theme static` block, with the width-OR-height condition in a single custom variant whose compiled media conditions carry literal pixel values and no `var()` reference. file: `web/src/styles/tailwind-theme.css`.
- 1.4.2 - `useIsMobile` and `platform.ts` derive their threshold from the same token rather than a hardcoded number. file: `web/src/hooks/useIsMobile.ts`.
- 1.4.3 - No hardcoded 430px or 480px viewport threshold remains in `web/src`. file: `web/src/styles/app-shell.css`.
- 1.4.4 - A guard parses the compiled variant's literal media conditions and the emitted `:root` custom properties and fails when either side moves alone. test: `web/src/__tests__/cssTokenIntegrity.test.ts`.
- 1.4.5 - The height≤500px clause is live: a 932×430 viewport renders the mobile tier. behavior: "landscape phone renders mobile tier" in `web/src/hooks/useIsMobile.ts`.
- 1.4.6 - Boundary tests cover 767/768, 500/501, fine-pointer landscape, live resize, malformed token fallback, `matchMedia`-absent degradation, and listener cleanup; device-capability exports are renamed off the layout path. test: `web/src/hooks/__tests__/useIsMobile.test.ts`.

## P2: Settings Unification

`kind: framing`

**Goal**: One settings surface.

### 2.1 Retire legacy Settings.tsx onto SettingsOverlay [category: code] (depends: 1.4)

`kind: deliverable`

Targets:
- `web/src/__tests__/settingsSliderFocus.test.ts::*` — scope-reason: replaced wholesale with render-based focus assertions against the overlay slider
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/Settings.tsx::*` — scope-reason: the whole file is deleted
- `web/src/components/__tests__/Settings.test.tsx::*` — scope-reason: the legacy test file is retired with a per-assertion disposition map
- `web/src/components/app/useAppCommandPalette.ts::useAppCommandPalette`
- `web/src/components/app/__tests__/useAppCommandPalette.test.tsx::*` — scope-reason: both settings actions gain assertions that they open the overlay exactly once
- `web/src/components/settings/sections/AppearanceSection.tsx::*` — scope-reason: gains the ported reset-to-defaults action and pressed-group semantics alongside the slider
- `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx::*` — scope-reason: gains the ported aria-pressed group semantics and reset-to-defaults assertions
- `web/src/main.tsx::*` — scope-reason: the settings.css side-effect import is removed
- `web/src/styles/settings.css`
- `web/src/App.tsx::*` — scope-reason: the settingsOpen state and legacy Settings render block are removed
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: import expectations for the removed settings.css import update
- `web/src/hooks/useSettings.ts::*` — scope-reason: persisted font sizes clamp into the canonical 12–24 domain where the stored value enters state
- `web/src/hooks/__tests__/useSettings.test.ts::*` — scope-reason: clamp tests for persisted and API round trips at 12, 24, and 48
- `web/src/__tests__/App.test.tsx::*` — scope-reason: gains the App-level assertion that settings opens the overlay exactly once and the legacy branch is unreachable

The legacy panel is still live: rendered at `App.tsx:684-691` behind `settingsOpen`, opened from two command-palette actions (`web/src/components/app/useAppCommandPalette.ts:117` and `:196`), in parallel with the new `SettingsOverlay` (cog button → `settingsOverlay.open()`, `App.tsx:514`).

- **Control disposition map** (every legacy control gets an explicit destination; the overlay surface itself must not change rendered output): theme options (aria-pressed group) → AppearanceSection theme control; Default Mode group (`Settings.tsx:96-105`, aria-pressed) → ChatVoiceSection default-chat-mode select (deliberate semantics change on an already-shipped surface); font-size slider (drives `--font-size-base`; the live legacy control accepts 12–48 while the overlay slider is 12–24) → AppearanceSection slider, with **12–24 adopted as the canonical domain** (user decision 2026-08-08: single-user install, no out-of-range values worth preserving) — stored values normalize through one presence-preserving `normalizePersistedSettings` boundary at both untrusted entries in `useSettings.ts` (the localStorage read and the settings-API merge, which receives `Partial<Settings>` from `fetchUISettings` — the backend omits unset keys): the normalizer returns a **partial** object — when `fontSize` is not an own property of the input it stays absent so the spread merge preserves the prior valid value; a present finite number clamps into 12–24; a present invalid value (null, string, non-finite number, explicit undefined) maps to the default; a malformed persisted root is discarded whole — so an omitted remote key never overwrites a valid local font size and no stored value renders an unrepresentable UI or a `NaNpx` font size; **reset-to-defaults (`reset-button`, `Settings.tsx:115`) has no overlay equivalent — port it into AppearanceSection** as a reset action with a test.
- **Test disposition map** for `web/src/components/__tests__/Settings.test.tsx` (8 assertions): focus-first/Escape-close, forward/backward focus trap, and focus-restore are already covered by `SettingsOverlay.test.tsx:107/116/138` — retire with this mapping recorded in the commit; "labels setting groups and marks selected options as pressed" **ports** into `AppearanceSection.test.tsx` (theme group) and `ChatVoiceSection.test.tsx` (mode select present); legacy-model-selector-absent and both voice-controls-absent assertions retire (negative assertions about the deleted surface; placement covered by ChatVoiceSection tests).
- Redirecting the two command-palette actions must produce identical rendered output on the overlay — the redirect changes what opens, never how the overlay renders (1.4 stays the sole parity exemption).
- Repoint both `useAppCommandPalette.ts` actions to `settingsOverlay.open()`; delete the `settingsOpen` state and the `<Settings>` render block from `App.tsx`. Every retired opener gets direct transition coverage: `useAppCommandPalette.test.tsx` asserts both settings actions call `settingsOverlay.open()` exactly once, and an App-level assertion proves the legacy Settings branch can no longer render while the overlay opens.
- Delete `web/src/components/Settings.tsx` and `web/src/styles/settings.css` (276 lines; includes ~75 already-dead lines — `.settings-stack`, `.settings-row*`, `.model-select*`, `.loading-text`, `.no-models-text`); remove the settings.css import from `main.tsx`.
- Replace `web/src/__tests__/settingsSliderFocus.test.ts` (postcss-parses `settings.css` at module scope — it throws once the file is gone) with an equivalent render-based focus-ring assertion on the SettingsOverlay slider: no bare `outline` on rest state, `:focus-visible` ring using `var(--accent)` per the WCAG focus contract.
- Ratchet: drop `Settings.tsx` raw-element entries (4 button, 1 input), the `settings.css` `CSS_FILE_ALLOWLIST` entry; this batch deletes >200 CSS lines → lower ceiling in the same commit. Update `mobileChromeCss.test.ts` import expectations for the removed `main.tsx` import.

**Acceptance:**

- 2.1.1 - Command-palette settings actions open the overlay; the legacy panel is unreachable. file: `web/src/components/app/useAppCommandPalette.ts`.
- 2.1.2 - `Settings.tsx` and `settings.css` are deleted; `main.tsx` no longer imports the sheet. file: `web/src/main.tsx`.
- 2.1.3 - Slider focus-ring contract is asserted against the overlay implementation. test: `web/src/__tests__/settingsSliderFocus.test.ts`.
- 2.1.4 - Allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 2.1.5 - Reset-to-defaults exists in AppearanceSection with a test; the aria-pressed group-semantics assertion is ported. test: `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx`.
- 2.1.6 - Persisted and API font-size values normalize on load with field presence distinguished from invalidity: 12 and 24 round-trip unchanged, 48 loads as 24, present null/string/non-finite/explicit-undefined values and a malformed root fall back to the default, an absent `fontSize` key (localStorage and API merge) preserves the prior valid value, and the disposition map records the legacy 12–48 domain. test: `web/src/hooks/__tests__/useSettings.test.ts`.
- 2.1.7 - Both command-palette settings actions open the overlay exactly once, asserted at the hook level. test: `web/src/components/app/__tests__/useAppCommandPalette.test.tsx`.
- 2.1.8 - Opening settings through App renders SettingsOverlay exactly once and the deleted legacy `Settings` branch is unreachable. test: `web/src/__tests__/App.test.tsx`.

## P3: New Primitives

`kind: framing`

**Goal**: Six primitive additions land in `web/src/components/ui/` — five new (Chip, Card, FormField, NativeSelect, Textarea) plus TabBar promoted from `shared/` — each replacing every competing implementation it unifies. The app ends with **two sanctioned select paths**: Radix `ui/Select` for toolbar/picker contexts and `ui/NativeSelect` composed by `SelectField` for form contexts (the 3.3 rule). Primitives follow the Button pattern: component + separate `*Variants.ts` cva recipe, tokens only (no raw colors), coarse-pointer flow-through, focus rings per `focusStyles.ts`. Every interactive primitive carries an executable 44×44 coarse-pointer hit-area contract (see 3.3). 2.1 → 3.1 → 3.2 → 3.3 → 3.4 run serialized: each rewrites the exact-census ratchet ledger, which admits no concurrent writers.

### 3.1 ui/Chip primitive [category: code] (depends: 1.4, 2.1)

`kind: deliverable`

Targets:
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTab.helpers.tsx::*` — scope-reason: the uppercase tone-chip renderers across the helpers migrate onto Chip
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/tasks/task-execution.css`
- `web/src/components/ui/Chip.tsx`
- `web/src/components/ui/chipVariants.ts`
- `web/src/components/ui/__tests__/Chip.test.tsx`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

Four parallel status-chip implementations exist (plus the agents tag-inputs, excluded below). Create `Chip.tsx` + `chipVariants.ts`:

- API: `tone: neutral | accent | info | warning | error` (state palette, icon/lightness-first per `.impeccable.md`), `uppercase?: boolean` (default false — preserves the session-chip `text-transform: uppercase` delta over task chips), `asChild` via Radix Slot. **No `size` prop** — all four status-chip families are geometrically identical (verified 2026-08-08: `height 1.25rem; padding-inline .375rem; border-radius 9999px; font-size var(--text-2xs); font-weight 600; white-space nowrap`), so Chip ships one geometry.
- **Excluded from Chip's scope:** `AGENT_RULES_CHIP_*` (agents-styles.ts:76) is a removable tag-input token (`text-sm`, `rounded-xl`, embedded remove button) — a different species; it migrates to call-site utilities in the 4.2 sweep.
- **3.1 owns exactly two adopter families**: the uppercase tone chips + inline warning chip in `SessionsTab.helpers.tsx:104-153` (`.chip`/`.chip--*`) and the task chips in `TaskBadges.tsx` (`TASK_BADGE_CLS` + `.chip--state/--priority/--type` modifiers become tone + className). Every other live chip family adopts in the sweep that owns its surface, with explicit adoption acceptance there: the 17 `.activity-chip` adopters (agents list/detail → 4.2; pipelines defs list/detail → 4.3; the 13 integrations/memory/rules/skills/stages list and detail components → 4.8), `.activity-mcp-chip` (`mcp-tab.css:72`) → 4.8, and the `STEP_CHIP*`/`AGENT_RULES_CHIP*` constants → 4.1/4.2 (chip *display* usages compose Chip; the tag-input rows are excluded above). (Wiki citation chips are out of scope with the Ask surface — see 4.11.)
- Delete the `.chip` rule blocks from `sessions-tab.css` and `task-execution.css` as their two consumers migrate here (the sheets themselves retire in P5; deleting the duplicate pair here resolves the import-order-dependent collision). The `.activity-chip` rules stay in the activity-panel sheet until their last adopter migrates and die with that sheet in 5.4.
- Ratchet/guards: shrink `CLS_CONSTANT_ALLOWLIST` for `TaskBadges.tsx`; visual parity per surface; `ActivityRowStatusDot` untouched (dots are not chips).

**Acceptance:**

- 3.1.1 - Chip primitive and variants exist with tone + uppercase API. file: `web/src/components/ui/Chip.tsx`.
- 3.1.2 - The session and task chip families render through Chip; the duplicate `.chip` selector pair is gone. file: `web/src/components/chat/styles/sessions-tab.css`.
- 3.1.3 - Chip has unit coverage alongside the other `ui/` tests. test: `web/src/components/ui/__tests__/Chip.test.tsx`.
- 3.1.4 - State-bearing Chip tones carry a non-hue cue (icon or lightness step), asserted rather than left to review. test: `web/src/components/ui/__tests__/Chip.test.tsx`.

### 3.2 ui/Card primitive [category: code] (depends: 1.4, 3.1)

`kind: deliverable`

Targets:
- `web/src/components/activity/wiki/WikiQuickOpen.tsx::*` — scope-reason: skeleton shells and result cards adopt the Card primitive across the component
- `web/src/components/activity/FilesTab.tsx::*` — scope-reason: the line-667 shell is an immediate Card adoption ahead of the 4.6 sweep
- `web/src/components/dev/TierPreview.tsx::*` — scope-reason: immediate Card adoption replaces its hand-rolled shell
- `web/src/components/ui/Card.tsx`
- `web/src/components/ui/cardVariants.ts`
- `web/src/components/ui/__tests__/Card.test.tsx`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

~28 hand-rolled `rounded-lg border …` shells exist. Create `Card.tsx` + `cardVariants.ts`:

- API: `padding: none | sm | md`, `interactive?: boolean` (hover/focus treatment for clickable cards), `asChild`. Hierarchy from spacing and background shift, minimal borders, no nested cards (`.impeccable.md`).
- Immediate adoptions (rest arrive with their surface sweeps in P4): the three identical wiki skeleton shells (`animate-pulse rounded-lg border border-border bg-muted/30`), `dev/TierPreview`, `activity/FilesTab.tsx:667`, `wiki/WikiQuickOpen.tsx:154`.
- P4 sweeps then migrate: `AGENT_DEF_CARD_*`/`STEP_CARD_*` (agents), `STEP_CLS`/`ADD_DROPDOWN_CLS` (pipelines), `STEP_CARD_WRAPPER_CLS` (execution-utils), ToolCallCard's five card variants, ThinkingBlock, CodeBlockRenderers, MermaidBlock, ChannelDetailPanel, TerminalTab.

**Acceptance:**

- 3.2.1 - Card primitive and variants exist. file: `web/src/components/ui/Card.tsx`.
- 3.2.2 - Initial adoptions render through Card. file: `web/src/components/activity/wiki/WikiQuickOpen.tsx`.
- 3.2.3 - Card has unit coverage. test: `web/src/components/ui/__tests__/Card.test.tsx`.
- 3.2.4 - `interactive` Cards render a semantic focusable host and do not nest interactive elements inside it. test: `web/src/components/ui/__tests__/Card.test.tsx`.

### 3.3 ui/FormField primitive and fields consolidation [category: code] (depends: 1.4, 3.2)

`kind: deliverable`

Targets:
- `web/src/components/activity/fields/DateTimeField.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/fields/FieldPrimitives.tsx::*` — scope-reason: every field primitive is rebuilt on FormField + ui controls
- `web/src/components/settings/fields/StringListField.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/fields/KeyValueMapField.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/fields/TypedListField.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/fields/BoundedSelectField.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/sections/configFields.tsx::*` — scope-reason: hand-rolled label/row markup is replaced by FormField composition throughout
- `web/src/components/ui/FormField.tsx`
- `web/src/components/ui/NativeSelect.tsx`
- `web/src/components/ui/Textarea.tsx`
- `web/src/components/ui/Input.tsx::*` — scope-reason: the existing primitive gains the invisible coarse-pointer hit-area expansion
- `web/src/components/ui/Select.tsx::*` — scope-reason: Radix trigger and items gain the invisible coarse-pointer hit-area expansion
- `web/src/components/ui/__tests__/FormField.test.tsx`
- `web/src/components/ui/__tests__/Input.test.tsx`
- `web/src/components/ui/__tests__/Select.test.tsx`
- `web/src/components/ui/__tests__/Textarea.test.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: gains per-primitive computed-box assertions for the invisible hit-area contract
- `web/tests/coarse-pointer-hit-areas.spec.ts`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

Six labeled-form-row implementations exist. Create `FormField.tsx`: label + optional hint/error + control slot (`useId` wiring, `aria-describedby`), the shell equivalent of today's `fieldShellClass = "flex flex-col gap-1.5"` / `labelClass` / `controlClass` trio.

- Rebuild `activity/fields/FieldPrimitives.tsx` (TextField, SecretField, NumberField, TextAreaField, SelectField, TagsField) on `FormField` + `ui/Input` / `ui/Textarea` / `ui/NativeSelect` — this finally gives `ui/Input` (currently zero consumers) its adoption path. Remove the duplicated class trio from `DateTimeField.tsx:13-15`.
- **Create `ui/Textarea`** (the repo has no Textarea primitive today — the adopters above and the 4.10 composer migration reference a component that must be built here): `forwardRef<HTMLTextAreaElement>`, full native prop passthrough, class merging, `aria-invalid`/`aria-describedby` error wiring, token-only styling on the shared focus/coarse-pointer contract. Its unit test pins ref forwarding to a real `HTMLTextAreaElement` and auto-grow compatibility (height changes driven through the forwarded ref survive re-render) — the chat composer depends on both.
- Migrate `settings/fields/*` (`StringListField`, `KeyValueMapField`, `TypedListField`, `BoundedSelectField`) and `settings/sections/configFields.tsx` onto the same primitives; their hand-rolled label/row markup goes away.
- The remaining field variants (`agents-styles.ts` `AGENT_EDIT_FIELD/LABEL/HINT/INPUT`, `PipelineEditor.styles.ts` `FIELD_*`, `ValidationDetectionEditor` `FORM_FIELD_CLS` family) migrate in their P4 surface sweeps onto these primitives.
- Select consolidation decision encoded here: **form contexts use `SelectField`, which composes the new `ui/NativeSelect`; toolbar/picker contexts use Radix `ui/Select`**. That is the whole-app rule the P4 sweeps apply. `NativeSelect` is the smallest boundary that keeps the native-select behavior form contexts want while satisfying the standing rule that raw `<select>` lives only inside `components/ui` — a native select rendered directly by `FieldPrimitives.tsx` would remain a raw element outside `ui/` and could never reach the ratchet end state.
- Ratchet: `FieldPrimitives.tsx` raw-element entries (1 button, 4 input, 1 select, 1 textarea) drop to zero, with the select composing inside `ui/`; settings-section input entries shrink.
- **Executable 44×44 coarse-pointer contract, per primitive.** `Input`, `Textarea`, `NativeSelect`, and Radix `Select` triggers/items currently sit at 36px; under `pointer: coarse` each primitive supplies an invisible hit-area expansion (pseudo-element or padding compensation) reaching 44×44 without changing rendered visuals — parity-safe by construction. `coarsePointerTouchTargets.test.ts` gains computed-box assertions for each primitive and for representative migrated compositions as the P4 sweeps land; dense `Button` compositions are constrained to documented moats that supply an equivalent target. The Button size×dense ladder itself stays untouched. JSDOM computed-box assertions cannot prove pseudo-element hit-test geometry, so a Chromium spec (reusing the 1.3 Playwright substrate) additionally proves **effective activation**: under an emulated coarse pointer, clicks at the expanded perimeter of Input, Textarea, NativeSelect, and Radix Select trigger/items activate or focus the control while visible geometry is unchanged. The spec runs under the existing chromium project with spec-level `test.use` coarse-pointer emulation (`hasTouch` plus the touch descriptor — the same pointer axis the 1.3 matrix asserts); its named command — `cd web && npx playwright test coarse-pointer-hit-areas.spec.ts` — is part of this leaf's validation and the V2 checklist.

**Acceptance:**

- 3.3.1 - FormField exists with label/hint/error/control API. file: `web/src/components/ui/FormField.tsx`.
- 3.3.2 - Activity field primitives and settings fields render through FormField and ui controls. file: `web/src/components/activity/fields/FieldPrimitives.tsx`.
- 3.3.3 - `ui/Input` has production consumers. symbol: `Input`.
- 3.3.4 - FormField has unit coverage. test: `web/src/components/ui/__tests__/FormField.test.tsx`.
- 3.3.5 - FormField pins label-to-control association, hint/error `aria-describedby`, and `aria-invalid` wiring. test: `web/src/components/ui/__tests__/FormField.test.tsx`.
- 3.3.6 - `NativeSelect` exists in `ui/` on the shared focus/token/coarse-pointer contract, and both select paths (native and Radix) are unit-tested. file: `web/src/components/ui/NativeSelect.tsx`.
- 3.3.7 - Computed-box tests prove 44×44 coarse-pointer hit areas for Input, Textarea, NativeSelect, and Radix Select trigger/items via invisible expansion, with rendered visuals unchanged. test: `web/src/__tests__/coarsePointerTouchTargets.test.ts`.
- 3.3.8 - `ui/Textarea` exists with ref-forwarding and auto-grow-compatibility tests. file: `web/src/components/ui/Textarea.tsx`.
- 3.3.9 - A Chromium spec proves click/focus activation at the expanded 44×44 perimeter under `pointer: coarse` for Input, Textarea, NativeSelect, and Radix Select trigger/items, with visible geometry unchanged, executed via `cd web && npx playwright test coarse-pointer-hit-areas.spec.ts`. test: `web/tests/coarse-pointer-hit-areas.spec.ts`.

### 3.4 Promote TabBar into ui/ [category: refactor] (depends: 1.4, 3.3)

`kind: deliverable`

Targets:
- `web/src/components/shared/TabBar.tsx::*` — scope-reason: the whole file moves to ui/ (94 lines, roving focus, currently 1 raw button)
- `web/src/components/shared/__tests__/TabBar.test.tsx::*` — scope-reason: the test moves alongside the component to ui/__tests__/
- `web/src/components/ui/TabBar.tsx`
- `web/src/components/ui/__tests__/TabBar.test.tsx`
- `web/src/components/FilesPage.tsx::*` — scope-reason: the line-281 tab strip adopts TabBar with the new onTabClose slot
- `web/src/components/agents/AgentEditForm.tsx::*` — scope-reason: the sidebar tab strip (lines 271-281) adopts TabBar
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

`shared/TabBar.tsx` (94 lines, roving focus, `role="tablist"`, zero production consumers) is the blessed tab strip. Move it to `components/ui/` (with its test), then adopt:

- `FilesPage.tsx:281` tab strip (`TABS_CLS`/`TAB_CLS`/`TAB_ACTIVE_CLS`/`TAB_NAME_CLS`/`TAB_CLOSE_CLS` — the only other `role="tablist"`; needs a per-tab close affordance, so extend TabBar with an optional `onTabClose`/actions slot rather than forking).
- The `.sidebar-tab-bar`/`.sidebar-tab*` rules in `SidebarPanel.css` (consumer: `AgentEditForm.tsx:271-281`) — adopt TabBar here, which orphans those rules. The sheet itself is deleted in **4.2** together with its importing component (`SidebarPanel.tsx` carries the side-effect import), where the allowlist entry and the `.sidebar-tab` pin in `mobileChromeCss.test.ts` also drop; deleting the sheet here would break the still-live import.
- `AgentPickerDropdown` scope toggle (`SCOPE_TOGGLE_CLS` family) — migrate to `SegmentedControl` (it is a value toggle, not navigation) during 4.10; noted here so TabBar's scope stays navigation-only.
- `.activity-panel-tab-strip` (`activity-panel.css:22`) adoption happens in 5.4 with that sheet's retirement.

**Acceptance:**

- 3.4.1 - TabBar lives in `components/ui/` with its test moved alongside. file: `web/src/components/ui/TabBar.tsx`.
- 3.4.2 - FilesPage and AgentEditForm tab strips render through TabBar; the `.sidebar-tab*` rules are orphaned pending the 4.2 sheet retirement. file: `web/src/components/agents/AgentEditForm.tsx`.
- 3.4.3 - TabBar pins tab/tablist roles, roving Arrow/Home/End focus, and keeps the close action out of the tab's own activation path. test: `web/src/components/ui/__tests__/TabBar.test.tsx`.

## P4: Surface Sweeps — raw elements and *_CLS to the sanctioned floor

`kind: framing`

**Goal**: Every unsanctioned raw `<button>`/`<input>`/`<select>`/`<textarea>` composes a `ui/` primitive; every `*_CLS` constant folds into cva recipes or call-site utilities; per-file allowlist entries hit zero as each sweep lands. Counts below are the 2026-07-29 snapshot — re-read the live allowlist per leaf. Each sweep: migrate, delete constants, shrink allowlist entries (and `inputFocusAdoption` entries where named), verify visual parity, run full web validation. Sweeps run serialized 4.1 → 4.2 → … → 4.10: every sweep rewrites the same exact-census ledger and shared guard pins, which admit no concurrent writers; the chain adds no functional coupling beyond the dependencies already noted.

### 4.1 Agents editors sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/agents/agents-styles.ts::*` — scope-reason: editor-facing constant sections are deleted as they empty
- `web/src/components/agents/AgentEditForm.tsx::*` — scope-reason: 25 raw elements and the AGENT_* constant consumers migrate onto primitives across the form
- `web/src/components/agents/AgentRulesEditor.tsx::*` — scope-reason: 16 raw elements plus rules-chip constant usage migrate
- `web/src/components/agents/AgentStepsEditor.tsx::*` — scope-reason: 20 raw elements plus step-card constants migrate
- `web/src/components/agents/AgentVariablesEditor.tsx::*` — scope-reason: 7 raw elements migrate
- `web/src/components/agents/AgentSkillsEditor.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/agents/AgentToolBlocksEditor.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/agents/IsolationTargetSelector.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable

The heaviest single surface: `AgentEditForm.tsx` (8 btn / 7 input / 9 select / 1 textarea), `AgentRulesEditor.tsx` (11 btn / 2 input / 3 select), `AgentStepsEditor.tsx` (10 btn / 4 input / 3 select / 3 textarea), `AgentVariablesEditor.tsx` (5 btn / 2 input), `AgentSkillsEditor.tsx`, `AgentToolBlocksEditor.tsx`, `IsolationTargetSelector.tsx`. Styling from `agents-styles.ts` (113 `*_CLS`, ~258 lines): `AGENT_BTN*` → `Button` variants; `AGENT_EDIT_FIELD/LABEL/HINT/INPUT` → FormField + ui controls; chip *display* constants (`STEP_CHIP*`) adopt the 3.1 Chip primitive here; step cards → Card; selects per the 3.3 rule. Delete `agents-styles.ts` sections as they empty; the editor-facing sections should empty here.

**Acceptance:**

- 4.1.1 - Agent editor components compose ui primitives exclusively; their raw-element and `*_CLS` allowlist entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.1.2 - Editor sections of `agents-styles.ts` are deleted. file: `web/src/components/agents/agents-styles.ts`.

### 4.2 Agents cards and portfolio sweep [category: refactor] (depends: 4.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/agents/AgentPortfolioPage.tsx::*` — scope-reason: portfolio-wide sweep of raw elements and card/filter styling onto primitives
- `web/src/components/agents/agents-styles.ts::*` — scope-reason: the file is deleted entirely with its allowlist entry
- `web/src/components/activity/agents/AgentsTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/agents/AgentsDetailPanel.tsx::*` — scope-reason: its status chips adopt ui/Chip with the surface sweep
- `web/src/components/agents/AgentEditForm.tsx::*` — scope-reason: absorbs the SidebarPanel shell utilities as the component retires
- `web/src/components/agents/__tests__/AgentEditors.test.tsx::*` — scope-reason: destination for the ported SidebarPanel a11y assertions (focus trap, Escape, focus restore)
- `web/src/components/shared/SidebarPanel.css`
- `web/src/components/shared/SidebarPanel.tsx::*` — scope-reason: the component is retired and its shell folds into AgentEditForm utilities
- `web/src/components/shared/__tests__/SidebarPanel.test.tsx::*` — scope-reason: the a11y assertions (focus trap, Escape, focus restore) port to AgentEditForm-level tests as the component retires
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change

`AgentPortfolioPage.tsx` (2 btn / 2 select incl. `.agent-filter-select`), the `AGENT_DEF_CARD_*` / `STEP_CARD_*` families → Card, remaining `agents-styles.ts` content deleted, file removed entirely with its `CLS_CONSTANT_ALLOWLIST` entry (113 → 0). `AGENT_RULES_CHIP_*` tag-inputs (excluded from Chip per 3.1) become call-site utilities here. `SidebarPanel.tsx` (1 btn) — **retire the component** (verified 2026-08-08: `AgentEditForm` is its only production consumer): fold the panel shell into AgentEditForm utilities, port the SidebarPanel a11y test assertions (focus trap, Escape close, focus restore) into `AgentEditors.test.tsx`, delete `SidebarPanel.css` (44 lines — its side-effect import lives in the retiring `SidebarPanel.tsx:5`; its `.sidebar-tab*` rules were orphaned in 3.4) with its `CSS_FILE_ALLOWLIST` entry and the sidebar pin in `mobileChromeCss.test.ts` (line ~492), and remove the SidebarPanel allowlist entries. `AgentsTabList.tsx` and `AgentsDetailPanel.tsx` status chips adopt `ui/Chip` here.

**Acceptance:**

- 4.2.1 - `agents-styles.ts` is deleted and its allowlist entry removed. file: `web/src/components/agents/agents-styles.ts`.
- 4.2.2 - Portfolio filter selects follow the Select rule; agents/ raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.2.3 - AgentsTabList and AgentsDetailPanel status chips render through ui/Chip; `SidebarPanel.css` is deleted with its importing component. file: `web/src/components/activity/agents/AgentsDetailPanel.tsx`.
- 4.2.4 - AgentEditForm-level tests prove focus trapping, Escape close, and focus restoration survive the SidebarPanel retirement. test: `web/src/components/agents/__tests__/AgentEditors.test.tsx`.

### 4.3 Pipelines sweep [category: refactor] (depends: P3, 4.2)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/pipelines/PipelineEditor.styles.ts::*` — scope-reason: the file is deleted with its allowlist and focus-adoption entries
- `web/src/components/activity/pipelines/PipelineEditor.tsx::*` — scope-reason: editor form, buttons, and textarea migrate onto FormField/ui controls
- `web/src/components/activity/pipelines/PipelineStepFields.tsx::*` — scope-reason: 14 raw controls migrate onto FormField/ui controls
- `web/src/components/activity/pipelines/PipelineStepList.tsx::*` — scope-reason: 8 raw controls and step-card styling migrate
- `web/src/components/activity/pipelines/PipelinesDefsList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/pipelines/PipelinesDefsDetail.tsx::*` — scope-reason: its status chips adopt ui/Chip with the surface sweep
- `web/src/components/shared/executions/execution-utils.tsx::*` — scope-reason: workflow-trace icon utilities and the execution card/badge/button styling both migrate onto ui primitives
- `web/src/components/activity/PipelinesTab.tsx::*` — scope-reason: tab raw buttons and execution styling migrate to primitives
- `web/src/components/__tests__/inputFocusAdoption.test.ts::*` — scope-reason: the pipelines entry is removed as the surface migrates

`PipelineEditor.tsx` (3 btn / 1 input / 1 textarea), `PipelineStepFields.tsx` (2 btn / 10 input / 2 textarea), `PipelineStepList.tsx` (6 btn / 1 input / 1 select), `PipelinesDefsList.tsx` (1 btn), `web/src/components/activity/PipelinesTab.tsx` (3 btn — `RAW_ELEMENT_ALLOWLIST` line 33; owned here so it does not survive into the endgame floor). `PipelineEditor.styles.ts` (47 `*_CLS`): `BTN_CLS`/`BTN_PRIMARY_CLS` → Button; `FIELD_*` → FormField; `STEP_CLS`/`ADD_DROPDOWN_CLS` → Card; `KV_*` rows → utilities. Delete the file (removes both its `CLS_CONSTANT_ALLOWLIST` entry and its `inputFocusAdoption` entry). `execution-utils.tsx` (20 `*_CLS`: run buttons, badges → Button/Badge/Chip, step cards → Card) sweeps here too since PipelinesTab consumes it.

**Acceptance:**

- 4.3.1 - `PipelineEditor.styles.ts` is deleted; pipelines raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.3.2 - `execution-utils.tsx` styles via ui primitives and utilities. file: `web/src/components/shared/executions/execution-utils.tsx`.
- 4.3.3 - The pipelines `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.
- 4.3.4 - PipelinesDefsList and PipelinesDefsDetail status chips render through ui/Chip. file: `web/src/components/activity/pipelines/PipelinesDefsDetail.tsx`.

### 4.4 Wiki sweep [category: refactor] (depends: P3, 4.3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/wiki/WikiPageReader.tsx::*` — scope-reason: 7 raw buttons across the reader chrome migrate
- `web/src/components/activity/WikiTab.tsx::*` — scope-reason: 6 raw controls migrate across the tab shell
- `web/src/components/activity/wiki/WikiBacklinks.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/wiki/WikiSourcesManager.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/WikiSourceRemovalDialog.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/wiki/WikiPageTree.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/wiki/WikiGraphView.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/wiki/WikiTabToolbar.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/wiki/WikiPageEditor.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/wiki/WikiQuickOpen.tsx::*` — scope-reason: skeleton shells and result cards adopt the Card primitive across the component
- `web/src/components/__tests__/inputFocusAdoption.test.ts::*` — scope-reason: the WikiQuickOpen entry is removed as the surface migrates

~21 buttons + ~8 other controls. `WikiResearchMode.tsx` no longer exists (deleted with #19683), and `WikiAskMode.tsx` is excluded from this sweep — its surface is being replaced (see 4.11). In scope: `WikiPageReader.tsx` (7 btn), `WikiBacklinks.tsx` (3), `WikiSourcesManager.tsx` (3), `WikiTab.tsx` (3 btn / 3 input), `WikiPageTree.tsx` (2), `WikiTabToolbar.tsx` (1), `WikiSourceRemovalDialog.tsx` (2 btn / 1 input), `WikiGraphView.tsx` (2 input), `WikiPageEditor.tsx` (1 input), `WikiQuickOpen.tsx` (1 input + `inputFocusAdoption` entry). Buttons → Button (ghost/secondary per role); inputs → Input; selects per rule.

**Acceptance:**

- 4.4.1 - All wiki/ raw-element allowlist entries are zero except the deferral-covered `WikiAskMode.tsx` entries (7 button / 1 textarea), which carry a comment naming the 4.11 deferral. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.4.2 - The WikiQuickOpen `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.11 Wiki Ask-surface migration (deferred)

`kind: deferred`

The wiki Ask and Research modes are being replaced by agent-native exploration (#19672); Research mode is already deleted (#19683). Migrating `WikiAskMode.tsx`'s raw elements inside Phase 2 would style a surface scheduled for replacement, so that slice of 4.4.1 moves to the replacement work, which builds on the ui/ primitives this plan ships.

```yaml
deferral:
  task_ref: "#19672"
  reason: "WikiAskMode is replaced by agent-native exploration; its raw-element migration (7 button / 1 textarea) lands with the replacement surface, built on ui/ primitives."
  owner: "wiki-exploration retool (#19672)"
  original_acceptance_items:
    - 4.4.1
```

### 4.5 Graph explorers sweep [category: refactor] (depends: P3, 4.4)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/code-graph/CodeGraphExplorer.tsx::*` — scope-reason: explorer chrome sweep folds 32 _CLS constants and raw controls into primitives; graph logic untouched but interleaved through the file
- `web/src/components/activity/memory/KnowledgeGraph.tsx::*` — scope-reason: 19 _CLS constants and 10 raw controls across the graph chrome migrate; canvas logic untouched
- `web/src/components/__tests__/inputFocusAdoption.test.ts::*` — scope-reason: both explorer entries are removed as the surfaces migrate

`CodeGraphExplorer.tsx` (32 `*_CLS`, 6 btn, 5 input) and `activity/memory/KnowledgeGraph.tsx` (19 `*_CLS`, 5 btn, 5 input): controls/search/legend/physics panels → Button, Input, Card, utilities at call site. Both files carry `inputFocusAdoption` entries — removed on migration. Canvas/graph rendering logic untouched.

**Acceptance:**

- 4.5.1 - Both explorers' raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.5.2 - Both `inputFocusAdoption` entries are removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.6 FilesPage sweep [category: refactor] (depends: P3, 4.5)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/FilesPage.tsx::*` — scope-reason: page-wide sweep of 49 _CLS constants, tab strip, toolbar, and dialogs onto primitives
- `web/src/components/activity/FilesTab.tsx::*` — scope-reason: all 12 nested native controls (rename inputs, action-menu triggers, context-menu items, move dialog) migrate onto ui primitives while the composite div rows keep their keyboard-guard semantics
- `web/src/components/chat/styles/files-tab.css`

49 `*_CLS` + 4 buttons: page shell/sidebar/tree/toolbar/viewers → utilities + Card; tab strip → TabBar (3.4); `CONFIRM_KEEP_CLS`/`CONFIRM_DISCARD_CLS` dialog → `ConfirmDialog` + Button; git-status color constants stay as data maps (they are token lookups, not style strings — rename away from `_CLS` if the ratchet pattern catches them). `.file-viewer-btn` usage ties into `files-tab.css` retirement (5.5).

**FilesTab nested-control migration** (owns the entries the old sanctioned floor mis-covered; classification verified 2026-08-08 — all 12 are ordinary controls, the composite tree rows are divs the ratchet never counted): inline-rename inputs (`FilesTab.tsx:437/:496`) → `ui/Input` keeping Enter/Escape/blur row-embed behavior; per-row Actions menu triggers (`:452/:516`) → `Button size="icon" variant="ghost"`; the five context-menu `role="menuitem"` buttons (`:642-651`) → Button-composed menu items; move-dialog input and Cancel/Move buttons (`:678/:690/:693`) → `ui/Input` + `ui/Button`. FilesTab raw-element entries reach zero.

**Acceptance:**

- 4.6.1 - FilesPage and FilesTab raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.6.2 - The discard-confirm flow uses ConfirmDialog. file: `web/src/components/FilesPage.tsx`.
- 4.6.3 - FilesTab composite tree rows keep their div/keyboard-guard semantics while every nested native control composes a ui primitive. file: `web/src/components/activity/FilesTab.tsx`.

### 4.7 Tasks sweep [category: refactor] (depends: P3, 4.6)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/tasks/TaskCreateForm.tsx::*` — scope-reason: form-wide migration of fields, selects, and buttons onto FormField/ui controls
- `web/src/components/tasks/QuickCaptureTask.tsx::*` — scope-reason: 12 _CLS constants and 3 raw controls migrate; its inputFocusAdoption entry is removed
- `web/src/components/tasks/taskModalStyles.ts::*` — scope-reason: the file is deleted with its allowlist entry
- `web/src/components/activity/TaskFieldEditors.tsx::*` — scope-reason: 5 raw controls including the inline-edit select migrate onto FormField/ui controls
- `web/src/components/activity/TaskCloseDialog.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TaskTreeRow.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/__tests__/inputFocusAdoption.test.ts::*` — scope-reason: the QuickCaptureTask entry is removed as the surface migrates

`TaskCreateForm.tsx` (14 `*_CLS`, 3 btn / 2 input / 4 select / 2 textarea), `QuickCaptureTask.tsx` (12 `*_CLS`, 2 btn / 1 input, `inputFocusAdoption` entry), `taskModalStyles.ts` (3), `TaskBadges.tsx` (3 — Chip from 3.1), `TaskFieldEditors.tsx` (1 btn / 2 input / 1 select / 1 textarea incl. `.task-inline-edit--select`), `TaskCloseDialog.tsx` (1 textarea), `TaskTreeRow.tsx` (2 btn). Modals → Dialog primitives; forms → FormField + ui controls; selects per rule.

**Acceptance:**

- 4.7.1 - tasks/ raw-element and `*_CLS` entries are zero (incl. `taskModalStyles.ts` deleted). file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.7.2 - The QuickCaptureTask `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.8 Activity lists and detail panels sweep [category: refactor] (depends: P3, 4.7)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: filter-panel selects and rules-tab styling migrate to SelectField/primitives (its local RulesFilterDropdown rebuilds on the shared presentational shell in 4.9)
- `web/src/components/activity/SkillsTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/skills/SkillsHubView.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/skills/SkillsInstalledList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/rules/RulesDetailPanel.tsx::*` — scope-reason: its status chips adopt ui/Chip with the surface sweep
- `web/src/components/activity/skills/SkillsInstalledDetail.tsx::*` — scope-reason: its status chips adopt ui/Chip with the surface sweep
- `web/src/components/activity/stages/ProfileDetailPanel.tsx::*` — scope-reason: its status chips adopt ui/Chip with the surface sweep
- `web/src/components/activity/stages/StageDetailPanel.tsx::*` — scope-reason: its status chips adopt ui/Chip with the surface sweep
- `web/src/components/activity/MemoryTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/memory/MemoryDetailPanel.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/memory/MemoryTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/IntegrationsTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/integrations/IntegrationsFilterPanel.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/integrations/ChannelDetailPanel.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/integrations/ChannelsList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/StagesTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/stages/StagesList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/stages/ProfilesList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TracesTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/CronTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/FileChangesTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/ActivityMcpTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/taskdetail/TaskDetailKV.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/taskdetail/TaskDetailRelationships.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TasksTabDetailPanel.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/PlanReviewCard.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/rules/RulesTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/fields/KeyValueField.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable

The 1–4-count long tail across activity surfaces: `RulesTab.tsx` (1 btn / 4 select), `SkillsTab.tsx` (1 btn / 2 select), `SkillsHubView.tsx` (1 btn / 1 input / 1 select), `SkillsInstalledList.tsx`, `MemoryTab.tsx` (1 btn / 3 input), `MemoryDetailPanel.tsx`, `MemoryTabList.tsx`, `IntegrationsTab.tsx`, `IntegrationsFilterPanel.tsx` (2 select), `ChannelDetailPanel.tsx` (1 btn / 1 input), `ChannelsList.tsx`, `StagesTab.tsx`, `StagesList.tsx`, `ProfilesList.tsx`, `TracesTab.tsx` (2 btn), `CronTab.tsx` (2 btn), `FileChangesTab.tsx` (2 btn), `ActivityMcpTab.tsx` (4 btn), `WikiTab` covered by 4.4, `TaskDetailKV.tsx`, `TaskDetailRelationships.tsx`, `TasksTabDetailPanel.tsx`, `AgentsTabList.tsx`, `PlanReviewCard.tsx`, `RulesTabList.tsx`, `KeyValueField.tsx` (2 btn / 2 input), `DateTimeField.tsx` (1 input). Unclassed selects styled by `.activity-filter-panel__field select` descend from the filter panels — migrate to `SelectField` so 5.5 can delete those descendant rules. The 13 `.activity-chip` adopters owned here (the integrations, memory, rules, skills, and stages lists **and** their detail panels) plus the `.activity-mcp-chip` renderer compose `ui/Chip` as each surface sweeps.

**Acceptance:**

- 4.8.1 - All listed activity files' raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.8.2 - Filter-panel selects render through SelectField. file: `web/src/components/activity/RulesTab.tsx`.
- 4.8.3 - Every `.activity-chip` adopter in this sweep and the `.activity-mcp-chip` renderer compose ui/Chip; the orphaned `.activity-chip` rules die with their sheet in 5.4. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

### 4.9 Activity chrome sweep [category: refactor] (depends: P3, 4.8)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/ActivityFilterDropdown.tsx::*` — scope-reason: keeps its immediate-select-and-close controller and rebuilds on the shared presentational filter shell
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: panel chrome, local dropdown, and sheet-import relocation all land here
- `web/src/components/activity/FilterPrimitives.tsx::*` — scope-reason: the shared presentational filter-field/shell component is built here on ui primitives
- `web/src/components/activity/SessionsFilterDropdown.tsx::*` — scope-reason: 6 raw controls rebuild on the shared presentational shell; live-propagation and Apply-to-close semantics are preserved
- `web/src/components/activity/TasksTabFilters.tsx::*` — scope-reason: its draft/Apply controller rebuilds on the shared presentational shell; staged-commit semantics are preserved
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: the local RulesFilterDropdown rebuilds on the shared presentational shell in place, after the 4.8 control migration
- `web/src/components/activity/TasksTabToolbar.tsx::*` — scope-reason: the tasks filter trigger and toolbar chrome migrate onto primitives
- `web/src/components/activity/__tests__/SessionsFilterDropdown.test.tsx::*` — scope-reason: apply/reset/Escape/outside-click/focus semantics are pinned before and after the rebuild
- `web/src/components/activity/__tests__/sessionsFilters.test.ts::*` — scope-reason: live-propagation filter semantics are pinned before and after the rebuild
- `web/src/components/activity/__tests__/TasksTab.filters.test.tsx::*` — scope-reason: draft/Apply staged-commit semantics are pinned before and after the rebuild
- `web/src/components/activity/__tests__/ActivityFilterDropdown.test.tsx`
- `web/src/components/activity/rules/__tests__/RulesTab.test.tsx::*` — scope-reason: gains direct state-transition coverage for the local RulesFilterDropdown controller
- `web/src/components/activity/ActivityPanelSearch.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/QuickMenu.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionInteractionModal.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTabDetail.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/terminal/TerminalKeysBar.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/terminal/TerminalView.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable

Panel chrome and popup dropdowns: `ActivityPanel.tsx` (2 btn, local `ActivityDropdown`), `ActivityFilterDropdown.tsx` (1 btn), `SessionsFilterDropdown.tsx` (1 btn / 5 input), `TasksTabFilters.tsx` (`FilterDropdown` mirror), the local `RulesFilterDropdown` (`RulesTab.tsx:44`), `ActivityPanelSearch.tsx` (1 input), `web/src/components/activity/FilterPrimitives.tsx` (1 input — `RAW_ELEMENT_ALLOWLIST` line 119; the shared filter-dropdown consolidation below is its natural owner), `QuickMenu.tsx` (2 btn), `SessionInteractionModal.tsx` (2 btn / 1 textarea), `SessionsTab*.tsx` (3 btn), `terminal/TerminalKeysBar.tsx` (2 btn / 1 input), `terminal/TerminalView.tsx` (2 btn). **Share presentation, keep the controllers.** The four filter dropdowns are visually similar but behaviorally divergent — `ActivityFilterDropdown` is immediate-select-and-close, Sessions propagates live and uses Apply only to close, Tasks stages a draft until Apply, and Rules opens from an inline panel. Extract one shared **presentational** filter-field/shell component (trigger, popup shell, field rows — Button + DropdownCaret + FormField controls, built in `FilterPrimitives.tsx`) and rebuild all four controllers on it **without changing any controller's open/close, draft/apply, live-propagation, reset, Escape, outside-click, or focus semantics**; the existing filter tests pin those semantics before and after. All four controllers carry direct state-transition tests: Sessions (`SessionsFilterDropdown.test.tsx` plus `sessionsFilters.test.ts`), Tasks (`TasksTab.filters.test.tsx`), Activity (`ActivityFilterDropdown.test.tsx` — new — pinning immediate-select-and-close), and Rules (`RulesTab.test.tsx` gains open/close, reset, Escape, outside-click, and focus-return coverage for the local controller). The Rules/Skills/Integrations *inline filter panel* family is exclusively 5.5's shared-panel work, not part of this consolidation.

**Acceptance:**

- 4.9.1 - One shared presentational filter-field/shell component serves all four filter dropdowns, each keeping its own controller with apply/reset/Escape/outside-click/focus semantics proven by the ported tests, and each of the four controllers mapped to a direct test (Sessions, Tasks, Activity immediate-select-and-close, Rules open/reset/Escape/outside-click/focus-return). file: `web/src/components/activity/FilterPrimitives.tsx`.
- 4.9.2 - Listed chrome files' raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

### 4.10 Chat, command-browser, and app-shell sweep [category: refactor] (depends: P3, 4.9)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/ProviderPicker.tsx::*` — scope-reason: picker buttons and dialog chrome migrate to primitives
- `web/src/components/chat/CommandBar.tsx::*` — scope-reason: raw button migrates to Button with the bar-sizing hook class
- `web/src/components/chat/PlanApprovalActions.tsx::*` — scope-reason: its textarea migrates to ui/Textarea within the approval flow
- `web/src/components/chat/ChatInput.tsx::*` — scope-reason: the composer textarea migrates to ui/Textarea; the sanctioned icon-button moat keeps its pinned entries
- `web/src/components/chat/ToolCallCard.tsx::*` — scope-reason: the header buttons and copy input migrate to primitives while the expandable-header composite semantics stay
- `web/src/App.tsx::*` — scope-reason: its raw button migrates and the legacy Settings render block was removed in 2.1
- `web/src/components/ProjectSelector.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/ValidationDetectionEditor.tsx::*` — scope-reason: 9 _CLS constants and 3 raw controls migrate; its inputFocusAdoption entry is removed
- `web/src/components/auth/LoginPage.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/app/AppErrorBoundary.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/BranchIndicator.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/AgentPickerDropdown.tsx::*` — scope-reason: 11 _CLS constants migrate and the scope toggle moves to SegmentedControl
- `web/src/components/chat/ChatCommandPalette.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/CommandPalette.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/ResumeSessionModal.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/ActiveAgentIndicator.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/CodeBlockRenderers.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/ToolResultImage.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/ChatInputModelControls.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/chat/ChatInputToolbar.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/command-browser/ToolBrowserModal.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/command-browser/SkillBrowserModal.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/command-browser/ToolArgumentForm.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/SettingsOverlay.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/sections/PromptsTemplatesSection.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/settings/sections/McpToolsSection.tsx::*` — scope-reason: its raw filter input (line 283) migrates through ui/Input
- `web/src/components/settings/sections/ToolApprovalsSection.tsx::*` — scope-reason: its raw filter input (line 141) migrates through ui/Input
- `web/src/components/shared/DiffBlock.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/shared/MermaidBlock.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/__tests__/inputFocusAdoption.test.ts::*` — scope-reason: the ValidationDetectionEditor entry is removed as the surface migrates

Remaining files: `App.tsx` (1 btn), `ProjectSelector.tsx` (2 btn / 1 input), `web/src/components/chat/CommandBar.tsx` (1 btn — `RAW_ELEMENT_ALLOWLIST` line 96), `web/src/components/chat/PlanApprovalActions.tsx` (1 textarea — line 191), `ValidationDetectionEditor.tsx` (9 `*_CLS`, 1 btn / 1 input / 1 textarea, `inputFocusAdoption` entry), `auth/LoginPage.tsx` (1 btn / 3 input), `AppErrorBoundary.tsx` (2 btn), chat: `ProviderPicker.tsx` (3 btn), `BranchIndicator.tsx` (3 btn), `AgentPickerDropdown.tsx` (11 `*_CLS`, 4 btn — scope toggle → SegmentedControl per 3.4), `ChatCommandPalette.tsx` (1 btn), `CommandPalette.tsx` (1 input), `ResumeSessionModal.tsx` (1 btn / 2 input), `ActiveAgentIndicator.tsx` (1 btn), `CodeBlockRenderers.tsx` (1 btn), `ToolResultImage.tsx` (1 btn), `ChatInputModelControls.tsx`/`ChatInputToolbar.tsx` (non-composer entries), command-browser: `ToolBrowserModal.tsx` (4 btn), `SkillBrowserModal.tsx` (3 btn), `ToolArgumentForm.tsx` (1 each btn/input/select/textarea), settings: `SettingsOverlay.tsx` (2 btn), `PromptsTemplatesSection.tsx` (1 btn / 1 input), the remaining section inputs (`McpToolsSection.tsx:283` and `ToolApprovalsSection.tsx:141` — both migrate through `ui/Input`, without which the empty input map in 4.10.1 is unreachable), `shared/DiffBlock.tsx`, `shared/MermaidBlock.tsx` (1 btn each), `chat/ToolCallCard.tsx` (1 input + 2 header buttons — **all three migrate**: the expandable-header composite semantics are the sanctioned part, its nested native buttons are not, per the Constraints floor). **The only sanctioned pinned entries are the composer icon buttons** (`ChatInput.tsx`, `ChatInputQueuedFiles.tsx`, `ChatInputPrimaryButton.tsx`, `ChatInputModelControls.tsx` composer instances — moat 05198494). The moat covers **buttons only**: `web/src/components/chat/ChatInput.tsx` also carries a textarea entry (`RAW_ELEMENT_ALLOWLIST` line 190) sitting beside its sanctioned button entry (line 91). That textarea migrates to `ui/Textarea` here; only the button entry survives. FilesTab's nested controls are owned by 4.6.

**Acceptance:**

- 4.10.1 - `RAW_ELEMENT_ALLOWLIST` input and select maps are empty; the textarea map holds only the deferral-covered `WikiAskMode.tsx` entry; the button map contains only the composer-moat entries plus the deferral-covered `WikiAskMode.tsx` entry (see 4.11). file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.10.2 - `CLS_CONSTANT_ALLOWLIST` is empty. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.10.3 - The ValidationDetectionEditor `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.
- 4.10.4 - The composer textarea renders through `ui/Textarea` with the composer look preserved. file: `web/src/components/chat/ChatInput.tsx`.

## P5: BEM Sheet Retirement

`kind: framing`

**Goal**: Every legacy BEM sheet empties into utilities/cva and is deleted, with its `CSS_FILE_ALLOWLIST` entry and guard-test pins. Ceiling lowers with each >200-line batch. Order: cheapest and most isolated first, serialized 5.1 → 5.2 → 5.3 → 5.4 → 5.5 → 5.6 — every retirement rewrites the ratchet ledger and the shared guard pins (coarse-pointer, typography, mobile-chrome), which admit no concurrent writers.

### 5.1 Retire message.css and empty-state.css [category: refactor] (depends: P4)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/MessageItem.tsx::*` — scope-reason: markdown typography utilities are applied at the component as message.css retires
- `web/src/components/chat/styles/message.css`
- `web/src/components/chat/styles/empty-state.css`
- `web/src/components/chat/styles.css`
- `web/src/components/chat/MessageList.tsx::*` — scope-reason: its empty-state classes move to component utilities as the sheet retires
- `web/src/components/shared/MarkdownBody.tsx::*` — scope-reason: becomes the canonical owner of the scoped markdown typography utilities as message.css retires
- `web/src/components/FilesPage.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/FilesTab.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/PlanReviewCard.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/SessionsTabDetail.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/TasksTabDetailPanel.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/skills/SkillContentView.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/activity/wiki/WikiPageReader.tsx::*` — scope-reason: message-content markdown consumer verified with parity as the typography moves to the shared surface
- `web/src/components/chat/__tests__/ToolCallCard.interactive.test.tsx::*` — scope-reason: gains direct-consumer assertions that ToolCallCard DOM and styling are unchanged by the typography relocation
- `web/src/components/activity/ActivityPanelEmpty.tsx::*` — scope-reason: its empty-state classes move to component utilities and its 1.1 sheet import is removed
- `web/src/components/activity/__tests__/ActivityPanelEmpty.test.tsx::*` — scope-reason: source-regex assertions on the retired sheet drop; structure/copy/adoption pins stay
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: empty-state typography pins re-point at the component

`message.css` (205 lines) is pure `.message-content <element>` markdown typography — the cleanest whole-file kill: express it as a scoped set of Tailwind descendant utilities (`[&_h1]:…`) in a constant or cva honoring the ~65–75ch prose cap. **Wrapper-neutral API:** `MarkdownBody` returns a fragment and stays that way — the canonical scoped typography utility/cva is *exported from* `MarkdownBody.tsx` and applied at each host element that today carries `.message-content`, so styling authority lives in one module while every host keeps its own wrapper DOM. The `.message-content` census (2026-08-08, corrected 2026-08-09) counts nine production hosts: `MessageItem.tsx` in chat plus eight non-chat wrappers — `FilesPage.tsx`, `FilesTab.tsx`, `PlanReviewCard.tsx`, `SessionsTabDetail.tsx`, `TasksTabDetailPanel.tsx`, `SkillContentView.tsx`, `TaskDetailEditableCore.tsx`, `WikiPageReader.tsx`. Eight render markdown through the shared `MarkdownBody.tsx`; `TaskDetailEditableCore.tsx`'s `StaticBlock` renders plain text inside its `.message-content` div and adopts the exported utility directly. Direct `MarkdownBody` consumers outside the nine hosts — `ToolCallCard.tsx`, `RichContentBlocks.tsx`, the `chat/Markdown.tsx` alias, and the deferral-covered `WikiAskMode.tsx` (4.11) — are untouched by the utility because it attaches at host elements: direct-consumer tests pin their DOM and styling unchanged. `empty-state.css` (78 lines): `.activity-tab-empty*` becomes a small `ActivityEmptyState` presentational component (already componentized — move classes to utilities inside it); `.chat-empty-state*` (`MessageList.tsx:281-290`) and the orphan `.command-palette-empty` migrate to utilities. Update `ActivityPanelEmpty.test.tsx` (drops its two source-regex assertions on the sheet, keeps structure/copy/adoption pins) and `typographyLadder.test.ts` (empty-state pins re-point at the component). Both `empty-state.css` owner imports — the chat barrel member and the `ActivityPanelEmpty.tsx` side-effect import added in 1.1 — are removed with the sheet.

**Acceptance:**

- 5.1.1 - Both sheets are deleted with allowlist entries dropped. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.1.2 - Message markdown typography renders via utilities with parity. file: `web/src/components/chat/MessageItem.tsx`.
- 5.1.3 - Empty-state guard assertions target the component. test: `web/src/components/activity/__tests__/ActivityPanelEmpty.test.tsx`.
- 5.1.4 - Non-chat markdown surfaces (FilesPage, FilesTab, PlanReviewCard, SessionsTabDetail, TasksTabDetailPanel, SkillContentView, TaskDetailEditableCore, WikiPageReader) keep scoped markdown typography with parity after the sheet retires; MarkdownBody keeps its fragment output, and direct-consumer tests prove ToolCallCard, RichContentBlocks, and WikiAskMode DOM and styling unchanged. file: `web/src/components/shared/MarkdownBody.tsx`.

### 5.2 Retire the chat input family [category: refactor] (depends: 4.10, 5.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/input-base.css`
- `web/src/components/chat/styles/input-composer.css`
- `web/src/components/chat/styles/input-voice.css`
- `web/src/components/chat/styles/input-responsive.css`
- `web/src/components/chat/styles/input-status.css`
- `web/src/components/chat/styles/input.css`
- `web/src/components/chat/styles.css`
- `web/src/styles/accessibility.css`
- `web/src/components/chat/ChatInput.tsx::*` — scope-reason: composer shell and textarea rules land as component utilities with the moat button look preserved as scoped utilities
- `web/src/components/chat/ChatInputToolbar.tsx::*` — scope-reason: toolbar, meta, and notice rules from the composer sheet land as component utilities
- `web/src/components/chat/ChatInputModelControls.tsx::*` — scope-reason: the chat-input-select family rules land as component utilities
- `web/src/components/chat/ChatInputVoiceControls.tsx::*` — scope-reason: voice-mic rules from the voice sheet land as component utilities
- `web/src/components/chat/ChatInputPrimaryButton.tsx::*` — scope-reason: primary-button styling arrives through the hook-authored class strings as the sheet retires
- `web/src/components/chat/useChatInputPrimaryAction.ts::*` — scope-reason: authors the chat-input-primary-button class strings that absorb the sheet's rules
- `web/src/components/chat/useChatInputNarrow.ts::*` — scope-reason: its chat-column container lookup follows the relocated container declaration
- `web/src/components/chat/ChatMainColumn.tsx::*` — scope-reason: hosts the chat-column container declaration that moves to component-owned styling
- `web/src/components/chat/AgentStatusBar.tsx::*` — scope-reason: agent-status-bar rules from the status sheet land as component utilities
- `web/src/components/chat/VoiceStatusBar.tsx::*` — scope-reason: voice-status-bar rules from the voice sheet land as component utilities
- `web/src/components/chat/ChatCommandPalette.tsx::*` — scope-reason: the input-base command-palette dropdown rules land as component utilities
- `web/src/components/chat/ActiveAgentIndicator.tsx::*` — scope-reason: its chat-input-agent-button geometry from input-responsive.css lands as component utilities with the sanctioned composer-persona behavior intact
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: agent/voice status-bar typography pins re-point at components
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/components/chat/__tests__/planApprovalDesign.test.tsx::*` — scope-reason: source assertions on the retired sheets convert to JSX/computed-style assertions

`input-base.css` (398), `input-composer.css` (263), `input-voice.css` (187), `input-responsive.css` (151), `input-status.css` (18), `input.css` barrel (5) — 1,022 lines onto the composer components (`ChatInput`, `ChatInputToolbar`, `ChatInputModelControls`, `ChatInputVoiceControls`, `ChatInputPrimaryButton`, `AgentStatusBar`, `VoiceStatusBar`, `ChatCommandPalette`). All eight are formal Targets, plus the two coupled hooks the census surfaced: `useChatInputPrimaryAction.ts` authors the `chat-input-primary-button` class strings that absorb the sheet's rules, and `useChatInputNarrow.ts` locates the `.chat-column` container declared in `input-composer.css` (line 22), which moves to `ChatMainColumn`-owned styling with the retirement. `ActiveAgentIndicator.tsx` (census addition 2026-08-09) authors `.chat-input-agent-button` from `input-responsive.css:34` — its button geometry migrates to component utilities with the sanctioned persona behavior intact, the `mobileChromeCss.test.ts` pins on that relation (lines 457/474) re-point to the component, and the indicator participates in composer parity. Composer icon buttons keep their purpose-built look (moat) as scoped utilities. Container queries move to `@container` utilities / the components' own scoped styles. The `input-voice.css:176` `animation: none !important` relocates to `web/src/styles/accessibility.css` with a justification comment (reduced-motion class). Guard updates: `mobileChromeCss.test.ts` chat container-query pins and `planApprovalDesign.test.tsx` `.agent-status-bar` source assertions re-point or convert to JSX/computed-style assertions; `coarsePointerTouchTargets` fixture hooks that referenced these sheets move to compiled-Tailwind candidates.

**Acceptance:**

- 5.2.1 - All six input sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.2.2 - Composer renders with visual parity across the capture matrix. behavior: "composer parity" in `web/tests/style-surfaces.spec.ts`.
- 5.2.3 - The single voice `animation: none !important` relocated from `input-voice.css:176` lives in accessibility.css under its own `prefers-reduced-motion` query with rationale, and `IMPORTANT_ALLOWLIST` moves that one count with it. file: `web/src/styles/accessibility.css`.
- 5.2.4 - Computed-style assertions prove reduced-motion suppression and no-preference animation behavior for the relocated voice families (recording, speaking/listening, loading, streaming), and the 1.3 reduced-motion subset passes before and after the move. behavior: "reduced-motion relocation" in `web/tests/style-surfaces.spec.ts`.

### 5.3 Retire layout.css, variables.css, and the chat barrel [category: refactor] (depends: 5.1, 5.2)

`kind: deliverable`

Targets:
- `web/src/components/chat/styles.css`
- `web/src/components/chat/styles/variables.css`
- `web/src/components/chat/ChatPage.tsx::*` — scope-reason: its barrel side-effect import is removed at retirement
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/layout.css`
- `web/src/styles/base.css`
- `web/src/components/chat/CommandBar.tsx::*` — scope-reason: the command-bar rule family lands as component utilities
- `web/src/components/chat/CommandPalette.tsx::*` — scope-reason: the full-screen command-palette rule family lands as component utilities
- `web/src/components/chat/MessageItem.tsx::*` — scope-reason: the layout half of the message-content shell rule lands with the shared markdown typography owner
- `web/src/components/chat/MessageList.tsx::*` — scope-reason: chat-scaled type-scaling consumer restyles via component utilities
- `web/src/components/activity/WatchingTranscript.tsx::*` — scope-reason: chat-scaled type-scaling consumer restyles via component utilities
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: the command-bar typography pin re-points at the component
- `web/src/components/chat/__tests__/planApprovalDesign.test.tsx::*` — scope-reason: the command-bar source assertion re-points as the sheet retires

`layout.css` (468) splits live-vs-dead on the 2026-08-08 selector census. **Migrates:** the `.command-bar*` family → `CommandBar` utilities; the full-screen `.command-palette-*` family → `CommandPalette` utilities; the `.message-content` shell rule (line 74 — the layout half of the cross-sheet duplicate 5.1 resolves) → the shared markdown typography owner applied by `MessageItem`; the `.chat-scaled` type-scaling rules → their two production consumers, `MessageList` and `WatchingTranscript`. **Deleted as dead** (zero component consumers anywhere in `web/src`): `.chat-container`, `.chat-messages`, `.chat-page`, `.chat-main`, the entire `.mobile-chat-drawer*` family, and the `.message`/`.message-user`/`.message-assistant`/`.message-header`/`.message-role*`/`.message-time`/`.message-model-switch` shells — the classification is re-verified and recorded in the deletion commit. `variables.css` (12): its four alias custom properties (`--bg-code`, `--bg-muted`, `--border-color`, `--accent-color`) have **zero consumers** anywhere in `web/src` (verified 2026-08-08) — delete the sheet outright with the `mobileChromeCss` alias-only assertion; nothing inlines and nothing graduates. The barrel `styles.css` (32): the `.tool-code-surface` `!important` rule (beats react-syntax-highlighter's inline style — must survive) relocates to `web/src/styles/base.css` with its #14721 comment; barrel deleted. `IMPORTANT_ALLOWLIST` moves the entry accordingly. `mobileChromeCss` `.command-bar` pins re-point; `typographyLadder` `.command-bar-btn` pin re-points; `planApprovalDesign` `.command-bar` assertion re-points. Naming hazard resolved: the chat-input dropdown formerly `.command-palette` (input-base) and the modal `.command-palette-*` (layout) end as component-scoped utilities, killing the collision. **Precondition (why this depends on 5.1 AND 5.2):** at deletion time the barrel must import only `layout.css` and `variables.css` — 5.1 removes its retained empty-state member and 5.2 removes its input-family chain; deleting the barrel earlier leaves `ChatPage.tsx` importing a file whose members still exist.

**Acceptance:**

- 5.3.1 - `layout.css`, `variables.css`, and `chat/styles.css` are deleted with allowlist entries dropped. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.3.2 - The tool-code-surface override survives in base.css and tool cards render flat code backgrounds. file: `web/src/styles/base.css`.
- 5.3.3 - Import-order-dependent behavior is gone from chat styling (no cross-sheet duplicate selectors remain). behavior: "no duplicate selectors" in `web/src/components/chat/ChatPage.tsx`.

### 5.4 Retire sessions-tab.css and activity-panel.css [category: refactor] (depends: 4.2, 4.3, 4.9, 5.3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/activity-panel.css`
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: panel chrome utilities land on the component as its sheet retires
- `web/src/components/activity/SessionsTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTab.tsx::*` — scope-reason: its sessions-tab.css side-effect import (owner since 1.1) is removed at retirement
- `web/src/components/activity/SessionsTab.helpers.tsx::*` — scope-reason: remaining session-entry styling lands as utilities on the helpers
- `web/src/components/activity/TasksTabToolbar.tsx::*` — scope-reason: the single .activity-filter-button authoring site lands here
- `web/src/components/activity/ActivityActionsContext.tsx::*` — scope-reason: activity-panel action-button and header rules it consumes land as component utilities
- `web/src/components/activity/ActivityRowStatusDot.tsx::*` — scope-reason: activity-row-status-dot rules and pulse keyframes it consumes land as component utilities
- `web/src/components/activity/__tests__/ActivityActionButtons.test.tsx::*` — scope-reason: action-button assertions re-point from sheet selectors to component assertions
- `web/src/components/activity/__tests__/ActivityRowStatusDot.test.tsx::*` — scope-reason: status-dot assertions re-point from sheet selectors to component assertions
- `web/src/components/activity/mcp/McpDetailPanel.tsx::*` — scope-reason: its activity-panel action-button and status-bar classes migrate here before the sheet deletes; its mcp-tab.css selectors stay with 5.5
- `web/src/components/activity/mcp/__tests__/McpDetailPanel.test.tsx::*` — scope-reason: detail-panel action-button assertions re-point from sheet selectors to component assertions
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: activity-row and status-bar typography pins re-point to components
- `web/src/components/chat/__tests__/planApprovalDesign.test.tsx::*` — scope-reason: the activity-panel-tabs source assertion re-points as the sheet retires

`activity-panel.css` (622): `.activity-panel*` shell/tabs/toolbar/status-bar/mobile chrome → utilities on `ActivityPanel`/`ActivityActionsContext`; `.activity-panel-tab-strip` → TabBar; `.activity-chip` rules die here with their sheet (adopters migrated in 4.2/4.3/4.8); `.activity-filter-button` consolidates to a single authoring site (Button variant + utilities; its `task-execution.css` and `rules-tab.css` fragments die with those sheets). `sessions-tab.css` (561): `.session-entry*` and remaining rules → utilities on `SessionsTabList`/helpers. Direct-consumer dispositions: `ActivityActionsContext.tsx` (`.activity-panel-actions-slot`, `.activity-panel-action-btn*`, header-segmented) and `ActivityRowStatusDot.tsx` (`.activity-row-status-dot*` incl. the pulse keyframes) keep their components and behavior — only styling authority moves to component utilities — and their direct tests (`ActivityActionButtons.test.tsx`, `ActivityRowStatusDot.test.tsx`) re-point from sheet selectors to component assertions. `McpDetailPanel.tsx` splits across two deliverables by selector family: its `.activity-panel-action-btn*` and status-bar classes migrate **here**, before this sheet deletes (with `McpDetailPanel.test.tsx` re-pointed), while its `mcp-tab.css` selectors stay with 5.5 — so the 5.4→5.5 serialization leaves no window where the detail panel's action buttons are unstyled. Dependency rationale: 4.2 and 4.3 own the agent/pipeline `.activity-chip` adopters and 4.9 carries 4.8's thirteen list/detail adopters transitively, so no adopter outlives this sheet. Guard updates: `mobileChromeCss` activity-toolbar pins, `typographyLadder` `.activity-row-*`/status-bar pins re-point to components, `planApprovalDesign` `.activity-panel-tabs` assertion, `coarsePointerTouchTargets` hooks (`.activity-panel-mobile-menu__item`) move to compiled utilities.

**Acceptance:**

- 5.4.1 - Both sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.4.2 - `.activity-filter-button` has exactly one authoring site. behavior: "single filter-button authoring site" in `web/src/components/activity/TasksTabToolbar.tsx`.
- 5.4.3 - Typography-ladder pins assert against components. test: `web/src/components/activity/__tests__/typographyLadder.test.ts`.

### 5.5 Retire the small activity tab sheets [category: refactor] (depends: 4.3, 4.6, 4.8, 5.4)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: remaining rules-tab styling moves to the shared filter panel/utilities and its rules-tab.css owner import is removed
- `web/src/components/chat/styles/files-tab.css`
- `web/src/components/chat/styles/mcp-tab.css`
- `web/src/components/chat/styles/rules-tab.css`
- `web/src/components/chat/styles/cron-tab.css`
- `web/src/components/chat/styles/traces-tab.css`
- `web/src/components/chat/styles/pipelines-tab.css`
- `web/src/components/activity/skills/SkillsTab.css`
- `web/src/components/activity/SkillsTab.tsx::*` — scope-reason: its SkillsTab.css and rules-tab.css owner imports are removed as its styling moves to the shared filter panel/utilities
- `web/src/components/activity/FilesTab.tsx::*` — scope-reason: its files-tab.css side-effect import (owner since 1.1) is removed at retirement
- `web/src/components/activity/ActivityMcpTab.tsx::*` — scope-reason: its mcp-tab.css side-effect import is removed at retirement
- `web/src/components/activity/CronTab.tsx::*` — scope-reason: its cron-tab.css side-effect import is removed at retirement
- `web/src/components/activity/TracesTab.tsx::*` — scope-reason: its traces-tab.css side-effect import is removed at retirement
- `web/src/components/activity/PipelinesTab.tsx::*` — scope-reason: its pipelines-tab.css side-effect import is removed at retirement
- `web/src/components/activity/integrations/IntegrationsFilterPanel.tsx::*` — scope-reason: its rules-tab.css side-effect import is removed as the shared filter panel lands
- `web/src/components/FilesPage.tsx::*` — scope-reason: file-viewer selector consumers move to utilities as files-tab.css retires
- `web/src/components/activity/FileChangesTab.tsx::*` — scope-reason: files-tab selector consumers move to utilities at retirement
- `web/src/components/activity/mcp/McpDetailPanel.tsx::*` — scope-reason: mcp-tab selector consumers move to utilities at retirement; its activity-panel action-button family already migrated in 5.4
- `web/src/components/activity/mcp/McpServerFields.tsx::*` — scope-reason: activity-mcp-detail selector consumer moves to utilities at retirement
- `web/src/components/activity/mcp/mcpIcons.tsx::*` — scope-reason: activity-mcp-row-chevron selector consumer moves to utilities at retirement
- `web/src/components/activity/rules/RulesDetailPanel.tsx::*` — scope-reason: rules-detail and rules-readonly selector consumer moves to utilities at retirement
- `web/src/components/activity/rules/RulesTabList.tsx::*` — scope-reason: rules-tab list selector consumer moves to utilities at retirement
- `web/src/components/activity/rules/RulesYamlView.tsx::*` — scope-reason: rules-tab yaml selector consumer moves to utilities at retirement
- `web/src/components/activity/FilterPrimitives.tsx::*` — scope-reason: gains the presentational InlineFilterPanel that replaces the shared activity-filter-panel rules
- `web/src/components/activity/__tests__/FilterPrimitives.test.tsx`
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: file-tree and cron typography pins re-point to components

`files-tab.css` (300), `mcp-tab.css` (239), `rules-tab.css` (212 incl. `.activity-filter-panel` used by Skills + Integrations — becomes the presentational **`InlineFilterPanel`** added to `FilterPrimitives.tsx`: panel shell and field rows only, adopted by RulesTab, SkillsTab, and IntegrationsFilterPanel with direct render coverage in `FilterPrimitives.test.tsx`; the 4.9 rule stands — controllers keep their semantics and stay put), `cron-tab.css` (61), `traces-tab.css` (36), `pipelines-tab.css` (35), plus `activity/skills/SkillsTab.css` (3). Import owners updated per sheet (each was relocated to its owning component in 1.1; the owning component's side-effect import is removed as its sheet dies). `rules-tab.css` has **three** owner imports since 1.1 — RulesTab, SkillsTab, and IntegrationsFilterPanel — all removed here as the shared filter panel lands. Selector consumers whose styling moves to utilities (2026-08-08 census): FilesTab/FilesPage/FileChangesTab, McpDetailPanel/ActivityMcpTab/McpServerFields (`.activity-mcp-detail*`)/mcpIcons (`.activity-mcp-row-chevron`), RulesTab/RulesDetailPanel (`.rules-detail*`, `.rules-readonly*`)/RulesTabList/RulesYamlView, CronTab, TracesTab, PipelinesTab. Guard updates: `typographyLadder` file-tree and cron pins re-point; `coarsePointerTouchTargets` hooks from these sheets move to utilities.

**Acceptance:**

- 5.5.1 - All seven sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.5.2 - The shared inline filter panel serves Rules, Skills, and Integrations from the presentational InlineFilterPanel in one implementation, with direct render coverage. file: `web/src/components/activity/FilterPrimitives.tsx`.

### 5.6 Retire task-execution.css and task-detail.css [category: refactor] (depends: 4.7, 4.8, 5.5)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/tasks/task-execution.css`
- `web/src/components/activity/taskdetail/task-detail.css`
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TasksTabDetailPanel.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TasksTab.tsx::*` — scope-reason: activity-tasks pane and toolbar selector consumer restyles via component utilities
- `web/src/components/activity/TaskCloseDialog.tsx::*` — scope-reason: activity-task-close selector consumer restyles via component utilities
- `web/src/components/activity/TaskTreeRow.tsx::*` — scope-reason: activity-task-row selector consumer restyles via component utilities
- `web/src/components/activity/TaskFieldEditors.tsx::*` — scope-reason: task inline-edit selector consumer restyles via component utilities
- `web/src/components/activity/taskdetail/TaskDetailHeader.tsx::*` — scope-reason: task-detail header selector consumer restyles via component utilities
- `web/src/components/activity/taskdetail/TaskDetailEditableCore.tsx::*` — scope-reason: task-detail editable-core selector consumer restyles via component utilities
- `web/src/components/activity/taskdetail/TaskDetailKV.tsx::*` — scope-reason: task-detail KV selector consumer restyles via component utilities
- `web/src/components/activity/taskdetail/TaskDetailRelationships.tsx::*` — scope-reason: task-detail relationships selector consumer restyles via component utilities
- `web/src/components/activity/taskdetail/TaskDetailStatusLine.tsx::*` — scope-reason: task-detail statusline selector consumer restyles via component utilities
- `web/src/components/activity/taskdetail/TaskDetailTrace.tsx::*` — scope-reason: task-detail trace selector consumer restyles via component utilities
- `web/src/components/activity/__tests__/TasksTab.test.tsx::*` — scope-reason: activity-tasks-pane and detail-header class queries re-point to component assertions
- `web/src/components/activity/__tests__/TasksTab.rowOrder.test.tsx::*` — scope-reason: activity-task-row class queries re-point to component assertions
- `web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx::*` — scope-reason: activity-task-detail class queries re-point to component assertions
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/components/activity/__tests__/typographyLadder.test.ts::*` — scope-reason: task typography pins stay component-side as the sheets retire
- `web/src/components/chat/__tests__/planApprovalDesign.test.tsx::*` — scope-reason: retired-sheet references re-point as the sheets die

`task-execution.css` (738 — largest sheet; import owner `TaskBadges.tsx:4` removed with it; `task-detail.css` import owner `TasksTabDetailPanel.tsx:1` likewise): `.chip` block already dead (3.1); `.activity-task-*` rows/panes/toolbars → utilities on TaskTreeRow/TasksTab components; `.activity-filter-*` fragments consolidated per 5.4. `activity/taskdetail/task-detail.css` (346): detail header/KV/relationships → utilities; `.task-inline-edit--select` already migrated (4.7). Full consumer census (2026-08-08): the raw-control migrations for `TaskFieldEditors`, `TaskDetailKV`, `TaskDetailRelationships`, and `TasksTabDetailPanel` are owned earlier by 4.7/4.8 (explicit there; 5.6 depends on both), while their class hooks — plus `TasksTab` panes/toolbars, `TaskCloseDialog` (`.activity-task-close*`), `TaskTreeRow` (`.activity-task-row*`), and the full task-detail family (`TaskDetailHeader`, `TaskDetailEditableCore`, `TaskDetailKV`, `TaskDetailRelationships`, `TaskDetailStatusLine`, `TaskDetailTrace`) — land here as component utilities. Class-sensitive tests re-point with them: `TasksTab.test.tsx` (`.activity-tasks-pane`, detail-header queries), `TasksTab.rowOrder.test.tsx` (`.activity-task-row-*`), `TasksTabDetailPanel.test.tsx` (`.activity-task-detail-*`). Guard updates: `typographyLadder` task pins and `PRIORITY_TEXT_WEIGHTS` stay component-side; `coarsePointerTouchTargets` `.task-more-btn`/`.activity-task-row-toggle`/`.activity-task-detail-edit-error__dismiss` hooks move to compiled utilities; `mobileChromeCss`/`planApprovalDesign` references re-point.

**Acceptance:**

- 5.6.1 - Both sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.6.2 - Coarse-pointer 44px promotion for task rows is asserted via compiled utilities. test: `web/src/__tests__/coarsePointerTouchTargets.test.ts`.

## P6: Cascade Flip

`kind: framing`

**Goal**: Tailwind utilities no longer need `important: true` because the BEM cascade they fought is gone.

### 6.1 Remove important:true behind the screenshot gate [category: config] (depends: P5)

`kind: deliverable`

Targets:
- `docs/guides/frontend-style-guide.md`
- `web/tailwind.config.ts::*` — scope-reason: the important flag is removed and the file becomes content-scanning only

Highest-risk step. Preconditions met by P5: surviving CSS is enumerable (token infra + hook sheets + settings-overlay.css). Procedure:

1. Run the 1.3 capture matrix (before) — a finalized immutable labeled run including the grayscale and reduced-motion subsets.
2. Remove `important: true` from `web/tailwind.config.ts` (file becomes content-scanning only).
3. Verify the six surviving intentional `!important` declarations still hold — all six beat an inline style or serve reduced-motion, so none of them depends on the flag. By this point they sit in two files: `base.css` carries the four-declaration reduced-motion block plus the `.tool-code-surface` background relocated in 5.3, and `accessibility.css` carries the voice `animation: none` relocated in 5.2. That is the same six the plan opens with (`chat/styles.css` 1 + `input-voice.css` 1 + `base.css` 4), redistributed from three files to two by sheet retirement — no declaration is added or removed by this phase.
4. Run the matrix (after) as a second finalized immutable labeled run; review every pair; fix any regression at its source (specificity at the component, never a new `!important`), then re-run until pairs match exactly.
5. Sweep for utility-vs-hook-sheet conflicts: the remaining hook sheets (`app-shell.css`, `segmented-control.css`, `dropdown-caret.css`, `settings-overlay.css`) are the only stylesheets that can now out-specificity utilities — audit their selectors against utility-bearing elements they touch.

Update `docs/guides/frontend-style-guide.md` anti-patterns wording ("Tailwind utilities are already configured with `important: true`" — no longer true).

**Acceptance:**

- 6.1.1 - `important: true` is gone. file: `web/tailwind.config.ts`.
- 6.1.2 - Before/after capture pairs across the full matrix show exact parity against the immutable post-1.4 baseline; 1.4 remains the sole rendered-output exemption plan-wide. behavior: "matrix parity review" in `web/tests/style-surfaces.spec.ts`.
- 6.1.3 - The style guide reflects the new cascade. file: `docs/guides/frontend-style-guide.md`.

## P7: Hook Sheets, Overlay, and Load Order

`kind: framing`

**Goal**: The last non-infra stylesheets fold into components; `main.tsx` carries no side-effect CSS beyond fonts and `index.css`. The phase runs serialized 7.1 → 7.2 → 7.3 → 7.4: all four rewrite the root import list, the mobile-chrome guard pins, and the ratchet ledger, which admit no concurrent writers.

### 7.1 Retire segmented-control.css and dropdown-caret.css [category: refactor] (depends: 6.1)

`kind: deliverable`

Targets:
- `web/src/components/ui/SegmentedControl.tsx::*` — scope-reason: the primitive absorbs its stylesheet as utilities/cva while keeping the control-height contract
- `web/src/main.tsx::*` — scope-reason: both hook-sheet side-effect imports are removed
- `web/src/styles/segmented-control.css`
- `web/src/styles/dropdown-caret.css`
- `web/src/components/ui/DropdownCaret.tsx::*` — scope-reason: the primitive absorbs its stylesheet as utilities
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: both sheets' CSS_FILE_ALLOWLIST entries drop in the same commit
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: the sheet-order assertion retires and segmented-control pins convert to component assertions

38 lines total move into their owning primitives: `.segmented-control*` rules → `SegmentedControl.tsx` utilities/cva (keeping the `--control-row-height` contract and the `mobileChromeCss` option-padding pins as component assertions); `.dropdown-caret` → `DropdownCaret.tsx`. Drop both `main.tsx` imports; update `mobileChromeCss.test.ts` (its segmented-control-before-app-shell sheet-order assertion retires; segmented-control pins convert to component assertions).

**Acceptance:**

- 7.1.1 - Both sheets are deleted; the primitives self-style. file: `web/src/components/ui/SegmentedControl.tsx`.
- 7.1.2 - Allowlist entries dropped; `main.tsx` imports removed. file: `web/src/main.tsx`.

### 7.2 Retire app-shell.css [category: refactor] (depends: 7.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/styles/app-shell.css`
- `web/src/main.tsx::*` — scope-reason: the app-shell.css side-effect import is removed
- `web/src/App.tsx::*` — scope-reason: the header cluster (app-header/app-brand/app-header-actions/app-health-badge classes) restyles via utilities/cva
- `web/src/components/ThemeToggle.tsx::*` — scope-reason: the app-theme-toggle sizing hook moves to component-owned styling
- `web/src/components/ProjectSelector.tsx::*` — scope-reason: the project-selector responsive-swap classes move to component-owned styling
- `.impeccable.md`
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: app-header pins convert to JSX/component assertions

157 lines of header-cluster hooks (`app-*` classes sizing the theme-toggle/cog/logout cluster, project-selector responsive swap, health badge). The named consumers: the `App.tsx` header cluster, `ThemeToggle.tsx` (`.app-theme-toggle`), and `ProjectSelector.tsx` (`.project-selector*` responsive swap). Express as utilities/cva on those components while preserving the canonical-cluster contract from `.impeccable.md` (equal icon widths via `size="icon"`, `--status-bar-control-height` row, coarse-pointer hit-area expansion, mobile collapse to a single settings entry). `mobileChromeCss` app-header pins convert to JSX/component assertions. **The `.impeccable.md` app-header contract updates in this same deliverable via the impeccable skill's teach mode** — the canonical-cluster clause stops referencing `app-shell.css` hook selectors and points at component-owned styling, so contract and implementation never disagree between phases. The style guide's "sanctioned exception" paragraph for hook sheets is rewritten in 8.2.

**Acceptance:**

- 7.2.1 - `app-shell.css` is deleted with its allowlist entry; header renders with parity in both tiers. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 7.2.2 - Header pins live as component assertions. test: `web/src/__tests__/mobileChromeCss.test.ts`.
- 7.2.3 - The `.impeccable.md` app-header/canonical-cluster clause references component-owned styling, updated via teach mode in this deliverable. behavior: "app-header contract matches shipped architecture" in `.impeccable.md`.

### 7.3 Retire settings-overlay.css [category: refactor] (depends: 6.1, 7.2)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/styles/settings-overlay.css`
- `web/src/components/settings/SettingsOverlay.tsx::*` — scope-reason: shell, section, and specialty-editor rules land as utilities on the overlay components as the sheet retires
- `web/src/components/settings/WorkflowVariablesEditor.tsx::*` — scope-reason: the settings-variables* rules it consumes land as component utilities
- `web/src/components/settings/fields/StringListField.tsx::*` — scope-reason: overlay field/row rules it consumes land as component utilities
- `web/src/components/settings/fields/KeyValueMapField.tsx::*` — scope-reason: overlay field/row rules it consumes land as component utilities
- `web/src/components/settings/fields/TypedListField.tsx::*` — scope-reason: overlay field/row rules it consumes land as component utilities
- `web/src/components/settings/fields/BoundedSelectField.tsx::*` — scope-reason: overlay field/row rules it consumes land as component utilities
- `web/src/components/settings/sections/AppearanceSection.tsx::*` — scope-reason: overlay section rules it consumes (incl. appearance-font-size*) land as component utilities
- `web/src/components/settings/sections/AutomationWorkflowsSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/ChatVoiceSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/IntegrationsHooksSection.tsx::*` — scope-reason: overlay section rules it consumes (incl. settings-hubs-field*) land as component utilities
- `web/src/components/settings/sections/McpToolsSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/ObservabilitySection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/ProjectsSessionsSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/PromptsTemplatesSection.tsx::*` — scope-reason: overlay section rules it consumes (incl. settings-prompt-row) land as component utilities
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::*` — scope-reason: overlay section rules it consumes (incl. settings-endpoint-editor) land as component utilities
- `web/src/components/settings/sections/RuntimeInfrastructureSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/SecretsAuthSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/ToolApprovalsSection.tsx::*` — scope-reason: overlay section rules it consumes land as component utilities
- `web/src/components/settings/sections/SettingsSection.tsx::*` — scope-reason: the shared renderer that emits the settings-section and settings-subsection shells; its rules land as component utilities
- `web/src/components/settings/sections/configFields.tsx::*` — scope-reason: the shared field renderer consuming overlay field/row rules; its class hooks land as component utilities
- `web/src/components/settings/sections/__tests__/SettingsSection.test.tsx`
- `web/src/components/settings/__tests__/SettingsOverlay.test.tsx::*` — scope-reason: its backdrop assertion queries the retiring settings-overlay-shell__backdrop selector and re-points to a stable semantic/data seam
- `web/src/main.tsx::*` — scope-reason: the settings-overlay.css side-effect import is removed
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change

712 lines across `SettingsOverlay.tsx`, `WorkflowVariablesEditor.tsx`, `settings/fields/*`, the 13 section components, and the two shared renderers that actually emit the section shells — `SettingsSection.tsx` (`.settings-section*`, `.settings-subsection*`) and `configFields.tsx` (overlay field/row hooks) — whose class hooks land as component utilities with a direct render pin in the new `SettingsSection.test.tsx`. Much of the field/row styling is already superseded by FormField adoption (3.3) — delete superseded rules first, then migrate the shell (`.settings-overlay-shell*`), sections (`.settings-section*`, `.settings-subsection*`), and specialty editors (`.settings-variables*`, `.settings-endpoint-editor`, `.settings-hubs-field*`, `.settings-prompt-row`, `.appearance-font-size*`) to utilities. `SettingsOverlay.test.tsx` queries `.settings-overlay-shell__backdrop` (line 166) — that assertion re-points to a stable semantic/data seam as the selector retires. Work in 2–3 commits (shell → sections → specialty) to stay bisectable. Drop the `main.tsx` import; lower ceiling (>200 lines).

**Acceptance:**

- 7.3.1 - `settings-overlay.css` is deleted with its allowlist entry and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 7.3.2 - Settings overlay renders with parity across the capture matrix for every registry-derived section cell — one per live `SETTINGS_SECTIONS` entry with seeded specialty-editor states (variables, endpoint, hubs, prompt-row) — as defined in 1.3. behavior: "settings overlay parity" in `web/tests/style-surfaces.spec.ts`.
- 7.3.3 - The shared renderers carry direct render pins: section/subsection shells in `SettingsSection.test.tsx` and `configFields.tsx` row/field rendering, plus the SettingsOverlay backdrop assertion re-pointed to a stable semantic seam. test: `web/src/components/settings/sections/__tests__/SettingsSection.test.tsx`.

### 7.4 Load-order rationalization [category: refactor] (depends: 7.2, 7.3)

`kind: deliverable`

Targets:
- `web/src/main.tsx::*` — scope-reason: ends with exactly the two font imports plus index.css
- `web/src/styles/index.css`
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: pins the final import list and index.css directive order deliberately

End state: `main.tsx` imports the two font packages and `./styles/index.css` only; `index.css` owns the full `@import` chain (`tailwindcss`, `@config`, `tailwind-theme`, `tokens`, `base`, `markdown`, `accessibility`). Update `mobileChromeCss.test.ts` to pin the final import list and `index.css` directive order deliberately (the pins become simpler, and intentional). **Precondition (why this depends on 7.2 AND 7.3):** the exactly-three-imports acceptance is only reachable after 7.1/7.2 remove the two hook sheets and the app-shell sheet and 7.3 removes the settings-overlay sheet (the legacy settings sheet already left in 2.1) — at entry, `main.tsx` must carry no side-effect CSS import other than `index.css`.

**Acceptance:**

- 7.4.1 - `main.tsx` has exactly three style-bearing imports (two fonts + index.css). file: `web/src/main.tsx`.
- 7.4.2 - The import pins assert the final order. test: `web/src/__tests__/mobileChromeCss.test.ts`.

## P8: Ratchet Endgame

`kind: framing`

**Goal**: The ratchet stops being a ledger and becomes a set of bans.

### 8.1 Simplify the ratchet to pure bans [category: test] (depends: P7)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/__tests__/styleRatchet.test.ts::*` — scope-reason: ratchet mechanics simplify to ban-plus-pinned-floor while keeping the target-branch parser

- `CLS_CONSTANT_ALLOWLIST = {}` and `BTN_CLASS_ALLOWLIST = {}` — pure bans.
- `RAW_ELEMENT_ALLOWLIST` reduces to the pinned sanctioned floor: **the composer icon-button entries only** (`ChatInput.tsx`, `ChatInputQueuedFiles.tsx`, `ChatInputPrimaryButton.tsx`, `ChatInputModelControls.tsx` — each comment-linked to moat 05198494), plus the deferral-covered `WikiAskMode.tsx` entries comment-linked to the 4.11 deferral (#19672) and removed when that surface ships. FilesTab and ToolCallCard carry no entries — their composite semantics are guarded structurally, and their nested native controls migrated in 4.6/4.10. The test's remedy strings note additions require an explicit moat.
- `IMPORTANT_ALLOWLIST` reduces to exactly two infra-sheet entries carrying the same six declarations it records today: `src/styles/base.css: 5` (the four-declaration reduced-motion block plus the `.tool-code-surface` background relocated in 5.3) and `src/styles/accessibility.css: 1` (the voice `animation: none` relocated in 5.2). The `chat/styles.css` and `input-voice.css` entries are removed as those sheets retire, and each surviving entry gets a justification comment naming what inline style or media query it beats.
- `CSS_FILE_ALLOWLIST` pins exactly the six infra sheets: `index.css`, `tokens.css`, `tailwind-theme.css`, `base.css`, `markdown.css`, `accessibility.css`.
- `CSS_TOTAL_LINE_CEILING` is replaced by an **exact-pin assertion**: the scanner total must equal the recorded final infra total exactly, and `CSS_LINE_TIGHTEN_SLACK` is deleted. Any infra CSS change updates the pin consciously in the same commit (a comment explains the policy) — matching the bidirectional-exact philosophy of the element census; the ceiling stops existing as a burn-down metric.
- Simplify `styleRatchet.test.ts` mechanics where the allowlist shape allows (stale-entry loops over empty maps, target-branch parser still required for the pinned floors); keep `parseAllowlistSnapshot` compatibility.

**Acceptance:**

- 8.1.1 - Allowlists are empty or pinned floors with moat-linked comments. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 8.1.2 - The ratchet test enforces the end state and passes. test: `web/src/__tests__/styleRatchet.test.ts`.

### 8.2 Update the style guide and design contract [category: docs] (depends: P7)

`kind: deliverable`

Targets:
- `docs/guides/frontend-style-guide.md`
- `.impeccable.md`

- Rewrite `docs/guides/frontend-style-guide.md`: ui/ inventory gains Chip, Card, FormField, **NativeSelect**, **Textarea**, TabBar — documenting the two sanctioned select paths (Radix `ui/Select` for toolbar/picker, `ui/NativeSelect` via `SelectField` for forms); the Legacy CSS Files section becomes the end-state contract (six infra sheets, everything else utilities/cva); the hook-sheet sanctioned exception is removed; the Style Debt Ratchet section documents the ban-plus-floor model with the exact-pin line total; anti-pattern wording updated post-flip.
- Update `.impeccable.md` Canonical Components via the impeccable skill's teach mode: the ui/ inventory and select-path rule above land in the contract; the app-header clause was already updated in 7.2 (this pass verifies it still matches and finishes any remaining segmented-control/component-owned styling references). This file is edited through the skill, per project rule.

**Acceptance:**

- 8.2.1 - The style guide documents the end state. file: `docs/guides/frontend-style-guide.md`.
- 8.2.2 - The design contract's component references match the shipped architecture. behavior: "Canonical Components reflect component-owned styling" in `.impeccable.md`.

## V2 End-to-End Verification

`kind: verification`

- `cd web && npm run test && npm run type-check && npm run lint && npm run lint:tokens` — green at every phase boundary; the ratchet proves recorded debt is exact at each step and the ban-state at the end.
- Playwright matrix (`web/tests/style-surfaces.spec.ts`): before/after parity review at 1.4 (re-baseline, not parity), 5.2, 5.4, 6.1, 7.2, 7.3 minimum; full-matrix final pass across every manifest entry (24 at authoring; count asserted against the live tab registry) × dark/light × fine/coarse × three reference viewports, every entry asserting its visible checkpoint, in immutable labeled runs with per-PNG hashes.
- Coarse-pointer activation spec: `cd web && npx playwright test coarse-pointer-hit-areas.spec.ts` (`web/tests/coarse-pointer-hit-areas.spec.ts`) — green at the 3.3 boundary and in the final pass.
- Grayscale subset from the manifest (deutan contract): success/error distinguishable without hue on state-bearing rows in both themes.
- Responsive tier conformance: `.impeccable.md` tier model holds in the shipped code — 767/768 boundary identical in JS and CSS, height≤500px clause live, no surviving 430px or 480px threshold.
- CI target-branch ratchet check green on the PR (`STYLE_RATCHET_TARGET_REF` path — local green is insufficient evidence).
- Guard suite intact and meaningful: `mobileChromeCss`, `coarsePointerTouchTargets` (Button ladder unchanged), `typographyLadder`, `cssTokenIntegrity`, `inputFocusAdoption` (empty), `planApprovalDesign`, `ActivityPanelEmpty`, `ActivityRowStatusDot` all pass against the consolidated architecture.

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: `bc949091-edef-4ebf-b4c9-d8b4d94c6937`
- enhancer_session: `5d3bad49-38b8-4e42-ae22-a4a17d9bcb17`
- converged: false
- suggestions_presented: E1, E2, E3, E4, E5
- accepted: E1, E2, E3, E4 (as amended), E5
- declined: none
- resolution_notes: E1 assigned the five unowned `RAW_ELEMENT_ALLOWLIST` entries to sweeps (PipelinesTab → 4.3, FilterPrimitives → 4.9, CommandBar + PlanApprovalActions + the ChatInput textarea → 4.10) and sharpened 4.10.1 to per-map emptiness; all five entries were verified present in the live allowlist before adoption. E2 added `ui/NativeSelect.tsx` to 3.3 and resolved the self-contradiction in the select rule. E3 replaced generic primitive-coverage acceptance with contract-specific a11y assertions (3.1.4, 3.2.4, 3.3.5, 3.4.3). E5 turned 1.3's surface list into a surface-scenario manifest with per-entry visible checkpoints, determinism controls, focused/open and overflow states, and a grayscale subset; corrected the surface count from "~12" to 17. E4 was accepted with an amendment: the enhancer classified the responsive drift as `lens: bigger` / `category: scope` and proposed a mechanism-silent "single mobile-tier definition," but `.impeccable.md` "Responsive Tiers (Product UI)" already settles the tier model and mandates that the threshold live in a single **token** consumed by both CSS and `useIsMobile`. The finding is therefore contract conformance, not new scope, and the deliverable was written as a theme-layer token hoist (new 1.4) rather than corrected hardcodes. Drift was verified live before adoption: `useIsMobile.ts:7-9` correct, `platform.ts:13` independently hardcoded, eight sheets inclusive at 768px, `app-shell.css:145` 430px, `files-tab.css:97` 480px, no height clause anywhere. 1.4 is the sole deliverable exempt from visual parity; Constraints records the carve-out and makes its output the baseline for everything after.

**Round 1** `kind: verification`

- reviewer_run: `779f4e6f-9eb1-40dc-873d-063c5df1f5f5`
- reviewer_session: `b3c952b9-d8f0-499a-a157-21f613115f57` (#10331)
- verdict: needs_review
- findings:
- WS2-R1-F01 blocking gobby-format — P3 target inventories incomplete
- WS2-R1-F02 blocking gobby-format — P4 sweep targets omit live allowlist owners
- WS2-R1-F03 blocking gobby-format — retirement targets omit sheets, import owners, guards
- WS2-R1-F04 blocking weak-testability — settings retirement lacks control/test disposition
- WS2-R1-F05 blocking unhandled-edge — responsive-tier runtime semantics split across two predicates
- WS2-R1-F06 blocking missing-requirement — no executable 44×44 contract per primitive
- WS2-R1-F07 blocking weak-testability — capture matrix omits migration families
- WS2-R1-F08 blocking weak-testability — capture artifacts not immutable/isolated
- WS2-R1-F09 blocking weak-testability — reduced-motion relocation unverified as rendered behavior
- WS2-R1-F10 blocking bad-sequencing — deletion deliverables missing import-removal dependencies
- WS2-R1-F11 blocking traceability — 6.1 created a second visual-change exemption
- WS2-R1-F12 blocking bad-sequencing — app-shell deletion and design-contract update non-atomic
- WS2-R1-F13 blocking traceability — NativeSelect missing from primitive counts and 8.2 inventory
- WS2-R1-F14 blocking missing-requirement — sanctioned floor mis-covered nested native controls
- WS2-R1-F15 blocking missing-requirement — four plan-level branches left to implementation time
- resolution_notes: All 15 findings accepted by the user (F05 with one amendment: the demanded SSR test dimension is encoded as a matchMedia-absent environment guard — the app is a browser-rendered SPA in both dev and installed modes, with no server render path). Repairs applied: P3/P4/P5/P7 target inventories completed from the live allowlist census and import graph (109 files, 164 entries, 92 guard-test references); 2.1 gained the full control/test disposition map including the ported aria-pressed assertion and the reset-to-defaults gap; 1.4 gained the single-predicate rule, renamed device heuristics, and boundary tests; 3.3 gained the per-primitive 44×44 invisible-hit-area contract with computed-box tests; 1.3 was rebuilt against the live 16-tab registry (24 entries, count asserted in-spec) with immutable labeled runs, per-PNG hashes, readiness callbacks, a reduced-motion subset, and representative mappings for the unmounted CodeGraphExplorer/AgentPortfolioPage and the hidden Traces tab; 5.3 and 7.4 gained their missing dependencies with explicit preconditions; 6.1.2 now requires exact parity (the reviewed-intended-fixes alternative is deleted); the app-header contract update moved into 7.2; NativeSelect is reconciled across the P3 goal, 8.2, and the style guide (two sanctioned select paths); the sanctioned raw floor is reduced to the composer moat plus the 4.11 deferral with FilesTab (4.6) and ToolCallCard (4.10) nested controls migration-owned; and the four open branches are resolved with verified evidence — no Chip size prop (all four status-chip families geometrically identical; agents tag-inputs excluded as a different species), SidebarPanel retired unconditionally (sole consumer verified), variables.css deleted outright (zero consumers of its four aliases), and the line ceiling replaced by an exact-pin assertion with the slack constant deleted.

```json plan-review-round
{"evidence_id":"2af81d5c-43ce-4ed3-a138-621ddfc87b29","plan_hash":"8bd2bb7f4e827cecfda23d18f964353a11c8afaed8d7f908858875b6285fbc1d","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"cef0c10b5a480813af29166dc569432c4ea51b9bc854e38f4ef4c1a1e2a859f7","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":15,"total":22},"evidence_id":"2af81d5c-43ce-4ed3-a138-621ddfc87b29","lanes":[{"candidate_count":9,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":32,"manifest_digest":"686a4710f8e2e17419fd0c3ba23de01e57911596dd010d093ccf49e9c651ac49","status":"valid"},"source_digest":"fee70094ff66af39d6a20537e6e5f96a3e5289a82e6e908e14522785f3154c92","version":1},"findings":[{"category":"gobby-format","check_key":"complete-target-inventory-p3","description":"Phase 3 omits primitive tests, the old and new TabBar source/test paths, the style-ratchet allowlist, and named Card/FormField consumers. An isolated leaf cannot execute the described migrations from its formal Targets.","finding_id":"WS2-R1-F01","fix":"Expand §§ 3.1–3.4 Targets to list every primitive test, old/new TabBar path, moved test, allowlist update, and named production consumer.","location":"Phase 3 / §§ 3.1–3.4","prevention":"Compare each deliverable's nouns, moves, consumers, tests, and ratchet effects against its exact Targets block.","principle":"Every deliverable must enumerate every file it creates, moves, tests, or directly migrates.","root_cause":"Phase 3 describes cross-file primitive creation and adoption while its Targets name only anchor implementations.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"complete-target-inventory-p4","description":"The Phase 4 Targets omit many production owners from the live raw-control and class-constant inventories across editor, activity, settings, chat, command-browser, and shared surfaces.","finding_id":"WS2-R1-F02","fix":"Re-read the live allowlist and enumerate every owned production path under its exact Phase 4 deliverable, including graph, task, filter, chat, settings, and command-browser families.","location":"Phase 4 / §§ 4.1–4.10","prevention":"Diff the live raw-element and class-constant ownership inventories against every Phase 4 Targets block before review.","principle":"A migration sweep must materialize its live ownership inventory into file-qualified Targets.","root_cause":"Phase 4 uses representative anchors while the acceptance text assigns edits across dozens of inventory owners.","section_id":"4.1","severity":"blocking"},{"category":"gobby-format","check_key":"complete-target-inventory-retirements","description":"The retirement target sets omit promised sheets, import owners, and coarse-pointer, typography, or ratchet guards. Following them can leave imports to deleted files or tests that read missing paths.","finding_id":"WS2-R1-F03","fix":"Add the full chat, sessions, activity, task-detail, and dropdown-caret sheet inventories, their direct owners, and applicable guard tests to §§ 5.1–5.6 and 7.1.","location":"Phase 5 / §§ 5.1–5.6 and Phase 7 / § 7.1","prevention":"For each deleted stylesheet, trace imports and readFileSync guards and require all resulting paths in Targets.","principle":"Stylesheet retirement must target the deleted sheet, every import owner, and every guard that reads that path.","root_cause":"Retirement sections name representative sheets while their barrels, direct consumers, and source-reading guards remain implicit.","section_id":"5.2","severity":"blocking"},{"category":"weak-testability","check_key":"settings-retirement-parity","description":"The legacy Settings tests cover theme, font size, reset/default behavior, focus trapping, Escape, and focus restoration, while the replacement overlay seam does not cover the same set. Redirecting actions can also change rendered output despite 1.4 being the sole parity exemption.","finding_id":"WS2-R1-F04","fix":"Target the legacy and replacement tests, map every Settings control to its overlay destination, port all retained assertions, and require visual/output parity for the redirected command actions.","location":"Phase 2 / § 2.1","prevention":"Inventory legacy controls and assertions, then map each to the replacement surface or an explicit removal requirement.","principle":"Deleting a UI surface requires an explicit behavioral and visual disposition for every control and test seam.","root_cause":"Section 2.1 redirects command actions to a structurally different overlay without mapping legacy behavior or tests.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"responsive-tier-runtime-semantics","description":"A fine-pointer 932×430 viewport, post-load resize, SSR, or malformed token can make cached platform classification disagree with reactive useIsMobile and CSS tier behavior.","finding_id":"WS2-R1-F05","fix":"Define one validated width-or-height query builder for reactive layout, keep device-capability heuristics separate or rename them, and add 767/768, 500/501, landscape, resize, SSR, token-failure, and listener-cleanup tests.","location":"Phase 1 / § 1.4","prevention":"Test width, height, pointer, resize, SSR, and token-failure dimensions separately for every public responsive API.","principle":"One layout tier requires one reactive geometry predicate with identical CSS and JavaScript boundaries.","root_cause":"The plan routes a cached touch/user-agent device heuristic through geometry tokens alongside a reactive viewport hook.","section_id":"1.4","severity":"blocking"},{"category":"missing-requirement","check_key":"coarse-pointer-primitive-contract","description":"Input, Textarea, NativeSelect, Radix Select triggers/items, and dense Button compositions lack a plan-level invariant proving 44×44 hit areas after legacy CSS retirement.","finding_id":"WS2-R1-F06","fix":"Specify invisible hit-area behavior for each primitive, add computed-box tests in representative migrated surfaces, and constrain dense Button use to documented moats that supply an equivalent target.","location":"Phase 3 / § 3.3 and Phase 4 migration sweep","prevention":"Assert computed hit boxes for every canonical primitive and representative production composition under coarse pointers.","principle":"Every canonical interactive primitive needs an executable 44×44 coarse-pointer hit-area contract.","root_cause":"The plan specifies the touch floor globally while current guards cover a closed Button-centric list and the Input/Select paths remain 36px.","section_id":"3.3","severity":"blocking"},{"category":"weak-testability","check_key":"visual-matrix-surface-coverage","description":"The 17-surface matrix omits AgentEditForm/Portfolio, both graph explorers, FilesPage, and memory, integrations, channels, stages, and profiles migrations.","finding_id":"WS2-R1-F07","fix":"Add scenarios for the omitted families or explicitly map each to an existing representative capture with seeded state and an equivalence rationale; update the matrix count.","location":"Phase 1 / § 1.3","prevention":"Map every visually affected deliverable to a named screenshot scenario and seeded state before freezing the matrix.","principle":"Visual-parity evidence must exercise every visually affected migration family or document a valid representative mapping.","root_cause":"The fixed screenshot manifest covers core activity/chat surfaces while several migration families have no scenario.","section_id":"1.3","severity":"blocking"},{"category":"weak-testability","check_key":"capture-artifact-isolation","description":"A retry or second run can overwrite baselines, and lazy or asynchronous content can be photographed before it settles, making parity evidence non-reproducible.","finding_id":"WS2-R1-F08","fix":"Use separate before/after run directories, refuse overwrite, emit git/plan/scenario/PNG hashes, isolate matrix cells, and define readiness callbacks for each captured state.","location":"Phase 1 / § 1.3 and Phase 6 / § 6.1","prevention":"Require immutable run labels, artifact hashes, fresh contexts, overwrite refusal, and scenario-specific readiness callbacks.","principle":"Before/after visual artifacts must be immutable, pairable, isolated, and captured after declared runtime readiness.","root_cause":"Stable filenames share one gitignored directory and visible checkpoints do not settle asynchronous descendants.","section_id":"1.3","severity":"blocking"},{"category":"weak-testability","check_key":"reduced-motion-relocation","description":"The plan can preserve selector counts while breaking reduced-motion suppression or ordinary animation behavior after the voice and global rules move.","finding_id":"WS2-R1-F09","fix":"Add a reduced-motion capture subset and computed-style assertions for recording, speaking/listening, loading, and streaming states, plus a no-preference control.","location":"Phase 5 / § 5.2 and Phase 6 / § 6.1","prevention":"Run bounded reduced-motion and no-preference checks for every relocated animation family.","principle":"Required accessibility behavior must be verified as rendered behavior when selectors move between stylesheets.","root_cause":"The matrix omits reduced motion and relocation acceptance checks source placement rather than computed animation behavior.","section_id":"5.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"deletion-prerequisite-dependencies","description":"Section 5.3 deletes chat/styles.css without depending on 5.1, which removes a retained barrel member. Section 7.4 requires exactly three imports without depending on 7.3, which removes settings-overlay.css.","finding_id":"WS2-R1-F10","fix":"Make 5.3 depend on 5.1 and 5.2; make 7.4 depend on 7.2 and 7.3; state the exact import-count preconditions.","location":"Phase 5 / § 5.3 and Phase 7 / § 7.4","prevention":"For every deletion, trace all retained imports and barrel members back to their owning deliverables and add each dependency.","principle":"A deletion deliverable must depend on every task that removes the imports or retained members required by its acceptance.","root_cause":"Dependencies follow neighboring retirement order while skipping independent import-removal prerequisites.","section_id":"5.3","severity":"blocking"},{"category":"traceability","check_key":"sole-visual-parity-exemption","description":"The phrase allowing parity or reviewed intended fixes creates an unbounded second visual-change exemption at the cascade transition.","finding_id":"WS2-R1-F11","fix":"Require exact parity against the immutable post-1.4 baseline and remove the reviewed-intended-fixes alternative.","location":"Phase 6 / § 6.1","prevention":"Compare every parity acceptance clause against the plan-wide exemption list.","principle":"Acceptance criteria cannot weaken a governing constraint.","root_cause":"Section 6.1 permits reviewed intended fixes even though the Constraints reserve rendered-output changes exclusively for 1.4.","section_id":"6.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"design-contract-atomicity","description":"The shipped implementation and .impeccable.md would disagree between phases, and 8.2 targets app-shell.css after its deletion.","finding_id":"WS2-R1-F12","fix":"Move the app-header contract update into 7.2 through the impeccable workflow and remove app-shell.css from 8.2 Targets.","location":"Phase 7 / § 7.2 and Phase 8 / § 8.2","prevention":"Pair every architecture-changing deletion with its design-contract edit and remove deleted files from later Targets.","principle":"A governing design contract must change atomically with the implementation architecture it describes.","root_cause":"The plan deletes app-shell.css in 7.2 while postponing the canonical app-header contract update to 8.2.","section_id":"7.2","severity":"blocking"},{"category":"traceability","check_key":"native-select-end-state-inventory","description":"The plan intentionally retains two select paths yet its primitive accounting and final UI inventory describe only one.","finding_id":"WS2-R1-F13","fix":"State the two sanctioned select paths, correct the Phase 3 primitive count/family language, and include NativeSelect in 8.2.","location":"Phase 3 / § 3.3 and Phase 8 / § 8.2","prevention":"Reconcile every created primitive with phase goals, adoption rules, and final inventory documentation.","principle":"Primitive-family counts and final design-system inventories must describe the same intended end state.","root_cause":"Section 3.3 introduces NativeSelect while Phase 3 still counts four missing primitives and 8.2 omits NativeSelect.","section_id":"8.2","severity":"blocking"},{"category":"missing-requirement","check_key":"sanctioned-raw-control-floor","description":"The contract protects composite div containers and keyboard guards, while their nested real buttons and FilesTab inputs remain migration-owned. Pinning those controls as sanctioned raw debt contradicts the canonical Button/Input contract.","finding_id":"WS2-R1-F14","fix":"Retain the composite div/event-target guards, migrate nested native buttons and inputs, assign every FilesTab input an owner, and limit the raw floor to composer controls plus the typed Wiki deferral.","location":"Phase 4 / § 4.10 and Phase 8 / § 8.1","prevention":"Separate container semantics from each nested native control when classifying ratchet exemptions.","principle":"A sanctioned non-native composite container does not exempt nested native controls from canonical primitives.","root_cause":"The plan treats the FilesTab and ToolCallCard container exceptions as a floor for their nested raw buttons and inputs.","section_id":"4.10","severity":"blocking"},{"category":"missing-requirement","check_key":"resolve-plan-implementation-branches","description":"Chip size, SidebarPanel retirement, token-alias graduation, and ratchet slack remain open choices, leaving leaf agents to make plan-level scope decisions.","finding_id":"WS2-R1-F15","fix":"Inspect current consumers now and choose explicit outcomes for all four branches, updating Targets and acceptance accordingly.","location":"Phase 3 / § 3.1, Phase 4 / § 4.2, Phase 5 / § 5.3, Phase 8 / § 8.1","prevention":"Resolve each conditional end state during planning and encode one explicit outcome in acceptance and Targets.","principle":"An execution plan must resolve branches that materially change scope, deletion, public API, or ratchet policy.","root_cause":"Several sections defer final choices to implementation-time inspection.","section_id":"8.1","severity":"blocking"}],"reviewer_session":"#10331","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"15be8dcd-f9ee-4429-9b40-10cbb6705e6b"}
```


**Round 2** `kind: verification`

- reviewer_run: `1cb6bb76-215b-4dd7-aea1-e17be5966c8d`
- reviewer_session: `89596331-d655-4cff-b03b-3f55dbe4165a` (#10337)
- verdict: needs_review
- findings:
- WS2-R2-F01 blocking gobby-format — 46 noncanonical Targets across P1/P2/P3/P5/P7
- WS2-R2-F02 blocking gobby-format — 1.4/3.1/3.3 inventories omit importers, adopters, primitives, guards
- WS2-R2-F03 blocking gobby-format — 4.2/4.9/4.10 omit consumers, tests, and two unowned settings inputs
- WS2-R2-F04 blocking gobby-format — retirement leaves omit import owners, consumers, deps, ratchet edit
- WS2-R2-F05 blocking unhandled-edge — standalone activity mounts can render unstyled after the 1.1 split
- WS2-R2-F06 blocking unhandled-edge — immutable capture runs unrecoverable under retry/parallelism
- WS2-R2-F07 blocking unhandled-edge — font-size value domain mismatch (legacy 12–48 vs overlay 12–24)
- WS2-R2-F08 blocking missing-requirement — ui/Textarea referenced but does not exist
- WS2-R2-F09 blocking weak-testability — JSDOM cannot prove effective 44×44 hit-area activation
- WS2-R2-F10 blocking over-engineering — one dropdown abstraction erases four divergent filter controllers
- WS2-R2-F11 blocking bad-sequencing — P1–P3 leaves lack baseline dependency edges
- resolution_notes: All 11 findings accepted by the user (F07 with one amendment: 12–24 stays the canonical font-size domain — single-user install — so persisted/API values outside it clamp on load in useSettings with 12/24/48 round-trip tests, rather than preserving 12–48). Repairs applied: all 46 shortened Targets canonicalized to repository-relative paths and every bare symbol-bearing target given a justified file-wide scope; 1.4 gained the platform test and both graph-explorer import sites for the device-capability rename; 3.1 narrowed to its two directly-owned chip families with the 17 `.activity-chip` adopters assigned explicit adoption acceptance in 4.2/4.3/4.8; 3.3 gained the existing Input/Select primitives, the new ui/Textarea primitive (P3 count now six) with ref/auto-grow contract, four new primitive tests, the coarse-pointer guard, and a Chromium perimeter-activation spec (3.3.8, 3.3.9); 4.2 owns AgentEditForm, the AgentEditors destination test, and the SidebarPanel.css deletion moved out of 3.4; 4.9 reshaped to share only a presentational filter shell while keeping all four controllers with semantics pinned by ported tests, sequenced after 4.8; 4.10 owns the two settings-section inputs; 1.1 gives empty-state.css and rules-tab.css explicit multi-owner imports with standalone render pins (1.1.4) and 5.1/5.5 remove all owners; 1.3 rebuilt around attempt-scoped staging, per-cell manifest fragments, and one atomic finalizer with overwrite refusal against finalized runs only; 5.5 depends on 4.3/4.6/4.8 with full import-owner/consumer inventories; 7.1 gained the ratchet edit; 7.2 names its three app-header consumers; 7.3 targets WorkflowVariablesEditor, the four field components, and all 13 section components; and 1.1/1.2/1.4 now depend on 1.3 with 2.1 and all of P3 depending on 1.4.

```json plan-review-round
{"evidence_id":"dba9b662-7882-440d-be5c-758eb1729e58","plan_hash":"b0c0bd1c71bba9d0af0d585c6d1cf5ec3a6d520b8c37463412b4c034746f997a","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"be1569341b9f4999256fedb87ab77ab29f3bd1e7423c1968c04ab0e52a39bafd","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":18,"emitted_findings":11,"total":29},"evidence_id":"dba9b662-7882-440d-be5c-758eb1729e58","lanes":[{"candidate_count":12,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":9,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":32,"manifest_digest":"7b8bbf209827fda42473866ba413420c40c7a77b4bd42cc0625e11c5d385ea3f","status":"valid"},"source_digest":"9281bb359ebe40fa53c59356dbedfc779e5207da1552f7bf3d5a81088ab0f867","version":1},"findings":[{"category":"gobby-format","check_key":"concrete-symbol-scoped-targets","description":"Forty-six Targets remain noncanonical, including `activity/ActivityPanel.tsx`, `mobileChromeCss.test.ts`, `main.tsx`, and `index.css`. These paths do not resolve to the actual repository files and several existing symbol-bearing targets also lack exact symbols or a justified `::*` scope.","finding_id":"WS2-R2-F01","fix":"Replace every shortened entry with its exact repository-relative path, such as `web/src/components/activity/ActivityPanel.tsx::*` and `web/src/__tests__/mobileChromeCss.test.ts::*`, then resolve each existing symbol-bearing file to exact qualified symbols or one justified file-wide scope.","location":"P1 §1.1 and adjacent Targets in §§1.2, 2.1, 3.4, 4.3, 5.1–5.6, and 7.1–7.4","prevention":"Resolve every changed file through the live index and reject shortened, basename-only, or unscoped existing-file Targets.","principle":"Targets must use exact repository-relative paths and symbol scope for existing symbol-bearing files.","root_cause":"The repair inventory retained basename and shortened-path shorthands that do not identify files from the repository root.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","check_key":"complete-target-inventory-p3","description":"Section 1.4 omits both graph importers and `platform.test.ts` from the `IS_MOBILE` rename; 3.1 omits seventeen live `.activity-chip` adopters while requiring all families migrated; 3.3 omits existing Input/Select implementations, primitive tests, and the coarse-pointer guard it changes.","finding_id":"WS2-R2-F02","fix":"Add exact scoped Targets for all `IS_MOBILE` importers/tests, every Chip adopter owned by 3.1, and every modified UI primitive and test. Where P4 owns a Chip adopter, narrow 3.1 acceptance and add explicit adoption acceptance to that exact P4 leaf.","location":"P1 §1.4 and P3 §§3.1, 3.3","prevention":"Diff each P1/P3 acceptance clause against the live import, consumer, primitive, and direct-test graph.","principle":"A self-contained foundation or primitive leaf must target every caller, consumer, implementation, and test changed by its acceptance.","root_cause":"The accepted repairs added representative anchors while leaving live blast-radius owners outside the formal inventories.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"complete-target-inventory-p4","description":"Section 4.2 omits `AgentEditForm` and the destination test for SidebarPanel retirement; 4.9 omits `RulesTab`, `TasksTabToolbar`, and direct filter behavior tests; 4.10 omits the live raw inputs in `McpToolsSection.tsx` and `ToolApprovalsSection.tsx` even though acceptance requires the input map to be empty.","finding_id":"WS2-R2-F03","fix":"Add the exact consumer and test Targets, assign the RulesTab dropdown to one sequenced leaf, and migrate the two unowned settings inputs through `ui/Input` before 4.10.1 can assert zero.","location":"P4 §§4.2, 4.9, 4.10","prevention":"Compare every P4 Target block against the complete live allowlist, constructors/callers, and tests before asserting a zero map.","principle":"A migration sweep cannot reach a zero-debt acceptance state while live owners, callers, or destination tests remain unassigned.","root_cause":"The repaired P4 census omitted several caller and raw-control owners and left overlapping ownership unsequenced.","section_id":"4.2","severity":"blocking"},{"category":"gobby-format","check_key":"complete-target-inventory-retirements","description":"SidebarPanel.css is deleted before its importing component retires; 5.4/5.5 omit direct import owners and utility consumers and 5.5 lacks dependencies on 4.3 and 4.6; 7.1 omits the mandatory ratchet edit; 7.2 names no app-header consumers; 7.3 targets only SettingsOverlay while moving rules across WorkflowVariablesEditor, fields, and thirteen section components.","finding_id":"WS2-R2-F04","fix":"Move SidebarPanel.css deletion into 4.2, add every named import owner and consumer to 5.4/5.5/7.2/7.3, make 5.5 depend on 4.3, 4.6, and 4.8 (or all P4), and add `styleRatchet.allowlist.ts` to 7.1.","location":"P3 §3.4, P5 §§5.4–5.5, and P7 §§7.1–7.3","prevention":"For each stylesheet, trace direct imports and selector consumers, then require all resulting files, tests, ratchet edits, and dependencies in the deleting section.","principle":"A deleting leaf must own every import removal, replacement-utility consumer, ratchet update, guard, and prerequisite edge.","root_cause":"The retirement repair enumerated deleted sheets and some guards while leaving their live owners and consumers implicit.","section_id":"5.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"standalone-style-ownership","description":"`ActivityPanelEmpty` still depends on `empty-state.css` loaded only by the chat barrel, while Skills and Integrations depend on filter-panel rules loaded only by RulesTab after the split. These surfaces can render unstyled in standalone or lazy-mounted states, contradicting 1.1.2.","finding_id":"WS2-R2-F05","fix":"In 1.1, either move those shared rules to component utilities immediately or give the temporary sheets every direct consumer as an explicit import owner; add standalone render checks for ActivityPanelEmpty, SkillsTab, and IntegrationsFilterPanel before later retirement.","location":"P1 §1.1 with retirement follow-through in §§5.1 and 5.5","prevention":"For every relocated sheet, enumerate all selector consumers and render each consumer without ChatPage or sibling tabs mounted.","principle":"Every lazy or standalone-mounted activity surface must load all styling it requires through itself or an explicit shared owner.","root_cause":"The split moves each activity sheet to one nominal owner while shared selectors remain in the ChatPage barrel or a sibling tab's sheet.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"capture-artifact-retry-isolation","description":"A failed partial attempt leaves the label occupied, so its retry is rejected as an overwrite; parallel cells can also race while updating one run manifest. The repair guarantees immutability but does not define recoverable publication.","finding_id":"WS2-R2-F06","fix":"Write each retry and matrix cell to attempt-scoped staging paths, emit immutable per-cell manifest fragments, and use one deterministic atomic finalizer. Refuse overwrite only when a finalized run already exists.","location":"P1 §1.3 and P6 §6.1","prevention":"Test first-attempt failure, retry, concurrent matrix cells, and interrupted finalization for every immutable artifact writer.","principle":"Immutable evidence generation must support retries and parallel workers without collision or partial-manifest corruption.","root_cause":"One git-SHA-plus-side label owns one directory and manifest while Playwright runs fully parallel and retries twice in CI.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"settings-retirement-value-domain","description":"Existing users can have font sizes from 25 through 48. After retirement those values still load into state, but the replacement range control cannot represent them, and the plan neither preserves the range nor migrates stored/API values.","finding_id":"WS2-R2-F07","fix":"Preserve the legacy 12–48 range in AppearanceSection, correct the disposition map, and add tests for 12, 24, 48, and persisted/API round trips so no accepted value becomes unrepresentable.","location":"P2 §2.1","prevention":"Compare legacy and replacement bounds and test persisted values below, within, and above the replacement range.","principle":"Replacing a persisted control must preserve its accepted value domain or define and test a data migration.","root_cause":"The disposition map records the legacy slider as 12–24, but the live legacy control accepts 12–48 while the overlay caps at 24 and persistence does not clamp.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"textarea-primitive-end-state","description":"FieldPrimitives and the chat composer are told to migrate to `ui/Textarea`, yet there is no `web/src/components/ui/Textarea.tsx`. ChatInput also requires a real `HTMLTextAreaElement` ref for focus and auto-grow behavior, so the missing boundary cannot be inferred safely by a leaf.","finding_id":"WS2-R2-F08","fix":"Create and target `ui/Textarea.tsx` plus its unit test in 3.3; specify `forwardRef<HTMLTextAreaElement>`, full native props, class merging, ARIA/error and coarse-pointer behavior, and ref/auto-grow tests. Update the P3 primitive count and final component inventory before 4.10 adopts it.","location":"P3 §3.3 and P4 §4.10","prevention":"Reconcile every referenced primitive against the live `components/ui` inventory before assigning adopters.","principle":"Every canonical primitive named by migration acceptance must have an owned implementation, API contract, adoption order, and tests.","root_cause":"The plan assumes `ui/Textarea` already exists, but the repository has no Textarea primitive and P3 neither creates nor counts one.","section_id":"3.3","severity":"blocking"},{"category":"weak-testability","check_key":"coarse-pointer-effective-hit-area","description":"The accepted invisible 44×44 expansion can pass planned computed-box assertions while remaining unclickable because JSDOM cannot verify pseudo-element or hit-test geometry. Visual screenshots also cannot establish target activation.","finding_id":"WS2-R2-F09","fix":"Add a Chromium test Target in 3.3 for Input, Textarea, NativeSelect, Radix Select trigger, and SelectItem; assert unchanged visible geometry plus click/focus activation at the expanded perimeter under `pointer: coarse`.","location":"P3 §3.3","prevention":"Exercise every invisible hit-area implementation in a real browser at its perimeter under a coarse pointer.","principle":"A touch-target test must prove the browser's effective clickable area, not only source declarations or host box dimensions.","root_cause":"The existing Vitest/JSDOM guard flattens CSS and cannot measure pseudo-element geometry, perimeter hit testing, or click-to-focus behavior.","section_id":"3.3","severity":"blocking"},{"category":"over-engineering","check_key":"filter-consolidation-state-semantics","description":"ActivityFilterDropdown is immediate-select-and-close, Sessions propagates live and uses Apply only to close, Tasks stages drafts until Apply, and Rules is an inline panel with no dropdown/dialog semantics. A single unspecified dropdown risks erasing commit/discard and focus behavior.","finding_id":"WS2-R2-F10","fix":"Use the simpler form: share only a presentational filter-field/shell component, keep the four surface controllers, and assign Rules/Skills/Integrations exclusively to the 5.5 inline-panel work. Target and port tests for Apply, Reset, Escape, outside click, focus trap/restore, and discard semantics.","location":"P4 §§4.8–4.9 and P5 §5.5","prevention":"Compare open/close, draft/apply, immediate/live, reset, Escape, outside-click, and focus semantics before consolidating sibling components.","principle":"Shared UI mechanism must preserve each consumer's state-transition and accessibility contract; visually similar but behaviorally divergent controllers should stay separate.","root_cause":"The plan forces four different interaction models into one dropdown abstraction and also assigns the inline filter family to a second shared-panel effort.","section_id":"4.9","severity":"blocking"},{"category":"bad-sequencing","check_key":"post-1-4-baseline-dependency","description":"The capture harness can land after 1.1/1.2/1.4, and later deliverables can run before 1.4 establishes the sole post-change parity baseline. The shadow manifest confirms these leaves are independently runnable.","finding_id":"WS2-R2-F11","fix":"Make 1.1, 1.2, and 1.4 depend on 1.3; make 2.1 and every P3 deliverable depend on 1.4. Existing P4-and-later phase dependencies then carry the post-1.4 baseline transitively.","location":"P1 §§1.1–1.4, P2 §2.1, and P3 §§3.1–3.4","prevention":"Inspect the derived manifest, not heading order, and trace every baseline consumer back to its producing leaf.","principle":"Evidence tooling and the declared baseline must exist before any deliverable whose acceptance depends on them.","root_cause":"Phase numbering implies order, but the derived manifest gives 1.1–1.4, 2.1, and 3.1–3.4 no dependency edges.","section_id":"1.4","severity":"blocking"}],"reviewer_session":"#10337","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"15be8dcd-f9ee-4429-9b40-10cbb6705e6b"}
```


**Round 3** `kind: verification`

- reviewer_run: `ed198989-81c6-45c7-b714-c8f7b02c6c52`
- reviewer_session: `acb2cace-6674-4527-8dd2-70332569334b` (#10342)
- verdict: needs_review
- findings:
- WS2-R3-F01 blocking unhandled-edge — capture finalizer not exactly-once under fullyParallel + retries
- WS2-R3-F02 blocking bad-sequencing — shared exact-census writers concurrently ready across P1/P3/P4/P5/P7
- WS2-R3-F03 blocking weak-testability — coarse-pointer Chromium spec has no named execution command
- WS2-R3-F04 blocking unhandled-edge — font-size clamp misses untrusted non-numeric and malformed inputs
- WS2-R3-F05 blocking weak-testability — command-palette settings routing has no transition coverage
- WS2-R3-F06 blocking weak-testability — two of four filter controllers lack direct state-transition tests
- WS2-R3-F07 blocking weak-testability — SidebarPanel a11y behaviors absent from 4.2 acceptance
- WS2-R3-F08 blocking traceability — message.css retirement omits eight non-chat .message-content consumers
- WS2-R3-F09 blocking traceability — 5.2 Targets omit every composer/status consumer of the input family
- WS2-R3-F10 blocking traceability — 5.3 omits six layout.css consumers and no dead-selector classification
- WS2-R3-F11 blocking bad-sequencing — 5.4 can delete the activity sheet before its chip adopters migrate
- WS2-R3-F12 blocking traceability — 5.5 omits MCP/Rules consumers and leaves the inline panel unresolved
- WS2-R3-F13 blocking bad-sequencing — 5.6 omits ten task-sheet consumers and the 4.8 prerequisite
- WS2-R3-F14 blocking traceability — 7.3 omits the shared SettingsSection and configFields renderers
- resolution_notes: All 14 findings accepted by the user with no amendments ("accept"); this is the final round at the operator-set cap of 3. Repairs applied: 1.3 gained a run-level `captureRunFinalizer.ts` registered as Playwright `globalTeardown` (success-fragment-last protocol, missing-cell abort, highest-attempt retry resolution, one atomic rename) with `captureRunFinalizer.spec.ts` covering missing/duplicate/retry/interruption/parallel outcomes and new acceptance 1.3.8. Shared-writer serialization encoded as dependency edges plus a stated rationale in each phase framing: 1.1 → 1.2 → 1.4 after 1.3; 2.1 → 3.1 → 3.2 → 3.3 → 3.4; 4.1 → … → 4.10; 5.1 → … → 5.6; 7.1 → … → 7.4 — every chain link is an exact-census ledger or shared guard-pin writer, and no chain adds functional coupling beyond the edges already present. 3.3 and V2 now name `cd web && npx playwright test coarse-pointer-hit-areas.spec.ts` with spec-level coarse-pointer emulation under the existing chromium project. 2.1 replaced the bare clamp with one `normalizeFontSize(value: unknown)` boundary applied at both untrusted entries in `useSettings.ts` (localStorage read and settings-API merge), discarding malformed roots, with null/string/non-finite cases added to 2.1.6; new acceptance 2.1.7 plus the `useAppCommandPalette.test.tsx` target pin both palette actions opening the overlay exactly once, and the body requires an App-level assertion that the legacy branch cannot render. 4.9 gained a new `ActivityFilterDropdown.test.tsx` and direct Rules-controller coverage in `rules/__tests__/RulesTab.test.tsx`, with 4.9.1 mapping all four controllers to named tests. 4.2 gained acceptance 4.2.4 for focus trap, Escape close, and focus restoration in `AgentEditors.test.tsx`. 5.1 made `shared/MarkdownBody.tsx` the canonical markdown-typography owner and targets all eight non-chat `.message-content` consumers verified by census (FilesPage, FilesTab, PlanReviewCard, SessionsTabDetail, TasksTabDetailPanel, SkillContentView, TaskDetailEditableCore, WikiPageReader) with parity acceptance 5.1.4. 5.2 gained the eight composer/status consumers plus the two coupled hooks the census surfaced (`useChatInputPrimaryAction.ts` authors the primary-button classes; `useChatInputNarrow.ts` reads the `.chat-column` container that moves to `ChatMainColumn`) and the typography guard. 5.3 gained CommandBar, CommandPalette, MessageItem, MessageList, and WatchingTranscript as targets and now classifies layout.css selector-by-selector: the command-bar, command-palette, `.message-content`, and `.chat-scaled` families migrate, while `.chat-container`/`.chat-messages`/`.chat-page`/`.chat-main`, the whole `.mobile-chat-drawer*` family, and the `.message*` shells are deleted as dead (zero component consumers, verified 2026-08-08). 5.4 now depends on 4.2, 4.3, and 4.9 and targets `ActivityActionsContext.tsx`, `ActivityRowStatusDot.tsx`, and their two direct tests with retained-hook versus replaced-style dispositions. 5.5 gained McpServerFields, mcpIcons, RulesDetailPanel, RulesTabList, and RulesYamlView, and resolves the open abstraction into a presentational `InlineFilterPanel` in `FilterPrimitives.tsx` with a direct `FilterPrimitives.test.tsx`; 5.5.2 re-anchors to that implementation. 5.6 now depends on 4.7 and 4.8 and targets all ten task-sheet consumers plus the three class-sensitive tests, stating explicitly where 4.7/4.8 own the earlier control migrations. 7.3 gained `SettingsSection.tsx` and `configFields.tsx` — the shared renderers that actually emit the `.settings-section*` shells — with a new `SettingsSection.test.tsx` render pin.

```json plan-review-round
{"evidence_id":"13396121-e587-4d74-bab8-ddfad738d7d8","plan_hash":"f7537f589528a99ffc26aeb452b87c428d907372bacadc5c4b1e16ef1111e08b","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"5445f5e37fde64fa6c5e25cce95432dddf1b9321a00da193056dbc107cf3dc0c","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":14,"total":21},"evidence_id":"13396121-e587-4d74-bab8-ddfad738d7d8","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":32,"manifest_digest":"6a83310742ad24f4bb6b6fd4b450332c492cd4bdb5f75833925a1330bcd65e8a","status":"valid"},"source_digest":"2d4f177a12ab5b810a6710a6222cd2cc1d49f423753337c215b4f8c06a67dab4","version":1},"findings":[{"category":"unhandled-edge","check_key":"capture-finalizer-exactly-once","description":"The current Playwright configuration uses fullyParallel execution and CI retries. A spec-local finalizer can run per worker, and the plan does not define how exactly one finalizer excludes failed attempts and waits for every expected cell.","finding_id":"WS2-R3-F01","fix":"Add a targeted run-level finalizer module registered through Playwright global teardown or an equivalent exactly-once coordinator. Each passed attempt writes its success fragment last; the finalizer rejects missing or duplicate successful cells, deterministically selects retry winners, and performs one atomic rename. Add unit coverage for missing, duplicate, retry, interruption, and parallel completion.","location":"P1 / § 1.3","prevention":"For parallel capture plans, verify missing, duplicate, failed-then-successful, interrupted, and concurrent cell outcomes before approving publication.","principle":"Parallel artifact publication requires one run-level coordinator to select a complete set of successful attempts before publication.","root_cause":"The plan names an atomic finalizer without defining the Playwright lifecycle hook, success-fragment protocol, duplicate retry selection, or a finalizer module target.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"shared-writer-dependency-serialization","description":"The manifest permits parallel stale edits to the ratchet and guard files even though each migration must shrink the live exact census in the same commit. Conflicting or lossy merges are reachable across P1, P3, P4, P5, and P7.","finding_id":"WS2-R3-F02","fix":"Add deterministic dependency chains for each shared-writer cluster while preserving existing functional prerequisites: order P1 mobile-guard writers, the P2/P3 ratchet writers, P4 ratchet/input-focus writers, P5 ratchet/coarse/typography writers, and P7 main/mobile/ratchet writers. Encode the final order in section dependencies so the derived manifest carries it.","location":"Cross-phase sequencing beginning at § 1.1","prevention":"Build a shared-writer matrix during planning and add dependencies until every overlapping exact-ledger, barrel, guard, and component writer is ordered.","principle":"Dependency-ready leaves that update an exact textual census or shared guard must have deterministic write ordering.","root_cause":"Many sibling deliverables edit styleRatchet.allowlist.ts, mobileChromeCss.test.ts, coarsePointerTouchTargets.test.ts, inputFocusAdoption.test.ts, typographyLadder.test.ts, main.tsx, App.tsx, RulesTab.tsx, or FilesTab.tsx while remaining concurrently ready.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"browser-spec-execution-gate","description":"Acceptance 3.3.9 can remain unexecuted while all listed phase-boundary commands and V2 checks pass.","finding_id":"WS2-R3-F03","fix":"Add an explicit Chromium command or package script for web/tests/coarse-pointer-hit-areas.spec.ts to § 3.3 and V2, including the coarse-pointer project/emulation arguments needed by the perimeter activation assertions.","location":"P3 / § 3.3","prevention":"For every added test runner/spec, map it to an exact executable command at leaf validation and final verification.","principle":"Every browser-only acceptance test requires a named command in the deliverable and end-to-end verification path.","root_cause":"The new coarse-pointer spec is outside Vitest, while V2 names only the style-surfaces Playwright spec.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"settings-font-size-untrusted-normalization","description":"Invalid persisted or API values can bypass the settled numeric-domain intent and produce invalid state or a NaNpx CSS value.","finding_id":"WS2-R3-F04","fix":"Specify one normalizeFontSize(value: unknown) boundary used by local and remote settings loads: finite numbers clamp to 12–24 and invalid values fall back to the default. Validate the parsed settings root as an object. Keep the settled 12/24/48 cases and add null, string, non-finite, and malformed-root tests.","location":"P2 / § 2.1","prevention":"Test lower bound, upper bound, high numeric clamp, invalid scalar types, non-finite values, and malformed containers at every untrusted settings boundary.","principle":"Values entering typed settings state from storage or an API must be runtime-normalized before arithmetic or CSS serialization.","root_cause":"The repaired clamp covers numeric 12, 24, and 48 while localStorage and API JSON can still supply null, strings, non-finite values, or a malformed root.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","check_key":"settings-command-routing-regression","description":"SettingsOverlay focus tests do not prove that either command-palette action opens the overlay or that the legacy Settings render path has disappeared.","finding_id":"WS2-R3-F05","fix":"Add web/src/components/app/__tests__/useAppCommandPalette.test.tsx to Targets and assert both settings actions call settingsOverlay.open exactly once. Add an App-level assertion that the legacy Settings branch cannot render while the overlay route opens.","location":"P2 / § 2.1","prevention":"When retiring a surface, enumerate every opener and add tests proving each reaches the replacement and the retired branch is unreachable.","principle":"Every retired UI entry path needs direct transition coverage to its replacement surface.","root_cause":"The plan changes both command-palette settings actions but omits the existing useAppCommandPalette test and an App-level reachability assertion.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","check_key":"filter-controller-semantic-parity","description":"Acceptance 4.9.1 promises semantics for four controllers, yet two controller families can regress without a targeted test.","finding_id":"WS2-R3-F06","fix":"Add direct test Targets for ActivityFilterDropdown immediate-select-and-close behavior and the Rules controller's open/close, reset, Escape, outside-click, and focus-return behavior. Map all four controllers to their tests in 4.9.1.","location":"P4 / § 4.9","prevention":"For every controller sharing a view shell, map its distinct open, close, apply, reset, Escape, outside-click, and focus transitions to a named test.","principle":"A shared presentation refactor must directly pin every behaviorally distinct controller it preserves.","root_cause":"The plan targets Sessions and Tasks tests while ActivityFilterDropdown and the Rules-local controller have no direct state-transition tests.","section_id":"4.9","severity":"blocking"},{"category":"weak-testability","check_key":"sidebar-retirement-a11y-acceptance","description":"The derived leaf can satisfy every current acceptance item without proving the three SidebarPanel accessibility behaviors survived in AgentEditForm.","finding_id":"WS2-R3-F07","fix":"Add acceptance requiring SidebarPanel.tsx/CSS retirement and AgentEditForm-level tests for focus trap, Escape close, and focus restoration in AgentEditors.test.tsx.","location":"P4 / § 4.2","prevention":"When deleting a tested accessibility wrapper, carry each behavioral assertion into a destination acceptance item.","principle":"Ported accessibility behavior must remain explicit in manifest-derived acceptance criteria.","root_cause":"The section body and Targets name AgentEditors.test.tsx, while its acceptance items omit focus trapping, Escape close, and focus restoration.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"stylesheet-consumer-inventory-message-content","description":"FilesPage, FilesTab, PlanReviewCard, SessionsTabDetail, TasksTabDetailPanel, SkillContentView, TaskDetailEditableCore, and WikiPageReader can lose scoped markdown typography when message.css is deleted.","finding_id":"WS2-R3-F08","fix":"Add MarkdownBody.tsx and every wrapper that still supplies .message-content to Targets. Move the descendant typography utilities into the canonical shared markdown surface, migrate remaining direct consumers, and add non-chat markdown parity acceptance.","location":"P5 / § 5.1","prevention":"Before deleting a stylesheet, enumerate every selector consumer, choose one canonical replacement owner, and add parity coverage for each consumer class.","principle":"A stylesheet retirement must inventory every live selector consumer and its replacement owner.","root_cause":"The plan assigns message.css typography to MessageItem although .message-content is also used across multiple non-chat surfaces and a shared MarkdownBody implementation exists.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"stylesheet-consumer-inventory-input-family","description":"The leaf cannot route the files that must receive 1,022 lines of input-family replacement styling.","finding_id":"WS2-R3-F09","fix":"Add exact scoped Targets for ChatInput, ChatInputToolbar, ChatInputModelControls, ChatInputVoiceControls, ChatInputPrimaryButton, useChatInputPrimaryAction, AgentStatusBar, VoiceStatusBar, ChatCommandPalette, ChatMainColumn, typographyLadder.test.ts, and every other direct consumer found by the selector census.","location":"P5 / § 5.2","prevention":"Cross-check every deleted selector against production consumers and class-sensitive tests before finalizing Targets.","principle":"A stylesheet-retirement leaf must target every component and guard that absorbs deleted rules.","root_cause":"Section 5.2 names production destinations in prose while its Targets contain no composer/status consumers and omit additional live consumers.","section_id":"5.2","severity":"blocking"},{"category":"traceability","check_key":"stylesheet-consumer-inventory-layout","description":"ChatMainColumn, CommandBar, CommandPalette, MessageList, MessageItem, and WatchingTranscript are outside the executable edit inventory.","finding_id":"WS2-R3-F10","fix":"Add exact scoped Targets for those six consumers plus their class-sensitive tests. State which layout.css selectors migrate to each component and which are deleted as dead.","location":"P5 / § 5.3","prevention":"Resolve every selector consumer, target its replacement owner, and explicitly classify dead selectors.","principle":"Deleting a layout sheet requires a complete consumer-to-replacement map.","root_cause":"Section 5.3 targets ChatPage while assigning layout.css rules to several other components and omitting a live activity consumer.","section_id":"5.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"activity-sheet-retirement-readiness","description":"The activity-panel sheet can be deleted before all .activity-chip adopters migrate, and remaining direct consumers can be left without replacement styling.","finding_id":"WS2-R3-F11","fix":"Make 5.4 depend on 4.2, 4.3, and 4.9. Add exact Targets for ActivityActionsContext.tsx, ActivityRowStatusDot.tsx, ActivityActionButtons.test.tsx, and ActivityRowStatusDot.test.tsx with explicit retained-hook versus replaced-style dispositions.","location":"P5 / § 5.4","prevention":"For each stylesheet deletion, require all selector-adopter leaves and inventory every direct production/test consumer.","principle":"A shared stylesheet can be deleted only after all adopter migrations complete and every remaining consumer/test is routed.","root_cause":"Section 5.4 depends on 4.9 while agent and pipeline chip adoptions live in 4.2 and 4.3; its inventory also omits ActivityActionsContext, ActivityRowStatusDot, and direct guard tests.","section_id":"5.4","severity":"blocking"},{"category":"traceability","check_key":"small-sheet-consumer-and-shared-panel-inventory","description":"McpServerFields, mcpIcons, RulesDetailPanel, RulesTabList, and RulesYamlView are absent, and the Rules/Skills/Integrations inline panel has no deterministic implementation target.","finding_id":"WS2-R3-F12","fix":"Add those omitted consumer Targets. Extend web/src/components/activity/FilterPrimitives.tsx with a presentational InlineFilterPanel used by RulesTab, SkillsTab, and IntegrationsFilterPanel, add a direct FilterPrimitives test Target, and point 5.5.2 to that implementation while keeping controllers separate.","location":"P5 / § 5.5","prevention":"Run a selector-consumer census and resolve every planned abstraction to one named file plus direct tests.","principle":"A multi-sheet retirement must name every consumer and one concrete shared implementation for consolidated presentation.","root_cause":"The inventory omits direct MCP and Rules consumers and leaves the inline-panel implementation as an execution-time component-or-utilities choice.","section_id":"5.5","severity":"blocking"},{"category":"bad-sequencing","check_key":"task-sheet-retirement-readiness","description":"TasksTab, TaskCloseDialog, TaskTreeRow, TaskFieldEditors, TaskDetailHeader, TaskDetailEditableCore, TaskDetailKV, TaskDetailRelationships, TaskDetailStatusLine, and TaskDetailTrace remain outside the deletion inventory or can migrate after deletion.","finding_id":"WS2-R3-F13","fix":"Add every direct task-execution.css/task-detail.css consumer and class-sensitive test to 5.6 Targets, and make 5.6 depend on both 4.7 and 4.8. If a consumer is fully migrated earlier, state that ownership explicitly in the earlier section and retain the dependency.","location":"P5 / § 5.6","prevention":"For each stylesheet deletion, enumerate all consumers and block on every leaf that migrates them.","principle":"Task stylesheet deletion requires every live consumer migration and every prerequisite edge.","root_cause":"Section 5.6 targets two consumers and depends only on 4.7 although 4.8 owns several task-detail migrations.","section_id":"5.6","severity":"blocking"},{"category":"traceability","check_key":"settings-overlay-shared-consumer-inventory","description":"The components that render .settings-section* and .settings-subsection* can retain dead class hooks or lose replacement styling outside the leaf scope.","finding_id":"WS2-R3-F14","fix":"Add exact scoped Targets for web/src/components/settings/sections/SettingsSection.tsx and web/src/components/settings/sections/configFields.tsx, plus affected render tests.","location":"P7 / § 7.3","prevention":"Include shared render primitives in every selector-consumer census before stylesheet deletion.","principle":"Shared selector owners must be included when their stylesheet retires.","root_cause":"The repaired settings-overlay inventory includes fields and section consumers while omitting the shared SettingsSection and configFields implementations.","section_id":"7.3","severity":"blocking"}],"reviewer_session":"#10342","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"15be8dcd-f9ee-4429-9b40-10cbb6705e6b"}
```


**Cap extension** `kind: verification`

- trigger: `needs_review` at the operator-set review cap of 3 finalized adversary rounds
- finalized_rounds_at_trigger: 3 (Round 1: 15 findings; Round 2: 11 findings; Round 3: 14 findings — 40 total, all accepted and repaired)
- operator_decision: cap raised from 3 to 4 (user, 2026-08-09) — round 3 still emitted 14 blocking findings, so the finding rate had not converged and human handoff was declined in favor of another adversarial round
- effect: the human-handoff route (`derive_plan_handoff_manifest` / `apply_plan_handoff_manifest`) was not entered; the artifact carries every accepted repair and is base-validated at this commit, and Round 4 reviews the round-3 repairs that no adversary has yet seen

**Round 4** `kind: verification`

- reviewer_run: 63e6b1a1-aab8-4495-9147-41aa0be51b9c
- reviewer_session: 7c84aa02-c208-415d-a820-a75c9adbb48a
- verdict: needs_review
- findings:
- WS2-R4-F01/blocking/deferral receiver #19672 carried the 4.4 source-section label and lacked the 4.4.1 artifact obligation
- WS2-R4-F02/blocking/finalizer completeness used the base scenario count and globalTeardown had no activation boundary
- WS2-R4-F03/blocking/body-last success fragments precede afterEach/fixture-teardown verdicts
- WS2-R4-F04/blocking/normalizeFontSize conflated absent fontSize with present-invalid at the Partial settings merge
- WS2-R4-F05/blocking/promised App-level legacy-branch assertion had no target or acceptance item
- WS2-R4-F06/blocking/ActiveAgentIndicator authors chat-input-agent-button but was absent from the 5.2 census
- WS2-R4-F07/blocking/McpDetailPanel consumes activity-panel action-btn but first appears in 5.5 after 5.4 deletes the sheet
- WS2-R4-F08/blocking/MarkdownBody returns a fragment and TaskDetailEditableCore.StaticBlock renders plain text — the nine-hosts-via-MarkdownBody claim was false
- WS2-R4-F09/blocking/SettingsOverlay.test.tsx queries a retiring backdrop selector outside Targets; SettingsSection.test.tsx pin had no acceptance
- WS2-R4-F10/blocking/one settings capture cell covered 13 registry-backed sections
- resolution_notes: All 10 findings accepted by the user with no amendments ("accept"); this is the final round at the operator-extended cap of 4, and 9 of 10 findings are second-order defects in the round-3 repairs. Repairs applied: 1.3's finalizer contract now derives a canonical cell roster from a pure matrix-expansion function shared by spec and finalizer, publishes only on exact expected-key-set equality (unknown keys reject as foreign), records success through a runner-final reporter onTestEnd seam, and gates spec and teardown on an explicit capture-run id so ordinary Playwright runs and stale staging are no-ops — 1.3.8 extended accordingly; the settings-overlay scenario expands into one cell per live SETTINGS_SECTIONS entry with seeded specialty-editor data, and 7.3.2 names that registry-backed set; 2.1's normalization is presence-preserving (absent fontSize keys survive the Partial merge; present-invalid values default) with 2.1.6 updated and new 2.1.8 plus the App.test.tsx target for the legacy-branch assertion; 5.2 gained ActiveAgentIndicator.tsx with its mobileChromeCss pin re-point; 5.4 gained McpDetailPanel.tsx and McpDetailPanel.test.tsx for the action-button family with the 5.4/5.5 selector split stated on both sides; 5.1 went wrapper-neutral — the typography utility exports from MarkdownBody.tsx and attaches at the nine host elements, the census now records StaticBlock's plain-text rendering, and ToolCallCard/RichContentBlocks/WikiAskMode direct-consumer pins were added with the ToolCallCard.interactive.test.tsx target; 7.3 gained SettingsOverlay.test.tsx with the backdrop re-point and new 7.3.3 binding the shared-renderer pins. F01 was repaired in the task record: #19672 relabeled deferred-from:web-styling-consolidation-phase-2:4.11 with the 4.4.1 styleRatchet.allowlist.ts artifact obligation added to its validation criteria.

```json plan-review-round
{"evidence_id":"fb2db9c0-8190-4ddb-8c76-ea65bb9dbaec","plan_hash":"66375b62fd8a58099879dd4fd839109224765ca4c0fa07c22dd23d4eef847095","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c1acad5a01f0520d2a6344724377dbf6cbc4864da8a0b7139be81b8e0dc5557b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":8,"total":10},"evidence_id":"fb2db9c0-8190-4ddb-8c76-ea65bb9dbaec","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":32,"manifest_digest":"b7b82c94f3ad66522cdb0ba0565a62d9f847ec2ad014376818c16080dace77e0","status":"valid"},"source_digest":"8a5e3c212e42c1db5d0df46c3f4a52e44094c8628b5ab8bd32572c40ed1f2f4f","version":1},"evidence_id":"fb2db9c0-8190-4ddb-8c76-ea65bb9dbaec","findings":[{"category":"gobby-format","check_key":"deferral-receiver-provenance","description":"Plan-Coverage Contract rejection: malformed deferral. Live task #19672 is open, but it carries `deferred-from:web-styling-consolidation-phase-2:4.4` instead of the required `...:4.11`; its validation criteria also do not duplicate the `web/src/__tests__/styleRatchet.allowlist.ts` artifact from deferred item 4.4.1.","finding_id":"WS2-R4-F01","fix":"Keep Wiki Ask/Research deferred. Update #19672 with `deferred-from:web-styling-consolidation-phase-2:4.11`, add a validation criterion that owns the deferred WikiAskMode raw-control allowlist/migration artifact from 4.4.1, and retain a valid recovery dependency or cited-parent relation.","location":"P4 / § 4.11","prevention":"Before accepting a deferral, compare its live task state, exact deferred-from label, validation artifact references, and recovery dependency/cited-parent relation.","principle":"A typed deferral is valid only when its live receiver carries the exact section provenance and duplicates the deferred artifact obligation.","root_cause":"The receiver task still records the original 4.4 source section rather than the typed 4.11 deferral section, and its validation criteria omit 4.4.1's artifact.","section_id":"4.11","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"WS2-R3-F01","causal_section_ids":["1.3"],"check_key":"capture-run-authority","description":"The stated `(manifest's asserted entry count)` check can publish after only one variant per scenario while other required variants are missing. A later ordinary Playwright run can also encounter staging because the globally registered teardown has no specified capture activation or invocation namespace.","finding_id":"WS2-R4-F02","fix":"Add a pure shared matrix-expansion function consumed by the spec and finalizer; require exact expected-key equality and reject unknown keys; gate both spec and teardown on an explicit capture run id/activation variable; document the opt-in command; extend 1.3.8 with expansion, inactive-run, stale-staging, and foreign-run tests.","introduced_in_round":3,"location":"P1 / § 1.3","prevention":"Derive one canonical cell-key roster for producer and finalizer, compare exact sets, and test capture-disabled runs plus stale and foreign staging.","principle":"A run finalizer may publish only an explicitly activated run whose exact canonical expanded cell set succeeded.","root_cause":"The finalizer contract uses the 24-entry base scenario count as expected-cell completeness even though themes, pointers, three viewports, grayscale, reduced motion, and states expand that set; globalTeardown also lacks an explicit run namespace/activation boundary.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"WS2-R3-F01","causal_section_ids":["1.3"],"check_key":"playwright-runner-final-success","description":"The success-fragment-last protocol does not represent Playwright's final result. An `afterEach` or fixture teardown failure after the fragment write lets globalTeardown select and publish an attempt the runner marked failed.","finding_id":"WS2-R4-F03","fix":"Move success attestation to a reporter `onTestEnd` or equivalent runner-final seam keyed to a final passed result, and add integration coverage for body-pass plus hook/fixture-teardown failure as well as retries.","introduced_in_round":3,"location":"P1 / § 1.3","prevention":"Exercise body-pass/hook-fail and body-pass/fixture-teardown-fail paths whenever external success markers drive publication.","principle":"Success evidence must be emitted after the test runner has observed hook and fixture-teardown outcomes.","root_cause":"A fragment written as the test body's last act precedes `afterEach` and fixture teardown, so a runner-failed attempt can retain a success marker.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"WS2-R3-F04","causal_section_ids":["2.1"],"check_key":"partial-settings-field-presence","description":"Live `get_ui_settings` omits unset keys and `fetchUISettings` returns `Partial<Settings>`. Calling `normalizeFontSize(remote.fontSize)` for an absent key yields the default and overwrites a valid local/previous font size during the API merge; acceptance 2.1.6 has no omitted/undefined cases.","finding_id":"WS2-R4-F04","fix":"Make persisted-root normalization return a partial object: preserve omission when `fontSize` is not an own property, normalize present finite values by clamping, and map present invalid values to the default. Add localStorage/API tests for absent and explicit undefined plus the existing malformed-root cases.","introduced_in_round":3,"location":"P2 / § 2.1","prevention":"For every normalized Partial field, test omitted, explicit undefined, null, malformed, and valid values at each merge entry.","principle":"Normalization of a partial update must distinguish an absent field from a present invalid value.","root_cause":"The new `normalizeFontSize(value: unknown)` contract gives invalid values a default fallback without specifying field-presence behavior at the settings API's partial merge boundary.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"WS2-R3-F05","causal_section_ids":["2.1"],"check_key":"app-legacy-settings-unreachable","description":"Section 2.1 says an App-level assertion proves the legacy `Settings` branch cannot render while the overlay opens. Existing `web/src/__tests__/App.test.tsx` is absent from Targets, and 2.1.7 asserts only the two hook actions.","finding_id":"WS2-R4-F05","fix":"Add `web/src/__tests__/App.test.tsx::*` as a Target and add acceptance for opening settings through App, rendering SettingsOverlay exactly once, and proving the deleted legacy branch is unreachable.","introduced_in_round":3,"location":"P2 / § 2.1","prevention":"Cross-check every promised test sentence against both Targets and a numbered acceptance item.","principle":"A promised integration regression assertion must have an exact target and acceptance item.","root_cause":"The body added an App-level legacy-branch assertion while the Targets and acceptance stop at hook-level command-palette coverage.","section_id":"2.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"WS2-R3-F09","causal_section_ids":["5.2"],"check_key":"chat-input-agent-button-consumer","description":"Deleting `input-responsive.css` in 5.2 removes `.chat-input-agent-button` styling while `ActiveAgentIndicator.tsx` still emits that class. `mobileChromeCss.test.ts` also pins the component/style relation, yet the component is absent from 5.2.","finding_id":"WS2-R4-F06","fix":"Add `web/src/components/chat/ActiveAgentIndicator.tsx::*` as a 5.2 Target, migrate the button geometry with the sanctioned composer-persona styling intact, repoint the mobileChrome guard, and include it in composer parity.","introduced_in_round":3,"location":"P5 / § 5.2","prevention":"For each retiring selector family, gcode-search every production author and test query, then reconcile the result against Targets.","principle":"Every live selector consumer must migrate in the same deliverable that deletes its stylesheet.","root_cause":"The revised composer census omitted `ActiveAgentIndicator.tsx`, which authors `.chat-input-agent-button` from `input-responsive.css`.","section_id":"5.2","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"WS2-R3-F11","causal_section_ids":["5.4","5.5"],"check_key":"activity-panel-mcp-consumer-order","description":"The new 5.4→5.5 serialization creates an intermediate parity break: `McpDetailPanel.tsx` still emits `activity-panel-action-btn` and `__label`, but 5.4 neither targets nor migrates it before deleting their sheet.","finding_id":"WS2-R4-F07","fix":"Add `McpDetailPanel.tsx::*` and its direct test seam to 5.4 for the activity-panel action/status family. Keep the component in 5.5 solely for its `mcp-tab.css` selectors and state that split explicitly.","introduced_in_round":3,"location":"P5 / §§ 5.4–5.5","prevention":"At every stylesheet-deletion edge, subtract all earlier migrated consumers from the live gcode census and require the remainder in the deletion task.","principle":"A stylesheet retirement must follow migration of every live consumer in the serialized chain.","root_cause":"`McpDetailPanel.tsx` consumes the activity-panel action-button family but first appears in 5.5, after 5.4 deletes `activity-panel.css`.","section_id":"5.4","severity":"blocking"},{"category":"traceability","causal_finding_id":"WS2-R3-F08","causal_section_ids":["5.1"],"check_key":"markdown-typography-attachment-contract","description":"`MarkdownBody` currently returns a fragment. `TaskDetailEditableCore.StaticBlock` is one of the nine `.message-content` hosts but renders plain `{value}`, so the claim that all nine render through MarkdownBody is false. Adding a wrapper inside MarkdownBody would also change direct consumers such as `ToolCallCard`, `RichContentBlocks`, and deferred `WikiAskMode` that are outside the nine-host list.","finding_id":"WS2-R4-F08","fix":"Specify a wrapper-neutral API: keep MarkdownBody's fragment output, export the canonical scoped typography utility/cva from `MarkdownBody.tsx`, apply it to the exact nine existing `.message-content` hosts, correct TaskDetailEditableCore's census description, and add direct-consumer tests proving ToolCallCard/RichContentBlocks/WikiAskMode DOM and styling remain unchanged.","introduced_in_round":3,"location":"P5 / § 5.1","prevention":"Resolve component return shape, aliases, direct consumers, and styled host elements before assigning canonical style ownership.","principle":"A shared styling migration must name the DOM attachment point and account for every direct consumer affected by that choice.","root_cause":"The census conflates `.message-content` hosts with `MarkdownBody` consumers and assumes the fragment-returning renderer owns a wrapper.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"WS2-R3-F14","causal_section_ids":["7.3"],"check_key":"settings-overlay-test-seams","description":"`SettingsOverlay.test.tsx` queries `.settings-overlay-shell__backdrop`, which 7.3 says becomes utilities, but the test is absent from Targets. The body also promises a new direct render pin in `SettingsSection.test.tsx`; neither 7.3.1 nor 7.3.2 requires it.","finding_id":"WS2-R4-F09","fix":"Add `web/src/components/settings/__tests__/SettingsOverlay.test.tsx::*` to Targets and repoint the backdrop assertion to a stable semantic/data seam. Add 7.3.3 requiring `SettingsSection.test.tsx` to cover section/subsection shells and `configFields.tsx` row/field rendering.","introduced_in_round":3,"location":"P7 / § 7.3","prevention":"Search test code for every retired selector and reconcile every new test Target with numbered acceptance.","principle":"A stylesheet retirement must migrate class-sensitive tests and acceptance-bind every new direct render pin.","root_cause":"The revised target list adds `SettingsSection.test.tsx` without acceptance and omits the existing overlay test that queries a retiring BEM hook.","section_id":"7.3","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"WS2-R3-F14","causal_section_ids":["7.3"],"check_key":"settings-section-parity-matrix","description":"7.3 migrates the shell, 13 section components, shared renderers, and specialty editors, but the single settings-overlay capture sees only the active default Appearance section. Acceptance 7.3.2 can pass while the other 12 sections and seeded specialty states regress.","finding_id":"WS2-R4-F10","fix":"Expand 1.3's settings scenario into cells derived from live `SETTINGS_SECTIONS`, with deterministic data for variables, endpoint, hubs, prompt-row, and other specialty editors; require every cell in finalizer completeness and name the full registry-backed set in 7.3.2.","introduced_in_round":3,"location":"P1 / § 1.3 and P7 / § 7.3","prevention":"For registry-backed conditional surfaces, derive parity cells from the live registry and seed specialty states before approving a whole-surface migration.","principle":"Visual-parity evidence must render every materially changed stateful surface, rather than only its default branch.","root_cause":"The capture manifest has one settings-overlay scenario while the live overlay mounts one active section at a time and defaults to Appearance.","section_id":"7.3","severity":"blocking"}],"plan_hash":"66375b62fd8a58099879dd4fd839109224765ca4c0fa07c22dd23d4eef847095","reviewer_session":"7c84aa02-c208-415d-a820-a75c9adbb48a","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"15be8dcd-f9ee-4429-9b40-10cbb6705e6b"}
```

**Human handoff** `kind: verification`

- trigger: needs_review verdict at the operator-extended review cap of 4 (evidence fb2db9c0-8190-4ddb-8c76-ea65bb9dbaec, finalized 2026-08-09)
- operator_decision: explicit handoff to expansion (user, 2026-08-09) — all 10 round-4 findings accepted and repaired, four finalized adversarial rounds complete (55 findings total, rate converged 16→15→14→10 with 9 of 10 final-round findings second-order defects in prior repairs); the operator judged the plan execution-ready without a fifth round
- route: coordinator-only derive_plan_handoff_manifest + apply_plan_handoff_manifest (manifest_digest bf2b43aee74f61b7759e42050094f6097827f7fd3d30e5e26b2524f10f07c243, 32 entries); no adversary verdict manufactured, no further review rounds
- effect: `## M1 Task Manifest` written from the drift-checked handoff derivation; expansion proceeds via start_expansion_run on #19148 under the fully interactive execution decision

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Split the chat/styles.css barrel along the chat/activity seam
  category: refactor
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.1.1: The 8 activity sheets are imported by their owning
    activity components and removed from the chat barrel. file: `web/src/components/chat/styles.css`.

    1.1.2: Activity surfaces render styled without `ChatPage` mounted. behavior: "activity
    sheets load with their owning components" in `web/src/components/activity/ActivityPanel.tsx`.

    1.1.3: The stale hatch comment is gone. file: `web/src/components/chat/styles/input-responsive.css`.

    1.1.4: ActivityPanelEmpty, SkillsTab, and IntegrationsFilterPanel each carry their
    own sheet import and render styled standalone (without ChatPage or the Rules tab
    mounted), with import-relation pins updated. test: `web/src/__tests__/mobileChromeCss.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:1.1:1.1.1
  - covers:web-styling-consolidation-phase-2:1.1:1.1.2
  - covers:web-styling-consolidation-phase-2:1.1:1.1.3
  - covers:web-styling-consolidation-phase-2:1.1:1.1.4
  tdd: false
  source_section: '1.1'
  assigned_agent: backend-developer
- title: Delete dead session CSS
  category: refactor
  task_type: feature
  depends_on:
  - '1.1'
  - '1.3'
  validation_criteria: '1.2.1: `session-primitives.css` is deleted and its index import
    removed. file: `web/src/styles/index.css`.

    1.2.2: The workflow trace icon renders via utilities. file: `web/src/components/shared/executions/execution-utils.tsx`.

    1.2.3: Both dead `.session-kill-btn` definitions are gone. file: `web/src/components/chat/styles/sessions-tab.css`.

    1.2.4: Allowlist entry dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:1.2:1.2.1
  - covers:web-styling-consolidation-phase-2:1.2:1.2.2
  - covers:web-styling-consolidation-phase-2:1.2:1.2.3
  - covers:web-styling-consolidation-phase-2:1.2:1.2.4
  tdd: false
  source_section: '1.2'
  assigned_agent: backend-developer
- title: Add the Playwright surface-capture spec
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: "1.3.1: The capture spec exists and produces the full named\
    \ matrix in one run. test: `web/tests/style-surfaces.spec.ts`.\n1.3.2: A documented\
    \ two-run before/after workflow (run, flip, run, compare by name) is described\
    \ in the spec's header comment. behavior: \"before/after capture workflow\" in\
    \ `web/tests/style-surfaces.spec.ts`.\n1.3.3: Every manifest entry asserts its\
    \ visible checkpoint and readiness callback before capturing, the run fails if\
    \ a checkpoint is absent, and the entry count is asserted against the live tab\
    \ registry. behavior: \"surface checkpoint assertion\" in `web/tests/style-surfaces.spec.ts`.\n\
    1.3.4: The grayscale subset covers state-bearing rows in both themes. behavior:\
    \ \"grayscale state subset\" in `web/tests/style-surfaces.spec.ts`.\n1.3.5: Runs\
    \ are immutable, pairable, and recoverable: attempt-scoped staging, per-cell manifest\
    \ fragments, one run-level atomic finalizer registered via Playwright globalTeardown,\
    \ overwrite refusal against finalized runs only, and a merged run-manifest JSON\
    \ with git SHA and per-PNG hashes \u2014 a failed attempt or parallel cell never\
    \ blocks a retry or corrupts a manifest. behavior: \"immutable capture runs\"\
    \ in `web/tests/style-surfaces.spec.ts`.\n1.3.6: The reduced-motion subset captures\
    \ the animation families under both preference states with computed-style suppression\
    \ assertions. behavior: \"reduced-motion subset\" in `web/tests/style-surfaces.spec.ts`.\n\
    1.3.7: Unphotographable surfaces (Traces, CodeGraphExplorer, AgentPortfolioPage)\
    \ carry recorded representative mappings with equivalence rationales in the spec.\
    \ behavior: \"representative mappings\" in `web/tests/style-surfaces.spec.ts`.\n\
    1.3.8: The finalizer selects exactly one successful fragment per expected cell\
    \ against the shared matrix-expansion roster with exact key-set equality \u2014\
    \ missing cells abort publication, unknown keys reject as foreign, retry duplicates\
    \ resolve to the highest attempt, success attestation comes from the runner-final\
    \ reporter seam, and an inactive run id makes the teardown a no-op even with stale\
    \ staging present \u2014 with coverage for missing, duplicate, retry, interruption,\
    \ parallel completion, matrix expansion, inactive-run, stale-staging, foreign-key,\
    \ and body-pass/teardown-fail outcomes. test: `web/tests/support/captureRunFinalizer.spec.ts`."
  labels:
  - covers:web-styling-consolidation-phase-2:1.3:1.3.1
  - covers:web-styling-consolidation-phase-2:1.3:1.3.2
  - covers:web-styling-consolidation-phase-2:1.3:1.3.3
  - covers:web-styling-consolidation-phase-2:1.3:1.3.4
  - covers:web-styling-consolidation-phase-2:1.3:1.3.5
  - covers:web-styling-consolidation-phase-2:1.3:1.3.6
  - covers:web-styling-consolidation-phase-2:1.3:1.3.7
  - covers:web-styling-consolidation-phase-2:1.3:1.3.8
  tdd: false
  source_section: '1.3'
  assigned_agent: backend-developer
- title: Hoist the responsive tier into the theme layer
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  validation_criteria: "1.4.1: The mobile tier is authored once in a `@theme static`\
    \ block, with the width-OR-height condition in a single custom variant whose compiled\
    \ media conditions carry literal pixel values and no `var()` reference. file:\
    \ `web/src/styles/tailwind-theme.css`.\n1.4.2: `useIsMobile` and `platform.ts`\
    \ derive their threshold from the same token rather than a hardcoded number. file:\
    \ `web/src/hooks/useIsMobile.ts`.\n1.4.3: No hardcoded 430px or 480px viewport\
    \ threshold remains in `web/src`. file: `web/src/styles/app-shell.css`.\n1.4.4:\
    \ A guard parses the compiled variant's literal media conditions and the emitted\
    \ `:root` custom properties and fails when either side moves alone. test: `web/src/__tests__/cssTokenIntegrity.test.ts`.\n\
    1.4.5: The height\u2264500px clause is live: a 932\xD7430 viewport renders the\
    \ mobile tier. behavior: \"landscape phone renders mobile tier\" in `web/src/hooks/useIsMobile.ts`.\n\
    1.4.6: Boundary tests cover 767/768, 500/501, fine-pointer landscape, live resize,\
    \ malformed token fallback, `matchMedia`-absent degradation, and listener cleanup;\
    \ device-capability exports are renamed off the layout path. test: `web/src/hooks/__tests__/useIsMobile.test.ts`."
  labels:
  - covers:web-styling-consolidation-phase-2:1.4:1.4.1
  - covers:web-styling-consolidation-phase-2:1.4:1.4.2
  - covers:web-styling-consolidation-phase-2:1.4:1.4.3
  - covers:web-styling-consolidation-phase-2:1.4:1.4.4
  - covers:web-styling-consolidation-phase-2:1.4:1.4.5
  - covers:web-styling-consolidation-phase-2:1.4:1.4.6
  tdd: true
  source_section: '1.4'
  implementation_domain: frontend
- title: Retire legacy Settings.tsx onto SettingsOverlay
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: "2.1.1: Command-palette settings actions open the overlay;\
    \ the legacy panel is unreachable. file: `web/src/components/app/useAppCommandPalette.ts`.\n\
    2.1.2: `Settings.tsx` and `settings.css` are deleted; `main.tsx` no longer imports\
    \ the sheet. file: `web/src/main.tsx`.\n2.1.3: Slider focus-ring contract is asserted\
    \ against the overlay implementation. test: `web/src/__tests__/settingsSliderFocus.test.ts`.\n\
    2.1.4: Allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.\n\
    2.1.5: Reset-to-defaults exists in AppearanceSection with a test; the aria-pressed\
    \ group-semantics assertion is ported. test: `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx`.\n\
    2.1.6: Persisted and API font-size values normalize on load with field presence\
    \ distinguished from invalidity: 12 and 24 round-trip unchanged, 48 loads as 24,\
    \ present null/string/non-finite/explicit-undefined values and a malformed root\
    \ fall back to the default, an absent `fontSize` key (localStorage and API merge)\
    \ preserves the prior valid value, and the disposition map records the legacy\
    \ 12\u201348 domain. test: `web/src/hooks/__tests__/useSettings.test.ts`.\n2.1.7:\
    \ Both command-palette settings actions open the overlay exactly once, asserted\
    \ at the hook level. test: `web/src/components/app/__tests__/useAppCommandPalette.test.tsx`.\n\
    2.1.8: Opening settings through App renders SettingsOverlay exactly once and the\
    \ deleted legacy `Settings` branch is unreachable. test: `web/src/__tests__/App.test.tsx`."
  labels:
  - covers:web-styling-consolidation-phase-2:2.1:2.1.1
  - covers:web-styling-consolidation-phase-2:2.1:2.1.2
  - covers:web-styling-consolidation-phase-2:2.1:2.1.3
  - covers:web-styling-consolidation-phase-2:2.1:2.1.4
  - covers:web-styling-consolidation-phase-2:2.1:2.1.5
  - covers:web-styling-consolidation-phase-2:2.1:2.1.6
  - covers:web-styling-consolidation-phase-2:2.1:2.1.7
  - covers:web-styling-consolidation-phase-2:2.1:2.1.8
  tdd: true
  source_section: '2.1'
  implementation_domain: frontend
- title: ui/Chip primitive
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '2.1'
  validation_criteria: '3.1.1: Chip primitive and variants exist with tone + uppercase
    API. file: `web/src/components/ui/Chip.tsx`.

    3.1.2: The session and task chip families render through Chip; the duplicate `.chip`
    selector pair is gone. file: `web/src/components/chat/styles/sessions-tab.css`.

    3.1.3: Chip has unit coverage alongside the other `ui/` tests. test: `web/src/components/ui/__tests__/Chip.test.tsx`.

    3.1.4: State-bearing Chip tones carry a non-hue cue (icon or lightness step),
    asserted rather than left to review. test: `web/src/components/ui/__tests__/Chip.test.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:3.1:3.1.1
  - covers:web-styling-consolidation-phase-2:3.1:3.1.2
  - covers:web-styling-consolidation-phase-2:3.1:3.1.3
  - covers:web-styling-consolidation-phase-2:3.1:3.1.4
  tdd: true
  source_section: '3.1'
  implementation_domain: frontend
- title: ui/Card primitive
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '3.1'
  validation_criteria: '3.2.1: Card primitive and variants exist. file: `web/src/components/ui/Card.tsx`.

    3.2.2: Initial adoptions render through Card. file: `web/src/components/activity/wiki/WikiQuickOpen.tsx`.

    3.2.3: Card has unit coverage. test: `web/src/components/ui/__tests__/Card.test.tsx`.

    3.2.4: `interactive` Cards render a semantic focusable host and do not nest interactive
    elements inside it. test: `web/src/components/ui/__tests__/Card.test.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:3.2:3.2.1
  - covers:web-styling-consolidation-phase-2:3.2:3.2.2
  - covers:web-styling-consolidation-phase-2:3.2:3.2.3
  - covers:web-styling-consolidation-phase-2:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  implementation_domain: frontend
- title: ui/FormField primitive and fields consolidation
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '3.2'
  validation_criteria: "3.3.1: FormField exists with label/hint/error/control API.\
    \ file: `web/src/components/ui/FormField.tsx`.\n3.3.2: Activity field primitives\
    \ and settings fields render through FormField and ui controls. file: `web/src/components/activity/fields/FieldPrimitives.tsx`.\n\
    3.3.3: `ui/Input` has production consumers. symbol: `Input`.\n3.3.4: FormField\
    \ has unit coverage. test: `web/src/components/ui/__tests__/FormField.test.tsx`.\n\
    3.3.5: FormField pins label-to-control association, hint/error `aria-describedby`,\
    \ and `aria-invalid` wiring. test: `web/src/components/ui/__tests__/FormField.test.tsx`.\n\
    3.3.6: `NativeSelect` exists in `ui/` on the shared focus/token/coarse-pointer\
    \ contract, and both select paths (native and Radix) are unit-tested. file: `web/src/components/ui/NativeSelect.tsx`.\n\
    3.3.7: Computed-box tests prove 44\xD744 coarse-pointer hit areas for Input, Textarea,\
    \ NativeSelect, and Radix Select trigger/items via invisible expansion, with rendered\
    \ visuals unchanged. test: `web/src/__tests__/coarsePointerTouchTargets.test.ts`.\n\
    3.3.8: `ui/Textarea` exists with ref-forwarding and auto-grow-compatibility tests.\
    \ file: `web/src/components/ui/Textarea.tsx`.\n3.3.9: A Chromium spec proves click/focus\
    \ activation at the expanded 44\xD744 perimeter under `pointer: coarse` for Input,\
    \ Textarea, NativeSelect, and Radix Select trigger/items, with visible geometry\
    \ unchanged, executed via `cd web && npx playwright test coarse-pointer-hit-areas.spec.ts`.\
    \ test: `web/tests/coarse-pointer-hit-areas.spec.ts`."
  labels:
  - covers:web-styling-consolidation-phase-2:3.3:3.3.1
  - covers:web-styling-consolidation-phase-2:3.3:3.3.2
  - covers:web-styling-consolidation-phase-2:3.3:3.3.3
  - covers:web-styling-consolidation-phase-2:3.3:3.3.4
  - covers:web-styling-consolidation-phase-2:3.3:3.3.5
  - covers:web-styling-consolidation-phase-2:3.3:3.3.6
  - covers:web-styling-consolidation-phase-2:3.3:3.3.7
  - covers:web-styling-consolidation-phase-2:3.3:3.3.8
  - covers:web-styling-consolidation-phase-2:3.3:3.3.9
  tdd: true
  source_section: '3.3'
  implementation_domain: frontend
- title: Promote TabBar into ui/
  category: refactor
  task_type: feature
  depends_on:
  - '1.4'
  - '3.3'
  validation_criteria: '3.4.1: TabBar lives in `components/ui/` with its test moved
    alongside. file: `web/src/components/ui/TabBar.tsx`.

    3.4.2: FilesPage and AgentEditForm tab strips render through TabBar; the `.sidebar-tab*`
    rules are orphaned pending the 4.2 sheet retirement. file: `web/src/components/agents/AgentEditForm.tsx`.

    3.4.3: TabBar pins tab/tablist roles, roving Arrow/Home/End focus, and keeps the
    close action out of the tab''s own activation path. test: `web/src/components/ui/__tests__/TabBar.test.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:3.4:3.4.1
  - covers:web-styling-consolidation-phase-2:3.4:3.4.2
  - covers:web-styling-consolidation-phase-2:3.4:3.4.3
  tdd: false
  source_section: '3.4'
  assigned_agent: backend-developer
- title: Agents editors sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  validation_criteria: '4.1.1: Agent editor components compose ui primitives exclusively;
    their raw-element and `*_CLS` allowlist entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.1.2: Editor sections of `agents-styles.ts` are deleted. file: `web/src/components/agents/agents-styles.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.1:4.1.1
  - covers:web-styling-consolidation-phase-2:4.1:4.1.2
  tdd: false
  source_section: '4.1'
  assigned_agent: backend-developer
- title: Agents cards and portfolio sweep
  category: refactor
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: `agents-styles.ts` is deleted and its allowlist entry
    removed. file: `web/src/components/agents/agents-styles.ts`.

    4.2.2: Portfolio filter selects follow the Select rule; agents/ raw-element entries
    are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.2.3: AgentsTabList and AgentsDetailPanel status chips render through ui/Chip;
    `SidebarPanel.css` is deleted with its importing component. file: `web/src/components/activity/agents/AgentsDetailPanel.tsx`.

    4.2.4: AgentEditForm-level tests prove focus trapping, Escape close, and focus
    restoration survive the SidebarPanel retirement. test: `web/src/components/agents/__tests__/AgentEditors.test.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.2:4.2.1
  - covers:web-styling-consolidation-phase-2:4.2:4.2.2
  - covers:web-styling-consolidation-phase-2:4.2:4.2.3
  - covers:web-styling-consolidation-phase-2:4.2:4.2.4
  tdd: false
  source_section: '4.2'
  assigned_agent: backend-developer
- title: Pipelines sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.2'
  validation_criteria: '4.3.1: `PipelineEditor.styles.ts` is deleted; pipelines raw-element
    and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.3.2: `execution-utils.tsx` styles via ui primitives and utilities. file: `web/src/components/shared/executions/execution-utils.tsx`.

    4.3.3: The pipelines `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

    4.3.4: PipelinesDefsList and PipelinesDefsDetail status chips render through ui/Chip.
    file: `web/src/components/activity/pipelines/PipelinesDefsDetail.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.3:4.3.1
  - covers:web-styling-consolidation-phase-2:4.3:4.3.2
  - covers:web-styling-consolidation-phase-2:4.3:4.3.3
  - covers:web-styling-consolidation-phase-2:4.3:4.3.4
  tdd: false
  source_section: '4.3'
  assigned_agent: backend-developer
- title: Wiki sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.3'
  validation_criteria: '4.4.1: All wiki/ raw-element allowlist entries are zero except
    the deferral-covered `WikiAskMode.tsx` entries (7 button / 1 textarea), which
    carry a comment naming the 4.11 deferral. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.4.2: The WikiQuickOpen `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.4:4.4.1
  - covers:web-styling-consolidation-phase-2:4.4:4.4.2
  tdd: false
  source_section: '4.4'
  assigned_agent: backend-developer
- title: Graph explorers sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.4'
  validation_criteria: '4.5.1: Both explorers'' raw-element and `*_CLS` entries are
    zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.5.2: Both `inputFocusAdoption` entries are removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.5:4.5.1
  - covers:web-styling-consolidation-phase-2:4.5:4.5.2
  tdd: false
  source_section: '4.5'
  assigned_agent: backend-developer
- title: FilesPage sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.5'
  validation_criteria: '4.6.1: FilesPage and FilesTab raw-element and `*_CLS` entries
    are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.6.2: The discard-confirm flow uses ConfirmDialog. file: `web/src/components/FilesPage.tsx`.

    4.6.3: FilesTab composite tree rows keep their div/keyboard-guard semantics while
    every nested native control composes a ui primitive. file: `web/src/components/activity/FilesTab.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.6:4.6.1
  - covers:web-styling-consolidation-phase-2:4.6:4.6.2
  - covers:web-styling-consolidation-phase-2:4.6:4.6.3
  tdd: false
  source_section: '4.6'
  assigned_agent: backend-developer
- title: Tasks sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.6'
  validation_criteria: '4.7.1: tasks/ raw-element and `*_CLS` entries are zero (incl.
    `taskModalStyles.ts` deleted). file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.7.2: The QuickCaptureTask `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.7:4.7.1
  - covers:web-styling-consolidation-phase-2:4.7:4.7.2
  tdd: false
  source_section: '4.7'
  assigned_agent: backend-developer
- title: Activity lists and detail panels sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.7'
  validation_criteria: '4.8.1: All listed activity files'' raw-element entries are
    zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.8.2: Filter-panel selects render through SelectField. file: `web/src/components/activity/RulesTab.tsx`.

    4.8.3: Every `.activity-chip` adopter in this sweep and the `.activity-mcp-chip`
    renderer compose ui/Chip; the orphaned `.activity-chip` rules die with their sheet
    in 5.4. file: `web/src/__tests__/styleRatchet.allowlist.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.8:4.8.1
  - covers:web-styling-consolidation-phase-2:4.8:4.8.2
  - covers:web-styling-consolidation-phase-2:4.8:4.8.3
  tdd: false
  source_section: '4.8'
  assigned_agent: backend-developer
- title: Activity chrome sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.8'
  validation_criteria: '4.9.1: One shared presentational filter-field/shell component
    serves all four filter dropdowns, each keeping its own controller with apply/reset/Escape/outside-click/focus
    semantics proven by the ported tests, and each of the four controllers mapped
    to a direct test (Sessions, Tasks, Activity immediate-select-and-close, Rules
    open/reset/Escape/outside-click/focus-return). file: `web/src/components/activity/FilterPrimitives.tsx`.

    4.9.2: Listed chrome files'' raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.9:4.9.1
  - covers:web-styling-consolidation-phase-2:4.9:4.9.2
  tdd: false
  source_section: '4.9'
  assigned_agent: backend-developer
- title: Chat, command-browser, and app-shell sweep
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.9'
  validation_criteria: '4.10.1: `RAW_ELEMENT_ALLOWLIST` input and select maps are
    empty; the textarea map holds only the deferral-covered `WikiAskMode.tsx` entry;
    the button map contains only the composer-moat entries plus the deferral-covered
    `WikiAskMode.tsx` entry (see 4.11). file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.10.2: `CLS_CONSTANT_ALLOWLIST` is empty. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    4.10.3: The ValidationDetectionEditor `inputFocusAdoption` entry is removed. test:
    `web/src/components/__tests__/inputFocusAdoption.test.ts`.

    4.10.4: The composer textarea renders through `ui/Textarea` with the composer
    look preserved. file: `web/src/components/chat/ChatInput.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:4.10:4.10.1
  - covers:web-styling-consolidation-phase-2:4.10:4.10.2
  - covers:web-styling-consolidation-phase-2:4.10:4.10.3
  - covers:web-styling-consolidation-phase-2:4.10:4.10.4
  tdd: false
  source_section: '4.10'
  assigned_agent: backend-developer
- title: Retire message.css and empty-state.css
  category: refactor
  task_type: feature
  depends_on:
  - '4.1'
  - '4.2'
  - '4.3'
  - '4.4'
  - '4.5'
  - '4.6'
  - '4.7'
  - '4.8'
  - '4.9'
  - '4.10'
  validation_criteria: '5.1.1: Both sheets are deleted with allowlist entries dropped.
    file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    5.1.2: Message markdown typography renders via utilities with parity. file: `web/src/components/chat/MessageItem.tsx`.

    5.1.3: Empty-state guard assertions target the component. test: `web/src/components/activity/__tests__/ActivityPanelEmpty.test.tsx`.

    5.1.4: Non-chat markdown surfaces (FilesPage, FilesTab, PlanReviewCard, SessionsTabDetail,
    TasksTabDetailPanel, SkillContentView, TaskDetailEditableCore, WikiPageReader)
    keep scoped markdown typography with parity after the sheet retires; MarkdownBody
    keeps its fragment output, and direct-consumer tests prove ToolCallCard, RichContentBlocks,
    and WikiAskMode DOM and styling unchanged. file: `web/src/components/shared/MarkdownBody.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:5.1:5.1.1
  - covers:web-styling-consolidation-phase-2:5.1:5.1.2
  - covers:web-styling-consolidation-phase-2:5.1:5.1.3
  - covers:web-styling-consolidation-phase-2:5.1:5.1.4
  tdd: false
  source_section: '5.1'
  assigned_agent: backend-developer
- title: Retire the chat input family
  category: refactor
  task_type: feature
  depends_on:
  - '4.10'
  - '5.1'
  validation_criteria: '5.2.1: All six input sheets are deleted with allowlist entries
    dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    5.2.2: Composer renders with visual parity across the capture matrix. behavior:
    "composer parity" in `web/tests/style-surfaces.spec.ts`.

    5.2.3: The single voice `animation: none !important` relocated from `input-voice.css:176`
    lives in accessibility.css under its own `prefers-reduced-motion` query with rationale,
    and `IMPORTANT_ALLOWLIST` moves that one count with it. file: `web/src/styles/accessibility.css`.

    5.2.4: Computed-style assertions prove reduced-motion suppression and no-preference
    animation behavior for the relocated voice families (recording, speaking/listening,
    loading, streaming), and the 1.3 reduced-motion subset passes before and after
    the move. behavior: "reduced-motion relocation" in `web/tests/style-surfaces.spec.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:5.2:5.2.1
  - covers:web-styling-consolidation-phase-2:5.2:5.2.2
  - covers:web-styling-consolidation-phase-2:5.2:5.2.3
  - covers:web-styling-consolidation-phase-2:5.2:5.2.4
  tdd: false
  source_section: '5.2'
  assigned_agent: backend-developer
- title: Retire layout.css, variables.css, and the chat barrel
  category: refactor
  task_type: feature
  depends_on:
  - '5.1'
  - '5.2'
  validation_criteria: '5.3.1: `layout.css`, `variables.css`, and `chat/styles.css`
    are deleted with allowlist entries dropped. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    5.3.2: The tool-code-surface override survives in base.css and tool cards render
    flat code backgrounds. file: `web/src/styles/base.css`.

    5.3.3: Import-order-dependent behavior is gone from chat styling (no cross-sheet
    duplicate selectors remain). behavior: "no duplicate selectors" in `web/src/components/chat/ChatPage.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:5.3:5.3.1
  - covers:web-styling-consolidation-phase-2:5.3:5.3.2
  - covers:web-styling-consolidation-phase-2:5.3:5.3.3
  tdd: false
  source_section: '5.3'
  assigned_agent: backend-developer
- title: Retire sessions-tab.css and activity-panel.css
  category: refactor
  task_type: feature
  depends_on:
  - '4.2'
  - '4.3'
  - '4.9'
  - '5.3'
  validation_criteria: '5.4.1: Both sheets are deleted with allowlist entries dropped
    and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    5.4.2: `.activity-filter-button` has exactly one authoring site. behavior: "single
    filter-button authoring site" in `web/src/components/activity/TasksTabToolbar.tsx`.

    5.4.3: Typography-ladder pins assert against components. test: `web/src/components/activity/__tests__/typographyLadder.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:5.4:5.4.1
  - covers:web-styling-consolidation-phase-2:5.4:5.4.2
  - covers:web-styling-consolidation-phase-2:5.4:5.4.3
  tdd: false
  source_section: '5.4'
  assigned_agent: backend-developer
- title: Retire the small activity tab sheets
  category: refactor
  task_type: feature
  depends_on:
  - '4.3'
  - '4.6'
  - '4.8'
  - '5.4'
  validation_criteria: '5.5.1: All seven sheets are deleted with allowlist entries
    dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    5.5.2: The shared inline filter panel serves Rules, Skills, and Integrations from
    the presentational InlineFilterPanel in one implementation, with direct render
    coverage. file: `web/src/components/activity/FilterPrimitives.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:5.5:5.5.1
  - covers:web-styling-consolidation-phase-2:5.5:5.5.2
  tdd: false
  source_section: '5.5'
  assigned_agent: backend-developer
- title: Retire task-execution.css and task-detail.css
  category: refactor
  task_type: feature
  depends_on:
  - '4.7'
  - '4.8'
  - '5.5'
  validation_criteria: '5.6.1: Both sheets are deleted with allowlist entries dropped
    and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    5.6.2: Coarse-pointer 44px promotion for task rows is asserted via compiled utilities.
    test: `web/src/__tests__/coarsePointerTouchTargets.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:5.6:5.6.1
  - covers:web-styling-consolidation-phase-2:5.6:5.6.2
  tdd: false
  source_section: '5.6'
  assigned_agent: backend-developer
- title: Remove important:true behind the screenshot gate
  category: config
  task_type: feature
  depends_on:
  - '5.1'
  - '5.2'
  - '5.3'
  - '5.4'
  - '5.5'
  - '5.6'
  validation_criteria: '6.1.1: `important: true` is gone. file: `web/tailwind.config.ts`.

    6.1.2: Before/after capture pairs across the full matrix show exact parity against
    the immutable post-1.4 baseline; 1.4 remains the sole rendered-output exemption
    plan-wide. behavior: "matrix parity review" in `web/tests/style-surfaces.spec.ts`.

    6.1.3: The style guide reflects the new cascade. file: `docs/guides/frontend-style-guide.md`.'
  labels:
  - covers:web-styling-consolidation-phase-2:6.1:6.1.1
  - covers:web-styling-consolidation-phase-2:6.1:6.1.2
  - covers:web-styling-consolidation-phase-2:6.1:6.1.3
  tdd: true
  source_section: '6.1'
  assigned_agent: backend-developer
- title: Retire segmented-control.css and dropdown-caret.css
  category: refactor
  task_type: feature
  depends_on:
  - '6.1'
  validation_criteria: '7.1.1: Both sheets are deleted; the primitives self-style.
    file: `web/src/components/ui/SegmentedControl.tsx`.

    7.1.2: Allowlist entries dropped; `main.tsx` imports removed. file: `web/src/main.tsx`.'
  labels:
  - covers:web-styling-consolidation-phase-2:7.1:7.1.1
  - covers:web-styling-consolidation-phase-2:7.1:7.1.2
  tdd: false
  source_section: '7.1'
  assigned_agent: backend-developer
- title: Retire app-shell.css
  category: refactor
  task_type: feature
  depends_on:
  - '7.1'
  validation_criteria: '7.2.1: `app-shell.css` is deleted with its allowlist entry;
    header renders with parity in both tiers. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    7.2.2: Header pins live as component assertions. test: `web/src/__tests__/mobileChromeCss.test.ts`.

    7.2.3: The `.impeccable.md` app-header/canonical-cluster clause references component-owned
    styling, updated via teach mode in this deliverable. behavior: "app-header contract
    matches shipped architecture" in `.impeccable.md`.'
  labels:
  - covers:web-styling-consolidation-phase-2:7.2:7.2.1
  - covers:web-styling-consolidation-phase-2:7.2:7.2.2
  - covers:web-styling-consolidation-phase-2:7.2:7.2.3
  tdd: false
  source_section: '7.2'
  assigned_agent: backend-developer
- title: Retire settings-overlay.css
  category: refactor
  task_type: feature
  depends_on:
  - '6.1'
  - '7.2'
  validation_criteria: "7.3.1: `settings-overlay.css` is deleted with its allowlist\
    \ entry and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.\n\
    7.3.2: Settings overlay renders with parity across the capture matrix for every\
    \ registry-derived section cell \u2014 one per live `SETTINGS_SECTIONS` entry\
    \ with seeded specialty-editor states (variables, endpoint, hubs, prompt-row)\
    \ \u2014 as defined in 1.3. behavior: \"settings overlay parity\" in `web/tests/style-surfaces.spec.ts`.\n\
    7.3.3: The shared renderers carry direct render pins: section/subsection shells\
    \ in `SettingsSection.test.tsx` and `configFields.tsx` row/field rendering, plus\
    \ the SettingsOverlay backdrop assertion re-pointed to a stable semantic seam.\
    \ test: `web/src/components/settings/sections/__tests__/SettingsSection.test.tsx`."
  labels:
  - covers:web-styling-consolidation-phase-2:7.3:7.3.1
  - covers:web-styling-consolidation-phase-2:7.3:7.3.2
  - covers:web-styling-consolidation-phase-2:7.3:7.3.3
  tdd: false
  source_section: '7.3'
  assigned_agent: backend-developer
- title: Load-order rationalization
  category: refactor
  task_type: feature
  depends_on:
  - '7.2'
  - '7.3'
  validation_criteria: '7.4.1: `main.tsx` has exactly three style-bearing imports
    (two fonts + index.css). file: `web/src/main.tsx`.

    7.4.2: The import pins assert the final order. test: `web/src/__tests__/mobileChromeCss.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:7.4:7.4.1
  - covers:web-styling-consolidation-phase-2:7.4:7.4.2
  tdd: false
  source_section: '7.4'
  assigned_agent: backend-developer
- title: Simplify the ratchet to pure bans
  category: test
  task_type: feature
  depends_on:
  - '7.1'
  - '7.2'
  - '7.3'
  - '7.4'
  validation_criteria: '8.1.1: Allowlists are empty or pinned floors with moat-linked
    comments. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

    8.1.2: The ratchet test enforces the end state and passes. test: `web/src/__tests__/styleRatchet.test.ts`.'
  labels:
  - covers:web-styling-consolidation-phase-2:8.1:8.1.1
  - covers:web-styling-consolidation-phase-2:8.1:8.1.2
  tdd: false
  source_section: '8.1'
  assigned_agent: backend-developer
- title: Update the style guide and design contract
  category: docs
  task_type: feature
  depends_on:
  - '7.1'
  - '7.2'
  - '7.3'
  - '7.4'
  validation_criteria: '8.2.1: The style guide documents the end state. file: `docs/guides/frontend-style-guide.md`.

    8.2.2: The design contract''s component references match the shipped architecture.
    behavior: "Canonical Components reflect component-owned styling" in `.impeccable.md`.'
  labels:
  - covers:web-styling-consolidation-phase-2:8.2:8.2.1
  - covers:web-styling-consolidation-phase-2:8.2:8.2.2
  tdd: false
  source_section: '8.2'
  assigned_agent: tech-writer
```
