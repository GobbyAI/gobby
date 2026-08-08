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
- **Canonical component standards** live in `.impeccable.md` (Canonical Components); it is edited only via the impeccable skill's teach mode. Sanctioned non-native-button exceptions (FilesTab composite rows, ToolCallCard expandable header, chat composer icon buttons — moat 05198494) are preserved, so the raw-element endgame is a pinned sanctioned floor, not literal zero.
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

- **Surface-scenario manifest, not a surface list.** Each of the 17 surfaces is an entry declaring its route, its seeded state, and one visible checkpoint the run asserts before capturing. A tab that renders empty or still-loading produces a stable screenshot while showing none of the controls a migration touches — the checkpoint is what makes the capture meaningful. Surfaces: chat + composer; the twelve activity tabs (tasks, sessions, pipelines, cron, files, traces, changes, plans, mcp, rules, skills, wiki); settings overlay; login; mobile toolbar state.
- **Determinism per entry:** fixed seed data via the existing API/WebSocket mock patterns in `web/tests/`, frozen clock, `document.fonts.ready` awaited, and an asserted `matchMedia` state for the pointer axis so a mis-emulated descriptor fails loudly instead of capturing the wrong tier.
- **State coverage where the surface owns it:** entries for focused/open (dropdown, dialog, filter panel) and long-content/overflow states on the surfaces that own those affordances, so migrations to those code paths are actually photographed.
- Matrix: dark and light theme × fine and coarse pointer (`hasTouch` + touch descriptor) × reference viewports 1440×900 (desktop), 440×956 (portrait), 932×430 (landscape — exercises the height≤500px mobile-tier clause, which only becomes a real tier after 1.4).
- **Grayscale subset:** state-bearing rows (task/session/pipeline status, error and success surfaces) captured desaturated in both themes — the repeatable form of the deutan contract check, scoped to a subset rather than doubling the full matrix.
- Output to a gitignored run directory with stable file names (`<surface>--<state>--<theme>--<pointer>--<viewport>.png`) so before/after runs diff by name; no committed baselines and no pixel-diff gate (per decision — human review of pairs).
- Reuse existing `playwright.config.ts` (web server auto-start, `PLAYWRIGHT_BASE_URL` override) and existing fixture patterns from `web/tests/`.
- Tag the spec so it is excluded from any default CI test run (manual/opt-in execution), matching how existing live specs are handled.

**Acceptance:**

- 1.3.1 - The capture spec exists and produces the full named matrix in one run. test: `web/tests/style-surfaces.spec.ts`.
- 1.3.2 - A documented two-run before/after workflow (run, flip, run, compare by name) is described in the spec's header comment. behavior: "before/after capture workflow" in `web/tests/style-surfaces.spec.ts`.
- 1.3.3 - Every one of the 17 manifest entries asserts its visible checkpoint before capturing, and the run fails if a checkpoint is absent. behavior: "surface checkpoint assertion" in `web/tests/style-surfaces.spec.ts`.
- 1.3.4 - The grayscale subset covers state-bearing rows in both themes. behavior: "grayscale state subset" in `web/tests/style-surfaces.spec.ts`.

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
- `web/src/components/app/useAppCommandPalette.ts::useAppCommandPalette`
- `web/src/main.tsx`
- `web/src/styles/settings.css`
- `App.tsx`
- `mobileChromeCss.test.ts`

The legacy panel is still live: rendered at `App.tsx:684-691` behind `settingsOpen`, opened from two command-palette actions (`web/src/components/app/useAppCommandPalette.ts:117` and `:196`), in parallel with the new `SettingsOverlay` (cog button → `settingsOverlay.open()`, `App.tsx:514`).

- Confirm `SettingsOverlay` parity for what the legacy panel does: font-size slider (`.slider`, drives `--font-size-base`) and theme selector. `settings/sections/AppearanceSection.tsx` already carries appearance controls — close any gap found (e.g., slider labels, reset action `reset-button`).
- Repoint both `useAppCommandPalette.ts` actions to `settingsOverlay.open()`; delete the `settingsOpen` state and the `<Settings>` render block from `App.tsx`.
- Delete `web/src/components/Settings.tsx` and `web/src/styles/settings.css` (276 lines; includes ~75 already-dead lines — `.settings-stack`, `.settings-row*`, `.model-select*`, `.loading-text`, `.no-models-text`); remove the `./styles/settings.css` import from `main.tsx`.
- Replace `web/src/__tests__/settingsSliderFocus.test.ts` (postcss-parses `settings.css` at module scope — it throws once the file is gone) with an equivalent render-based focus-ring assertion on the SettingsOverlay slider: no bare `outline` on rest state, `:focus-visible` ring using `var(--accent)` per the WCAG focus contract.
- Ratchet: drop `Settings.tsx` raw-element entries (4 button, 1 input), the `settings.css` `CSS_FILE_ALLOWLIST` entry; this batch deletes >200 CSS lines → lower ceiling in the same commit. Update `mobileChromeCss.test.ts` import expectations for the removed `main.tsx` import.

**Acceptance:**

- 2.1.1 - Command-palette settings actions open the overlay; the legacy panel is unreachable. file: `web/src/components/app/useAppCommandPalette.ts`.
- 2.1.2 - `Settings.tsx` and `settings.css` are deleted; `main.tsx` no longer imports the sheet. file: `web/src/main.tsx`.
- 2.1.3 - Slider focus-ring contract is asserted against the overlay implementation. test: `web/src/__tests__/settingsSliderFocus.test.ts`.
- 2.1.4 - Allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

## P3: New Primitives

`kind: framing`

**Goal**: The four missing primitives exist as cva recipes in `web/src/components/ui/`, each replacing every competing implementation it unifies. Primitives follow the Button pattern: component + separate `*Variants.ts` cva recipe, tokens only (no raw colors), coarse-pointer flow-through, focus rings per `focusStyles.ts`.

### 3.1 ui/Chip primitive [category: code]

`kind: deliverable`

Targets:
- `tasks/TaskBadges.tsx`
- `web/src/components/chat/styles/sessions-tab.css`
- `web/src/components/ui/Chip.tsx`
- `activity-panel.css`
- `chipVariants.ts`
- `mcp-tab.css`
- `task-execution.css`

Five parallel chip implementations exist. Create `Chip.tsx` + `chipVariants.ts`:

- API: `tone: neutral | accent | info | warning | error` (state palette, icon/lightness-first per `.impeccable.md`), `uppercase?: boolean` (default false — preserves the session-chip `text-transform: uppercase` delta over task chips), `size?: sm | md` if call sites need it, `asChild` via Radix Slot. Base look from the current shared rule: `height 1.25rem; padding-inline .375rem; border-radius 9999px; font-size var(--text-2xs); font-weight 600; white-space nowrap`.
- Adopt at all chip renderers: `.chip`/`.chip--*` in `activity/SessionsTab.helpers.tsx:104-153` (uppercase tone chips + inline warning chip); `.activity-chip`/`--accent/--info/--warning/--error` across the 14+ list/detail components (agents, integrations, memory, pipelines, rules, skills, stages surfaces); `AGENT_RULES_CHIP*` and `STEP_CHIP*` constants from `agents/agents-styles.ts` (chip *display* usages — editable chip-input rows may compose Chip with a remove Button); task chips in `tasks/TaskBadges.tsx` (`TASK_BADGE_CLS` + `.chip--state/--priority/--type` modifiers become tone + className); `.activity-mcp-chip` (`mcp-tab.css:72`). (Wiki citation chips are out of scope with the Ask surface — see 4.11.)
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
- `web/src/components/ui/Card.tsx`
- `cardVariants.ts`

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
- `fields/DateTimeField.tsx`
- `web/src/components/activity/fields/FieldPrimitives.tsx::*` — scope-reason: every field primitive is rebuilt on FormField + ui controls
- `web/src/components/ui/FormField.tsx`
- `web/src/components/ui/NativeSelect.tsx`

Six labeled-form-row implementations exist. Create `FormField.tsx`: label + optional hint/error + control slot (`useId` wiring, `aria-describedby`), the shell equivalent of today's `fieldShellClass = "flex flex-col gap-1.5"` / `labelClass` / `controlClass` trio.

- Rebuild `activity/fields/FieldPrimitives.tsx` (TextField, SecretField, NumberField, TextAreaField, SelectField, TagsField) on `FormField` + `ui/Input` / `ui/Textarea` / `ui/NativeSelect` — this finally gives `ui/Input` (currently zero consumers) its adoption path. Remove the duplicated class trio from `fields/DateTimeField.tsx:13-15`.
- Migrate `settings/fields/*` (`StringListField`, `KeyValueMapField`, `TypedListField`, `BoundedSelectField`) and `settings/sections/configFields.tsx` onto the same primitives; their hand-rolled label/row markup goes away.
- The remaining field variants (`agents-styles.ts` `AGENT_EDIT_FIELD/LABEL/HINT/INPUT`, `PipelineEditor.styles.ts` `FIELD_*`, `ValidationDetectionEditor` `FORM_FIELD_CLS` family) migrate in their P4 surface sweeps onto these primitives.
- Select consolidation decision encoded here: **form contexts use `SelectField`, which composes the new `ui/NativeSelect`; toolbar/picker contexts use Radix `ui/Select`**. That is the whole-app rule the P4 sweeps apply. `NativeSelect` is the smallest boundary that keeps the native-select behavior form contexts want while satisfying the standing rule that raw `<select>` lives only inside `components/ui` — a native select rendered directly by `FieldPrimitives.tsx` would remain a raw element outside `ui/` and could never reach the ratchet end state.
- Ratchet: `FieldPrimitives.tsx` raw-element entries (1 button, 4 input, 1 select, 1 textarea) drop to zero, with the select composing inside `ui/`; settings-section input entries shrink.

**Acceptance:**

- 3.3.1 - FormField exists with label/hint/error/control API. file: `web/src/components/ui/FormField.tsx`.
- 3.3.2 - Activity field primitives and settings fields render through FormField and ui controls. file: `web/src/components/activity/fields/FieldPrimitives.tsx`.
- 3.3.3 - `ui/Input` has production consumers. symbol: `Input`.
- 3.3.4 - FormField has unit coverage. test: `web/src/components/ui/__tests__/FormField.test.tsx`.
- 3.3.5 - FormField pins label-to-control association, hint/error `aria-describedby`, and `aria-invalid` wiring. test: `web/src/components/ui/__tests__/FormField.test.tsx`.
- 3.3.6 - `NativeSelect` exists in `ui/` on the shared focus/token/coarse-pointer contract, and both select paths (native and Radix) are unit-tested. file: `web/src/components/ui/NativeSelect.tsx`.

### 3.4 Promote TabBar into ui/ [category: refactor]

`kind: deliverable`

Targets:
- `web/src/components/shared/SidebarPanel.css`
- `web/src/components/ui/TabBar.tsx`
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

`AgentPortfolioPage.tsx` (2 btn / 2 select incl. `.agent-filter-select`), the `AGENT_DEF_CARD_*` / `STEP_CARD_*` families → Card, remaining `agents-styles.ts` content deleted, file removed entirely with its `CLS_CONSTANT_ALLOWLIST` entry (113 → 0). `SidebarPanel.tsx` (1 btn) composes Button; with `SidebarPanel.css` already gone (3.4), fold the remaining panel shell into utilities and retire `shared/SidebarPanel.tsx` if `AgentEditForm` is its only consumer.

**Acceptance:**

- 4.2.1 - `agents-styles.ts` is deleted and its allowlist entry removed. file: `web/src/components/agents/agents-styles.ts`.
- 4.2.2 - Portfolio filter selects follow the Select rule; agents/ raw-element entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.

### 4.3 Pipelines sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `shared/executions/execution-utils.tsx`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/pipelines/PipelineEditor.styles.ts`
- `web/src/components/shared/executions/execution-utils.tsx::*` — scope-reason: workflow-trace icon utilities and the execution card/badge/button styling both migrate onto ui primitives
- `web/src/components/activity/PipelinesTab.tsx::*` — scope-reason: tab raw buttons and execution styling migrate to primitives

`PipelineEditor.tsx` (3 btn / 1 input / 1 textarea), `PipelineStepFields.tsx` (2 btn / 10 input / 2 textarea), `PipelineStepList.tsx` (6 btn / 1 input / 1 select), `PipelinesDefsList.tsx` (1 btn), `web/src/components/activity/PipelinesTab.tsx` (3 btn — `RAW_ELEMENT_ALLOWLIST` line 33; owned here so it does not survive into the endgame floor). `PipelineEditor.styles.ts` (47 `*_CLS`): `BTN_CLS`/`BTN_PRIMARY_CLS` → Button; `FIELD_*` → FormField; `STEP_CLS`/`ADD_DROPDOWN_CLS` → Card; `KV_*` rows → utilities. Delete the file (removes both its `CLS_CONSTANT_ALLOWLIST` entry and its `inputFocusAdoption` entry). `shared/executions/execution-utils.tsx` (20 `*_CLS`: run buttons, badges → Button/Badge/Chip, step cards → Card) sweeps here too since PipelinesTab consumes it.

**Acceptance:**

- 4.3.1 - `PipelineEditor.styles.ts` is deleted; pipelines raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.3.2 - `execution-utils.tsx` styles via ui primitives and utilities. file: `web/src/components/shared/executions/execution-utils.tsx`.
- 4.3.3 - The pipelines `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.4 Wiki sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch

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

`CodeGraphExplorer.tsx` (32 `*_CLS`, 6 btn, 5 input) and `activity/memory/KnowledgeGraph.tsx` (19 `*_CLS`, 5 btn, 5 input): controls/search/legend/physics panels → Button, Input, Card, utilities at call site. Both files carry `inputFocusAdoption` entries — removed on migration. Canvas/graph rendering logic untouched.

**Acceptance:**

- 4.5.1 - Both explorers' raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.5.2 - Both `inputFocusAdoption` entries are removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.6 FilesPage sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/FilesPage.tsx::*` — scope-reason: page-wide sweep of 49 _CLS constants, tab strip, toolbar, and dialogs onto primitives
- `files-tab.css`

49 `*_CLS` + 4 buttons: page shell/sidebar/tree/toolbar/viewers → utilities + Card; tab strip → TabBar (3.4); `CONFIRM_KEEP_CLS`/`CONFIRM_DISCARD_CLS` dialog → `ConfirmDialog` + Button; git-status color constants stay as data maps (they are token lookups, not style strings — rename away from `_CLS` if the ratchet pattern catches them). `.file-viewer-btn` usage ties into `files-tab.css` retirement (5.5).

**Acceptance:**

- 4.6.1 - FilesPage raw-element and `*_CLS` entries are zero. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.6.2 - The discard-confirm flow uses ConfirmDialog. file: `web/src/components/FilesPage.tsx`.

### 4.7 Tasks sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/tasks/TaskCreateForm.tsx::*` — scope-reason: form-wide migration of fields, selects, and buttons onto FormField/ui controls
- `TaskCloseDialog.tsx`
- `TaskTreeRow.tsx`

`TaskCreateForm.tsx` (14 `*_CLS`, 3 btn / 2 input / 4 select / 2 textarea), `QuickCaptureTask.tsx` (12 `*_CLS`, 2 btn / 1 input, `inputFocusAdoption` entry), `taskModalStyles.ts` (3), `TaskBadges.tsx` (3 — Chip from 3.1), `TaskFieldEditors.tsx` (1 btn / 2 input / 1 select / 1 textarea incl. `.task-inline-edit--select`), `TaskCloseDialog.tsx` (1 textarea), `TaskTreeRow.tsx` (2 btn). Modals → Dialog primitives; forms → FormField + ui controls; selects per rule.

**Acceptance:**

- 4.7.1 - tasks/ raw-element and `*_CLS` entries are zero (incl. `taskModalStyles.ts` deleted). file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 4.7.2 - The QuickCaptureTask `inputFocusAdoption` entry is removed. test: `web/src/components/__tests__/inputFocusAdoption.test.ts`.

### 4.8 Activity lists and detail panels sweep [category: refactor] (depends: P3)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/activity/RulesTab.tsx::*` — scope-reason: filter-panel selects, the local filter dropdown, and rules-tab styling all migrate to SelectField/primitives

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

Remaining files: `App.tsx` (1 btn), `ProjectSelector.tsx` (2 btn / 1 input), `web/src/components/chat/CommandBar.tsx` (1 btn — `RAW_ELEMENT_ALLOWLIST` line 96), `web/src/components/chat/PlanApprovalActions.tsx` (1 textarea — line 191), `ValidationDetectionEditor.tsx` (9 `*_CLS`, 1 btn / 1 input / 1 textarea, `inputFocusAdoption` entry), `auth/LoginPage.tsx` (1 btn / 3 input), `AppErrorBoundary.tsx` (2 btn), chat: `ProviderPicker.tsx` (3 btn), `BranchIndicator.tsx` (3 btn), `AgentPickerDropdown.tsx` (11 `*_CLS`, 4 btn — scope toggle → SegmentedControl per 3.4), `ChatCommandPalette.tsx` (1 btn), `CommandPalette.tsx` (1 input), `ResumeSessionModal.tsx` (1 btn / 2 input), `ActiveAgentIndicator.tsx` (1 btn), `CodeBlockRenderers.tsx` (1 btn), `ToolResultImage.tsx` (1 btn), `ChatInputModelControls.tsx`/`ChatInputToolbar.tsx` (non-composer entries), command-browser: `ToolBrowserModal.tsx` (4 btn), `SkillBrowserModal.tsx` (3 btn), `ToolArgumentForm.tsx` (1 each btn/input/select/textarea), settings: `SettingsOverlay.tsx` (2 btn), `PromptsTemplatesSection.tsx` (1 btn / 1 input), remaining section inputs, `shared/DiffBlock.tsx`, `shared/MermaidBlock.tsx` (1 btn each), `chat/ToolCallCard.tsx` (1 input — its 2 sanctioned header buttons stay). **Sanctioned exceptions keep pinned entries**: `FilesTab.tsx` composite-row buttons, `ToolCallCard.tsx` header buttons, composer icon buttons (`ChatInput.tsx`, `ChatInputQueuedFiles.tsx`, `ChatInputPrimaryButton.tsx`, `ChatInputModelControls.tsx` composer instances — moat 05198494). The moat covers **buttons only**: `web/src/components/chat/ChatInput.tsx` also carries a textarea entry (`RAW_ELEMENT_ALLOWLIST` line 190) sitting beside its sanctioned button entry (line 91). That textarea migrates to `ui/Textarea` here; only the button entry survives.

**Acceptance:**

- 4.10.1 - `RAW_ELEMENT_ALLOWLIST` input and select maps are empty; the textarea map holds only the deferral-covered `WikiAskMode.tsx` entry; the button map contains only the named sanctioned exceptions plus the deferral-covered `WikiAskMode.tsx` entry (see 4.11). file: `web/src/__tests__/styleRatchet.allowlist.ts`.
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
- `ActivityPanelEmpty.test.tsx`
- `MessageList.tsx`
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
- `web/src/styles/accessibility.css`
- `input-voice.css`
- `mobileChromeCss.test.ts`
- `planApprovalDesign.test.tsx`

`input-base.css` (398), `input-composer.css` (263), `input-voice.css` (187), `input-responsive.css` (151), `input-status.css` (18), `input.css` barrel (5) — 1,022 lines onto the composer components (`ChatInput`, `ChatInputToolbar`, `ChatInputModelControls`, `ChatInputVoiceControls`, `ChatInputPrimaryButton`, `AgentStatusBar`, `VoiceStatusBar`, `ChatCommandPalette`). Composer icon buttons keep their purpose-built look (moat) as scoped utilities. Container queries move to `@container` utilities / the components' own scoped styles. The `input-voice.css:176` `animation: none !important` relocates to `web/src/styles/accessibility.css` with a justification comment (reduced-motion class). Guard updates: `mobileChromeCss.test.ts` chat container-query pins and `planApprovalDesign.test.tsx` `.agent-status-bar` source assertions re-point or convert to JSX/computed-style assertions; `coarsePointerTouchTargets` fixture hooks that referenced these sheets move to compiled-Tailwind candidates.

**Acceptance:**

- 5.2.1 - All six input sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.2.2 - Composer renders with visual parity across the capture matrix. behavior: "composer parity" in `web/tests/style-surfaces.spec.ts`.
- 5.2.3 - The single voice `animation: none !important` relocated from `input-voice.css:176` lives in accessibility.css under its own `prefers-reduced-motion` query with rationale, and `IMPORTANT_ALLOWLIST` moves that one count with it. file: `web/src/styles/accessibility.css`.

### 5.3 Retire layout.css, variables.css, and the chat barrel [category: refactor] (depends: 5.2)

`kind: deliverable`

Targets:
- `chat/styles.css`
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/layout.css`
- `web/src/styles/base.css`

`layout.css` (468): `.chat-container/-messages/-page/-main`, `.command-bar*`, `.message*` shells, `.mobile-chat-drawer`, and the full-screen `.command-palette-*` family → utilities on `ChatMainColumn`, `CommandBar`, `MessageList`/`MessageItem`, `CommandPalette`. `variables.css` (12): its four alias custom properties (`--bg-code`, `--bg-muted`, `--border-color`, `--accent-color`) either inline to their consumers or graduate to `tokens.css` if genuinely shared — then delete the sheet and the `mobileChromeCss` alias-only assertion. `chat/styles.css` (32): the `.tool-code-surface` `!important` rule (beats react-syntax-highlighter's inline style — must survive) relocates to `web/src/styles/base.css` with its #14721 comment; barrel deleted. `IMPORTANT_ALLOWLIST` moves the entry accordingly. `mobileChromeCss` `.command-bar` pins re-point; `typographyLadder` `.command-bar-btn` pin re-points; `planApprovalDesign` `.command-bar` assertion re-points. Naming hazard resolved: the chat-input dropdown formerly `.command-palette` (input-base) and the modal `.command-palette-*` (layout) end as component-scoped utilities, killing the collision.

**Acceptance:**

- 5.3.1 - `layout.css`, `variables.css`, and `chat/styles.css` are deleted with allowlist entries dropped. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.3.2 - The tool-code-surface override survives in base.css and tool cards render flat code backgrounds. file: `web/src/styles/base.css`.
- 5.3.3 - Import-order-dependent behavior is gone from chat styling (no cross-sheet duplicate selectors remain). behavior: "no duplicate selectors" in `web/src/components/chat/ChatPage.tsx`.

### 5.4 Retire sessions-tab.css and activity-panel.css [category: refactor] (depends: 4.9)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/chat/styles/activity-panel.css`

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

`files-tab.css` (300), `mcp-tab.css` (239), `rules-tab.css` (212 incl. `.activity-filter-panel` used by Skills + Integrations — becomes a shared filter-panel component or utilities), `cron-tab.css` (61), `traces-tab.css` (36), `pipelines-tab.css` (35), plus `activity/skills/SkillsTab.css` (3). Consumers: FilesTab/FilesPage/FileChangesTab, McpDetailPanel/ActivityMcpTab, RulesTab, CronTab, TracesTab, PipelinesTab. Guard updates: `typographyLadder` file-tree and cron pins re-point; `coarsePointerTouchTargets` hooks from these sheets move to utilities.

**Acceptance:**

- 5.5.1 - All seven sheets are deleted with allowlist entries dropped and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 5.5.2 - The shared filter panel serves Rules, Skills, and Integrations from one implementation. file: `web/src/components/activity/RulesTab.tsx`.

### 5.6 Retire task-execution.css and task-detail.css [category: refactor] (depends: 4.7)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/components/tasks/task-execution.css`

`task-execution.css` (738 — largest sheet): `.chip` block already dead (3.1); `.activity-task-*` rows/panes/toolbars → utilities on TaskTreeRow/TasksTab components; `.activity-filter-*` fragments consolidated per 5.4. `activity/taskdetail/task-detail.css` (346): detail header/KV/relationships → utilities; `.task-inline-edit--select` already migrated (4.7). Guard updates: `typographyLadder` task pins and `PRIORITY_TEXT_WEIGHTS` stay component-side; `coarsePointerTouchTargets` `.task-more-btn`/`.activity-task-row-toggle`/`.activity-task-detail-edit-error__dismiss` hooks move to compiled utilities; `mobileChromeCss`/`planApprovalDesign` references re-point.

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

1. Run the 1.3 capture matrix (before).
2. Remove `important: true` from `web/tailwind.config.ts` (file becomes content-scanning only).
3. Verify the six surviving intentional `!important` declarations still hold — all six beat an inline style or serve reduced-motion, so none of them depends on the flag. By this point they sit in two files: `base.css` carries the four-declaration reduced-motion block plus the `.tool-code-surface` background relocated in 5.3, and `accessibility.css` carries the voice `animation: none` relocated in 5.2. That is the same six the plan opens with (`chat/styles.css` 1 + `input-voice.css` 1 + `base.css` 4), redistributed from three files to two by sheet retirement — no declaration is added or removed by this phase.
4. Run the matrix (after); review every pair; fix any regression at its source (specificity at the component, never a new `!important`).
5. Sweep for utility-vs-hook-sheet conflicts: the remaining hook sheets (`app-shell.css`, `segmented-control.css`, `dropdown-caret.css`, `settings-overlay.css`) are the only stylesheets that can now out-specificity utilities — audit their selectors against utility-bearing elements they touch.

Update `docs/guides/frontend-style-guide.md` anti-patterns wording ("Tailwind utilities are already configured with `important: true`" — no longer true).

**Acceptance:**

- 6.1.1 - `important: true` is gone. file: `web/tailwind.config.ts`.
- 6.1.2 - Before/after capture pairs across the full matrix show parity (or reviewed, intended fixes). behavior: "matrix parity review" in `web/tests/style-surfaces.spec.ts`.
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
- `DropdownCaret.tsx`
- `app-shell.css`
- `mobileChromeCss.test.ts`

38 lines total move into their owning primitives: `.segmented-control*` rules → `SegmentedControl.tsx` utilities/cva (keeping the `--control-row-height` contract and the `mobileChromeCss` option-padding pins as component assertions); `.dropdown-caret` → `DropdownCaret.tsx`. Drop both `main.tsx` imports; update `mobileChromeCss.test.ts` (its `segmented-control.css < app-shell.css` order assertion retires; segmented-control pins convert to component assertions).

**Acceptance:**

- 7.1.1 - Both sheets are deleted; the primitives self-style. file: `web/src/components/ui/SegmentedControl.tsx`.
- 7.1.2 - Allowlist entries dropped; `main.tsx` imports removed. file: `web/src/main.tsx`.

### 7.2 Retire app-shell.css [category: refactor] (depends: 7.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/styles/app-shell.css`

157 lines of header-cluster hooks (`app-*` classes sizing the theme-toggle/cog/logout cluster, project-selector responsive swap, health badge). Express as utilities/cva on the App header components while preserving the canonical-cluster contract from `.impeccable.md` (equal icon widths via `size="icon"`, `--status-bar-control-height` row, coarse-pointer hit-area expansion, mobile collapse to a single settings entry). `mobileChromeCss` app-header pins convert to JSX/component assertions. The style guide's "sanctioned exception" paragraph for hook sheets is rewritten in 8.2.

**Acceptance:**

- 7.2.1 - `app-shell.css` is deleted with its allowlist entry; header renders with parity in both tiers. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 7.2.2 - Header pins live as component assertions. test: `web/src/__tests__/mobileChromeCss.test.ts`.

### 7.3 Retire settings-overlay.css [category: refactor] (depends: 6.1)

`kind: deliverable`

Targets:
- `web/src/__tests__/styleRatchet.allowlist.ts::*` — scope-reason: the ratchet census must shrink in the same commit as every migration; entries touched vary per batch
- `web/src/styles/settings-overlay.css`
- `main.tsx`

712 lines across `SettingsOverlay.tsx`, `WorkflowVariablesEditor.tsx`, `settings/fields/*`, and the 13 section components. Much of the field/row styling is already superseded by FormField adoption (3.3) — delete superseded rules first, then migrate the shell (`.settings-overlay-shell*`), sections (`.settings-section*`, `.settings-subsection*`), and specialty editors (`.settings-variables*`, `.settings-endpoint-editor`, `.settings-hubs-field*`, `.settings-prompt-row`, `.appearance-font-size*`) to utilities. Work in 2–3 commits (shell → sections → specialty) to stay bisectable. Drop the `main.tsx` import; lower ceiling (>200 lines).

**Acceptance:**

- 7.3.1 - `settings-overlay.css` is deleted with its allowlist entry and ceiling lowered. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 7.3.2 - Settings overlay renders with parity across the capture matrix. behavior: "settings overlay parity" in `web/tests/style-surfaces.spec.ts`.

### 7.4 Load-order rationalization [category: refactor] (depends: 7.2)

`kind: deliverable`

Targets:
- `web/src/main.tsx`
- `index.css`
- `mobileChromeCss.test.ts`

End state: `main.tsx` imports the two font packages and `./styles/index.css` only; `index.css` owns the full `@import` chain (`tailwindcss`, `@config`, `tailwind-theme`, `tokens`, `base`, `markdown`, `accessibility`). Update `mobileChromeCss.test.ts` to pin the final import list and `index.css` directive order deliberately (the pins become simpler, and intentional).

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
- `RAW_ELEMENT_ALLOWLIST` reduces to the pinned sanctioned-exception floor (FilesTab composite rows, ToolCallCard header, composer icon buttons) with a comment linking each entry to its `.impeccable.md` moat, plus the deferral-covered `WikiAskMode.tsx` entries comment-linked to the 4.11 deferral (#19672) and removed when that surface ships; the test's remedy strings note additions require an explicit moat.
- `IMPORTANT_ALLOWLIST` reduces to exactly two infra-sheet entries carrying the same six declarations it records today: `src/styles/base.css: 5` (the four-declaration reduced-motion block plus the `.tool-code-surface` background relocated in 5.3) and `src/styles/accessibility.css: 1` (the voice `animation: none` relocated in 5.2). The `chat/styles.css` and `input-voice.css` entries are removed as those sheets retire, and each surviving entry gets a justification comment naming what inline style or media query it beats.
- `CSS_FILE_ALLOWLIST` pins exactly the six infra sheets: `index.css`, `tokens.css`, `tailwind-theme.css`, `base.css`, `markdown.css`, `accessibility.css`.
- `CSS_TOTAL_LINE_CEILING` pins to the final infra total with slack reduced to a small fixed value (or the two-sided band replaced by an exact-pin assertion) — decide in-code with a comment; the ceiling stops being a burn-down metric.
- Simplify `styleRatchet.test.ts` mechanics where the allowlist shape allows (stale-entry loops over empty maps, target-branch parser still required for the pinned floors); keep `parseAllowlistSnapshot` compatibility.

**Acceptance:**

- 8.1.1 - Allowlists are empty or pinned floors with moat-linked comments. file: `web/src/__tests__/styleRatchet.allowlist.ts`.
- 8.1.2 - The ratchet test enforces the end state and passes. test: `web/src/__tests__/styleRatchet.test.ts`.

### 8.2 Update the style guide and design contract [category: docs] (depends: P7)

`kind: deliverable`

Targets:
- `docs/guides/frontend-style-guide.md`
- `.impeccable.md`
- `app-shell.css`

- Rewrite `docs/guides/frontend-style-guide.md`: ui/ inventory gains Chip, Card, FormField, TabBar; the Legacy CSS Files section becomes the end-state contract (six infra sheets, everything else utilities/cva); the hook-sheet sanctioned exception is removed; the Style Debt Ratchet section documents the ban-plus-floor model; anti-pattern wording updated post-flip.
- Update `.impeccable.md` Canonical Components via the impeccable skill's teach mode (app-header cluster sizing no longer references `app-shell.css` hook selectors; selector/segmented-control references point at component-owned styling). This is the one file edited through the skill, per project rule.

**Acceptance:**

- 8.2.1 - The style guide documents the end state. file: `docs/guides/frontend-style-guide.md`.
- 8.2.2 - The design contract's component references match the shipped architecture. behavior: "Canonical Components reflect component-owned styling" in `.impeccable.md`.

## V2 End-to-End Verification

`kind: verification`

- `cd web && npm run test && npm run type-check && npm run lint && npm run lint:tokens` — green at every phase boundary; the ratchet proves recorded debt is exact at each step and the ban-state at the end.
- Playwright matrix (`web/tests/style-surfaces.spec.ts`): before/after parity review at 1.4 (re-baseline, not parity), 5.2, 5.4, 6.1, 7.2, 7.3 minimum; full-matrix final pass across all 17 manifest surfaces × dark/light × fine/coarse × three reference viewports, every entry asserting its visible checkpoint.
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
