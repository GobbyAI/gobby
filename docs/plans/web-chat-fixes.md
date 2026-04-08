# Plan: #11509 Web Chat UX Fixes

## Context

Epic #11509 has 8 subtasks from Drawbridge UI annotations. All are open/new. Before starting implementation, two housekeeping items need handling: adding `.playwright-cli/` to `.gitignore` and cleaning up 11 stale `agent-*` worktrees.

The tasks split into three natural groups by area of the codebase they touch.

---

## Phase 0: Housekeeping (chore, no task needed)

1. **Add `.playwright-cli/` to `.gitignore`** — append after the existing `.playwright/` entry
2. **Remove 11 stale agent worktrees** — `git worktree remove` for each `agent-*` under `.claude/worktrees/`
3. Single commit for both

---

## Phase 1: Bug Fixes (P1)

### #11510 — Mystery CLI sessions + "No messages yet" in activity panel

**Investigation needed.** First step: use Playwright to screenshot the activity panel sessions tab and identify what mystery sessions appear. Then:
- What sessions appear that shouldn't (mystery CLI sessions)
- Why "No messages yet" shows for sessions that do have messages
- Likely culprits: `SessionsTab.tsx` fetch filtering (lines 92-109), `useSessionDetail` hook, or session source filtering

**Validation:** Playwright screenshot of sessions tab showing only expected sessions with messages rendering correctly.

**Files:** `web/src/components/activity/SessionsTab.tsx`, `web/src/hooks/useSessionDetail.ts`

### #11512 — Session click should not switch main chat to Observing mode

**Root cause identified.** The sidebar (`ConversationPicker.tsx:333-342`) and `ActiveSessionsModal.tsx` call `onViewCliSession` which triggers `viewSession()` in useChat — this sets `viewingSessionId`, which disables the chat input and shows the observation CommandBar segment.

**Fix:** Session clicks in the sidebar/modal for CLI sessions should show the transcript inline in the activity panel (via `activity.showTab("sessions")`) rather than hijacking the main chat into observing mode. The activity panel's `SessionsTab` already has its own local message viewing — we just need to wire the sidebar click to open that instead.

**Files:**
- `web/src/App.tsx` (~line 666-670) — `handleViewCliSession` should open activity panel sessions tab + select the session, instead of calling `viewSession()`
- `web/src/components/activity/SessionsTab.tsx` — may need to expose a way to programmatically select a session (e.g., via prop or ref)
- `web/src/components/chat/ConversationPicker.tsx` — verify the click path
- `web/src/components/chat/ActiveSessionsModal.tsx` — same fix

---

## Phase 2: UI Polish (P2 bugs)

### #11511 — Project selector z-index (renders behind activity panel)

**Root cause identified.** `ProjectSelector.tsx` dropdown uses `z-50`. The activity panel renders inline in the flex layout but on mobile uses `z-200`. More importantly, the panel's content (particularly filter dropdowns at `z-99`/`z-100`) can overlap.

**Fix:** Render the dropdown via a React portal at document root so it escapes any parent stacking context and always appears in the foreground (above activity panel, modals, etc.). This avoids z-index wars entirely — the portal gets the same z-250 treatment as Dialog overlays.

**Files:** `web/src/components/ProjectSelector.tsx` (line 98) — wrap dropdown in `createPortal`, position absolutely relative to the trigger button using `getBoundingClientRect()`

### #11514 — New chat (+) should use current provider/model selection

**Root cause:** `onNewChat` in CommandBar creates a fresh session but doesn't carry forward the current `chat.provider` selection. The provider state resets to "Auto" on session switch.

**Fix:** Pass the current provider selection through `onNewChat` → `startNewChat` so the new session inherits it. Store last-used provider in localStorage so it persists.

**Files:**
- `web/src/components/chat/CommandBar.tsx` — pass provider to `onNewChat`
- `web/src/hooks/useChat.ts` — persist provider to localStorage, restore on init
- `web/src/App.tsx` — wire through

---

## Phase 3: Feature Work (P2 features)

### #11515 — Replace Claude text with icon, remove Attach, move provider to chat input

**Changes:**
- CommandBar: Replace "Claude" text with a small icon/logo
- ChatInput: Remove the "Attach" (paperclip) button
- ChatInput: Move the provider selector from its current position (below CommandBar, line 309-328 of ChatPage) into the ChatInput toolbar row

**Files:** `web/src/components/chat/ChatPage.tsx`, `web/src/components/chat/ChatInput.tsx`, `web/src/components/chat/CommandBar.tsx`

### #11513 — Provider/model picker redesign (icon button + confirmation modal)

**Changes:**
- Replace the plain `<select>` with an icon button that opens a Radix Dialog modal
- Modal shows available providers with model sub-options in a list layout
- Confirmation step before switching mid-conversation (to avoid accidental changes)

**Pattern:** Model after `ActiveSessionsModal` — uses the project's Radix `Dialog`/`DialogContent`/`DialogTitle`/`DialogDescription` wrappers from `web/src/components/chat/ui/Dialog.tsx` (z-index 250, portal-rendered, overlay + centered content). List-style selection with provider rows, similar to how ActiveSessionsModal renders agent/session rows.

**Files:** New component `web/src/components/chat/ProviderPicker.tsx`, modifications to `ChatInput.tsx` and `ChatPage.tsx`

### #11516 — New chat (+) moves existing chat to activity panel as Watching session

**Changes:**
- When clicking (+), the current session continues running but moves to the activity panel's sessions tab as a "Watching" entry
- User can see it streaming in the activity panel while working in a fresh chat

**Files:** `web/src/App.tsx` (session management), `web/src/hooks/useSessions.ts`, `web/src/components/activity/SessionsTab.tsx`

### #11517 — Add local model selection for Claude in model picker

**Changes:**
- Extend the provider picker to show model options (Opus, Sonnet, Haiku) when Claude is selected
- Fetch available models from daemon API or use a static list
- Wire model selection through to useChat's send path

**Files:** `web/src/components/chat/ProviderPicker.tsx` (new), `web/src/hooks/useChat.ts`, daemon API if model endpoint doesn't exist yet

---

## Execution Order

1. **Phase 0** — Housekeeping commit (gitignore + worktree cleanup)
2. **#11511** — Quick z-index fix (5 min, unblocks other UI work)
3. **#11510** — Investigate mystery sessions (needs browser testing)
4. **#11512** — Fix observing mode on session click
5. **#11514** — Provider persistence on new chat
6. **#11515** — UI cleanup (icon, remove attach, move provider)
7. **#11513** — Provider/model picker redesign (depends on #11515 moving provider to input)
8. **#11517** — Model selection (depends on #11513 picker)
9. **#11516** — New chat → watching session (most complex, touches session lifecycle)

---

## Verification (Playwright CLI)

All visual verification uses the `playwright-cli` skill via MCP. Each task's validation criteria must include Playwright steps.

**Per-task Playwright verification:**

| Task | Playwright verification |
|------|----------------------|
| #11511 | Navigate to chat page → open project selector dropdown → screenshot → verify dropdown renders above activity panel (no clipping/overlap) |
| #11510 | Navigate to chat page → open activity panel sessions tab → screenshot → verify no mystery sessions appear; click a real session → verify messages render (not "No messages yet") |
| #11512 | Navigate to chat page → open activity panel sessions tab → click a CLI session → screenshot main chat area → verify chat input is NOT disabled and CommandBar does NOT show observation segment |
| #11514 | Select provider → click (+) new chat → screenshot → verify provider selection persists in new chat |
| #11515 | Screenshot chat input area → verify: no "Claude" text (icon instead), no Attach/paperclip button, provider selector is inside input toolbar |
| #11513 | Click provider icon in chat input → screenshot picker modal → verify provider list with model sub-options and confirmation flow |
| #11516 | Start a chat → click (+) new chat → screenshot activity panel → verify previous session appears as "Watching" entry with live streaming |
| #11517 | Open provider/model picker → select Claude → screenshot → verify model options (Opus, Sonnet, Haiku) appear; select one → verify it persists |

**Build verification:**
- `npm run build` in `web/` — no TS errors
- `npm test` in `web/` — existing tests pass
