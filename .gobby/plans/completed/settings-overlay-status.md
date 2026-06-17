# Settings Overlay — Status & Next Steps

_Handoff written 2026-06-14. Gitignored (`.gobby/handoffs/`), so it does not add
to the branch review. Branch: `0.5.0`._

## TL;DR

Epic **#16998** (hamburger → activity-panel migration) is down to the **Settings
overlay chain + hamburger removal**. Phase A (consistency cleanup) is **done**.
**#17045 (settings overlay shell + header cog) is built, validated, and
committed** as `ad8f61917`, but it is **paused at its human checkpoint
(§13.1.3)** awaiting your mock sign-off. It has been **escalated** to reflect
that — resume by de-escalating it.

## Where things stand

### Epic #16998 leaves
| Task | What | State |
|---|---|---|
| #17045 | Settings overlay shell + header cog | **Built + committed `ad8f61917`; escalated, awaiting §13.1.3 mock sign-off** |
| #17046 | Build the 13 settings section bodies from the audit IA | Open, blocked on #17045 |
| #17047 | Delete ConfigurationPage + satellites | Open, blocked on #17046 |
| #17048 | Remove hamburger / Sidebar / DashboardPage; finalize header | Open, blocked on #17047 (final epic gate) |
| #16998 | Close the epic | After all leaves done |

Phase A (A.1 #17092 → A.4 #17095) is fully closed.

### What #17045 delivered (commit `ad8f61917`, 12 files)
New `web/src/components/settings/` family:
- `SettingsOverlay.tsx` — `role=dialog aria-modal` full-screen overlay, lazy-loaded
  above the app shell (chat stays mounted underneath). Focus trap, focus-into on
  open, focus-restore to the cog on close, Esc that respects `defaultPrevented`
  (closes the section dropdown first, then the overlay), backdrop-click close.
- `useSettingsOverlay.ts` — open/close, active-section routing, dirty-section
  guard registry behind one confirm path (`registerDirtyGuard`).
- `sections.ts` — the 13-section IA from `docs/audits/configuration-audit.md`
  (`appearance` … `runtime-infrastructure`), `as const satisfies`,
  `SettingsSectionId` union, `DEFAULT_SETTINGS_SECTION = 'appearance'`.
- `index.ts` barrel; `__tests__/SettingsOverlay.test.tsx` (11 tests).

Sections are picked from a **dropdown** (reuses `ActivityFilterDropdown`), not a
left rail (per your feedback). Content area is the scroll region.

Header + icons:
- `App.tsx` — accent cog (`btn btn-accent btn-sm app-settings-cog`) right of
  `ThemeToggle`; lazy `<SettingsOverlay>` mounted only while open; `useSettingsOverlay`.
- `icons/AppIcons.tsx` — added `SettingsCogIcon` (gear), `LightbulbIcon`, `ChevronDownIcon`.
- `ThemeToggle.tsx` — light affordance is now the lightbulb (was a sun that read
  like the cog).
- `styles/settings-overlay.css`, `styles/app-shell.css` (cog pin), `main.tsx` (css import).

### Validation (all green)
- TDD: `SettingsOverlay.test.tsx` red (unresolved import) → green.
- `cd web && npm run type-check` clean.
- Tests: settings 11/11, ThemeToggle 4/4.
- `npm run lint:js && npm run lint:css && npm run lint:tokens` clean.
- `gobby test-quality audit` 0 issues.
- chrome-devtools dark + light verified; 390×844 full-bleed + 44px targets.
- Mocks: `.gobby/tmp/settings-dropdown-{dark,light}.png` (gitignored).

## The decision you paused on (§13.1.3)
Sign off on the dark+light shell mock (icons + section dropdown), or request more
shell tweaks, **before** #17046 builds the real section bodies.

## Next steps when you return
1. **Review the mock** (`.gobby/tmp/settings-dropdown-{dark,light}.png`, or open
   `http://localhost:60889/#chat` and click the header cog).
2. **If approved:** de-escalate #17045 (`gobby-tasks de_escalate_task`), record
   "mock review recorded" for §13.1.3, then close #17045 with commit
   `ad8f61917`. Then start **#17046**.
3. **If changes wanted:** de-escalate #17045 and tell the next session what to
   change; the shell iterates before #17046.
4. **#17046 plan:** one `<PascalCase(id)>Section.tsx` per keep-section (13),
   ordered registry, per-section draft + Save/Discard via `useDetailDraft`,
   secrets masked with reveal, section dirty-guard via
   `useSettingsOverlay.registerDirtyGuard`. Client-only settings from
   `useSettings.ts` join their sections.

## Open follow-ups / notes
- A **second, older settings surface still exists**: the lightweight
  `web/src/components/Settings.tsx` modal (font size / theme / default mode),
  opened via the command palette (`setSettingsOpen`). It is untouched and runs in
  parallel with the new overlay. Its fate (fold into the overlay or delete) is not
  yet scoped in a leaf — likely reconcile during #17046/#17047. Its unused
  `SettingsIcon` export is now superseded by `SettingsCogIcon`.
- The header still has the hamburger + `<Sidebar>`; removed in **#17048**.
- `compact_self` self-interrupt mechanism: a "tool use was rejected" result from
  `gobby-sessions:compact_self` means it WORKED (injects the interrupt + /compact
  into the session's own tmux pane). Not a decline.
