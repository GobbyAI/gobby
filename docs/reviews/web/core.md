# Review: web core (lib, styles, contexts, utils, types, setup, App)

- **Scope:** `web/src/lib/`, `web/src/styles/`, `web/src/contexts/`, `web/src/utils/`, `web/src/types/`, `web/src/setup/`, `web/src/App.tsx`, `web/src/main.tsx` (~10k non-test lines; tests cross-referenced).
- **Reviewer:** Claude Fable 5 — 5-agent fan-out (App/contexts/types; chat mapping + provider models; task lib + small lib + utils; styles design audit; setup wizard) + synthesizer verification of every Blocker against source
- **Commit / branch:** `a08024c28` / `0.5.0`
- **Summary:** 4 Blocker · 46 Important · 24 Nit — the setup wizard is the weak spot (LAN-exposing bind, password on argv, failed installs reported as success, every failure message visible for 300ms), App.tsx accumulates registry/hydration drift, the light theme fails AA on its amber lane, and the mapping/normalization libs default to silent-drop and fail-open. Strong verified-clean notes: tokens.css has full dark/light parity with the locked palette hues exactly right; the reduced-motion override genuinely covers everything; the color modules are exemplary and in exact TS↔CSS sync; timestamp parsing in chat mapping has no tz landmine.

## Findings — Blockers

### [BLOCKER] Tailscale step binds the unauthenticated daemon to 0.0.0.0 (LAN exposure)

- **Where:** `web/src/setup/steps/Tailscale.tsx:55` (`setBindHost("0.0.0.0")` in the "Yes, configure tailscale serve" path) → `web/src/setup/utils/config.ts:50`
- **Failure mode:** `bind_host` is the uvicorn bind for the daemon HTTP server (`runner_lifecycle.py:132`), and the UI host follows it. The HTTP server is explicitly unauthenticated ("Local-first version: no platform auth", `servers/http.py:5,52`). `tailscale serve` proxies to 127.0.0.1, so the 0.0.0.0 bind isn't even needed for the stated goal. The prompt says "Expose Gobby's web UI over Tailscale?" but the action exposes the full daemon API — agent spawning is arbitrary code execution — on every interface including untrusted LAN/Wi-Fi. The macOS pf firewall is a separate, skippable, earlier step whose result is never checked; Linux gets no automated firewall at all.
- **Minimal fix:** Drop `setBindHost("0.0.0.0")` entirely; if a non-local bind is ever required, bind to the Tailscale interface IP only and require `firewall_configured`.
- **Confidence:** high — synthesizer-verified the call and the unauthenticated-server claim.

### [BLOCKER] FalkorDB password passed on argv to `gobby install`

- **Where:** `web/src/setup/steps/Services.tsx:43-49` (`args.push("--falkordb-password", password)`), consumed at `src/gobby/cli/install.py:382-387`
- **Failure mode:** The typed secret sits in the child process's command line — world-readable via `ps`/`/proc/<pid>/cmdline` for the full install duration (up to 120s of Docker pull). The sibling Integrations step already does this right via `secrets set --stdin` (`Integrations.tsx:42-47`); both tests enshrine the argv contract so it won't regress away on its own.
- **Minimal fix:** Add `--falkordb-password-stdin` to `gobby install` and pass via `spawnSync`'s `input`; update both tests.
- **Confidence:** high — synthesizer-verified.

### [BLOCKER] Configuration step runs a full `gobby install`, ignores the result, and reports "Configuration saved."

- **Where:** `web/src/setup/steps/Configuration.tsx:39-40` (`runGobby(["install"], { timeout: 30000 })`, return value dropped; comment claims "DB init + config")
- **Failure mode:** Flagless `gobby install` sets `all_flag = True` (`cli/install.py:505-517`) — a *full* install: hooks for all detected CLIs, daemon setup/migrations, embedding setup, and Docker installs. (1) If it exits non-zero or is killed by the 30s timeout mid-migration, the wizard still marks the step complete and prints "Configuration saved." (2) The later CliHooks "select which CLIs" step is illusory — hooks for every CLI were already installed here. (3) 30s is far too small for the work the command can do.
- **Minimal fix:** Call a narrow command (or add `--config-only`), check `r.success`, surface failure with retry/exit before marking the step complete.
- **Confidence:** high — synthesizer-verified the bare call and dropped result.

### [BLOCKER] Pre-hydration project flip fires `set_project`, permanently reassigning the persisted session to the default project

- **Where:** `web/src/App.tsx:409-415` (ungated sync effect: `sendProjectChange(effectiveProjectId)`), `:275-281` (`effectiveProjectId` falls back to `defaultProjectId` before `/api/config/ui-settings` hydration supplies the real selection); backend `src/gobby/servers/websocket/handlers/session_config.py:345-352` (`handle_set_project` pauses the chat and writes `project_id=new_project_id` to the DB session)
- **Failure mode:** Unlike its sibling effects (`:378-393`, `:396-407`), this one has no `projectReady`/`uiSettingsLoaded` guard. With the WS open and a persisted conversation restored, the default-project flip cancels the active chat, sets `status="paused"`, and rewrites the session row's `project_id` to the default project — then unregisters the in-memory session, so when hydration lands milliseconds later, the corrective `set_project(realProject)` only sets `_pending_projects` and the DB row is never written back. The conversation vanishes from its project's session list and does not self-heal.
- **Minimal fix:** Gate the effect on `projectReady`, like its siblings.
- **Confidence:** high on mechanism (traced end-to-end) — synthesizer-verified the ungated effect and the server-side rewrite; med on real-world frequency.

## Findings — Important

### Setup wizard

- **[IMPORTANT] Resume prompt unreachable; completed setup re-runs to a blank screen** — `setup/App.tsx:105-121` auto-jumps past Welcome for any valid saved step id, so "Resume / start fresh" is dead UI; after full completion, re-running renders an empty `<Box/>`. (high)
- **[IMPORTANT] Failed secret store never shown** — `Integrations.tsx:153-157` sets `error` then switches to the menu phase, which doesn't render `error`; user proceeds believing the GitHub/Linear token was saved. (high)
- **[IMPORTANT] Root-executed firewall script staged in world-writable /tmp with predictable name** — `NetworkSecurity.tsx:73-80` (`gobby-fw-${Date.now()}.sh` then `sudo bash`); classic TOCTOU; the copy is unnecessary. (med)
- **[IMPORTANT] Firewall "Yes" always fails under npx distribution** — `NetworkSecurity.tsx:60-70`: `GOBBY_INSTALL_DIR` is set only by the Python `gobby setup` launcher; under `npx @gobby/setup` the script path is null → instant "failed". (high)
- **[IMPORTANT] Long-running spawnSync inside input handlers freezes the TUI; promised spinners never render** — `Services.tsx:87-89` (120s Docker pull), `Configuration.tsx`, `NetworkSecurity.tsx`, `Tailscale.tsx`, `Integrations.tsx`; the correct phase-then-useEffect pattern exists in ProjectDiscovery/CliHooks. (high)
- **[IMPORTANT] Step results — including failures — flash for 300ms before auto-advance** — the `finish(...)` + `setTimeout(onNext, 300)` pattern in six steps unmounts every error render almost immediately; combined with always-advance, real failures become invisible. (high)
- **[IMPORTANT] ProjectDiscovery records failed repos as initialized** — `ProjectDiscovery.tsx:48-58, 119-124`: failed `gobby init` paths persist as `projects` and flow into the Launch summary; green banner regardless. (high)
- **[IMPORTANT] PersonalWorkspace swallows init failure, always claims success** — `PersonalWorkspace.tsx:213-237, 303`. (high)
- **[IMPORTANT] Port commit: duplicates accepted; pre-existing bootstrap.yaml ports silently win over "defaults"** — `Configuration.tsx` patches ports only when they differ from hardcoded defaults, so an old install's ports survive a "use defaults" choice and Launch health-checks the wrong port for 30s. (high mechanics, med frequency)
- **[IMPORTANT] Launch ignores `gobby start` failure and celebrates regardless** — `Launch.tsx:25, 49-58, 87-124`: start result dropped, browser opened to a dead URL, "Setup complete!" unconditional. (high)

### App shell / types

- **[IMPORTANT] Hash routing is write-only** — `App.tsx:204-213`: no `hashchange`/`popstate` listener (repo-wide grep: zero); back/forward changes the URL but never the UI, and every tab click pushes a dead history entry. (high)
- **[IMPORTANT] Palette "New Chat" bypasses the #15703 mode-reset fix** — `App.tsx:668-669` passes raw `startNewChat` while the conversations path uses `handleStartNewChat` (the documented fix at `:631-645`); a palette-opened chat can inherit the prior session's bypass/plan mode. (high)
- **[IMPORTANT] Mode-restore effect silently skips when the session row arrives late** — `App.tsx:649-659`: `webChatSessions` is read but not a dependency, and WS catalog refreshes never toggle `isLoading`, so the restore is dropped. (med)
- **[IMPORTANT] UI-settings hydration clobbers selections made during load** — `App.tsx:339-357` applies fetched values unconditionally; pre-hydration changes are also never persisted (the persist effects gate on `uiSettingsLoaded`), so the overwrite wins on every future load too. (high mechanism)
- **[IMPORTANT] `handleKillAgent`/`handleExpireSession` omit the `VITE_API_BASE_URL` prefix** — `App.tsx:564, 587` vs 26 other call sites; in split-origin deployments the two destructive operations 404. (high)
- **[IMPORTANT] Error-boundary gaps: header, sidebar, all four modals, FilesProvider unprotected; no root boundary** — `App.tsx:712-964`, `main.tsx:14`: a render throw in any of those takes the whole app to a white screen with no fallback. (high)
- **[IMPORTANT] "Return to Chat" recovery button is a no-op when the crash happened on the chat tab** — `AppErrorBoundary.tsx:29-33, 102-115`: identical state → no `componentDidUpdate` → boundary never resets; chat is the default tab, so the likeliest crash site has a dead primary CTA. (high)
- **[IMPORTANT] Deep-link handoff state survives project switch** — `App.tsx:218-221, 206` vs the project-change effect at `:396-407` which resets only chat state; `initialTraceId`/`initialPipelineExecutionId`/`activityTabRequest` leak cross-project (sibling of the filed never-cleared `initialTraceId`). (med)
- **[IMPORTANT] Navigation registry drift siblings** — `appNavigation.tsx:22-43`: palette can't reach Projects/Integrations/Dashboard; `dashboard` is hash-only (valid + rendered but in no sidebar/palette). Four hand-maintained copies of the tab list. (high)
- **[IMPORTANT] `accept_edits` ChatMode: two restore paths disagree and the selector can't render it** — `types/chat.ts:4, 33-53, 60-65` vs `useContinuationRestore.ts:20-25`: App's path normalizes it away; continuation restores it verbatim into a mode the registry has no entry for. (med-high)

### Chat mapping / provider models

- **[IMPORTANT] User messages containing the substring "tool_result" are silently dropped** — `chatMessageMapping.ts:476`: substring match over raw JSON, before the proper block-array parse; pasted logs or questions about tool results vanish from transcripts. (high)
- **[IMPORTANT] Orphaned tool results discarded; unmatched calls stay "calling" forever** — `chatMessageMapping.ts:469-473, 614-617, 381-397`: three silent drop paths (window-boundary orphans, unmatched `tool_use_id`, flush-interleaved results) leave permanent spinners; `findToolCallById` (`:265`) also re-pairs duplicate ids with completed calls. (med)
- **[IMPORTANT] Hook feedback overwrites the status of a *successful* tool call** — `chatMessageMapping.ts:415-435, 478-486`: `markLatestToolCallError` rewrites the last call to `error` unconditionally, masking real results with hook text; only the still-`calling` case is tested. (med-high)
- **[IMPORTANT] Fallback message ids can collide in `mapApiMessages`** — `chatMessageMapping.ts:624`: `msg-${state.result.length}` computed before the pending assistant flushes; downstream id-keyed Maps drop a message. (med; latent — rendered API always sets ids)
- **[IMPORTANT] Two consumers use the wrong mapper, resetting all restored timestamps to "now"** — `useChat/actions.ts:165-173` and `sessionViewing.ts:266-278` feed `/api/chat/{id}/messages` rows (which carry `created_at`) to `mapRenderedMessageToChatMessage` (which reads `timestamp` → `?? Date.now()`); `lifecycle.ts:97`/`transportLifecycle.ts:92` use the correct `mapStoredChatMessage`. (high)
- **[IMPORTANT] Blockless `system`/unknown-role messages silently dropped** — `chatMessageMapping.ts:634-640`: role dispatch handles only user/assistant/tool; the backend renderer genuinely emits `role: "system"` groups. (med)
- **[IMPORTANT] Catalog fetch discards the stale-but-valid cache on failure** — `providerModels.ts:74-95`: TTL lapse + transient failure → `[]` despite `cachedModels` holding the last good catalog; every model picker empties on a daemon blip. (high)
- **[IMPORTANT] No runtime validation of `/api/providers/models`** — `providerModels.ts:86` blind cast; one malformed entry throws inside `Array.find` in the chat input area and the bad payload is cached for 5 minutes. (med-high)
- **[IMPORTANT] Claude strength ranking lets version dominate family** — `providerModels.ts:405-417`: `familyScore` (300/200/100) is swamped by `versionScore` (~40,000+), so `haiku-4-5` outranks `opus-4-1` and becomes the auto-selected default; Codex/Gemini parsers correctly scale tier ×10,000. (high)
- **[IMPORTANT] `fable` family missing from the Claude family list** — `providerModels.ts:405` (`["opus","sonnet","haiku"]`) while the backend ships `claude-fable-5` as a first-class model (`provider_model_defaults.py:11`); Fable ranks below Haiku and can never be auto-selected. No test covers it. (high)
- **[IMPORTANT] Backend-declared `is_default` effectively ignored** — `providerModels.ts:284-305, 561-562`: consulted only as a 4th-level tie-breaker; provider-declared defaults lose to label-parsing heuristics. (med-high)

### Task lib / small lib / utils

- **[IMPORTANT] `extractImageSrc` classifies any bare HTTPS URL or rooted path as an image** — `imageSources.ts:90-91` + `isSafeImageSrc:33-54`; ToolCallCard replaces the whole tool result with a broken `<img>` and the browser fires an unsolicited GET to an arbitrary host (IP/referrer disclosure). (high)
- **[IMPORTANT] `relativeTime` renders "NaNmo ago" on the normalizer's fabricated `''` timestamps** — `utils/formatTime.ts:1-13` (no NaN guard; sibling `formatRelativeTime` has one) + `taskNormalization.ts:409-410` (defaults missing timestamps to `''`, reachable via WS partial-payload append). Also poisons `PriorityBoard`'s date sort. (high lib gap)
- **[IMPORTANT] Three divergent "current stage" resolvers** — `taskNormalization.ts:369, 383-393` (direct-first; `position ?? index`), `taskState.ts:139` (state-first), `stageActions.ts:102-115` (positioned-first) vs backend `state_semantics.py:34-56` (state-first; `position or 0` + name tiebreak): board placement, display state, and badges can disagree with each other and the backend on the same payload. (high divergence, med impact)
- **[IMPORTANT] `normalizeStageRow` defaults `review_policy` to `'none'`** — `taskNormalization.ts:327-329`: most-permissive default while bundled work stages are `required`; `resolveAdvanceAction` would resolve backend-rejected `'complete'` actions the moment it's wired to a button. Sibling of the known `state: 'ready'` default. (med)
- **[IMPORTANT] Client attachment cap hardcodes the backend's *default*, not its configured limit** — `chatAttachments.ts:25, 105-114` vs configurable 1B–500MB (`config/features.py:285-290`); also reports the decimal limit in binary units ("95.4 MB limit"). (high mismatch, med frequency)
- **[IMPORTANT] `pruneTimeBoundLru` clock-skew guard disables the size bound too** — `timeBoundLru.ts:143-147`: one future timestamp skips LRU eviction entirely; the voice-prepare map can exceed its 128-entry bound and stale throttles persist. (med)
- **[IMPORTANT] `isValidGithubRepoSlug` rejects legitimate repo names containing `--`** — `githubRepo.ts:13`: GitHub forbids `--` in owners, not repos; ProjectSettings refuses valid slugs and TaskDetailTrace silently drops valid PR links. (med)

### Styles (design-system audit)

- **[IMPORTANT] Light-theme amber lane fails AA everywhere** — `tokens.css:287, 289`: computed ratios 3.25–3.96:1 for warning-foreground on every light surface, and 4.25:1 for white-on-amber; live consumers include the source-control stale/cleanup badges (10.4px text) and the solid amber config button. Dark passes at 6.7–7.3:1. Lower light warning-foreground to ~50% L and flip `--text-on-warning` to dark ink. (high)
- **[IMPORTANT] `.slider { outline: none }` leaves the font-size slider with zero focus indicator** — `settings.css:163-171`: unconditional, unlayered — beats the `@layer base` ring; no thumb focus styling either. (high)
- **[IMPORTANT] Merged-PR badge emits undefined `sc-badge--purple`** — `StatusBadge.tsx:9,19` vs `source-control.css:156-161` (no purple, and purple isn't in the locked palette); merged badges render unstyled; `sc-badge--sm` is also undefined while `--md` is defined and unreachable. (high)
- **[IMPORTANT] `.session-delete-btn` is 20×20px and invisible until hover** — `session-primitives.css:182-212`: below even the 24px WCAG 2.5.8 floor, no coarse-pointer promotion, destructive action. (high)
- **[IMPORTANT] source-control surface has zero coarse-pointer promotion** — `.sc-btn` ~30px, filter chips ~28px, detail-close ~24×32px, orphans toggle ~21px, `.app-menu-button` fixed 2rem width (`source-control.css`, `source-control-issues.css`, `app-shell.css:19-29`); `grep "pointer: coarse"` in those files → none. (high)
- **[IMPORTANT] Dark `.sc-badge--muted`/draft-label: grey text on info-blue tint, 4.28:1 and semantically wrong** — `source-control.css:161, 733-741`; the light theme already fixed this, dark was left behind. (high)
- **[IMPORTANT] Light accent and magenta badge text ride below 4.5:1 on tertiary/tinted surfaces** — `tokens.css:276, 293` consumers: error badge mix 4.37:1, accent mix 4.19:1, accent-on-tertiary 4.21:1, accent-on-tint 3.73:1. Light accent was tuned only against the two lightest surfaces. (med)
- **[IMPORTANT] State-token sibling set diverges from contract with inverted bare-token semantics** — `tokens.css:54-78, 282-306`: Info lacks `-foreground`/`-tint`/`text-on-info`; bare `--color-info` means *foreground* while bare `--color-warning` means *surface*; tailwind-theme maps both identically, so `text-warning` yields near-invisible 30%-L surface color. (high drift)
- **[IMPORTANT] Off-ladder typography throughout pre-migration style files; the brand title escapes the font-size slider entirely** — ~30 live `calc()` multipliers (0.65/0.7/0.8/0.85/0.9/1.1) across source-control.css/diff/base/sidebar/session-primitives, plus raw rem in `app-shell.css:41,142` (the one piece of chrome that ignores the density slider). The ladder test scans none of `web/src/styles/`. (high)
- **[IMPORTANT] `--color-review` (hue 200) and `--color-inactive` (hue 30, 5× the neutral chroma band) are unsanctioned palette extensions** — `tokens.css:69, 76-78`: the token file and .impeccable.md disagree; teach the contract or fold them in. (med)

## Findings — Nits

- **[NIT] FilesContext slice is dead wiring with live runtime cost** — CodePage has zero importers (repo grep), yet `FilesProvider` mounts unconditionally: a duplicate projects fetch on every load, a `beforeunload` listener, and an un-memoized context value pre-armed to churn per streaming token if ever consumed.
- **[NIT] No `<StrictMode>`** — `main.tsx:14`; the cheapest detector for exactly the lifecycle-bug class this codebase keeps accumulating (the WS teardown races were StrictMode-reproducible).
- **[NIT] Dead exported types** — `RenderedMessage`, `ProjectProps` (`types/chat.ts:153, 346`), `SessionFilters`, `ProjectInfo` (`types/sessions.ts:68, 75`); worse, live code re-declares looser local copies (`RenderedMessageLike`, `useFiles.Project`) that drift independently.
- **[NIT] Type honesty** — all-optional image ContentBlock (`{type:"image"}` with no payload typechecks); `ChatState`'s ~50 fields with independent booleans whose illegal combinations already produced #15681/#15703; `session_type` closed union in one file vs open string in another.
- **[NIT] `sessionsFilters` localStorage is global, not per-project** — project A's task-ref filter silently empties project B's list.
- **[NIT] ~300 dead lines in source-control.css** (`.sc-page*`, `.sc-overview*` blocks — zero TSX refs, greps shown) including one of the two `outline: none` kills; dead settings.css block carries the slice's only hard-coded hex (`%23737373`) and another `outline: none`; dead session-primitives classes include the focus-unsafe 20px kill button and a 9.6px badge; buttons.css documents five variants no component uses.
- **[NIT] Hard-coded dark shadow on `.sc-detail-panel` leaks into light mode** — `source-control.css:389` bypasses `--shadow-panel-left`.
- **[NIT] `--border-soft` duplicates the border literal instead of `var(--border)`** — `tokens.css:31, 260`.
- **[NIT] `display: flex` on a `<td>`** — `source-control.css:492-496` breaks fixed table layout.
- **[NIT] `context_length` is a dead field in the web catalog** — the context gauge reads usage events, not the catalog; repo grep shows zero consumers.
- **[NIT] `parseQwenModelInfo` regex `/gemini.*pro|pro/`** — alternation makes the gemini branch unreachable; any "pro" gets Gemini-tier rank.
- **[NIT] Label-keyed model dedup collapses distinct variants** — `-fast` suffixes strip to the same label and merge into one option.
- **[NIT] `ws-` random fallback id re-mints per mapping** — `chatMessageMapping.ts:33-38`; repeated id-less broadcasts would append duplicates (safe today; renderer always assigns ids).
- **[NIT] `getCanonicalTaskState` treats a `done` current stage as closed, overriding explicit `is_closed: false`** — `taskState.ts:162-163`.
- **[NIT] `isRecord` in taskNormalization accepts arrays** — phantom stage rows from `current_stage: []`; the sibling in imageSources excludes arrays.
- **[NIT] Dead/duplicate lib exports** — `getStepTypeColorVar` (PipelineEditor hardcodes the strings instead), unused chart constants, byte-identical `loadPersistedConversationId`/`loadPersistedDbSessionId` readers compared against each other as if distinct, unreachable fallback in `getCanonicalStageName`.
- **[NIT] Setup misc** — dead `readConfig`/`writeConfig`; Services y/n/p chooser is an invisible text field where typos silently mean "skip"; Integrations sets state during render (double-fire under StrictMode); stale "0.2.20" version fallback; Launch summary omits clawhub and reports the wrong bind host; "start fresh" resets only two fields (moot while unreachable).

## Systemic patterns

1. **The wizard's state machine always advances.** Every step persists `completed_step_id` on success *and* failure, failure output lives for 300ms, there is no Back/Retry, and three subprocess call sites discard results outright. The installer converts real failures into silent ones structurally, not incidentally — and its tests enshrine the bugs (argv password, bare install call; no resume/failure/unhealthy-launch coverage).

2. **Hand-maintained parallel registries.** Four copies of the tab list (valid-tabs / sidebar / palette / render chain), three copies of the mode set (`CHAT_MODES` / `normalizeChatMode` / `RESTORABLE_CHAT_MODES`), three current-stage resolvers (two frontend + backend), two `isRecord`s with different semantics, canonical types vs redeclared local copies. The filed `integrations` bug is one instance of a mechanism that recurs at least six more times in this slice. Derive, don't duplicate.

3. **Hydration-vs-interaction races from unguarded effects.** The one App effect that talks to the backend lacks the `projectReady` guard its three siblings have (the Blocker); the hydration fetch applies results unconditionally; pre-hydration user changes are neither honored nor persisted.

4. **Silent drop / fail-open as the default policy at trust boundaries.** chatMessageMapping has five no-log drop paths; `isSafeImageSrc` accepts any https URL; `review_policy` defaults to the most permissive value; the LRU clock guard fails open on its size bound; the provider catalog discards its own good cache on failure. Normalizers and guards in this codebase default toward acceptance/fabrication when uncertain — the unsafe direction.

5. **Two generations of CSS coexist; enforcement watches the wrong directory.** buttons/accessibility/tokens follow the contract; source-control/settings/session-primitives predate it (off-ladder calcs, outline kills, 20px targets, zero coarse promotion). The only typography test scans chat/activity CSS — `web/src/styles/` escapes it entirely, which is precisely where the violations live. A stylelint pass over styles/ would have caught the bulk mechanically.

6. **Light theme tuned only against the lightest surfaces.** Light accent, warning-foreground, and the 10% badge mixes pass on bg-primary then slide under AA on bg-secondary/tertiary and inside tints; dark has 1.5–3× more headroom everywhere. "Equal polish dark and light" currently fails at the contrast layer.
