# One-surface UI: migrate hamburger pages into activity-panel tabs

## Overview
`kind: framing`

The web UI today splits between two navigation models: a hamburger menu of full-page hash routes (`#workflows`, `#mcp`, `#memory`, …) that **replace** the chat shell, and the activity panel's tab dropdown (Sessions, Tasks, Plans, …) that lives **beside** chat. Switching to any hamburger page tears the user out of web chat. The goal is one surface: everything reachable from the activity-panel dropdown, chat never lost.

This is not greenfield. Epic #14767 already proved the pattern end-to-end (D1–D7, commits `b94b9874b`…`332d2aa92`): Tasks was fully migrated out of the hamburger into a two-pane activity tab and its standalone page deleted. The tri-state layout keystone (D1 #14768, `chat|split|panel`) is shipped shared infra — new surfaces inherit it for free. This plan replicates that proven path for every remaining in-scope hamburger surface, then deletes the hamburger.

## Constraints
`kind: framing`

- **Design system (`.impeccable.md`) is binding.** Dark default, hue-125 chartreuse brand accent, deutan-safe state palette (lightness then hue — color is the fourth signal), WCAG 2.2 AA, 44px touch targets, no border-left/border-right greater than 1px, no gradient text, Vercel/Linear restraint. Each surface migration is also an impeccable redesign, **not a 1:1 port**.
- **Tailwind 4 only — no new legacy CSS.** Retire any legacy class-string modules the source pages drag in.
- **JS `useIsMobile()` breakpoint at the canonical 768px.** No Tailwind `md:` variants.
- **Anti-monolith (CLAUDE.md rule 2).** Non-test `.tsx`/`.ts` files must stay under 1000 lines. `SessionsTab.tsx` (953L) and `TasksTab.tsx` (949L) are already at the ceiling — only the multi-file family pattern keeps them under. Every new surface ships at least five files in the family.
- **Canonical references (swapped from CronTab in the prior draft):** SessionsTab is the in-toolbar SegmentedControl exemplar (`Live | Expired` at line 622, `STATUS_MODE_OPTIONS` declared at line 58). TasksTab plus its Tasks-prefixed sibling files (Data, Model, Toolbar, Filters, Actions, DetailPanel, List) is the multi-file decomposition exemplar.
- **No deep-link bridge.** Bookmarks to legacy hashes (`#mcp`, `#workflows`, …) dead-end to default chat after the hamburger is removed. Trading bookmark continuity for a smaller diff.
- **Out of scope:** Configuration (moving to a gear tab separately), Dashboard, Projects. The hamburger entries for these stay until their own efforts retire them; the final hamburger-removal gate only fires after every in-scope surface has migrated.
- **Reports is deleted outright** — no panel-tab replacement.
- **Cron page dedup and Traces page dedup are independent tasks**, not a combined epic. Different domains; they share only the pattern.
- **Validation isolated from the user's daemon.** Any test that needs daemon behavior starts an isolated test daemon. Prefix pytest with `GOBBY_TEST_PROTECT=1`. Never run the full 15k pytest suite.
- **Verification surface:** `http://localhost:60889/#chat` via chrome-devtools MCP.

## P1: Framework foundation
`kind: framing`

**Goal**: Document the recipe and the final-gate spec so every surface migration follows the same shape and the eventual hamburger removal has unambiguous exit criteria.

### 1.1 Codify the two-pane tab recipe and view-selector convention [category: docs]
`kind: deliverable`

Targets:
- `docs/guides/one-surface-tab-recipe.md`

Write a single guide that future surface-migration agents load before doing any work. The guide is the durable form of the recipe that the original draft kept inline; lifting it out of the plan keeps each per-surface deliverable self-contained without copy-pasting the recipe into every section.

Content the guide must contain, written in prose, not as backticked file placeholders:

1. **Canonical exemplars.** SessionsTab is named as the in-toolbar SegmentedControl exemplar with concrete line references (segment around line 622, `STATUS_MODE_OPTIONS` declared at line 58). TasksTab and its Tasks-prefixed sibling files (Data, Model, Toolbar, Filters, Actions, DetailPanel, List) are named as the multi-file decomposition exemplar.
2. **File-family checklist.** Eight slots per surface, all under the activity components folder, all prefixed with the surface name:
   1. The Tab shell — toolbar (optional SegmentedControl view selector), top list/tree, vertical ResizeHandle, bottom detail with the h-10 status strip that holds action buttons (the middle bar). Resize/selection wiring is copied from the TasksTab exemplar.
   2. A Data file — wraps the existing per-surface hook (useMcp, useMemory, …) and the relevant useWebSocketEvent subscription if the surface has live updates.
   3. A Model file — pure filter/derive selectors. Keeps the shell file under the 1000-line ceiling.
   4. A Toolbar file — ActivityPanelSearch, filter trigger, optional SegmentedControl view selector.
   5. A Filters file — FilterSection, FilterCheckboxRow, ActivityFilterFooter.
   6. An Actions file — mutations the toolbar and middle bar wire to.
   7. A DetailPanel file — where the impeccable IA redesign concentrates.
   8. Empty states via ActivityPanelEmpty.
3. **Registration — exactly three edits**, mirroring what Tasks did:
   1. Add the surface id to the ActivityTab union and the ACTIVITY_PANEL_TABS array. Icon: re-path the matching glyph from the app-navigation module to a 24x24 outline.
   2. Add the surface id to the VALID_TABS array in the useActivityPanel hook. The activity-panel-tab-v2 localStorage key auto-persists the active tab.
   3. Import the new Tab component, add a case to tabContent inside the ActivityPanel shell, and thread projectId (and any extra props) through ActivityPanelProps and the App → ChatPage → ActivityPanel chain only if more than projectId is needed (most surfaces don't).
4. **IA-redesign rules.** Apply the impeccable design system in full. Collapse the source page's wide multi-column layout into the narrow panel by promoting one primary axis to the list and moving the rest to detail plus filters. Retire legacy class-string modules (Tailwind only). No border-left or border-right greater than 1px. No gradient text. State communication must survive a grayscale screenshot (lightness plus icon plus position).
5. **View-selector rule.** Multi-view surfaces use SegmentedControl in the activity-panel-toolbar slot, exactly like the SessionsTab exemplar. Per-view content lives in surface-prefixed Pane files.
6. **D7 delete checklist (when a tab replaces a page).** The app-navigation module loses its createAppNavItems entry, its APP_VALID_TABS string, and its APP_NAV_PAGES entry for that surface. The AppPages module loses the lazy import. The App shell loses the activeTab equality branch. The page module and its private satellites are deleted from disk. Confirm with a repo-wide grep that no consumer outside the deleted set still imports from the deleted module. Migrate tests: delete page UI tests, grow the activity tests folder under the surface's test scope; keep underlying hook tests.
7. **Per-surface verification (gate before D7):** `cd web && npm run type-check` clean; `cd web && npm run test -- --run` scoped to the new Tab test scope and migrated hook suites green; `npx eslint <touched files>` clean; chrome-devtools MCP at the chat URL — tab visible in the dropdown, both panes render, ResizeHandle drags, middle-bar actions perform real mutations (WS round-trip); SegmentedControl swaps views (when present); a 768px resize in both directions with no stuck state and the activity-panel-tab-v2 localStorage key correct; grayscale screenshot — state legible without hue; AA contrast plus brand focus ring in both dark and light; 44px touch targets; explicit parity checklist — enumerate every old-page capability and confirm each is reachable in the tab.
8. **Pitfalls.** Common mistakes the recipe must call out: copying CronTab as exemplar (deprecated guidance — use SessionsTab); leaving border-left color stripes from the source page; using Tailwind md: variants instead of the JS breakpoint; failing to retire legacy class-string modules; landing a single-file Tab that crosses the 1000-line ceiling.

**Acceptance:**

- 1.1.1 - Recipe guide exists at the canonical path and contains the eight numbered sections above (exemplars, file family, registration, IA rules, view selector, D7 delete, per-surface verification, pitfalls). file: `docs/guides/one-surface-tab-recipe.md`.
- 1.1.2 - Guide cites SessionsTab as the SegmentedControl exemplar with concrete line references. behavior: "SessionsTab cited as segment exemplar with line refs" in `docs/guides/one-surface-tab-recipe.md`.
- 1.1.3 - Guide cites the Tasks-prefixed sibling-file decomposition (Data, Model, Toolbar, Filters, Actions, DetailPanel, List) as the multi-file exemplar. behavior: "Tasks-prefixed family enumerated as exemplar" in `docs/guides/one-surface-tab-recipe.md`.

### 1.2 Specify the hamburger-removal final gate [category: docs]
`kind: deliverable`

Targets:
- `docs/guides/one-surface-final-gate.md`

Write the exit-criteria checklist for the hamburger-removal task in phase P6 so the gate has a deterministic literal-diff target the agent can either satisfy or fail.

Content the guide must contain:

1. Required render-tree state at gate-pass time: the page header DOM is project selector plus ThemeToggle plus logout (no hamburger trigger).
2. Exit-criteria checklist:
   - The AppPages module lazy-imports only DashboardPage, ConfigurationPage, and ProjectsPage (the three out-of-scope surfaces). No other page-suffixed modules remain.
   - The App shell has render branches only for chat, dashboard, projects, and configuration, plus the default fallback. No in-scope migrated surface (mcp, memory, skills, integrations, the workflows split into rules and stages and agents and pipelines-defs, reports, cron, traces, workflows) retains a render branch.
   - The Sidebar hamburger trigger is removed from the header.
   - Full `cd web && npm run type-check && npm run test -- --run` green; full `npx eslint` clean; `git grep` for the deleted page modules returns no hits.
3. Failure modes documented, including: orphaned class-string import in a shared component, leftover test that imports a deleted page, page file unreachable from AppPages but still on disk.

**Acceptance:**

- 1.2.1 - Final-gate spec exists with the exit-criteria checklist above. file: `docs/guides/one-surface-final-gate.md`.
- 1.2.2 - Spec names the three out-of-scope surfaces (Dashboard, Projects, Configuration) that remain in the hamburger after this epic. behavior: "out-of-scope surfaces enumerated" in `docs/guides/one-surface-final-gate.md`.

## P2: Reference proof — MCP
`kind: framing`

**Goal**: Migrate the MCP hamburger page into an activity-panel tab, then delete the page. This is the canary: it battle-tests the recipe once before P3, P4, and P5 fan out in parallel.

### 2.1 Build the MCP activity-panel tab [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `web/src/components/activity/McpTab.tsx`
- `web/src/components/activity/McpTabData.ts`
- `web/src/components/activity/McpTabModel.ts`
- `web/src/components/activity/McpTabToolbar.tsx`
- `web/src/components/activity/McpTabFilters.tsx`
- `web/src/components/activity/McpTabActions.ts`
- `web/src/components/activity/McpTabDetailPanel.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Implement the MCP tab following the recipe from 1.1. MCP is a single-view two-pane surface: list of MCP servers on top, server-detail plus tool tree on the bottom. The source page at the MCP page module (367 lines, read-only context — deletion lives in 2.2) plus its two satellites (the server-form and tool-detail modules in the same mcp subtree) are the migration input — read them to understand the data model and the existing UX, then redesign per the impeccable contract.

File responsibilities:

- `web/src/components/activity/McpTab.tsx` — shell (toolbar, top server list, ResizeHandle, bottom detail with middle-bar action strip).
- `web/src/components/activity/McpTabData.ts` — wraps the existing useMcp hook plus the useWebSocketEvent subscription for mcp_server_status updates.
- `web/src/components/activity/McpTabModel.ts` — pure selectors: filter by transport pill (INTERNAL, HTTP, STDIO, WEBSOCKET, SSE), filter by connected status, sort by name.
- `web/src/components/activity/McpTabToolbar.tsx` — ActivityPanelSearch (server name plus tool name), transport-pill filter trigger, "+ Server" action.
- `web/src/components/activity/McpTabFilters.tsx` — FilterSection with checkbox rows for transport and connection status.
- `web/src/components/activity/McpTabActions.ts` — mutations: clearCache, import, addServer, removeServer, refreshTools.
- `web/src/components/activity/McpTabDetailPanel.tsx` — server-meta header (name, transport badge, tool count) plus tool tree (collapsible by tool category). The impeccable IA redesign concentrates here.

Register the tab via the three edits from 1.1:

1. Add `"mcp"` to the ActivityTab union and to the ACTIVITY_PANEL_TABS array in `web/src/components/activity/ActivityPanelTabs.tsx`. Icon: re-path the McpIcon from the app-navigation module to a 24x24 outline.
2. Add `"mcp"` to the VALID_TABS array in `web/src/components/activity/useActivityPanel.ts`.
3. Import McpTab and add a `case "mcp": return <McpTab projectId={projectId} />;` inside `tabContent` in `web/src/components/activity/ActivityPanel.tsx`.

IA redesign rules for this tab:

- Replace the source page's wide MCP Servers header bar with the canonical activity-tab header (label plus filter button plus "+ Server").
- Drop the page's repeated `connected` / tool-count chip pattern. Connection state is conveyed by lightness plus icon, not hue alone (deutan-safe state palette).
- Tool tree (currently inline in the mcp-subtree tool-detail satellite read for context) moves into the detail panel below the resize handle.
- Touch targets at least 44x44 on every interactive element. Focus rings use the brand accent at AA contrast in both dark and light.

**Acceptance:**

- 2.1.1 - McpTab is registered in the ActivityTab union and ACTIVITY_PANEL_TABS array and VALID_TABS array. file: `web/src/components/activity/ActivityPanelTabs.tsx`. file: `web/src/components/activity/useActivityPanel.ts`.
- 2.1.2 - tabContent renders McpTab when activeTab equals mcp. symbol: `tabContent` in `web/src/components/activity/ActivityPanel.tsx`.
- 2.1.3 - Tab ships the multi-file family (Tab plus Data plus Model plus Toolbar plus Filters plus Actions plus DetailPanel) — no single file is greater than 1000 lines. file: `web/src/components/activity/McpTab.tsx`.
- 2.1.4 - Server list and tool tree both render against useMcp data in chrome-devtools verification with the panel showing the mcp tab. behavior: "list and tool tree visible against live data" in chrome-devtools verification.
- 2.1.5 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.
- 2.1.6 - Parity checklist enumerates every MCP-page capability (server CRUD, transport filtering, tool-tree expand, cache clear, server import, tool refresh) and maps each to the new-tab location (toolbar action, middle-bar action, detail-panel section, or filter row). behavior: "MCP parity checklist enumerated with new-tab mapping" in the closing commit message and the build-task description.

### 2.2 Delete McpPage and the hamburger entry for MCP [category: refactor] (depends: 2.1)
`kind: deliverable`

Targets:
- `web/src/components/mcp/McpPage.tsx`
- `web/src/components/mcp/McpServerForm.tsx`
- `web/src/components/mcp/McpToolDetail.tsx`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`
- `web/src/components/activity/__tests__/McpTab.test.tsx`

Parity gate. Execute the D7 checklist from 1.1 only after 2.1's verification passes.

Steps:

- In `web/src/components/app/appNavigation.tsx`: remove the createAppNavItems MCP entry, the APP_VALID_TABS `"mcp"` string, the APP_NAV_PAGES `{ id: "mcp", label: "MCP" }` entry, and the now-orphan `McpIcon` import line. (the project enforces `noUnusedLocals: true` in its web tsconfig and `@typescript-eslint/no-unused-vars` in its web eslint config, which fail the build on the unused import otherwise.)
- In `web/src/components/app/AppPages.tsx`: remove the lazy import for McpPage.
- In `web/src/App.tsx`: remove the activeTab equals mcp render branch.
- Delete `web/src/components/mcp/McpPage.tsx`, `web/src/components/mcp/McpServerForm.tsx`, and `web/src/components/mcp/McpToolDetail.tsx`. Confirm with a repo-wide grep that no module outside the deleted set imports from these three.
- Migrate tests: delete any McpPage UI tests; grow `web/src/components/activity/__tests__/McpTab.test.tsx`. Keep the underlying useMcp hook tests.

**Acceptance:**

- 2.2.1 - McpPage and its two satellites are deleted; a repo-wide grep for imports from the deleted modules returns no hits. file: `web/src/components/mcp/McpPage.tsx`.
- 2.2.2 - APP_VALID_TABS no longer contains mcp. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`.
- 2.2.3 - App shell no longer has an activeTab equals mcp branch. file: `web/src/App.tsx`.
- 2.2.4 - The orphan `McpIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `McpIcon` in `web/src/components/app/appNavigation.tsx`.
- 2.2.5 - `cd web && npm run type-check` plus `cd web && npm run test -- --run` (scoped) plus `npx eslint` all clean. behavior: "scoped CI green after deletion" in `web/` test scope.

## P3: Single-view tab migrations
`kind: framing`

**Goal**: Migrate the remaining hamburger surfaces (Memory, Skills) and the Workflows-page sub-tabs (Rules, Stages+Profiles, Agents) into top-level activity-panel tabs, in parallel after MCP proves the recipe.

### 3.1 Build the Memory tab with a List | Graph segment [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/MemoryTab.tsx`
- `web/src/components/activity/MemoryTabData.ts`
- `web/src/components/activity/MemoryTabModel.ts`
- `web/src/components/activity/MemoryTabToolbar.tsx`
- `web/src/components/activity/MemoryTabFilters.tsx`
- `web/src/components/activity/MemoryTabActions.ts`
- `web/src/components/activity/MemoryTabDetailPanel.tsx`
- `web/src/components/activity/MemoryListPane.tsx`
- `web/src/components/activity/MemoryGraphPane.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Memory has two views: tabular list (current default) and knowledge graph. Source page: `web/src/components/memory/MemoryPage.tsx` (397L) plus satellites `web/src/components/memory/KnowledgeGraph.tsx`, `web/src/components/memory/MemoryDetail.tsx`, `web/src/components/memory/MemoryFilters.tsx`, `web/src/components/memory/MemoryForm.tsx`, `web/src/components/memory/MemoryTable.tsx`.

Ship the multi-file family per 1.1. Add a `SegmentedControl<"list" | "graph">` in MemoryTabToolbar per the SessionsTab exemplar (segment around line 622). Per-view content lives in MemoryListPane and MemoryGraphPane.

The source page's category-filter pills (Fact, Preference, Pattern, Context, 24H) move into MemoryTabFilters. Memory cards in the source currently use 4px left-color stripes — the IA pass removes those (per the impeccable BAN 1 rule against side-stripe borders) and conveys memory type via lightness plus a leading inline label, not a side stripe.

Register via the three edits from 1.1.

**Acceptance:**

- 3.1.1 - MemoryTab family is created under the activity components folder (Tab plus Data plus Model plus Toolbar plus Filters plus Actions plus DetailPanel plus ListPane plus GraphPane). file: `web/src/components/activity/MemoryTab.tsx`.
- 3.1.2 - SegmentedControl with list and graph segments is wired in the toolbar; swapping the segment swaps the rendered pane. symbol: `MemoryTabToolbar` in `web/src/components/activity/MemoryTabToolbar.tsx`.
- 3.1.3 - No memory card uses border-left or border-right greater than 1px. behavior: "no greater-than-1px side-stripe on memory cards" verified by grayscale screenshot of the rendered tab.
- 3.1.4 - Tab is registered in `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, and `web/src/components/activity/ActivityPanel.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 3.1.5 - Parity checklist enumerates every Memory-page capability (memory CRUD, category-pill filter, search, list view, graph view, 24H filter, type-label assignment) and maps each to the new-tab location. behavior: "Memory parity checklist enumerated with new-tab mapping" in the closing commit message and the build-task description.
- 3.1.6 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.

### 3.2 Delete MemoryPage and its hamburger entry [category: refactor] (depends: 3.1)
`kind: deliverable`

Targets:
- `web/src/components/memory/MemoryPage.tsx`
- `web/src/components/memory/KnowledgeGraph.tsx`
- `web/src/components/memory/MemoryDetail.tsx`
- `web/src/components/memory/MemoryFilters.tsx`
- `web/src/components/memory/MemoryForm.tsx`
- `web/src/components/memory/MemoryTable.tsx`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Apply the D7 checklist from 1.1. Delete MemoryPage. Move any genuinely shared components (knowledge-graph rendering primitives, for example) out of the memory subtree into a shared location before deletion if any consumers outside the memory subtree exist; verify with a repo-wide grep for imports from the memory subtree.

Remove the memory entry from the app-navigation module (including the now-orphan `MemoryIcon` import line, since `noUnusedLocals` plus `@typescript-eslint/no-unused-vars` fail on the unused import), the AppPages lazy import, and the activeTab equals memory branch in the App shell.

**Acceptance:**

- 3.2.1 - MemoryPage is deleted; consumers outside the memory subtree either updated or none exist. file: `web/src/components/memory/MemoryPage.tsx`.
- 3.2.2 - APP_VALID_TABS no longer contains memory; the App shell no longer has the memory render branch. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`. file: `web/src/App.tsx`.
- 3.2.3 - The orphan `MemoryIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `MemoryIcon` in `web/src/components/app/appNavigation.tsx`.

### 3.3 Build the Skills tab with an Installed | Hub segment [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/SkillsTab.tsx`
- `web/src/components/activity/SkillsTabData.ts`
- `web/src/components/activity/SkillsTabModel.ts`
- `web/src/components/activity/SkillsTabToolbar.tsx`
- `web/src/components/activity/SkillsTabFilters.tsx`
- `web/src/components/activity/SkillsTabActions.ts`
- `web/src/components/activity/SkillsTabDetailPanel.tsx`
- `web/src/components/activity/SkillsInstalledPane.tsx`
- `web/src/components/activity/SkillsHubPane.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`
- `web/src/components/skills/styles.ts`

Skills source: `web/src/components/skills/SkillsPage.tsx` (474L) plus the 7 satellites in the skills subtree: SkillDetail, SkillForm, SkillHubBrowser, SkillImportModal, SkillScanPanel, SkillsFilters, SkillsGrid, and the legacy `web/src/components/skills/styles.ts` class-string module.

Ship the multi-file family. Add a `SegmentedControl<"installed" | "hub">` in the toolbar. Per-view content in SkillsInstalledPane (uses the SkillsGrid-derived list/detail) and SkillsHubPane (uses the SkillHubBrowser-derived list/detail). The legacy `web/src/components/skills/styles.ts` module is retired in favor of Tailwind utilities — every class string moves out before deletion.

The category-filter pills (authoring, core-cli, development, engineering, frontend, methodology, optimization, uncategorized) move into SkillsTabFilters. The source page's 4-column card grid collapses into a single-column list in the narrow panel; card density compresses (name plus INSTALLED badge plus 1-line description plus tag chip row plus status toggle), with full description in the detail pane below the resize handle.

Register via the three edits.

**Acceptance:**

- 3.3.1 - SkillsTab family is created (Tab plus Data plus Model plus Toolbar plus Filters plus Actions plus DetailPanel plus InstalledPane plus HubPane). file: `web/src/components/activity/SkillsTab.tsx`.
- 3.3.2 - SegmentedControl with installed and hub segments swaps panes; both panes render against live data in chrome-devtools verification. symbol: `SkillsTabToolbar` in `web/src/components/activity/SkillsTabToolbar.tsx`.
- 3.3.3 - The legacy skills/styles class-string module is not referenced by any new file in the family. behavior: "no skills/styles import in activity SkillsTab family" verified by a repo-wide grep inside the activity components folder returning no hits.
- 3.3.4 - Tab is registered in `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, and `web/src/components/activity/ActivityPanel.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 3.3.5 - Parity checklist enumerates every Skills-page capability (installed-vs-hub view, category-pill filter, search, install/uninstall, status toggle, description display, tag chip row, skill import, scan panel) and maps each to the new-tab location. behavior: "Skills parity checklist enumerated with new-tab mapping" in the closing commit message and the build-task description.
- 3.3.6 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.

### 3.4 Delete SkillsPage and its hamburger entry [category: refactor] (depends: 3.3)
`kind: deliverable`

Targets:
- `web/src/components/skills/SkillsPage.tsx`
- `web/src/components/skills/SkillDetail.tsx`
- `web/src/components/skills/SkillForm.tsx`
- `web/src/components/skills/SkillHubBrowser.tsx`
- `web/src/components/skills/SkillImportModal.tsx`
- `web/src/components/skills/SkillScanPanel.tsx`
- `web/src/components/skills/SkillsFilters.tsx`
- `web/src/components/skills/SkillsGrid.tsx`
- `web/src/components/skills/styles.ts`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

D7 deletion. Remove SkillsPage plus the 8 private satellites. Confirm `git grep "from.*components/skills/"` returns no hits outside the deleted set (shared symbols, if any, get hoisted into a shared module before deletion). Remove the skills nav entry, the APP_VALID_TABS string, the now-orphan `SkillsIcon` import line (required by `noUnusedLocals` plus `@typescript-eslint/no-unused-vars`), the AppPages lazy import, and the activeTab equals skills branch in the App shell.

**Acceptance:**

- 3.4.1 - SkillsPage and the 8 satellites deleted. file: `web/src/components/skills/SkillsPage.tsx`.
- 3.4.2 - APP_VALID_TABS no longer contains skills; the App shell no longer has the skills render branch. file: `web/src/components/app/appNavigation.tsx`. file: `web/src/App.tsx`.
- 3.4.3 - The orphan `SkillsIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `SkillsIcon` in `web/src/components/app/appNavigation.tsx`.

### 3.5 Build the Rules tab from Workflows.Rules [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/RulesTab.tsx`
- `web/src/components/activity/RulesTabData.ts`
- `web/src/components/activity/RulesTabModel.ts`
- `web/src/components/activity/RulesTabToolbar.tsx`
- `web/src/components/activity/RulesTabFilters.tsx`
- `web/src/components/activity/RulesTabActions.ts`
- `web/src/components/activity/RulesTabDetailPanel.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

The Workflows page's Rules sub-tab moves to a top-level activity-panel tab. Source: `web/src/components/workflows/RulesTab.tsx` and the companion `web/src/components/workflows/RulesTab.css` legacy stylesheet. The `.css` file is legacy and must be retired — all styling moves to Tailwind utilities in the new family.

Ship the multi-file family. Single-view tab (no segment). The bulk-toggle behavior currently in WorkflowsPage (handleBulkToggleRules, rulesAllEnabled state) moves into RulesTabActions and surfaces in the middle-bar action strip as a single "Toggle all" affordance. Register via the three edits.

Note: this task does NOT delete the workflows-page Rules sub-tab — that happens in the P6 Workflows-page deletion gate after all four workflow split tabs (Rules, Stages+Profiles, Agents, Pipelines-defs fold) are in.

**Acceptance:**

- 3.5.1 - RulesTab family is created under the activity components folder (Tab plus Data plus Model plus Toolbar plus Filters plus Actions plus DetailPanel). file: `web/src/components/activity/RulesTab.tsx`.
- 3.5.2 - Tab references no `web/src/components/workflows/RulesTab.css`. behavior: "no workflows RulesTab.css import in activity tab family" verified by a repo-wide grep inside the activity components folder returning no hits.
- 3.5.3 - Middle bar exposes a Toggle-all action wired to the existing rule-bulk-toggle path. symbol: `RulesTabActions` in `web/src/components/activity/RulesTabActions.ts`.
- 3.5.4 - Tab is registered in `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, and `web/src/components/activity/ActivityPanel.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 3.5.5 - Parity checklist enumerates every Workflows.Rules sub-tab capability (rule list, enable toggle, bulk-toggle-all, rule detail, scope filter) and maps each to the new-tab location. behavior: "Rules parity checklist enumerated with new-tab mapping" in the closing commit message and the build-task description.
- 3.5.6 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.

### 3.6 Build the Stages+Profiles tab with a Stages | Profiles segment [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/StagesTab.tsx`
- `web/src/components/activity/StagesTabData.ts`
- `web/src/components/activity/StagesTabModel.ts`
- `web/src/components/activity/StagesTabToolbar.tsx`
- `web/src/components/activity/StagesTabFilters.tsx`
- `web/src/components/activity/StagesTabActions.ts`
- `web/src/components/activity/StagesTabDetailPanel.tsx`
- `web/src/components/activity/StagesPane.tsx`
- `web/src/components/activity/ProfilesPane.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Stages and Profiles both shape the lifecycle manifest (stages = registry entries; profiles = preset overlays that resolve into a manifest). One tab, internal segment, keeps the mental model coherent and pre-empts a single-purpose Profiles tab.

Sources: `web/src/components/workflows/StagesTab.tsx` and `web/src/components/workflows/ProfilesTab.tsx`.

Ship the multi-file family. SegmentedControl with stages and profiles segments in StagesTabToolbar (the *new* one, in the activity components folder). Per-view content in StagesPane and ProfilesPane. Register via the three edits using the tab id stages (Profiles is the secondary segment, not a separate tab).

**Acceptance:**

- 3.6.1 - StagesTab family is created under the activity components folder with StagesPane and ProfilesPane. file: `web/src/components/activity/StagesTab.tsx`.
- 3.6.2 - SegmentedControl with stages and profiles segments swaps panes; both render against their live data. symbol: `StagesTabToolbar` in `web/src/components/activity/StagesTabToolbar.tsx`.
- 3.6.3 - Tab is registered with id stages in `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, and `web/src/components/activity/ActivityPanel.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 3.6.4 - Parity checklist enumerates every Workflows.Stages and Workflows.Profiles capability (stage list, registry-row CRUD, profile list, profile-row CRUD, default-profile selection, stage-segment swap) and maps each to the new-tab location. behavior: "Stages+Profiles parity checklist enumerated with new-tab mapping" in the closing commit message and the build-task description.
- 3.6.5 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.

### 3.7 Build the Agents tab from Workflows.Agents [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/AgentsTab.tsx`
- `web/src/components/activity/AgentsTabData.ts`
- `web/src/components/activity/AgentsTabModel.ts`
- `web/src/components/activity/AgentsTabToolbar.tsx`
- `web/src/components/activity/AgentsTabFilters.tsx`
- `web/src/components/activity/AgentsTabActions.ts`
- `web/src/components/activity/AgentsTabDetailPanel.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Source: the workflows-folder AgentsTab module (already multi-file: actions, cards, data, payloads, types). The existing decomposition is the migration starting point — port to the canonical activity-folder family naming (AgentsTabActions, AgentsTabData, etc.) and add the missing pieces (AgentsTabModel, AgentsTabToolbar, AgentsTabFilters, AgentsTabDetailPanel).

Single-view tab (no segment). The card grid from the source page collapses into the canonical top-list plus bottom-detail two-pane. Register via the three edits.

**Acceptance:**

- 3.7.1 - AgentsTab family is created under the activity components folder with the full seven-file decomposition. file: `web/src/components/activity/AgentsTab.tsx`.
- 3.7.2 - Tab is registered in `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, and `web/src/components/activity/ActivityPanel.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 3.7.3 - Parity checklist enumerates every Workflows.Agents capability (agent list, definition detail, enable toggle, spawn from definition, persona apply, payload editing) and maps each to the new-tab location. behavior: "Agents parity checklist enumerated with new-tab mapping" in the closing commit message and the build-task description.
- 3.7.4 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.

### 3.8 Hoist workflow-folder execution helpers out to shared locations [category: refactor] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/lib/executionFormatters.ts`
- `web/src/components/activity/executionUtils.tsx`
- `web/src/components/activity/isolationColors.ts`
- `web/src/components/activity/PipelinesTab.tsx`
- `web/src/components/activity/TracesTab.tsx`
- `web/src/components/activity/CronTab.tsx`
- `web/src/components/cron/RunHistoryTable.tsx`
- `web/src/components/traces/TracesPage.tsx`

Three helper modules in the workflows subtree have live consumers outside that subtree:

- The workflows-folder execution-formatters module (pure formatter functions formatDateTime, formatDuration, formatTime) is imported by the activity-folder PipelinesTab, the activity-folder TracesTab, the activity-folder CronTab, and the traces-subtree TracesPage.
- The workflows-folder execution-utils module (React components PipelineStatusDot, StepDisplay, ChevronIcon, plus the StepData type) is imported by the activity-folder PipelinesTab and the cron-subtree RunHistoryTable.
- The workflows-folder isolation-colors module (color mapping by isolation mode) — confirm consumers via `gcode usages isolationColors` and hoist if any live outside the workflows subtree.

P6's 6.1 deletes the entire workflows subtree. Without this hoist, that deletion breaks the four live activity-tab consumers; with this hoist, 6.1 deletes already-orphan files.

Steps:

- Move the three pure formatter functions into the new lib-folder execution-formatters module (lib is the canonical home for pure cross-domain helpers in this repo).
- Move the React execution helpers into the new activity-folder execution-utils module (these components only render inside activity-panel tabs).
- Move the isolation-color map into the new activity-folder isolation-colors module (or fold into the new execution-utils module if it is small and tightly coupled).
- Update the import paths in the five live consumers — the activity-folder PipelinesTab, TracesTab, and CronTab; the cron-subtree RunHistoryTable; the traces-subtree TracesPage — to point at the new shared paths. (Yes, this includes the two consumers that will be deleted in 5.2 and 5.3 — those deletions remove the import along with the module, no extra work, no risk window.)
- Leave the original three workflows-folder helper modules in place; 6.1 deletes them as orphans. The scoped grep `grep -rn "from.*workflows/execution-utils\|from.*workflows/executionFormatters\|from.*workflows/isolationColors" web/src | grep -v "^web/src/components/workflows/"` must return no hits after the hoist; the whole-tree grep still returns workflows-internal self-imports (those go away when 6.1 deletes the subtree).

**Acceptance:**

- 3.8.1 - New shared modules exist at the new lib-folder execution-formatters path (pure functions) and the new activity-folder execution-utils path (React components plus types). file: `web/src/lib/executionFormatters.ts`. file: `web/src/components/activity/executionUtils.tsx`.
- 3.8.2 - Every live consumer of the workflows-folder execution helpers imports from the new shared paths. file: `web/src/components/activity/PipelinesTab.tsx`. file: `web/src/components/activity/TracesTab.tsx`. file: `web/src/components/activity/CronTab.tsx`. file: `web/src/components/cron/RunHistoryTable.tsx`. file: `web/src/components/traces/TracesPage.tsx`.
- 3.8.3 - Outside-workflows grep `grep -rn "from.*workflows/execution-utils\|from.*workflows/executionFormatters\|from.*workflows/isolationColors" web/src | grep -v "^web/src/components/workflows/"` returns no hits. Workflows-internal self-imports remain by design and are cleaned up by 6.1's subtree deletion. behavior: "no workflows-folder helper imports remain outside the workflows subtree after hoist" verified by the scoped grep.
- 3.8.4 - `cd web && npm run type-check` plus `cd web && npm run test -- --run` (scoped to touched files) plus `npx eslint` clean after the hoist. behavior: "scoped CI green after hoist" in `web/` test scope.

## P4: Pipelines fold and Integrations stub
`kind: framing`

**Goal**: Bring the Workflows page's Pipelines (definitions) sub-tab into the existing pipelines panel tab as a Live | Defs segment, and add a stub Integrations tab so the surface is reserved for future work.

### 4.1 Add a Live | Defs segment to the existing pipelines panel tab [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/PipelinesTab.tsx`
- `web/src/components/activity/PipelinesLivePane.tsx`

Today's `web/src/components/activity/PipelinesTab.tsx` (322L) shows live executions. The Workflows page's Pipelines sub-tab shows YAML definitions. They're the same noun viewed two ways — same fold as Sessions' Live and Expired segment. Add a SegmentedControl with live and defs segments in the toolbar; default segment is live (existing behavior preserved). Persist the active segment under a new localStorage key (for example gobby-pipelines-segment-v1).

Refactor PipelinesTab to extract a PipelinesLivePane containing the current rendering, leaving the parent shell with just the toolbar plus segment plus selected pane. This keeps the file well under the 1000-line ceiling and prepares for 4.2.

**Acceptance:**

- 4.1.1 - PipelinesTab renders a SegmentedControl with live and defs segments in its toolbar; the default value is live. symbol: `PipelinesTab` in `web/src/components/activity/PipelinesTab.tsx`.
- 4.1.2 - Live execution rendering is extracted to PipelinesLivePane. file: `web/src/components/activity/PipelinesLivePane.tsx`.
- 4.1.3 - Segment selection persists across reload via the new localStorage key. behavior: "segment selection survives reload" verified in chrome-devtools.

### 4.2 Migrate Workflows.Pipelines (definitions) into the Defs pane with an impeccable IA pass [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `web/src/components/activity/PipelinesDefsPane.tsx`
- `web/src/components/activity/PipelinesTab.tsx`

Source: `web/src/components/workflows/PipelinesTab.tsx` (the workflows-page sub-tab variant). Move the YAML-definitions list and PipelineEditor-driven detail into PipelinesDefsPane. The source page's card grid (pipeline cards like dev, expand-task, gobby-merge, merge-clone) collapses into a list/detail two-pane: pipeline name plus PIPELINE badge plus 1-line description in the list; tags, enable toggle, action icons, and the YAML editor in the detail panel below the resize handle.

Wire PipelinesDefsPane into the defs segment branch added in 4.1. The workflows-page PipelinesTab module is not deleted here — its deletion is part of P6's Workflows-page gate after all four workflow-split tabs are in.

**Acceptance:**

- 4.2.1 - PipelinesDefsPane renders against the workflows pipeline-definitions data source. file: `web/src/components/activity/PipelinesDefsPane.tsx`.
- 4.2.2 - Selecting the defs segment swaps live executions for the definitions list. behavior: "Live and Defs swap shows correct content in chrome-devtools verification" in chrome-devtools verification.
- 4.2.3 - Pipeline cards no longer use a 4-column card-grid layout in the narrow panel. behavior: "list/detail two-pane replaces card grid in narrow panel" verified by chrome-devtools screenshot.

### 4.3 Build the Integrations stub tab [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/IntegrationsTab.tsx`
- `web/src/components/activity/IntegrationsTabData.ts`
- `web/src/components/activity/IntegrationsTabModel.ts`
- `web/src/components/activity/IntegrationsTabToolbar.tsx`
- `web/src/components/activity/IntegrationsTabFilters.tsx`
- `web/src/components/activity/IntegrationsTabActions.ts`
- `web/src/components/activity/IntegrationsTabDetailPanel.tsx`
- `web/src/components/activity/IntegrationsChannelsPane.tsx`
- `web/src/components/activity/IntegrationsMessagesPane.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Integrations is in the hamburger nav (createAppNavItems IntegrationsIcon entry) but NOT in APP_VALID_TABS — the page is a stub today. The migration adds it as a reserved future surface: a Channels and Messages two-pane shell with proper empty states, no live data.

Source page satellites that can inform the shell: `web/src/components/integrations/ChannelCard.tsx`, `web/src/components/integrations/ChannelDetail.tsx`, `web/src/components/integrations/ChannelForm.tsx`, `web/src/components/integrations/MessageList.tsx`, `web/src/components/integrations/channelMetadata.ts`. Most are stubs themselves; port shapes and discard styles. `web/src/components/integrations/styles.ts` is legacy class-string module — retire it; Tailwind only.

Ship the multi-file family. SegmentedControl with channels and messages segments in the toolbar. Both panes render ActivityPanelEmpty with surface-specific copy ("Connect a channel to start", "Messages will appear here once a channel is linked"). Register via the three edits using tab id integrations.

**Acceptance:**

- 4.3.1 - IntegrationsTab family is created with both panes rendering ActivityPanelEmpty. file: `web/src/components/activity/IntegrationsTab.tsx`.
- 4.3.2 - The legacy integrations/styles class-string module is not referenced from the new family. behavior: "no integrations/styles import in activity IntegrationsTab family" verified by a repo-wide grep inside the activity components folder returning no hits.
- 4.3.3 - Tab is registered with id integrations in `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, and `web/src/components/activity/ActivityPanel.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 4.3.4 - Parity checklist documents that the stub tab intentionally exposes no live capabilities (no live Integrations capabilities exist to map; empty-state copy and reserved-surface intent are explicit). behavior: "Integrations stub parity checklist documents intentional empty state" in the closing commit message and the build-task description.
- 4.3.5 - Tab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist. behavior: "grayscale and AA and touch-target gates pass" in chrome-devtools verification.

### 4.4 Delete IntegrationsPage and the hamburger entry for Integrations [category: refactor] (depends: 4.3)
`kind: deliverable`

Targets:
- `web/src/components/integrations/IntegrationsPage.tsx`
- `web/src/components/integrations/ChannelCard.tsx`
- `web/src/components/integrations/ChannelDetail.tsx`
- `web/src/components/integrations/ChannelForm.tsx`
- `web/src/components/integrations/MessageList.tsx`
- `web/src/components/integrations/channelMetadata.ts`
- `web/src/components/integrations/styles.ts`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Parity-and-cleanup gate for the Integrations stub. The current `web/src/components/integrations/IntegrationsPage.tsx` is a stub page lazy-imported by AppPages.tsx (line 22 region) and rendered in App.tsx (the `activeTab === "integrations"` branch around line 902), with the createAppNavItems entry at line 64 of appNavigation.tsx. Six private satellites live alongside the page: ChannelCard.tsx, ChannelDetail.tsx, ChannelForm.tsx, MessageList.tsx, channelMetadata.ts, styles.ts. Repo-wide grep confirms no consumers outside the integrations subtree.

Steps mirror 2.2's shape exactly:

- In `web/src/components/app/appNavigation.tsx`: remove the createAppNavItems Integrations entry and the `IntegrationsIcon` import that becomes orphan.
- In `web/src/components/app/AppPages.tsx`: remove the lazy import for IntegrationsPage and the `IntegrationsPage` re-export.
- In `web/src/App.tsx`: remove the `activeTab === "integrations"` render branch and the `IntegrationsPage` import line.
- Delete the seven integrations-subtree files. Confirm with a repo-wide grep that `from.*components/integrations/` returns no hits.

**Acceptance:**

- 4.4.1 - IntegrationsPage and the six private satellites in the integrations subtree are deleted. file: `web/src/components/integrations/IntegrationsPage.tsx`.
- 4.4.2 - App shell no longer has an `activeTab === "integrations"` branch; AppPages no longer lazy-imports IntegrationsPage. file: `web/src/App.tsx`. file: `web/src/components/app/AppPages.tsx`.
- 4.4.3 - The createAppNavItems Integrations entry and the now-orphan IntegrationsIcon import are removed from `web/src/components/app/appNavigation.tsx`. symbol: `createAppNavItems` in `web/src/components/app/appNavigation.tsx`.
- 4.4.4 - Repo-wide grep for `from.*components/integrations/` returns no hits. behavior: "no orphan integrations references" verified by repo-wide grep.

## P5: Standalone deletions and page dedups
`kind: framing`

**Goal**: Three independent cleanup tasks — Reports gets deleted outright (no replacement), and the Cron and Traces hamburger pages get folded into their already-shipped panel tabs and their pages deleted. These three are parallel with each other and with the P3/P4 surface work.

### 5.1 Delete ReportsPage outright with no replacement [category: refactor] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/workflows/ReportsPage.tsx`
- `web/src/components/workflows/ReportsPage.PipelineDetail.tsx`
- `web/src/components/workflows/ReportsPage.AgentDetail.tsx`
- `web/src/components/workflows/ReportsPage.styles.ts`
- `web/src/components/workflows/ReportsPage.helpers.ts`
- `web/src/components/workflows/ReportsPage.useResizablePanel.ts`
- `web/src/components/workflows/ReportsPage.icons.tsx`
- `web/src/components/workflows/reports-page.css`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Reports is deleted, not migrated. Remove the page file (769L) and its private satellites: ReportsPage.PipelineDetail (245L), ReportsPage.AgentDetail (224L), ReportsPage.styles (192L), ReportsPage.helpers (148L), ReportsPage.useResizablePanel (83L), ReportsPage.icons (74L), reports-page.css. Confirm no imports outside the reports set via a repo-wide grep returning no hits.

Remove the reports entry from the app-navigation module (including the now-orphan `ReportsIcon` import line, required by `noUnusedLocals` plus `@typescript-eslint/no-unused-vars`), the AppPages lazy import, and the activeTab equals reports branch in the App shell.

**Acceptance:**

- 5.1.1 - ReportsPage and its six satellites plus the CSS file are deleted. file: `web/src/components/workflows/ReportsPage.tsx`.
- 5.1.2 - APP_VALID_TABS no longer contains reports; the App shell no longer has the reports render branch. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`. file: `web/src/App.tsx`.
- 5.1.3 - Repo-wide grep for ReportsPage and reports-page returns no hits. behavior: "no orphan Reports references" verified by repo-wide grep.
- 5.1.4 - The orphan `ReportsIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `ReportsIcon` in `web/src/components/app/appNavigation.tsx`.

### 5.2 Fold CronJobsPage into the existing cron panel tab with an impeccable redesign and delete the page [category: refactor] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/CronTab.tsx`
- `web/src/components/CronJobsPage.tsx`
- `web/src/components/cron/RunHistoryTable.tsx`
- `web/src/components/cron/formatters.ts`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

The existing `web/src/components/activity/CronTab.tsx` (249L) provides the panel-tab two-pane shape but was authored pre-impeccable and references the deprecated CronTab guidance (per 1.1 step 8). `web/src/components/CronJobsPage.tsx` (789L) holds page-only richness (batch-action affordances, run-history table, formatters) that must migrate. The Constraints clause is binding here: this is an impeccable redesign on top of the existing CronTab, not a 1:1 fold.

Page-private satellites in the cron subtree are deleted outright (not hoisted): `web/src/components/cron/RunHistoryTable.tsx` (297L, imported only by CronJobsPage) and `web/src/components/cron/formatters.ts` (24L, imported only by CronJobsPage). Any row-rendering behavior worth preserving from RunHistoryTable folds inline into a CronTab satellite (DetailPanel or a dedicated history pane file in the activity-folder cron family). The `formatRelativeTime` function from `cron/formatters.ts` is reimplemented inline in the activity family (or replaced by `formatDateTime` from `web/src/lib/executionFormatters.ts` per 3.8, which is already in scope of this epic). This deliverable does not write to `web/src/lib/executionFormatters.ts`; 3.8 owns that file exclusively.

CronTab post-fold target shape: a single file. CronTab today is 249L; CronJobsPage is 789L. A faithful fold without dead code drops well under the 1000-line ceiling because CronJobsPage's bulk is the in-page detail layout and run-history table — both of which compress sharply when moved into the narrow two-pane (the page-detail layout collapses into the existing detail-pane slot; the run-history table compresses into a list row with detail-pane spans). If a fair implementation pass discovers the ceiling is at risk, that is an implementation-time decision recorded as a follow-up task; this plan commits to single-file CronTab and asserts the ceiling.

Steps:

- Inventory the source page's capabilities (job CRUD, manual trigger, enable/disable, run-history view, last-run formatting, status filtering, schedule editing).
- Map each capability to a CronTab location: toolbar action, middle-bar action, detail-panel section, or filter row.
- Implement the impeccable redesign on CronTab: bring it up to the SessionsTab exemplar (1.1 step 1) — SegmentedControl in the toolbar if multi-view is justified, retire any pre-impeccable styling, no border-left or border-right greater than 1px, deutan-safe state palette for run-status indication.
- Delete `web/src/components/CronJobsPage.tsx`, `web/src/components/cron/RunHistoryTable.tsx`, and `web/src/components/cron/formatters.ts` together with the cron entry in appNavigation.tsx, the now-orphan `CronIcon` import line (required by `noUnusedLocals` plus `@typescript-eslint/no-unused-vars`), the AppPages lazy import, and the `activeTab === "cron"` branch in App.tsx. Confirm with a repo-wide grep that `from.*components/cron/` returns no hits.
- The activity-panel cron tab remains in ActivityPanelTabs and useActivityPanel (already shipped).

**Acceptance:**

- 5.2.1 - Every capability from CronJobsPage is either reachable from the cron panel tab or explicitly documented as dropped in the closing commit. behavior: "Cron parity checklist documented with new-tab mapping" in the closing commit message and the build-task description.
- 5.2.2 - CronJobsPage and the two cron-subtree satellites (RunHistoryTable.tsx, formatters.ts) are deleted; APP_VALID_TABS no longer contains cron. file: `web/src/components/CronJobsPage.tsx`.
- 5.2.3 - The App shell no longer has the cron render branch. file: `web/src/App.tsx`.
- 5.2.4 - Repo-wide grep for `from.*components/cron/` returns no hits. behavior: "no orphan cron-subtree references" verified by repo-wide grep.
- 5.2.5 - Refreshed CronTab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist; no border-left or border-right greater than 1px in the cron list rows. behavior: "Cron tab grayscale and AA and touch-target and side-stripe gates pass" in chrome-devtools verification.
- 5.2.6 - The orphan `CronIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `CronIcon` in `web/src/components/app/appNavigation.tsx`.
- 5.2.7 - `web/src/components/activity/CronTab.tsx` is a single file under 1000 lines after the fold. behavior: "single-file CronTab under anti-monolith ceiling" verified by `wc -l web/src/components/activity/CronTab.tsx`.

### 5.3 Fold TracesPage into the existing traces panel tab with an impeccable redesign and delete the page [category: refactor] (depends: P2)
`kind: deliverable`

Targets:
- `web/src/components/activity/TracesTab.tsx`
- `web/src/components/traces/TracesPage.tsx`
- `web/src/components/traces/TraceDetail.tsx`
- `web/src/components/traces/TraceWaterfall.tsx`
- `web/src/components/traces/llm-utils.ts`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Same shape as 5.2 with a smaller core but the same set of page-private satellites to clean up. `web/src/components/traces/TracesPage.tsx` (145L) is smaller than `web/src/components/activity/TracesTab.tsx` (207L), so the page-side fold is lightweight; the bulk of the work is the impeccable redesign of TracesTab (also pre-impeccable per the 1.1 step 8 pitfall) plus the satellite cleanup.

Page-private satellites in the traces subtree are deleted outright (not hoisted to lib): `TraceDetail.tsx`, `TraceWaterfall.tsx`, and `llm-utils.ts` — repo-wide grep confirms no consumers outside the traces subtree. Behavior worth preserving from TraceDetail folds inline into TracesTab's detail-panel slot or a dedicated TracesDetailPane file in the activity-folder traces family; behavior from TraceWaterfall folds into a TracesWaterfallPane file in the same family. Any pure-formatter functions in `llm-utils.ts` are reimplemented inline in the activity-folder family. This deliverable does not write to `web/src/lib/executionFormatters.ts`; 3.8 owns that file exclusively.

TracesTab today is 207L; TracesPage is 145L. The fold stays well under the 1000-line ceiling even as a single file. Acceptance asserts the single-file post-fold result and the ceiling.

Steps:

- Inventory the source page's capabilities (trace list, trace detail, span waterfall, LLM-specific metadata, time-range filter, search).
- Map each capability to a TracesTab location.
- Implement the impeccable redesign on TracesTab: bring it up to the SessionsTab exemplar (1.1 step 1), retire any pre-impeccable styling, no border-left or border-right greater than 1px, deutan-safe state palette for trace-status indication.
- Delete `web/src/components/traces/TracesPage.tsx` and the three sibling files (TraceDetail.tsx, TraceWaterfall.tsx, llm-utils.ts) together with the traces entry in appNavigation.tsx, the now-orphan `TracesIcon` import line (required by `noUnusedLocals` plus `@typescript-eslint/no-unused-vars`), the AppPages lazy import, and the `activeTab === "traces"` branch in App.tsx. Confirm with a repo-wide grep that `from.*components/traces/` returns no hits.

**Acceptance:**

- 5.3.1 - Every capability from TracesPage is either reachable from the traces panel tab or documented as dropped in the closing commit. behavior: "Traces parity checklist documented with new-tab mapping" in the closing commit message and the build-task description.
- 5.3.2 - TracesPage and the three traces-subtree satellites (TraceDetail.tsx, TraceWaterfall.tsx, llm-utils.ts) are deleted; APP_VALID_TABS no longer contains traces. file: `web/src/components/traces/TracesPage.tsx`.
- 5.3.3 - The App shell no longer has the traces render branch. file: `web/src/App.tsx`.
- 5.3.4 - Repo-wide grep for `from.*components/traces/` returns no hits. behavior: "no orphan traces-subtree references" verified by repo-wide grep.
- 5.3.5 - Refreshed TracesTab passes the grayscale plus dark/light AA plus 44px-target verification per 1.1's per-surface checklist; no border-left or border-right greater than 1px in the trace list or waterfall rows. behavior: "Traces tab grayscale and AA and touch-target and side-stripe gates pass" in chrome-devtools verification.
- 5.3.6 - The orphan `TracesIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `TracesIcon` in `web/src/components/app/appNavigation.tsx`.
- 5.3.7 - `web/src/components/activity/TracesTab.tsx` is a single file under 1000 lines after the fold. behavior: "single-file TracesTab under anti-monolith ceiling" verified by `wc -l web/src/components/activity/TracesTab.tsx`.

## P6: Final gates — Workflows page deletion and hamburger removal
`kind: framing`

**Goal**: Delete the workflows page once all five workflow sub-tabs have migrated, then delete the hamburger trigger and any orphan plumbing once every in-scope migration is in.

### 6.1 Delete WorkflowsPage and its sub-tab files [category: refactor] (depends: 3.5, 3.6, 3.7, 3.8, 4.2)
`kind: deliverable`

Targets:
- `web/src/components/workflows/WorkflowsPage.tsx`
- `web/src/components/workflows/RulesTab.tsx`
- `web/src/components/workflows/RulesTab.css`
- `web/src/components/workflows/StagesTab.tsx`
- `web/src/components/workflows/AgentsTab.tsx`
- `web/src/components/workflows/AgentsTab.actions.ts`
- `web/src/components/workflows/AgentsTab.cards.tsx`
- `web/src/components/workflows/AgentsTab.data.ts`
- `web/src/components/workflows/AgentsTab.payloads.ts`
- `web/src/components/workflows/AgentsTab.types.ts`
- `web/src/components/workflows/ProfilesTab.tsx`
- `web/src/components/workflows/PipelinesTab.tsx`
- `web/src/components/workflows/PipelineEditor.tsx`
- `web/src/components/workflows/PipelineExecutionsView.tsx`
- `web/src/components/workflows/ReportingTab.tsx`
- `web/src/components/workflows/workflows-styles.ts`
- `web/src/components/workflows/pipelines-reporting.css`
- `web/src/components/workflows/executionFormatters.ts`
- `web/src/components/workflows/isolationColors.ts`
- `web/src/components/workflows/execution-utils.tsx`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Parity gate. Only after Rules (3.5), Stages+Profiles (3.6), Agents (3.7), the hoist (3.8), and Pipelines defs fold (4.2) are all in:

- Delete `web/src/components/workflows/WorkflowsPage.tsx` (742L).
- Delete the workflows-folder Tab modules that the splits replaced: RulesTab and RulesTab.css, StagesTab, AgentsTab plus its actions, cards, data, payloads, and types siblings, ProfilesTab, the workflows-folder PipelinesTab (distinct from the activity-folder PipelinesTab), PipelineEditor, PipelineExecutionsView, ReportingTab, workflows-styles, pipelines-reporting.css. Confirm each via repo-wide grep returning no hits outside the deleted set.
- Delete the now-orphan helper modules `executionFormatters`, `isolationColors`, `execution-utils` that 3.8 hoisted out — after the hoist plus the workflows-subtree deletion, these have no live consumers, so deletion is safe. Confirm with the whole-tree grep on `from.*workflows/execution-utils`, `workflows/executionFormatters`, `workflows/isolationColors` under `web/src` returning no hits before deletion (the whole-tree grep is meaningful here because the workflows subtree itself is being deleted in the same deliverable).
- Remove workflows from the app-navigation module (including the now-orphan `WorkflowsIcon` import line, required by `noUnusedLocals` plus `@typescript-eslint/no-unused-vars`), the AppPages lazy import, and the activeTab equals workflows branch in the App shell.
- The activity-panel pipelines tab keeps the Live and Defs segment from P4.

**Acceptance:**

- 6.1.1 - WorkflowsPage is deleted; repo-wide grep for WorkflowsPage returns no hits. file: `web/src/components/workflows/WorkflowsPage.tsx`.
- 6.1.2 - The five workflows-folder Tab modules (RulesTab, StagesTab, AgentsTab, ProfilesTab, the workflows-folder PipelinesTab) are deleted; their activity-folder counterparts remain. file: `web/src/components/workflows/WorkflowsPage.tsx`.
- 6.1.3 - APP_VALID_TABS no longer contains workflows; the App shell no longer has the workflows render branch. file: `web/src/components/app/appNavigation.tsx`. file: `web/src/App.tsx`.
- 6.1.4 - The orphan `WorkflowsIcon` import is removed from `web/src/components/app/appNavigation.tsx`. symbol: `WorkflowsIcon` in `web/src/components/app/appNavigation.tsx`.

### 6.2 Remove the hamburger menu and reduce the header to project + theme + logout [category: refactor] (depends: 2.2, 3.2, 3.4, 4.3, 4.4, 5.1, 5.2, 5.3, 6.1)
`kind: deliverable`

Targets:
- `web/src/App.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/components/app/appNavigation.tsx`

Final gate. Run the exit-criteria checklist from 1.2.

Steps:

- Remove the Sidebar hamburger trigger from the header. Header DOM is project selector plus ThemeToggle plus logout.
- Verify the AppPages module lazy-imports only DashboardPage, ConfigurationPage, ProjectsPage (the three out-of-scope surfaces).
- Verify the App shell has render branches only for chat, dashboard, projects, and configuration (plus the default fallback).
- Verify the app-navigation module's createAppNavItems returns only Chat plus the three out-of-scope entries.
- Verify the icon-import block in `web/src/components/app/appNavigation.tsx`: per-deliverable icon removals from 2.2.4, 3.2.3, 3.4.3, 4.4.3, 5.1.4, 5.2.6, 5.3.6, and 6.1.4 each removed the matching orphan import. By this point the only remaining icon imports must be `ChatIcon`, `ProjectsIcon`, `ConfigurationIcon` (the three out-of-scope surfaces). 6.2 does not own the icon-by-icon removal — each page-deletion deliverable owns its own — but 6.2 owns the final-state assertion.
- Full type-check plus scoped tests plus eslint green.

**Acceptance:**

- 6.2.1 - Header DOM contains project selector plus ThemeToggle plus logout, no hamburger trigger. file: `web/src/App.tsx`.
- 6.2.2 - The AppPages module lazy-imports exactly DashboardPage, ConfigurationPage, ProjectsPage (no other page-suffixed modules). file: `web/src/components/app/AppPages.tsx`.
- 6.2.3 - The App shell's render branches are reduced to chat, dashboard, projects, and configuration (plus default). file: `web/src/App.tsx`.
- 6.2.4 - The icon-import block in `web/src/components/app/appNavigation.tsx` contains exactly three symbols — `ChatIcon`, `ProjectsIcon`, `ConfigurationIcon` — with no other icon imports remaining. symbol: icon-imports block in `web/src/components/app/appNavigation.tsx`.
- 6.2.5 - Full `cd web && npm run type-check && cd web && npm run test -- --run` green; full `npx eslint` clean; `git grep` for the deleted page modules returns no hits. behavior: "epic-close CI clean" in `web/` test scope.

## Plan Changelog
`kind: framing`

- Initial draft seeded from `docs/plans/one-surface-ui-draft.md`. Swapped canonical references CronTab → SessionsTab + TasksTab; dropped the deep-link bridge entirely; removed Configuration / Dashboard / Projects from scope (Configuration is moving to a gear tab separately); split Cron and Traces dedups into independent tasks; deleted Reports outright with no replacement; folded Profiles into the Stages tab as a segment; folded Workflows.Pipelines definitions into the existing pipelines panel tab as a Live and Defs segment; kept Integrations as a stub tab (Channels and Messages two-pane shell with empty states); rewrote section 1.1 to avoid backticked file-name placeholders so the target-coverage linter doesn't trip on prose patterns.
- Round 2 adversary revisions (2026-05-20): (F1) added explicit orphan-icon-import removal step and acceptance item to every page-deletion deliverable that previously left the import dangling (`McpIcon` → 2.2.4; `MemoryIcon` → 3.2.3; `SkillsIcon` → 3.4.3; `ReportsIcon` → 5.1.4; `CronIcon` → 5.2.6; `TracesIcon` → 5.3.6; `WorkflowsIcon` → 6.1.4). Reframed 6.2.4 from icon-by-icon action to final-state assertion ("icon-import block contains exactly ChatIcon, ProjectsIcon, ConfigurationIcon"). Rationale: project enforces `noUnusedLocals` in `web/tsconfig.json` plus `@typescript-eslint/no-unused-vars` in `web/eslint.config.js`, so per-deliverable type-check/eslint gates self-fail if the matching icon import is not removed in the same deliverable. (F2) Scoped 3.8 step 5 + acceptance 3.8.3 from a whole-tree grep to an outside-workflows grep (`grep -rn ... web/src | grep -v "^web/src/components/workflows/"`). Workflows-folder files (ReportsPage, ReportsPage.AgentDetail, ReportsPage.PipelineDetail, ReportingTab, AgentsTab.cards, PipelineExecutionsView, execution-utils self-imports) keep referencing the helpers internally; those go away when 6.1 deletes the workflows subtree. (F3) Removed the optional hoist path from 5.2 and 5.3 — `cron/formatters.ts` and `traces/llm-utils.ts` are deleted outright, not hoisted to `web/src/lib/executionFormatters.ts`; 3.8 owns the lib file exclusively. Any behavior worth preserving from those satellites folds inline into the respective activity-folder tab family. 5.2 and 5.3 no longer reference `web/src/lib/executionFormatters.ts` in their body or Targets, eliminating the undeclared 3.8 dependency. (F4) Committed 5.2 to single-file CronTab post-fold with anti-monolith acceptance (5.2.7 asserts `wc -l web/src/components/activity/CronTab.tsx` under 1000); same treatment for 5.3 (5.3.7).
- Round 1 adversary revisions (2026-05-20): (F6+N1) corrected SessionsTab option-list line citation from "lines 59–60" to "line 58" (`STATUS_MODE_OPTIONS` declaration), and removed the three read-only mcp-subtree source files from 2.1's Targets list (they appear correctly in 2.2's deletion Targets). (F5) added parity-checklist + grayscale/AA/touch-target acceptance items to every build deliverable (2.1, 3.1, 3.3, 3.5, 3.6, 3.7, 4.3) and to the two page-fold deliverables (5.2, 5.3). (F2) added new 3.8 hoist deliverable to relocate `workflows/executionFormatters.ts`, `workflows/execution-utils.tsx`, and `workflows/isolationColors.ts` to shared locations (`web/src/lib/executionFormatters.ts` and `web/src/components/activity/executionUtils.tsx`) and update the five live consumers (activity/PipelinesTab, activity/TracesTab, activity/CronTab, cron/RunHistoryTable, traces/TracesPage) before 6.1 deletes the workflows subtree; updated 6.1's depends list to include 3.8 and reframed 6.1's deletion of the three helpers as orphan cleanup. (F3) extended 5.2's Targets and acceptance to include the cron-subtree satellites `cron/RunHistoryTable.tsx` and `cron/formatters.ts` and extended 5.3's Targets and acceptance to include the traces-subtree satellites `traces/TraceDetail.tsx`, `traces/TraceWaterfall.tsx`, and `traces/llm-utils.ts`. (F4) restructured 5.2 and 5.3 as impeccable redesigns on top of the existing pre-impeccable CronTab and TracesTab; added grayscale/AA/touch-target/side-stripe acceptance gates mirroring 2.1.5. (F1) added new 4.4 deliverable to delete the existing `integrations/IntegrationsPage.tsx` and its six private satellites, mirroring 2.2's shape; updated 6.2's depends list to include 4.4. (N2) enumerated the eight orphan icon imports that 6.2 must remove from `appNavigation.tsx` after the in-scope deletions land (`McpIcon`, `MemoryIcon`, `SkillsIcon`, `IntegrationsIcon`, `WorkflowsIcon`, `ReportsIcon`, `CronIcon`, `TracesIcon`).

## M1 Task Manifest
`kind: framing`

```yaml
- title: Codify the two-pane tab recipe and view-selector convention
  category: docs
  task_type: feature
  depends_on: []
  validation_criteria: docs/guides/one-surface-tab-recipe.md exists and contains the eight numbered sections (exemplars, file family, registration, IA rules, view selector, D7 delete, per-surface verification, pitfalls) with SessionsTab and Tasks-prefixed family citations.
  labels:
    - covers:14923:1.1:1.1.1
    - covers:14923:1.1:1.1.2
    - covers:14923:1.1:1.1.3
  assigned_agent: tech-writer
  tdd: false
  source_section: '1.1'
- title: Specify the hamburger-removal final gate
  category: docs
  task_type: feature
  depends_on: []
  validation_criteria: docs/guides/one-surface-final-gate.md exists with the exit-criteria checklist and enumerates Dashboard, Projects, Configuration as out-of-scope surfaces that remain in the hamburger after this epic.
  labels:
    - covers:14923:1.2:1.2.1
    - covers:14923:1.2:1.2.2
  assigned_agent: tech-writer
  tdd: false
  source_section: '1.2'
- title: Build the MCP activity-panel tab (canary)
  category: code
  task_type: feature
  depends_on:
    - '1.1'
  validation_criteria: McpTab multi-file family is registered in ActivityPanelTabs, useActivityPanel, and ActivityPanel; renders against useMcp data; passes parity checklist plus grayscale/AA/touch-target gates in chrome-devtools verification.
  labels:
    - covers:14923:2.1:2.1.1
    - covers:14923:2.1:2.1.2
    - covers:14923:2.1:2.1.3
    - covers:14923:2.1:2.1.4
    - covers:14923:2.1:2.1.5
    - covers:14923:2.1:2.1.6
  assigned_agent: frontend-developer
  tdd: true
  source_section: '2.1'
- title: Delete McpPage and the hamburger entry for MCP
  category: refactor
  task_type: chore
  depends_on:
    - '2.1'
  validation_criteria: McpPage and its two satellites are deleted; APP_VALID_TABS no longer contains mcp; App shell mcp branch removed; orphan McpIcon import removed; scoped type-check, tests, and eslint clean.
  labels:
    - covers:14923:2.2:2.2.1
    - covers:14923:2.2:2.2.2
    - covers:14923:2.2:2.2.3
    - covers:14923:2.2:2.2.4
    - covers:14923:2.2:2.2.5
  assigned_agent: frontend-developer
  tdd: false
  source_section: '2.2'
- title: Build the Memory tab with a List | Graph segment
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: MemoryTab multi-file family with ListPane and GraphPane is registered and renders both views via SegmentedControl; no greater-than-1px side stripes on memory cards; passes parity plus grayscale/AA/touch-target gates.
  labels:
    - covers:14923:3.1:3.1.1
    - covers:14923:3.1:3.1.2
    - covers:14923:3.1:3.1.3
    - covers:14923:3.1:3.1.4
    - covers:14923:3.1:3.1.5
    - covers:14923:3.1:3.1.6
  assigned_agent: frontend-developer
  tdd: true
  source_section: '3.1'
- title: Delete MemoryPage and its hamburger entry
  category: refactor
  task_type: chore
  depends_on:
    - '3.1'
  validation_criteria: MemoryPage and its five satellites are deleted; APP_VALID_TABS no longer contains memory; App shell memory branch removed; orphan MemoryIcon import removed.
  labels:
    - covers:14923:3.2:3.2.1
    - covers:14923:3.2:3.2.2
    - covers:14923:3.2:3.2.3
  assigned_agent: frontend-developer
  tdd: false
  source_section: '3.2'
- title: Build the Skills tab with an Installed | Hub segment
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: SkillsTab multi-file family with InstalledPane and HubPane is registered and renders both views via SegmentedControl; no skills/styles class-string imports in activity family; passes parity plus grayscale/AA/touch-target gates.
  labels:
    - covers:14923:3.3:3.3.1
    - covers:14923:3.3:3.3.2
    - covers:14923:3.3:3.3.3
    - covers:14923:3.3:3.3.4
    - covers:14923:3.3:3.3.5
    - covers:14923:3.3:3.3.6
  assigned_agent: frontend-developer
  tdd: true
  source_section: '3.3'
- title: Delete SkillsPage and its hamburger entry
  category: refactor
  task_type: chore
  depends_on:
    - '3.3'
  validation_criteria: SkillsPage and its eight satellites (including legacy styles.ts) are deleted; APP_VALID_TABS no longer contains skills; App shell skills branch removed; orphan SkillsIcon import removed.
  labels:
    - covers:14923:3.4:3.4.1
    - covers:14923:3.4:3.4.2
    - covers:14923:3.4:3.4.3
  assigned_agent: frontend-developer
  tdd: false
  source_section: '3.4'
- title: Build the Rules tab from Workflows.Rules
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: RulesTab multi-file family is registered; no workflows/RulesTab.css imports in activity family; bulk Toggle-all wired in the middle bar; passes parity plus grayscale/AA/touch-target gates.
  labels:
    - covers:14923:3.5:3.5.1
    - covers:14923:3.5:3.5.2
    - covers:14923:3.5:3.5.3
    - covers:14923:3.5:3.5.4
    - covers:14923:3.5:3.5.5
    - covers:14923:3.5:3.5.6
  assigned_agent: frontend-developer
  tdd: true
  source_section: '3.5'
- title: Build the Stages+Profiles tab with a Stages | Profiles segment
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: StagesTab multi-file family with StagesPane and ProfilesPane is registered under tab id stages; SegmentedControl swaps both views against live data; passes parity plus grayscale/AA/touch-target gates.
  labels:
    - covers:14923:3.6:3.6.1
    - covers:14923:3.6:3.6.2
    - covers:14923:3.6:3.6.3
    - covers:14923:3.6:3.6.4
    - covers:14923:3.6:3.6.5
  assigned_agent: frontend-developer
  tdd: true
  source_section: '3.6'
- title: Build the Agents tab from Workflows.Agents
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: AgentsTab multi-file family is registered; card grid collapses to canonical top-list plus bottom-detail two-pane; passes parity plus grayscale/AA/touch-target gates.
  labels:
    - covers:14923:3.7:3.7.1
    - covers:14923:3.7:3.7.2
    - covers:14923:3.7:3.7.3
    - covers:14923:3.7:3.7.4
  assigned_agent: frontend-developer
  tdd: true
  source_section: '3.7'
- title: Hoist workflow-folder execution helpers out to shared locations
  category: refactor
  task_type: chore
  depends_on:
    - '2.2'
  validation_criteria: New shared modules at web/src/lib/executionFormatters.ts and web/src/components/activity/executionUtils.tsx; five live consumers updated; outside-workflows grep returns no hits; scoped CI clean.
  labels:
    - covers:14923:3.8:3.8.1
    - covers:14923:3.8:3.8.2
    - covers:14923:3.8:3.8.3
    - covers:14923:3.8:3.8.4
  assigned_agent: frontend-developer
  tdd: false
  source_section: '3.8'
- title: Add a Live | Defs segment to the existing pipelines panel tab
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: PipelinesTab toolbar renders a SegmentedControl with live and defs (default live); live rendering extracted to PipelinesLivePane; segment selection persists across reload via a new localStorage key.
  labels:
    - covers:14923:4.1:4.1.1
    - covers:14923:4.1:4.1.2
    - covers:14923:4.1:4.1.3
  assigned_agent: frontend-developer
  tdd: true
  source_section: '4.1'
- title: Migrate Workflows.Pipelines (definitions) into the Defs pane with an impeccable IA pass
  category: code
  task_type: feature
  depends_on:
    - '4.1'
  validation_criteria: PipelinesDefsPane renders the workflows pipeline-definitions data source; selecting defs swaps to definitions list; narrow-panel layout uses list/detail two-pane, not a 4-column card grid.
  labels:
    - covers:14923:4.2:4.2.1
    - covers:14923:4.2:4.2.2
    - covers:14923:4.2:4.2.3
  assigned_agent: frontend-developer
  tdd: true
  source_section: '4.2'
- title: Build the Integrations stub tab
  category: code
  task_type: feature
  depends_on:
    - '2.2'
  validation_criteria: IntegrationsTab multi-file family with ChannelsPane and MessagesPane renders ActivityPanelEmpty in both panes; legacy integrations/styles is not imported by the activity family; registered with tab id integrations; passes grayscale/AA/touch-target gates.
  labels:
    - covers:14923:4.3:4.3.1
    - covers:14923:4.3:4.3.2
    - covers:14923:4.3:4.3.3
    - covers:14923:4.3:4.3.4
    - covers:14923:4.3:4.3.5
  assigned_agent: frontend-developer
  tdd: true
  source_section: '4.3'
- title: Delete IntegrationsPage and the hamburger entry for Integrations
  category: refactor
  task_type: chore
  depends_on:
    - '4.3'
  validation_criteria: IntegrationsPage and its six satellites are deleted; App shell integrations branch and AppPages lazy import removed; createAppNavItems Integrations entry and orphan IntegrationsIcon import removed; repo-wide grep for components/integrations returns no hits.
  labels:
    - covers:14923:4.4:4.4.1
    - covers:14923:4.4:4.4.2
    - covers:14923:4.4:4.4.3
    - covers:14923:4.4:4.4.4
  assigned_agent: frontend-developer
  tdd: false
  source_section: '4.4'
- title: Delete ReportsPage outright with no replacement
  category: refactor
  task_type: chore
  depends_on:
    - '2.2'
  validation_criteria: ReportsPage and its six satellites plus reports-page.css are deleted; APP_VALID_TABS no longer contains reports; App shell reports branch removed; orphan ReportsIcon import removed; repo-wide grep for ReportsPage and reports-page returns no hits.
  labels:
    - covers:14923:5.1:5.1.1
    - covers:14923:5.1:5.1.2
    - covers:14923:5.1:5.1.3
    - covers:14923:5.1:5.1.4
  assigned_agent: frontend-developer
  tdd: false
  source_section: '5.1'
- title: Fold CronJobsPage into the existing cron panel tab with an impeccable redesign and delete the page
  category: refactor
  task_type: chore
  depends_on:
    - '2.2'
  validation_criteria: CronJobsPage and the cron-subtree satellites (RunHistoryTable, formatters) are deleted; cron parity checklist captured; App shell cron branch removed; orphan CronIcon import removed; CronTab passes redesign gates and remains a single file under 1000 lines.
  labels:
    - covers:14923:5.2:5.2.1
    - covers:14923:5.2:5.2.2
    - covers:14923:5.2:5.2.3
    - covers:14923:5.2:5.2.4
    - covers:14923:5.2:5.2.5
    - covers:14923:5.2:5.2.6
    - covers:14923:5.2:5.2.7
  assigned_agent: frontend-developer
  tdd: false
  source_section: '5.2'
- title: Fold TracesPage into the existing traces panel tab with an impeccable redesign and delete the page
  category: refactor
  task_type: chore
  depends_on:
    - '2.2'
  validation_criteria: TracesPage and the traces-subtree satellites (TraceDetail, TraceWaterfall, llm-utils) are deleted; traces parity checklist captured; App shell traces branch removed; orphan TracesIcon import removed; TracesTab passes redesign gates and remains a single file under 1000 lines.
  labels:
    - covers:14923:5.3:5.3.1
    - covers:14923:5.3:5.3.2
    - covers:14923:5.3:5.3.3
    - covers:14923:5.3:5.3.4
    - covers:14923:5.3:5.3.5
    - covers:14923:5.3:5.3.6
    - covers:14923:5.3:5.3.7
  assigned_agent: frontend-developer
  tdd: false
  source_section: '5.3'
- title: Delete WorkflowsPage and its sub-tab files
  category: refactor
  task_type: chore
  depends_on:
    - '3.5'
    - '3.6'
    - '3.7'
    - '3.8'
    - '4.2'
  validation_criteria: WorkflowsPage plus the five workflows-folder Tab modules plus the three now-orphan helpers are deleted; APP_VALID_TABS no longer contains workflows; App shell workflows branch removed; orphan WorkflowsIcon import removed.
  labels:
    - covers:14923:6.1:6.1.1
    - covers:14923:6.1:6.1.2
    - covers:14923:6.1:6.1.3
    - covers:14923:6.1:6.1.4
  assigned_agent: frontend-developer
  tdd: false
  source_section: '6.1'
- title: Remove the hamburger menu and reduce the header to project + theme + logout
  category: refactor
  task_type: chore
  depends_on:
    - '2.2'
    - '3.2'
    - '3.4'
    - '4.3'
    - '4.4'
    - '5.1'
    - '5.2'
    - '5.3'
    - '6.1'
  validation_criteria: Header DOM contains project selector plus ThemeToggle plus logout; AppPages lazy-imports only DashboardPage/ConfigurationPage/ProjectsPage; App shell render branches reduced to chat/dashboard/projects/configuration plus default; icon-import block contains exactly ChatIcon/ProjectsIcon/ConfigurationIcon; full type-check, tests, and eslint clean.
  labels:
    - covers:14923:6.2:6.2.1
    - covers:14923:6.2:6.2.2
    - covers:14923:6.2:6.2.3
    - covers:14923:6.2:6.2.4
    - covers:14923:6.2:6.2.5
  assigned_agent: frontend-developer
  tdd: false
  source_section: '6.2'
```
