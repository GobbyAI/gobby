# Drawbridge UI batch — 11 pending annotations + 1 reported bug

> **Round 5 revision (final adversarial round)** — addresses Round 4 adversary findings F1–F4 (recorded in #12911 description). All four were structural plumbing issues in §1.6. Key changes:
>
> - **§1.6 step 0 migration uses real registration pattern** — Round 4 F1: registry pattern is `MIGRATIONS: list[tuple[int, str, MigrationAction]]` in `src/gobby/storage/_migration_registry.py` (existing pattern: `(220, "Add terminal_reason to agent_runs", "ALTER TABLE …")`). New entry at version 221 (or whatever's next). PLUS: add the column to `baseline_schema.sql`'s existing `CREATE TABLE agent_runs` (line 135) and `CREATE TABLE sessions` (line 175) so fresh installs match. NO bare function in `migrations.py`.
> - **§1.6 spawn-time write threads through real chain** — Round 4 F2: real call chain is `spawn_agent_impl` → `SpawnRequest` → `execute_spawn()` → `_spawn_*_terminal()` → `prepare_terminal_spawn()` (`src/gobby/agents/spawn.py`) → `LocalAgentRunManager.create()` AND `ChildSessionManager.create_child_session(ChildSessionConfig)`. Plan now spells out threading: add `is_local: bool = False` to `SpawnRequest`, `prepare_terminal_spawn(...)` (next to existing `requested_reasoning_effort` etc.), `ChildSessionConfig` (next to existing `sandbox_enabled: bool = False`), and `LocalAgentRunManager.create(..., is_local: bool = False)`. ALSO pass `model=effective_model` through `prepare_terminal_spawn` → `agent_run_mgr.create` (currently the model is dropped — `agent_runs.model` is NULL for terminal-spawned runs today; this fix incidentally restores it).
> - **§1.6 + §1.5 session-creation paths corrected** — Round 4 F3: actual storage files are `src/gobby/storage/session_models.py` + `src/gobby/storage/sessions/_crud.py` + `src/gobby/storage/sessions/_manager.py` (NOT `src/gobby/storage/sessions.py` — that path doesn't exist; previous draft was wrong). Plan now names them. Three creation paths get explicit treatment: (a) terminal child via `prepare_terminal_spawn` → `ChildSessionConfig.is_local`; (b) web chat via `routes/sessions/core.py` + `routes/agent_spawn.py` calling `session_manager.create_web_chat_session()` — compute `is_local = (requested_model == "local")` BEFORE local-model resolution; (c) hook auto-recovery via `SessionStartMixin.handle_session_start()` / `SessionLookupService._resolve_session_id()` — no spawn signal, default `is_local=0` and rely on legacy fallback at read time. NOTE: `sessions.model` semantics are unchanged (`ChatSession.model` returns the requested alias today — that's outside §1.5/§1.6 scope; only `is_local` needs to be authoritative).
> - **§1.6 agent-definition cards get a separate config-side predicate** — Round 4 F4: `is_local_legacy_fallback("claude", "local")` returns False because the helper intentionally drops literal-string detection (which would conflict with row-level handling where "local" is never persisted). For agent definition cards (config-side, NOT row-side), add a separate `is_local_agent_definition(provider, model) -> bool` that returns True when `model == "local"` literally OR `provider in LOCAL_PROVIDERS` OR model contains `gpt-oss`. Used in `src/gobby/servers/routes/agents.py` for definition payloads. Test asserts a definition with `model: "local"` renders LOCAL.
>
> Round 4 (still in force): §1.6 promoted from route-time helper to schema-level fix; §3.2 corrected to use `Extract<ProtocolContentSegment, { type: ... }>`; §3.1 names `ActivityPanel.tsx`; §4.1 phase-heading dep moved to `### 4.1`.
>
> Round 3 (still in force): §1.5 + §1.6 introduced the `is_local` concept; §3.2 corrected to use real API shape; §4.1 documents stateful mock pattern; §1.6 + §3.3 dropped invalid `(depends: refactor #12917)` notation.
>
> Round 2 (still in force): §1.5 split into §1.5 + §1.6; §2.1 replaced with attribution-failure hypothesis + failing-test gate; endpoint URLs corrected; §3.2 captures committed fixture; §4.1 retargeted to `ChatPage.tsx:169-172`; §1.2 alias cleanup folded into §1.6 wrap-up.

## Overview

Land the 10 pending Drawbridge freeform annotations from `.moat/moat-tasks-detail.json` (rows 115–124 in `.moat/moat-tasks.md`), the user-asked Sessions-tab LOCAL chip, gobby-task **#11199** (Local chip on agent cards in the Workflows tab), and one user-reported bug (mobile activity panel auto-close when toggled). All work targets the Vite/React frontend at `web/src/`, with one backend touch each for #4 (token-event source/model attribution) and #1.6 (`is_local` field on the agents API). Verification is end-to-end via Chrome DevTools at `http://localhost:60889/#chat`.

## Constraints

- Per-Drawbridge-task lifecycle: flip `.moat/moat-tasks-detail.json` `status: "to do" → "doing"` before edits, `→ "done"` after edits, and tick `[x]` in `.moat/moat-tasks.md`. Batch JSON updates per Gobby task to avoid 10 separate writes.
- Chip work introduces ONE reusable `.chip` class consumed by Tasks list (§1.3), Sessions tab `renderBadges` (§1.5), and agent cards (§1.6). No parallel chip implementations.
- §1.2's `.session-kind-badge` aliases are temporary scaffolding; §1.6 deletes them in its wrap-up step (not a separate follow-up).
- Token Efficiency chart fix (§2.2) preserves visual design; only the green-tooltip-shows-0 bug is fixed by dropping `stackId="tokens"`.
- Granularity selector (§2.3) is **deleted**, not hidden; granularity derives from `hours` (≤6h → `30m`, ≤168h → `1h`, else `1d`).
- Sessions filter dropdown blue (§3.1) applies only to the collapsed trigger; expanded menu styling stays.
- §3.3 (ToolCallCard wrap) and §1.6 (AgentsTab) touch files over 1000 lines. The line-cap rule is satisfied via in-section extraction: a `JsonBlock` component for §3.3 and an `AgentCard` row component for §1.6, each landing in the same commit and bringing the host file under 1000 lines. Refactor task **#12917** is referenced as prose context only — `(depends:)` plan-draft syntax does not accept external task refs and is not used here.
- Backend changes (§2.1 attribution fix, §1.6 `is_local` field) ship in their own commits.
- DO NOT run the full pytest suite (CLAUDE.md). Run scoped tests only.
- Verification must use Chrome DevTools at `http://localhost:60889/#chat` (Vite dev server port; the daemon HTTP API is at 60887).

---

## Phase 1: Chat input + chip standard

**Goal**: Restore the original chat-input footprint, normalize TaskTree font size, introduce a single `.chip` class, and ship LOCAL chips on both Sessions tab rows (user ask) and Workflows agent cards (#11199).

### 1.1 Restore one-line textarea and 36px send button [category: code]

Target: `web/src/components/chat/ChatInput.tsx`

Commit `8efb1a9a0` ("surface Speaker + Microphone toggles in chat input") inflated the textarea minimum height and the primary button. Revert those specific Tailwind class values without touching the auto-grow `useEffect` or the speaker/mic toggles themselves.

Specific edits:

- Line ~721 — textarea className contains `min-h-[52px]`. Change `min-h-[52px]` → `min-h-[36px]`.
- Line ~591 — `primaryButtonClassName` `cn(...)` first argument starts with `'inline-flex h-[52px] w-[52px] self-start ...'`. Change `h-[52px] w-[52px] self-start` → `h-[36px] w-[36px] self-end`. (The `self-start` came in with the inflation; `self-end` keeps the small button bottom-aligned with the textarea, matching pre-#12070 behavior.)

Leave the auto-grow effect intact:

```ts
useEffect(() => {
  const textarea = textareaRef.current
  if (textarea) {
    textarea.style.height = '0'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
    textarea.scrollTop = textarea.scrollHeight
  }
}, [input])
```

The textarea grows to fit content up to 200px; that behavior is unchanged.

**Validation criteria**: Open `http://localhost:60889/#chat`. The chat input textarea is one line tall on initial render. Send button is 36×36px (inspect via DevTools — computed `height`/`width`). Typing several lines still grows the textarea up to 200px max. Speaker/mic icons in the toolbar remain present.

### 1.2 Introduce shared `.chip` class and migrate session-kind badges [category: refactor]

Target: `web/src/components/chat/styles/sessions-tab.css`, `web/src/components/activity/SessionsTab.tsx`, `web/src/components/chat/AgentStatusBar.tsx`

Promote the existing `.session-kind-badge` rules into a generic `.chip` + `.chip--<variant>` system so Tasks list (§1.3), the Sessions LOCAL variant (§1.5), and the agent-card LOCAL variant (§1.6) all share one CSS contract. Backwards-compatible aliases stay until §1.6 deletes them.

In `web/src/components/chat/styles/sessions-tab.css` (around line 271), refactor:

```css
/* shared chip base */
.chip {
  display: inline-flex;
  align-items: center;
  padding: 0 0.5rem;
  height: 1.25rem;
  font-size: 0.625rem;
  font-weight: 600;
  letter-spacing: 0.05em;
  border-radius: 9999px;
  text-transform: uppercase;
}

.chip--tmux  { background: color-mix(in srgb, #14b8a6 15%, transparent); color: #14b8a6; }
.chip--web   { background: color-mix(in srgb, #0284c7 15%, transparent); color: #0284c7; }
.chip--auto  { background: color-mix(in srgb, #f97316 15%, transparent); color: #f97316; }
.chip--sandbox { /* keep existing color */ }

/* TEMPORARY alias — deleted by §1.6 once all callers emit `chip chip--*` */
.session-kind-badge { /* same rules as .chip */ }
.session-kind-badge--tmux { /* same as .chip--tmux */ }
/* etc */
```

Update `SessionsTab.tsx` `getSessionTypeBadge` / `getSandboxBadge` / `getAgentBadge` (lines 75–99) and `renderBadges` (line 101) to emit `chip chip--<variant>` className strings. Update `AgentStatusBar.tsx` likewise.

**Validation criteria**: `gcode search-content "session-kind-badge"` returns no consumers in `.tsx` files (only the CSS aliases remain). Sessions tab still renders TMUX/WEB/SB chips identically (visual diff via Chrome DevTools screenshot before/after — pixel-equal). `chip` class is exported and ready for §1.3, §1.5, §1.6.

### 1.3 Convert Tasks list highlighted-text labels to chips [category: code] (depends: 1.2)

Target: `web/src/components/tasks/TaskTree.tsx`, `web/src/components/tasks/TaskBadges.tsx`, `web/src/components/tasks/task-execution.css`

The right-side state/priority/category labels on Tasks list rows currently render as plain highlighted text (visible in the screenshot for Drawbridge task `df52f9bf-f43a-43b7-9490-f2a5c266d997`: "Ready", "Medium", "refactor"). Convert these to `chip` instances using the class introduced in §1.2.

In `TaskBadges.tsx`:

- `StatusBadge` (lines 61–73) — already a chip-like span; change className to `chip chip--state-{status}` and add CSS rules for state variants (Ready=blue, In Progress=amber, Done=green, etc.) in `task-execution.css`.
- Add `PriorityBadge` and `TypeBadge` if they don't already export — if they do, similarly convert to `chip chip--priority-{n}` and `chip chip--type-{kind}`.

In `TaskTree.tsx` node renderer (around the row body), wrap the right-side label spans in the new chip components. Confirm the row layout still aligns (chips shouldn't push the title off-screen at narrow widths; test in DevTools mobile viewport at 375px).

**Validation criteria**: Tasks list rows render labels as rounded chips visually matching TMUX/WEB/SB. Hover/focus styling intact. No layout regression at 375px viewport.

### 1.4 Match Tasks list font size to Sessions list [category: code]

Target: `web/src/components/tasks/task-execution.css`

Drop the `0.85` multiplier on `.tree-node` font-size so Tasks list rows match the Sessions list at the default base size.

```css
.tree-node {
  /* was: font-size: calc(var(--font-size-base) * 0.85); */
  font-size: var(--font-size-base);
}
.tree-node-title { font-size: inherit; }
.tree-node-ref   { font-size: inherit; }
```

Re-test row height: a base-sized row in `.tree-node` may need vertical padding adjusted to match Sessions list row height. If Sessions list uses a specific row-height utility, mirror it in `.tree-node`. Compare side-by-side in DevTools.

**Validation criteria**: Open Activity panel → switch between Sessions tab and Tasks tab. Row text is the same size (DevTools computed `font-size` matches). Row vertical rhythm matches; no overlap or excessive whitespace.

### 1.5 LOCAL chip variant on Sessions tab rows [category: code] (depends: 1.2, 1.6)

Target: `web/src/components/activity/SessionsTab.tsx`, `web/src/components/chat/styles/sessions-tab.css`, plus the route that emits the watching-sessions payload (locate via `gcode search-content "watching_sessions"` / `WatchingSessionEntry` under `src/gobby/servers/routes/`).

**Hard dependency on §1.6**: §1.6 lands the schema migration adding `is_local` to `sessions` AND the spawn/session-create-time write that populates it. This task surfaces that column on the watching-sessions API and renders the chip.

User ask (separate from #11199): Sessions tab rows running against a daemon local model get a `LOCAL` chip alongside TMUX/WEB/SB.

**Why model-equality at the route was wrong (Round 3 F1):** comparing `session.model == daemon_config.local.model` produces:
- False positive: `local_cfg.model='claude-opus-4-7'` matches a real Claude cloud run.
- False negative: `local_cfg.model='auto'` resolves to a runtime-loaded id at spawn time; the literal `'auto'` is never persisted as `session.model`.

The fix is the §1.6 schema column populated at session-creation time. This section just reads it.

Backend change:

1. The watching-sessions route SELECT must include `sessions.is_local`. Map it to `is_local: bool` (default `false` when NULL — legacy rows; the §1.6 `is_local_model` helper provides best-effort fallback for legacy NULL via a SQL `COALESCE(is_local, ...)` expression or a Python-side fallback in the route serializer).

Frontend changes:

2. Add `isLocal: boolean` to `WatchingSessionEntry` (line ~42 of `SessionsTab.tsx`).
3. Add `getLocalBadge`:

   ```ts
   function getLocalBadge(entry: WatchingSessionEntry): { label: string; className: string } | null {
     return entry.isLocal ? { label: 'LOCAL', className: 'chip--local' } : null
   }
   ```

4. Add to `renderBadges`:

   ```ts
   const badges = [
     getSessionTypeBadge(entry.sessionType),
     getSandboxBadge(entry.sandboxEnabled),
     getAgentBadge(entry.agentRunId),
     getLocalBadge(entry),
   ].filter(...).sort(...)
   ```

5. CSS rule (added once, consumed by §1.6 too):

   ```css
   .chip--local {
     background: color-mix(in srgb, #8b5cf6 15%, transparent);
     color: #8b5cf6;
   }
   ```

   (Violet to differentiate from cyan TMUX and blue WEB.)

**Validation criteria**:
- New session started with `model: local` (any `local_cfg.model` including `auto`) writes `sessions.is_local=1`. `sqlite3 ~/.gobby/gobby-hub.db "SELECT id, model, is_local FROM sessions ORDER BY created_at DESC LIMIT 5"` confirms.
- Watching-sessions route returns `is_local: true` for that session and `is_local: false` for a Claude/Codex/Gemini cloud session.
- Frontend shows a violet `LOCAL` chip on local-session rows, NOT on cloud-session rows.
- Legacy session with `is_local=NULL` falls back through the helper: `provider='lmstudio'` legacy row → still shows LOCAL; `provider='claude', model='qwen-coder-32b'` legacy row → does NOT show LOCAL (the helper drops the model-equality precedence).

### 1.6 LOCAL chip on Workflows agent cards [category: code] (depends: 1.2)

Linked gobby-task: **#11199** ("Add Local chip to agent cards in web UI"). Claim and link this commit.

Target backend (verified file paths):
- `src/gobby/storage/_migration_registry.py` (add new MIGRATIONS tuple at version 221).
- `src/gobby/storage/baseline_schema.sql` (add `is_local INTEGER NOT NULL DEFAULT 0` to `CREATE TABLE agent_runs` line ~135 and `CREATE TABLE sessions` line ~175).
- `src/gobby/storage/agents.py` (`AgentRun` dataclass: add `is_local: bool = False`; `LocalAgentRunManager.create()`: add `is_local` parameter, INSERT column, value).
- `src/gobby/storage/session_models.py` (session dataclass: add `is_local: bool = False`).
- `src/gobby/storage/sessions/_crud.py` (INSERT column for `is_local`).
- `src/gobby/storage/sessions/_manager.py` (any high-level wrappers that pass through to `_crud`).
- `src/gobby/agents/spawn_executor.py` (`SpawnRequest` dataclass: add `is_local: bool = False`; verify `model: str | None = None` already exists or add it; each `_spawn_*_terminal` passes both through).
- `src/gobby/agents/spawn.py` (`prepare_terminal_spawn(...)`: add `model: str | None = None` and `is_local: bool = False`; pass `is_local` to `ChildSessionConfig`; pass `model, is_local` to `agent_run_mgr.create`).
- `src/gobby/agents/session.py` (`ChildSessionConfig` dataclass: add `is_local: bool = False`; `ChildSessionManager.create_child_session()` reads `config.is_local` and writes to the session row).
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` (compute `is_local_run = (effective_model == "local")` BEFORE `ensure_local_model`; populate `SpawnRequest.is_local`).
- `src/gobby/servers/routes/sessions/core.py` AND `src/gobby/servers/routes/agent_spawn.py` (web-chat path: compute `is_local = (requested_model == "local")` BEFORE local-model resolution; pass through to `create_web_chat_session`).
- `src/gobby/servers/routes/agents.py` (definitions route: emit `is_local` via `is_local_agent_definition` predicate; agent-runs route: SELECT `is_local` from `agent_runs`).
- `src/gobby/llm/local_detection.py` (NEW — `is_local_legacy_fallback` for row-level NULL fallback AND `is_local_agent_definition` for config-level cards).

Target frontend: `web/src/components/workflows/AgentsTab.tsx` (currently 1050 lines — see extraction caveat below) and any subcomponents that render an agent card.

**Why this is a schema-level fix, not a route-time helper (Round 3 F1):**

`model: local` is resolved into `local_cfg.model` (or, when `local_cfg.model='auto'`, into the runtime-loaded model id from `ensure_local_model`) at spawn time (`spawn_agent/_implementation.py`) and at session creation (`chat_session.py`). The persisted `agent_run.model` / `session.model` is therefore the resolved name, not `"local"`. Two failure modes for any route-time helper that compares `model == local_cfg.model`:

1. **False positive**: `local_cfg.model='claude-opus-4-7'` (any operator could choose this name) matches a real Claude cloud run with the same model name.
2. **False negative**: `local_cfg.model='auto'` causes `ensure_local_model` to return `loaded_ids[0]` (whatever LMStudio happens to have loaded). The literal `'auto'` is never persisted as the model name; the predicate finds nothing to match against.

The only authoritative signal is **the spawn caller knows whether `model: local` was requested**. We capture that bit at spawn/session-create time, before resolution, and persist it.

**Migration (step 0 — uses the canonical migration registration pattern):**

Migrations are registered as tuples in `MIGRATIONS: list[tuple[int, str, MigrationAction]]` in `src/gobby/storage/_migration_registry.py`. Existing precedent (the only entry post-baseline-flatten):

```python
MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    (
        220,
        "Add terminal_reason to agent_runs",
        """
        ALTER TABLE agent_runs ADD COLUMN terminal_reason TEXT
        """,
    )
]
```

Add a new entry at the next version (currently 221 — verify by reading `_migration_registry.py` on first edit; bump if a newer entry has landed):

```python
(
    221,
    "Add is_local to agent_runs and sessions",
    """
    ALTER TABLE agent_runs ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sessions   ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0;
    UPDATE agent_runs SET is_local = 1
     WHERE is_local = 0
       AND (
         LOWER(COALESCE(provider, '')) IN ('lmstudio', 'ollama', 'llamacpp', 'local')
         OR LOWER(COALESCE(model, '')) LIKE '%gpt-oss%'
       );
    UPDATE sessions SET is_local = 1
     WHERE is_local = 0
       AND (
         LOWER(COALESCE(provider, '')) IN ('lmstudio', 'ollama', 'llamacpp', 'local')
         OR LOWER(COALESCE(model, '')) LIKE '%gpt-oss%'
       );
    """,
),
```

(SQLite supports multi-statement strings via `executescript`; verify the `MigrationAction` runner uses `executescript` for `str` actions or split into a callable that runs them sequentially. Read `src/gobby/storage/migrations.py` for the runner implementation.)

PLUS update `src/gobby/storage/baseline_schema.sql` so fresh installs match:
- Add `is_local INTEGER NOT NULL DEFAULT 0` inside `CREATE TABLE agent_runs` (line ~135, before `terminal_reason`).
- Add `is_local INTEGER NOT NULL DEFAULT 0` inside `CREATE TABLE sessions` (line ~175).

Backfill rationale: conservative — no model-equality (fragile false-positive path). Legacy `provider=lmstudio` rows + legacy `gpt-oss-*` model rows get `is_local=1`; legacy local-via-Claude-SDK rows do NOT (acceptable: new sessions are correct from migration time forward; mention in PR).

**Spawn-time write (step 1) — threads through the real chain:**

The actual write chain for terminal-spawned agents is `spawn_agent_impl` (in `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`) → builds `SpawnRequest` → `execute_spawn(request)` (in `src/gobby/agents/spawn_executor.py`) → provider-specific `_spawn_*_terminal(request)` → `prepare_terminal_spawn(...)` (in `src/gobby/agents/spawn.py`) → `ChildSessionManager.create_child_session(ChildSessionConfig)` AND `LocalAgentRunManager.create(...)` (in `src/gobby/storage/agents.py`).

Threading required:

1.1 `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` — compute `is_local_run = (effective_model == "local")` BEFORE calling `ensure_local_model`. Pass it on the `SpawnRequest` along with the resolved model:

```python
is_local_run = (effective_model == "local")
if is_local_run:
    effective_model = await ensure_local_model(local_cfg, registry=registry)
request = SpawnRequest(
    ...,
    model=effective_model,
    is_local=is_local_run,
)
```

1.2 `src/gobby/agents/spawn_executor.py` — `SpawnRequest` dataclass: add `is_local: bool = False` (and confirm `model: str | None = None` already exists; if not, add it). Each `_spawn_*_terminal(request)` passes them through:

```python
prepared = prepare_terminal_spawn(
    ...,
    model=request.model,
    is_local=request.is_local,
    requested_reasoning_effort=request.requested_reasoning_effort,
    ...,
)
```

1.3 `src/gobby/agents/spawn.py` — `prepare_terminal_spawn` signature: add `model: str | None = None` and `is_local: bool = False` next to the existing reasoning-effort kwargs (lines ~100-105). In the `ChildSessionConfig(...)` construction (around line ~141), pass `is_local=is_local`. In the `agent_run_mgr.create(...)` call (around line ~178), pass `model=model, is_local=is_local`.

1.4 `src/gobby/agents/session.py` — `ChildSessionConfig` dataclass: add `is_local: bool = False` next to existing `sandbox_enabled: bool = False`. `ChildSessionManager.create_child_session(config)` reads `config.is_local` and includes it in the INSERT SQL.

1.5 `src/gobby/storage/agents.py` — `LocalAgentRunManager.create(...)`: add `is_local: bool = False` parameter, add `is_local` to the INSERT SQL columns and values list, add `is_local` to the `AgentRun` dataclass (`is_local: bool = False`) and to `from_row` and `to_dict` mappings. The existing `model: str | None = None` parameter already exists in the signature — `prepare_terminal_spawn` was simply not passing it; that's fixed at step 1.3.

**Session creation paths (step 1.6 — three distinct entrypoints):**

(a) **Terminal child sessions**: covered by step 1.4 above. `prepare_terminal_spawn` builds `ChildSessionConfig(is_local=is_local)`, which `create_child_session` writes.

(b) **Web chat sessions**: real path is `routes/sessions/core.py` (and `routes/agent_spawn.py`) calling `session_manager.create_web_chat_session(...)` (locate exact method via `gcode search "create_web_chat_session"`). Compute `is_local = (requested_model == "local")` from the route input BEFORE handing the model to local-model resolution. Add `is_local: bool = False` to `create_web_chat_session`'s signature and to the underlying CRUD insert in `src/gobby/storage/sessions/_crud.py`.

(c) **Hook auto-recovery sessions**: `SessionStartMixin.handle_session_start()` / `SessionLookupService._resolve_session_id()` create rows for sessions discovered after the fact (e.g., a Codex CLI session that wasn't spawned through the daemon). No spawn signal exists, so write `is_local=0` and rely on `is_local_legacy_fallback(provider, model)` at route serialization time. Document this explicitly in the route serializer: "auto-registered sessions default to `is_local=0`; falls back through `is_local_legacy_fallback` only if the column is `0`".

**`SessionModel` / storage models (step 1.7):**

The session storage is split across `src/gobby/storage/session_models.py` (dataclass), `src/gobby/storage/sessions/_crud.py` (INSERT/UPDATE), `src/gobby/storage/sessions/_manager.py` (high-level manager). Add `is_local: bool = False` to the dataclass, INSERT column, and any `from_row`/`to_dict` mappings. Read `_crud.py` for the existing INSERT shape on first edit.

**Heuristic helpers (step 2 — two separate predicates for different surfaces):**

```python
# src/gobby/llm/local_detection.py (new)
from __future__ import annotations
from typing import Optional

LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama", "llamacpp", "local"})

def is_local_legacy_fallback(provider: Optional[str], model: Optional[str]) -> bool:
    """Row-level NULL fallback for `is_local` on agent_runs/sessions.

    Used ONLY when the persisted `is_local` column is 0 AND we suspect the row
    pre-dates the migration. New rows persist `is_local` authoritatively at
    creation time (see spawn_agent/_implementation.py, prepare_terminal_spawn,
    and create_web_chat_session). Do NOT compare against `local_cfg.model` here —
    that path produces both false positives (cloud Claude run with
    local_cfg.model='claude-opus-4-7') and false negatives (local_cfg.model='auto'
    resolves to a runtime-loaded id at spawn time).

    Does NOT detect literal `model == "local"` because the resolution always
    happens before persistence — no row should have `model == "local"`.
    """
    if provider and provider.lower() in LOCAL_PROVIDERS:
        return True
    if model and "gpt-oss" in model.lower():
        return True
    return False


def is_local_agent_definition(provider: Optional[str], model: Optional[str]) -> bool:
    """Config-level local detection for agent DEFINITIONS (not runs).

    Used by the agent-definitions route (`src/gobby/servers/routes/agents.py`).
    Definitions store the user's literal `model` value (often `"local"`), so
    the literal-string detection IS the strongest signal here — unlike rows.

    Returns True when:
      1. `model == "local"` literally (the canonical config-side signal).
      2. Provider is in `LOCAL_PROVIDERS`.
      3. Model name matches the `gpt-oss-*` heuristic.
    """
    if model and model.strip().lower() == "local":
        return True
    if provider and provider.lower() in LOCAL_PROVIDERS:
        return True
    if model and "gpt-oss" in model.lower():
        return True
    return False
```

**Test coverage required (write before implementation):**

`tests/llm/test_local_detection.py`:
- `is_local_legacy_fallback("lmstudio", "anything")` → `True`.
- `is_local_legacy_fallback("openai", "gpt-oss-120b")` → `True`.
- `is_local_legacy_fallback("claude", "claude-opus-4-7")` → `False`.
- `is_local_legacy_fallback("claude", "qwen-coder-32b")` → `False` (false-positive guard).
- `is_local_legacy_fallback("claude", "local")` → `False` (resolution always happens before persistence; a row should never store the literal "local").
- `is_local_legacy_fallback(None, None)` → `False`.
- `is_local_agent_definition("claude", "local")` → `True` (literal-string IS the canonical config-side signal).
- `is_local_agent_definition("lmstudio", "qwen-coder-32b")` → `True` (provider).
- `is_local_agent_definition("openai", "gpt-oss-120b")` → `True` (heuristic).
- `is_local_agent_definition("claude", "claude-opus-4-7")` → `False`.
- `is_local_agent_definition(None, None)` → `False`.

`tests/mcp_proxy/tools/test_spawn_agent_local_persistence.py` (new): end-to-end spawn through `spawn_agent_impl` with `model="local"` (mock `ensure_local_model` to return `"qwen-coder-32b"`). Assert: the inserted `agent_runs` row has `is_local=1` AND `model="qwen-coder-32b"`; the inserted `sessions` row (created by `prepare_terminal_spawn` → `ChildSessionManager.create_child_session`) has `is_local=1`. Repeat with `model="local"` and `local_cfg.model="auto"` (mock `ensure_local_model` to return `"gpt-oss-120b"`); assert `is_local=1` AND `model="gpt-oss-120b"`. Repeat with `model="claude-opus-4-7"`; assert `is_local=0`.

`tests/agents/test_prepare_terminal_spawn_is_local.py` (new): unit test for `prepare_terminal_spawn(..., is_local=True, model="qwen-coder-32b")` — assert `ChildSessionConfig.is_local=True` is passed to `create_child_session`, and `LocalAgentRunManager.create` is called with `is_local=True, model="qwen-coder-32b"`.

`tests/servers/routes/test_web_chat_session_is_local.py` (new): POST to the web-chat session creation route with `model="local"` — assert the inserted `sessions` row has `is_local=1`. Repeat with `model="claude-opus-4-7"` — `is_local=0`.

`tests/storage/test_migration_221_is_local.py` (new): apply migration 221 to a DB with pre-migration `agent_runs` containing rows of `(provider='lmstudio', model='x')`, `(provider='claude', model='gpt-oss-120b')`, and `(provider='claude', model='claude-opus-4-7')`. Assert backfill flips the first two to `is_local=1` and leaves the third as `0`. Repeat for `sessions`.

Route changes (step 3):

3.1 **Agent-run / running-agents routes** (locate via `gcode search-content "running_agents"` under `src/gobby/servers/routes/`): SELECT `is_local` from `agent_runs`. Serializer maps to `is_local: bool` on the wire. When the column is `0`, apply `is_local_legacy_fallback(row.provider, row.model)` at the serializer to catch unmigrated legacy data (this is a no-op for fresh-install rows since the spawn-time write authoritatively sets `is_local`).

3.2 **Agent-definitions route** (`src/gobby/servers/routes/agents.py` — confirm exact path on first edit): definitions are NOT rows in `agent_runs`; they're config records loaded from `.gobby/agents/`. Their payload also gets `is_local: bool`, but computed via `is_local_agent_definition(def.provider, def.model)` — the predicate that DOES treat literal `model == "local"` as canonical (per the test cases above).

3.3 **Watching-sessions route** (consumed by §1.5): SELECT `sessions.is_local`, fall back via `is_local_legacy_fallback(row.provider, row.model)` when the column is `0`.

Frontend changes (step 4):

The `AgentDefInfo` interface near the top of `AgentsTab.tsx` plus the running-agent shape gets `isLocal: boolean`. In the agent-card JSX, render the `LOCAL` chip when `def.isLocal === true`. Use the same `chip chip--local` className introduced in §1.5 — no parallel CSS.

Extraction (in-scope for this commit — see Constraints):

5. `AgentsTab.tsx` is 1050 lines, over the CLAUDE.md 1000-line cap. Extract a single `AgentCard.tsx` row component as part of this commit: move the ~50–80 lines of card rendering JSX into a new file `web/src/components/workflows/AgentCard.tsx`, import it back into `AgentsTab.tsx`. The post-edit `AgentsTab.tsx` MUST be under 1000 lines. (Refactor task #12917 covers the broader monolith breakup — if it lands first, this extraction may already be done; verify before re-extracting.)

Wrap-up step (in this same commit, after the chip ships):

6. Delete the `.session-kind-badge*` aliases introduced in §1.2 from `web/src/components/chat/styles/sessions-tab.css`. Confirm `gcode search-content "session-kind-badge"` returns ZERO matches across the repo (TSX consumers were migrated in §1.2, §1.3, §1.5; this commit completes the cleanup).

**Validation criteria**:
- Migration 221 runs cleanly on existing dev DBs. Both `ALTER TABLE` statements succeed; backfill `UPDATE`s touch only matching rows.
- `baseline_schema.sql` for fresh installs includes `is_local INTEGER NOT NULL DEFAULT 0` on both `agent_runs` and `sessions`.
- All four new test files pass: spawn persistence (covers `auto` resolution + same-name false-positive), `prepare_terminal_spawn` threading, web-chat session creation, migration backfill.
- `tests/llm/test_local_detection.py` covers BOTH `is_local_legacy_fallback` and `is_local_agent_definition` (literal `model: "local"` returns True for definitions, False for the row-level fallback).
- Agent-runs route returns `is_local: true` for fresh local agent runs and `is_local: false` for cloud. Confirm with `curl`.
- Agent-definitions route returns `is_local: true` for any def with `model: "local"` literally. Confirm with `curl` against `/api/agents/definitions` (or whatever the actual path is).
- Watching-sessions route returns `is_local: true` for sessions started via `model: local`.
- Workflows tab → Agents shows a `LOCAL` chip on local-model agent cards (both running agents AND defs with `model: local`). Cloud agents do NOT show the chip.
- `wc -l web/src/components/workflows/AgentsTab.tsx` returns < 1000.
- `wc -l web/src/components/workflows/AgentCard.tsx` returns > 0 (extraction landed).
- `gcode search-content "session-kind-badge"` returns 0 matches anywhere in the repo (CSS aliases deleted, no TSX consumers).
- gobby-task #11199 closed via this commit.

---

## Phase 2: Dashboard cards

**Goal**: Surface non-Claude usage by fixing source/model attribution, fix the Token Efficiency chart's misleading hover values, and remove the redundant granularity selector.

### 2.1 Fix `by_source` / `by_model` attribution so non-Claude usage appears [category: code]

Target: `src/gobby/sessions/token_tracker.py` (the `SessionTokenTracker.get_usage_summary()` aggregator that populates `usage_by_source` / `usage_by_model`), `src/gobby/servers/routes/admin/_usage.py` (the `/api/admin/usage` route), plus whichever transcript/lifecycle code path attributes the per-session source and model.

**Live diagnostic** (curl against the running daemon):

```text
$ curl -s http://localhost:60887/api/admin/usage | jq '.by_source, .by_model'
{
  "claude": { "input_tokens": 823092, "output_tokens": 33727353, "cache_read_tokens": 16545761935, "cache_creation_tokens": 792847992, "session_count": 1119 }
}
{
  "claude-opus-4-7": { ... },
  "unknown": { "input_tokens": ... }
}
```

Despite `sessions` table showing 512 codex + 91 gemini sessions:

```text
$ sqlite3 ~/.gobby/gobby-hub.db "SELECT source, COUNT(*) FROM sessions GROUP BY source"
claude|1215
codex|512
cron|1
gemini|91
pipeline|37
qwen|1
system|1
```

The `by_source` aggregation only emits `claude`, AND the `by_model` aggregation has an `unknown` bucket — meaning attribution is partially happening but mapping non-Claude sources to their proper labels is broken. The previous draft's "Codex `_parse_message` returns `usage=None`" hypothesis was **wrong**: `CodexTranscriptParser._parse_event_msg` (codex.py:271) emits a `ParsedMessage` with `TokenUsage` for `event_msg` `token_count` payloads, and `lifecycle.py` records any message with `usage` present.

Implementation:

1. **Pin the actual point of failure** by writing a failing end-to-end test before any fix:
   - Test path: `tests/sessions/test_token_tracker_attribution.py` (new file).
   - Fixture: a Codex `event_msg` `token_count` line and a Gemini usage line, fed through `SessionLifecycleManager._process_session_transcript` against an in-memory DB.
   - Assertion: `SessionTokenTracker.get_usage_summary()` returns `usage_by_source["codex"]` non-zero and `usage_by_model["gpt-5.2-codex"]` (or whatever model the fixture uses) non-zero.
   - Expected: this test FAILS before the fix.

2. **Trace the failure** to one of:
   - The `sessions.source` column not propagating to `token_events` rows (re-read `lifecycle.py:541` — confirm `session_source` actually reads the right column).
   - `SessionTokenTracker.get_usage_summary()` filtering or normalizing source values (e.g., grouping all non-`claude` rows into something hidden).
   - Model name attribution being dropped at the parser → TokenEvent boundary (Codex models stored as NULL → "unknown" in the route).
   - Backfill issue: token_events table actually has no codex rows (current data: `SELECT source, COUNT(*) FROM token_events GROUP BY source` returns `claude|9364`). Confirm whether real Codex sessions at runtime DO emit token_events but the existing data is from a pre-fix era — if so, the fix lands forward-only and the validation criteria below will exercise it on a fresh Codex session.

3. **Apply the narrow fix** indicated by step 2. Most likely one of:
   - Wire missing source/model fields in the lifecycle insertion path.
   - Remove a `WHERE source = 'claude'` (or equivalent) filter in the aggregator.
   - Ensure Codex/Gemini sessions trigger transcript processing at all (verify the lifecycle actually picks them up — check `_process_session_transcript`'s source dispatch).

4. **Backfill is OUT OF SCOPE** for this commit. The fix must work for new sessions; existing pre-fix data stays as-is. Note this in the PR description.

**Validation criteria**:
- Failing test from step 1 turns green after the fix.
- Run a new Codex session against any cloud GPT model. After it completes a turn, `curl -s http://localhost:60887/api/admin/usage | jq '.by_source'` includes `codex` with non-zero tokens.
- `sqlite3 ~/.gobby/gobby-hub.db "SELECT source, model, SUM(input_tokens) FROM token_events GROUP BY source, model"` shows non-zero rows for `codex` (and `gemini` if a Gemini session ran), with model names attributed (no `unknown` for new rows).
- DevTools: dashboard `UsageCard.tsx` lists at least one non-Claude row (e.g. `gpt-5.4`, `gpt-5.2-codex`).

### 2.2 Fix Token Efficiency chart green-tooltip-shows-0 bug [category: code]

Target: `web/src/components/dashboard/TokenEfficiencyCard.tsx`

`TokenEfficiencyCard.tsx` lines 148–163 stack two `<Area>`s with `stackId="tokens"`. Recharts stacked Areas report stacked-endpoint values in tooltip payload, so the upper series ("Tokens Saved", green) shows the offset (often 0 in idle buckets) instead of its true magnitude.

Fix: drop `stackId="tokens"` from both Areas so they render independently. Saved and Spent are not additive halves of one whole; overlapping rendering is more honest.

```tsx
<Area
  type="monotone"
  dataKey="tokens_spent"
  name="tokens_spent"
  stroke="#3b82f6"
  fill="rgba(59,130,246,0.2)"
/>
<Area
  type="monotone"
  dataKey="tokens_saved"
  name="tokens_saved"
  stroke="#22c55e"
  fill="rgba(34,197,94,0.2)"
/>
```

Optional follow-up if visual overlap looks bad: switch the lower series to `<Line>` instead of `<Area>` so the upper Area sits cleanly on top of a baseline line. Defer to Chrome DevTools verification.

**Validation criteria**: Hover over green spikes in the chart. Tooltip "Saved" value matches the visible spike magnitude (non-zero where the line is non-zero). Cross-check by comparing the tooltip value to a `tokens_saved` value from the live `/api/admin/tokens/timeseries` Network response (e.g. `curl -s 'http://localhost:60887/api/admin/tokens/timeseries?hours=24&granularity=1h' | jq '.buckets[0]'`) — they should match for the same bucket timestamp.

### 2.3 Remove GranularityToggle; derive granularity from hours [category: refactor]

Target: `web/src/components/dashboard/TokenEfficiencyCard.tsx`, `web/src/components/dashboard/GranularityToggle.tsx` (delete)

The 30m/1h/1d granularity selector is redundant with the page-level `TimeRangePills` (1h/6h/12h/24h/7d/30d/All). Derive granularity from the `hours` prop and delete the local toggle.

Edits to `TokenEfficiencyCard.tsx`:

- Remove `useState` for granularity (line 79).
- Remove `import { GranularityToggle }` (line 18).
- Compute granularity inline: `const granularity: TimeSeriesGranularity = hours <= 6 ? '30m' : hours <= 168 ? '1h' : '1d'`.
- Remove the `<GranularityToggle ... />` element from the `action` prop (lines 113–116).
- The `tokenEventsEnabled && modelBreakdown.length > 0` block at line 179 stays — it gates the per-model breakdown, not the toggle.

After this, `gcode search-content "GranularityToggle"` returns only the file itself. Delete `web/src/components/dashboard/GranularityToggle.tsx`.

**Validation criteria**: GranularityToggle file is deleted (`ls` confirms). `gcode search "GranularityToggle"` returns no usages. Page-level `TimeRangePills` still works; switching from 1h → 7d → All re-buckets the chart sensibly without a separate selector. No console errors.

---

## Phase 3: Watching panel + transcript

**Goal**: Visual fixes plus the Codex Protocol-tag leak (with a real fixture) and the wrap/scroll-on-click UX issues in the Watching panel.

### 3.1 Sessions filter dropdown collapsed-trigger blue [category: code]

Target: `web/src/components/activity/ActivityPanel.tsx` (the dropdown trigger that switches between "Sessions / Tasks / Plans / Changes / Files / A2UI / Canvas / Pipelines" — its accessible label is `Tasks ▼` per the live page snapshot at uid `_313`). If the trigger styling lives in CSS rather than only Tailwind classes, also touch `web/src/components/chat/styles/activity-panel.css` (or whichever stylesheet `ActivityPanel.tsx` imports for the trigger).

Match the project-selector active-state blue (`bg-accent text-accent-foreground` from `web/src/components/chat/ProjectSelector.tsx:91`) on the COLLAPSED dropdown trigger button only. Expanded menu items keep their current styling.

Apply `bg-accent text-accent-foreground` to the trigger's className in `ActivityPanel.tsx`. Confirm by inspecting the project selector's "gobby" pill styling in DevTools, then matching the same hex/CSS-var on the dropdown trigger.

**Validation criteria**: Collapsed Sessions filter dropdown trigger has the same blue background and text color as the `ProjectSelector.tsx` "gobby" active pill (DevTools computed `background-color` matches). Open the dropdown — menu items display in their previous (non-blue) style. Switch the filter to "Tasks" — the trigger remains blue.

### 3.2 Codex transcript Protocol-tag leak — capture fixture, fix at `splitProtocolContent` [category: code]

Targets:
- `web/src/components/chat/protocolContent.ts` — primary: this is where `splitProtocolContent` extracts `<system_instructions>…</system_instructions>` blocks from raw text into the collapsed Protocol segments. The leak means the splitter doesn't match this body.
- `web/src/components/chat/Markdown.tsx` — secondary: the `blockProtocolTagRe` here is the fallback for any leftover text that bypasses `splitProtocolContent`. May or may not need updating depending on diagnosis.
- `src/gobby/sessions/transcript_renderer.py` — backend mirror: server-side renderer has its own `_PROTOCOL_TOOL_TAGS` and pattern. If the leak is rooted in tag-naming or pattern mismatch, this file may need a parallel fix to keep frontend/backend rendering consistent. Flag for review during implementation; only edit if necessary.

User-visible symptom: Codex sessions in the Watching panel render `<system_instructions>`-style content as raw markdown text outside the collapsed "Protocol" block (live-confirmed via Chrome DevTools MCP at viewport 375×800, screenshot for Drawbridge task `e49d8f0c-9907-43f0-9728-73a2c24f1ee3`).

**Capture the offending body as a committed fixture (mandatory first step):**

1. Read `~/.codex/sessions/2026/04/24/rollout-2026-04-24T01-49-53-019dbe40-89d2-7e73-a654-326b775e6035.jsonl` (confirmed contains `<permissions instructions>`, `<INSTRUCTIONS>`, and `skills_instructions`).
2. Extract the full content of the offending `developer`-role message that produces the leak. Use `jq` to filter `select(.payload.role == "developer")` and pull the `content[].text` body.
3. Commit the body as `web/src/components/chat/__tests__/fixtures/codex-protocol-leak.txt` (the file becomes the test fixture for steps below — DO NOT skip; the plan author confirmed the JSONL exists locally but it is not in the repo, so without this commit the bug is not reproducible).

**Diagnose and fix:**

4. Write a failing Vitest in `web/src/components/chat/__tests__/protocolContent.test.ts`. The actual exported API is `splitProtocolContent(content: string, idPrefix: string): ProtocolContentSegment[]` where each segment is either `{ type: 'text', content: string }` or `{ type: 'tool_call', call: ToolCall }`, and protocol-derived tool calls have `call.tool_type === 'protocol'` (see `makeProtocolToolCall` in `protocolContent.ts:126-151`). Use `Extract<...>` for clean type narrowing — an inline shape predicate like `s is { type: 'tool_call'; call: { tool_type: string } }` won't typecheck because `ToolCall` requires `id`, `tool_name`, `server_name`, and `status`:

   ```ts
   import { describe, it, expect } from 'vitest'
   import { splitProtocolContent, type ProtocolContentSegment } from '../protocolContent'
   import fixture from './fixtures/codex-protocol-leak.txt?raw'

   type TextSegment = Extract<ProtocolContentSegment, { type: 'text' }>
   type ToolCallSegment = Extract<ProtocolContentSegment, { type: 'tool_call' }>

   describe('splitProtocolContent — Codex leak regression', () => {
     it('collapses leaked Codex system_instructions into protocol tool calls', () => {
       const segments = splitProtocolContent(fixture, 'codex-protocol-leak')

       const visible = segments
         .filter((s): s is TextSegment => s.type === 'text')
         .map(s => s.content)
         .join('\n')

       const protocolCalls = segments.filter(
         (s): s is ToolCallSegment =>
           s.type === 'tool_call' && s.call.tool_type === 'protocol',
       )

       // Body contains "request_user_input availability" only inside the protocol block;
       // visible text must not contain that phrase post-fix.
       expect(visible).not.toContain('request_user_input availability')
       expect(protocolCalls.length).toBeGreaterThanOrEqual(1)
     })
   })
   ```

   The `?raw` import works without any config changes: `web/src/vite-env.d.ts` references `vite/client`, and Vitest runs through `web/vite.config.ts`. No `assetsInclude` needed.
5. Run the test — it should fail. The error message tells you whether the leak originates in `splitProtocolContent` (most likely — the visible text contains the protocol body) or somewhere downstream. If `splitProtocolContent` is the culprit:
   - The non-greedy regex in the splitter terminates early when the body contains a triple-backtick code fence with `</tag>` text inside, OR when nested same-name tags appear.
   - Switch the offending regex to a balanced tokenizer: walk forward from each `<tag…>` opening, count `<tag>` / `</tag>` depth (ignoring text inside `` `…` `` inline code and triple-backtick fences), close on depth 0.
6. If the leak is downstream (i.e. `splitProtocolContent` returns the right segments but `Markdown.tsx` re-renders the protocol body as visible text), apply the same balanced-tokenizer fix to `blockProtocolTagRe` in `Markdown.tsx`.
7. Audit `src/gobby/sessions/transcript_renderer.py` `_PROTOCOL_TOOL_TAG_PATTERN` for the same vulnerability. If the backend renderer has the equivalent regex and the same issue, fix it in parallel (separate logical commit if it touches Python — keep frontend and backend consistent).

**Validation criteria**:
- New fixture file `web/src/components/chat/__tests__/fixtures/codex-protocol-leak.txt` exists.
- Vitest in `protocolContent.test.ts` (and `Markdown.test.tsx` if §3.2.6 fired) passes — visible text does not leak protocol content.
- Open a Codex session (use one from `~/.codex/sessions/2026/04/24/`) in the Watching panel via Chrome DevTools at `http://localhost:60889/#chat`. The `system_instructions` / `permissions instructions` content stays inside the collapsed "Protocol" block. Expanding the block reveals the content; collapsing hides it.
- If §3.2.7 fired, backend renderer test (e.g. `tests/sessions/test_transcript_renderer.py`) also asserts the captured body produces a single tool_chain block.

### 3.3 Wrap tool-call args/result instead of horizontal scroll [category: code]

Target: `web/src/components/chat/ToolCallCard.tsx` (currently 1008 lines — see extraction caveat below) and `web/src/components/chat/UnknownBlockCard.tsx`.

Find the `<pre>` blocks that render tool-call args and result JSON. Per `gcode search-content "overflow-x"`, both files have `overflow-x` on JSON-display blocks. Replace `overflow-x-auto` with `whitespace-pre-wrap break-words` Tailwind utilities (or `break-all` if URLs/long identifiers overflow). Keep the `<pre>` tag for monospace and indentation.

Don't strip `overflow-x` from layout containers (panels, tabs); only from JSON/code display blocks.

Extraction (in-scope for this commit — see Constraints):

`ToolCallCard.tsx` is 1008 lines, just over the CLAUDE.md 1000-line cap. The implementing agent extracts a `JsonBlock` component (the `<pre>` + className logic for args/result JSON; ~30–60 lines including any related copy-button or syntax-highlight wrapper) into a new file `web/src/components/chat/JsonBlock.tsx` as part of the same commit. The extraction must bring `ToolCallCard.tsx` below 1000 lines. (Refactor task #12917 covers the broader monolith breakup — if it lands first, this extraction may already be done; verify before re-extracting.)

**Validation criteria**:
- Open a tool call with long args/result JSON (e.g. a `gobby-tasks.list_tasks` call with a long response). No horizontal scrollbar appears in the args/result block. Long lines wrap.
- Resize the activity panel narrow (DevTools responsive mode at 600px width) — wrap holds.
- `gcode search-content "overflow-x"` in `ToolCallCard.tsx` returns no matches in JSON-display contexts.
- `wc -l web/src/components/chat/ToolCallCard.tsx` returns < 1000.

### 3.4 Scroll Watching panel to bottom on session click [category: code]

Target: `web/src/components/activity/SessionsTab.tsx`

The auto-scroll `useEffect` (around lines 462–469) currently fires only on `[chatMessages.length, contentMode]` changes. When the user clicks a different session in the sidebar, `chatMessages` changes asynchronously (after the new session's transcript loads) and the effect may not scroll for the user's expectation.

Fix: add the watched session id to the dependency array so the effect fires the moment the user picks a new session, and use `behavior: "auto"` for instant jump (smooth-scroll across a long transcript is slow):

```ts
useEffect(() => {
  if (contentMode === "transcript") {
    const messagesEnd = messagesEndRef.current;
    if (messagesEnd?.scrollIntoView) {
      messagesEnd.scrollIntoView({ behavior: "auto" });
    }
  }
}, [chatMessages.length, contentMode, watchedSessionId]); // add watchedSessionId
```

`watchedSessionId` is whatever prop or state holds the currently-watched session — confirm by reading `SessionsTabProps` lines 26–36 and use the actual prop name (likely `selectedSessionId` or similar).

**Validation criteria**: Click between several sessions in the sidebar. Each click jumps the Watching panel to the most recent message instantly. Scroll up in a session, click a different session, click back — landing on the most recent message both times. Streaming new messages still scrolls to bottom (the `chatMessages.length` dep still fires).

---

## Phase 4: Mobile activity panel auto-close

**Goal**: Fix the user-reported bug where clicking the activity-panel toggle on mobile opens the panel for ~6ms then auto-closes it. Phase 4 is sequenced after Phase 3 (the dependency lives on §4.1 below, where plan-draft compilation actually honors it).

### 4.1 Scope `ChatPage.tsx` mobile auto-close to viewport transitions only [category: code] (depends: Phase 3)

Target: `web/src/components/chat/ChatPage.tsx` (specifically lines 169–172 — the `useEffect` that auto-closes the panel on every isPinned change while isMobile is true).

User-reported bug: "the activity panel is flickering and disappearing when i click the button on the chat ui — but only on mobile."

**Diagnosis confirmed (Round 1 adversary + live verification):**

`web/src/components/chat/ChatPage.tsx:169-172` contains:

```tsx
useEffect(() => {
  if (isMobile && isPinned) {
    setIsPinned(false);
  }
}, [isMobile, isPinned, setIsPinned]);
```

Because `isPinned` is in the deps, the effect fires every time the user toggles `isPinned` to true on mobile, immediately setting it back to false. This matches the live MutationObserver evidence from Chrome DevTools at viewport 375×800:

```text
t=0      ms  "Show activity panel"   (closed)
t=64879  ms  "Hide activity panel"   (toggle ran — opened)
t=64885  ms  "Show activity panel"   (effect fired — closed again, 5.8ms later)
```

The intent of the effect is "auto-close the panel when the viewport shrinks from desktop to mobile" — which only needs to fire on the `isMobile` *transition*, not on every `isPinned` change. The previous draft's "click-outside racing" theory was wrong; the only `mousedown` listener in `ActivityPanel.tsx` is for the mobile tab menu, not the panel itself.

**Important — preserve intentional auto-closes elsewhere:**

`ChatPage.tsx` also contains intentional `if (isMobile && isPinned) setIsPinned(false)` calls at lines ~201–210 and ~618–628. Those are inside callbacks (e.g., after attaching to a session, after approving a plan) — those should auto-close on mobile. Do NOT touch those callbacks. Only the standalone `useEffect` at lines 169–172 is wrong.

Fix: gate the auto-close on the desktop→mobile transition by tracking previous `isMobile` via a ref. Read `isPinned` via ref so it's not in the effect's deps:

```tsx
const prevIsMobileRef = useRef(isMobile);
const isPinnedRef = useRef(isPinned);
useEffect(() => {
  isPinnedRef.current = isPinned;
}, [isPinned]);

useEffect(() => {
  // Auto-close the pinned panel only on the desktop → mobile transition,
  // not on every user-initiated isPinned change.
  if (!prevIsMobileRef.current && isMobile && isPinnedRef.current) {
    setIsPinned(false);
  }
  prevIsMobileRef.current = isMobile;
}, [isMobile, setIsPinned]);
```

**Test target — must mount the real cascade:**

The integration test must mount enough of `ChatPage` to reproduce the cascade. The existing `ChatPage.test.tsx` mocks `useActivityPanel` like this:

```ts
vi.mock('../../activity/useActivityPanel', () => ({
  useActivityPanel: () => ({
    activeTab: 'artifacts',
    closeIfAutoOpened: vi.fn(),
    isPinned: false,
    panelWidth: 320,
    setActiveTab: vi.fn(),
    setIsPinned: vi.fn(),     // bare spy — never flips state
    setPanelWidth: vi.fn(),
    showTab: vi.fn(),
    // togglePanel missing entirely
  }),
}))
```

That mock is unfit for purpose for §4.1: a click on the toggle calls a no-op `setIsPinned`, `isPinned` never re-renders to `true`, the `useEffect` at line 169-172 never runs against `isPinned=true`, and the bug's cascade is unobservable. The new tests need stateful behavior. Two viable patterns — pick whichever the implementing agent finds cleaner during the rewrite:

**Option A — stateful mock backed by real `useState`:**

Wrap the existing mock in a closure that holds React state. Apply this mock only to the new `describe('ChatPage mobile auto-close', ...)` block via `vi.mock` + `vi.unmock` or a separate test file (e.g., `ChatPage.mobile.test.tsx`) so the existing tests keep their bare-spy mock:

```ts
import { useState, useCallback } from 'react'
// ...
vi.mock('../../activity/useActivityPanel', () => ({
  useActivityPanel: () => {
    const [isPinned, setIsPinned] = useState(false)
    const togglePanel = useCallback(() => setIsPinned(prev => !prev), [])
    return {
      activeTab: 'artifacts',
      closeIfAutoOpened: vi.fn(),
      isPinned,
      panelWidth: 320,
      setActiveTab: vi.fn(),
      setIsPinned,
      setPanelWidth: vi.fn(),
      showTab: vi.fn(),
      togglePanel,
    }
  },
}))
```

The mocked `AgentStatusBar` (or the real one if not mocked elsewhere) MUST call `togglePanel` — verify by reading the actual `AgentStatusBar` toggle handler. Whichever it calls (`togglePanel` vs `setIsPinned(prev => !prev)`), the stateful mock supports both.

**Option B — use the real `useActivityPanel` hook:**

`vi.unmock('../../activity/useActivityPanel')` at the top of the new describe block, then mount `ChatPage` normally. The real hook initializes `isPinned` from `localStorage` / `window.innerWidth >= 1100`; force `isPinned=false` initial state by stubbing `localStorage.getItem(STORAGE_KEY_PINNED)` → `'false'` and `window.innerWidth` → `375` before render. This is more faithful to production but requires more setup.

**Required test cases:**

```ts
describe('ChatPage mobile auto-close', () => {
  it('does NOT auto-close when user toggles pin on mobile', async () => {
    // Mock useIsMobile -> true
    // Use stateful useActivityPanel mock (Option A)
    render(<ChatPage {...props} />)
    const toggle = screen.getByRole('button', { name: /show activity panel/i })
    await userEvent.click(toggle)
    // Wait one tick + a small buffer for the effect to (incorrectly) re-fire
    await waitFor(
      () => expect(screen.getByRole('button', { name: /hide activity panel/i })).toBeInTheDocument(),
      { timeout: 100 },
    )
    // Confirm it stays — no second flip back to "Show"
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getByRole('button', { name: /hide activity panel/i })).toBeInTheDocument()
  })

  it('DOES auto-close on desktop -> mobile transition with panel pinned', async () => {
    // Start: useIsMobile=false, useActivityPanel state isPinned=true
    // Re-render with: useIsMobile=true (mock the hook to flip its return value)
    // Assert: button label flips to "Show activity panel"
  })
})
```

Read the real `AgentStatusBar` component first (`gcode outline web/src/components/chat/AgentStatusBar.tsx` then `gcode symbol <toggle handler>`) to confirm the exact `aria-label` strings and which prop the toggle calls (`onTogglePanel`, `togglePanel`, etc.). Match those in the mock.

**Validation criteria**:
- Open `http://localhost:60889/#chat` in Chrome DevTools mobile emulation (viewport `375x800x2,mobile,touch`, `isMobile=true`).
- Click the "Show activity panel" button in the chat input area. The button label flips to "Hide activity panel" and stays. Panel becomes visible and stays.
- Click again → label flips back to "Show activity panel", panel hides cleanly.
- Repeat 5× — no flicker, no disappearance.
- Re-run the diagnostic MutationObserver script (the one used to confirm the bug originally): timeline shows 1 flip per click, not 2.
- Resize browser from desktop (1100px) to mobile (375px) with the panel pinned — the panel auto-closes (intentional behavior preserved).
- New `ChatPage.test.tsx` `it()` blocks pass.
- Same manual test at 768px viewport (tablet) — no regression.

---

## Task Mapping

| Plan Item | Drawbridge ID / Gobby ref | Status |
|-----------|---------------------------|--------|
| 1.1 | drawbridge `9533…2d63` (Make textarea one line, smaller send) | open |
| 1.2 | (refactor — no Drawbridge ref) | open |
| 1.3 | drawbridge `df52…d997` (chips not highlighted text) | open |
| 1.4 | drawbridge `8651…d6ac` (font size match Sessions) | open |
| 1.5 | (user ask — Sessions tab LOCAL chip; no Drawbridge ref) — depends on §1.6 schema | open |
| 1.6 | gobby-task **#11199** (LOCAL chip on agent cards) — schema migration + spawn-time `is_local` write + `AgentCard.tsx` extraction in same commit | open |
| 2.1 | drawbridge `82e5…1604` (usage stats only Claude — attribution fix) | open |
| 2.2 | drawbridge `dd0a…1b28` (chart hover green=0) | open |
| 2.3 | drawbridge `e53b…f561` (granularity tied to time range) | open |
| 3.1 | drawbridge `ef1c…df97` (dropdown blue) | open |
| 3.2 | drawbridge `e49d…1ee3` (Codex Protocol leak — fixture-driven) | open |
| 3.3 | drawbridge `70c1…4d80` (wrap not scroll) — extracts `JsonBlock.tsx` in same commit | open |
| 3.4 | drawbridge `aab7…ac30` (scroll bottom on session click) | open |
| 4.1 | (user-reported, no Drawbridge ref) — `ChatPage.tsx:169-172` fix | open |

## Verification Checklist

```text
Plan Verification:
✓ Test specifications are explicit (Vitest in §3.2, ChatPage integration tests in §4.1, pytest in §1.6 — including end-to-end spawn persistence tests, lifecycle test in §2.1) — TDD wrappers will be auto-inserted around each
✓ Dependency tree is valid:
    - 1.3 depends on 1.2 (chip class)
    - 1.5 depends on 1.2 (chip class) AND 1.6 (sessions.is_local schema column + spawn-time write)
    - 1.6 depends on 1.2 (chip class)
    - 4.1 depends on Phase 3 (sequencing — declared on the task heading, not the phase heading, since plan-draft compilation only honors deps on `### N.N` headings)
    - No cycles
✓ All `(depends:)` references are to in-plan task numbers or phase sub-epics — no external task IDs in plan-draft notation, no phase-heading deps
✓ File-size compliance handled in-section: §1.6 extracts `AgentCard.tsx`, §3.3 extracts `JsonBlock.tsx`. Both bring their host file under 1000 lines in the same commit. Refactor task #12917 referenced as prose context only.
✓ Schema-level fix in §1.6: `is_local` column added to `agent_runs` and `sessions` via `_migration_registry.py` (version 221) AND `baseline_schema.sql` (for fresh installs). Computed at spawn/session-create time BEFORE local-model resolution, threaded through `spawn_agent_impl` → `SpawnRequest` → `execute_spawn` → `_spawn_*_terminal` → `prepare_terminal_spawn` → `ChildSessionConfig` + `LocalAgentRunManager.create`. Web-chat path threads through `routes/sessions/core.py` + `routes/agent_spawn.py` → `create_web_chat_session`. Hook auto-recovery sessions default to `is_local=0` + legacy fallback at read.
✓ Two predicates in `local_detection.py`: `is_local_legacy_fallback(provider, model)` for row-level NULL fallback (does NOT match literal `"local"` since rows persist resolved names); `is_local_agent_definition(provider, model)` for config-side cards (DOES match literal `model: "local"` as the canonical signal).
✓ Categories assigned correctly (code/refactor only)
✓ Phase headings use canonical `## Phase N: Name` form (no inline dep notation)
✓ Task sections are self-contained: every section names exact files, line numbers, code snippets, and validation steps
```

Ready for review.
