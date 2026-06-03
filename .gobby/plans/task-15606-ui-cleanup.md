# Activity Panels: Verification & Fix Plan

## Context

The user asked for a Chrome-DevTools-driven audit of four Gobby activity panels, with
fixes planned for whatever is broken:

1. **Plans** — every supported CLI should be able to request a plan, have it appear in
   the Plans panel for approval, reject it, get an updated plan, approve it, and have the
   chat **mode automatically switch out of Plan**.
2. **A2UI Canvas** — verify it works.
3. **Changes** — must work for live web-chat sessions, tmux view/attach sessions, and
   resumed sessions; switching sessions must switch the Changes contents.
4. **Artifacts** — likely redundant with Files/Changes; remove unless a valid use case
   exists.

Verification method: two background subagents driving **separate browsers** (chrome-devtools
MCP → Claude/Codex/Droid; playwright MCP → Gemini/Grok/Qwen + A2UI), each creating its own
Plan-mode web-chat sessions and reporting a pass/fail matrix, plus direct read-only code
analysis. Subagents isolate the huge-transcript snapshot cost out of the planning context.

> Live matrix from the two browser subagents is folded into §1/§2 below once they report;
> the code-level root causes are already confirmed and drive the fixes.

---

## §1 Plans panel & cross-CLI plan approval — BROKEN for non-Claude CLIs

### Findings
- **LIVE RESULT contradicts the code-only prediction: the Plans panel populates for NOBODY
  in web chat — not even Claude.** In the chrome-devtools run, Claude and Codex both
  presented the plan as **inline chat text** ("Please approve this plan…") with the Plans
  tab stuck on its empty state and **no Approve/Reject controls anywhere**. So the whole
  plan-capture → Plans-panel-card → approve/reject pipeline is non-functional for managed
  web chat. The Claude SDK `ExitPlanMode → plan_pending_approval` broadcast that exists in
  `chat_session.py` is **not being triggered** in Gobby's injected-plan-mode web flow (the
  agent emits a normal assistant turn instead of an `ExitPlanMode` tool call), and/or the
  broadcast never renders in the Plans panel.
- **Claude (SDK) has the only *partial* plumbing.** `chat_session.py` can block
  `ExitPlanMode`, broadcast `plan_pending_approval`, and on approve call
  `sync_sdk_permission_mode()` → `client.set_permission_mode(...)`. But per the live result
  this path is not exercised by the managed web-chat plan flow — fixing the trigger is step 1.
- **All ACP web-chat CLIs (Codex, Droid, Gemini, Grok, Qwen) are stubbed:**
  - `src/gobby/servers/websocket/chat/acp_permissions.py:176-177` —
    `async def sync_sdk_permission_mode(self): pass` (no-op).
  - `:92-94` — `has_pending_plan` always `False` (no ExitPlanMode-style gate).
  - `:88-90` — `provide_plan_decision` only sets a soft `_plan_approved` flag.
  - `:100-106` — on approval the agent is merely *told* "approved but still in PLAN MODE…
    do not execute until switched to YOLO". No automatic mode switch.
  - There is **no `_on_plan_ready` / `plan_pending_approval` broadcast** in the ACP path,
    so a plan presented by an ACP CLI never populates the Plans panel.
- **Live confirmation:** a real Codex (tmux) session that produced a full plan showed the
  Plans panel empty ("Plans appear here when the agent proposes one for review") — the plan
  only rendered inline in the transcript. (tmux is a separate watch path, but it
  corroborates that plan capture is Claude/SDK-specific.)

### Live web-chat matrix
| Provider | plan in Plans panel | approve/reject controls | notes |
|---|---|---|---|
| Claude | ❌ no | ❌ no | plan shown inline only; Plans tab stayed empty |
| Codex  | ❌ no | ❌ no | plan shown inline only (fast); Plans tab empty |
| Droid  | ⚠️ n/a | n/a | auth failed in that run (pre-re-auth); re-test now that it's authenticated |
| Gemini | ❌ no | ❌ no | plan shown inline only; Plans tab empty |
| Grok   | ❌ no | ❌ no | plan shown inline only; Plans tab empty (also exercised §5.1 readline on a large message) |
| Qwen   | ❌ no | ❌ no | warmup 401'd (§5.3); with a valid LM Studio token the GET returns 200 + the qwen model is present, so it starts and behaves like the others |

Conclusion: the Plans-panel approval flow is **broken for all web-chat providers**, so the
fix below is the whole pipeline (trigger → broadcast → panel card → approve/reject →
mode switch), starting with Claude, then generalized to the ACP CLIs.

### UX (decided with design review against `.impeccable.md`)
Surface a pending plan at three altitudes, **reusing existing chrome (no new bar)**:
- **In flow:** the plan renders inline in the transcript as a distinct, collapsible plan
  block, marked "awaiting approval" with the **warning** state (amber + icon, never hue-only;
  **no left-stripe accent** — banned). Matches how every CLI already presents plans.
- **Glanceable action:** the Approve / Request-changes / View affordance lives as a *pending
  state of the existing composer-top strip* `web/src/components/chat/AgentStatusBar.tsx` —
  which already hosts the context-usage pie (`ContextUsageIndicator.tsx`) + New Chat — **not a
  second docked bar**. It sits directly above the Plan/Act/YOLO row, so Approve visibly flips
  Plan → the post-plan mode. It's present in the mobile chat view too, so approving never
  requires opening the activity panel ("don't amputate on mobile").
- **Depth on demand:** the Plans panel holds the full plan text + revision history across
  reject→revise cycles, reached via `View`.
This satisfies "status at a glance, depth on demand" + keyboard-first + the responsive rule.

### Fix
Make plan capture + approval a **backend-agnostic** capability for managed web-chat sessions:
0. **Fix the base trigger first (Claude/SDK).** Establish that presenting a plan in web-chat
   Plan mode produces a `plan_pending_approval` broadcast, then renders it at the three
   altitudes above (inline collapsible block + `AgentStatusBar` pending affordance + Plans
   panel history). Determine why it doesn't fire today — either wire the SDK so the agent
   actually emits `ExitPlanMode` (routed through `chat_session_permissions`), or detect the
   plan turn and broadcast — and confirm the broadcast reaches the UI
   (`transportConversationEvents.ts` → `useChatPageArtifacts` → `AgentStatusBar` + `PlansTab`).
1. **Surface plans for ACP CLIs.** Detect when an ACP agent finishes presenting a plan in
   plan mode (it emits the plan as a normal assistant turn) and broadcast
   `plan_pending_approval` with the plan content, mirroring the Claude `_on_plan_ready`
   path. Wire it through the ACP backends in
   `src/gobby/servers/websocket/chat/backends/` and the shared mixin.
2. **Real mode switch on approve.** Implement `sync_sdk_permission_mode()` per backend
   using the underlying protocol's mode message (ACP `session/set_mode` where the agent
   advertises it; Gemini via its own `gemini_permissions` mixin). For any CLI whose
   protocol cannot accept a mode push, keep the injected-context fallback **but still**
   flip Gobby's `chat_mode` to the resolved post-plan mode and broadcast `mode_changed`
   (reason `plan_approved`) so the **UI Plan radio switches off automatically** — that is
   the user-visible requirement and it is already handled by
   `transportConversationEvents.ts`.
3. **Request-changes loop** already injects `_plan_feedback` on the next prompt
   (acp_permissions.py:125-128); verify the Plans panel re-renders the revised plan after a
   reject by re-broadcasting on the agent's next plan turn.
4. Add a per-CLI capability flag so the UI can show approve/reject for every managed CLI,
   and degrade gracefully (e.g., note "manual switch required") only where a protocol truly
   cannot auto-switch — but pursue the real auto-switch wherever the protocol allows it.

Critical files: `acp_permissions.py`, `gemini_permissions.py`,
`servers/websocket/chat/backends/{codex,droid,gemini,grok,qwen}.py`,
`servers/websocket/handlers/plan_approval.py`, `chat_session.py` (reference impl).

---

## §2 A2UI Canvas — REMOVE the canvas feature (pending user confirmation)

User: never uses it, doesn't think it ever worked. The footprint audit confirms it.

### Findings (code-confirmed)
- The "canvas" feature has **two distinct, unused render paths**:
  - **A2UI surfaces** — `render_surface`/`update_surface` (`mcp_proxy/tools/canvas.py`) →
    `A2UIRenderer` mounted inline in `web/src/components/chat/ToolCallCard.tsx`; state in
    `useChat` `canvasSurfaces`.
  - **HTML/url panel** — `canvas_present` writes `~/.gobby/canvas/{uuid}.html`, served at
    `/__gobby__/canvas/...`, rendered in the iframe of `CanvasPanel.tsx` and the activity
    `CanvasTab.tsx`; state in `useCanvasPanel` (`canvasPanel`).
- **Zero product adoption:** no bundled agent / skill / workflow / pipeline / rule invokes
  any canvas tool. The only callers are the MCP tools themselves (manual) and tests.
- `show_file` lives in `canvas.py` but is a **separate, useful** artifacts tool — KEEP it
  (extract to its own module/registry so canvas can be deleted cleanly).
- **Live test:** `render_surface` *does* succeed and renders **inline in chat** as a
  `CanvasSurfaceCard`; the "A2UI Canvas" activity tab only ever shows `canvas_present` HTML
  iframes — it never displays `render_surface` surfaces. Removal must therefore drop **both**
  the inline A2UI surface path and the HTML-canvas activity tab.

### Decision
Remove the A2UI/canvas feature (both modes) and the `gobby-canvas` MCP server, preserving
`show_file`. → confirm via AskUserQuestion.

### Removal checklist (condensed; full list captured in investigation notes)
- **Backend:** strip `render_surface`/`update_surface`/`wait_for_interaction`/`close_canvas`/
  `canvas_present` + broadcaster/state globals from `canvas.py` (extract & keep `show_file`);
  remove canvas registry init (`registries.py`), `broadcast_canvas_event`
  (`websocket/broadcast.py`), `_handle_canvas_interaction` (`websocket/server.py`),
  `SAFE_CANVAS_CALL_TOOLS` (`tool_approvals.py`, `chat_session_permissions.py`, codex
  adapter), canvas broadcaster wiring + `/__gobby__/canvas` static mount (`app_factory.py`).
- **Frontend:** remove `canvas` from the `ActivityTab` union + `ACTIVITY_PANEL_TABS`
  (`ActivityPanelTabs.tsx`), from `VALID_TABS` (`useActivityPanel.ts`), and the
  `ActivityPanel.tsx` case; delete `web/src/components/canvas/` and `activity/CanvasTab.tsx`;
  remove `canvasSurfaces`/`canvasPanel`/`respondToCanvas` from `useChat.ts` +
  `transportCanvasEvents.ts`/`transportRouter.ts`/`transportLifecycle.ts`; remove canvas
  props down the `ChatPage → ActivityPanel → ToolCallCard` chain; drop the
  `gobby-canvas-panel-width` localStorage key.
- **Docs/tests:** delete `install/shared/skills/canvas/SKILL.md`, the canvas sections of
  `docs/guides/canvas-artifacts.md` + `docs/guides/mcp-tools.md`, and
  `docs/plans/completed/a2ui-canvas-v2-rfe.md`; update activity-panel + chat test mocks;
  trim `tests/mcp_proxy/tools/test_canvas.py` to the retained `show_file` tests; update
  codex-adapter + chat-session-permissions test references.
- **Migration:** remap any stored `canvas` activity tab → a default (e.g. `changes`), like
  the existing artifacts legacy remap, so users don't land on a missing tab.

---

## §3 Changes panel — BROKEN (project-scoped, not session-scoped)

### Findings (code-confirmed)
- `web/src/hooks/useFileChanges.ts:30,66` derives the changed-file list **only from the
  live chat's `messages`** (scans completed edit/write tool calls).
- `:68-85` `fetchDiff` calls `/api/files/git-diff?project_id=…&path=…` — **project-scoped**,
  computing `git diff HEAD` on the project repo, with **no session id**.
- `FileChangesTab.tsx:8-11` receives only `changedFiles` + `fetchDiff`; it has **no session
  awareness**.

Consequences:
- **Live web chat:** appears to work only because the live messages happen to be the active
  chat's; the diff is still the project working tree, not the session's.
- **tmux view/attach & agent sessions:** sessions running in a **worktree/clone** show the
  wrong diff (project repo, not the session's working dir).
- **Resumed sessions:** work already committed shows nothing under `git diff HEAD`.
- **Session switching:** the list/diff are tied to project + live messages, so switching the
  *viewed* session does not reliably switch Changes contents.

### Live result (chrome-devtools subagent)
> _PENDING — agent switches between two existing sessions and reports whether Changes
> contents change._

### Fix — make Changes session-scoped
1. **Backend:** add a session-scoped changes endpoint (e.g. `GET /api/sessions/{id}/changes`
   and `…/diff?path=`) that resolves the session's working directory and base ref from the
   session record + `task_artifacts` (worktree/clone path, base/target branch) and runs git
   there. Reuse existing git utilities in `src/gobby/utils/` and the worktree/clone path
   resolution already used by the dispatch system. Extend or supersede
   `servers/routes/files.py` `git-diff`.
2. **Frontend:** thread the viewed session id (`routing.activityPanelChatSessionId`, i.e.
   `viewingSessionId ?? attachedSessionId ?? dbSessionId`) into the Changes tab; fetch the
   changed-file list + diffs from the session endpoint; key the data on session id so it
   re-fetches on switch. Keep the message-scan as a fast live overlay for the active chat
   only.
3. Add a frontend test in `web/src/components/activity/__tests__/` (and the e2e session
   spec) asserting Changes contents change on session switch.

Critical files: `web/src/hooks/useFileChanges.ts`,
`web/src/components/activity/FileChangesTab.tsx`,
`web/src/components/chat/ChatPage.tsx` (passes props),
`web/src/hooks/useChatPageSessionRouting.ts` (session id source),
`src/gobby/servers/routes/files.py` (+ a sessions changes route).

---

## §4 Artifacts tab — REMOVE (confirmed by user)

### Findings (code-confirmed)
- The activity **Artifacts** tab (`ArtifactsTab.tsx`) shows an in-memory history of
  model-generated artifacts (code/text/image/sheet) from `useArtifacts()` — **frontend-only,
  no backend, lost on reload**.
- It is distinct from Files (real on-disk tree + git status) and Changes (diffs), and from
  the **chat artifacts** system in `web/src/components/chat/artifacts/` (which must stay).
- The same artifacts already render inline via the chat artifact panel, and **Plans** is a
  specialized artifacts view (`isPlan` filter) sharing `ArtifactPanel`.
- `useActivityPanel.ts:57-58` already remaps a legacy stored `artifacts` tab → `changes`,
  showing the team previously began moving away from it.

### Recommendation
Remove the **activity Artifacts tab** (tab A) — confirmed by the user. Its only unique value
is an ephemeral history list, which is low-value given inline rendering + the Plans tab, and
it muddies the "Files vs Changes vs Artifacts" mental model. Keep the chat artifacts system
(tab B) untouched.

### Removal checklist
- `ActivityPanelTabs.tsx` — drop the `artifacts` entry + icon.
- `useActivityPanel.ts` — drop `'artifacts'` from `VALID_TABS`; keep the legacy→`changes`
  remap so existing users land on Changes.
- `ActivityPanel.tsx` — remove the import and `case "artifacts"`.
- Delete `ArtifactsTab.tsx` + `__tests__/ArtifactsTab.test.tsx`.
- `useChatPageArtifacts.ts` — reroute `openCodeAsArtifact`/`openFileAsArtifact`/`onArtifactEvent`
  `showTab("artifacts")` calls (to `plans` or inline only).
- Update `ActivityPanel.test.tsx`, `useActivityPanel.test.tsx`, `ActivityPanelEmpty.test.tsx`.

---

## §5 Bugs surfaced during live testing (fix — "you found it, you own it")

### 5.1 ACP stream readline overflow (Grok + any large message)
`src/gobby/adapters/acp_client.py:571` `_read_stream` does
`asyncio.wait_for(reader.readline(), …)`. The subprocess pipe StreamReader uses the default
64 KiB limit, so a single JSON-RPC line larger than 64 KiB raises `LimitOverrunError`
(`ValueError: Separator is found, but chunk is longer than limit`) and kills the session
(observed live: "Grok managed session … error"). Affects **every** ACP CLI
(Codex/Droid/Gemini/Grok/Qwen) whenever the agent emits a large message.
Fix: build the subprocess StreamReader with a large `limit` (e.g. `create_subprocess_exec(…,
limit=8–16 MiB)` or a custom reader), or replace `readline` with chunked reads that split on
newlines, so arbitrarily large JSON-RPC frames are handled. Add a regression test feeding an
oversized line.

### 5.2 ToolResultEvent before ToolCallEvent → `tool_name "unknown"`
`servers/websocket/chat/_stream_events._handle_tool_result` logs
"ToolResultEvent … arrived before ToolCallEvent (tool_name will be 'unknown')"; out-of-order
ACP events drop the tool name in the UI.
Fix: buffer orphan tool-results by call id and reconcile when the matching ToolCallEvent
arrives (or create a provisional tool-call keyed by id and backfill the name). Add a test for
out-of-order delivery.

### 5.3 Qwen web chat dies on LM Studio auth (401 "Malformed token: lm-studio")
**Confirmed live:** `GET http://localhost:1234/api/v1/models` → **401 without a token, 200
with a valid one** (catalog includes `qwen/qwen3.6-35b-a3b`). The token lives in
`~/.qwen/settings.json` → top-level `env.LMSTUDIO_API_KEY`, currently the dummy placeholder
`"lm-studio"` (both `gemma-4-31b-q8-local` and `qwen3.6-35b-a3b-q8-local` providers reference
it via `envKey: LMSTUDIO_API_KEY`, base `http://localhost:1234/v1`). Newer LM Studio enforces
real API tokens and rejects the placeholder; the warmup
(`local_openai_warmup._prepare_lm_studio_model:284`) surfaces the raw 401 and kills
chat-session start. This is **not** the #15363 secrets migration (which touched only embedding
credentials, not this Qwen-settings path) — Gobby reads the token live from the Qwen CLI's
settings file and has no managed copy.
Fix: (a) source the LM Studio token from the same configured/secret credential the embeddings
backend uses rather than the Qwen `env` placeholder; (b) surface a clear "set a valid LM
Studio API token / disable LM Studio auth" setup error instead of a raw 401 traceback that
kills the chat-session start. Immediate user unblock: put a real LM Studio token in the Qwen
provider config, or disable auth in LM Studio.

---

## Verification (end-to-end)
- **Plans:** for each managed CLI (incl. now-authenticated **Droid**), new web chat → Plan
  mode → ask for a plan → confirm a Plans-panel card with Approve/Request-changes → reject
  w/ feedback → confirm revised plan → approve → confirm the **Plan radio auto-switches off**
  and the CLI actually leaves plan mode. Add backend tests for the broadcast + mode-switch
  per backend.
- **A2UI/canvas removal:** `web` typecheck + vitest pass; `gobby-canvas` gone from the MCP
  server list; `show_file` still works; stored `canvas` tab remaps cleanly; no dangling
  imports.
- **Changes:** open a worktree/clone session and a resumed session; confirm correct,
  session-scoped diffs; switch sessions and confirm contents change. Run the new frontend +
  e2e tests.
- **Artifacts:** after removal, `web` typecheck/tests pass; legacy `artifacts` localStorage
  lands on Changes.
- **§5 bugs:** regression tests for oversized ACP frame (5.1) and out-of-order tool events
  (5.2); manually re-run a Grok web chat to confirm no readline crash.
- Targeted suites only (never the full pytest run): relevant `tests/servers/…`,
  `tests/adapters/…`, `tests/sessions/…`, and `web` vitest/playwright specs.

## Cleanup
Throwaway web-chat sessions created during verification (delete when convenient): #6538,
#6539, #6542, #6543, #6544 (chrome-devtools run) plus any SB/WEB sessions from the playwright
run. None touched the repo (Plan mode).
