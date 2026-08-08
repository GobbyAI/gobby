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

**Goal**: Structural prerequisites and early wins — CSS ownership made explicit, dead CSS deleted, the responsive tier hoisted to one token, the screenshot harness in place.

### 1.1 Split the chat/styles.css barrel along the chat/activity seam [category: refactor]

`kind: deliverable`

Targets:
- `activity/ActivityMcpTab.tsx`
- `activity/ActivityPanel.tsx`
- `activity/CronTab.tsx`
- `activity/FilesTab.tsx`
- `activity/PipelinesTab.tsx`
- `activity/RulesTab.tsx`
- `activity/SessionsTab.tsx`
- `activity/TracesTab.tsx`
- `web/src/components/chat/styles.css`
- `web/src/components/chat/styles/input-responsive.css`
- `TaskBadges.tsx`
- `TasksTabDetailPanel.tsx`
- `activity-panel.css`
- `cron-tab.css`
- `files-tab.css`
- `mcp-tab.css`
- `pipelines-tab.css`
- `rules-tab.css`
- `sessions-tab.css`
- `traces-tab.css`

`ChatPage.tsx:1` imports `chat/styles.css`, whose 13-sheet `@import` chain loads 8 sheets that style `components/activity/` surfaces (48% of the barrel's lines): `activity-panel.css`, `sessions-tab.css`, `mcp-tab.css`, `rules-tab.css`, `files-tab.css`, `cron-tab.css`, `traces-tab.css`, `pipelines-tab.css`. Any activity surface rendered without `ChatPage` mounted is unstyled, and per-sheet retirement is impossible to bisect.

- Move each activity sheet's import to its owning component (side-effect import at the consumer, matching the existing `TaskBadges.tsx:4` / `TasksTabDetailPanel.tsx:1` pattern): `activity-panel.css` → `activity/ActivityPanel.tsx`; `sessions-tab.css` → `activity/SessionsTab.tsx`; `mcp-tab.css` → `activity/ActivityMcpTab.tsx`; `rules-tab.css` → `activity/RulesTab.tsx`; `files-tab.css` → `activity/FilesTab.tsx`; `cron-tab.css` → `activity/CronTab.tsx`; `traces-tab.css` → `activity/TracesTab.tsx`; `pipelines-tab.css` → `activity/PipelinesTab.tsx`.
- `chat/styles.css` keeps: `variables.css`, `layout.css`, `message.css`, `input.css`, `empty-state.css` imports plus the `.tool-code-surface` rule. (`empty-state.css` serves both chat and activity empty states; keep it in the chat barrel and note the split in a comment — it retires in 5.1.)
- `.activity-filter-panel` rules currently in `rules-tab.css` are consumed by `SkillsTab.tsx:338` and `integrations/IntegrationsFilterPanel.tsx:18` too — they ride along with `rules-tab.css` and are noted for 5.5.
- Delete the stale `!important` comment paragraph in `web/src/components/chat/styles/input-responsive.css` (~lines 100–102) — the hatch it documents was already removed.
- No selector, rule, or emitted-bundle change beyond import relocation; visual parity exact.

**Acceptance:**

- 1.1.1 - The 8 activity sheets are imported by their owning activity components and removed from the chat barrel. file: `web/src/components/chat/styles.css`.
- 1.1.2 - Activity surfaces render styled without `ChatPage` mounted. behavior: "activity sheets load with their owning components" in `web/src/components/activity/ActivityPanel.tsx`.
- 1.1.3 - The stale hatch comment is gone. file: `web/src/components/chat/styles/input-responsive.css`.

### 1.2 Delete dead session CSS [category: refactor]

`kind: deliverable`

Targets:
- `styles/index.css`
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/shared/executions/execution-utils.tsx::*` — scope-reason: workflow-trace icon utilities and the execution card/badge/button styling both migrate onto ui primitives
- `web/src/styles/index.css`
- `web/src/styles/session-primitives.css`

`session-primitives.css` (229 lines, imported from `styles/index.css`) has exactly one live consumer: `.workflow-trace-icon` (line 226), used at `web/src/components/shared/executions/execution-utils.tsx:310`. The other ~215 lines (`.session-item*`, `.session-dot*`, `.session-name*`, `.session-badge`, `.session-kill-btn`, `.session-delete-btn`, `.terminals-*`, `.session-group*`) have zero component consumers anywhere in `web/src`. `.session-kill-btn` is also defined in `sessions-tab.css:236-253` — both definitions are dead.

- Convert the `execution-utils.tsx:310` call site to Tailwind utilities equivalent to the `.workflow-trace-icon` rule; delete `session-primitives.css` entirely; remove its `@import` from `styles/index.css`.
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
- `web/playwright.config.ts`

A checked-in capture harness producing the fixed screenshot matrix used as before/after evidence for every risky step (especially 6.1). Chrome DevTools stays the ad-hoc debugging tool; this spec is the repeatable gate.

- **Surface-scenario manifest, not a surface list.** Every entry declares its route, its seeded state, and one visible checkpoint the run asserts before capturing. A tab that renders empty or still-loading produces a stable screenshot while showing none of the controls a migration touches — the checkpoint is what makes the capture meaningful. The manifest is built from the **live** `ACTIVITY_PANEL_TABS` registry (`ActivityPanelTabs.tsx`), currently 16 tabs: sessions, terminal, tasks, mcp, agents, stages, skills, memory, integrations, wiki, rules, plans, changes, files, pipelines, cron. Full entry list (24): chat; composer; the 16 tabs; agents tab with the editor panel open (heaviest sweep, 4.1); memory tab in graph view (KnowledgeGraph, 4.5); `FilesPage` (4.6); settings overlay; login; mobile toolbar state. The spec asserts its entry count against the manifest so a registry change fails loudly.
- **Explicit representative mappings** for surfaces the matrix cannot photograph, each with a recorded equivalence rationale in the spec: Traces is deliberately hidden from the tab strip (moat 66e919e3, #19152) — `TracesTab` migration (4.8) is covered by its component tests; `CodeGraphExplorer` and `AgentPortfolioPage` have zero production mounts (test-only imports) — their sweeps (4.5, 4.2) are covered by component tests and the KnowledgeGraph capture as the graph-chrome representative. Wiki Ask mode is excluded (4.11 deferral).
- **Determinism per entry:** fixed seed data via the existing API/WebSocket mock patterns in `web/tests/`, frozen clock, `document.fonts.ready` awaited, and an asserted `matchMedia` state for the pointer axis so a mis-emulated descriptor fails loudly instead of capturing the wrong tier.
- **State coverage where the surface owns it:** entries for focused/open (dropdown, dialog, filter panel) and long-content/overflow states on the surfaces that own those affordances, so migrations to those code paths are actually photographed.
- Matrix: dark and light theme × fine and coarse pointer (`hasTouch` + touch descriptor) × reference viewports 1440×900 (desktop), 440×956 (portrait), 932×430 (landscape — exercises the height≤500px mobile-tier clause, which only becomes a real tier after 1.4).
- **Grayscale subset:** state-bearing rows (task/session/pipeline status, error and success surfaces) captured desaturated in both themes — the repeatable form of the deutan contract check, scoped to a subset rather than doubling the full matrix.
- **Reduced-motion subset:** the animation families (voice recording, speaking/listening, loading, streaming) captured under `prefers-reduced-motion: reduce` plus a no-preference control, with computed-style assertions that the suppression actually holds — the executable form of the reduced-motion contract exercised at 5.2 and 6.1.
- **Immutable, pairable runs:** each run writes to its own gitignored directory named by run label (git SHA + `before`/`after`), refuses to overwrite an existing label, and emits a run-manifest JSON recording git SHA, plan section under test, scenario list, and a SHA-256 per PNG — so a retry can never silently replace baseline evidence. Stable file names inside a run (`<surface>--<state>--<theme>--<pointer>--<viewport>.png`) pair across runs by name; no committed baselines and no pixel-diff gate (per decision — human review of pairs).
- **Per-scenario readiness:** each manifest entry declares a readiness callback (beyond the visible checkpoint) that settles asynchronous descendants — lazy content, streaming placeholders, animation-driven layout — before capture; fresh browser context per matrix cell.
- Reuse existing `playwright.config.ts` (web server auto-start, `PLAYWRIGHT_BASE_URL` override) and existing fixture patterns from `web/tests/`.
- Tag the spec so it is excluded from any default CI test run (manual/opt-in execution), matching how existing live specs are handled.

**Acceptance:**

- 1.3.1 - The capture spec exists and produces the full named matrix in one run. test: `web/tests/style-surfaces.spec.ts`.
- 1.3.2 - A documented two-run before/after workflow (run, flip, run, compare by name) is described in the spec's header comment. behavior: "before/after capture workflow" in `web/tests/style-surfaces.spec.ts`.
- 1.3.3 - Every manifest entry asserts its visible checkpoint and readiness callback before capturing, the run fails if a checkpoint is absent, and the entry count is asserted against the live tab registry. behavior: "surface checkpoint assertion" in `web/tests/style-surfaces.spec.ts`.
- 1.3.4 - The grayscale subset covers state-bearing rows in both themes. behavior: "grayscale state subset" in `web/tests/style-surfaces.spec.ts`.
- 1.3.5 - Runs are immutable and pairable: label-named run directories, overwrite refusal, and a run-manifest JSON with git SHA and per-PNG hashes. behavior: "immutable capture runs" in `web/tests/style-surfaces.spec.ts`.
- 1.3.6 - The reduced-motion subset captures the animation families under both preference states with computed-style suppression assertions. behavior: "reduced-motion subset" in `web/tests/style-surfaces.spec.ts`.
- 1.3.7 - Unphotographable surfaces (Traces, CodeGraphExplorer, AgentPortfolioPage) carry recorded representative mappings with equivalence rationales in the spec. behavior: "representative mappings" in `web/tests/style-surfaces.spec.ts`.

### 1.4 Hoist the responsive tier into the theme layer [category: code]

`kind: deliverable`

Targets:
- `web/src/styles/tailwind-theme.css`
- `web/src/hooks/useIsMobile.ts::useIsMobile`
- `web/src/utils/platform.ts::*` — scope-reason: the hardcoded 768 viewport check folds onto the shared tier-token read; the file's indexed symbols are types only
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
- **One reactive geometry predicate; device heuristics stay out of it.** A single validated width-or-height query builder is the only source of layout-tier truth in JS, and it is reactive (`matchMedia` change listeners), matching the CSS variant exactly. `platform.ts`'s cached touch/user-agent classification is a *device-capability* signal: it must not feed geometry tokens or tier decisions, and its exports are renamed so they cannot be mistaken for layout signals. The builder validates the token values it reads and falls back to the authored defaults (with a console warning) on a malformed or missing token rather than composing a query that never matches.
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

### 2.1 Retire legacy Settings.tsx onto SettingsOverlay [category: code]

`kind: deliverable`

Targets:
- `./styles/settings.css`
- `styles/settings.css`
- `web/src/__tests__/settingsSliderFocus.test.ts::*` — scope-reason: replaced wholesale with render-based focus assertions against the overlay slider
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/Settings.tsx::*` — scope-reason: the whole file is deleted
- `web/src/components/__tests__/Settings.test.tsx::*` — scope-reason: the legacy test file is retired with a per-assertion disposition map
- `web/src/components/app/useAppCommandPalette.ts::useAppCommandPalette`
- `web/src/components/settings/sections/AppearanceSection.tsx::AppearanceSection`
- `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx::*` — scope-reason: gains the ported aria-pressed group semantics and reset-to-defaults assertions
- `web/src/main.tsx`
- `web/src/styles/settings.css`
- `App.tsx`
- `mobileChromeCss.test.ts`

The legacy panel is still live: rendered at `App.tsx:684-691` behind `settingsOpen`, opened from two command-palette actions (`web/src/components/app/useAppCommandPalette.ts:117` and `:196`), in parallel with the new `SettingsOverlay` (cog button → `settingsOverlay.open()`, `App.tsx:514`).

- **Control disposition map** (every legacy control gets an explicit destination; the overlay surface itself must not change rendered output): theme options (aria-pressed group) → AppearanceSection theme control; Default Mode group (`Settings.tsx:96-105`, aria-pressed) → ChatVoiceSection default-chat-mode select (deliberate semantics change on an already-shipped surface); font-size slider (drives `--font-size-base`, bounds 12–24) → AppearanceSection slider (already covered); **reset-to-defaults (`reset-button`, `Settings.tsx:115`) has no overlay equivalent — port it into AppearanceSection** as a reset action with a test.
- **Test disposition map** for `web/src/components/__tests__/Settings.test.tsx` (8 assertions): focus-first/Escape-close, forward/backward focus trap, and focus-restore are already covered by `SettingsOverlay.test.tsx:107/116/138` — retire with this mapping recorded in the commit; "labels setting groups and marks selected options as pressed" **ports** into `AppearanceSection.test.tsx` (theme group) and `ChatVoiceSection.test.tsx` (mode select present); legacy-model-selector-absent and both voice-controls-absent assertions retire (negative assertions about the deleted surface; placement covered by ChatVoiceSection tests).
- Redirecting the two command-palette actions must produce identical rendered output on the overlay — the redirect changes what opens, never how the overlay renders (1.4 stays the sole parity exemption).
- Repoint both `useAppCommandPalette.ts` actions to `settingsOverlay.open()`; delete the `settingsOpen` state and the `<Settings>` render block from `App.tsx`.
- Delete `web/src/components/Settings.tsx` and `web/src/styles/settings.css` (276 lines; includes ~75 already-dead lines — `.settings-stack`, `.settings-row*`, `.model-select*`, `.loading-text`, `.no-models-text`); remove the `./styles/settings.css` import from `main.tsx`.
- Replace `web/src/__tests__/settingsSliderFocus.test.ts` (postcss-parses `settings.css` at module scope — it throws once the file is gone) with an equivalent render-based focus-ring assertion on the SettingsOverlay slider: no bare `outline` on rest state, `:focus-visible` ring using `var(--accent)` per the WCAG focus contract.
- Ratchet: drop `Settings.tsx` raw-element entries (4 button, 1 input), the `settings.css` `CSS_FILE_ALLOWLIST` entry; this batch deletes >200 CSS lines → lower ceiling in the same commit. Update `mobileChromeCss.test.ts` import expectations for the removed `main.tsx` import.

**Acceptance:**

- 2.1.1 - Command-palette settings actions open the overlay; the legacy panel is unreachable. file: `web/src/components/app/useAppCommandPalette.ts`.
- 2.1.2 - `Settings.tsx` and `settings.css` are deleted; `main.tsx` no longer imports the sheet. file: `web/src/main.tsx`.
- 2.1.3 - Slider focus-ring contract is asserted against the overlay implementation. test: `web/src/__tests__/settingsSliderFocus.test.ts`.
- 2.1.4 - Allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 2.1.5 - Reset-to-defaults exists in AppearanceSection with a test; the aria-pressed group-semantics assertion is ported. test: `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx`.

## P3: New Primitives

`kind: framing`

**Goal**: Five primitive additions land in `web/src/components/ui/` — four new (Chip, Card, FormField, NativeSelect) plus TabBar promoted from `shared/` — each replacing every competing implementation it unifies. The app ends with **two sanctioned select paths**: Radix `ui/Select` for toolbar/picker contexts and `ui/NativeSelect` composed by `SelectField` for form contexts (the 3.3 rule). Primitives follow the Button pattern: component + separate `*Variants.ts` cva recipe, tokens only (no raw colors), coarse-pointer flow-through, focus rings per `focusStyles.ts`. Every interactive primitive carries an executable 44×44 coarse-pointer hit-area contract (see 3.3).

### 3.1 ui/Chip primitive [category: code]

`kind: deliverable`

Targets:
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTab.helpers.tsx::*` — scope-reason: the uppercase tone-chip renderers across the helpers migrate onto Chip
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/chat/styles/activity-panel.css`
- `web/src/components/chat/styles/mcp-tab.css`
- `web/src/components/tasks/task-execution.css`
- `web/src/components/ui/Chip.tsx`
- `web/src/components/ui/chipVariants.ts`
- `web/src/components/ui/__tests__/Chip.test.tsx`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

Four parallel status-chip implementations exist (plus the agents tag-inputs, excluded below). Create `Chip.tsx` + `chipVariants.ts`:

- API: `tone: neutral | accent | info | warning | error` (state palette, icon/lightness-first per `.impeccable.md`), `uppercase?: boolean` (default false — preserves the session-chip `text-transform: uppercase` delta over task chips), `asChild` via Radix Slot. **No `size` prop** — all four status-chip families are geometrically identical (verified 2026-08-08: `height 1.25rem; padding-inline .375rem; border-radius 9999px; font-size var(--text-2xs); font-weight 600; white-space nowrap`), so Chip ships one geometry.
- **Excluded from Chip's scope:** `AGENT_RULES_CHIP_*` (agents-styles.ts:76) is a removable tag-input token (`text-sm`, `rounded-xl`, embedded remove button) — a different species; it migrates to call-site utilities in the 4.2 sweep.
- Adopt at all chip renderers: `.chip`/`.chip--*` in `activity/SessionsTab.helpers.tsx:104-153` (uppercase tone chips + inline warning chip); `.activity-chip`/`--accent/--info/--warning/--error` across the 14+ list/detail components (agents, integrations, memory, pipelines, rules, skills, stages surfaces); `AGENT_RULES_CHIP*` and `STEP_CHIP*` constants from `agents/agents-styles.ts` (chip *display* usages — editable chip-input rows may compose Chip with a remove Button); task chips in `TaskBadges.tsx` (`TASK_BADGE_CLS` + `.chip--state/--priority/--type` modifiers become tone + className); `.activity-mcp-chip` (`mcp-tab.css:72`). (Wiki citation chips are out of scope with the Ask surface — see 4.11.)
- Delete the `.chip` rule blocks from `sessions-tab.css` and `task-execution.css` and `.activity-chip` from `activity-panel.css` as their consumers migrate (the sheets themselves retire in P5; deleting the rules here resolves the import-order-dependent duplicate).
- Ratchet/guards: shrink `CLS_CONSTANT_ALLOWLIST` for `TaskBadges.tsx`; visual parity per surface; `ActivityRowStatusDot` untouched (dots are not chips).

**Acceptance:**

- 3.1.1 - Chip primitive and variants exist with tone + uppercase API. file: `web/src/components/ui/Chip.tsx`.
- 3.1.2 - All five chip families render through Chip; the duplicate `.chip` selector pair is gone. file: `web/src/components/chat/styles/sessions-tab.css`.
- 3.1.3 - Chip has unit coverage alongside the other `ui/` tests. test: `web/src/components/ui/__tests__/Chip.test.tsx`.
- 3.1.4 - State-bearing Chip tones carry a non-hue cue (icon or lightness step), asserted rather than left to review. test: `web/src/components/ui/__tests__/Chip.test.tsx`.

### 3.2 ui/Card primitive [category: code]

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

### 3.3 ui/FormField primitive and fields consolidation [category: code]

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
- `web/src/components/ui/__tests__/FormField.test.tsx`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

Six labeled-form-row implementations exist. Create `FormField.tsx`: label + optional hint/error + control slot (`useId` wiring, `aria-describedby`), the shell equivalent of today's `fieldShellClass = "flex flex-col gap-1.5"` / `labelClass` / `controlClass` trio.

- Rebuild `activity/fields/FieldPrimitives.tsx` (TextField, SecretField, NumberField, TextAreaField, SelectField, TagsField) on `FormField` + `ui/Input` / `ui/Textarea` / `ui/NativeSelect` — this finally gives `ui/Input` (currently zero consumers) its adoption path. Remove the duplicated class trio from `DateTimeField.tsx:13-15`.
- Migrate `settings/fields/*` (`StringListField`, `KeyValueMapField`, `TypedListField`, `BoundedSelectField`) and `settings/sections/configFields.tsx` onto the same primitives; their hand-rolled label/row markup goes away.
- The remaining field variants (`agents-styles.ts` `AGENT_EDIT_FIELD/LABEL/HINT/INPUT`, `PipelineEditor.styles.ts` `FIELD_*`, `ValidationDetectionEditor` `FORM_FIELD_CLS` family) migrate in their P4 surface sweeps onto these primitives.
- Select consolidation decision encoded here: **form contexts use `SelectField`, which composes the new `ui/NativeSelect`; toolbar/picker contexts use Radix `ui/Select`**. That is the whole-app rule the P4 sweeps apply. `NativeSelect` is the smallest boundary that keeps the native-select behavior form contexts want while satisfying the standing rule that raw `<select>` lives only inside `components/ui` — a native select rendered directly by `FieldPrimitives.tsx` would remain a raw element outside `ui/` and could never reach the ratchet end state.
- Ratchet: `FieldPrimitives.tsx` raw-element entries (1 button, 4 input, 1 select, 1 textarea) drop to zero, with the select composing inside `ui/`; settings-section input entries shrink.
- **Executable 44×44 coarse-pointer contract, per primitive.** `Input`, `Textarea`, `NativeSelect`, and Radix `Select` triggers/items currently sit at 36px; under `pointer: coarse` each primitive supplies an invisible hit-area expansion (pseudo-element or padding compensation) reaching 44×44 without changing rendered visuals — parity-safe by construction. `coarsePointerTouchTargets.test.ts` gains computed-box assertions for each primitive and for representative migrated compositions as the P4 sweeps land; dense `Button` compositions are constrained to documented moats that supply an equivalent target. The Button size×dense ladder itself stays untouched.

**Acceptance:**

- 3.3.1 - FormField exists with label/hint/error/control API. file: `web/src/components/ui/FormField.tsx`.
- 3.3.2 - Activity field primitives and settings fields render through FormField and ui controls. file: `web/src/components/activity/fields/FieldPrimitives.tsx`.
- 3.3.3 - `ui/Input` has production consumers. symbol: `Input`.
- 3.3.4 - FormField has unit coverage. test: `web/src/components/ui/__tests__/FormField.test.tsx`.
- 3.3.5 - FormField pins label-to-control association, hint/error `aria-describedby`, and `aria-invalid` wiring. test: `web/src/components/ui/__tests__/FormField.test.tsx`.
- 3.3.6 - `NativeSelect` exists in `ui/` on the shared focus/token/coarse-pointer contract, and both select paths (native and Radix) are unit-tested. file: `web/src/components/ui/NativeSelect.tsx`.
- 3.3.7 - Computed-box tests prove 44×44 coarse-pointer hit areas for Input, Textarea, NativeSelect, and Radix Select trigger/items via invisible expansion, with rendered visuals unchanged. test: `web/src/__tests__/coarsePointerTouchTargets.test.ts`.

### 3.4 Promote TabBar into ui/ [category: refactor]

`kind: deliverable`

Targets:
- `web/src/components/shared/TabBar.tsx::*` — scope-reason: the whole file moves to ui/ (94 lines, roving focus, currently 1 raw button)
- `web/src/components/shared/__tests__/TabBar.test.tsx::*` — scope-reason: the test moves alongside the component to ui/__tests__/
- `web/src/components/shared/SidebarPanel.css`
- `web/src/components/ui/TabBar.tsx`
- `web/src/components/ui/__tests__/TabBar.test.tsx`
- `web/src/components/FilesPage.tsx::*` — scope-reason: the line-281 tab strip adopts TabBar with the new onTabClose slot
- `web/src/components/agents/AgentEditForm.tsx::*` — scope-reason: the sidebar tab strip (lines 271-281) adopts TabBar
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `mobileChromeCss.test.ts`

`shared/TabBar.tsx` (94 lines, roving focus, `role="tablist"`, zero production consumers) is the blessed tab strip. Move it to `components/ui/` (with its test), then adopt:

- `FilesPage.tsx:281` tab strip (`TABS_CLS`/`TAB_CLS`/`TAB_ACTIVE_CLS`/`TAB_NAME_CLS`/`TAB_CLOSE_CLS` — the only other `role="tablist"`; needs a per-tab close affordance, so extend TabBar with an optional `onTabClose`/actions slot rather than forking).
- `shared/SidebarPanel.css` `.sidebar-tab-bar`/`.sidebar-tab*` (consumer: `agents/AgentEditForm.tsx:271-281`) — adopt TabBar, delete `SidebarPanel.css` (44 lines) and its `CSS_FILE_ALLOWLIST` entry; update the `coarsePointerTouchTargets`-adjacent `.sidebar-tab` pin in `mobileChromeCss.test.ts`.
- `AgentPickerDropdown` scope toggle (`SCOPE_TOGGLE_CLS` family) — migrate to `SegmentedControl` (it is a value toggle, not navigation) during 4.10; noted here so TabBar's scope stays navigation-only.
- `.activity-panel-tab-strip` (`activity-panel.css:22`) adoption happens in 5.4 with that sheet's retirement.

**Acceptance:**

- 3.4.1 - TabBar lives in `components/ui/` with its test moved alongside. file: `web/src/components/ui/TabBar.tsx`.
- 3.4.2 - FilesPage and AgentEditForm tab strips render through TabBar; `SidebarPanel.css` is deleted. file: `web/src/components/shared/SidebarPanel.css`.
- 3.4.3 - TabBar pins tab/tablist roles, roving Arrow/Home/End focus, and keeps the close action out of the tab's own activation path. test: `web/src/components/ui/__tests__/TabBar.test.tsx`.

## P4: Surface Sweeps — raw elements and *_CLS to the sanctioned floor

`kind: framing`

**Goal**: Every unsanctioned raw `<button>`/`<input>`/`<select>`/`<textarea>` composes a `ui/` primitive; every `*_CLS` constant folds into cva recipes or call-site utilities; per-file allowlist entries hit zero as each sweep lands. Counts below are the 2026-07-29 snapshot — re-read the live allowlist per leaf. Each sweep: migrate, delete constants, shrink allowlist entries (and `inputFocusAdoption` entries where named), verify visual parity, run full web validation.

### 4.1 Agents editors sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/agents/agents-styles.ts`
- `web/src/components/agents/AgentEditForm.tsx::*` — scope-reason: 25 raw elements and the AGENT_* constant consumers migrate onto primitives across the form
- `web/src/components/agents/AgentRulesEditor.tsx::*` — scope-reason: 16 raw elements plus rules-chip constant usage migrate
- `web/src/components/agents/AgentStepsEditor.tsx::*` — scope-reason: 20 raw elements plus step-card constants migrate
- `web/src/components/agents/AgentVariablesEditor.tsx::*` — scope-reason: 7 raw elements migrate
- `web/src/components/agents/AgentSkillsEditor.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/agents/AgentToolBlocksEditor.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/agents/IsolationTargetSelector.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable

The heaviest single surface: `AgentEditForm.tsx` (8 btn / 7 input / 9 select / 1 textarea), `AgentRulesEditor.tsx` (11 btn / 2 input / 3 select), `AgentStepsEditor.tsx` (10 btn / 4 input / 3 select / 3 textarea), `AgentVariablesEditor.tsx` (5 btn / 2 input), `AgentSkillsEditor.tsx`, `AgentToolBlocksEditor.tsx`, `IsolationTargetSelector.tsx`. Styling from `agents-styles.ts` (113 `*_CLS`, ~258 lines): `AGENT_BTN*` → `Button` variants; `AGENT_EDIT_FIELD/LABEL/HINT/INPUT` → FormField + ui controls; chips already on Chip (3.1); step cards → Card; selects per the 3.3 rule. Delete `agents-styles.ts` sections as they empty; the editor-facing sections should empty here.

**Acceptance:**

- 4.1.1 - Agent editor components compose ui primitives exclusively; their raw-element and `*_CLS` allowlist entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.1.2 - Editor sections of `agents-styles.ts` are deleted. file: `web/src/components/agents/agents-styles.ts`.

### 4.2 Agents cards and portfolio sweep [category: refactor] (depends: 4.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/agents/AgentPortfolioPage.tsx::*` — scope-reason: portfolio-wide sweep of raw elements and card/filter styling onto primitives
- `web/src/components/agents/agents-styles.ts`
- `web/src/components/activity/agents/AgentsTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/shared/SidebarPanel.tsx::*` — scope-reason: the component is retired and its shell folds into AgentEditForm utilities
- `web/src/components/shared/__tests__/SidebarPanel.test.tsx::*` — scope-reason: the a11y assertions (focus trap, Escape, focus restore) port to AgentEditForm-level tests as the component retires
- `web/src/__tests__/mobileChromeCss.test.ts::*` — scope-reason: guard-test pins on named sheets and import order are re-pointed as those sheets and imports change

`AgentPortfolioPage.tsx` (2 btn / 2 select incl. `.agent-filter-select`), the `AGENT_DEF_CARD_*` / `STEP_CARD_*` families → Card, remaining `agents-styles.ts` content deleted, file removed entirely with its `CLS_CONSTANT_ALLOWLIST` entry (113 → 0). `AGENT_RULES_CHIP_*` tag-inputs (excluded from Chip per 3.1) become call-site utilities here. `SidebarPanel.tsx` (1 btn) — **retire the component** (verified 2026-08-08: `AgentEditForm` is its only production consumer): fold the panel shell into AgentEditForm utilities, port the SidebarPanel a11y test assertions (focus trap, Escape close, focus restore) into AgentEditForm-level tests, and remove the SidebarPanel allowlist entries.

**Acceptance:**

- 4.2.1 - `agents-styles.ts` is deleted and its allowlist entry removed. file: `web/src/components/agents/agents-styles.ts`.
- 4.2.2 - Portfolio filter selects follow the Select rule; agents/ raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

### 4.3 Pipelines sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `shared/executions/execution-utils.tsx`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/pipelines/PipelineEditor.styles.ts`
- `web/src/components/activity/pipelines/PipelineEditor.tsx::*` — scope-reason: editor form, buttons, and textarea migrate onto FormField/ui controls
- `web/src/components/activity/pipelines/PipelineStepFields.tsx::*` — scope-reason: 14 raw controls migrate onto FormField/ui controls
- `web/src/components/activity/pipelines/PipelineStepList.tsx::*` — scope-reason: 8 raw controls and step-card styling migrate
- `web/src/components/activity/pipelines/PipelinesDefsList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/shared/executions/execution-utils.tsx::*` — scope-reason: workflow-trace icon utilities and the execution card/badge/button styling both migrate onto ui primitives
- `web/src/components/activity/PipelinesTab.tsx::*` — scope-reason: tab raw buttons and execution styling migrate to primitives
- `web/src/components/__tests__/inputFocusAdoption.test.ts`

`PipelineEditor.tsx` (3 btn / 1 input / 1 textarea), `PipelineStepFields.tsx` (2 btn / 10 input / 2 textarea), `PipelineStepList.tsx` (6 btn / 1 input / 1 select), `PipelinesDefsList.tsx` (1 btn), `web/src/components/activity/PipelinesTab.tsx` (3 btn — `RAW_ELEMENT_ALLOWLIST` line 33; owned here so it does not survive into the endgame floor). `PipelineEditor.styles.ts` (47 `*_CLS`): `BTN_CLS`/`BTN_PRIMARY_CLS` → Button; `FIELD_*` → FormField; `STEP_CLS`/`ADD_DROPDOWN_CLS` → Card; `KV_*` rows → utilities. Delete the file (removes both its `CLS_CONSTANT_ALLOWLIST` entry and its `inputFocusAdoption` entry). `shared/executions/execution-utils.tsx` (20 `*_CLS`: run buttons, badges → Button/Badge/Chip, step cards → Card) sweeps here too since PipelinesTab consumes it.

**Acceptance:**

- 4.3.1 - `PipelineEditor.styles.ts` is deleted; pipelines raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.3.2 - `execution-utils.tsx` styles via ui primitives and utilities. file: `web/src/components/shared/executions/execution-utils.tsx`.
- 4.3.3 - The pipelines `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.4 Wiki sweep [category: refactor] (depends: P3)

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
- `web/src/components/__tests__/inputFocusAdoption.test.ts`

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

### 4.5 Graph explorers sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/code-graph/CodeGraphExplorer.tsx::*` — scope-reason: explorer chrome sweep folds 32 _CLS constants and raw controls into primitives; graph logic untouched but interleaved through the file
- `web/src/components/activity/memory/KnowledgeGraph.tsx::*` — scope-reason: 19 _CLS constants and 10 raw controls across the graph chrome migrate; canvas logic untouched
- `web/src/components/__tests__/inputFocusAdoption.test.ts`

`CodeGraphExplorer.tsx` (32 `*_CLS`, 6 btn, 5 input) and `activity/memory/KnowledgeGraph.tsx` (19 `*_CLS`, 5 btn, 5 input): controls/search/legend/physics panels → Button, Input, Card, utilities at call site. Both files carry `inputFocusAdoption` entries — removed on migration. Canvas/graph rendering logic untouched.

**Acceptance:**

- 4.5.1 - Both explorers' raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.5.2 - Both `inputFocusAdoption` entries are removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.6 FilesPage sweep [category: refactor] (depends: P3)

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

### 4.7 Tasks sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/tasks/TaskCreateForm.tsx::*` — scope-reason: form-wide migration of fields, selects, and buttons onto FormField/ui controls
- `web/src/components/tasks/QuickCaptureTask.tsx::*` — scope-reason: 12 _CLS constants and 3 raw controls migrate; its inputFocusAdoption entry is removed
- `web/src/components/tasks/taskModalStyles.ts`
- `web/src/components/activity/TaskFieldEditors.tsx::*` — scope-reason: 5 raw controls including the inline-edit select migrate onto FormField/ui controls
- `web/src/components/activity/TaskCloseDialog.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TaskTreeRow.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/__tests__/inputFocusAdoption.test.ts`

`TaskCreateForm.tsx` (14 `*_CLS`, 3 btn / 2 input / 4 select / 2 textarea), `QuickCaptureTask.tsx` (12 `*_CLS`, 2 btn / 1 input, `inputFocusAdoption` entry), `taskModalStyles.ts` (3), `TaskBadges.tsx` (3 — Chip from 3.1), `TaskFieldEditors.tsx` (1 btn / 2 input / 1 select / 1 textarea incl. `.task-inline-edit--select`), `TaskCloseDialog.tsx` (1 textarea), `TaskTreeRow.tsx` (2 btn). Modals → Dialog primitives; forms → FormField + ui controls; selects per rule.

**Acceptance:**

- 4.7.1 - tasks/ raw-element and `*_CLS` entries are zero (incl. `taskModalStyles.ts` deleted). file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.7.2 - The QuickCaptureTask `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.8 Activity lists and detail panels sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: filter-panel selects, the local filter dropdown, and rules-tab styling all migrate to SelectField/primitives
- `web/src/components/activity/SkillsTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/skills/SkillsHubView.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/skills/SkillsInstalledList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
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

The 1–4-count long tail across activity surfaces: `RulesTab.tsx` (1 btn / 4 select), `SkillsTab.tsx` (1 btn / 2 select), `SkillsHubView.tsx` (1 btn / 1 input / 1 select), `SkillsInstalledList.tsx`, `MemoryTab.tsx` (1 btn / 3 input), `MemoryDetailPanel.tsx`, `MemoryTabList.tsx`, `IntegrationsTab.tsx`, `IntegrationsFilterPanel.tsx` (2 select), `ChannelDetailPanel.tsx` (1 btn / 1 input), `ChannelsList.tsx`, `StagesTab.tsx`, `StagesList.tsx`, `ProfilesList.tsx`, `TracesTab.tsx` (2 btn), `CronTab.tsx` (2 btn), `FileChangesTab.tsx` (2 btn), `ActivityMcpTab.tsx` (4 btn), `WikiTab` covered by 4.4, `TaskDetailKV.tsx`, `TaskDetailRelationships.tsx`, `TasksTabDetailPanel.tsx`, `AgentsTabList.tsx`, `PlanReviewCard.tsx`, `RulesTabList.tsx`, `KeyValueField.tsx` (2 btn / 2 input), `DateTimeField.tsx` (1 input). Unclassed selects styled by `.activity-filter-panel__field select` descend from the filter panels — migrate to `SelectField` so 5.5 can delete those descendant rules.

**Acceptance:**

- 4.8.1 - All listed activity files' raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.8.2 - Filter-panel selects render through SelectField. file: `web/src/components/activity/RulesTab.tsx`.

### 4.9 Activity chrome sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/ActivityFilterDropdown.tsx::*` — scope-reason: becomes the single shared filter-dropdown implementation absorbing three sibling variants
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: panel chrome, local dropdown, and sheet-import relocation all land here
- `web/src/components/activity/FilterPrimitives.tsx::*` — scope-reason: filter controls rebuilt on ui primitives inside the consolidated dropdown
- `web/src/components/activity/SessionsFilterDropdown.tsx::*` — scope-reason: 6 raw controls fold into the consolidated shared filter dropdown
- `web/src/components/activity/TasksTabFilters.tsx::*` — scope-reason: its FilterDropdown mirror is absorbed by the shared implementation
- `web/src/components/activity/ActivityPanelSearch.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/QuickMenu.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionInteractionModal.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTabDetail.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/SessionsTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/terminal/TerminalKeysBar.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/terminal/TerminalView.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable

Panel chrome and popup dropdowns: `ActivityPanel.tsx` (2 btn, local `ActivityDropdown`), `ActivityFilterDropdown.tsx` (1 btn), `SessionsFilterDropdown.tsx` (1 btn / 5 input), `TasksTabFilters.tsx` (`FilterDropdown` mirror), the local `RulesFilterDropdown` (`RulesTab.tsx:44`), `ActivityPanelSearch.tsx` (1 input), `web/src/components/activity/FilterPrimitives.tsx` (1 input — `RAW_ELEMENT_ALLOWLIST` line 119; the shared filter-dropdown consolidation below is its natural owner), `QuickMenu.tsx` (2 btn), `SessionInteractionModal.tsx` (2 btn / 1 textarea), `SessionsTab*.tsx` (3 btn), `terminal/TerminalKeysBar.tsx` (2 btn / 1 input), `terminal/TerminalView.tsx` (2 btn). Consolidate the three near-identical filter dropdowns (`ActivityFilterDropdown`, `SessionsFilterDropdown`, `TasksTabFilters.FilterDropdown`, `RulesFilterDropdown`) into one shared filter-dropdown component composing Button + DropdownCaret + FormField controls — four implementations to one.

**Acceptance:**

- 4.9.1 - One shared filter-dropdown component serves all four former implementations. file: `web/src/components/activity/ActivityFilterDropdown.tsx`.
- 4.9.2 - Listed chrome files' raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

### 4.10 Chat, command-browser, and app-shell sweep [category: refactor] (depends: P3)

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
- `web/src/components/shared/DiffBlock.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/shared/MermaidBlock.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/__tests__/inputFocusAdoption.test.ts`

Remaining files: `App.tsx` (1 btn), `ProjectSelector.tsx` (2 btn / 1 input), `web/src/components/chat/CommandBar.tsx` (1 btn — `RAW_ELEMENT_ALLOWLIST` line 96), `web/src/components/chat/PlanApprovalActions.tsx` (1 textarea — line 191), `ValidationDetectionEditor.tsx` (9 `*_CLS`, 1 btn / 1 input / 1 textarea, `inputFocusAdoption` entry), `auth/LoginPage.tsx` (1 btn / 3 input), `AppErrorBoundary.tsx` (2 btn), chat: `ProviderPicker.tsx` (3 btn), `BranchIndicator.tsx` (3 btn), `AgentPickerDropdown.tsx` (11 `*_CLS`, 4 btn — scope toggle → SegmentedControl per 3.4), `ChatCommandPalette.tsx` (1 btn), `CommandPalette.tsx` (1 input), `ResumeSessionModal.tsx` (1 btn / 2 input), `ActiveAgentIndicator.tsx` (1 btn), `CodeBlockRenderers.tsx` (1 btn), `ToolResultImage.tsx` (1 btn), `ChatInputModelControls.tsx`/`ChatInputToolbar.tsx` (non-composer entries), command-browser: `ToolBrowserModal.tsx` (4 btn), `SkillBrowserModal.tsx` (3 btn), `ToolArgumentForm.tsx` (1 each btn/input/select/textarea), settings: `SettingsOverlay.tsx` (2 btn), `PromptsTemplatesSection.tsx` (1 btn / 1 input), remaining section inputs, `shared/DiffBlock.tsx`, `shared/MermaidBlock.tsx` (1 btn each), `chat/ToolCallCard.tsx` (1 input + 2 header buttons — **all three migrate**: the expandable-header composite semantics are the sanctioned part, its nested native buttons are not, per the Constraints floor). **The only sanctioned pinned entries are the composer icon buttons** (`ChatInput.tsx`, `ChatInputQueuedFiles.tsx`, `ChatInputPrimaryButton.tsx`, `ChatInputModelControls.tsx` composer instances — moat 05198494). The moat covers **buttons only**: `web/src/components/chat/ChatInput.tsx` also carries a textarea entry (`RAW_ELEMENT_ALLOWLIST` line 190) sitting beside its sanctioned button entry (line 91). That textarea migrates to `ui/Textarea` here; only the button entry survives. FilesTab's nested controls are owned by 4.6.

**Acceptance:**

- 4.10.1 - `RAW_ELEMENT_ALLOWLIST` input and select maps are empty; the textarea map holds only the deferral-covered `WikiAskMode.tsx` entry; the button map contains only the composer-moat entries plus the deferral-covered `WikiAskMode.tsx` entry (see 4.11). file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.10.2 - `CLS_CONSTANT_ALLOWLIST` is empty. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.10.3 - The ValidationDetectionEditor `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.
- 4.10.4 - The composer textarea renders through `ui/Textarea` with the composer look preserved. file: `web/src/components/chat/ChatInput.tsx`.

## P5: BEM Sheet Retirement

`kind: framing`

**Goal**: Every legacy BEM sheet empties into utilities/cva and is deleted, with its `CSS_FILE_ALLOWLIST` entry and guard-test pins. Ceiling lowers with each >200-line batch. Order: cheapest and most isolated first.

### 5.1 Retire message.css and empty-state.css [category: refactor] (depends: P4)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/MessageItem.tsx::*` — scope-reason: markdown typography utilities are applied at the component as message.css retires
- `web/src/components/chat/styles/message.css`
- `web/src/components/chat/styles/empty-state.css`
- `web/src/components/chat/styles.css`
- `web/src/components/chat/MessageList.tsx::*` — scope-reason: its empty-state classes move to component utilities as the sheet retires
- `ActivityPanelEmpty.test.tsx`
- `typographyLadder.test.ts`

`message.css` (205 lines) is pure `.message-content <element>` markdown typography — the cleanest whole-file kill: express it as a scoped set of Tailwind descendant utilities (`[&_h1]:…`) in a constant or cva applied by `MessageItem.tsx`, honoring the ~65–75ch prose cap. `empty-state.css` (78 lines): `.activity-tab-empty*` becomes a small `ActivityEmptyState` presentational component (already componentized — move classes to utilities inside it); `.chat-empty-state*` (`MessageList.tsx:281-290`) and the orphan `.command-palette-empty` migrate to utilities. Update `ActivityPanelEmpty.test.tsx` (drops its two source-regex assertions on the sheet, keeps structure/copy/adoption pins) and `typographyLadder.test.ts` (empty-state pins re-point at the component).

**Acceptance:**

- 5.1.1 - Both sheets are deleted with allowlist entries dropped. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.1.2 - Message markdown typography renders via utilities with parity. file: `web/src/components/chat/MessageItem.tsx`.
- 5.1.3 - Empty-state guard assertions target the component. test: `web/src/components/activity/__tests__/ActivityPanelEmpty.test.tsx`.

### 5.2 Retire the chat input family [category: refactor] (depends: 4.10)

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
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `mobileChromeCss.test.ts`
- `planApprovalDesign.test.tsx`

`input-base.css` (398), `input-composer.css` (263), `input-voice.css` (187), `input-responsive.css` (151), `input-status.css` (18), `input.css` barrel (5) — 1,022 lines onto the composer components (`ChatInput`, `ChatInputToolbar`, `ChatInputModelControls`, `ChatInputVoiceControls`, `ChatInputPrimaryButton`, `AgentStatusBar`, `VoiceStatusBar`, `ChatCommandPalette`). Composer icon buttons keep their purpose-built look (moat) as scoped utilities. Container queries move to `@container` utilities / the components' own scoped styles. The `input-voice.css:176` `animation: none !important` relocates to `web/src/styles/accessibility.css` with a justification comment (reduced-motion class). Guard updates: `mobileChromeCss.test.ts` chat container-query pins and `planApprovalDesign.test.tsx` `.agent-status-bar` source assertions re-point or convert to JSX/computed-style assertions; `coarsePointerTouchTargets` fixture hooks that referenced these sheets move to compiled-Tailwind candidates.

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
- `mobileChromeCss.test.ts`
- `typographyLadder.test.ts`
- `planApprovalDesign.test.tsx`

`layout.css` (468): `.chat-container/-messages/-page/-main`, `.command-bar*`, `.message*` shells, `.mobile-chat-drawer`, and the full-screen `.command-palette-*` family → utilities on `ChatMainColumn`, `CommandBar`, `MessageList`/`MessageItem`, `CommandPalette`. `variables.css` (12): its four alias custom properties (`--bg-code`, `--bg-muted`, `--border-color`, `--accent-color`) have **zero consumers** anywhere in `web/src` (verified 2026-08-08) — delete the sheet outright with the `mobileChromeCss` alias-only assertion; nothing inlines and nothing graduates. The barrel `styles.css` (32): the `.tool-code-surface` `!important` rule (beats react-syntax-highlighter's inline style — must survive) relocates to `web/src/styles/base.css` with its #14721 comment; barrel deleted. `IMPORTANT_ALLOWLIST` moves the entry accordingly. `mobileChromeCss` `.command-bar` pins re-point; `typographyLadder` `.command-bar-btn` pin re-points; `planApprovalDesign` `.command-bar` assertion re-points. Naming hazard resolved: the chat-input dropdown formerly `.command-palette` (input-base) and the modal `.command-palette-*` (layout) end as component-scoped utilities, killing the collision. **Precondition (why this depends on 5.1 AND 5.2):** at deletion time the barrel must import only `layout.css` and `variables.css` — 5.1 removes its retained empty-state member and 5.2 removes its input-family chain; deleting the barrel earlier leaves `ChatPage.tsx` importing a file whose members still exist.

**Acceptance:**

- 5.3.1 - `layout.css`, `variables.css`, and `chat/styles.css` are deleted with allowlist entries dropped. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.3.2 - The tool-code-surface override survives in base.css and tool cards render flat code backgrounds. file: `web/src/styles/base.css`.
- 5.3.3 - Import-order-dependent behavior is gone from chat styling (no cross-sheet duplicate selectors remain). behavior: "no duplicate selectors" in `web/src/components/chat/ChatPage.tsx`.

### 5.4 Retire sessions-tab.css and activity-panel.css [category: refactor] (depends: 4.9)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/activity-panel.css`
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: panel chrome utilities land on the component as its sheet retires
- `web/src/components/activity/SessionsTabList.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `mobileChromeCss.test.ts`
- `typographyLadder.test.ts`
- `planApprovalDesign.test.tsx`

`activity-panel.css` (622): `.activity-panel*` shell/tabs/toolbar/status-bar/mobile chrome → utilities on `ActivityPanel`/`ActivityActionsContext`; `.activity-panel-tab-strip` → TabBar; `.activity-chip` already dead (3.1); `.activity-filter-button` consolidates to a single authoring site (Button variant + utilities; its `task-execution.css` and `rules-tab.css` fragments die with those sheets). `sessions-tab.css` (561): `.session-entry*` and remaining rules → utilities on `SessionsTabList`/helpers. Guard updates: `mobileChromeCss` activity-toolbar pins, `typographyLadder` `.activity-row-*`/status-bar pins re-point to components, `planApprovalDesign` `.activity-panel-tabs` assertion, `coarsePointerTouchTargets` hooks (`.activity-panel-mobile-menu__item`) move to compiled utilities.

**Acceptance:**

- 5.4.1 - Both sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.4.2 - `.activity-filter-button` has exactly one authoring site. behavior: "single filter-button authoring site" in `web/src/components/activity/TasksTabToolbar.tsx`.
- 5.4.3 - Typography-ladder pins assert against components. test: `web/src/components/activity/__tests__/typographyLadder.test.ts`.

### 5.5 Retire the small activity tab sheets [category: refactor] (depends: 4.8)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: filter-panel selects, the local filter dropdown, and rules-tab styling all migrate to SelectField/primitives
- `web/src/components/chat/styles/files-tab.css`
- `web/src/components/chat/styles/mcp-tab.css`
- `web/src/components/chat/styles/rules-tab.css`
- `web/src/components/chat/styles/cron-tab.css`
- `web/src/components/chat/styles/traces-tab.css`
- `web/src/components/chat/styles/pipelines-tab.css`
- `web/src/components/activity/skills/SkillsTab.css`
- `web/src/components/activity/SkillsTab.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `typographyLadder.test.ts`

`files-tab.css` (300), `mcp-tab.css` (239), `rules-tab.css` (212 incl. `.activity-filter-panel` used by Skills + Integrations — becomes a shared filter-panel component or utilities), `cron-tab.css` (61), `traces-tab.css` (36), `pipelines-tab.css` (35), plus `activity/skills/SkillsTab.css` (3). Import owners updated per sheet (each was relocated to its owning component in 1.1; the owning component's side-effect import is removed as its sheet dies). Consumers: FilesTab/FilesPage/FileChangesTab, McpDetailPanel/ActivityMcpTab, RulesTab, CronTab, TracesTab, PipelinesTab. Guard updates: `typographyLadder` file-tree and cron pins re-point; `coarsePointerTouchTargets` hooks from these sheets move to utilities.

**Acceptance:**

- 5.5.1 - All seven sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.5.2 - The shared filter panel serves Rules, Skills, and Integrations from one implementation. file: `web/src/components/activity/RulesTab.tsx`.

### 5.6 Retire task-execution.css and task-detail.css [category: refactor] (depends: 4.7)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/tasks/task-execution.css`
- `web/src/components/activity/taskdetail/task-detail.css`
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/components/activity/TasksTabDetailPanel.tsx::*` — scope-reason: raw controls and styling in this file migrate onto ui primitives/utilities in this deliverable
- `web/src/__tests__/coarsePointerTouchTargets.test.ts::*` — scope-reason: fixture hooks referencing the retired sheets move to compiled-Tailwind candidates
- `mobileChromeCss.test.ts`
- `typographyLadder.test.ts`
- `planApprovalDesign.test.tsx`

`task-execution.css` (738 — largest sheet; import owner `TaskBadges.tsx:4` removed with it; `task-detail.css` import owner `TasksTabDetailPanel.tsx:1` likewise): `.chip` block already dead (3.1); `.activity-task-*` rows/panes/toolbars → utilities on TaskTreeRow/TasksTab components; `.activity-filter-*` fragments consolidated per 5.4. `activity/taskdetail/task-detail.css` (346): detail header/KV/relationships → utilities; `.task-inline-edit--select` already migrated (4.7). Guard updates: `typographyLadder` task pins and `PRIORITY_TEXT_WEIGHTS` stay component-side; `coarsePointerTouchTargets` `.task-more-btn`/`.activity-task-row-toggle`/`.activity-task-detail-edit-error__dismiss` hooks move to compiled utilities; `mobileChromeCss`/`planApprovalDesign` references re-point.

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
- `web/tailwind.config.ts`

Highest-risk step. Preconditions met by P5: surviving CSS is enumerable (token infra + hook sheets + settings-overlay.css). Procedure:

1. Run the 1.3 capture matrix (before) — an immutable labeled run including the grayscale and reduced-motion subsets.
2. Remove `important: true` from `web/tailwind.config.ts` (file becomes content-scanning only).
3. Verify the six surviving intentional `!important` declarations still hold — all six beat an inline style or serve reduced-motion, so none of them depends on the flag. By this point they sit in two files: `base.css` carries the four-declaration reduced-motion block plus the `.tool-code-surface` background relocated in 5.3, and `accessibility.css` carries the voice `animation: none` relocated in 5.2. That is the same six the plan opens with (`chat/styles.css` 1 + `input-voice.css` 1 + `base.css` 4), redistributed from three files to two by sheet retirement — no declaration is added or removed by this phase.
4. Run the matrix (after) as a second immutable labeled run; review every pair; fix any regression at its source (specificity at the component, never a new `!important`), then re-run until pairs match exactly.
5. Sweep for utility-vs-hook-sheet conflicts: the remaining hook sheets (`app-shell.css`, `segmented-control.css`, `dropdown-caret.css`, `settings-overlay.css`) are the only stylesheets that can now out-specificity utilities — audit their selectors against utility-bearing elements they touch.

Update `docs/guides/frontend-style-guide.md` anti-patterns wording ("Tailwind utilities are already configured with `important: true`" — no longer true).

**Acceptance:**

- 6.1.1 - `important: true` is gone. file: `web/tailwind.config.ts`.
- 6.1.2 - Before/after capture pairs across the full matrix show exact parity against the immutable post-1.4 baseline; 1.4 remains the sole rendered-output exemption plan-wide. behavior: "matrix parity review" in `web/tests/style-surfaces.spec.ts`.
- 6.1.3 - The style guide reflects the new cascade. file: `docs/guides/frontend-style-guide.md`.

## P7: Hook Sheets, Overlay, and Load Order

`kind: framing`

**Goal**: The last non-infra stylesheets fold into components; `main.tsx` carries no side-effect CSS beyond fonts and `index.css`.

### 7.1 Retire segmented-control.css and dropdown-caret.css [category: refactor] (depends: 6.1)

`kind: deliverable`

Targets:
- `web/src/components/ui/SegmentedControl.tsx::*` — scope-reason: the primitive absorbs its stylesheet as utilities/cva while keeping the control-height contract
- `web/src/main.tsx`
- `web/src/styles/segmented-control.css`
- `web/src/styles/dropdown-caret.css`
- `web/src/components/ui/DropdownCaret.tsx::*` — scope-reason: the primitive absorbs its stylesheet as utilities
- `mobileChromeCss.test.ts`

38 lines total move into their owning primitives: `.segmented-control*` rules → `SegmentedControl.tsx` utilities/cva (keeping the `--control-row-height` contract and the `mobileChromeCss` option-padding pins as component assertions); `.dropdown-caret` → `DropdownCaret.tsx`. Drop both `main.tsx` imports; update `mobileChromeCss.test.ts` (its segmented-control-before-app-shell sheet-order assertion retires; segmented-control pins convert to component assertions).

**Acceptance:**

- 7.1.1 - Both sheets are deleted; the primitives self-style. file: `web/src/components/ui/SegmentedControl.tsx`.
- 7.1.2 - Allowlist entries dropped; `main.tsx` imports removed. file: `web/src/main.tsx`.

### 7.2 Retire app-shell.css [category: refactor] (depends: 7.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/styles/app-shell.css`
- `web/src/main.tsx`
- `.impeccable.md`
- `mobileChromeCss.test.ts`

157 lines of header-cluster hooks (`app-*` classes sizing the theme-toggle/cog/logout cluster, project-selector responsive swap, health badge). Express as utilities/cva on the App header components while preserving the canonical-cluster contract from `.impeccable.md` (equal icon widths via `size="icon"`, `--status-bar-control-height` row, coarse-pointer hit-area expansion, mobile collapse to a single settings entry). `mobileChromeCss` app-header pins convert to JSX/component assertions. **The `.impeccable.md` app-header contract updates in this same deliverable via the impeccable skill's teach mode** — the canonical-cluster clause stops referencing `app-shell.css` hook selectors and points at component-owned styling, so contract and implementation never disagree between phases. The style guide's "sanctioned exception" paragraph for hook sheets is rewritten in 8.2.

**Acceptance:**

- 7.2.1 - `app-shell.css` is deleted with its allowlist entry; header renders with parity in both tiers. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 7.2.2 - Header pins live as component assertions. test: `web/src/__tests__/mobileChromeCss.test.ts`.
- 7.2.3 - The `.impeccable.md` app-header/canonical-cluster clause references component-owned styling, updated via teach mode in this deliverable. behavior: "app-header contract matches shipped architecture" in `.impeccable.md`.

### 7.3 Retire settings-overlay.css [category: refactor] (depends: 6.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/styles/settings-overlay.css`
- `web/src/components/settings/SettingsOverlay.tsx::*` — scope-reason: shell, section, and specialty-editor rules land as utilities on the overlay components as the sheet retires
- `main.tsx`
- `mobileChromeCss.test.ts`

712 lines across `SettingsOverlay.tsx`, `WorkflowVariablesEditor.tsx`, `settings/fields/*`, and the 13 section components. Much of the field/row styling is already superseded by FormField adoption (3.3) — delete superseded rules first, then migrate the shell (`.settings-overlay-shell*`), sections (`.settings-section*`, `.settings-subsection*`), and specialty editors (`.settings-variables*`, `.settings-endpoint-editor`, `.settings-hubs-field*`, `.settings-prompt-row`, `.appearance-font-size*`) to utilities. Work in 2–3 commits (shell → sections → specialty) to stay bisectable. Drop the `main.tsx` import; lower ceiling (>200 lines).

**Acceptance:**

- 7.3.1 - `settings-overlay.css` is deleted with its allowlist entry and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 7.3.2 - Settings overlay renders with parity across the capture matrix. behavior: "settings overlay parity" in `web/tests/style-surfaces.spec.ts`.

### 7.4 Load-order rationalization [category: refactor] (depends: 7.2, 7.3)

`kind: deliverable`

Targets:
- `web/src/main.tsx`
- `index.css`
- `mobileChromeCss.test.ts`

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

- Rewrite `docs/guides/frontend-style-guide.md`: ui/ inventory gains Chip, Card, FormField, **NativeSelect**, TabBar — documenting the two sanctioned select paths (Radix `ui/Select` for toolbar/picker, `ui/NativeSelect` via `SelectField` for forms); the Legacy CSS Files section becomes the end-state contract (six infra sheets, everything else utilities/cva); the hook-sheet sanctioned exception is removed; the Style Debt Ratchet section documents the ban-plus-floor model with the exact-pin line total; anti-pattern wording updated post-flip.
- Update `.impeccable.md` Canonical Components via the impeccable skill's teach mode: the ui/ inventory and select-path rule above land in the contract; the app-header clause was already updated in 7.2 (this pass verifies it still matches and finishes any remaining segmented-control/component-owned styling references). This file is edited through the skill, per project rule.

**Acceptance:**

- 8.2.1 - The style guide documents the end state. file: `docs/guides/frontend-style-guide.md`.
- 8.2.2 - The design contract's component references match the shipped architecture. behavior: "Canonical Components reflect component-owned styling" in `.impeccable.md`.

## V2 End-to-End Verification

`kind: verification`

- `cd web && npm run test && npm run type-check && npm run lint && npm run lint:tokens` — green at every phase boundary; the ratchet proves recorded debt is exact at each step and the ban-state at the end.
- Playwright matrix (`web/tests/style-surfaces.spec.ts`): before/after parity review at 1.4 (re-baseline, not parity), 5.2, 5.4, 6.1, 7.2, 7.3 minimum; full-matrix final pass across every manifest entry (24 at authoring; count asserted against the live tab registry) × dark/light × fine/coarse × three reference viewports, every entry asserting its visible checkpoint, in immutable labeled runs with per-PNG hashes.
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

