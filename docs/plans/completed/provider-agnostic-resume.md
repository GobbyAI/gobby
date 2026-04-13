# Plan: Multi-Provider Web Chat — Unified Session Interaction

**Parent task:** #10591
**Spun off:** #11204 (API-only web chat via LiteLLM/APIChatSession)

## Context

The web UI has three disconnected surfaces for session interaction: SessionsTab (watch-only activity panel), the chat area (web chat + view/attach for CLI sessions), and a standalone terminals page. Users can *watch* a session in the activity panel but can't *interact* with it. They can interact via chat but only through the conversation picker. Terminal sessions need a separate page entirely.

Meanwhile, Gobby supports multiple CLI providers (Claude, Gemini, Codex, local via `claude --model`) but web chat is Claude-only. The competitive landscape (OMX, Agent Hand, muxtree) is converging on tmux orchestration — Gobby's differentiator is a **unified web UI** that can view, attach to, and take over any session regardless of provider.

**Goal:** Collapse these into one model. SessionsTab is the session control surface. The chat area is the universal viewport. Any session renders through the unified transcript renderer. Web chat supports starting sessions with any provider (subscription-first via CLI subprocesses).

---

## Phase 1: Wire SessionsTab to Chat Area (frontend, 3 tasks)

### What changes

SessionsTab lives inside ActivityPanel (`web/src/components/activity/ActivityPanel.tsx`) as one of 7 tabs (Sessions, Pipelines, Tasks, Files, Plans, Changes, Canvas). Currently, clicking a session toggles an inline message preview *within the panel*. Instead, clicking should load the session in the main chat area via `useChat.viewSession()`. The swap between sessions (and back to web chat) initiates from the **SessionsTab header bar** inside the activity panel, plus the command palette.

### UI hierarchy

```
ChatPage
├── CommandBar (top of chat area — already has attach/detach via ObservationSegment)
├── Chat area (left — universal viewport for messages)
└── ActivityPanel (right side panel)
    ├── Tab strip (Sessions | Pipelines | Tasks | Files | Plans | Changes | Canvas)
    └── SessionsTab (when "Sessions" tab active)
        ├── Session list (clickable entries with provider/status badges)
        ├── Session header bar ← PRIMARY SWAP SURFACE (View/Attach/Take Over buttons)
        └── Session context menu (right-click: Attach, Take Over, Send Context, Expire)
```

### Files

| File | Change |
|------|--------|
| `web/src/components/activity/SessionsTab.tsx` | Add `onViewSession`, `onAttachSession`, `onTakeOverSession` callback props. Single-click calls `onViewSession(id)`. Session header bar shows View/Attach/Take Over actions. Context menu gets "Attach" and "Take Over" entries. |
| `web/src/components/activity/ActivityPanel.tsx` | Thread the three new callbacks from ChatPage through `ActivityPanelProps` to SessionsTab (lines 197-205). Add `onViewSession`, `onAttachSession`, `onTakeOverSession` to `ActivityPanelProps`. |
| `web/src/components/chat/ChatPage.tsx` | Wire `useChat` methods to ActivityPanel: `viewSession` → `onViewSession`, `attachToSession` → `onAttachSession`, `continueSessionInChat` → `onTakeOverSession` |

### Task breakdown

1. **Add session action callbacks to SessionsTab** — Add `onViewSession(sessionId)`, `onAttachSession(sessionId)`, `onTakeOverSession(sessionId, projectId?)` props. Single-click calls `onViewSession` (loads transcript in chat area). Replace the inline "Watching {name}" preview with action buttons in the session header bar: "Attach" (if active + has terminal), "Take Over" (if resumable), "Back to Chat" (clears viewing). Context menu adds "Attach" and "Take Over" entries. Keep inline preview as fallback when `onViewSession` is not provided.

2. **Wire callbacks through ActivityPanel → ChatPage** — Add `onViewSession`, `onAttachSession`, `onTakeOverSession` to `ActivityPanelProps` (line 125). Thread them to SessionsTab (line 198). In ChatPage, pass `useChat` methods: `viewSession` → `onViewSession`, `attachToSession` → `onAttachSession`, `continueSessionInChat` → `onTakeOverSession`.

3. **Session capability indicators** — Show "Live" badge for active sessions (attachable). Show provider icon + model badge. Highlight which session the chat area is currently viewing/attached to (use `chatSessionId` prop that already exists). Dim sessions that can't be interacted with.

---

## Phase 2: Chat Area Mode UX (frontend, 3 tasks)

### What changes

The SessionsTab header bar (in the activity panel) is the primary swap initiation surface. The CommandBar's `ObservationSegment` already handles attach/detach when viewing/attached — it needs "Take Over" added. The command palette also supports session operations. When in normal chat mode, the provider picker (Phase 5) appears in the new chat flow.

### Interaction modes (computed from existing state)

```
viewingSessionId && !attachedSessionId  →  "view"     (read-only transcript)
attachedSessionId                       →  "attached"  (live + input enabled)
neither                                 →  "chat"      (normal web chat)
```

No new state — derived from `viewingSessionId` and `attachedSessionId` already in `useChat`.

### Files

| File | Change |
|------|--------|
| `web/src/hooks/useChat.ts` | Add computed `interactionMode` to return value. Add `canTakeOver` derived from session capabilities. |
| `web/src/components/chat/CommandBar.tsx` | Add "Take Over" button to `ObservationSegment` (between Attach/Detach and close). Add "Back to Chat" when viewing. Show provider/model in observation segment. |
| `web/src/components/chat/ChatInput.tsx` | Mode-specific placeholder: "Viewing session #N..." (view), "Send to #N..." (attached), "Message or /command..." (chat). |
| Command palette integration | Add commands: "View Session", "Attach to Session", "Take Over Session", "Detach", "Back to Chat". |

### Task breakdown

4. **Computed interactionMode in useChat** — Add `interactionMode: "chat" | "view" | "attached"` as computed return value. Add `canTakeOver: boolean` derived from attach response capabilities (Phase 4 enriches this, but start with `true` for active sessions).

5. **CommandBar mode controls** — Extend `ObservationSegment` with "Take Over" button (shows confirmation dialog before calling `continueSessionInChat`). Add "Back to Chat" button that calls `clearViewingSession()`. Show provider name and model in the observation bar.

6. **ChatInput mode behavior + command palette** — Update placeholder text per mode. Add session commands to command palette: "View Session #N", "Attach to Session #N", "Take Over Session", "Detach from Session", "Back to Chat".

---

## Phase 3: Resume Strategy Pattern (backend, 4 tasks)

### What changes

Extract provider-specific resume logic from `handle_continue_in_chat()` (195 lines, Claude-specific) into isolated strategy classes. The refactored handler becomes ~20 lines of dispatch.

### Files

| File | Change |
|------|--------|
| `src/gobby/servers/chat_resume_strategy.py` | **NEW** — `ResumeStrategy` protocol + per-provider implementations |
| `src/gobby/servers/websocket/handlers/session_observe.py` | Refactor `handle_continue_in_chat()` to use strategy dispatch |
| `src/gobby/servers/websocket/chat/_session.py` | Extend provider routing in `_create_chat_session()` for Gemini |

### Resume Strategy Protocol

```python
class ResumeStrategy(Protocol):
    provider_name: str
    
    def can_native_resume(self, source_session) -> bool: ...
    async def resolve_resume_id(self, source_session, session_manager, agent_run_manager) -> str | None: ...
    async def kill_owner(self, source_session, session_manager) -> None: ...
    async def build_history_context(self, source_session, max_chars: int) -> str | None: ...
```

### Strategy matrix (verified via context7 docs)

| Strategy | Providers | Resume method | Confirmed API |
|----------|-----------|--------------|---------------|
| `ClaudeResumeStrategy` | claude_code, claude_sdk_web_chat, local models (via --model) | SDK native via `resume` param | `ClaudeAgentOptions(resume="session-xyz")` |
| `CodexResumeStrategy` | codex, codex_web_chat | Thread resume via JSON-RPC | JSON-RPC `thread/resume` |
| `GeminiResumeStrategy` | gemini, gemini_cli, gemini_web_chat | Native CLI resume via `--resume` flag | `gemini --resume <session-uuid> -p "prompt" --output-format stream-json` |
| `TranscriptResumeStrategy` | Any without native resume | Parse JSONL via existing `TranscriptParser` subclasses | Fallback only |

### Refactored handler

```python
strategy = get_resume_strategy(source_session.source)
resume_id = await strategy.resolve_resume_id(...)
await strategy.kill_owner(...)
history_context = None if resume_id else await strategy.build_history_context(...)
session = await self._create_chat_session(
    conversation_id,
    resume_session_id=resume_id,
    history_context=history_context,
    provider_source=source_session.source,
)
```

### History context injection (TranscriptResumeStrategy fallback only)

For providers without native resume (unknown sources):
1. Look up `session.jsonl_path` or archive at `~/.gobby/session_transcripts/`
2. Select parser based on `session.source` (existing parsers in `src/gobby/sessions/transcripts/`)
3. Extract last N turns via `extract_last_messages()` or `extract_turns_since_clear()`
4. Inject via `history_context` param → session's `system_prompt_override` or dedicated field on `ChatSessionProtocol`

Note: Claude, Codex, and Gemini all have native resume — transcript injection is a last-resort fallback.

### Task breakdown

7. **Create `chat_resume_strategy.py`** — Protocol definition + `ClaudeResumeStrategy` (extract from `session_observe.py:75-149`). Registry function `get_resume_strategy(source) -> ResumeStrategy`.

8. **`CodexResumeStrategy`** — Wire `CodexChatSession.resume_thread()` into strategy. Handle thread_id resolution from `external_id`.

9. **`GeminiResumeStrategy` + `TranscriptResumeStrategy`** — Gemini uses native CLI resume via `gemini --resume <session-uuid>`. `TranscriptResumeStrategy` is the generic fallback for providers without native resume — uses existing transcript parsers for history injection.

10. **Refactor `handle_continue_in_chat()`** — Replace 195-line Claude-specific handler with strategy dispatch. ~20 lines of core logic.

---

## Phase 4: Attach Enrichment (backend + frontend, 2 tasks)

### What changes

When the frontend attaches to a session, the backend response includes capability info so the UI can show the right buttons without guessing.

### Files

| File | Change |
|------|--------|
| `src/gobby/servers/websocket/handlers/session_observe.py` | Enrich `attach_to_session_result` with capabilities |
| `web/src/hooks/useChat.ts` | Read capabilities from attach response, expose as `canAttach`/`canTakeOver`/`resumeMethod` |
| `web/src/components/chat/CommandBar.tsx` | Conditionally show buttons based on capabilities |

### Enriched attach response

```python
{
    "type": "attach_to_session_result",
    "session_id": "...",
    # EXISTING: ref, source, status
    # NEW:
    "can_interact": bool(session.terminal_context),  # has tmux
    "can_take_over": strategy.can_native_resume(session) or bool(session.jsonl_path),
    "resume_method": "sdk_native" | "thread_resume" | "history_injection" | "fresh",
    "provider": session.source,
    "model": session.model,
}
```

### Task breakdown

11. **Backend: enrich attach response** — Add `can_interact`, `can_take_over`, `resume_method`, `provider`, `model` to `attach_to_session_result`. Use resume strategy to determine capabilities.

12. **Frontend: capability-driven UI** — Read capabilities from attach response in `useChat`. Expose `canTakeOver`, `resumeMethod`, `sessionProvider`, `sessionModel`. CommandBar conditionally shows "Take Over" only when `canTakeOver` is true. Show resume method as tooltip hint.

---

## Phase 5: Gemini Web Chat + Provider Picker + Personas (backend + frontend, 4 tasks)

### What changes

Add Gemini as a web chat provider. Subscription-first: wraps `gemini` CLI as a subprocess (same pattern as `ChatSession` wrapping `claude`). User's Google auth flows through the CLI.

### Files

| File | Change |
|------|--------|
| `src/gobby/servers/gemini_chat_session.py` | **NEW** — `GeminiChatSession` implementing `ChatSessionProtocol` |
| `src/gobby/servers/websocket/chat/_session.py` | Extend `_create_chat_session()` with gemini provider routing |
| `src/gobby/sessions/transcripts/gemini.py` | Complete `extract_turns_since_clear()` and `is_session_boundary()` |
| Web UI — provider picker | Model/provider selector in CommandBar or new chat dialog |

### GeminiChatSession design

- Wraps `gemini` CLI subprocess (same pattern as `ChatSession` wrapping `claude` CLI)
- Non-interactive mode: `gemini -p "prompt" --output-format stream-json`
- Streaming output: NDJSON with event types `tool_use`, `message`, `result` (confirmed via context7)
- Subscription-first auth: user's `gcloud`/ADC auth flows through the CLI
- Translates Gemini NDJSON events to `ChatEvent` types (`TextChunk`, `ToolCallEvent`, `ToolResultEvent`, `DoneEvent`)
- Resume: native via `gemini --resume <session-uuid> -p "prompt" --output-format stream-json` (confirmed via context7)

### Provider routing extension

```python
# In _create_chat_session():
if use_gemini:
    session = GeminiChatSession(conversation_id=conversation_id)
elif use_codex:
    session = CodexChatSession(conversation_id=conversation_id)
else:
    session = ChatSession(conversation_id=conversation_id)
```

### Task breakdown

13. **GeminiChatSession** — Implement `ChatSessionProtocol` wrapping `gemini` CLI subprocess. Use `gemini -p "prompt" --output-format stream-json` for non-interactive streaming. Parse NDJSON events (`tool_use`, `message`, `result`) and translate to `ChatEvent` types. Resume via `gemini --resume <session-uuid>`. Handle model selection (`gemini-3.1-pro`, `gemini-3-flash`, etc.).

14. **Provider routing + Gemini integration** — Extend `_create_chat_session()` with `use_gemini` detection. Capture Gemini session UUID from CLI output for future resume. Set `source="gemini_web_chat"` in DB. Complete `GeminiTranscriptParser.extract_turns_since_clear()` and `is_session_boundary()` for view mode transcript rendering. Update stale model names in `src/gobby/config/llm_providers.py`: Codex models to `gpt-5.4,gpt-5.3-codex,gpt-5.3-codex-spark`, Gemini models to `gemini-3-flash,gemini-3.1-pro`.

15. **Web UI provider picker** — Add provider selector to new chat flow (CommandBar "+" button or new chat dialog). The picker filters agent definitions with `sources` containing `*_web_chat` and presents them as provider choices (Claude, Gemini, Codex). Model dropdown updates based on provider. Under the hood, selecting a provider selects the corresponding web chat agent definition (`default-web-chat`, `default-gemini-web-chat`, `default-codex-web-chat`). Local models appear under Claude with model sub-selection.

16. **Persona system (`mode: persona`)** — Add `"persona"` to the `AgentDefinitionBody.mode` Literal type. Persona-mode definitions are apply-only: `apply_persona_impl()` layers their behavioral config (rules, skills, variables, tool restrictions, system prompt) onto an existing session without spawning. The agent spawner skips `mode: persona` definitions. Add `/personas` as a **web chat slash command** (only available in web UI chat input, not CLI sessions): `list` (shows persona-eligible defs), `apply <name>` (sends WS message → backend calls `apply_persona_impl`), `remove` (resets to default). The provider picker optionally offers persona selection alongside provider choice.

### Persona system details

**New mode value:** `mode: "persona"` on `AgentDefinitionBody` (line 269 of `definitions.py`)
- `mode: interactive` — spawnable, interactive CLI session
- `mode: autonomous` — spawnable, runs independently
- `mode: inherit` — inherits from parent
- `mode: persona` — **NEW** — apply-only, never spawned

**Composition model:**
- **Provider** = where/what runs (Claude CLI, Gemini CLI, Codex, local model)
- **Persona** = how it behaves (rules, skills, system prompt, tool restrictions)
- A web chat is: `provider + model + optional persona`
- Same persona can apply to any provider: "code-reviewer" works on Claude, Gemini, or local

**Files to modify:**
- `src/gobby/workflows/definitions.py:269` — Add `"persona"` to mode Literal
- `src/gobby/agents/spawn.py` — Skip `mode: persona` in spawn validation
- `src/gobby/mcp_proxy/tools/apply_persona.py` — Already works, no changes needed
- Web UI — `/personas` command in command palette
- `src/gobby/install/shared/workflows/agents/` — Create example persona templates

**Existing infrastructure reused:**
- `apply_persona_impl()` already does the full behavioral merge
- `build_persona_changes()` computes rules, skills, variables, step workflows
- `SessionVariableManager.merge_variables()` persists the changes
- Web chat agent definitions (`default-web-chat.yaml`, `default-codex-web-chat.yaml`) already demonstrate the pattern

---

## Unified Transcript Rendering

**Already handled.** The transcript renderer and `UnknownBlockCard` component already exist:

- `web/src/components/chat/UnknownBlockCard.tsx` — Renders unrecognized blocks with amber styling, collapsible raw JSON
- `web/src/components/chat/MessageItem.tsx` — Routes to `UnknownBlockCard` for unknown content types

**What to verify during implementation:**
- Non-Claude provider transcripts (Gemini, Codex) render correctly through the unified renderer
- Unknown tool types from new providers hit the `UnknownBlockCard` path (not crash)
- Add `console.warn()` logging when unknown blocks are encountered (for diagnostics)

---

## Key Design Decisions

1. **SessionsTab is the control surface, chat area is the viewport.** Clean separation of navigation from display.
2. **Three existing `useChat` primitives are sufficient** for Phases 1-2. `viewSession`, `attachToSession`, `continueSessionInChat` already implement view/attach/takeover.
3. **Strategy pattern for resume** keeps provider logic isolated and easy to extend.
4. **All three major providers have native resume** (verified via context7): Claude SDK `ClaudeAgentOptions(resume=id)`, Codex `thread_resume(thread_id)`, Gemini CLI `--resume <uuid>`. Transcript injection is fallback only for providers without native resume.
5. **Subscription-first auth** — CLI subprocesses (claude, gemini, codex) handle their own auth. No API key management needed initially.
6. **CLI subprocess for all providers** — `ChatSession` wraps `claude`, `CodexChatSession` wraps Codex, `GeminiChatSession` wraps `gemini` (with `--output-format stream-json` for NDJSON streaming). Same pattern throughout.
7. **No database changes.** Existing `sessions` table has `source`, `external_id`, `jsonl_path`, `terminal_context`, `parent_session_id`.
8. **Unknown blocks degrade gracefully.** `UnknownBlockCard` already renders amber with collapsible JSON. Just verify the pipeline works for non-Claude content.
9. **Local models = Claude sessions.** `claude --model local-model` uses the same `ChatSession` class. No special handling needed.

---

## Critical Files

| File | Phase | Change |
|------|-------|--------|
| `web/src/components/activity/SessionsTab.tsx` | 1 | Add action callbacks, replace inline preview dispatch |
| `web/src/components/activity/ActivityPanel.tsx` | 1 | Thread callbacks from ChatPage |
| `web/src/components/chat/ChatPage.tsx` | 1 | Wire useChat methods to SessionsTab |
| `web/src/hooks/useChat.ts` | 2, 4 | Computed `interactionMode`, capability props |
| `web/src/components/chat/CommandBar.tsx` | 2, 4 | Mode controls, Take Over, provider picker |
| `web/src/components/chat/ChatInput.tsx` | 2 | Mode-specific placeholders |
| `src/gobby/servers/chat_resume_strategy.py` | 3 | **NEW** — Strategy protocol + implementations |
| `src/gobby/servers/websocket/handlers/session_observe.py` | 3, 4 | Strategy dispatch, attach enrichment |
| `src/gobby/servers/websocket/chat/_session.py` | 3, 5 | Provider routing |
| `src/gobby/servers/gemini_chat_session.py` | 5 | **NEW** — GeminiChatSession |
| `src/gobby/servers/chat_session_base.py` | 3 | ChatSessionProtocol (reference) |
| `src/gobby/sessions/transcripts/gemini.py` | 5 | Complete parser |
| `src/gobby/workflows/definitions.py` | 5 | Add `"persona"` to mode Literal |
| `src/gobby/agents/spawn.py` | 5 | Skip `mode: persona` in spawn validation |
| `src/gobby/mcp_proxy/tools/apply_persona.py` | 5 | Already works (reference) |
| `src/gobby/install/shared/workflows/agents/default-gemini-web-chat.yaml` | 5 | **NEW** — Gemini web chat agent def |
| `src/gobby/config/llm_providers.py` | 5 | Update stale model names (Codex → gpt-5.4/5.3-codex, Gemini → gemini-3-flash/3.1-pro) |

---

## Verification

- **Phase 1-2:** Sessions tab > click session > chat area loads transcript > click "Attach" in CommandBar > input enables with "Send to #N" > type message > delivered via `send_to_cli_session` > click "Take Over" > confirmation > new web chat session created
- **Phase 3:** Resume a Codex session > verify `thread_resume(thread_id)` called. Resume a Gemini session > verify `gemini --resume <uuid>` invoked with session UUID.
- **Phase 4:** Attach to session > verify response includes capabilities > frontend conditionally shows buttons
- **Phase 5 — Gemini:** Start new web chat with Gemini provider via picker > verify `GeminiChatSession` created > messages stream via `--output-format stream-json` > tool calls render > unknown blocks render as amber cards
- **Phase 5 — Provider picker:** Click "+" in CommandBar > see Claude/Gemini/Codex options > select Gemini > model dropdown shows `gemini-3.1-pro`, `gemini-3-flash` > start chat > correct agent def selected
- **Phase 5 — Personas:** `/personas list` shows persona-mode definitions > `/personas apply code-reviewer` > session rules/skills update > `/personas remove` > reverts to default
- **Swap test:** View session A > click session B in SessionsTab > chat area auto-detaches from A, loads B
- **Provider test:** Web chat with Claude > swap to watching Gemini CLI session > "Take Over" > new Gemini web chat session with native resume (`--resume <uuid>`)
- **Persona composition:** Start Gemini web chat > apply "code-reviewer" persona > verify Gemini provider unchanged but rules/skills/prompt updated
- **Unknown block test:** Feed transcript with unrecognized tool type > renders as `UnknownBlockCard` > console.warn logged
