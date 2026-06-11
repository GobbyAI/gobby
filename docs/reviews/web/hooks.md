# Review: web hooks (`web/src/hooks/`)

- **Scope:** all of `web/src/hooks/` — the useChat module family (core, actions, handlers, lifecycle, transport routing/events, session viewing/identity/attachment/persistence), session data hooks (useSessionDetail, useSessionCatalog, sessionTranscriptWindow, useTmuxSessions, useTraces), tasks/voice/agents hooks, and the data/CRUD layer (~17.5k non-test lines; tests cross-referenced for contract claims).
- **Reviewer:** Claude Fable 5 — 6-agent fan-out (useChat core; useChat session state; transport events/streams; session data; tasks/voice/agents; data/CRUD catalogue) + synthesizer verification of every Blocker against source
- **Commit / branch:** `b2b7d283b` / `0.5.0`
- **Summary:** 7 Blocker · 41 Important · 29 Nit (plus one Blocker cross-filed in `components-b.md`: cron creation omitting `project_id`, whose type gap lives in `useCronJobs.ts:48-58`). The dominant failure classes: async work guarded by state read before the last await (mic left hot, sockets resurrected after teardown, stale responses applied), WS results uncorrelated with the requests that asked for them (session-identity hijacks), and a hook layer that swallows HTTP failures into null/false sentinels which callers treat as success. The repo contains the correct idiom for every one of these classes — generation refs, `cancelled` flags, identity guards, `useWiki`'s throwing contract — applied inconsistently.

## Findings — Blockers

### [BLOCKER] Editing any file >1 MiB silently destroys everything past the first 1 MiB

- **Where:** `web/src/hooks/useFiles.ts:281-297` (read handler stores content, drops `truncated` — the flag appears nowhere in the file), `:360-404` (`saveFile` POSTs verbatim); backend `src/gobby/servers/routes/files.py:301-306` (truncates at `DEFAULT_MAX_SIZE = 1_048_576`, returns `truncated: true`), `:340-372` (write is verbatim)
- **Failure mode:** `/api/files/read` truncates and flags; the hook stores the truncated text as `content/originalContent/editContent` and discards the flag. The edit toolbar gates only on `image/binary/loading/error` (`FilesPage.tsx:270`), so the user edits truncated text and Save overwrites the file — tail permanently lost, success reported. Large JSON/lock/log/SQL files are routine.
- **Minimal fix:** Persist `truncated` on the tab; refuse `toggleEditing`/`saveFile` when set, with a "too large to edit" notice.
- **Confidence:** high — synthesizer-verified the flag is unread in the hook.

### [BLOCKER] Clearing project settings fields is a silent no-op reported as "Settings saved"

- **Where:** `web/src/hooks/useProjects.ts:8-9, 102-121` (type allows `string | null`); consumer `web/src/components/projects/ProjectSettings.tsx:81-84` (sends `|| null` to clear); backend `src/gobby/servers/routes/projects.py:142` (`model_dump(exclude_none=True)` drops the nulls)
- **Failure mode:** Clearing `github_url`/`github_repo`/`linear_team_id`/`linear_project_id` sends `null`; Pydantic excludes None; the PUT succeeds and echoes the unchanged project; `updateProject` returns true and the UI toasts "Settings saved" while the value persists. Users cannot unlink GitHub/Linear from a project at all.
- **Minimal fix:** Switch the backend to `model_dump(exclude_unset=True)` so explicit nulls clear fields (the correct contract), or send empty-string sentinels normalized server-side.
- **Confidence:** high — synthesizer-verified all three links.

### [BLOCKER] useWebSocketEvent teardown race: zombie reconnect, clobbered singleton, duplicate event delivery

- **Where:** `web/src/hooks/useWebSocketEvent.ts:135-147` (cleanup: `closed = true` → `ws.close(); ws = null` → synchronously `closed = false`), `:60-69` (`onclose` schedules reconnect when `!closed`), `:49-51` (`connect()` has no `if (ws) return` guard)
- **Failure mode:** The socket's `close` event fires asynchronously after cleanup has already reset `closed = false`, so `onclose` schedules a reconnect — with zero consumers, a zombie connection reconnects forever; with a remount in between, `onclose` first nulls the module-level `ws` (orphaning the live replacement socket, which stays OPEN and still dispatches into the shared `handlers` map) and then creates a third socket — duplicate delivery of every event, double refetch storms. Masked today only because `useProjects`/`useSessionCatalog` subscribe unconditionally at the App root so `handlers.size` never reaches 0; guaranteed under StrictMode remounts and reachable via any future conditional root consumer. This is the shared WS primitive under ~14 hooks. (Found independently by two reviewers.)
- **Minimal fix:** Detach the socket's handlers (`ws.onclose = null; ws.onmessage = null`) before closing instead of the `closed` flag dance; guard `connect()` with `if (ws) return`.
- **Confidence:** high — synthesizer-verified the flag reset and unguarded reconnect.

### [BLOCKER] Session catalog applies stale in-flight responses across project switches — wrong project's sessions persist

- **Where:** `web/src/hooks/useSessionCatalog.ts:110-133` (`resetAndFetch` — unconditional `setSessions(result.sessions)` after the await, no generation token), `:137-158` (`refreshPageOne` merges by id into `prev`), `:160-170` (project effect)
- **Failure mode:** WS `session_event` schedules a debounced `refreshPageOne` under project A → user switches to project B → `resetAndFetch(B)` clears and fetches → A's slow response resolves last and is merged into B's catalog. Because every subsequent update is a merge/patch (nothing evicts non-matching rows), the wrong-project rows persist until a full reload. Same last-write-wins race on filter changes. The catalog feeds the SessionsTab/activity UI. No test covers either race.
- **Minimal fix:** A `fetchGenerationRef` bumped on every project/filter reset; drop responses whose captured generation (and `projectId`) no longer match.
- **Confidence:** high — synthesizer-verified the unguarded set and merge.

### [BLOCKER] Transcript tail-refresh stitches a non-contiguous page — silently drops >50 missed groups and wedges `hasNewer` forever

- **Where:** `web/src/hooks/sessionTranscriptWindow.ts:244-272` (`applyTailRefreshTranscriptPage`: `tailContiguous = state.windowEnd >= state.renderedTotal`, then appends and sets `windowEnd = state.windowEnd + appendedMessages.length`); consumed at `web/src/hooks/useSessionDetail.ts:454-470`, `loadNewer` offset math at `:624`
- **Failure mode:** The function assumes the refreshed tail page is contiguous with `windowEnd`, never checking the page's implied start (`page.renderedTotal - page.messages.length`). After >PAGE (50) groups are missed (WS drop, tab sleep), the tail page (e.g. groups 150–200) is appended directly after group 99 and `windowEnd` becomes 150 while the window actually holds rows 150–200 — groups 100–150 are never rendered. Recovery is impossible: `hasNewer` drives `loadNewer` at `offset=150`, which returns rows already present, zero new ids, `windowEnd` never advances — permanent silent hole plus a stuck "load newer" state until session re-select. Unit tests cover overlap and single-row gaps only.
- **Minimal fix:** When the page's implied start exceeds `state.windowEnd`, don't stitch — rebase onto the tail page via `createTailTranscriptWindow` (or return `needsFetch: true` reusing the live-gap path).
- **Confidence:** high on the math — synthesizer-verified the contiguity test and windowEnd arithmetic; med on trigger frequency.

### [BLOCKER] Push-to-talk can leave the microphone hot with no way to stop it

- **Where:** `web/src/hooks/voice/useVoiceCapture.ts:584-655` (`startRecording`: only re-entry guard is async React state `isRecording` at `:585`; `await getUserMedia` at `:598`; `recCtxRef.current` assigned unconditionally at `:629`), `:439-441` (`stopRecording` early-returns `if (!rec) return`), `:666-676` (unmount cleanup runs `cancelRecording` against a still-null ref)
- **Failure mode:** No generation token across the awaits. (1) Release-before-grant: pointer-up during the permission prompt hits the `!rec` early return and no-ops; the grant then resolves, the mic goes hot with no held button. (2) Unmount during pending start: cleanup runs while `recCtxRef` is null; the stream resolves afterward and is never stopped — mic open until page reload. (3) Double-tap: two concurrent `getUserMedia`; the second overwrites `recCtxRef.current` and the first MediaStream is orphaned with live tracks. In (2)/(3) nothing in the UI can ever stop the orphaned stream; the OS recording indicator stays on.
- **Why it matters:** Open microphone without user intent is a privacy violation.
- **Minimal fix:** A `startGenerationRef` bumped by start/stop/cancel/unmount; after each await, if the generation moved (or unmounted), stop the just-acquired tracks and close the context; tear down any existing `recCtxRef` before assigning a new one.
- **Confidence:** high — synthesizer-verified the guard, the early return, and the unconditional assignment.

### [BLOCKER] `continueSessionInChat` crashes or permanently wedges the chat if the WS drops mid-flight

- **Where:** `web/src/hooks/useChat/actions.ts:344-349` (WS-open check at entry only), `:379-380` (`continuingSessionIdRef`/`setIsContinuingSession(true)` committed), `:449` (`wsRef.current.send(...)` after two awaited fetches, no re-check); `web/src/hooks/useChat/transportLifecycle.ts:135` (`onclose` nulls `wsRef.current`)
- **Failure mode:** A disconnect during the fetch window makes the send throw `TypeError` on null (unhandled — the caller in `App.tsx:604-612` doesn't catch) or be silently discarded on a CLOSING socket. The continuing flags are only cleared by a `session_continued` or transport `error` frame that will never arrive (the request was never sent): chat input stays disabled (`useChatPageProviderState.ts:126`), the entry guard blocks all future continuations, the rollback snapshot is never applied. No timeout, no reconnect cleanup — reload required. (Found independently by two reviewers.)
- **Minimal fix:** Re-check `wsRef.current?.readyState === WebSocket.OPEN` immediately before the send; on failure clear continuing state and apply `continuationRollbackRef`; add a continuation timeout or clear continuing state in `onclose` as a backstop.
- **Confidence:** high — synthesizer-verified the entry-only guard, the post-await dereference, and the onclose nulling.

> **Cross-filed Blocker:** cron jobs created from a project view persist unscoped and vanish from the filtered list while continuing to run — the type gap is `useCronJobs.ts:48-58` (`CreateCronJobRequest` lacks `project_id`); full write-up in `components-b.md`.

## Findings — Important

### useChat core (transport, streaming, actions)

- **[IMPORTANT] Reconnect backfill writes the previous conversation's messages into the newly active chat** — `transportLifecycle.ts:83-101`: the `.then` guards only `viewingSessionIdRef`, not `conversationIdRef.current !== convId`; switching conversations during the backfill appends the old conversation's messages (dedup is against the new list) and poisons `lastSeqRef` with the old `max_seq`. Every sibling fetch has the guard (`actions.ts:163-166`, `lifecycle.ts:92-96`). (high)
- **[IMPORTANT] Offline-queued messages render twice after reconnect and lose `injectContext`** — `actions.ts:716-741` (optimistic bubble + queue), `transportLifecycle.ts:62-81` (flush re-runs full `sendMessage`, appending a second bubble; queue entry shape has no `injectContext`, flush passes undefined — the model receives different context than the user saw attached). No test covers the flush path. (high)
- **[IMPORTANT] The 30s "stream is dead" failsafe is cancelled after 2s and can never fire** — `transportLifecycle.ts:139-157`: the reconnect timer's first statement clears the disconnect timer; every retry repeats it. During any outage >~2s while streaming, `isStreaming`/`isThinking`/`activeRequestIdRef` stay set indefinitely — permanent spinner, composer stuck mid-stream. The comment describes behavior that cannot occur. (high)
- **[IMPORTANT] Chat transport reconnects after unmount — ghost socket loop** — `lifecycle.ts:148-158` cleanup closes the socket without nulling `wsRef.current` or detaching `onclose`; the async `onclose` passes its identity guard, schedules `connectRef.current?.()` in a new timer nothing clears, and a fresh socket subscribes to 8 event types on an unmounted hook tree, reconnecting every 2s forever on daemon restart. Masked by the single App-root mount. (Found independently by two reviewers.) (high)
- **[IMPORTANT] `model_switched` system notice appended regardless of conversation match** — `handlers.ts:377-399`: `matchesActiveConversation` gates the meta update but not the `setMessages` append — a wrong-conversation event injects "Model switched…" into the active chat. (med-high)
- **[IMPORTANT] `pending_approval` bypasses the request gate with no session check — approval cards land in the wrong chat** — `handlers.ts:210-249`: pending_approval frames skip `isActiveRequest`; `ToolStatusMessage` carries no `conversation_id`; after a switch, a late frame fabricates an assistant message + approval card in the new chat, and `respondToApproval` routes the decision to the wrong conversation while the original approval hangs. (med-high)
- **[IMPORTANT] `chat_error` reuses the assistant `message_id` for a new system message — duplicate ids** — `handlers.ts:199-206` + producer `_streaming.py:158-166`: two list entries share an id whenever an error follows streamed content; id-based lookups and backfill dedup silently target/drop the wrong entry; React keys collide. (high)
- **[IMPORTANT] `sendMode` commits locally then silently drops the server send when disconnected** — `actions.ts:556-573`: `setCurrentMode` runs before the WS check; offline mode flips (act→plan) show locally while the backend stays in the old mode — messages queued for reconnect then execute under the wrong mode. Sibling actions return `false` on closed WS. (med)

### useChat session/viewing state

- **[IMPORTANT] `clearViewingSession` restores chat mode without syncing `currentModeRef`** — `sessionViewing.ts:283-295` calls the UI callback but never writes the ref (every other restore site syncs it first). After viewing a `bypass` session, the UI can show `plan` while `currentModeRef` holds the viewed session's mode: `ensureMainSession` creates sessions with the stale mode and `sendMode`'s dedupe eats the user's correction. (high)
- **[IMPORTANT] `attach_to_session_result` adopted unconditionally — a late attach hijacks the viewer** — `transportProxyEvents.ts:44-53, 112-113`: no check that an attach to that session is still desired; attach(A) → view(B) → A's result yanks viewing back to A and replaces B's transcript; the client also stays server-side attached to A with no detach sent. (med-high)
- **[IMPORTANT] `session_continued` applied without correlating to the active continuation** — `transportConversationEvents.ts:147-164`: the server echoes `source_session_id` but the client never reads it; a late frame after a conversation switch rebinds the main chat (and localStorage) to the continuation session. (med)
- **[IMPORTANT] Persisted viewing-session ID has no 404 invalidation — permanent empty viewer across reloads** — `sessionViewing.ts:144-160` bails without clearing on a dead session; the persistence effect re-writes the dead id, the reconnect retry refetches the 404 forever; the main-chat restore path (`lifecycle.ts:55-66`) handles exactly this. (med-high)
- **[IMPORTANT] `clearViewingSession` never restores the `lastSeqRef` watermark — reconnect backfill silently disabled afterwards** — `sessionViewing.ts:129, 266-281` vs `transportLifecycle.ts:83` (backfill requires `lastSeqRef > 0`); messages broadcast during a later WS drop are simply missing until a switch/reload. (med-high)
- **[IMPORTANT] `preAttachContextUsageRef` orphaned when a detach result is superseded** — `sessionViewing.ts:111-121, 219-233` + `transportProxyEvents.ts:152-180`: refs nulled before the detach result arrives, so the snapshot restore never runs; the next attach/detach cycle restores an outdated usage snapshot to the pie. (med)
- **[IMPORTANT] `setOnArtifactEvent` registered by useChatPageArtifacts, never unregistered** — `callbacksState.ts:42-45`: identical class to the filed `onPlanReadyRef` leak; should ride the same fix. (med-high)

### Transport events / streams

- **[IMPORTANT] WS vs REST `event_at` format mismatch defeats token-event dedupe — double-counted usage** — `useTokenEventsStream.ts:16-29` + `useSessionTokenEvents.ts:59-68` key on the raw `event_at` string; the WS payload is canonical `...Z` (`storage/token_events.py:61`) while REST serializes `...+00:00` — for events without `message_id` (an expected state per the partial unique index), the same row delivered by both channels is kept twice and `breakdown` sums it twice. The two client key functions also disagree on cache fields. (med)
- **[IMPORTANT] Poll `since` cursor permanently skips same-second events missed during WS gaps** — `useSessionTokenEvents.ts:102-104` + server `event_at > %s` (strict) over second-truncated timestamps: an event recorded at the cursor's exact second after the previous poll is never returned by any later poll — deterministic blind spot in the reconnect-recovery mechanism. (med)
- **[IMPORTANT] `handleTokenEvent` writes cumulative session totals into context-usage state; snapshot preservation launders them into `context_used_tokens`** — `transportUsageEvents.ts:305-320, 205-210, 256-268`: cumulative input ÷ context window is not occupancy; the ratio drifts to 100% on long sessions and the `"token_event"`-sourced snapshot makes the corruption stick across merges. (med)
- **[IMPORTANT] useDialogFocus: un-cancelled initial-focus rAF steals focus after the dialog closes** — `useDialogFocus.ts:50-52, 80-90`: open/close within a frame restores focus then the pending rAF re-focuses an element inside the closed dialog. Shared primitive under 8+ dialogs. (high)

### Session data hooks

- **[IMPORTANT] Old session's transcript stays rendered under the newly selected session until its first page lands** — `useSessionDetail.ts:280-296, 355-358`: `resetPaging` resets counters but never clears `messages`; the spinner gate (`WatchingTranscript.tsx:298`) is bypassed because `chatMessages.length > 0`; no `key` remount saves it. (high)
- **[IMPORTANT] Stale trace fetch/timer clobbers the detail view after switching traces** — `useTraces.ts:101-138`: no post-await staleness check, and the `trace_event` debounce timer survives a `traceId` change, firing the old closure's `fetchDetail` over the new trace's spans. Same shape in `useTraces`' list timer. (high)
- **[IMPORTANT] Live WS message applied during the initial transcript load is silently clobbered by the fetched snapshot** — `useSessionDetail.ts:361, 418-422` vs the live handler at `:683-707`: `isCurrent()` checks session id and load version but not `tailWindowVersionRef` — the load-more paths guard exactly this; the initial load doesn't. (med)
- **[IMPORTANT] Scroll-up history loading starves under active streaming** — `useSessionDetail.ts:576-589` + version bump on *every* window change (`:258-268`): tail appends void the in-flight older page even though prepends commute; with messages arriving faster than one fetch RTT, history never loads while the user watches an active session. (med-high)
- **[IMPORTANT] Failed messages fetch during session load renders as "empty transcript"** — `useSessionDetail.ts:141-146` swallows non-2xx into `{mapped: [], ok: false}` and `loadSessionDetail` never checks `ok`: a transient 500 (or the intended 413 "download instead" case) presents as a definitive empty state with `sessionError` null. (high)

### Tasks / voice / agents hooks

- **[IMPORTANT] TTS barge-in race: stopped source's `onended` corrupts playback accounting → overlapping audio** — `useTTSPlayback.ts:132, 184-198`: `stopLocalPlayback` never detaches `onended`; the stopped source's callback fires while the next utterance's source plays, resets `isPlayingRef`, and a second concurrent playback chain starts. Barge-in → new TTS is the normal voice loop. (high)
- **[IMPORTANT] TTS AudioContext is suspended, never closed, on unmount** — `useTTSPlayback.ts:245-253`: each remount creates a fresh context; browsers cap concurrent contexts; after enough remounts `new AudioContext()` throws and TTS dies silently. (med-high)
- **[IMPORTANT] AudioContext leaked on every failed PTT start** — `useVoiceCapture.ts:608, 638-646`: the locally-scoped `ctx` is created inside the try and never closed in the catch (`recCtxRef` not yet assigned, so teardown no-ops) — one leak per press under a persistent worklet-load failure. (high)
- **[IMPORTANT] `useTasks()` has zero production consumers — 841-line hook + 966-line test suite guard dead code** — every component import is `import type` only (searches shown in the agent transcript); the live TasksTab uses a parallel data layer that carries the already-filed reconcile bug. Test investment and runtime risk are inverted. Wire it in or delete it. (high)
- **[IMPORTANT] useTasks stale-response window: version checked at headers, not after body read** — `useTasks.ts:366-371, 395-403`: request A passes the check, B completes during A's `response.json()`, A's stale list replaces B's. The staleness test only covers the pre-headers window. (med-high; latent while unwired)
- **[IMPORTANT] useTasks pagination collapses on every WS event; `loadMore` offset drifts** — `useTasks.ts:268-296, 370, 395, 764-766`: any task event debounce-refetches page 1 and replaces the accumulated list; WS appends inflate `allTasks.length` so concurrent `loadMore` skips rows. (med-high; latent)
- **[IMPORTANT] useTasks mutations swallow non-OK responses** — `createTask`/`updateTask`/`postTaskTransition`/`getTask` (`useTasks.ts:430-516`) return bare null, discarding the policy-bearing error bodies (claim conflicts, status gates) that `patchStage` (`:518-540`) correctly throws. (high; latent)
- **[IMPORTANT] `spawnBatch` and `saveDefaults` discard server error details** — `useAgentSpawn.ts:135-160` (parses `data.detail` then drops it; `lastResult` never set — a rejected batch renders "N failed" with no reason anywhere), `:176-197` (`saveDefaults` checks nothing — defaults silently fail to persist). Siblings of the filed 422 finding. (high)

### Data/CRUD layer

The fan-out produced a per-hook catalogue (contract / abort / error-surface / refetch / validation) — reproduced in compressed form: **throwing contract:** useWiki only (best in class); **mixed:** useSkills (4 of 12 mutations throw), useConfiguration (`{ok,errors}` for saves, swallows elsewhere), usePipelineExecutions (approve/reject throw); **swallow-everything:** useSourceControl, useMemory, useWorkflows, useMcp, useCronJobs, useRules, useIntegrations, useProjects, useCodeGraph, useTraces. **Abort/stale guards:** useFileChanges (reference implementation, with payload validation), dashboard pollers, useTokenTimeSeries, useMetricSnapshots only. **Validation:** useFileChanges only, fully.

- **[IMPORTANT] useSettings: write-before-fetch race clobbers remote UI settings; the intended gate is dead code** — `useSettings.ts:171` (`initialized` ref written at `:184`, never read), `:217-225` (persist effect gated only on first render), `:174-187`: a settings change before `/api/config/ui-settings` resolves PUTs localStorage/defaults over the server copy; the resolving fetch then clobbers the user's change and re-persists the reversion. (high)
- **[IMPORTANT] useSourceControl: cross-project data bleed on project switch** — `useSourceControl.ts:169-289, 493-514`: the `stale` flag guards future invocations only; in-flight responses for the old project setState into the new view on a 5s poll cadence. (high)
- **[IMPORTANT] useWorkflows internal refetches drop caller filters — soft-deleted pipelines vanish after any toggle** — `useWorkflows.ts:97-252, 303` call bare `fetchWorkflows()` while PipelinesTab loads with `include_deleted: true`; restore becomes undiscoverable until reload. Latent twin in useRules. (high)
- **[IMPORTANT] Detail-selection races in useWorkflows/useCronJobs** — `useWorkflows.ts:263-271`, `useCronJobs.ts:119-133, 239-246`: A-then-B selection can display A's detail/runs under B; no id comparison before setState. (med-high)
- **[IMPORTANT] Skills search: clearing the query doesn't cancel the pending debounce** — `useSkills.ts:253-280` + `SkillsPage.tsx:212-220`: clear → `refreshSkills()` → the armed timer fires anyway and overwrites the restored list with stale search results (distinct from the filed missing-abort item). (high)
- **[IMPORTANT] useFiles git status fetched once per project, never invalidated** — `useFiles.ts:155-165, 384-396`: saving (which changes git state) never refreshes; the Diff button gates on stale status — a freshly-dirtied file offers no Diff, a reverted one still shows modified. Save success also doesn't clear a prior tab `error`. (high)
- **[IMPORTANT] useTraces fetch failure silently empties the list with no error surface** — `useTraces.ts:50-56, 106-117`: a 500 renders as "no traces", wiping loaded data; the hook returns no `error`. (high)
- **[IMPORTANT] useTokenTimeSeries inherits the data-wipe-on-failed-poll defect** — `useTokenTimeSeries.ts:411-419` (`setData(null)` on error): include in the uniform fix with the filed dashboard hooks; `useMetricSnapshots` (`useMetrics.ts:139-149`) is the in-repo keep-stale-data template. (high)
- **[IMPORTANT] useMcp.callTool parses the body without checking `res.ok`** — `useMcp.ts:259-264`: FastAPI error envelopes become `{success: undefined}` — the tool-runner UI gets neither success nor error. (high)

## Findings — Nits

- **[NIT] tool_status update also keeps stale `tool_name`/`server_name`** — `handlers.ts:263-270`; sibling of the filed late-`arguments` drop; a call created as `"unknown"` is never repaired.
- **[NIT] `Record<string, any>` param contracts across the useChat modules** — `actions.ts:36`, `handlers.ts:25`, `lifecycle.ts:14`, `sessionViewing.ts:29-33`: ~60 destructured params per module are `any`; mis-wiring from `useChat.ts`'s giant call sites compiles clean. The transport layer (`transportTypes.ts`) shows the typed pattern.
- **[NIT] `clearViewingSession` can leave `isLoadingMessages` stuck true** — `sessionViewing.ts:152-154, 207-296`; masked by the current caller's immediate `switchConversation`; broken contract for any direct API consumer.
- **[NIT] `onChatClearedRef` never registered — `chat_cleared` events are dead wiring** — `callbacksState.ts:52-55`; multi-client clears silently leave stale transcripts.
- **[NIT] `loadPersistedConversationId` reads the db-session key** — `sessionPersistence.ts:9-15`; alias makes the reconciliation check redundant/misleading.
- **[NIT] `uuid()` fallback dereferences `crypto` outside its own guard** — `conversationPersistence.ts:38-49`.
- **[NIT] useWebSocketEvent never sends `unsubscribe`** — `useWebSocketEvent.ts:128-131`; the server keeps streaming dropped types for the socket's life (an unsubscribe handler exists server-side, unused).
- **[NIT] handleTokenEvent fallback treats a per-event delta as the session total** — `transportUsageEvents.ts:307-316`; dead today (all emitters include `session_totals`), wrong if ever live.
- **[NIT] Unvalidated casts in proxy handlers and REST token fetch** — `transportProxyEvents.ts:44, 97, 198`, `useSessionTokenEvents.ts:53`; contrast the hardened `transportUsageEvents.ts:46-113` in the same router.
- **[NIT] useDialogFocus Escape doesn't stop propagation** — nested-dialog double-close hazard, latent (no nested pair today).
- **[NIT] useConfirmDialog promise never settles if the host unmounts while pending** — `useConfirmDialog.tsx:20-30`; awaiting callers hang.
- **[NIT] Empty-session token poll re-enters loading state every 30s** — `useSessionTokenEvents.ts:32-36, 102-104`; spinner flicker.
- **[NIT] `handleCliSessionSendResult` clears the queued-message notice for unrelated results** — `transportProxyEvents.ts:304-306`.
- **[NIT] `useTmuxSessions` is dead code containing a reconnect-after-unmount leak** — zero consumers repo-wide; the same onclose-reschedules-after-cleanup bug as the live transports, pre-installed for whoever wires the tmux UI next. Delete or fix before reuse.
- **[NIT] `totalMessages` mixes parsed-message and rendered-group units** — `useSessionDetail.ts:465, 704`; the "N messages" figure drifts during live streams.
- **[NIT] `hist-${Math.random()}` fallback id breaks upsert-by-id** — `useSessionDetail.ts:91`; one id-less server row turns dedup into a duplicate generator (`renderedTotal` never shrinks).
- **[NIT] `refreshPageOne` cursor logic contradicts its own comment** — `useSessionCatalog.ts:150-156`; phantom "load more" affordance.
- **[NIT] `moveTaskToStage` rollback clobbers interim WS-delivered state** — `useTasks.ts:573, 617-622`; transient (latent while unwired).
- **[NIT] `useAgentRuns`: aborted requests still clear `isLoading`; non-OK blanks the list while network errors keep it** — `useAgentRuns.ts:46-80`; inconsistent failure semantics.
- **[NIT] `useVoiceStatus` warmup polling strands on one failed fetch and can double-loop on flag changes** — `useVoiceStatus.ts:93-168`.
- **[NIT] `useStagesRegistry` module-level cache never invalidated** — stage-registry changes invisible until reload.
- **[NIT] `parseColonCommand` resolves duplicate tool names to an arbitrary server** — `useColonAutocomplete.ts:196-204`.
- **[NIT] useMemory search path is dead and bypasses normalization; backend `to_dict` lacks `importance`** — `useMemory.ts:211-239` + `memory/protocol.py:177-195`; if ever wired, `importance * 100` crashes on undefined.
- **[NIT] `useSkills.filters.search` is a dead field; `useFiles.closeFile` leaves `activeFileIndex = 0` on empty; `UpdateCronJobRequest` omits `run_at` (once-jobs can't be re-timed); useMcp WS refresh flickers `isLoading`; useSettings doesn't clamp fontSize/theme from localStorage; `(data: any)` on trace events and `as any` graph link endpoints** — mechanical one-liners, sites as named.

## Systemic patterns

1. **Async work guarded by state read before the last await.** The PTT mic Blocker, the useTasks headers-time version check, useSettings' dead `initialized` ref, useVoiceStatus's shared cancel boolean, and every stale-response race share one root: the guard is evaluated once, then awaits intervene, then side effects run unconditionally. The repo owns the right idioms — `requestVersionRef`, `cancelled` flags, generation refs — and always misses the final await boundary. A shared `useAbortableFetch`/generation helper closes the class.

2. **Synchronous teardown vs asynchronous `onclose`.** Three independent surfaces (useWebSocketEvent, the chat transport, dead useTmuxSessions) tear down with flag/ref resets that the later-firing close event doesn't see, resurrecting sockets after unmount. Socket teardown must detach handlers (or flip a per-socket disposed flag) *before* `close()`.

3. **WS results uncorrelated with requests.** `attach_to_session_result`, `session_continued`, `send_to_cli_session_result`, and the pending_approval bypass are applied by frame type alone; every session-identity hijack above stems from it. REST paths got `viewRequestSeqRef`; the WS paths need pending-target refs / `source_session_id` checks.

4. **Swallow-to-sentinel is the house error contract, by accident.** ~70% of mutations return null/false with console-only logging; useWiki throws everywhere; useSkills throws on a third of its mutations; useConfiguration invents `{ok,errors}`. Callers wrap try/catch around functions that never throw. One `fetchJson` helper (res.ok + FastAPI `detail` extraction + abort) and one contract would eliminate the class — and most of the components-a/b silent-failure findings with it.

5. **Recovery mechanisms with deterministic blind spots.** Reconnect gap-recovery is per-channel and each has a hole: chat backfill loses its watermark after viewing, the token `since` poll skips same-second events and its dedupe key never matches across serializers, context usage doesn't recover at all. A single sequence-based recovery contract (the chat `after_seq` design, with the DB row id as identity) applied uniformly would replace three ad-hoc schemes.

6. **Web Audio resources released by suspension or abandonment, not closure.** Both voice hooks leak AudioContexts on failure/unmount paths; only the happy path closes properly — and the browser's hard context cap converts the leak into permanent TTS/PTT death.

7. **Dead code with green tests.** `useTasks` (841 lines + the strongest test suite in the directory), `useTmuxSessions` (with a pre-installed leak), the useMemory search path, `chat_cleared` wiring — test investment pinned to unwired surfaces while the live equivalents carry the real defects.
