# Web Chat: Migrate from localStorage to Database

**Status:** Active
**Created:** 2025-02-24
**Related tasks:** #9129 (plan mode stuck), #9131 (chat mode persistence)

## Goal

Make the web chat's state authoritative from the database. localStorage becomes a fast cache, not the source of truth. Chat history, user preferences, and session state survive browser clears, device switches, and daemon restarts.

---

## Phase 1: Quick Wins (no migrations needed)

### T1. Fix plan mode detection false positive from conversation history
**Files:** `src/gobby/workflows/observers.py`
**Change:** Strip `<conversation-history>...</conversation-history>` blocks from prompt text *before* extracting `<system-reminder>` tags. The current regex picks up historical plan mode indicators nested inside conversation history restoration blocks.
**Risk:** Low — surgical regex change, existing tests cover the observer.

### T2. Load chat messages from DB as source of truth on initial page load
**Files:** `web/src/hooks/useChat.ts`
**Change:** The DB hydration path already exists (lines 730-750, triggered on conversation switch). Refactor `loadMessagesForConversation()` and the initial `useState` to:
1. Show localStorage cache immediately (instant render)
2. Fetch from `/sessions/{dbSessionId}/messages` in parallel
3. Replace localStorage data with DB data when it arrives
4. Fall back to localStorage-only when no `dbSessionId` is available (new unsaved chats)

**Challenge:** On initial load, we don't have `dbSessionId` yet — only `conversationId` (the `external_id`). Need a lookup endpoint or to store the mapping.
**Approach:** Add a `/sessions/by-external-id/{external_id}` route, or store `dbSessionId` alongside `conversationId` in localStorage as a cache key.

### T3. Persist user settings (font size, model, theme) to config_store
**Files:** `web/src/hooks/useSettings.ts`, `src/gobby/servers/routes/` (new endpoint or use config_store API)
**Change:** On settings change, POST to `/config/{key}`. On load, GET from `/config/{key}` with localStorage as fallback cache. Keys: `web.font_size`, `web.model`, `web.theme`.
**Prerequisite:** Check if config_store HTTP endpoints exist. If not, add minimal GET/PUT `/config/{key}` routes.

### T4. Persist selected project ID to config_store
**Files:** `web/src/App.tsx`
**Change:** Same pattern as T3 — write-through to `config_store` with key `web.selected_project_id`. Read from DB on load, fall back to localStorage.

---

## Phase 2: Chat Mode Persistence (migration needed)

### T5. Add `chat_mode` column to sessions table
**Files:** `src/gobby/storage/migrations.py`
**Change:** New migration: `ALTER TABLE sessions ADD COLUMN chat_mode TEXT DEFAULT NULL`
**Values:** `plan`, `accept_edits`, `full_auto`, `bypass`, `NULL` (unset/default)

### T6. Wire ChatSessionPermissions to read/write chat_mode from DB
**Files:** `src/gobby/servers/chat_session_permissions.py`, `src/gobby/servers/websocket/chat.py`
**Change:**
- `__init__`: Load initial `chat_mode` from sessions table (if session exists)
- `set_chat_mode()`: Write-through to DB via `session_manager.update_chat_mode()`
- Add HTTP endpoint: `PATCH /sessions/{id}/chat-mode` for frontend toggle
- Frontend mode selector calls this endpoint instead of only updating local state

### T7. Fix detect_plan_mode_from_context to update DB, not just workflow variables
**Files:** `src/gobby/workflows/observers.py`
**Change:** When plan mode is detected/cleared from system reminders, also update the sessions table `chat_mode` column. This ensures the DB stays in sync even when Claude Code's UI drives the mode change.

---

## Phase 3: Task-Scoped Config (migration needed)

### T8. Add oversight_mode column to tasks table
**Files:** `src/gobby/storage/migrations.py`, task manager, `web/src/components/tasks/OversightSelector.tsx`
**Change:**
- Migration: `ALTER TABLE tasks ADD COLUMN oversight_mode TEXT DEFAULT 'ask_risky'`
- Backend: Expose via existing task update endpoint
- Frontend: Write-through on change, read from task data on load

### T9. Add permission_overrides JSON column to tasks table
**Files:** `src/gobby/storage/migrations.py`, task manager, `web/src/components/tasks/PermissionOverrides.tsx`
**Change:**
- Migration: `ALTER TABLE tasks ADD COLUMN permission_overrides TEXT DEFAULT NULL` (JSON string)
- Backend: Expose via existing task update endpoint
- Frontend: Write-through on change, read from task data on load

---

## Phase 4: Cleanup

### T10. Deprecate localStorage chat message storage
**Files:** `web/src/hooks/useChat.ts`
**Change:** Once DB-first loading is stable, stop writing to localStorage entirely for messages. Remove `saveMessagesForConversation()`, `loadMessagesForConversation()`, and the migration code. Keep localStorage only for:
- Panel widths (artifact, canvas)
- View mode preferences (memory page, KG animation)
- Any genuinely ephemeral UI state

### T11. Add /sessions/by-external-id lookup endpoint (if needed by T2)
**Files:** `src/gobby/servers/routes/sessions.py`
**Change:** `GET /sessions/by-external-id/{external_id}` → returns session with matching `external_id`. Needed so the frontend can resolve `conversationId` → `dbSessionId` on initial load without waiting for the sessions list.

---

## Implementation Order

```
T1 (plan mode fix) ─── standalone, do first
T2 (DB message load) ── T11 (lookup endpoint) if needed
T3 (settings to DB) ─── standalone
T4 (project to DB) ──── standalone (same pattern as T3)
T5 (migration) ──────── T6 (chat mode wire) ── T7 (observer fix)
T5 (migration) ──────── T8 (oversight) + T9 (permissions)
T10 (cleanup) ────────── after T2 is stable
```

T1, T3, T4 are independent quick wins. T2 depends on T11. T5 is the migration gate for T6-T9.

## Verification

- [ ] Plan mode doesn't get stuck when conversation history contains historical plan mode indicators
- [ ] Chat messages load from DB on page refresh (not just localStorage)
- [ ] Settings (font, model, theme) survive localStorage clear
- [ ] Selected project survives localStorage clear
- [ ] Chat mode persists across page refresh and daemon restart
- [ ] Oversight mode and permission overrides persist in DB with task
- [ ] localStorage-only items (panel widths) still work fine
