# One-Surface Activity-Panel Migration v2

## Overview
`kind: framing`

The web UI is converging on a single surface: chat on the left, activity panel on the right (tri-state `chat | split | panel` layout), everything reachable from the activity-panel tab selector, chat never lost. This plan supersedes `.gobby/plans/task-14923-one-surface-tabs-migration.md` (May 2026), which is outdated: the Reports/Cron/Traces pages are already deleted, MCP was partially migrated via a different shape than that plan specified (`ActivityMcpTab.tsx` reuses the legacy `McpAddServerModal`), and requirements have evolved — explicit Save/Discard editing, a kebab QuickMenu canon, a Configuration cog + full-screen settings overlay, a full-width graph mode, and a Wiki tab rework.

Scope: migrate every remaining hamburger surface into the activity panel — Rules (highest priority), MCP polish, Agents, Stages+Profiles, Pipelines definitions, Skills, Integrations, Memory — rework the Wiki tab, replace the Configuration page with a header-cog settings overlay, then remove the hamburger and delete the nav-orphaned DashboardPage. End-state header: project selector + theme toggle + configuration cog + logout.

## Constraints
`kind: framing`

- **Design system (`.impeccable.md`) is binding.** Dark default, hue-125 chartreuse accent, deutan-safe state palette (lightness + icon first, hue fourth), WCAG 2.2 AA, 44px touch targets, no border-left/right greater than 1px, no gradient text, Vercel/Linear restraint. Every migration is an impeccable redesign, not a 1:1 port.
- **Implementing agents MUST load the `impeccable` skill before any UI output.** On the `gobby-skills` MCP server via progressive discovery: `list_tools(server_name="gobby-skills")` → `get_tool_schema(..., "get_skill")` → `call_tool(..., "get_skill", {"name": "impeccable"})` — and read `.impeccable.md` at the repo root. Every UI deliverable below repeats this instruction; it is not optional.
- **Save/Discard, not commit-on-blur.** Editor detail panes hold a local draft; explicit **Save** and **Discard** buttons render on the bottom-pane status bar (the `h-10` strip — the same bar that shows "Watching…" on the Sessions tab), styled identically to the existing action buttons there. Dirty state guards selection change, segment change, tab change, and panel close through one shared confirm path. TasksTab's existing per-field commit behavior is intentionally unchanged in this epic.
- **Kebab QuickMenu is canon** for activity top-panel list rows. Row-level actions live behind a three-vertical-dot trigger. **Popup menus must never render off-screen** — the shared primitive measures via `getBoundingClientRect`, flips vertically near the bottom edge, and clamps horizontally.
- **`ui/SegmentedControl` in the activity-panel toolbar always uses `controlHeight="sm"`** (binding directive carried from epic #14767).
- **Teardown gates on spec, not parity.** Several legacy pages contain non-working functions/buttons/menus that are part of what is being cleaned out. Each build deliverable consumes a capability inventory of its legacy source with explicit dispositions — port / fix / drop-as-broken — but the deletion gate is the new tab working per this plan plus a zero-importers grep. Never block deletion on parity with broken legacy UI. Deletions land in a separate commit from the replacement they follow.
- **Tailwind 4 only — no new legacy CSS.** Retire legacy class-string modules (`skills/styles.ts`, `integrations/styles.ts`, `workflows-styles.ts`) and legacy stylesheets (`RulesTab.css`, `pipelines-reporting.css`) as their consumers migrate.
- **JS `useIsMobile()` breakpoint at 768px.** No Tailwind `md:` variants.
- **Anti-monolith (CLAUDE.md rule 2).** Non-test `.ts`/`.tsx` files stay under 1000 lines; every new surface ships as a multi-file family.
- **localStorage stability.** Keep the `gobby-activity-panel-tab-v2` key and all existing tab ids; only add ids. The layout key `gobby-activity-panel-layout` is never written by the graph override (see 10.1).
- **No deep-link bridge.** Legacy hashes dead-end to default chat after hamburger removal.
- **Non-goal: Wiki/CodeWiki graph surfaces.** The interactive Wiki/CodeWiki interfaces and graphs are blocked on the gwiki repo's agents finishing their current issue burn-down; when built, they reuse the 10.1 full-width override pattern. They are out of scope here and intentionally carry no deferral task.
- **Verification surface:** `http://localhost:60889/#chat` via the chrome-devtools MCP server. Frontend gates per deliverable: `cd web && npm run type-check`; scoped `npx vitest run <family>`; `npm run lint:js && npm run lint:css && npm run lint:tokens`; dark + light screenshots; grayscale (emulated achromatopsia) legibility; AA contrast spot-checks; 44px targets at 390×844 emulation. Any pytest run is prefixed `GOBBY_TEST_PROTECT=1` and never the full suite.

## Architecture Decisions
`kind: framing`

- **A1 — New draft-based field family; `TaskFieldEditors.tsx` stays put.** `web/src/components/activity/fields/` ships controlled (draft-model) primitives plus a `useDetailDraft` hook and the `DetailPaneHeader` status strip. Tasks' commit-on-blur editors are not refactored. Save orchestration stays per-surface in `XTabActions.ts` files because wire patterns differ (rules: merge draft over freshly fetched detail → full-definition PUT; MCP: PUT config + PATCH enabled; channels: PUT).
- **A2 — `ProjectSelectField`** is a thin `SelectField` + `useProjects()` composition labeled by project name with UUID values — not a reuse of the 353-line header `ProjectSelector`.
- **A3 — `DateTimeField`** wraps native `<input type="datetime-local">` (a11y, mobile pickers, deutan-safe for free) with unit-tested local↔UTC ISO conversion; no custom popover picker.
- **A4 — Alternate detail views** (Transcript/YAML/Messages/Graph) are a convention, not an abstraction: a local `viewMode` enum per detail pane with conditional render, shared visuals from `DetailPaneHeader`/`DetailActionButton`. No generic view-router.
- **A5 — `QuickMenu`** is the shared kebab primitive with viewport-aware flip/clamp; `TaskQuickMenu` and `McpQuickMenu` (both currently `position: fixed` with raw, unclamped coordinates) are retrofitted onto it.
- **A6 — Graph full-width via transient override**: `useActivityPanel` gains `requestPanelOverride()` / `releasePanelOverride()` / `effectiveMode`; the override is never persisted and auto-clears on tab change, explicit layout toggles, selection change, and view unmount.
- **A7 — Hoist workflows helpers before sub-tab migrations**: `executionFormatters.ts`, `execution-utils.tsx`, `isolationColors.ts` move to `web/src/components/shared/executions/` so the P7 subtree deletion is pure.
- **A8 — Configuration audit precedes overlay design**: a committed audit matrix is the input to the settings-overlay IA; the current ConfigurationPage is explicitly not the model.
- **Rules rename ships as a backend fix (2.1).** Today `RuleUpdateRequest` has no `name` field and the YAML path strips `name` before serializing (`src/gobby/servers/routes/rules.py:303`). 2.1 adds rename for user/project rules with a collision check scoped to `workflow_type="rule"` (`LocalWorkflowDefinitionManager.get_by_name()` is generic — never check unscoped). Bundled template-owned rules stay non-renameable: bundled sync re-seeds bundled names from disk on startup, so a renamed bundled row would resurrect the original beside it; bundled rules keep read-only name with copy + delete as their rename path.

## P1: Shared foundations
`kind: framing`

**Goal**: Land the primitives every surface consumes — the recipe guide, the draft-field family, the viewport-safe QuickMenu, and panel-registration hygiene — so P2–P11 build on one consistent base.

### 1.1 Write the activity-tab recipe guide [category: docs]
`kind: deliverable`

Targets:
- `docs/guides/one-surface-tab-recipe.md`

Write the guide that every subsequent surface-migration agent reads before working (alongside loading the `impeccable` skill from `gobby-skills` and reading `.impeccable.md` — state both in the guide's opening). Content, in prose:

1. **Exemplars.** `web/src/components/activity/SessionsTab.tsx` is the in-toolbar `SegmentedControl` exemplar (Live | Expired) and the alternate-view exemplar (the Transcript/Summary toggle that swaps the bottom-pane content inline). `web/src/components/activity/TasksTab.tsx` plus its `TasksTab*`-prefixed siblings (Data, Model, Toolbar, Filters, Actions, DetailPanel, List) is the multi-file-family exemplar. Cross-link `docs/guides/frontend-style-guide.md` for tokens/Tailwind/CVA conventions.
2. **File-family checklist.** Per surface, under `web/src/components/activity/<surface>/`: a Tab shell in the activity root (toolbar with `ActivityPanelSearch`, optional `SegmentedControl` with `controlHeight="sm"`, filter trigger), top list, vertical `ResizeHandle`, bottom detail with the `h-10` status strip, plus Data / List / DetailPanel / Actions files and `__tests__/`. Empty states via `ActivityPanelEmpty`.
3. **Save/Discard convention.** Detail panes are draft-based via `useDetailDraft` from `web/src/components/activity/fields/`; Save and Discard render in the status strip via `DetailPaneHeader`; dirty state guards selection/segment/tab changes through one confirm path. No commit-on-blur in new surfaces.
4. **Kebab convention.** Row actions live in the shared `QuickMenu` (`web/src/components/activity/QuickMenu.tsx`); menus must never render off-screen.
5. **Registration — exactly three edits**: add the id to the `ActivityTab` union + `ACTIVITY_PANEL_TABS` in the activity-panel tabs module (24×24 outline icon); add the id to `VALID_TABS` in the activity-panel state hook; add the `tabContent` case in the activity-panel shell.
6. **Teardown checklist (spec-gated, not parity-gated).** Capability inventory with port / fix / drop-as-broken dispositions; deletion gate = new tab works per plan + repo-wide zero-importers grep; nav edits (`appNavigation.tsx` entry + `APP_VALID_TABS` string + orphan icon import, `AppPages.tsx` lazy import, `App.tsx` render branch); deletions in a separate commit.
7. **Per-surface verification recipe** — the Constraints recipe verbatim (type-check, scoped vitest, lint:js/css/tokens, chrome-devtools walkthrough including Save/Discard wire check and kebab-at-viewport-edge check, grayscale, AA, 44px at 390×844).
8. **Pitfalls.** Copying legacy modals (modals are discouraged — detail-pane create mode instead); leaving >1px side-stripe borders; Tailwind `md:` variants; unclamped `position: fixed` menus; single-file tabs crossing the 1000-line ceiling; forgetting `controlHeight="sm"` on toolbar segments; serializing YAML from component state instead of fetched detail.

**Acceptance:**

- 1.1.1 - Guide exists with the eight numbered topics (exemplars, file family, Save/Discard, kebab, registration, teardown, verification, pitfalls). file: `docs/guides/one-surface-tab-recipe.md`.
- 1.1.2 - Guide opens by mandating the `impeccable` skill load from `gobby-skills` and `.impeccable.md` read before UI work. behavior: "impeccable mandate stated in opening section" in `docs/guides/one-surface-tab-recipe.md`.
- 1.1.3 - Guide names SessionsTab as the SegmentedControl/alternate-view exemplar and the TasksTab family as the multi-file exemplar. behavior: "exemplars cited" in `docs/guides/one-surface-tab-recipe.md`.

### 1.2 Build the draft-based detail-field family [category: code] (depends: 1.1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/fields/FieldPrimitives.tsx`
- `web/src/components/activity/fields/SwitchField.tsx`
- `web/src/components/activity/fields/KeyValueField.tsx`
- `web/src/components/activity/fields/useDetailDraft.ts`
- `web/src/components/activity/fields/DetailPaneHeader.tsx`
- `web/src/components/activity/fields/types.ts`
- `web/src/components/activity/fields/index.ts`
- `web/src/components/activity/fields/__tests__/FieldPrimitives.test.tsx`
- `web/src/components/activity/fields/__tests__/useDetailDraft.test.tsx`

Controlled draft-model primitives (plain `value`/`onChange`, no commit-on-blur — contrast with `TaskFieldEditors.tsx`, which is intentionally left untouched):

- `FieldPrimitives.tsx` — `TextField`, `TextAreaField`, `SelectField` (options: `{value, label}[]`), `TagsField` (string-array chips with add/remove). Each takes `label`, `value`, `onChange`, `disabled?`, `placeholder?`, `ariaLabel`. Styling: Tailwind utilities on the design tokens; 44px interactive targets; brand-accent focus rings.
- `SwitchField.tsx` — boolean field wrapping `web/src/components/ui/Switch.tsx`.
- `KeyValueField.tsx` — `Record<string, string>` editor (row per pair, add/remove row), for MCP `env`/`headers`.
- `useDetailDraft.ts` — the draft orchestrator:

```ts
function useDetailDraft<T>(opts: {
  source: T | null;            // last fetched canonical detail
  onSave: (draft: T) => Promise<boolean>;
}): {
  draft: T | null;
  setField: <K extends keyof T>(key: K, value: T[K]) => void;
  dirty: boolean;
  saving: boolean;
  serverChanged: boolean;      // source changed under a dirty draft
  save: () => Promise<void>;   // merges draft over latest source via onSave
  discard: () => void;
  confirmIfDirty: (next: () => void) => void;  // shared guard for selection/segment/tab changes
}
```

  When `source` updates while `dirty`, keep the draft and set `serverChanged` (rendered as a "changed on server" notice in `DetailPaneHeader`); a clean draft re-syncs from `source`. `save()` resolves conflicts by merging the draft's edited keys over the freshest `source`, never by serializing stale component state.
- `DetailPaneHeader.tsx` — the `h-10` status strip: title node, optional `serverChanged` notice, actions slot, `DetailActionButton` (icon + label, matching the existing SessionsTab action-strip buttons), and the standard Save (accent) / Discard (ghost) pair shown when `dirty`.
- `types.ts` — shared field/draft types; `index.ts` barrel.

**Acceptance:**

- 1.2.1 - Field primitives, SwitchField, and KeyValueField exist as controlled draft-model components. file: `web/src/components/activity/fields/FieldPrimitives.tsx`.
- 1.2.2 - `useDetailDraft` implements dirty tracking, serverChanged detection, merge-over-latest-source save, and `confirmIfDirty`. symbol: `useDetailDraft` in `web/src/components/activity/fields/useDetailDraft.ts`.
- 1.2.3 - Draft behavior is unit-tested: edit→dirty, discard→revert, source-update-while-dirty→serverChanged, save-merges-over-latest. test: `web/src/components/activity/fields/__tests__/useDetailDraft.test.tsx`.
- 1.2.4 - DetailPaneHeader renders title, actions slot, and the Save/Discard pair only when dirty. symbol: `DetailPaneHeader` in `web/src/components/activity/fields/DetailPaneHeader.tsx`.

### 1.3 Add ProjectSelectField and DateTimeField [category: code] (depends: 1.2)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/fields/ProjectSelectField.tsx`
- `web/src/components/activity/fields/DateTimeField.tsx`
- `web/src/components/activity/fields/__tests__/DateTimeField.test.tsx`

- `ProjectSelectField.tsx` (~60 lines) — composes `SelectField` with `useProjects()` (`web/src/hooks/useProjects.ts`): options labeled by project `display_name`/`name`, value = project UUID. When the current value is a UUID not present in the registry, render an "Unknown project (<uuid-prefix>)" option so the stored value is never silently dropped on save. Loading and error states degrade to a disabled select with hint text.
- `DateTimeField.tsx` — native `<input type="datetime-local">` styled to the design tokens, `color-scheme` driven by the resolved theme so the browser picker matches dark/light. Exposes `value` as a UTC ISO-8601 string; converts to/from the timezone-naive `datetime-local` format with explicit helpers in the same file. 44px target via padding.

**Acceptance:**

- 1.3.1 - ProjectSelectField lists registered projects by name with UUID values and an unknown-project fallback option. symbol: `ProjectSelectField` in `web/src/components/activity/fields/ProjectSelectField.tsx`.
- 1.3.2 - DateTimeField round-trips UTC ISO ↔ local input values without drift across DST boundaries. test: `web/src/components/activity/fields/__tests__/DateTimeField.test.tsx`.
- 1.3.3 - DateTimeField follows the resolved theme via `color-scheme`. behavior: "picker chrome matches active theme in dark and light screenshots" in chrome-devtools verification.

### 1.4 Build the shared QuickMenu primitive and retrofit existing menus [category: code] (depends: 1.1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/QuickMenu.tsx`
- `web/src/components/activity/TaskQuickMenu.tsx`
- `web/src/components/activity/mcp/McpQuickMenu.tsx`
- `web/src/components/activity/__tests__/QuickMenu.test.tsx`

`QuickMenu.tsx` — the canonical kebab: a three-vertical-dot trigger button (44px target) plus a `position: fixed` menu that, on open, measures itself and the trigger via `getBoundingClientRect` and (a) flips above the trigger when it would cross the bottom viewport edge, (b) clamps horizontally to the viewport with an 8px gutter. Esc and outside-click close; ArrowUp/ArrowDown/Home/End/Enter keyboard navigation; items accept `{label, icon?, destructive?, disabled?, onSelect}` — destructive items use the deutan-safe destructive tokens (hue 350) with an icon, never hue alone.

Retrofit `TaskQuickMenu.tsx` and `mcp/McpQuickMenu.tsx` onto the primitive, preserving their current items and tests. Both currently hard-code `position: "fixed"` coordinates with no clamping (`TaskQuickMenu.tsx:68`, `McpQuickMenu.tsx:33`) — the retrofit deletes that ad-hoc positioning.

**Acceptance:**

- 1.4.1 - QuickMenu flips/clamps so the menu never renders outside the viewport. test: `web/src/components/activity/__tests__/QuickMenu.test.tsx`.
- 1.4.2 - TaskQuickMenu and McpQuickMenu render through QuickMenu and contain no ad-hoc fixed-coordinate styles. symbol: `QuickMenu` in `web/src/components/activity/QuickMenu.tsx`. file: `web/src/components/activity/TaskQuickMenu.tsx`.
- 1.4.3 - Kebab opened on the bottom-most visible row and at the right viewport edge stays fully on-screen at 390×844 and desktop sizes. behavior: "menu fully visible at viewport edges" in chrome-devtools verification.

### 1.5 Fix VALID_TABS coverage and tab-selector scaling [category: code]
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/ActivityPanel.tsx`
- `web/src/components/activity/__tests__/useActivityPanel.test.tsx`

Two registration-hygiene fixes before the panel grows from 10 to 16 tabs:

1. `VALID_TABS` in `useActivityPanel.ts` is missing `'wiki'` (latent persistence bug — the wiki tab cannot be restored from localStorage). Fix it and add a unit test asserting `VALID_TABS` contains every member of the `ActivityTab` union so the omission class cannot recur.
2. Verify the tab selector UI in `ActivityPanelTabs.tsx`/`ActivityPanel.tsx` handles 16 entries without clipping at panel-min-width and at 390×844; add an overflow affordance (scroll or condensed layout) if it clips. This lands once, here — not per-phase.

**Acceptance:**

- 1.5.1 - `VALID_TABS` covers the full `ActivityTab` union, enforced by a test. test: `web/src/components/activity/__tests__/useActivityPanel.test.tsx`.
- 1.5.2 - Tab selector renders 16 simulated entries without clipping or dead entries at panel-min-width and 390×844. behavior: "16-tab selector fully operable" in chrome-devtools verification.

## P2: Rules tab
`kind: framing`

**Goal**: The highest-priority surface — Rules leaves the Workflows page and becomes a top-level activity tab with list/kebab management, draft-based field editing (including rename), and a YAML alternate view.

### 2.1 Add rule rename support to the backend [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/rules.py`
- `tests/servers/routes/test_rules_routes.py`

Add rename to the rules update route so the UI can edit the name like any other field:

- `RuleUpdateRequest` (`src/gobby/servers/routes/rules.py:47`) gains `name: str | None = Field(default=None, description="New rule name (rename)")`.
- In `update_rule_endpoint` (line ~278), when `name` is present and differs from the current row name: return 409 if another `workflow_type="rule"` row already carries that name — the collision check MUST be scoped by workflow type and follow the route's existing manager-fetch pattern, because `LocalWorkflowDefinitionManager.get_by_name()` is a generic registry lookup; return 400 if the target row is owned by a bundled template (use the same provenance marker the loader's drift-refresh uses — see the bundled sync in `src/gobby/workflows/loader.py`), since startup sync re-seeds bundled names from `src/gobby/install/shared/rules/` and a renamed bundled row would resurrect the original beside it. Allowed renames pass through `manager.update(row.id, name=..., **other_fields)`.
- The YAML definition path keeps stripping `name` from the body (line ~303): rename is row-level metadata; the definition body stays name-free. Rename combined with a definition update in one request must work.
- The response returns the rule under its new name; subsequent by-name routes use the new name.
- Test coverage in the existing route suite: rename happy path, 409 collision, bundled-rule rejection, rename+definition combined. Run with `GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_rules_routes.py -v`.

**Acceptance:**

- 2.1.1 - RuleUpdateRequest accepts a new name and the update route renames user/project rules. symbol: `RuleUpdateRequest` in `src/gobby/servers/routes/rules.py`.
- 2.1.2 - Name collisions return 409 (type-scoped check) and bundled-template rules are rejected with 400. test: `tests/servers/routes/test_rules_routes.py`.
- 2.1.3 - Rename combined with a definition update succeeds in one request; the YAML body remains name-free. test: `tests/servers/routes/test_rules_routes.py`.

### 2.2 Build the Rules activity tab [category: code] (depends: 2.1, P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/RulesTab.tsx`
- `web/src/hooks/useRules.ts`
- `web/src/components/activity/rules/RulesTabData.ts`
- `web/src/components/activity/rules/RulesTabList.tsx`
- `web/src/components/activity/rules/RulesDetailPanel.tsx`
- `web/src/components/activity/rules/RulesTabActions.ts`
- `web/src/components/activity/rules/__tests__/RulesTab.test.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Source (read-only input; deletion happens in 2.3): `web/src/components/workflows/RulesTab.tsx` (572L) + `RulesTab.css`. Data layer: `useRules()` (`web/src/hooks/useRules.ts`) — `fetchRules({event, group, enabled})` → `GET /api/rules`, `fetchRuleDetail(name)` → `GET /api/rules/{name}`, `toggleRule(name, enabled)` → `PUT /api/rules/{name}/toggle`, `bulkToggleRules`, `createRule`, `updateRule(name, definition)` (full-definition PUT), `deleteRule(name, force?)`, `setEnforcement`. WS: `useWebSocketEvent('workflow_event', …)` with the existing 500ms debounce.

- `RulesTab.tsx` — shell: toolbar (`ActivityPanelSearch` over name/description, `SegmentedControl<"enabled" | "disabled">` with `controlHeight="sm"` mirroring Sessions' Live | Expired, filter trigger for event/group/source/tags), top list, `ResizeHandle`, bottom detail.
- `RulesTabData.ts` — useRules wiring, segment + search + filter selection, `nextCopyName(name, existing)` → `name-copy`, `name-copy-2`, ….
- `RulesTabList.tsx` — rows: enabled state via `ActivityRowStatusDot` (lightness + icon, deutan-safe), name, event badge, group; row kebab via the shared `QuickMenu` with **Activate/Deactivate** (calls `toggleRule`), **Copy** (`fetchRuleDetail` + `createRule(nextCopyName(...))`; on 409/400 regenerate the suffix once, then surface the error), **Delete** (confirm dialog, then `deleteRule`).
- `RulesDetailPanel.tsx` — draft-based editor via `useDetailDraft`: `description` (TextAreaField), `event` (SelectField over the trigger-event enum), `group` (TextField), `priority` (numeric TextField), `tags` (TagsField), `audience` (SelectField: all/interactive/autonomous/custom), `agent_scope` (TagsField), `enabled` (SwitchField). **`name` is editable for user/project rules** via the 2.1 rename support — extend `useRules.updateRule` to carry the new name, and on save reselect the rule under its new name; bundled template-owned rules render name read-only (copy + delete is their rename path). Nested `when`/`effects`/`match` render as read-only summaries here; the YAML view (2.3) is their editor. Save/Discard in the `DetailPaneHeader` status strip.
- `RulesTabActions.ts` — save orchestration: re-fetch detail, merge draft fields over it, `updateRule(name, fullDefinition)`; serialize saves per rule to avoid read-merge-write races; refetch after mutation regardless of WS.
- Registration: the three edits (tab id `rules`, 24×24 outline icon, `VALID_TABS`, `tabContent` case).
- Global enforcement toggle (`setEnforcement`) surfaces in the filter dropdown footer, not the toolbar.

**Acceptance:**

- 2.2.1 - Rules tab is registered in the union, `ACTIVITY_PANEL_TABS`, `VALID_TABS`, and `tabContent`. file: `web/src/components/activity/ActivityPanelTabs.tsx`. file: `web/src/components/activity/ActivityPanel.tsx`.
- 2.2.2 - Enabled | Disabled segment filters the list; search and event/group/source filters compose. symbol: `RulesTab` in `web/src/components/activity/RulesTab.tsx`.
- 2.2.3 - Row kebab exposes Activate/Deactivate, Copy, Delete; copy collisions retry once then surface. symbol: `nextCopyName` in `web/src/components/activity/rules/RulesTabData.ts`. test: `web/src/components/activity/rules/__tests__/RulesTab.test.tsx`.
- 2.2.4 - Detail pane edits all scalar fields through a draft with Save/Discard on the status strip; renaming a user/project rule reselects it under the new name; bundled names are read-only; Save issues the full-definition PUT (verified on the wire). symbol: `RulesDetailPanel` in `web/src/components/activity/rules/RulesDetailPanel.tsx`.
- 2.2.5 - Tab passes the recipe verification (grayscale, AA dark+light, 44px, kebab-at-edge). behavior: "recipe gates pass" in chrome-devtools verification.

### 2.3 Add the Rules YAML editor view [category: code] (depends: 2.2)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/rules/RulesYamlView.tsx`
- `web/src/components/activity/rules/rulesYaml.ts`
- `web/src/components/activity/rules/__tests__/rulesYaml.test.ts`

A **YAML** `DetailActionButton` on the status strip (exactly the Sessions Transcript-button pattern) swaps the detail pane's `viewMode` from `fields` to `yaml`:

- `RulesYamlView.tsx` — `CodeMirrorEditor` (`web/src/components/shared/CodeMirrorEditor.tsx`, language `yaml` via the existing `@codemirror/lang-yaml` dep) filling the detail pane, with **Save** and **Close** in the `DetailPaneHeader`. Close with unsaved edits routes through the shared dirty guard. Esc inside CodeMirror does not close the view (check `defaultPrevented`).
- `rulesYaml.ts` — `definitionToYaml(detail)` serializes from the **fetched canonical detail** (never component state) via `js-yaml`; `yamlToDefinition(text)` parses and shape-validates (event present, effects list non-empty) and returns typed errors for inline display. On save: parse → validate → merge over freshly fetched detail so unknown keys survive → `updateRule`. Note the backend strips `name` from the definition body (`rules.py:303`) and the response exposes `effects` (list) with a legacy `effect` fallback — normalize to `effects` on load.

**Acceptance:**

- 2.3.1 - YAML button swaps the detail pane to a CodeMirror YAML editor with Save/Close on the status strip. symbol: `RulesYamlView` in `web/src/components/activity/rules/RulesYamlView.tsx`.
- 2.3.2 - Round-trip is lossless: load → edit one field → open YAML → save → reload equals expectation; unknown keys survive. test: `web/src/components/activity/rules/__tests__/rulesYaml.test.ts`.
- 2.3.3 - Invalid YAML or schema violations render inline errors and block save. behavior: "invalid YAML blocked with visible error" in chrome-devtools verification.

### 2.4 Remove the Workflows Rules sub-tab and legacy rule editor [category: refactor] (depends: 2.3)
`kind: deliverable`

Targets:
- `web/src/components/workflows/WorkflowsPage.tsx`
- `web/src/components/workflows/RulesTab.tsx`
- `web/src/components/workflows/RulesTab.css`
- `web/src/components/rules/RuleEditForm.tsx`
- `web/src/components/rules/ExpressionBuilder.tsx`
- `web/src/components/rules/ruleFormData.ts`

Spec-gated teardown (2.2 + 2.3 verified working first). Remove the `rules` entry from WorkflowsPage's `ActiveTab` union, `TABS`, the rules-specific lifted filter state, and the `activeTab === "rules"` render branches. Delete the workflows-folder RulesTab module and its stylesheet. Delete the `components/rules/` legacy editor family (RuleEditForm, ExpressionBuilder, ruleFormData, and siblings) if a repo-wide grep shows no remaining importers; hoist any still-imported helper before deletion. Capability inventory dispositions recorded in the closing commit (e.g., the form-builder for nested effects is **dropped** in favor of the YAML view — intentional).

**Acceptance:**

- 2.4.1 - WorkflowsPage no longer renders a Rules sub-tab. symbol: `WorkflowsPage` in `web/src/components/workflows/WorkflowsPage.tsx`.
- 2.4.2 - The workflows-folder RulesTab module, its stylesheet, and the unreferenced `components/rules/` editor family are deleted; repo-wide grep for their imports returns no hits. file: `web/src/components/workflows/RulesTab.tsx`.
- 2.4.3 - Scoped type-check, vitest, and lint pass after deletion. behavior: "scoped CI green after teardown" in `web/` test scope.

## P3: MCP polish
`kind: framing`

**Goal**: Finish the half-done MCP migration — kill the legacy modal, make server config a first-class draft-edited detail pane, and delete the orphaned page.

### 3.1 Replace the MCP add-server modal with detail-pane editing [category: code] (depends: P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/ActivityMcpTab.tsx`
- `web/src/components/activity/mcp/McpServerFields.tsx`
- `web/src/components/activity/mcp/McpTabActions.ts`
- `web/src/components/activity/mcp/__tests__/McpServerFields.test.tsx`

`ActivityMcpTab.tsx` (519L) currently opens `McpAddServerModal` from the legacy `../mcp/McpServerForm` (import at line 17, render at ~line 512). Replace it: the "+ Server" action selects a **new-server draft** that renders the detail pane in create mode — no modal (modals are an impeccable anti-pattern).

- `McpServerFields.tsx` — draft-based editor via `useDetailDraft` for `MCPServerConfig`: `name` (TextField; read-only once created — it is the registry key), `description` (TextAreaField), `transport` (SelectField: http/stdio/websocket/sse), `url` / `command` / `args` (TextFields shown conditionally per transport), `env` and `headers` (KeyValueField), `project_id` (**ProjectSelectField** — never a raw UUID text input), `enabled` and `requires_oauth` (SwitchField), `oauth_provider` (SelectField), `connect_timeout` (numeric TextField). Any datetime-valued config or status surface (e.g., OAuth token expiry, if exposed) uses `DateTimeField` — never a plain text input.
- `McpTabActions.ts` — save orchestration: create → `POST /api/mcp/servers`; update → `PUT /api/mcp/servers/{name}` plus `PATCH` for the enabled toggle; delete and refresh-tools stay on the existing paths. Extracting actions keeps `ActivityMcpTab.tsx` under the 1000-line ceiling.
- Existing tool-tree/detail behavior (`McpDetailPanel.tsx`) is unchanged; the server-fields view and tool view share the detail pane via the `viewMode` convention.

**Acceptance:**

- 3.1.1 - `ActivityMcpTab` no longer imports from `components/mcp/McpServerForm`; "+ Server" renders the detail-pane create mode. file: `web/src/components/activity/ActivityMcpTab.tsx`.
- 3.1.2 - Server fields edit through a draft with Save/Discard; `project_id` is a name-labeled dropdown backed by `useProjects`. symbol: `McpServerFields` in `web/src/components/activity/mcp/McpServerFields.tsx`.
- 3.1.3 - Create and update issue the correct POST/PUT/PATCH calls (verified on the wire); env/headers round-trip through KeyValueField. test: `web/src/components/activity/mcp/__tests__/McpServerFields.test.tsx`.
- 3.1.4 - Tab passes the recipe verification gates. behavior: "recipe gates pass" in chrome-devtools verification.

### 3.2 Delete McpPage and satellites [category: refactor] (depends: 3.1)
`kind: deliverable`

Targets:
- `web/src/components/mcp/McpPage.tsx`
- `web/src/components/mcp/McpServerForm.tsx`
- `web/src/components/mcp/McpToolDetail.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

McpPage is already unreachable from the nav (`mcp` is not in `APP_VALID_TABS`); it and its satellites are dead weight once 3.1 removes the last live import. Delete `McpPage.tsx`, `McpServerForm.tsx`, and `McpToolDetail.tsx` after a repo-wide grep confirms zero importers outside the deleted set; remove the `McpPage` lazy import/re-export from `AppPages.tsx` and any residual `mcp` branch in `App.tsx`.

**Acceptance:**

- 3.2.1 - The `components/mcp/` page family is deleted; repo-wide grep for `from.*components/mcp/` returns no hits. file: `web/src/components/mcp/McpPage.tsx`.
- 3.2.2 - AppPages no longer lazy-imports McpPage. file: `web/src/components/app/AppPages.tsx`.

## P4: Workflows helper hoist and Agents tab
`kind: framing`

**Goal**: Unblock the workflows-subtree deletion by hoisting shared helpers, then move Agents into the panel.

### 4.1 Hoist execution helpers to shared/executions [category: refactor]
`kind: deliverable`

Targets:
- `web/src/components/shared/executions/executionFormatters.ts`
- `web/src/components/shared/executions/executionUtils.tsx`
- `web/src/components/shared/executions/isolationColors.ts`
- `web/src/components/workflows/executionFormatters.ts`
- `web/src/components/workflows/execution-utils.tsx`
- `web/src/components/workflows/isolationColors.ts`
- `web/src/components/activity/PipelinesTab.tsx`
- `web/src/components/activity/TracesTab.tsx`
- `web/src/components/activity/CronTab.tsx`

Move `web/src/components/workflows/executionFormatters.ts` (37L, pure formatters), `web/src/components/workflows/execution-utils.tsx` (419L, React execution helpers), and `web/src/components/workflows/isolationColors.ts` (51L) to `web/src/components/shared/executions/`. Update every importer outside the workflows subtree — the activity `PipelinesTab`, `TracesTab`, `CronTab`, plus any others surfaced by `gcode usages`/grep. Workflows-internal self-imports may remain (they die with the P7 subtree deletion). Gate: `grep -rn "workflows/executionFormatters\|workflows/execution-utils\|workflows/isolationColors" web/src | grep -v "^web/src/components/workflows/"` returns no hits.

**Acceptance:**

- 4.1.1 - The three helper modules exist under `shared/executions/` and all non-workflows importers point at the new paths. file: `web/src/components/shared/executions/executionFormatters.ts`.
- 4.1.2 - The outside-workflows grep returns no hits; scoped type-check and vitest pass. behavior: "no workflows-helper imports outside the subtree" verified by the scoped grep.

### 4.2 Build the Agents activity tab [category: code] (depends: 4.1, P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/AgentsTab.tsx`
- `web/src/components/activity/agents/AgentsTabData.ts`
- `web/src/components/activity/agents/AgentsTabList.tsx`
- `web/src/components/activity/agents/AgentsDetailPanel.tsx`
- `web/src/components/activity/agents/AgentsTabActions.ts`
- `web/src/components/activity/agents/__tests__/AgentsTab.test.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Source: the `workflows/AgentsTab.*` family — `AgentsTab.tsx` (585L), `.actions.ts` (427L), `.cards.tsx` (387L), `.data.ts` (149L), `.payloads.ts` (276L), `.types.ts` (103L). The data/actions/payloads/types modules port nearly 1:1 into the activity family; the card grid collapses into the canonical top-list + bottom-detail two-pane.

- List rows: agent name, provider badge, enabled state via `ActivityRowStatusDot`; row kebab (QuickMenu) for enable/disable, duplicate, delete per the existing actions module.
- `AgentsDetailPanel.tsx` — draft-based scalar fields (name, description, provider, model, enabled switch, tags) with Save/Discard on the status strip; the existing `components/agents/AgentSkillsEditor`, `AgentVariablesEditor`, `AgentRulesEditor`, `AgentStepsEditor`, `AgentToolBlocksEditor` embed below the scalars (kept functional, restyled minimally to tokens), their edits feeding the same draft.
- Registration: three edits, tab id `agents`.

**Acceptance:**

- 4.2.1 - Agents tab registered (union, `ACTIVITY_PANEL_TABS`, `VALID_TABS`, `tabContent`). file: `web/src/components/activity/ActivityPanel.tsx`.
- 4.2.2 - List + kebab actions work against live data; detail pane edits through a draft with Save/Discard; embedded editors feed the draft. symbol: `AgentsDetailPanel` in `web/src/components/activity/agents/AgentsDetailPanel.tsx`.
- 4.2.3 - Tab passes the recipe verification gates. behavior: "recipe gates pass" in chrome-devtools verification.

### 4.3 Remove the Workflows Agents sub-tab and family [category: refactor] (depends: 4.2)
`kind: deliverable`

Targets:
- `web/src/components/workflows/WorkflowsPage.tsx`
- `web/src/components/workflows/AgentsTab.tsx`

Remove the `agents` entry from WorkflowsPage (union, TABS, filter state, render branch). Delete the workflows-folder AgentsTab module and its `.actions/.cards/.data/.payloads/.types` siblings after the zero-importers grep; port or migrate their `__tests__` to the activity family. Capability dispositions in the closing commit.

**Acceptance:**

- 4.3.1 - WorkflowsPage no longer renders an Agents sub-tab; the `workflows/AgentsTab.*` family is deleted with zero remaining importers. file: `web/src/components/workflows/AgentsTab.tsx`.
- 4.3.2 - Scoped CI green after deletion. behavior: "scoped CI green after teardown" in `web/` test scope.

## P5: Stages and Profiles tab
`kind: framing`

**Goal**: One panel tab for lifecycle-manifest shaping — stage registry and build profiles as segments.

### 5.1 Build the Stages tab with Stages | Profiles segments [category: code] (depends: 4.1, P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/StagesTab.tsx`
- `web/src/components/activity/stages/StagesTabData.ts`
- `web/src/components/activity/stages/StagesList.tsx`
- `web/src/components/activity/stages/StageDetailPanel.tsx`
- `web/src/components/activity/stages/ProfilesList.tsx`
- `web/src/components/activity/stages/ProfileDetailPanel.tsx`
- `web/src/components/activity/stages/StagesTabActions.ts`
- `web/src/components/activity/stages/__tests__/StagesTab.test.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Sources: `workflows/StagesTab.tsx` (416L) and `workflows/ProfilesTab.tsx` (511L). One tab (id `stages`), `SegmentedControl<"stages" | "profiles">` with `controlHeight="sm"` in the toolbar; each segment has its own list + detail pair (the layouts differ — registry rows vs. profile overlays). Detail panes are draft-based with Save/Discard; row kebabs for the existing row-level operations (duplicate/delete where the source supports them); default-profile selection surfaces as a kebab action on profile rows. Registration: three edits.

**Acceptance:**

- 5.1.1 - Stages tab registered with id `stages`; segment swap renders both views against live data. symbol: `StagesTab` in `web/src/components/activity/StagesTab.tsx`.
- 5.1.2 - Stage and profile detail panes edit through drafts with Save/Discard on the status strip. symbol: `ProfileDetailPanel` in `web/src/components/activity/stages/ProfileDetailPanel.tsx`.
- 5.1.3 - Tab passes the recipe verification gates. behavior: "recipe gates pass" in chrome-devtools verification.

### 5.2 Remove the Workflows Stages and Profiles sub-tabs [category: refactor] (depends: 5.1)
`kind: deliverable`

Targets:
- `web/src/components/workflows/WorkflowsPage.tsx`
- `web/src/components/workflows/StagesTab.tsx`
- `web/src/components/workflows/ProfilesTab.tsx`

Remove the `stages` and `profiles` entries from WorkflowsPage (union, TABS, `sourceOptionsForTab` special-casing, render branches). Delete the workflows-folder StagesTab and ProfilesTab modules after the zero-importers grep.

**Acceptance:**

- 5.2.1 - WorkflowsPage no longer renders Stages or Profiles sub-tabs; both source files deleted with zero importers. file: `web/src/components/workflows/StagesTab.tsx`.
- 5.2.2 - Scoped CI green after deletion. behavior: "scoped CI green after teardown" in `web/` test scope.

## P6: Pipelines Live | Defs
`kind: framing`

**Goal**: Fold the Workflows page's pipeline-definitions view into the existing live-executions panel tab as a second segment.

### 6.1 Add the Defs segment to the pipelines activity tab [category: code] (depends: 4.1, P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/PipelinesTab.tsx`
- `web/src/components/activity/pipelines/PipelinesDefsList.tsx`
- `web/src/components/activity/pipelines/PipelinesDefsDetail.tsx`
- `web/src/components/activity/pipelines/PipelinesDefsActions.ts`
- `web/src/components/activity/pipelines/PipelineEditor.tsx`
- `web/src/components/workflows/PipelineEditor.tsx`
- `web/src/components/activity/pipelines/__tests__/PipelinesDefs.test.tsx`

`activity/PipelinesTab.tsx` (322L) shows live executions only. Add `SegmentedControl<"live" | "defs">` (`controlHeight="sm"`) to its toolbar; **live stays the default and its rendering is unchanged** (extract to a pane component only if the file approaches the line ceiling). Persist the segment under `gobby-pipelines-segment-v1`.

Defs segment: list of pipeline definitions (name, PIPELINE badge, enabled state, one-line description) from the data source used by the workflows-folder PipelinesTab (508L, read-only input here; its deletion is 6.2); detail pane shows tags, enabled switch, description (draft-based Save/Discard), with an **Edit** `DetailActionButton` that swaps `viewMode` to the YAML editor — `web/src/components/workflows/PipelineEditor.tsx` (881L) **moves** to `web/src/components/activity/pipelines/PipelineEditor.tsx` largely as-is (a flat-field re-port is explicitly out of scope). Row kebab: enable/disable, run, delete per existing mutations.

**Acceptance:**

- 6.1.1 - Live | Defs segment renders in the toolbar with live as default; segment choice survives reload via `gobby-pipelines-segment-v1`. symbol: `PipelinesTab` in `web/src/components/activity/PipelinesTab.tsx`.
- 6.1.2 - Defs segment lists definitions and edits them via the relocated PipelineEditor behind an Edit action. file: `web/src/components/activity/pipelines/PipelineEditor.tsx`.
- 6.1.3 - Tab passes the recipe verification gates in both segments. behavior: "recipe gates pass" in chrome-devtools verification.

### 6.2 Remove the Workflows Pipelines sub-tab [category: refactor] (depends: 6.1)
`kind: deliverable`

Targets:
- `web/src/components/workflows/WorkflowsPage.tsx`
- `web/src/components/workflows/PipelinesTab.tsx`
- `web/src/components/workflows/PipelineExecutionsView.tsx`

Remove the `pipelines` entry from WorkflowsPage (it is the current default sub-tab — repoint the default to whatever sub-tab remains, or to the page's empty state if none). Delete the workflows-folder PipelinesTab module. Resolve `PipelineExecutionsView.tsx` (332L): the activity Live segment supersedes it — delete it and port any worthwhile assertions from its pagination test into the activity family's tests.

**Acceptance:**

- 6.2.1 - WorkflowsPage no longer renders a Pipelines sub-tab; `workflows/PipelinesTab.tsx` and `PipelineExecutionsView.tsx` are deleted with zero importers. file: `web/src/components/workflows/PipelinesTab.tsx`.
- 6.2.2 - Scoped CI green after deletion. behavior: "scoped CI green after teardown" in `web/` test scope.

## P7: WorkflowsPage teardown
`kind: framing`

**Goal**: With all five sub-tabs migrated, delete the Workflows page and its subtree.

### 7.1 Delete WorkflowsPage and the workflows component subtree [category: refactor] (depends: 2.4, 4.3, 5.2, 6.2)
`kind: deliverable`

Targets:
- `web/src/components/workflows/WorkflowsPage.tsx`
- `web/src/components/workflows/ReportingTab.tsx`
- `web/src/components/workflows/ReportsPage.icons.tsx`
- `web/src/components/workflows/workflows-styles.ts`
- `web/src/components/workflows/pipelines-reporting.css`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Delete `WorkflowsPage.tsx` (742L) and everything left in `components/workflows/`: the dead `ReportingTab.tsx` (713L — not imported by WorkflowsPage today), `ReportsPage.icons.tsx`, `workflows-styles.ts`, `pipelines-reporting.css`, the workflows-internal copies of the hoisted helpers (orphaned since 4.1), and the subtree's `__tests__`. Nav edits: remove the workflows entry from `createAppNavItems`, the `APP_VALID_TABS` string, the `APP_NAV_PAGES` entry, and the orphan `WorkflowsIcon` import (required by `noUnusedLocals` + `no-unused-vars`); remove the AppPages lazy import and the `activeTab === "workflows"` branch in `App.tsx`. Gate: `grep -rn "components/workflows" web/src` returns zero hits.

**Acceptance:**

- 7.1.1 - The `components/workflows/` directory is empty/removed; repo-wide grep for `components/workflows` returns no hits. file: `web/src/components/workflows/WorkflowsPage.tsx`.
- 7.1.2 - Nav, AppPages, and App.tsx no longer reference workflows; orphan icon import removed. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`.
- 7.1.3 - Full web type-check, vitest run, and lint pass. behavior: "full web CI green after subtree deletion" in `web/` test scope.

## P8: Skills tab
`kind: framing`

**Goal**: One Skills tab, two genuinely different surfaces — installed-skill management and hub search/scan/install.

### 8.1 Build the Skills tab Installed segment [category: code] (depends: P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/SkillsTab.tsx`
- `web/src/components/activity/skills/SkillsTabData.ts`
- `web/src/components/activity/skills/SkillsInstalledList.tsx`
- `web/src/components/activity/skills/SkillsInstalledDetail.tsx`
- `web/src/components/activity/skills/SkillsTabActions.ts`
- `web/src/components/activity/skills/__tests__/SkillsTab.test.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Source (read-only; deletion in 8.3): `components/skills/SkillsPage.tsx` (474L) + satellites. Data: `useSkills()` (`web/src/hooks/useSkills.ts`, 541L) — `/api/skills` CRUD, stats, scan, hubs, import, move-to-project. WS: `skill_event`.

- `SkillsTab.tsx` — shell with `SegmentedControl<"installed" | "hub">` (`controlHeight="sm"`). The orchestrator branches the **entire subtree** per segment — the two layouts are different by design; do not force a shared list component.
- Installed segment: search + category/source filters in the toolbar; list rows (name, category, INSTALLED/hub badge, enabled state dot); row kebab: enable/disable, move to project / move to installed, export, delete.
- `SkillsInstalledDetail.tsx` — draft-based fields with Save/Discard: `description` (TextAreaField), `version`/`license`/`compatibility` (TextFields), `allowed_tools` (TagsField), `enabled` + `always_apply` (SwitchField), `injection_format` (SelectField: summary/full/content), `project_id` (ProjectSelectField, shown for project-scoped skills); `content` opens in a CodeMirror alternate view behind a **Content** `DetailActionButton` (markdown language) with Save/Close — same pattern as the Rules YAML view.
- Registration: three edits, tab id `skills`.

**Acceptance:**

- 8.1.1 - Skills tab registered; Installed segment lists skills with working filters and kebab actions. symbol: `SkillsTab` in `web/src/components/activity/SkillsTab.tsx`.
- 8.1.2 - Installed detail edits through a draft with Save/Discard; Content alternate view edits skill content via CodeMirror. symbol: `SkillsInstalledDetail` in `web/src/components/activity/skills/SkillsInstalledDetail.tsx`.
- 8.1.3 - Tab passes the recipe verification gates. behavior: "recipe gates pass" in chrome-devtools verification.

### 8.2 Build the Skills Hub segment [category: code] (depends: 8.1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/skills/SkillsHubView.tsx`
- `web/src/components/activity/skills/SkillsHubDetail.tsx`
- `web/src/components/activity/skills/__tests__/SkillsHub.test.tsx`

The Hub segment is search-first, not list-first: top pane = hub picker (SelectField over `GET /api/skills/hubs` — currently 4 configured hubs — plus an "All hubs" option) + search input driving `GET /api/skills/hubs/search`, with results listed below the controls (name, hub badge, version, one-line description). Bottom pane = `SkillsHubDetail` for the selected result:

- Metadata (name, hub, version, license, description) and full content preview.
- **Safety scan**: a Scan action runs `POST /api/skills/scan` on the candidate content and renders findings deutan-safe — severity communicated by lightness step + icon (CRITICAL→INFO), never hue alone; each finding shows title, description, remediation, location.
- **Install** (`POST /api/skills/hubs/install`): disabled-with-reason until a scan has run and passed (`is_safe`), or the user explicitly confirms installing despite findings (confirm dialog naming the max severity).

**Acceptance:**

- 8.2.1 - Hub segment searches across the configured hubs with a hub picker and renders results. symbol: `SkillsHubView` in `web/src/components/activity/skills/SkillsHubView.tsx`.
- 8.2.2 - Scan renders severity-graded findings legible in grayscale; Install is gated on scan-pass or explicit confirm. symbol: `SkillsHubDetail` in `web/src/components/activity/skills/SkillsHubDetail.tsx`. test: `web/src/components/activity/skills/__tests__/SkillsHub.test.tsx`.
- 8.2.3 - Installing a hub skill lands it in the Installed segment without reload (WS or refetch). behavior: "installed skill appears in Installed segment after install" in chrome-devtools verification.

### 8.3 Delete SkillsPage and satellites [category: refactor] (depends: 8.2)
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

Delete the `components/skills/` subtree: the page, its seven satellites, and the legacy class-string `styles.ts` (all enumerated in Targets). Port surviving test logic (e.g., import-flow assertions) into the activity family first. Nav edits: skills entry, `APP_VALID_TABS` string, orphan `SkillsIcon` import, AppPages lazy import, `activeTab === "skills"` branch. Gate: repo-wide grep for `components/skills/` returns no hits.

**Acceptance:**

- 8.3.1 - The `components/skills/` subtree including `styles.ts` is deleted; repo-wide grep returns no hits. file: `web/src/components/skills/SkillsPage.tsx`.
- 8.3.2 - Nav, AppPages, and App.tsx no longer reference skills; orphan icon import removed. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`.

## P9: Integrations tab
`kind: framing`

**Goal**: Channels managed from the panel — editable config, live status, and inline message history.

### 9.1 Build the Integrations activity tab [category: code] (depends: P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/IntegrationsTab.tsx`
- `web/src/components/activity/integrations/IntegrationsTabData.ts`
- `web/src/components/activity/integrations/ChannelsList.tsx`
- `web/src/components/activity/integrations/ChannelDetailPanel.tsx`
- `web/src/components/activity/integrations/channelMetadata.ts`
- `web/src/components/activity/integrations/IntegrationsTabActions.ts`
- `web/src/components/activity/integrations/__tests__/IntegrationsTab.test.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Source (read-only; deletion in 9.3): `components/integrations/IntegrationsPage.tsx` (353L) + satellites. Data: `useIntegrations()` (`web/src/hooks/useIntegrations.ts`) — `GET/POST /api/comms/channels`, `PUT/DELETE /api/comms/channels/{id}`, `GET /api/comms/channels/{id}/status`. WS: `comms_event`.

- List rows: channel name, `channel_type` badge (slack/telegram/discord/teams/email/sms/gobby_chat), enabled/status dot; row kebab: enable/disable, delete; "+ Channel" renders the detail pane in create mode (channel-type SelectField first, which drives the per-type config fields).
- `ChannelDetailPanel.tsx` — draft-based Save/Discard: `name` (TextField), `channel_type` (SelectField, create-mode only), `enabled` (SwitchField), per-type `config_json` fields driven by the hoisted `channelMetadata.ts` (ported from the legacy integrations metadata module), `webhook_secret` as a masked TextField with reveal toggle. Channel status (from the status endpoint) renders as a read-only strip (lightness + icon).
- **503 handling**: when the communications manager is unavailable the backend returns 503 — render a designed "Communications not configured" state with a setup hint, visually and semantically distinct from the zero-channels empty state (which shows a create CTA). Neither is an error toast or a spinner.
- Registration: three edits, tab id `integrations`.

**Acceptance:**

- 9.1.1 - Integrations tab registered; channels list with type badges and kebab actions works against live data. symbol: `IntegrationsTab` in `web/src/components/activity/IntegrationsTab.tsx`.
- 9.1.2 - Channel detail edits config through a draft with Save/Discard; per-type fields derive from channelMetadata; webhook secret is masked. symbol: `ChannelDetailPanel` in `web/src/components/activity/integrations/ChannelDetailPanel.tsx`.
- 9.1.3 - 503 renders the "Communications not configured" state, distinct from the zero-channels empty state. test: `web/src/components/activity/integrations/__tests__/IntegrationsTab.test.tsx`.
- 9.1.4 - Tab passes the recipe verification gates. behavior: "recipe gates pass" in chrome-devtools verification.

### 9.2 Add the Messages alternate view [category: code] (depends: 9.1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/integrations/MessagesView.tsx`
- `web/src/components/activity/integrations/__tests__/MessagesView.test.tsx`

A **Messages** `DetailActionButton` on the status strip (Transcript-button pattern) swaps the detail pane to the selected channel's recent message history from `GET /api/comms/messages` filtered by channel: direction-distinguished rows (inbound/outbound by alignment + icon, not hue), content, status, relative timestamp, error text when present; newest at bottom; load-more pagination via the `limit` param. Close returns to the config view through the standard `viewMode` convention.

**Acceptance:**

- 9.2.1 - Messages button swaps the detail pane to the channel's message history with direction, status, and timestamps. symbol: `MessagesView` in `web/src/components/activity/integrations/MessagesView.tsx`.
- 9.2.2 - Pagination loads older messages; empty history renders a designed empty state. test: `web/src/components/activity/integrations/__tests__/MessagesView.test.tsx`.

### 9.3 Delete IntegrationsPage and satellites [category: refactor] (depends: 9.2)
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

Delete the `components/integrations/` subtree (page, channel satellites, metadata module, legacy `styles.ts` — all enumerated in Targets) — the metadata module only after 9.1's hoisted copy is live. Nav edits: integrations entry, orphan `IntegrationsIcon` import, AppPages lazy import, `activeTab === "integrations"` branch. Gate: repo-wide grep for `components/integrations/` returns no hits.

**Acceptance:**

- 9.3.1 - The `components/integrations/` subtree is deleted; repo-wide grep returns no hits. file: `web/src/components/integrations/IntegrationsPage.tsx`.
- 9.3.2 - Nav, AppPages, and App.tsx no longer reference integrations. file: `web/src/App.tsx`.

## P10: Memory tab
`kind: framing`

**Goal**: Memory management in the panel, with the 3D knowledge graph behind an explicit full-width view.

### 10.1 Add the transient panel-width override to useActivityPanel [category: code]
`kind: deliverable`

Targets:
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/__tests__/useActivityPanel.test.tsx`

Extend `useActivityPanel` with a transient full-width override — **not** `setMode('panel')`, which would persist to `gobby-activity-panel-layout` and strand the user in a full-width panel on reload:

```ts
viewOverride: 'panel' | null;        // never written to localStorage
requestPanelOverride(): void;
releasePanelOverride(): void;
effectiveMode: Mode;                  // 'panel' while override is set (desktop only)
```

Auto-clear rules: the override releases on `handleTabChange`, on the explicit layout toggles (`toggleFromChat` / `toggleFromPanel` — the user re-asserting layout wins), and the requesting view must release on unmount. On mobile, `effectiveMode` continues to derive from `mobileView` and the override is inert. Unit-test entry, exit, every auto-clear path, and that localStorage is untouched throughout.

**Acceptance:**

- 10.1.1 - Override API exists with the auto-clear rules and effectiveMode derivation. symbol: `requestPanelOverride` in `web/src/components/activity/useActivityPanel.ts`.
- 10.1.2 - Tests cover entry/exit/auto-clear/no-persist (localStorage layout key unchanged while overridden). test: `web/src/components/activity/__tests__/useActivityPanel.test.tsx`.

### 10.2 Build the Memory activity tab [category: code] (depends: 10.1, P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/MemoryTab.tsx`
- `web/src/components/activity/memory/MemoryTabData.ts`
- `web/src/components/activity/memory/MemoryTabList.tsx`
- `web/src/components/activity/memory/MemoryDetailPanel.tsx`
- `web/src/components/activity/memory/MemoryTabActions.ts`
- `web/src/components/activity/memory/__tests__/MemoryTab.test.tsx`
- `web/src/components/activity/ActivityPanelTabs.tsx`
- `web/src/components/activity/useActivityPanel.ts`
- `web/src/components/activity/ActivityPanel.tsx`

Source (read-only; deletion in 10.4): `components/memory/MemoryPage.tsx` (397L) + satellites (`MemoryDetail`, `MemoryFilters`, `MemoryForm`, `MemoryTable`; `KnowledgeGraph` is handled in 10.3). Data: the existing memory hook.

- Toolbar: search + filters (memory type/category pills from the source page move into the filter dropdown; 24H recency filter as a checkbox row). List rows: content preview, type label (leading inline label + lightness, no side stripes), created date; row kebab: delete, copy-content.
- `MemoryDetailPanel.tsx` — draft-based Save/Discard: `content` (TextAreaField), type/category (SelectField), `tags` (TagsField).
- `useMemory` has **no WS subscription** — `MemoryTabActions.ts` refetches after every mutation and the toolbar carries a visible manual refresh button.
- Registration: three edits, tab id `memory`.

**Acceptance:**

- 10.2.1 - Memory tab registered; list with type/recency filters works against live data. symbol: `MemoryTab` in `web/src/components/activity/MemoryTab.tsx`.
- 10.2.2 - Detail pane edits memory content/type/tags through a draft with Save/Discard; mutations refetch; manual refresh present. symbol: `MemoryDetailPanel` in `web/src/components/activity/memory/MemoryDetailPanel.tsx`.
- 10.2.3 - Tab passes the recipe verification gates; no >1px side-stripe borders on memory rows. behavior: "recipe gates pass, grayscale clean" in chrome-devtools verification.

### 10.3 Add the full-width Memory Graph view [category: code] (depends: 10.2)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/memory/MemoryGraphView.tsx`
- `web/src/components/activity/memory/KnowledgeGraph.tsx`
- `web/src/components/activity/memory/__tests__/MemoryGraphView.test.tsx`

A **Graph** `DetailActionButton` on the Memory tab's status strip opens the knowledge graph at full width: it sets the tab's `viewMode` to `graph` AND calls `requestPanelOverride()` so the activity panel expands to the full viewport; Close (button and Esc — one handler) and unmount call `releasePanelOverride()`, restoring whatever layout the user had. The 3D graph **never renders in the narrow column** — the button is the only entry — and the button is hidden under `useIsMobile()` with a one-line hint in its place.

`components/memory/KnowledgeGraph.tsx` **moves** to `activity/memory/KnowledgeGraph.tsx` with its test; `MemoryGraphView.tsx` lazy-imports it (`React.lazy`) so the 3D dependency stays out of the main chunk — verify with a build-output size check.

**Acceptance:**

- 10.3.1 - Graph button expands to full-width panel via the override and restores the prior layout on close/Esc/unmount. symbol: `MemoryGraphView` in `web/src/components/activity/memory/MemoryGraphView.tsx`. test: `web/src/components/activity/memory/__tests__/MemoryGraphView.test.tsx`.
- 10.3.2 - KnowledgeGraph is lazy-loaded; the 3D dependency is absent from the main bundle chunk. behavior: "3D dep in a lazy chunk per build output" in `web/` build verification.
- 10.3.3 - Graph button hidden on mobile; graph never mounts in the narrow column. behavior: "no graph entry below 768px" in chrome-devtools verification.

### 10.4 Delete MemoryPage and satellites [category: refactor] (depends: 10.3)
`kind: deliverable`

Targets:
- `web/src/components/memory/MemoryPage.tsx`
- `web/src/components/memory/MemoryDetail.tsx`
- `web/src/components/memory/MemoryFilters.tsx`
- `web/src/components/memory/MemoryForm.tsx`
- `web/src/components/memory/MemoryTable.tsx`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Delete the `components/memory/` subtree (page plus the four table/form/filter/detail satellites enumerated in Targets; the knowledge-graph module was already moved in 10.3). Nav edits: memory entry, `APP_VALID_TABS` string, orphan `MemoryIcon` import, AppPages lazy import, `activeTab === "memory"` branch. Gate: repo-wide grep for `components/memory/` returns no hits.

**Acceptance:**

- 10.4.1 - The `components/memory/` subtree is deleted; repo-wide grep returns no hits. file: `web/src/components/memory/MemoryPage.tsx`.
- 10.4.2 - Nav, AppPages, and App.tsx no longer reference memory; orphan icon import removed. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`.

## P11: Wiki tab rework
`kind: framing`

**Goal**: Bring the existing Wiki tab up to the canonical bar — it predates the conventions and is the worst off-screen-popup offender.

### 11.1 Rebuild WikiTab to the canonical pattern [category: code] (depends: P1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/activity/WikiTab.tsx`
- `web/src/components/activity/wiki/WikiTabData.ts`
- `web/src/components/activity/wiki/WikiTabList.tsx`
- `web/src/components/activity/wiki/WikiDetailPanel.tsx`
- `web/src/components/activity/WikiSourceRemovalDialog.tsx`
- `web/src/components/activity/WikiTab.utils.ts`
- `web/src/components/activity/wiki/__tests__/WikiTab.test.tsx`

Rebuild `WikiTab.tsx` (295L) as a multi-file family on the canonical shape: toolbar with `ActivityPanelSearch` + filters, list up top, `ResizeHandle`, detail below with `DetailPaneHeader`. All row/entry actions move into the shared `QuickMenu` (eliminating its off-screen popups). Keep the working behavior of `WikiSourceRemovalDialog.tsx` and `WikiTab.utils.ts` (re-home utils into the family); inventory the current tab's capabilities with port / fix / drop-as-broken dispositions in the closing commit. Full impeccable pass — deutan-safe states, no side stripes, 44px targets, dark/light parity. Wiki/CodeWiki *graph* surfaces remain out of scope per Constraints.

**Acceptance:**

- 11.1.1 - WikiTab is a multi-file family on the canonical list/detail shape with toolbar search. file: `web/src/components/activity/wiki/WikiTabList.tsx`.
- 11.1.2 - All wiki row actions render through the shared QuickMenu and stay on-screen at viewport edges. behavior: "wiki menus fully visible at viewport edges" in chrome-devtools verification.
- 11.1.3 - Tab passes the recipe verification gates. behavior: "recipe gates pass" in chrome-devtools verification.

## P12: Configuration audit
`kind: framing`

**Goal**: Inventory the real configuration surface before designing the settings overlay — the current page is explicitly not the model.

### 12.1 Author the configuration audit [category: docs]
`kind: deliverable`

Targets:
- `docs/audits/configuration-audit.md`

A code-archaeology deliverable, committed as a reviewable artifact (Josh signs off on the matrix before P13 starts):

1. **Backend surface**: every route + model in `src/gobby/servers/routes/configuration*.py` (core, models, values, prompts, templates, import_export, validation_detection, tool_approvals, secrets, context, ui_settings) plus schema fields in `src/gobby/config/` and `telemetry` config.
2. **Frontend surface**: every control in `web/src/components/ConfigurationPage.tsx` (954L) and its satellites (`.SchemaField`, `.SecretsTab`, `.TemplateTab`, `ValidationDetectionEditor.tsx`, `useConfiguration.ts`), plus client-only settings in `useSettings.ts` (theme, chat mode, etc. — candidates for the same overlay).
3. **Matrix**: one row per option — option → backend source → frontend control → status (`live` / `dead-backend` / `dead-frontend` / `mismatched-type` / `missing-validation`) → disposition (`keep` / `drop` / `fix`).
4. **Proposed overlay IA**: ordered section list assigning every `keep` option to a section; `drop`/`fix` rows enumerated as follow-up cleanup items.

**Acceptance:**

- 12.1.1 - Audit exists with the backend inventory, frontend inventory, full matrix, and proposed overlay IA. file: `docs/audits/configuration-audit.md`.
- 12.1.2 - Every matrix row carries a status and a disposition; drop/fix rows are enumerated as follow-ups. behavior: "complete matrix with dispositions" in `docs/audits/configuration-audit.md`.

## P13: Settings overlay
`kind: framing`

**Goal**: Configuration becomes a header cog opening a full-screen settings overlay built from the audited IA.

### 13.1 Build the settings overlay shell and header cog [category: code] (depends: 12.1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`. **Checkpoint: present a design mock (chrome-devtools screenshots of the shell with placeholder sections, dark + light) to Josh for review before building 13.2.**

Targets:
- `web/src/components/settings/SettingsOverlay.tsx`
- `web/src/components/settings/SettingsNav.tsx`
- `web/src/components/settings/useSettingsOverlay.ts`
- `web/src/App.tsx`
- `web/src/components/settings/__tests__/SettingsOverlay.test.tsx`

- `SettingsOverlay.tsx` — full-screen `role="dialog" aria-modal="true"` surface lazy-loaded above the app shell (conditional render, no route change — chat state is preserved underneath). Esc closes via a keydown handler that respects `event.defaultPrevented` (CodeMirror instances and open dropdowns inside the overlay swallow Esc first). Focus trap on open, focus restore to the cog on close. Internal layout: left `SettingsNav` (section list from the audit IA), right content area.
- Cog button in the `App.tsx` header actions (around line 728): same shape/size/variant as `ThemeToggle`, immediately to its right; opens the overlay.
- `useSettingsOverlay.ts` — open/close state, section routing, dirty-section guard hook.

**Acceptance:**

- 13.1.1 - Cog button renders right of ThemeToggle and opens the overlay; chat state survives open/close. file: `web/src/App.tsx`.
- 13.1.2 - Overlay is a focus-trapped dialog; Esc closes only when not consumed by inner editors/dropdowns; focus restores to the cog. test: `web/src/components/settings/__tests__/SettingsOverlay.test.tsx`.
- 13.1.3 - Design mock (dark + light) reviewed with Josh before 13.2 begins. behavior: "mock review recorded" in the build-task discussion.

### 13.2 Build the settings sections from the audit IA [category: code] (depends: 13.1)
`kind: deliverable`

Before any work: load the `impeccable` skill from `gobby-skills`, read `.impeccable.md`, and read `docs/guides/one-surface-tab-recipe.md`.

Targets:
- `web/src/components/settings/sections/`
- `web/src/components/settings/__tests__/sections.test.tsx`

One file per audit-IA section under `sections/` (names finalized by the audit — the IA section list in `docs/audits/configuration-audit.md` is the authoritative input; only `keep` rows are built). Controls use the `fields/` primitives with per-section draft + Save/Discard (the `DetailPaneHeader` pair adapts to a section footer here); secrets render masked with reveal; client-only settings from `useSettings.ts` (theme, chat mode) join their natural sections. Section-level dirty guards route through the shared confirm path before section switch or overlay close.

**Acceptance:**

- 13.2.1 - Every keep-row option from the audit is editable in exactly one section; sections save through drafts with Save/Discard. file: `web/src/components/settings/sections/`.
- 13.2.2 - Dirty-section guard blocks section switch and overlay close until saved or discarded. test: `web/src/components/settings/__tests__/sections.test.tsx`.
- 13.2.3 - Overlay passes grayscale, AA dark+light, 44px, and 390×844 scroll verification. behavior: "overlay gates pass" in chrome-devtools verification.

### 13.3 Delete ConfigurationPage and satellites [category: refactor] (depends: 13.2)
`kind: deliverable`

Targets:
- `web/src/components/ConfigurationPage.tsx`
- `web/src/components/ValidationDetectionEditor.tsx`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/App.tsx`

Delete `ConfigurationPage.tsx` and its satellites (`.SchemaField`, `.SecretsTab`, `.TemplateTab`, and `ValidationDetectionEditor.tsx` if absorbed into a section — hoist it first if anything else imports it). Nav edits: configuration entry, `APP_VALID_TABS` string, orphan `ConfigurationIcon` import, AppPages lazy import, `activeTab === "configuration"` branch. Gate: repo-wide grep for `ConfigurationPage` returns no hits.

**Acceptance:**

- 13.3.1 - ConfigurationPage and satellites are deleted; repo-wide grep returns no hits. file: `web/src/components/ConfigurationPage.tsx`.
- 13.3.2 - Nav, AppPages, and App.tsx no longer reference configuration; orphan icon import removed. symbol: `APP_VALID_TABS` in `web/src/components/app/appNavigation.tsx`.

## P14: Hamburger removal and end state
`kind: framing`

**Goal**: Nothing left in the hamburger — remove it, delete the dead Dashboard, and finalize the header.

### 14.1 Remove the hamburger, Sidebar, and DashboardPage; finalize the header [category: refactor] (depends: P7, 8.3, 9.3, 10.4, 11.1, 13.3)
`kind: deliverable`

Targets:
- `web/src/App.tsx`
- `web/src/components/Sidebar.tsx`
- `web/src/components/app/AppPages.tsx`
- `web/src/components/app/appNavigation.tsx`
- `web/src/components/app/useAppCommandPalette.ts`
- `web/src/components/app/useAppKeyboardShortcuts.ts`
- `web/src/hooks/useDashboard.ts`

Final gate, only after every in-scope migration is in:

- Remove the Sidebar render, `sidebarOpen` state, and the hamburger trigger from `App.tsx`; move Logout from the Sidebar into the header as an icon button (rightmost). End-state header: brand + health badge, `ProjectSelector`, `ThemeToggle`, settings cog, Logout.
- Delete `Sidebar.tsx` and the nav-orphaned `components/dashboard/` subtree (DashboardPage has no nav entry today; it is dead code). Delete orphaned hooks (`useDashboard.ts` etc.) after a zero-importers grep.
- Prune `AppPages.tsx` and `appNavigation.tsx` to whatever remains (likely deletable entirely); remove hash-based page routing remnants — `#chat` is the only route, and legacy hashes fall through to chat.
- Update the command palette and keyboard shortcuts (`useAppCommandPalette.ts` / `useAppKeyboardShortcuts.ts`): page-navigation entries become activity-tab activation entries plus "Open Settings".
- Full `cd web && npm run type-check && npm run test -- --run`, full lint, and `npm run build` green; `git grep` for each deleted page module returns no hits.

**Acceptance:**

- 14.1.1 - Header contains brand/health, ProjectSelector, ThemeToggle, cog, Logout — no hamburger trigger; Sidebar.tsx deleted. file: `web/src/App.tsx`.
- 14.1.2 - DashboardPage and the dashboard subtree are deleted; command palette routes to activity tabs and Open Settings. file: `web/src/components/app/appNavigation.tsx`.
- 14.1.3 - Full web type-check, test run, lint, and build are green; no orphan page references remain. behavior: "epic-close web CI green" in `web/` test scope.

## V1 Plan Changelog
`kind: verification`

**Round 0**

- reviewer_run: none (initial draft)
- verdict: drafted
- findings: none yet
- resolution_notes: Initial draft. Supersedes `.gobby/plans/task-14923-one-surface-tabs-migration.md` (task-14923 was never registered in the plans table; its file moves to `.gobby/plans/completed/`). Differences from the superseded plan: MCP is polish-on-existing-tab rather than greenfield (ActivityMcpTab shipped in the interim); Reports/Cron/Traces deliverables dropped (already done); Memory's List|Graph segment became a full-width graph override; Skills' Installed|Hub segments have intentionally different layouts; added Save/Discard draft editing, kebab QuickMenu canon with viewport clamping, Wiki tab rework, Configuration audit + settings overlay + header cog, Dashboard deletion; teardown gates changed from parity-with-legacy to spec-plus-zero-importers. Post-draft revisions: added backend rule-rename deliverable (2.1) with type-scoped collision check and bundled-rule guard, renumbering P2 (tab 2.2, YAML 2.3, teardown 2.4) and updating 7.1's dependency; confirmed Save/Discard stays for all non-task surfaces after weighing tasks' existing commit-on-blur pattern (field-level PATCH + harmless intermediate states there vs. full-object PUTs of live behavior-bearing config here).
