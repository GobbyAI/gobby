# Plan — One-surface UI: migrate all hamburger pages into the activity panel

## Context

The web UI today splits between two navigation models: a hamburger menu of
full-page hash routes (`#workflows`, `#mcp`, `#configuration`, …) that *replace*
the chat shell, and the activity panel's tab dropdown (Sessions, Tasks, Plans,
…) that lives *beside* chat. Switching to any hamburger page tears the user out
of web chat. The goal is one surface: everything reachable from the activity
panel dropdown, chat never lost.

This is not greenfield. Epic **#14767 "One-surface UI: Tasks in the activity
panel"** already proved the pattern end-to-end (D1–D7, commits
`b94b9874b`…`332d2aa92`): Tasks was fully migrated out of the hamburger into a
two-pane activity tab and its standalone page deleted. The tri-state layout
keystone (D1 #14768, `chat|split|panel`) is shipped shared infra — new surfaces
inherit it for free. This plan replicates that proven path for every remaining
hamburger surface, then deletes the hamburger.

Design system is binding: `/Users/josh/Projects/gobby/.impeccable.md` (dark
default, hue-125 accent, deutan-safe state palette by lightness then hue —
"color is the fourth signal and must not lie", WCAG 2.2 AA, 44px touch targets,
no >1px color stripes, no gradient text, Vercel/Linear restraint, Tailwind only
— **no new legacy CSS**, JS `useIsMobile()` at the canonical **768px** breakpoint,
no Tailwind `md:` variants). Each surface migration is also an impeccable
redesign, **not** a 1:1 port. Verify in the running UI via chrome-devtools MCP
at `http://localhost:60889/#chat`.

## Confirmed decisions (user)

- The existing `ActivityPanelTabs` dropdown is the scaling mechanism. No new nav
  architecture — add tab entries and build tab bodies.
- Every surface follows the **SessionsTab/TasksTab two-pane pattern**: list/tree
  on top, detail/editor on the bottom (resizable via `ResizeHandle`), action
  buttons in the middle status bar. Surfaces are *not* full-page-dense — decompose
  (MCP = server list → server detail/editor; same shape for Rules/Agents/Skills).
- Multi-view surfaces get an in-tab **view selector** reusing `SegmentedControl`
  (the pattern `CronTab` uses today).
- Cron and Traces tabs are canonical; fold any page-only richness behind a view
  selector, then delete `CronJobsPage`/`TracesPage`.
- The hamburger menu is **removed entirely** once every surface is migrated.
  Header keeps project selector + theme + logout.
- Legacy hashes (`#mcp`, `#workflows`, …) must **not** dead-end — preserve
  deep-link continuity to chat + the corresponding panel tab. (Explicit
  divergence from the Tasks path, which did no redirect.)
- Artifacts/Changes tabs verified functional (WS `artifact_event`/`show_file`;
  edit-tool-calls + `/api/files/git-diff`) — keep, not in scope.

## Architecture: the deep-link bridge (the one novel piece)

Everything else is a proven copy of the Tasks path; this is the only new
mechanism. Today `App.tsx activeTab` (hamburger router, hash-synced at
`App.tsx:185–192`) and `useActivityPanel.activeTab` (panel) are disconnected,
and `useActivityPanel` only mounts inside `ChatPage` (rendered when
`App.tsx activeTab === "chat"`). Approach — a one-shot pending-tab handoff, not
a router rewrite:

1. New `web/src/components/app/legacyHashRoutes.ts`:
   `LEGACY_HASH_TO_PANEL_TAB: Record<string, ActivityTab>` mapping
   `workflows|reports|memory|skills|mcp|integrations|configuration|projects|cron|traces`
   → the panel tab id.
2. `App.tsx` initial state (`:185`): if `hash ∈ LEGACY_HASH_TO_PANEL_TAB`, set
   `activeTab = "chat"` and stash the target in a new `pendingPanelTab` state;
   rewrite the hash to `#chat` so the `:191` hash-write effect does not loop.
3. Thread `pendingPanelTab` + an `onConsumed` clearer into `ChatPage` →
   `useActivityPanel`. On mount, if set, call the existing
   `showTab(pendingPanelTab)` (already sets the tab *and* opens the panel to
   `split`/`panel` — exactly the desired UX), then clear.
4. The map is the durable resolver: it keeps resolving legacy hashes after each
   surface's `APP_VALID_TABS` string is removed, so old bookmarks never
   dead-end. Unknown hash → `chat` + sessions (current default, preserved).
5. Also write a canonical shareable hash on consume (e.g. `#chat`); keep bare
   `#mcp` working as a permanent alias.

This is one Phase-0 task; every surface's deletion step just adds one map line.
Existing intra-app hooks `handleNavigateToTrace`/`handleNavigateToPipelineExecution`
(`App.tsx:208–219`, with `initialTraceId`/`initialPipelineExecutionId`) fold into
the Cron/Traces tab selection API during dedupe (#9).

## Canonical per-surface recipe (repeatable, mirrors #14767 D-build→D-IA→D-views→D7)

**A. Claim** the surface's D-build task (CLAUDE.md rule 3) before any edit.

**B. Read** the source page + its satellites; inventory data hook, sub-views,
actions, detail content.

**C. Create the tab file family** under `web/src/components/activity/`, mirroring
the `Tasks*` family (anti-monolith — never one mega-file):
- `<Surface>Tab.tsx` — shell: toolbar (optional `SegmentedControl` view
  selector) · top list/tree (`overflow-y-auto`) · `ResizeHandle` vertical ·
  bottom detail with the `h-10 bg-[var(--bg-secondary)]` status strip holding
  action buttons (the "middle bar"). Copy `CronTab.tsx`'s resize/selection wiring.
- `<Surface>TabData.ts` — existing hook (`useMcp`/`useMemory`/…) + `useWebSocketEvent`.
- `<Surface>TabModel.ts` — pure filter/derive selectors (keeps `.tsx` < 1000L).
- `<Surface>TabToolbar.tsx` — `ActivityPanelSearch` + filter trigger + view selector.
- `<Surface>TabFilters.tsx` — `FilterSection`/`FilterCheckboxRow` + `ActivityFilterFooter`.
- `<Surface>TabActions.ts` — mutations.
- `<Surface>TabDetailPanel.tsx` — the impeccable IA redesign concentrates here.
- Empty states via `ActivityPanelEmpty`.

**D. Register (exactly 3 edits, as Tasks did):**
1. `ActivityPanelTabs.tsx`: add to `ActivityTab` union + `ACTIVITY_PANEL_TABS[]`
   (icon = the `appNavigation.tsx` glyph re-pathed to 24×24 outline).
2. `useActivityPanel.ts`: add to `VALID_TABS[]` (auto-persists active tab).
3. `ActivityPanel.tsx`: import + `case` in `tabContent()` (~268–342); add to
   `ActivityPanelProps` (~46–91) and thread App→ChatPage→ActivityPanel only if
   the surface needs more than `projectId` (most don't).

**E. D-IA pass (impeccable redesign, mandatory, not a 1:1 port).** Apply
`.impeccable.md` fully. Collapse the page's wide multi-column layout into the
narrow panel by promoting one primary axis to the list and moving the rest to
detail + filters (same move as the prior-art board→list pivot). Retire any
legacy `*_CLS` class strings the source dragged in (Tailwind only).

**F. D-views pass (multi-view surfaces only).** Replace the page's sub-tab bar
with `SegmentedControl` in `activity-panel-toolbar`, exactly like
`CronTab.tsx:94`. Per-view content in `<Surface><View>Pane.tsx` files.

**G. Verify** (chrome-devtools MCP at `http://localhost:60889/#chat`): tab in
dropdown, both panes render, `ResizeHandle` drags, middle-bar actions perform
real mutations (WS round-trip); SegmentedControl swaps views; 768px resize both
ways with no stuck state and `localStorage['gobby-activity-panel-tab-v2']`
correct; grayscale screenshot — state legible without hue (lightness + icon +
position); AA contrast + brand focus ring in **dark and light**; 44px targets;
deep-link `#<surface>` → chat + tab + panel (post-Phase-0); **parity checklist**
— enumerate every old-page capability, confirm each reachable in the tab.

**H. D7 delete (parity gate — only when the tab fully replaces the page):**
- `appNavigation.tsx`: remove the `createAppNavItems()` entry + the
  `APP_VALID_TABS` string + the `APP_NAV_PAGES` entry.
- `AppPages.tsx`: remove the `lazy(() => import("…<Surface>Page"))`.
- `App.tsx`: remove the `activeTab === "<surface>"` render branch (~860–901)
  and add the `LEGACY_HASH_TO_PANEL_TAB` entry.
- Delete `<Surface>Page.tsx` + its *private* satellites (check imports first —
  `FilesTab`, execution utils are shared, do **not** delete).
- Migrate tests: delete page UI tests, grow
  `components/activity/__tests__/<Surface>Tab*.test.tsx` (Tasks went 0→11);
  keep hook tests.

**I. Commit** path-scoped `[gobby-#N]`; close the task with the commit.

## gobby-tasks decomposition (create after plan approval)

Structure: root epic → Phase-0 framework tasks (land first, block all) → one
child epic per surface with D-style sub-tasks → final removal gate. Rationale:
#14767 proved D-decomposition + "shared infra first" + parity gate; flat tasks
lose that discipline.

**ROOT EPIC** — "One-surface UI: migrate all hamburger pages into activity-panel
tabs; remove the hamburger menu". Acceptance: hamburger deleted; header =
project + theme + logout; every legacy hash deep-links to chat+tab; no `*Page`
orphan; all page tests migrated; `.impeccable.md` pass per surface.

**PHASE 0 (parallel; block every surface epic):**
- `task` code — "Framework: deep-link bridge — legacy hash → chat + panel tab"
  (the Architecture section above; the keystone).
- `task` code — "Framework: two-pane tab scaffold recipe + view-selector
  convention" (codify the file family + checklist in the epic body; annotate
  `CronTab.tsx`/`TasksTab.tsx` as canonical exemplars; no new base abstraction —
  it would fight the per-surface IA redesign).
- `task` chore — "Framework: hamburger-removal final-gate spec" (defines exit
  criteria + the literal final diff; blocked by all surface D7s).

**PER-SURFACE CHILD EPICS** — each with: `task` D-build · `task` D-IA · `task`
D-views (multi-view only) · `task` D7-delete (parity gate) · `task` D-refactor
(pre-emptive for Configuration & Workflows; on-demand elsewhere per CLAUDE.md
rule 2). Recommended order:

| # | Child epic | Cx | View selector |
|---|---|---|---|
| 1 | Tab: Integrations (Channels/Messages) — **reference proof** | S | 2 |
| 2 | Tab: MCP (server list → tool tree → detail) | M | – |
| 3 | Tab: Memory (list vs knowledge-graph) | M | 2 |
| 4 | Tab: Skills (Installed vs Hub) | M | 2 |
| 5 | Tab: Reports (Pipelines vs Agents run history) | M | 2 |
| 6 | Tab: Workflows (Pipelines/Agents/Rules/Stages/Profiles) | L | 5 |
| 7 | Tab: Configuration (6 schema-driven sub-tabs) | L | 6 |
| 8 | Tab: Projects (Overview/Files/Graph/SourceControl/Issues/PRs/CI-CD) | L | 8 |
| 9 | Dedup: fold CronJobsPage/TracesPage into existing Cron/Traces tabs; delete pages | M | yes |
| 10 | Gate: remove hamburger; header = project+theme+logout | S | – |

**Dependencies:** Phase-0 ×3 → all surface epics. #1 is the reference proof;
#2–#9 each `depends_on` #1's D7 (recipe battle-tested once before fan-out), then
run in parallel. #9 needs only the deep-link task. #10 `depends_on` D7 of all
#1–#9. **Parity-gate rule (non-negotiable):** a surface's nav entry is deleted
only in its own D7, only after chrome-devtools confirms tab parity; until then
page and tab co-exist (exactly how Cron/Traces co-exist today).

## Per-surface IA notes & risks

- **Workflows/Pipelines & Reports are NOT duplicates:** activity `PipelinesTab`
  = live runs (`usePipelineExecutions`); workflows Pipelines sub-tab = YAML
  *definitions* (`useWorkflows`); Reports = run *history*. Three distinct nouns —
  state this in the Workflows + Reports epic bodies so no one merges them.
- **Files IS a true duplicate:** `ProjectsPage.tsx:12` already renders the
  activity `FilesTab`. The Projects tab's Files segment reuses the **same**
  `FilesTab` — no copy.
- **Configuration daemon-restart:** no blocking overlay in a panel. Model
  restart as a status-bar affordance in the middle bar → inline progress chip
  using the **info** state (hue 250, icon+lightness, never hue-alone); chat and
  the rest of the panel stay interactive. Pre-emptive D-refactor (944L source).
- **Projects SourceControl/CodeGraph** are panel-hostile; per `.impeccable.md`
  `adapt`, don't amputate — promote a primary axis to list+detail; the wide
  CodeGraph segment may open in existing `LayoutMode === 'panel'` (full panel,
  chat hidden). Highest IA risk → sequence last, budget extra D-refactor.
- **Project selection stays in the header.** The Projects tab reads the
  header-selected `projectId` (like every tab); no second selector
  (avoids dual-source-of-truth). Overview segment shows the active project
  read-only with a hint at the header selector.
- **Monolith (CLAUDE.md rule 2):** `TasksTab` 944L / `SessionsTab` 953L only
  stay < 1000 via the multi-file family. Every surface ships ≥5 files;
  Configuration & Workflows get pre-emptive D-refactor; any file crossing 1000L
  mid-edit → stop, create/claim a refactor task before continuing.

## Critical files

- `web/src/App.tsx` — hash routing `:185–192`, render branches `:860–901`,
  `handleNavigateToTrace/PipelineExecution` `:208–219`; deep-link bridge +
  `pendingPanelTab` handoff lives here.
- `web/src/components/app/legacyHashRoutes.ts` — **new**, the durable resolver map.
- `web/src/components/activity/ActivityPanelTabs.tsx` — `ActivityTab` union +
  `ACTIVITY_PANEL_TABS[]`; per-surface registration.
- `web/src/components/activity/useActivityPanel.ts` — `VALID_TABS[]` + `showTab()`
  (the bridge consumes `showTab`).
- `web/src/components/activity/ActivityPanel.tsx` — `tabContent()` `:268–342` +
  `ActivityPanelProps`; per-surface `case`.
- `web/src/components/app/appNavigation.tsx` — `createAppNavItems()` /
  `APP_VALID_TABS` / `APP_NAV_PAGES`; per-surface D7 + final gate.
- `web/src/components/app/AppPages.tsx` — page lazy imports to delete per D7.
- `web/src/components/activity/CronTab.tsx` — canonical single-view two-pane +
  SegmentedControl exemplar to copy.
- `web/src/components/activity/TasksTab.tsx` (+ `Tasks*` family) — canonical
  multi-file decomposition template (anti-monolith).
- Source pages to migrate: `components/integrations/IntegrationsPage.tsx`,
  `components/mcp/McpPage.tsx`, `components/memory/MemoryPage.tsx`,
  `components/skills/SkillsPage.tsx`, `components/workflows/ReportsPage.tsx`,
  `components/workflows/WorkflowsPage.tsx`, `components/ConfigurationPage.tsx`,
  `components/projects/ProjectsPage.tsx`; `components/.../CronJobsPage`,
  `TracesPage` (dedup).

## Verification

**Per surface (gate before D7):**
1. `cd web && npm run type-check` — clean.
2. `cd web && npm run test -- --run` scoped to new `<Surface>Tab*` + migrated
   hook suites — green.
3. `npx eslint <touched files>` — clean.
4. chrome-devtools MCP `http://localhost:60889/#chat` — the Step-G checklist,
   including the explicit **parity checklist** (every old-page capability
   reachable in the tab; missing capability blocks D7) and grayscale + dark/light
   AA + 44px + deep-link checks.

**Epic-wide (gate before #10 and at epic close):**
- No `*Page` lazy import in `AppPages.tsx` except `DashboardPage`; no
  `activeTab === "<surface>"` branch in `App.tsx`.
- `Sidebar` hamburger trigger removed; header DOM = project selector +
  `ThemeToggle` + logout only.
- Scripted chrome-devtools sweep of every legacy hash → each lands on chat +
  correct tab + panel visible; unknown hash → chat/sessions, no console error;
  back/forward stable.
- Full `cd web && npm run type-check && npm run test -- --run` green; full
  `eslint` clean; `git grep` for deleted page modules → none.
