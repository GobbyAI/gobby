# One-Surface Activity Tab Recipe

Before starting any one-surface activity-panel UI work, load the `impeccable`
skill from the `gobby-skills` MCP server and read the repository design contract
in `.impeccable.md`. Then read this guide. The migration is an impeccable
redesign into the activity panel, not a direct copy of legacy pages.

Use [Frontend Style Guide](frontend-style-guide.md) for the token, Tailwind 4,
CVA, accessibility, and visual-system rules that apply to every surface.

## 1. Exemplars

Use `web/src/components/activity/SessionsTab.tsx` as the activity-toolbar
interaction exemplar:

- Its `SegmentedControl` shows the toolbar segment pattern for `Live | Expired`.
  New toolbar segments must pass `controlHeight="sm"`.
- Its Transcript/Summary switch is the alternate-view pattern: the selected row
  stays in place while the bottom-pane content swaps inline.
- Its status/action strip shows how compact bottom-pane actions should sit in
  the panel instead of opening a separate page.

Use `web/src/components/activity/TasksTab.tsx` and the `TasksTab*` family as the
multi-file exemplar:

- `TasksTab.tsx` owns orchestration, selected state, fetch flow, and composition.
- `TasksTabData.ts`, `TasksTabModel.ts`, `TasksTabToolbar.tsx`,
  `TasksTabFilters.tsx`, `TasksTabActions.ts`, `TasksTabDetailPanel.tsx`, and
  `TasksTabList.tsx` keep data shaping, UI sections, actions, and list/detail
  rendering out of the shell.
- Tests live beside the activity components under
  `web/src/components/activity/__tests__/`.

Do not treat current Tasks inline-edit behavior as the new editing model. It is
an existing surface kept stable during this epic. New migrated surfaces use the
draft-based Save/Discard convention below.

## 2. File-Family Checklist

Each migrated surface gets a file family under
`web/src/components/activity/<surface>/`, with a small tab shell exported through
the activity root. Keep non-test `.ts` and `.tsx` files below 1,000 lines.

Build the family in this shape:

- `XTab.tsx` in the activity root or a thin root export that composes the family.
- `XTabData.ts` for API calls, fetch helpers, and normalization.
- `XTabModel.ts` for local types, filters, sort keys, labels, and pure helpers.
- `XTabToolbar.tsx` for `ActivityPanelSearch`, optional
  `SegmentedControl controlHeight="sm"`, and filter trigger.
- `XTabFilters.tsx` when filters need their own dropdown or model.
- `XTabList.tsx` for the top list and row actions.
- `XTabDetailPanel.tsx` for the bottom detail pane and alternate detail views.
- `XTabActions.ts` for save, create, enable, delete, install, or other wire
  actions that need surface-specific API orchestration.
- `__tests__/` coverage for data/model helpers, key interactions, and regressions.

The runtime layout is consistent across surfaces:

- Toolbar at the top of the tab with search first, then segments and filters.
- Top list pane for rows, using realistic empty, loading, and error states.
- Vertical `ResizeHandle` between list and detail where the existing panel shape
  supports resizing.
- Bottom detail pane with an `h-10` status strip.
- Empty states rendered with `ActivityPanelEmpty`, not bespoke loose empty
  blocks.

## 3. Save/Discard Convention

New detail panes are draft-based. Use `useDetailDraft` from
`web/src/components/activity/fields/` once the shared field family lands.

The detail pane owns a draft derived from the latest fetched canonical detail.
Edits update draft state only. They do not write on blur, row change, segment
change, or tab change.

Save and Discard render in the bottom-pane status strip through
`DetailPaneHeader`:

- Save is explicit, uses the accent treatment, and writes the draft over the
  freshest detail fetched for that row.
- Discard is explicit, uses the ghost treatment, and restores the last canonical
  detail.
- `serverChanged` is shown in the strip when the canonical detail changes while
  the user has unsaved draft edits.
- Surface-specific actions still live in `XTabActions.ts`, because Rules, MCP,
  Skills, Integrations, and Memory each save through different API shapes.

Dirty state must guard every route out of unsaved work through one confirm path:

- Row selection changes inside a pane.
- Segment or alternate-view changes inside a pane.
- Activity-tab changes from the panel shell.
- Panel close and layout toggles.

Pane-local transitions use `useDetailDraft.confirmIfDirty`. Shell-originated
transitions use the dirty-guard registry from the activity shell. New surfaces
must not add their own second confirm dialog.

## 4. Kebab Convention

Row actions live behind the shared `QuickMenu` primitive at
`web/src/components/activity/QuickMenu.tsx` once it lands. The trigger is a
three-vertical-dot button with a 44px target.

Menus must never render off-screen:

- Measure the trigger and menu with `getBoundingClientRect`.
- Flip above the trigger near the bottom viewport edge.
- Clamp horizontally with an 8px viewport gutter.
- Support Escape, outside click, ArrowUp, ArrowDown, Home, End, and Enter.
- Pair destructive color with icon/text. Never rely on hue alone.

Do not copy older ad-hoc `position: fixed` coordinate menus. They are the thing
the shared primitive replaces.

## 5. Registration: Exactly Three Edits

Register a new activity tab with exactly these three code edits:

1. Add the id to the `ActivityTab` union and `ACTIVITY_PANEL_TABS` in
   `web/src/components/activity/ActivityPanelTabs.tsx`. Use a 24x24 outline
   icon that matches the existing selector weight.
2. Add the id to `VALID_TABS` in
   `web/src/components/activity/useActivityPanel.ts` so localStorage restore
   accepts it.
3. Add the `tabContent` case in
   `web/src/components/activity/ActivityPanel.tsx`.

Keep the existing `gobby-activity-panel-tab-v2` storage key stable. Add ids; do
not rename existing ids.

## 6. Teardown Checklist

Teardown is spec-gated, not parity-gated. Several legacy surfaces include broken
or incomplete controls. Do not preserve broken behavior for parity.

Before deleting a legacy page, write a capability inventory with one disposition
per capability:

- `port`: implemented in the new activity tab.
- `fix`: implemented differently because the legacy behavior was broken or
  unsafe.
- `drop-as-broken`: intentionally removed because it did not work and is not in
  the plan.

The deletion gate is:

- New activity tab works per the plan and this guide.
- Repo-wide zero-importers search proves no live code imports the legacy page or
  deleted components.
- Navigation edits are complete:
  - Remove the `appNavigation.tsx` entry.
  - Remove the `APP_VALID_TABS` string.
  - Remove orphan icon imports.
  - Remove the `AppPages.tsx` lazy import.
  - Remove the `App.tsx` render branch.
- Deletions land in a separate commit from the replacement tab commit.

Legacy hashes are not bridged. After hamburger removal, orphaned hashes dead-end
to default chat.

## 7. Per-Surface Verification Recipe

Run focused verification after the final edit for each surface. Do not run the
full pytest suite.

Required command gates for frontend surface work:

```bash
cd web && npm run type-check
cd web && npx vitest run <family-or-test-file>
cd web && npm run lint:js && npm run lint:css && npm run lint:tokens
```

Any Python test command must be prefixed with `GOBBY_TEST_PROTECT=1`.

Required Chrome DevTools MCP walkthrough at `http://localhost:60889/#chat`:

- Verify the tab in dark theme and light theme.
- Capture dark and light screenshots.
- Emulate 390x844 and confirm all controls remain reachable with 44px targets.
- Check grayscale or achromatopsia legibility. State must remain clear without
  relying on hue.
- Spot-check WCAG 2.2 AA contrast for text, controls, focus rings, and status
  treatments.
- Exercise Save and Discard wiring: dirty state appears, Save writes, Discard
  reverts, and dirty guards block row, segment, tab, and panel-close transitions.
- Open the kebab on the bottom-most visible row and near the right viewport edge.
  The menu must stay fully visible and keyboard-operable.
- Check empty, loading, error, overflow, and long-content states.

For non-frontend leaves that support the migration, run their focused command
gates and record why visual verification is not applicable. UI leaves still need
the DevTools walkthrough.

## 8. Pitfalls

Avoid these recurring failures:

- Copying legacy modals. New create/edit flows should prefer detail-pane create
  mode. Modals need a strong reason.
- Leaving `border-left` or `border-right` accents wider than 1px on cards, list
  rows, callouts, or alerts.
- Adding Tailwind `md:` variants. The JavaScript mobile breakpoint is 768px; use
  the established responsive hooks and component-level adaptation.
- Rendering unclamped `position: fixed` menus.
- Letting a single non-test tab file cross the 1,000-line ceiling.
- Forgetting `controlHeight="sm"` on activity-toolbar segments.
- Serializing YAML from stale component state. Fetch the latest detail, merge
  the draft over it, then serialize.
- Relying on color alone for state. Use icon, text, position, and lightness.
- Adding raw colors where tokens already exist.
- Treating `.impeccable.md` as optional. It is the design contract for this epic.
