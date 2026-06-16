# CodeRabbit Triage — `.gobby/plans/coderabbit.md` (437 findings)

## Context

`.gobby/plans/coderabbit.md` is a 277KB dump of **437 CodeRabbit findings** for the
`0.5.0` branch. They were generated before a large run of rapid changes, so a high
fraction are expected stale. The task: validate every claim against **current** repo
state, decide `fix`/`no-fix` with evidence, then fix only the still-valid ones.

Triage was run as a 37-agent read-only verification workflow (one agent per ~12
findings). Each agent read the cited file at the cited location in the current tree,
confirmed against real code (gcode/Read), and returned a structured verdict. Headline:

| Decision | Count |
|---|---|
| **fix** (still valid; incl. resolved fence #385) | 217 |
| **no-fix** (stale / already-fixed / false-positive / harmful; incl. resolved fence #1244) | 220 |
| **Total** | **437** |

Both fence items are **resolved per your call**: **#385 → fix**, **#1244 → no-fix**. (They already sit inside the 217/220 above — fence was an overlay, not a separate bucket.)

The 217 `fix` rows contain heavy duplication (CodeRabbit re-emitted the same issue
across batches) → roughly **110-130 unique edits**. The 220 `no-fix` rows are the
staleness you flagged: shifted line numbers, already-applied fixes, `asyncio_mode="auto"`
making marker findings moot, deliberate refactors CodeRabbit wants reverted, and
archived/generated files excluded by policy.

The complete finding-by-finding decision table (all 437, grouped by area, exact
columns) is the **Appendix** at the bottom. The body below is the actionable plan.

---

## Tier 0 — Significant fixes (security / correctness) — do first

These 7 are real defects, several security-relevant. They are the priority.

| # | File | Issue |
|---|---|---|
| 490, 774 | `web/.../activity/memory/KnowledgeGraph.tsx` | **XSS.** `nodeLabel` (and `linkLabel`, 774) interpolate `e.name`/`e.entity_type`/properties/`link.type` raw into HTML strings rendered as react-force-graph tooltips. HTML-escape all dynamic fields. An `escapeHtml` util already exists in `CodeGraphExplorer.tsx` to reuse. |
| 710 | `src/gobby/servers/routes/mcp/endpoints/server.py` | **Unauthenticated secret leak.** `list_mcp_servers` returns `config.env`/`config.headers` (API keys/tokens) and `/api/mcp/` is in `_PUBLIC_PREFIXES`. Drop `env`/`headers` from the response. (Note: finding 836 argues these hold `$secret:` refs not raw creds and the web edit UI needs them — **resolve the tension**: strip raw values / keep only `$secret:` refs, do not blanket-expose. See Open Question 1.) |
| 1156 | `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py` | **Path traversal.** L191 appends absolute task-mentioned paths directly; `_read_referenced_file_context` reads them. An absolute ref like `/etc/passwd` leaks files outside the repo. Resolve and confirm within `base_path` before adding. |
| 302 | `src/gobby/mcp_proxy/tools/sessions/_handoff.py` | **Cross-project guard bypass.** L225 `caller_project_id = project_id or project_ctx.id` lets a client-supplied `project_id` override trusted middleware context, defeating the guard at L291-306. Use the project context as authoritative. |
| 367 | `src/gobby/communications/adapters/telegram.py` | **Poll stall.** `poll()` appends every `update_id` to `_pending_update_ids` but only acks message-yielding ones; head-of-queue loop stalls forever on a leading non-message update, refetching it. Append only when `msg_list` is non-empty. |
| 279 | `src/gobby/workflows/state_manager.py` | **Crash.** `record_edited_file` does `json.loads(row['variables'])` but `session_variables.variables` is JSONB → psycopg returns native dict → `TypeError`. Reuse the dict|str decode from `get_variables()`. Same bug at L309/L409. |

(`significant_fix` count = 7; the 7th row is 774, the broader KnowledgeGraph escape that also covers `linkLabel`.)

---

## Tier 0.5 — High-value moderate correctness bugs

Not flagged "significant" but real correctness/security, worth grouping with Tier 0:

- **Fail-open close gate** — `_lifecycle_close.py` 196/239/241/310: on `get_variables` failure the code defaults `session_vars={}`, making `target_task_has_edits()==False` and **bypassing the commit gate**; also clobbers `claimed_tasks`/`task_edited_files` via wholesale `current.update()`. Fail **closed**; source vars from the task's owning session (`get_claimed_session_id`), refetch fresh before merge.
- **Hooks dedupe replay/TOCTOU** — `servers/routes/mcp/hooks.py` 318/320: replayed `PreToolUse`/`Stop` envelopes are auto-approved (discards the real gating decision), and the check-then-set is racy. Needs atomic claim-and-set storing a terminal result.
- **Summary staleness hash** — `_terminal.py` 304 + `summarize.py` 251: `_summary_digest_metadata_matches` only checks hash non-empty + turn-count; injected-context-only changes alter the hash (251) / changed content with equal turn count reuses stale summary (304).
- **typed_json.py shape crashes** — 202/204/324/326 (missing `isinstance` guards → `AttributeError` on non-dict messages; dropped tool_use_id correlation; `if func_response:` drops valid empty outputs) + **253 string-corruption bug** (`lstrip("\\n")` strips leading `n` chars, `"nice"→"ice"`).
- **Isolation async-blocking + races** — `isolation_clone.py`/`isolation_worktree.py`: sync `storage.delete()` inside async `cleanup_environment` (452/520/682, 458/536/706) and per-instance partial-state attrs racing concurrent `prepare_environment` (450/456); `generate_branch_name` produces invalid git refs (454/524/694, uuid fallback 528).
- **tmux pty_bridge race** — 365: check-then-store across released lock leaks fd/proc on concurrent same-id `attach()`.
- **stage_review converged** — `_stage_review.py` 1160: sets `new_state='ready'` on `has_suggestions` alone, ignoring `converged`; should stay `needs_review` when converged.
- **bool coercion** — `endpoints/server.py` 840: `bool(body.get('enabled', True))` makes `bool('false')==True`; validate `isinstance(bool)`.
- **memory recall double-budget** — `recall.py` 312: synthesis + selection each use full timeout (~2× budget); share one budget.
- **dream cancelled-run status** — `dream/service.py` 1022: `except CancelledError: raise` leaves runs `running` forever; persist terminal status first.
- **orphaned spawned run (#385, resolved fence)** — `spawn_agent/_implementation.py`: a `run_storage.start` failure leaves a `pending` run with a live tmux agent that `cleanup_stale_runs` never reconciles (it only targets `running`). Treat as fatal + `cleanup_failed_spawn`.

---

## Fence cases — RESOLVED (per your call)

1. **`#385` → FIX** — `spawn_agent/_implementation.py`: if `run_storage.start(run_id)` raises, the except logs+continues, `start_skipped` stays `False`, leaving a run `pending` with a **live tmux agent**; `cleanup_stale_runs` only targets `running`, so it's never reconciled. Fix: treat the `start` failure as fatal + call `cleanup_failed_spawn`. Folded into Tier 0.5 (moderate correctness).
2. **`#1244` → NO-FIX** — `settings/.../AutomationWorkflowsSection.tsx`: leave `escalation_webhook_url` unmasked. Backend deliberately excludes `_url` from secret detection, the sibling `webhook_base_url` is also plain text, and masking whole URLs hurts verifiability.

---

## Deduplication (do before editing)

~217 fix rows = ~110-130 unique edits. Duplicate rows are tagged inline in the appendix
table as `dup→#N` (canonical finding), covering **59** fix and no-fix duplicates. The
biggest fix-side clusters — collapse each to one edit:

- `IntegrationsTabData.deleteIntegrationChannel` → `IntegrationApiError`: **486, 588, 758, 896**
- `IntegrationPlatformIcon` return type + exhaustive default: **484, 754, 892**
- `IntegrationsTabModel` NaN guard: **592, 762, 900**
- `ChannelDetailPanel.copyWebhookUrl` try/catch: **584, 750, 888**
- `McpServerFields` connect_timeout fallback→30: **488, 600, 920**
- `McpTabActions` `{success:false}` on parse fail: **766, 924**
- `PipelinesDefsList` hoist `stepCount`: **502, 624, 790, 952**
- `KnowledgeGraph` XSS escape: **490, 774**; missing `.catch` infinite-loading: **492, 770**
- `MemoryTabList` aria-label `previewContent`: **494, 612, 936** (+ `PREVIEW_LENGTH` const 940)
- `KeyValueField` duplicate-key data-loss: **439, 884** (same class: `McpToolsSection` 1078, 1252)
- `configuration_import_export` databases-non-dict→422: **198, 247, 316**
- `local_openai_warmup` best-score selection: **200, 249, 322**
- `isolation_clone` async delete: **452, 520, 682**; `isolation_worktree` async delete: **458, 536, 706**
- `generate_branch_name` normalization: **454, 524, 694**
- test `db: HubDatabase` hints: **552, 556, 718, 848, 852**
- `test_graph_edge_weighting` module `pytestmark`: **1200, 1204, 1208**
- `mcp.py` storage preserve-zero/OAuth: **462, 464, 466**
- `server_registry` config-copy + docstring: **460, 540, 828**
- `text_legacy_symbols_removed.test.ts` exec→test + return types: **560, 722, 726, 730, 734**
- docs `source-tree.md` MD040 fences: **227, 294**

---

## Conflicts — same issue, opposite verdicts (resolve before editing)

CodeRabbit re-emitted some issues with wording that led verifier agents to opposite
calls. These need a single decision, not blind application of either row:

- **#53 (no-fix) vs #190 (fix)** — `require-rust-skill.yaml` `.cargo/config` matcher.
  #53: current `.endswith(('.cargo/config', '.cargo/config.toml'))` correctly matches
  both root and nested. #190: that same form false-matches `foo.cargo/config` (no path
  boundary). **Both are partly right.** Correct fix = boundary-aware match (exact
  `.cargo/config` OR `endswith('/.cargo/config')`), which covers repo-root *and* avoids
  the `foo.cargo/config` false positive. *Recommend: fix (boundary-aware), not a literal
  revert to leading-slash-only.*
- **#710 (fix) vs #836 (no-fix)** — `endpoints/server.py` env/headers exposure. See
  Open Question 1. *Recommend: strip raw values, keep `$secret:` refs.*
- **#1200/#1204/#1208 (fix) vs #856/#1212/#1216/#1220/#1224 (no-fix)** —
  `test_graph_edge_weighting.py` markers. Split 3-fix / 5-no-fix on the same markerless
  file. *Recommend: fix — add one module-level `pytestmark = pytest.mark.unit` (sibling
  convention, harmless); low value. The `@pytest.mark.asyncio` half is genuinely moot
  under `asyncio_mode="auto"`.*
- **#986 (fix-code) vs #1128/#1132 (fix-docs)** — `relations.py` `upsert_imports`/
  `upsert_calls` count contract. #986 wants `return cursor.rowcount`; #1128/#1132 want
  the docstring changed to "count attempted". Same issue, opposite remedy. *Recommend:
  docstring fix (#1128/#1132) — the return value is unused in prod per #986's own note,
  so changing behavior is unwarranted risk.*

## Implementation plan

Execute as a small set of themed tasks (each: claim task → edit → focused validation →
commit `[gobby-#N] <type>: <summary>` → close with `commit_sha`). Suggested order:

1. **`fix`: security & correctness (Tier 0 + 0.5)** — Python + the two web XSS edits.
   Highest risk; smallest blast radius per edit. ~25 unique edits.
2. **`fix`: web behavioral defects** — unhandled promise rejections / missing `.catch`,
   NaN guards on numeric config inputs, `useEffect` reset on changed prop
   (PipelineEditor 498, SkillsHubDetail 504, ValidationDetectionEditor 1086), data-loss
   key guards (KeyValueField/McpToolsSection). ~30 unique edits.
3. **`refactor`/`fix`: web polish** — `stepCount` hoist, `window.alert`→inline error
   (PipelineEditor 944), click-outside on AddStepButton (786), keyboard-accessible
   step header (500), aria-label previews, valid return-type annotations, secret-field
   in edit mode (482). Skip the no-fix "return type" churn rows.
4. **`fix`/`refactor`: Python hygiene** — `exc_info=True` on logged broad-catches,
   `Any`→concrete/Protocol types (isolation_factory 690, dream/cron 1164, workspace_context
   257/259), docstrings, dedup catalogs (provider_model_defaults 314), CHECK constraints
   in **new** migrations (not edits to shipped ones — see no-fix 544/1038/1184).
5. **`test`: test improvements** — `db: HubDatabase` hints, strengthen assertions
   (test_llm_routes 344, test_communications_routes 429, test_storage_tasks 860),
   behavioral rewrites of source-introspection tests (340), valid marker additions
   (960, 1192, 1200-cluster, 425). Skip no-fix marker rows.
6. **`docs`: documentation** — `GOBBY_TEST_PROTECT=1` prefix (225), MD040 fences
   (227/294), tool-count reconciliation (966), `sse` in url-required cell (229),
   pyproject CVE comment expansion (82), GEMINI.md duplicate heading (1108).

**Scope guards baked in by triage:** do **not** touch `.gobby/plans/completed/*`,
`.gobby/memories.jsonl`, `.gobby/wiki/.gwiki/*` (no-fix per policy/memory); do **not**
re-add the legacy `gobby-conversation-id` localStorage key (29, deliberate refactor);
do **not** edit shipped migrations 283/286/289 (constraints belong in new migrations);
do **not** revert `version_pins.py` (192) or the torch CVE ignore (816).

After all fixes land, **delete `.gobby/plans/coderabbit.md`** (processed) and record
reusable lessons via `gobby-review-learning.record_review_lesson` for the confirmed
patterns (preserve-explicit-zero, fail-closed-on-lookup-failure, escape-before-HTML,
JSONB-not-json.loads, async-wrap-blocking-storage-calls in cleanup).

## Open questions (resolve during execution, recommendations given)

1. **#710 vs #836 secret exposure** — confirm `env`/`headers` are stripped to
   `$secret:` refs only (not raw values) so the web edit UI keeps working while no raw
   creds ship over the public prefix. *Recommend: strip raw, keep `$secret:` refs.*
2. **Fence #385 / #1244** — see Fence section; defaults: fix 385, no-fix 1244.

## Verification

- Python: `GOBBY_TEST_PROTECT=1 uv run pytest <touched test files> -q`, plus
  `uv run ruff check src/ && uv run ruff format --check src/` and `uv run mypy src/`
  on touched modules. Per the project gate, validation runs on commit.
- web/: `cd web && npm run lint && npx tsc --noEmit && npm test` (vitest runs from
  `web/`). Target the touched component tests.
- Security spot-checks: confirm KnowledgeGraph tooltip escaping with a crafted entity
  name; confirm `/api/mcp/` response carries no raw secret values unauthenticated;
  confirm a `/etc/passwd`-style task ref is rejected by `_lifecycle_validation`.
- Per-group focused validation before each commit; do not run the full 15k-test suite.

---

## Appendix — full decision table (all 437)

Grouped by area (largest first). `# ` = source line in `.gobby/plans/coderabbit.md`.
Columns are the required `# | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix`.

#### web — 128 findings (79 fix / 49 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 29 | no-fix | web/src/lib/sessionPersistence.ts | none | Contradicts current direction. Commit 79e904280 'remove legacy compatibility shims' intentionally deleted the CONVERSATION_ID_STORAGE_KEY fallback from loadPersistedConversationId. Re-adding legacy-key migration reverts a deliberate refactor. |
| 431 | **fix** | web/src/components/activity/ActivityPanelSearch.tsx | none | Line 19 name={inputLabel.toLowerCase().replace(/\s+/g,'-')} only strips whitespace, leaving colons/ampersands. inputLabel is dev-controlled (ariaLabel/placeholder), so low risk, but trim + stricter regex to [a-z0-9_-] is a harmless robustness nit. |
| 433 | **fix** | web/src/components/activity/dirtyGuard.ts | none | guardedRun still uses void (async () =&gt; {...})() with no try/catch or .catch around guard.confirmLeave() loop (lines ~49-56 of useDirtyGuardController). A reject becomes an unhandled rejection. Add catch to log/handle errors. |
| 435 | **fix** | web/src/components/activity/fields/DetailPaneHeader.tsx | none | DetailActionButton (66-84) types onClick as () =&gt; void \| Promise&lt;void&gt; and passes it directly to button onClick={onClick}. A rejected Promise becomes an unhandled rejection with no user feedback. Wrap to catch rejections; optional loading/disable state. |
| 437 | no-fix | web/src/components/activity/fields/FieldPrimitives.tsx | Gobby web activity-panel buttons: pick canonical surfaces; design-system impeccable governs sizing. | .impeccable.md line 299 mandates 44x44 minimum touch targets. The tag remove button h-11 w-11 (44px, line 275) is the intended target size; shrinking to h-8 w-8 (32px) per the suggestion would violate the design system. The fix proposed is wrong. |
| 439 | **fix** | web/src/components/activity/fields/KeyValueField.tsx | none | updateKey (24-32) maps then entriesToRecord uses Object.fromEntries; renaming a key to an existing one silently merges/overwrites -&gt; data loss. Add duplicate-key guard/warning in updateKey. (Add-row path is object-modeled so duplicate empty keys can't accumulate.) |
| 441 | no-fix | web/src/components/activity/fields/KeyValueField.tsx | Confirmed CodeRabbit review lesson react-key-stability: use stable unique React keys; codebase already adopts this. | Already fixed. The entries.map row at line 39 already uses key={index}, not `${key}-${index}`. The unstable composite key the finding describes no longer exists. |
| 443 | **fix** | web/src/components/activity/rules/RulesDetailPanel.tsx | Task #17021 added Rules YAML editor view (RulesYamlView.tsx, Form/YAML toggle). | Premise (no-op) is wrong: setField (useDetailDraft.ts:56) adds to editedKeysRef and sets dirty=true, so draftState.setField('name', draft.name) in the catch intentionally marks the draft dirty on YAML parse error. Don't remove it; add a clarifying comment (the only valid part of the suggestion). |
| 445 | no-fix | web/src/components/activity/rules/RulesDetailPanel.tsx | none | Lines 111-116 guard setState with (state.sourceKey !== sourceKey) before calling it during render. This is React's officially recommended 'adjust state during render' pattern for resetting derived state on prop change; moving to useEffect would add an extra commit/flash and is discouraged. |
| 447 | no-fix | web/src/components/chat/styles/activity-panel.css | none | Stale. File is now 603 lines (not 991), well under the 1,000-line limit. grep finds no .rules-tab/.rules-filter-dropdown/.rules-list/.rules-row/.rules-detail selectors; rules styles already moved out. |
| 468 | no-fix | web/src/__tests__/test_legacy_symbols_removed.test.ts | Active CodeRabbit triage (mem e9f053a4) confirms web-scope findings should be judged against actual web/ runtime context. | False positive. Vitest runs from web/ (web/package.json 'test':'vitest run', vite.config.ts in web/). So process.cwd()=web/, sourceRoot=web/src, and relative paths are 'src/components/skills/...' which correctly match retiredSkillsPath. Never scans backend src. |
| 470 | no-fix | web/src/components/activity/ActivityMcpTab.tsx | MCP server save now orchestrated via McpTabActions (mem 140617b0); confirms current edit/create flow. | Described failure can't occur in practice. fetchServers is always supplied via useMcp in App.tsx (mcp={mcp}). And MCP server names are immutable registry keys (server_registry.py rejects rename), so edit-mode draft.name == original name and stays in any stale list. Optional ?. is defensive only. |
| 472 | **fix** | web/src/components/activity/IntegrationsTab.tsx | none | Confirmed: handleDelete (L161-173) places window.confirm INSIDE withChannelBusy, so the busy indicator shows before the user confirms. Move the confirm outside withChannelBusy; wrap only the actual delete+refresh. |
| 474 | no-fix | web/src/components/activity/SkillsTab.tsx | none | updateSkill returns ActivitySkill\|null via parseSkillResponse; after the !updated guard, updated is already a complete ActivitySkill. The `as` cast just narrows a spread-with-Partial; runtime object is complete. Masking-mismatch premise is weak; no real benefit. |
| 476 | **fix** | web/src/components/activity/agents/AgentsTabActions.ts | none | Confirmed in buildAgentDefinitionBody: timeout: Number(draft.form.timeout) \|\| 0 and max_turns: Number(...) \|\| 0 silently coerce NaN/invalid input to 0. Add parse + isNaN validation to reject malformed input instead of defaulting to 0. |
| 478 | **fix** | web/src/components/activity/agents/AgentsTabActions.ts | none | Confirmed in sendJson: throw new Error(`Agent request failed with ${response.status}`) includes only status. Parse response JSON, extract detail field, and append to the error message for better diagnostics. |
| 480 | **fix** | web/src/components/activity/fields/FieldPrimitives.tsx | none | Confirmed (moved to L275): remove button uses h-11 w-11 (44px) inside a min-h-8 (32px) tag pill span (L269), causing overflow. Reduce to h-5/h-6 w-5/w-6 to fit the pill. Line shifted from cited 177 but issue persists. |
| 482 | **fix** | web/src/components/activity/integrations/ChannelDetailPanel.tsx | none | Confirmed L313-326: in edit mode, secret fields render a read-only 'Configured' message instead of SecretField, so users cannot rotate/update secrets on existing channels. Drop the mode==='edit' branch and always render SecretField. |
| 484 | **fix** | web/src/components/activity/integrations/IntegrationPlatformIcon.tsx | none | Confirmed L8-74: no explicit return type and the switch over ChannelType has no default case, so unhandled types silently return undefined. Add React.ReactNode return type + exhaustive default that throws for type-safety. |
| 486 | **fix** | web/src/components/activity/integrations/IntegrationsTabData.ts | none | Confirmed L87-94: deleteIntegrationChannel throws generic Error while every sibling uses fetchJson which throws IntegrationApiError(status). Refactor to fetchJson (or throw IntegrationApiError) so callers can instanceof-check and read .status uniformly. |
| 488 | **fix** | web/src/components/activity/mcp/McpServerFields.tsx | none | Confirmed L181-188: connect_timeout onChange falls back to 0 when parse fails. App default is 30 (McpTabActions.ts L53,72), so 0 is inconsistent. Fall back to 30 (the existing default) instead of the ambiguous 0. |
| 490 | **fix** | web/src/components/activity/memory/KnowledgeGraph.tsx | none | Confirmed nodeLabel (~L453-463): e.name, e.entity_type, and props from e.properties are interpolated raw into the returned HTML string with no escaping - XSS via entity data. HTML-escape all dynamic fields before insertion. |
| 492 | **fix** | web/src/components/activity/memory/KnowledgeGraph.tsx | none | Confirmed L260-267: fetch useEffect uses .then() with no .catch(); on rejection setLoading(false) never runs and UI stays stuck on 'Loading knowledge graph...'. Add .catch() guarding the cancelled flag and setting loading false. |
| 494 | **fix** | web/src/components/activity/memory/MemoryTabList.tsx | none | Confirmed (lines shifted to 148,158,173,174): aria-labels embed full memory.content, producing verbose screen-reader output. previewContent helper already exists (L17, used L165); use it for the labels. Minor a11y nit. |
| 496 | **fix** | web/src/components/activity/pipelines/PipelineEditor.tsx | none | Confirmed: line 335 `await updateWorkflow(...)` result discarded; line 340 `setDirty(false)` runs unconditionally. Signature returns Promise&lt;WorkflowDetail\|null&gt;. Capture result, skip setDirty(false) and surface error when null. |
| 498 | **fix** | web/src/components/activity/pipelines/PipelineEditor.tsx | none | Confirmed: line 1 imports no useEffect; grep shows zero useEffect usage. useState(name/description/steps/isDirty) at 242-247 only init on mount, so reusing editor with a different `pipeline` prop keeps stale values. Add useEffect([pipeline]) reset. |
| 500 | **fix** | web/src/components/activity/pipelines/PipelineEditor.tsx | none | Confirmed: lines 414-417 are a clickable &lt;div className={STEP_HEADER_CLS} onClick={...}&gt; with no keyboard role/handler. Replace with &lt;button type="button"&gt; keeping className and onClick for keyboard/focus accessibility. |
| 502 | **fix** | web/src/components/activity/pipelines/PipelinesDefsList.tsx | none | Confirmed: line 82 calls stepCount(definition) twice; function (lines 16-23) does JSON.parse each call. Hoist to a const before the JSX and reuse for count and singular/plural check. |
| 504 | **fix** | web/src/components/activity/skills/SkillsHubDetail.tsx | none | Confirmed: scanResult/scanning/installing state (lines 90-92) with no useEffect; canAttemptInstall (112) = Boolean(content && scanResult). When `result` prop changes, stale scanResult can enable install for new content. Add useEffect([result]) reset to null/false/false. |
| 506 | no-fix | web/src/components/activity/wiki/WikiDetailPanel.tsx | User prefers maintainable real fixes over cosmetic-only changes (memory e22923bc). | DetailPaneHeader already gates save/discard buttons behind `{dirty && (...)}` (DetailPaneHeader.tsx line 27). WikiDetailPanel passes dirty={false}, so buttons never render and the no-op onSave/onDiscard are never invoked. No defect; purely cosmetic API tidy touching shared types. |
| 508 | no-fix | web/src/components/shared/executions/isolationColors.ts | none | Current code is correct: ISOLATION_COLORS (line 34) is consumed by getIsolationColorVar(mode: string) (line 49) which intentionally accepts arbitrary strings and falls back to var(--text-muted) for unknown modes (documented lines 14-16). Narrowing the Record key to a union would break the `ISOLATION_COLORS[mode]` lookup for string mode; trivial nit, no defect. |
| 510 | no-fix | web/src/styles/settings-overlay.css | none | base.css lines 81-90 already define a global `@media (prefers-reduced-motion: reduce)` block using `*, *::before, *::after` that forces animation-duration/transition-duration to 0.01ms !important, neutralizing settings-overlay-fade/rise. Also cited lines 195-215 moved to 468/478. Premise false. |
| 560 | **fix** | web/src/__tests__/test_legacy_symbols_removed.test.ts | none | Line 32 (and 46, 55) use `legacyPattern.exec(source)` / `taskGroupPattern.exec(source)` purely as booleans; match details are discarded. Replace with `.test(source)` for idiomatic boolean checks throughout the file. |
| 564 | no-fix | web/src/components/activity/ActivityMcpTab.tsx | none | handleSaveServerDraft already calls `await fetchServers?.()` (line 246) before moving the selection, so the described 'proceeds without refreshing / stale list' bug does not occur. Making the prop required (line 58) is an optional type tightening, not a current correctness issue. |
| 568 | no-fix | web/src/components/activity/StagesTab.tsx | none | Lines 60-71 keep current selection if still present, else fall back to the first item. Selecting the first item on refresh is a reasonable default; CodeRabbit's null suggestion is opinionated and arguably worse UX (blank detail panel after every refresh). Not a bug; subjective preference. |
| 572 | **fix** | web/src/components/activity/fields/FieldPrimitives.tsx | Gobby web activity panel has THREE canonical button surfaces; do not hand-roll Tailwind buttons (web-ui/activity-panel/design-system/buttons) | Line is now 275 (not 177): remove button `h-11 w-11` (44px) overflows the `min-h-8` (32px) pill at line 269. Overflow is real. But fix must respect impeccable/WCAG 44px touch-target: resize/restructure the pill rather than shrink the button to h-6; the literal h-6/h-7 suggestion conflicts with design rules. |
| 576 | no-fix | web/src/components/activity/fields/KeyValueField.tsx | none | Line 53 `&lt;div key={index}&gt;` over rows whose inputs are fully controlled by `value={key}`/`value={entryValue}`. Index keys are the standard tradeoff for a controlled KV editor where the natural key can be empty/duplicate mid-edit. The suggested parent-state UUID refactor is significant churn for negligible benefit; current code is acceptable. |
| 580 | no-fix | web/src/components/activity/integrations/ChannelDetailPanel.tsx | none | statusLabel at line 39 already declares `function statusLabel(status: ChannelStatus \| null): string`. Return type present; finding itself states no change needed. |
| 584 | **fix** | web/src/components/activity/integrations/ChannelDetailPanel.tsx | none | copyWebhookUrl (lines 195-200) does `await navigator.clipboard.writeText(webhookUrl); setCopied(true)` with no try-catch; failed writes reject unhandled and still flip copied state. Wrap in try-catch, setCopied(true) only on success. |
| 588 | **fix** dup→#486 | web/src/components/activity/integrations/IntegrationsTabData.ts | none | deleteIntegrationChannel (87-94) uses raw fetch + `throw new Error` while fetchJson throws IntegrationApiError. Align by throwing IntegrationApiError on !ok; do not call fetchJson&lt;void&gt; blindly since DELETE 204 has no JSON body to parse. |
| 592 | **fix** | web/src/components/activity/integrations/IntegrationsTabModel.ts | none | Line 106: `field.type === "number" ? Number(rawValue) : rawValue` with no NaN guard; non-numeric input yields NaN in config payload. Skip field (or keep raw) when Number(rawValue) is NaN. |
| 596 | no-fix | web/src/components/activity/mcp/McpServerFields.tsx | none | transportUsesUrl (41-43) already declares `: boolean`. The only symbol in 45-50 is the McpServerFields React component (line 45); component return-type annotations are omitted per project convention (e.g. ChannelDetailPanel). |
| 600 | **fix** dup→#488 | web/src/components/activity/mcp/McpServerFields.tsx | none | connect_timeout onChange (182-186) uses `Number.isFinite(next) ? next : 0`; invalid input clamps to 0 causing instant timeout. Default draft connect_timeout is 30, so fallback should be a positive default (e.g. 30) not 0. |
| 604 | **fix** | web/src/components/activity/mcp/McpTabActions.ts | none | recordOrEmpty (line 33) has no return type; body returns `value ? {...value} : {}` i.e. Record&lt;string,string&gt;. Add `: Record&lt;string, string&gt;` per type-hint guideline. |
| 608 | no-fix | web/src/components/activity/mcp/__tests__/McpServerFields.test.tsx | none | Test 'puts config updates before patching enabled state changes' (line 194) exists and exercises config-first-with-enabled:false then patch (line 223 enabled:false). Title is already self-documenting; doc-only comment is not a defect. |
| 612 | **fix** dup→#494 | web/src/components/activity/memory/MemoryTabList.tsx | none | Lines moved (52-&gt;148,158,173,174) but aria-labels still embed full memory.content while visual title uses previewContent (line 165, 140-char). Replace full content with previewContent(memory.content) in those aria-labels for a11y parity. |
| 616 | **fix** | web/src/components/activity/pipelines/PipelineEditor.tsx | none | handleSave still uses window.alert at lines 325 (duplicate step IDs) and 342 (save failed). Replace with inline error state local to PipelineEditor (App.tsx has showToast but it is not threaded down here). |
| 620 | no-fix | web/src/components/activity/pipelines/PipelinesDefsActions.ts | none | Backend route pipelines.py L276-279 does `project_id = request.project_id or ""; if not project_id: 400`. Omitting the field (CodeRabbit's suggested fix) yields None-&gt;""-&gt;same 400, so the suggested change is ineffective; real guard must ensure a non-empty projectId. |
| 624 | **fix** dup→#502 | web/src/components/activity/pipelines/PipelinesDefsList.tsx | none | Line 82 calls stepCount(definition) twice (display + plural check); stepCount JSON.parses definition_json each call. Hoist to a const before JSX and reuse. |
| 628 | no-fix | web/src/components/activity/skills/SkillsHubDetail.tsx | CodeRabbit findings are leads; verify current code (mem 45199ca4) | handleScan lacks return type but this is not a TS convention here: ESLint has no explicit-function-return-type rule and only 3 of 282 component fns use explicit returns. CodeRabbit misapplied the Python type-hint rule; adding would churn against convention. |
| 632 | no-fix | web/src/components/activity/skills/SkillsHubDetail.tsx | none | handleInstall lacks Promise&lt;void&gt; annotation, but the codebase does not annotate component/handler return types (no ESLint enforcement; 3/282 fns annotated). Inferred type is already correct; change is style churn against convention. |
| 636 | no-fix | web/src/components/activity/skills/SkillsHubView.tsx | none | handleSearchKeyDown (line 95) lacks a void annotation. TS infers void correctly; no ESLint rule requires explicit returns and the codebase overwhelmingly omits them. Adding is inconsistent churn. |
| 640 | no-fix | web/src/components/activity/skills/SkillsHubView.tsx | none | handleSearch useCallback (line 66) has no Promise&lt;void&gt; annotation. Type is correctly inferred; no enforcement and convention omits annotations. Suggestion contradicts established TS style. |
| 644 | no-fix | web/src/components/activity/skills/SkillsHubView.tsx | none | load async fn (line 42) lacks Promise&lt;void&gt;. Inferred correctly; effect calls it via void load(). No ESLint rule, convention omits annotations. Style churn, not a defect. |
| 648 | no-fix | web/src/components/activity/skills/SkillsInstalledDetail.tsx | none | handleSave useCallback (line 40) returns Promise&lt;boolean&gt; via inference (await onSave(draft) or false). Correctly typed already; no ESLint enforcement and the codebase omits explicit return annotations. Adding is churn. |
| 652 | no-fix | web/src/components/activity/skills/SkillsInstalledList.tsx | none | statusKind (line 24) infers literal union 'error'\|'active'\|'disabled' already. Sibling statusLabel has : string but most fns omit returns; no ESLint rule. Annotation is optional polish, not a defect. |
| 656 | **fix** | web/src/components/activity/stages/ProfilesList.tsx | CodeRabbit findings are leads; verify current code (mem 45199ca4) | Confirmed: line 37 disables 'Set as default' when profile.name==='default', blocking re-applying/refreshing the default profile. Change label to 'Update default' when already default, or drop the name check from disabled. |
| 660 | **fix** | web/src/components/activity/stages/StageDetailPanel.tsx | none | Confirmed: TextAreaFields at lines 163-174 for reviewer_agent_selector_json and dispatch_inputs_json have no client-side JSON validation; invalid JSON only fails server-side. Add a validateJson helper + error state on these fields for immediate feedback. |
| 664 | no-fix | web/src/components/chat/styles/activity-panel.css | none | Stale: file is now 603 lines (wc -l), well under the 1000-line limit. No extraction needed. |
| 668 | no-fix | web/src/components/shared/executions/execution-utils.tsx | none | All 15 exported icon/component fns (StepStatusIcon etc.) lack : JSX.Element, but 0 of them use explicit returns and only 3/282 component fns in web do. No ESLint enforcement. JSX returns are inferred correctly; annotating is convention-breaking churn. |
| 672 | no-fix | web/src/components/shared/executions/execution-utils.tsx | none | StepDisplay (line 137) returns JSX, inferred correctly. Same file's other exports all omit return types; no ESLint rule requires them. Suggestion contradicts the file's and codebase's established style. |
| 676 | **fix** | web/src/components/shared/executions/execution-utils.tsx | none | StatusBadge at lines 73-80 still lacks an explicit return type. Add `: React.JSX.Element` (or JSX.Element) after the params per the TS explicit-return-type guideline. |
| 722 | **fix** dup→#560 | web/src/__tests__/test_legacy_symbols_removed.test.ts | none | Line 32 `legacyPattern.exec(source) ?` uses exec() purely as a boolean. legacyPattern (line 13) has no global flag, so `.test()` is safe and idiomatic; replace exec with test (line 46 taskGroupPattern.exec is similar). |
| 726 | **fix** | web/src/__tests__/test_legacy_symbols_removed.test.ts | none | Confirmed lines 36-40: `function legacySkillsSubtreeFiles() {` returns `sourceFiles(sourceRoot).filter(...)` (string[]) with no return type. Add `: string[]` annotation. |
| 730 | **fix** | web/src/__tests__/test_legacy_symbols_removed.test.ts | none | Confirmed lines 29-34: `function legacyMatches() {` returns flatMap of relative() strings, no return type. Add `: string[]`. |
| 734 | **fix** | web/src/__tests__/test_legacy_symbols_removed.test.ts | none | Confirmed line 24 in sourceFiles: `if (!path.endsWith('.ts') && !path.endsWith('.tsx')) return []`. Double-negative readability nit; replace with `/\.(ts\|tsx)$/.test(path)` positive check. Cosmetic only. |
| 738 | **fix** | web/src/components/activity/ActivityMcpTab.tsx | none | Backend lowercases name (actions.py: `name = name.lower()`). ActivityMcpTab line 247 sets selection serverName: draft.name (raw); line 147 find uses exact `server.name === selection.serverName`. Mixed-case input breaks reselect. Lowercase draft.name. |
| 742 | **fix** | web/src/components/activity/agents/AgentsDetailPanel.tsx | none | Confirmed Timeout (~line 243) and Max turns (~line 255) use `setFormField(..., Number(value) \|\| 0)`, silently coercing invalid input to 0 with no user feedback. Add validation/error state for numeric fields. |
| 746 | no-fix | web/src/components/activity/agents/AgentsTabActions.ts | none | sendJson `return response.json().catch(()=&gt;({}))` but ALL callers (saveAgentDraft, setAgentEnabled, deleteAgentDefinition, duplicateAgentDefinition) discard the return and unconditionally `return true`. No failure is hidden; removing catch risks spurious throws on empty 2xx bodies. |
| 750 | **fix** dup→#584 | web/src/components/activity/integrations/ChannelDetailPanel.tsx | none | Confirmed copyWebhookUrl (lines 195-200): `await navigator.clipboard.writeText(webhookUrl); setCopied(true);` with no try/catch. Clipboard rejection causes unhandled rejection. Wrap in try/catch; only setCopied(true) on success, handle error in catch. |
| 754 | **fix** dup→#484 | web/src/components/activity/integrations/IntegrationPlatformIcon.tsx | none | Confirmed function (lines 8-74) has no return type and switch over `type` has NO default case, so unmatched type implicitly returns undefined. Add `: JSX.Element`/`React.ReactElement` and a default throwing/fallback case for runtime safety. |
| 758 | **fix** dup→#486 | web/src/components/activity/integrations/IntegrationsTabData.ts | none | Confirmed deleteIntegrationChannel (lines 87-94) throws `new Error(...)` while the module standard (fetchJson) throws `IntegrationApiError(message, response.status)`. Replace with IntegrationApiError for consistency. |
| 762 | **fix** dup→#592 | web/src/components/activity/integrations/IntegrationsTabModel.ts | none | Confirmed integrationPayloadFromDraft (~line 106): `config[field.key] = field.type === 'number' ? Number(rawValue) : rawValue` stores NaN for non-numeric. validateIntegrationDraft only checks presence, not numeric validity. Add NaN check/validation. |
| 766 | **fix** | web/src/components/activity/mcp/McpTabActions.ts | none | Confirmed sendMcpServerRequest (76-89): after `if(!response.ok) return false`, `data = ...catch(()=&gt;({}))` then `data.success !== false`. Endpoints always return JSON with `success`; on 2xx parse failure {} yields true (false success). Return `{success:false}` instead. |
| 770 | **fix** dup→#492 | web/src/components/activity/memory/KnowledgeGraph.tsx | none | Confirmed useEffect (~lines 260-267): `fetchKnowledgeGraph(limit).then(...)` with no `.catch`. Rejection never calls setLoading(false)/onError -&gt; infinite loading. Sibling effect (~166) has .catch. Add .catch that checks cancelled, setLoading(false), onErrorRef.current?.(). |
| 774 | **fix** dup→#490 | web/src/components/activity/memory/KnowledgeGraph.tsx | CodeRabbit skill (#14995) memory: treat findings as leads, verify current code; KnowledgeGraph confirmed under web/src/components/activity/memory/ (#17041). | nodeLabel (453-462) interpolates e.name, e.entity_type, props k/v and linkLabel (386) link.type into HTML strings with no escaping; react-force-graph renders these as HTML tooltips. An escapeHtml util already exists in CodeGraphExplorer.tsx to reuse. |
| 778 | **fix** | web/src/components/activity/memory/MemoryDetailPanel.tsx | none | useEffect (61-63) registers onConfirmLeaveChange(confirmIfDirty) but has no cleanup. Sibling panels AgentsDetailPanel (line 92) and ChannelDetailPanel (line 141) add `return () =&gt; onConfirmLeaveChange((next) =&gt; next())`. Add matching cleanup. |
| 782 | no-fix | web/src/components/activity/memory/MemoryTabActions.ts | none | copyMemoryContent is declared `async`, so it always returns Promise&lt;void&gt; regardless of the optional-chaining `await navigator.clipboard?.writeText(...)`. await undefined is valid. The type contract is not violated; false positive. |
| 786 | **fix** | web/src/components/activity/pipelines/PipelineEditor.tsx | none | AddStepButton (849-881) toggles `open` only via the button; dropdown (861-878) has no click-outside handler so it stays open. Add useRef on the outer div plus a document mousedown useEffect to setOpen(false). |
| 790 | **fix** dup→#502 | web/src/components/activity/pipelines/PipelinesDefsList.tsx | none | Line 82 calls stepCount(definition) twice (display + pluralization). Compute once into a const above the JSX and reuse for both the count text and the `!== 1` check. |
| 794 | **fix** | web/src/components/activity/skills/SkillsHubDetail.tsx | none | scanStatusLabel shows 'SAFE' when scanResult.is_safe (120-123), but badge className (182) and SeverityIcon (184) use severityKey(scanResult?.max_severity) unconditionally, so a SAFE scan can render with severity styling/icon. Gate style/icon on is_safe. |
| 798 | no-fix | web/src/components/activity/skills/SkillsInstalledList.tsx | none | canMoveToProject (source!=='project') and canMoveToInstalled (source==='project') are mutually exclusive, so at most one is true. `!canMoveToProject && !canMoveToInstalled` enables only when the relevant capability is true; AND logic is functionally correct, not a bug. |
| 802 | **fix** | web/src/components/activity/skills/SkillsTabData.ts | none | loadInstalledSkills hardcodes limit '200' (line 151) and includes deleted; can silently truncate. The skills API list path already uses limit=1000 in skills.py (line 245); raise/align the client limit to avoid truncation. |
| 806 | **fix** | web/src/components/activity/stages/StagesTabActions.ts | none | sendJson (28-29) does `String(data.detail)` after only checking 'detail' in data; if detail is an object it yields '[object Object]'. Add a string typeof check, else JSON.stringify, with the 'Request failed' fallback. |
| 810 | **fix** | web/src/components/shared/executions/executionFormatters.ts | none | formatTime returns '' on invalid date (line 3) while formatDateTime returns the raw iso (line 13). Inconsistent. Make formatDateTime also return '' (or add a documenting comment) for consistent handling. |
| 864 | no-fix | web/src/__tests__/test_legacy_symbols_removed.test.ts | none | Pure cosmetic nit, no functional issue. Lines 32/46/55/63 use pattern.exec() in boolean/ternary context; regexes (lines 13/15/16) have no /g flag so exec() and test() are equivalent (no lastIndex statefulness). exec() is correct and harmless; switching to test() is optional style only, not worth a change. |
| 868 | no-fix | web/src/components/activity/MemoryTab.tsx | none | Line 36 'const noop = () =&gt; {};' has TS-inferred return type void and is fully type-safe; TS infers () =&gt; void. No project rule mandates explicit return types on trivial arrow consts, and it is used only as a default callback. Cosmetic-only; not worth churn. |
| 872 | **fix** | web/src/components/activity/StagesTab.tsx | none | Inline arrow now at L303-305 (ProfileDetailPanel branch at 315-317). StageDetailPanel L50 / ProfileDetailPanel L68 include onConfirmLeaveChange in useEffect deps, so a fresh ref each render re-runs the effect. Wrap both setters in one useCallback([]) and pass it. |
| 876 | **fix** dup→#872 | web/src/components/activity/StagesTab.tsx | none | Same unstable inline onConfirmLeaveChange, second call site (StageDetailPanel branch now L303-305). Effect dep array in detail panels re-runs every render. Memoize the handler with useCallback (empty deps) and reuse at both branches. |
| 880 | no-fix | web/src/components/activity/__tests__/AgentsTab.test.tsx | none | L126 expect(putCall).toBeTruthy() already guards before L127 accesses putCall?.[1].body; if putCall is undefined the test fails first. JSON.parse never receives 'undefined' in practice. Suggested extra defensiveness is a redundant test-only nit. |
| 884 | **fix** dup→#439 | web/src/components/activity/fields/KeyValueField.tsx | Review lesson react-repeated-markdown-body-ids-use-call-id: use stable unique keys, not array index, for repeated React rows. | L53 still uses key={index} over Object.entries(value). Removing a row shifts indices, breaking focus/DOM identity. Value is a Record so a literal 'id field' needs adaptation; key by entry key (handling the empty-string add case) or restructure to keyed rows. |
| 888 | **fix** dup→#584 | web/src/components/activity/integrations/ChannelDetailPanel.tsx | none | copyWebhookUrl L195-200 awaits navigator.clipboard.writeText with no try/catch; on rejection (perms/insecure context) the promise rejects unhandled and setCopied(true) never runs, giving no feedback. Wrap in try/catch, surface an error, reset copied state. |
| 892 | **fix** dup→#484 | web/src/components/activity/integrations/IntegrationPlatformIcon.tsx | none | Switch L21-73 covers all 7 ChannelType members so TS does not error today, but it has no default; the function returns undefined for any out-of-union value and silently breaks if ChannelType gains a member. Add a default with a never exhaustiveness assertion. |
| 896 | **fix** dup→#486 | web/src/components/activity/integrations/IntegrationsTabData.ts | none | deleteIntegrationChannel L87-94 throws `Request failed: ${response.status}` with status only. Add response.statusText and, where feasible, the response body text for actionable debugging context. |
| 900 | **fix** dup→#592 | web/src/components/activity/integrations/IntegrationsTabModel.ts | none | integrationPayloadFromDraft L106 does config[field.key] = Number(rawValue) with no NaN guard; non-numeric input for a number field stores NaN in the payload. Validate Number.isNaN and skip (or surface validation error) before assigning. |
| 904 | no-fix | web/src/components/activity/integrations/__tests__/IntegrationsTab.test.tsx | none | setupFetch L79 / setupFetchFailure L138 lack return types, but web/eslint.config.js does not enable explicit-function-return-type and sibling helper lastJsonBodyFor (L157) omits return types too. The cited 'all functions need type hints' guideline is a Python rule, not enforced for these TS test helpers. |
| 908 | no-fix | web/src/components/activity/integrations/__tests__/IntegrationsTab.test.tsx | none | jsonResponse L150-155 has no return type, but explicit-function-return-type is not configured in web/eslint.config.js and the file's own helpers (e.g. lastJsonBodyFor) omit return types. Adding it contradicts local convention and is not enforced; benign nit. |
| 912 | no-fix | web/src/components/activity/integrations/__tests__/MessagesView.test.tsx | none | setupFetch L48 / requestUrls L62 lack return types, but web ESLint does not enforce explicit-function-return-type and jsonResponse in the same file also omits one. Not an enforced guideline for TS test helpers; contradicts the file's existing convention. |
| 916 | no-fix | web/src/components/activity/integrations/__tests__/MessagesView.test.tsx | none | jsonResponse L41-46 has no return type, but explicit-function-return-type is not configured in web/eslint.config.js and other helpers in this test file (setupFetch, requestUrls) omit return types. Not enforced; cited 'type hints' guideline is Python-scoped. |
| 920 | **fix** dup→#488 | web/src/components/activity/mcp/McpServerFields.tsx | none | Line 186 still uses `Number.isFinite(next) ? next : 0`; clearing/invalid input silently sets timeout to 0. Fix: fall back to previous draft.connect_timeout instead of 0. |
| 924 | **fix** dup→#766 | web/src/components/activity/mcp/McpTabActions.ts | none | Line 87: `response.json().catch(() =&gt; ({}))` then `data.success !== false` returns true for malformed/non-JSON responses, masking failures. Fix: have the catch yield a failure value (e.g. {success:false}). |
| 928 | **fix** | web/src/components/activity/memory/MemoryGraphView.tsx | none | Line 40 `static getDerivedStateFromError()` has no return type. Add `: { hasError: boolean }` to satisfy type-hint guideline. Issue still present. |
| 932 | **fix** | web/src/components/activity/memory/MemoryTabActions.ts | none | Line 31 `await navigator.clipboard?.writeText(...)` silently no-ops when Clipboard API absent. Fix: check navigator.clipboard and throw if missing so caller can surface failure. |
| 936 | **fix** dup→#494 | web/src/components/activity/memory/MemoryTabList.tsx | none | Full `memory.content` used in aria-labels (lines 148,158) and QuickMenu labels (173,174); screen readers announce entire long content. Fix: wrap with previewContent(). Lines shifted from cited 52/61/75-76. |
| 940 | **fix** | web/src/components/activity/memory/MemoryTabList.tsx | none | Line 18 previewContent still hardcodes 140. Extract to module-level PREVIEW_LENGTH constant. Cited range 16-18 shifted to 17-19; issue present. |
| 944 | **fix** | web/src/components/activity/pipelines/PipelineEditor.tsx | none | Lines 325 and 342 still call window.alert in handleSave (only two such calls in whole web app). A toast mechanism (showToast in App.tsx) exists, so routing these through it is actionable and consistent. |
| 948 | no-fix | web/src/components/activity/pipelines/PipelinesDefsActions.ts | none | Inconsistency exists (loadPipelineDefinitions throws line 34; update/toggle return null, delete returns bool, export returns string\|null) but this is intentional per-shape design callers depend on; subjective refactor, no correctness bug. |
| 952 | **fix** dup→#502 | web/src/components/activity/pipelines/PipelinesDefsList.tsx | none | Line 82 calls stepCount(definition) twice (JSON.parse each, line 18) per list item per render. Fix: store `const count = stepCount(definition)` and reuse in both ternary and display. |
| 956 | no-fix | web/src/components/shared/executions/executionFormatters.ts | none | formatTime returns '' (line 3) vs formatDateTime returns iso (line 13) for invalid dates. Both fallbacks are defensible (blank time vs raw iso); purely cosmetic with no clear correct direction and no functional impact. |
| 1062 | no-fix | web/src/components/activity/fields/FieldPrimitives.tsx | none | This is a controlled type="number" input; browsers do not surface invalid intermediate text via event.target.value for number inputs (return empty), and the cited '3.' case is finite (Number('3.')===3) so it isn't cleared. The raw-state/blur refactor adds complexity for an edge case the platform already handles. |
| 1066 | **fix** | web/src/components/settings/WorkflowVariablesEditor.tsx | none | Confirmed: handleDelete (L66-69) calls `void deleteWorkflow(variable.id)` with no await/catch, so a failed delete gives the user no feedback. Mirror handleCreate's await pattern and surface an error (toast/inline) on failure. |
| 1070 | **fix** | web/src/components/settings/WorkflowVariablesEditor.tsx | none | Confirmed: handleCreate (L47-64) awaits createWorkflow and only resets on truthy `created`; the falsy/failure branch is silent. Add user-visible error feedback when `created` is falsy while keeping resetForm on success. |
| 1074 | no-fix | web/src/components/settings/sections/IntegrationsHooksSection.tsx | Settings sections share reusable field helpers/components; do not re-implement per section. | Webhook editor (L306) doesn't gate on load, but TypedListField already exposes `disabled?` propagated to add/remove (TypedListField.tsx L17,69,89), and other sections wire `disabled`. The fix as written (add a NEW isLoading prop) contradicts the existing `disabled` convention; line cites (220-287) are also wrong. |
| 1078 | **fix** | web/src/components/settings/sections/McpToolsSection.tsx | none | Confirmed: updateKey (L226-231) and updateHub use Object.fromEntries with no collision guard, so renaming to an existing key silently collapses rows; addEntry (L247) inserts key `''`, so adding twice overwrites. Both cause silent data loss. Guard renames and assign unique new keys. |
| 1082 | no-fix | web/src/components/settings/sections/MemoryKnowledgeSection.tsx, web/src/components/settings/sections/configAccessors.ts | asTypedList is one of the shared draft-value coercers in configAccessors.ts. | asTypedList (configAccessors.ts L23-24) is a generic `&lt;T&gt;` coercer used by 4 sections (unknown/AudioBinding/ToolApprovalPolicy/WikiRoot). WikiRootsField reads each field via asString(root.scope/path) which safely coerces non-strings to ''. No runtime-error path; per-WikiRoot shape validation in a generic helper would be wrong. |
| 1086 | **fix** | web/src/components/ValidationDetectionEditor.tsx, web/src/components/settings/sections/ProjectsSessionsSection.tsx | none | Confirmed: ValidationDetectionEditor seeds jsonText via useState(() =&gt; JSON.stringify(normalized)) (L59) with no useEffect resync; call site (ProjectsSessionsSection L427-430) has no key. On draft discard/project switch the editor shows stale JSON. Add key={...} keyed on the value (or a resync effect). |
| 1090 | **fix** | web/src/components/settings/sections/SecretsAuthSection.tsx | Secrets resolve via SecretStore $secret: references; backend convention. | Confirmed: canSubmit (L178) only checks non-empty name/value. Backend _normalize_name only strip().lower() (storage/secrets.py L126-128), no charset check, so spaces/specials break $secret:NAME refs. Add /^[a-zA-Z0-9_-]+$/ check plus an inline hint. |
| 1094 | **fix** | web/src/lib/colorContrast.ts | none | Confirmed: OKLCH_PATTERN (L26) uses `[\d.]+`, matching `0..2`; Number('0..2') is NaN, so parseOklch returns a NaN-laden Oklch despite the L28 docstring 'Throws on malformed input'. Tighten the regex or add a post-parse isNaN guard that throws. |
| 1240 | no-fix | web/src/components/settings/WorkflowVariablesEditor.tsx | none | Toggling 'enabled' is the supported control for bundled defaults; CLAUDE.md says sync 'preserves the user's enabled toggle'. It does not mutate template values. Test contract only suppresses Delete for source=template (line 153), and asserts no disabled-Switch behavior. Disabling the Switch would remove the only control over bundled defaults. |
| 1244 | no-fix | web/src/components/settings/sections/AutomationWorkflowsSection.tsx | Resolved fence → NO-FIX (per maintainer). | Backend secret detection (is_secret_key_name, _SECRET_SUFFIXES in config_store.py) excludes _url; escalation_webhook_url is deliberately non-secret. Other webhook URL field (communications.webhook_base_url) is also plain TextConfigField. Masking a whole URL hurts verifiability. Fence: webhook URLs can embed tokens, so it's a defensible security judgment. |
| 1248 | no-fix | web/src/components/settings/sections/IntegrationsHooksSection.tsx | none | File is 359 lines, far under the 1000-line monolith threshold in CLAUDE.md. WebhookEndpointFields (208-289) is a local component with no correctness issue; extracting it is a subjective modularity preference, not a defect. |
| 1252 | **fix** | web/src/components/settings/sections/McpToolsSection.tsx | Use stable unique markdown ids for repeated React tool call bodies (react-key-stability) — keys must be stable AND unique. | addEntry() does commit({...hubs, '': {type:'clawdhub'}}); adding a second hub before renaming the first collides on the empty-string key and silently overwrites it (data loss). Fix: seed a unique placeholder key (e.g. counter/timestamp-based) so each new hub entry is distinct. |
| 1256 | no-fix | web/src/components/settings/sections/McpToolsSection.tsx | react-key-stability lesson: keys must be stable; the hub-name key is user-editable and starts empty, so it is neither stable nor unique. | key is the user-editable hub name starting as '' — using key={key} would remount the input and lose focus on every keystroke, and collide across empty-key entries. index is the deliberate, correct choice for an editable-key list with no stable id; the suggestion is harmful. |
| 1260 | **fix** | web/src/components/settings/sections/configAccessors.ts | CodeRabbit findings are leads to verify against current code (memory 45199ca4); local code wins. | Current asTypedList&lt;T&gt; (lines 23-25) is `return Array.isArray(value) ? (value as T[]) : []` with no JSDoc and no element validation. Adding a JSDoc noting the cast is intentional/unvalidated is a correct trivial doc improvement. |
| 1264 | **fix** | web/src/components/settings/sections/configAccessors.ts | none | Current asMap&lt;V&gt; (lines 27-31) casts `value as Record&lt;string, V&gt;` after only an object/non-array check, with no per-value validation and no comment. A clarifying comment that this is lenient coercion is a correct trivial nit. |
| 1268 | no-fix | web/src/components/settings/__tests__/WorkflowVariablesEditor.test.tsx | none | Tests already exist: __tests__/WorkflowVariablesEditor.test.tsx has describe blocks for parseVariableInput (true/false/null/[]/42/-7/3.14/'hello') and variableDisplayValue (bool/array/string/null/'not json'). Core cases covered. |
| 1272 | **fix** | web/src/components/settings/workflowVariables.ts | none | Current parseVariableInput (lines 8-16) has no doc block; it only handles 'true'/'false'/'null'/'[]' and /^-?\d+$/ , /^-?\d+\.\d+$/ regexes (excludes exp/NaN/Infinity/hex). Documenting supported/excluded subset is a correct trivial nit. |
| 1276 | no-fix | web/src/styles/tokens.css | tokens.css is the runtime design authority for the product UI (memory 4b38d146). | Claim is wrong. Light-theme --code-gutter-text oklch(48% 0.005 125) is at line 444 (line 196 is dark-theme 62%). Computed contrast: 5.99:1 vs --code-bg(97%) and 5.65:1 vs --code-bg-block(95%) — both exceed AA 4.5:1. Already passes. |

#### src/gobby/storage — 29 findings (7 fix / 22 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 102 | no-fix | src/gobby/storage/postgres_baseline_schema.sql | none | Already present. Lines 241-245 define CONSTRAINT sessions_summary_digest_turn_count_nonnegative CHECK (summary_digest_turn_count IS NULL OR summary_digest_turn_count &gt;= 0), next to the other token/count checks. Exact change requested already exists. |
| 104 | no-fix | src/gobby/storage/sessions/_field_update.py | none | persist_summary_state (301-381) now does SELECT summary_revision_id FOR UPDATE inside the db.transaction() block and computes previous_id under the row lock. The pre-transaction self.get()/fork race no longer exists. |
| 108 | no-fix | src/gobby/storage/migrations/278_session_summary_revisions.sql | none | App code (persist_summary_state) already uses approach (a): INSERTs the revision row first, then UPDATEs sessions.summary_revision_id, all in one transaction; FKs are DEFERRABLE. Circular FK is handled. |
| 128 | no-fix dup→#102 | src/gobby/storage/postgres_baseline_schema.sql | none | Duplicate of finding 102. Named constraint sessions_summary_digest_turn_count_nonnegative already exists at lines 241-245 mirroring the revision-table digest turn count constraint. |
| 130 | no-fix | src/gobby/storage/postgres_baseline_schema.sql | postgres_baseline_schema.sql is a CURRENT full-schema snapshot, not frozen at BASELINE_VERSION; baseline applies first on fresh DB. | Already implemented. Lines 277-278 add UNIQUE(id,session_id); 279-283 composite FK on (previous_revision_id,session_id)-&gt;(id,session_id); 286-291 sessions_summary_revision_fk uses (summary_revision_id,id)-&gt;(id,session_id). All requested changes present. |
| 151 | no-fix dup→#102 | src/gobby/storage/postgres_baseline_schema.sql | none | Primary ask already implemented: lines 241-245 add sessions_summary_digest_turn_count_nonnegative CHECK (summary_digest_turn_count IS NULL OR &gt;= 0). The summary_generation_mode CHECK is explicitly optional in the finding. |
| 153 | **fix** | src/gobby/storage/postgres_baseline_schema.sql | Baseline schema is the current snapshot applied first on fresh DBs; an existing-DB fix also needs a migration. | session_summary_revisions (lines 265-284) has no generation_mode CHECK; only modes agent_authored/full/delta/digest_fallback/noop are written. Add named CHECK session_summary_revisions_generation_mode_valid IN(...) to the CREATE TABLE. |
| 155 | no-fix dup→#104 | src/gobby/storage/sessions/_field_update.py | none | Already fixed. persist_summary_state opens db.transaction(), then SELECT summary_revision_id ... WHERE id=%s FOR UPDATE (line 322) and reads previous_id = current_row['summary_revision_id'] under lock (line 329). source_digest_turn_count validation unchanged. |
| 206 | no-fix | src/gobby/storage/migrations/279_session_summary_revision_integrity.sql | none | ON DELETE SET NULL (summary_revision_id) is valid PostgreSQL 15+ column-list syntax for the composite FK (summary_revision_id,id). Removing it would attempt to null id (the NOT NULL session PK) on cascade and break. CodeRabbit false positive. |
| 267 | no-fix dup→#206 | src/gobby/storage/migrations/279_session_summary_revision_integrity.sql | none | 'ON DELETE SET NULL (summary_revision_id)' is VALID PostgreSQL 15+ syntax (partial-column SET NULL). Gobby targets postgres:18 (data/postgres-pgsearch/Dockerfile). Nulling only the revision pointer (not session_id in the composite FK) is intentional. CodeRabbit's 'invalid syntax' claim is wrong. |
| 269 | no-fix dup→#206 | src/gobby/storage/migrations/279_session_summary_revision_integrity.sql | none | 'ON DELETE SET NULL (previous_revision_id)' is valid PostgreSQL 15+ partial-column SET NULL syntax; target is postgres:18. Intentionally nulls only previous_revision_id of the composite FK (previous_revision_id, session_id). Not invalid syntax. |
| 271 | no-fix | src/gobby/storage/session_models.py | none | No model-name inference or is_local_model helper exists in current code (gcode found none; no __post_init__/model_name in session_models.py). is_local defaults to False (field default and from_row else-branch). There is no 'previously computed value' to preserve; the premise is stale. |
| 273 | no-fix | src/gobby/storage/sessions/_registration_cache.py | none | Mixed cross-project candidates ARE guarded (len({project_id})&gt;1). For all-None candidates, selection is deterministic: _recovery_rank ends with unique session.id tiebreaker so the best candidate by completeness/age is chosen, not random. Treating all-None as ambiguous would regress legit recovery of unprojected sessions. |
| 328 | no-fix | src/gobby/storage/agents/_selectors.py | none | Premise false: agent_runs.is_local is BOOLEAN NOT NULL DEFAULT FALSE (baseline schema lines 199/430), so no NULL rows exist to misclassify. The old lmstudio/ollama/gpt-oss heuristic was deliberately removed in 4f0105ba6 for the local: prefix convention; restoring it would contradict current design. |
| 330 | no-fix dup→#206 | src/gobby/storage/migrations/279_session_summary_revision_integrity.sql | none | ON DELETE SET NULL (summary_revision_id) is valid PG15+ column-list syntax; repo bundles postgres:18-trixie and live DB is PG18.4. The composite FK is (summary_revision_id, id); plain SET NULL would try to null id (session PK, NOT NULL). Suggested fix is harmful. |
| 391 | no-fix | src/gobby/storage/merge_resolutions.py | none | Finding misreads param binding. WHERE `(%s IS NULL OR %s &lt;&gt; %s OR status = %s)` binds (status, status, PENDING, PENDING), i.e. `status IS NULL OR status &lt;&gt; 'pending' OR column status = 'pending'` — NOT a self-comparison. The guard meaningfully prevents resetting a non-pending row to pending. |
| 393 | no-fix | src/gobby/storage/tasks/_automation.py | none | Removal of `allow_automation IS TRUE` was intentional: commit f02dfdff5 '[gobby-#15906] fix: release stale manual task claims' deliberately dropped it so dead-session claims on manual (non-automated) tasks are also freed. Behavior is correct as-is. |
| 462 | **fix** | src/gobby/storage/mcp.py | Confirmed review lesson 'preserve-explicit-zero-limit' (mem e949ddb9): async/list defaults must preserve explicit zero; line 597 already uses 'is not None' for the update path, so from_row/to_config should too. | Confirmed: line 62 connect_timeout=float(row.get(...) or 30.0) coerces stored 0 to 30.0; lines 114-115 'if self.connect_timeout' drops 0 on serialize. Use 'is not None' checks and always emit connect_timeout. |
| 464 | **fix** | src/gobby/storage/mcp.py | none | Confirmed: _persist_server (177-179) and upsert (329-331) default requires_oauth=False/oauth_provider=None/connect_timeout=30.0; conflict clause (202-204) writes excluded values unconditionally, clobbering existing OAuth/timeout for legacy callers. description already uses COALESCE; apply same preservation. |
| 466 | **fix** | src/gobby/storage/migrations/283_mcp_server_auth_timeout_fields.sql | none | Confirmed: lines 6-9 UPDATE ... COALESCE(requires_oauth,FALSE)/COALESCE(connect_timeout,30.0) is redundant because ADD COLUMN ... DEFAULT applies defaults to existing rows in Postgres. Remove the UPDATE; harmless but unnecessary. |
| 544 | no-fix | src/gobby/storage/migrations/283_mcp_server_auth_timeout_fields.sql | Keep one-time data normalization in SQL migrations, not runtime schema guards (one-time-normalization-belongs-in-migration) | Migration 283 is already shipped (HEAD is 290); editing a committed migration won't re-run on applied DBs. App layer already enforces validity (models.py 154-156 rejects connect_timeout&lt;=0; storage/mcp.py NULL-coalesces). Constraints belong in a new migration, not this edit. |
| 548 | no-fix | src/gobby/storage/postgres_baseline_schema.sql | none | Baseline lines 34-36 already MATCH the live migration-283 shape (nullable + DEFAULT FALSE/30.0). The 'production schema' the finding claims diverges is identical. `enabled` (line 32) uses the same nullable+default pattern. App layer (models.py 154-156, mcp.py) enforces validity. No drift exists. |
| 714 | **fix** | src/gobby/storage/postgres_baseline_schema.sql | Baseline schema is the CURRENT snapshot (applied first on fresh DB); maintainer accepts editing it for 0.5.0 integrity fixes. | Line 515 task_validation_backoff FK `REFERENCES tasks(id) ON DELETE CASCADE` is the ONLY one of 15 tasks(id) FKs missing DEFERRABLE INITIALLY IMMEDIATE (14 have it). Add the clause for consistency. |
| 844 | no-fix | src/gobby/storage/postgres_baseline_schema.sql | Memory d472b6b1: schema/normalization concerns belong in migrations, not runtime guards. | No query scans next_retry_at. TaskValidationBackoffStore (_validation_backoff.py) only reads WHERE task_id=%s (the PRIMARY KEY); the backoff window is evaluated in Python via is_in_backoff_window, not SQL. An index on next_retry_at would optimize a non-existent query. Premise false. |
| 1034 | **fix** | src/gobby/storage/build_profiles.py | none | BuildProfile is a plain dataclass (no runtime type enforcement). _validate_profile (line 694) only checks &lt;0, while _parse_profile (lines 210-212) already rejects non-int/bool. Add the same isinstance int + not-bool check to _validate_profile so create()/update() can't silently int()-coerce floats/bools. |
| 1038 | no-fix | src/gobby/storage/migrations/286_code_index_prune_dirty_projects.sql | On fresh DB, baseline applies then post-261 migrations re-run; migrations run inside a transaction. | MigrationRunner.apply_pending wraps every migration in `with self._hub.transaction()` (migrations.py:71). CREATE INDEX CONCURRENTLY cannot run inside a transaction and would crash. The table is created empty in the same file, so there is nothing to block. Suggestion is harmful. |
| 1042 | no-fix | src/gobby/storage/postgres_baseline_schema.sql | none | By design both review and delete soft-hide the row: apply.py _soft_hide -&gt; mark_dreamed always sets deleted_at AND dream_action together (memories.py:462-466). No path sets dream_action='review' with deleted_at NULL. Constraint `dream_action IS NULL OR deleted_at IS NOT NULL` correctly matches design; relaxing it weakens the invariant. |
| 1180 | **fix** | src/gobby/storage/migrations/287_plan_enhancement_artifacts.sql | One-time normalization/constraints belong in SQL migrations, not runtime guards. | Lines 7,9 add plan_enhancement_rounds and ..._completed without CHECK. Sibling 288_build_profile_plan_enhancement_rounds.sql uses CHECK(&gt;=0). Append CHECK (col &gt;= 0) to both ADD COLUMN statements. |
| 1184 | no-fix | src/gobby/storage/migrations/289_memory_dream_soft_delete.sql | Migrations are run per-file inside a transaction (migrations.py apply_pending line 71). | MigrationRunner.apply_pending wraps every migration in self._hub.transaction(). CREATE INDEX CONCURRENTLY cannot run in a transaction block, so the suggested rewrite would break the migration. Suggestion contradicts the framework. |

#### src/gobby/mcp_proxy — 26 findings (16 fix / 10 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 36 | no-fix | src/gobby/mcp_proxy/tools/memory_dream.py | none | Already fixed. _try_acquire_background_slot (lines 77-83) does 'await asyncio.wait_for(semaphore.acquire(), timeout=0.001)' with TimeoutError-&gt;return False. No locked()-then-acquire TOCTOU pattern remains; asyncio is imported. |
| 57 | no-fix dup→#36 | src/gobby/mcp_proxy/tools/memory_dream.py | none | _try_acquire_background_slot (lines 77-83) already uses asyncio.wait_for(semaphore.acquire(), timeout=0.001) with TimeoutError-&gt;return False; no semaphore.locked() TOCTOU check remains. asyncio imported at line 5. |
| 126 | no-fix | src/gobby/mcp_proxy/tools/sessions/_handoff.py | none | wait_for_summary polling loop already runs `session = await asyncio.to_thread(session_manager.get, resolved_id)`, executing the blocking DB call off-loop. Subsequent summary_markdown/timeout/sleep logic unchanged. Fix already applied. |
| 149 | no-fix | src/gobby/mcp_proxy/tools/sessions/_terminal.py | none | Already simplified. Lines 419-432 rely solely on callable(persist_summary_state); no has_concrete_persist double-check exists. Fallback update_summary (432), digest_turn_count (426), and metadata_json (429) preserved as requested. |
| 194 | no-fix | src/gobby/mcp_proxy/tools/spawn_agent/_local_endpoint.py | none | Substantially stale. The cited 'registry: Any \| None' param no longer exists; signature now has run_manager: LocalAgentRunManager \| None (concrete, L30). Remaining daemon_config: Any mirrors resolve_local_generation_endpoint_selector's own config: Any param; tightening only here is inconsistent. |
| 196 | **fix** | src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py | none | Confirmed fail-open bug. L170-175 defaults session_vars={} on get_variables failure. That makes target_task_has_edits()==False (skips commit gate L194) AND remove_claimed_task({}) (L413) returns empty claimed_tasks/task_edited_files that merge_variables (current.update) clobbers in DB. Fail-closed: abort close on lookup failure. |
| 235 | **fix** | src/gobby/mcp_proxy/tools/sessions/_handoff.py | none | Lines 327-336: combined conditional returns 'Child session belongs to a different project' even when `not child_session` (None/not found), giving a misleading error. Add early distinct not-found return before project checks. |
| 237 | no-fix | src/gobby/mcp_proxy/tools/sessions/_terminal.py | none | _summary_digest_metadata_matches (289-308) is a fast pre-compact check; only `session` attrs available. Recomputing source hash needs _source_hash_payload inputs (prompt_template/summary_context) not present here. Turn-count match is the intended source-unchanged proxy. |
| 239 | **fix** dup→#196 | src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py | none | Except at 174-175 leaves session_vars={}, so target_task_has_edits({},id)=False (returns bool(set())), bypassing commit enforcement at lines 194/233. Fail-open. Mark load failed and treat as has-edits=True to fail closed. |
| 241 | **fix** | src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py | none | Line 413 remove_claimed_task uses stale session_vars (captured line 173); it rebuilds full claimed_tasks/task_edited_files dicts, then merge_variables does current.update() wholesale-replacing those keys, clobbering newer concurrent updates. Re-fetch fresh vars first. |
| 302 | **fix** | src/gobby/mcp_proxy/tools/sessions/_handoff.py | Memory ed79f613/c543a2c0 region: cross-project handoff hardening is an active concern; project context should be authoritative for cross-project guards. | Line 225 caller_project_id=project_id or project_ctx.id lets a client-supplied project_id override trusted middleware context, defeating the cross-project guard at lines 291-306 and enabling access to another project's handoff summary. |
| 304 | **fix** | src/gobby/mcp_proxy/tools/sessions/_terminal.py | Memory 4260ee20: summary refresh persists summary_source_context_hash from source payload; the hash is the staleness signal and should be validated, not just checked non-empty. | _summary_digest_metadata_matches (lines 300-308) only checks the stored hash is non-empty and turn counts match; it never recomputes/compares the source-context hash, so changed content with equal turn count reuses stale summary_markdown in the compact_self fast-path (line 538). |
| 306 | **fix** | src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py | none | The except ValueError handler at lines 328-329 returns {success:False,error:str(e)} without reasoning.to_dict(); other error paths (lines 301,725,969,989) include it and `reasoning` is in scope from line 290. Add reasoning.to_dict() for shape consistency. |
| 308 | **fix** | src/gobby/mcp_proxy/tools/spawn_agent/_local_endpoint.py | none | except block (lines 52-53) re-raises ValueError from ensure_local_model with `from exc` but logs nothing; the upstream caller also only returns str(e) without logging, so the underlying error is never logged. Add logger.debug/warning before re-raise. (Finding's registry/resolved_model names are stale; call now uses run_manager.) |
| 310 | **fix** | src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py | Memory ed79f613: gates should use task-scoped edit attribution (task_edited_files keyed by task UUID), confirming attribution must come from the task's owning session, not the closer. | Lines 170-178 read session_vars from the closer's resolved_session_id, then target_task_has_edits(session_vars, resolved_id). If a different session closes the task, the owner's edits aren't visible and the commit-requirement check (line 194) is wrongly skipped. Should source vars from get_claimed_session_id(task) (already imported, used at line 94). |
| 377 | no-fix | src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py | none | _fail_run is a best-effort cleanup helper; both catches are `except Exception` (not bare except:) in resilience paths whose purpose is to never abort cleanup. Narrowing to specific DB exceptions risks leaking resources on unexpected errors; broad catch is intentional here. |
| 379 | no-fix | src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py | none | _delete_child_session is best-effort cleanup wrapping db.transaction/conn.execute/session_storage.delete in one `except Exception as exc` that logs a warning. Broad catch is intentional to guarantee cleanup resilience; no correctness gain from narrowing. |
| 381 | **fix** | src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py | Dispatcher spawn handles mutex-attach race by calling _cleanup_unattached_spawned_run to kill/cleanup a spawned-but-unattached run (mem 8355e1f0), confirming cleanup-on-post-spawn-failure is the established pattern. | At lines 805-811 start_skipped=True returns an error while spawn_result.success was True and tmux/child-session/isolation resources were allocated; no cleanup_failed_spawn call. Add cleanup_failed_spawn(runner, run_id, ..., child_session_id=spawn_result.child_session_id) before returning. |
| 383 | no-fix | src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py | Dispatch mutex attach race already handled via held lease + _cleanup_unattached_spawned_run (mem 8355e1f0); per-task task_dispatch_mutex prevents duplicate spawns. | TaskSpawnLease.acquire() holds a DB-backed RuntimeDispatchMutex (per-task, TTL) from before the second check through execute_spawn until attach(run_id); concurrent spawns fail acquire with 'already has an agent spawn in progress'. The requested DB-level constraint already exists. |
| 385 | **fix** | src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py | Resolved fence → FIX (per maintainer). | If run_storage.start(run_id) raises, except logs a warning and continues; start_skipped stays False so run is left in 'pending' with a live tmux agent. cleanup_stale_runs only targets status='running', so it never reconciles a pending-but-spawned run. Fence: best-effort design may be intended. Suggested: treat as fatal + cleanup, or document recovery. |
| 460 | **fix** | src/gobby/mcp_proxy/client_manager/server_registry.py | none | Confirmed: update_server (230-281) sets config.enabled=existing.enabled and config.project_id on input config. normalize_bundled_server_config returns the SAME object when no normalization needed, so mutations leak to caller. Copy config before mutating. |
| 540 | **fix** | src/gobby/mcp_proxy/client_manager/server_registry.py | none | Line 247 `config.enabled = existing.enabled` silently overwrites caller-supplied enabled, but docstring (line 236) does not mention it. Add a line documenting that enabled is preserved and callers must use set_server_enabled. |
| 828 | **fix** dup→#540 | src/gobby/mcp_proxy/client_manager/server_registry.py | none | Valid docstring nit. Line 247 'config.enabled = existing.enabled' forces input enabled to be ignored, but docstring (line 236) 'Update an external server config and reset stale runtime state.' does not document this. Add one sentence noting input enabled is ignored; use set_server_enabled. |
| 832 | no-fix | src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py | none | Cited lines 401-402 are import statements, not a now capture. Actual now=datetime.now(UTC) is at line 535, captured immediately before the backoff-window check (537) and reused for record_failure (575)/escalation (585). Gap is only the LLM call duration; acceptable and shared with the window check. Premise no longer applies. |
| 1156 | **fix** | src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py | none | Line 191 appends absolute task-mentioned paths directly; _read_referenced_file_context then reads them via read_files_content. An absolute ref like /etc/passwd leaks files outside the repo. Resolve and confirm path is within base_path before adding to candidates. |
| 1160 | **fix** | src/gobby/mcp_proxy/tools/tasks/_stage_review.py | none | Line 588 sets new_state='ready' on has_suggestions alone, ignoring converged. Tool description (lines 615-622) states converged must stay needs_review for adversary continuation. Fix to: 'ready' only if has_suggestions and not converged. The cited second location (616-620) is description text, not code. |

#### src/gobby/agents — 23 findings (18 fix / 5 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 359 | **fix** | src/gobby/agents/completion_subscribers.py | none | remove_agent_completion_subscribers (92-108) only catches (sqlite3.DatabaseError, psycopg.Error); other exceptions break best-effort cleanup. Add a broad except Exception with debug logging. |
| 361 | **fix** | src/gobby/agents/kill.py | none | pid_matches_agent_identity (95-127) always runs 'ps -p', which fails on Windows so it returns False, blocking the Windows taskkill strategies (lines 187-210). Add OS detection with a Windows-safe path (tasklist or trusted-parent). |
| 363 | no-fix | src/gobby/agents/terminal_prompt_monitor.py | none | Same-prompt re-dismissal is already prevented by content fingerprint dedup (was_loop_prompt_dismissed at line 109). The run.id-keyed _last_loop_dismissed_at time throttle (60s) is deliberately content-agnostic to throttle even distinct prompts; pane-hash key would defeat it. |
| 365 | **fix** | src/gobby/agents/tmux/pty_bridge.py | none | attach() checks 'streaming_id in self._bridges' under self._lock then releases it before openpty/subprocess and re-acquires only to store. Concurrent same-id calls both pass and overwrite, leaking fd/proc. Hold lock across check+store via pending-registration or single critical section. |
| 423 | **fix** | src/gobby/agents/dry_run.py, tests/agents/test_tmux.py | none | dry_run.py:285 still TmuxSpawner(get_configured_tmux_config()) positional; test_tmux.py has 14 positional TmuxSpawner(TmuxConfig()). Constructor param is config. test_terminal_mode_worktrees.py already uses config= (355/363). Switch remaining to named config= for consistency. |
| 450 | **fix** | src/gobby/agents/isolation_clone.py | Clone isolation now appends a UUID suffix to clone paths (mem b2c5172b) so concurrent path collisions are handled, but partial-state instance attrs are a separate concern. | Confirmed: lines 64-65 still store _created_clone_path/_created_clone_id as instance state on CloneIsolationHandler. Concurrent prepare_environment calls race. Scope partial-state per call (local/context) instead of instance attrs. |
| 452 | **fix** | src/gobby/agents/isolation_clone.py | none | Confirmed: in cleanup_environment (~line 219) self._clone_storage.delete(self._created_clone_id) is a sync call not wrapped, while delete_clone just above IS wrapped in asyncio.to_thread. Wrap the storage delete in await asyncio.to_thread. |
| 454 | **fix** | src/gobby/agents/isolation_models.py | none | Confirmed: generate_branch_name slug logic (~lines 53-58) filters to alnum/'-' then truncates but never strips leading/trailing hyphens or handles empty slug. Add slug.strip('-') and a 'task' fallback when empty to avoid trailing-hyphen/empty branch names. |
| 456 | **fix** | src/gobby/agents/isolation_worktree.py | none | Confirmed: lines 49-50 still store _created_worktree_path/_id as instance state; same concurrent-prepare race as clone. (Finding 450's claim this was already fixed is wrong.) Either document single prepare/cleanup cycle or scope partial state per call. |
| 458 | **fix** | src/gobby/agents/isolation_worktree.py | none | Confirmed: in cleanup_environment (~line 215) self._worktree_storage.delete(self._created_worktree_id) is a sync call not wrapped, while delete_worktree above IS wrapped in asyncio.to_thread. Wrap the storage delete in await asyncio.to_thread. |
| 516 | **fix** | src/gobby/agents/isolation.py | isolation is a thin compatibility facade re-exporting the historical import surface; impl in sibling modules (memory 035804ad). | Confirmed: __all__ (lines 39-59) still lists 8 underscore-prefixed symbols. Direct test imports (tests/agents/test_spawn_executor_droid.py, test_isolation_droid_hooks.py) use explicit names not `*`, and no `import *` exists, so removing them from __all__ is safe and matches convention. |
| 520 | **fix** dup→#452 | src/gobby/agents/isolation_clone.py | none | Confirmed: in async cleanup_environment (line 206), line 222 `self._clone_storage.delete(self._created_clone_id)` is a synchronous blocking call, inconsistent with line 109 and the adjacent line 211 delete_clone which both use await asyncio.to_thread. Wrap line 222 the same way. |
| 524 | **fix** dup→#454 | src/gobby/agents/isolation_models.py | none | Confirmed in generate_branch_name (lines 52-59): replace(' ','-') (54) yields consecutive hyphens from multi-spaces; alnum/hyphen filter (56) leaves leading/trailing hyphens; truncate (58) can yield empty slug -&gt; 'task-N-'. Collapse spaces, strip('-'), fallback 'unnamed'. |
| 528 | **fix** | src/gobby/agents/isolation_models.py | Clone isolation appends a short UUID suffix to sanitized branch paths to avoid same-second/same-prefix collisions (memory b2c5172b) — same pattern applies here. | Confirmed: line 63 fallback `f"{prefix}{int(time.time())}"` uses second-precision; rapid calls in the no-task path collide. Append a short uuid suffix (uuid not yet imported) per the established clone-path uniqueness pattern. |
| 532 | no-fix | src/gobby/agents/isolation_repair.py | none | Lines 276-280: the second os.close(fd) in the except handler is already wrapped in `try/except OSError: pass`. The redundant close after a failed os.replace is intentionally and safely suppressed; no unhandled error exists. Behavior is correct. |
| 536 | **fix** dup→#458 | src/gobby/agents/isolation_worktree.py | none | Line 216 `self._worktree_storage.delete(...)` is a synchronous DB call inside async cleanup_environment, while peers at 69-71, 74-76, 150-157 use `await asyncio.to_thread(...)`. Wrap line 216 in `await asyncio.to_thread(self._worktree_storage.delete, self._created_worktree_id)`. |
| 682 | **fix** dup→#452 | src/gobby/agents/isolation_clone.py | Agent isolation decomposed into sibling modules (isolation_clone.py etc.); these are current files. | Line 222 `self._clone_storage.delete(self._created_clone_id)` is a direct blocking call in async cleanup_environment, inconsistent with `await asyncio.to_thread(...)` at line 109. Wrap with to_thread. |
| 686 | no-fix | src/gobby/agents/isolation_code_index.py | Isolation decomposed (#17086) into a thin facade re-exporting the historical import surface; wrapper-over-underscore is the established pattern. | ensure_isolation_code_index (11-32) is a deliberate public boundary over private _ensure_isolation_code_index, matching the module's facade/re-export design. Removing it is a stylistic nit, not a defect. |
| 690 | **fix** | src/gobby/agents/isolation_factory.py | none | get_isolation_handler (13-57) still uses `Any \| None` for git_manager, worktree_storage, clone_manager, clone_storage. Replace with concrete/Protocol types for strict-mypy type safety. |
| 694 | **fix** dup→#454 | src/gobby/agents/isolation_models.py | Clone isolation appends a short UUID suffix to sanitized branch paths (concurrency), but generate_branch_name itself does no hyphen collapsing. | generate_branch_name (40-63) filters non-alnum/hyphen then truncates; it does not collapse repeated hyphens, strip leading/trailing hyphens, or fall back when slug is empty, yielding invalid git refs. Add those normalizations. |
| 698 | no-fix | src/gobby/agents/isolation_repair.py | none | Line 276 `except BaseException:` in _patch_claude_json closes the fd, unlinks the tempfile, then re-raises. Catching BaseException is the correct idiom to guarantee cleanup on KeyboardInterrupt/SystemExit; narrowing to Exception would leak the temp file on interrupt. |
| 702 | no-fix | src/gobby/agents/isolation_repair.py | none | repair_isolation_environment wraps apply_isolation_git_hygiene in a broad `except Exception` that only logs (exc_info=True) and continues. This is a best-effort hygiene step; narrowing to OSError/SubprocessError would let other exceptions abort spawn prep, regressing resilience. |
| 706 | **fix** dup→#458 | src/gobby/agents/isolation_worktree.py | none | Line 216 `self._worktree_storage.delete(self._created_worktree_id)` is a direct blocking call in async cleanup_environment, while every other storage/git call in the file uses `await asyncio.to_thread(...)`. Wrap with to_thread. |

#### src/gobby/code_index — 21 findings (14 fix / 7 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 970 | **fix** | src/gobby/code_index/_storage/content.py | none | Line 88 logs `logger.warning("Code content keyword search failed: %s", exc)` with no traceback. Add exc_info=True per the project error-handling convention so the stack trace is captured on keyword-search failures. |
| 974 | no-fix | src/gobby/code_index/_storage/projects.py | none | Premise is wrong: IndexedProject.last_indexed_at is `str = ""` (models.py:225), not datetime\|None. The `or None` at line 36 converts empty string to NULL for the DB; removing it would store "" instead of NULL. Suggestion is harmful. |
| 978 | **fix** | src/gobby/code_index/_storage/prune_dirty.py | none | record_prune_failure (lines 29-39) runs UPDATE without checking cursor.rowcount; clear_prune_dirty already checks rowcount. Capture cursor.rowcount and log a warning when zero rows update so missing project_id rows are visible. |
| 982 | no-fix | src/gobby/code_index/_storage/relations.py | none | Schema (postgres_baseline_schema.sql:1494,1497) defines callee_symbol_id/callee_external_module as TEXT NOT NULL DEFAULT '' and part of the UNIQUE/ON CONFLICT key. Storing None would violate NOT NULL and break NULL-distinct dedup. The `or ""` is required. |
| 986 | **fix** | src/gobby/code_index/_storage/relations.py | none | upsert_imports/upsert_calls return len(imports)/len(calls) but docstrings say "count inserted"; ON CONFLICT DO NOTHING can skip in-batch dupes. Return cursor.rowcount to honor the documented contract. Low value (return is unused in prod, only tests). |
| 990 | no-fix | src/gobby/code_index/_storage/search_helpers.py | Memory 4839e885: code-index search helpers are internal facade-composed mixins under _storage/. | Not exploitable: `table` is never user-controlled. The only two callers pass string literals (content.py:86 "code_content_chunks", symbols.py:169 "code_symbols"). No dynamic/external value reaches the f-string. |
| 994 | **fix** | src/gobby/code_index/_storage/search_helpers.py | none | No dedicated unit tests import search_helpers (grep of tests/ for rows_by_ids/make_snippet/search_helpers returns nothing). Add direct tests covering empty-id list for rows_by_ids and found/not-found tokens for make_snippet to firm up coverage. |
| 998 | **fix** | src/gobby/code_index/_storage/symbols.py | none | search_symbols_fts (lines 170-171) catches bare `except Exception` and logs `logger.debug(... %s, exc)` without exc_info. Narrow to the keyword backend's expected exceptions and add exc_info=True for debuggability. |
| 1002 | **fix** | src/gobby/code_index/context.py | none | Vector clear at line 167 uses result.get("success", True) while graph clear at line 116 uses default False. Inconsistent: a missing key silently treats vector clear as success. Change vector default to False for safe-fail parity. |
| 1006 | **fix** | src/gobby/code_index/models.py | none | CodeIndexPruneDirtyProject (276-298) lacks __post_init__ to default created_at/updated_at, unlike Symbol (63), IndexedFile (168), ContentChunk (315). Add the same _now_iso() guard for consistency. Cosmetic: currently only built via from_row which supplies values. |
| 1010 | **fix** | src/gobby/code_index/sync_worker.py | Memory 2c13c58b: sync_worker treats gcode graph skipped/indexed_file_not_found as terminal success for the projection queue. | _sync_vector_file (def at line 363) uses result.get("success", True) at line 370. A missing key is treated as success; change default to False so vector sync failures fail safe, matching context.py graph default. |
| 1112 | **fix** | src/gobby/code_index/_storage/constants.py, src/gobby/code_index/_storage/files.py, src/gobby/code_index/_storage/summaries.py, tests/code_index/test_storage.py | none | Confirmed: SYNC_FAILURE_COOLOFF_SECONDS (constants.py L3) plus `failure_cooloff_seconds` params and the import in files.py (L8,126,129), summaries.py (L7,23,29), and test names/usages in test_storage.py (L276,279,294,751). 'cooldown' is the correct spelling; consistent internal rename, cosmetic only. |
| 1116 | **fix** | src/gobby/code_index/_storage/files.py | none | get_orphan_files (lines 99-107) still SELECTs all project file_paths then filters in Python by current_paths. Sibling get_stale_files (lines 80-97) uses a _current_hashes temp-table join; apply the same temp-table/anti-join pattern to filter at the DB. |
| 1120 | no-fix | src/gobby/code_index/_storage/projection_cleanup.py | none | list_projection_cleanup_pending is a read-only SELECT using self.db.fetchall(). This matches the consistent read convention across the codebase (get_unsynced_files, get_project_stats, list_prune_dirty_projects). Transactions are reserved for writes; wrapping a single read adds no value. |
| 1124 | no-fix dup→#974 | src/gobby/code_index/_storage/projects.py | none | Finding's premise is false: IndexedProject.last_indexed_at is typed `str = ""` (models.py line 225), not datetime\|None. `project.last_indexed_at or None` converts empty-string to SQL NULL; removing it would persist "" instead of NULL. The `or None` is intentional. |
| 1128 | **fix** | src/gobby/code_index/_storage/relations.py | none | upsert_imports returns len(imports) but docstring says 'count inserted'. With ON CONFLICT DO NOTHING, intra-file duplicate (project_id,source_file,target_module) tuples are skipped, so len overcounts. Update docstring to 'count attempted'. |
| 1132 | **fix** | src/gobby/code_index/_storage/relations.py | none | upsert_calls returns len(calls) but docstring says 'count inserted'. ON CONFLICT DO NOTHING can skip duplicate rows so the count is attempts not inserts. Update docstring to clarify it returns rows attempted. |
| 1140 | **fix** | src/gobby/code_index/_storage/search_helpers.py | none | rows_by_ids (line 17) interpolates `table` into f-string SQL. Both callers pass hardcoded literals ('code_content_chunks','code_symbols'), so no live injection path, but no quote_identifier helper exists. Add an allowlist of valid table names as defense-in-depth. |
| 1144 | no-fix | src/gobby/code_index/maintenance.py | none | _reconcile_orphan_files already queues authoritative orphan cleanup: after deleting hub rows it calls mark_prune_dirty (later runs `gcode prune --force`) plus clear_graph (lines 232-244). vector_sync_file is the per-file reconcile attempt; the prune net covers vectors regardless. Design is sound. |
| 1148 | no-fix | src/gobby/code_index/models.py | none | CodeIndexPruneDirtyProject is constructed only via from_row (always populates timestamps from DB); mark_prune_dirty insert lets the DB default created_at/updated_at. Sibling ProjectionCleanupPending intentionally follows the same no-__post_init__ pattern. No functional impact. |
| 1152 | **fix** | src/gobby/code_index/storage.py | none | __init__ at line 32 declares `db: HubDatabase \| HubDatabase`, a redundant self-union. Simplify to `db: HubDatabase`. |

#### tests/skills — 17 findings (0 fix / 17 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 38 | no-fix | tests/skills/test_javascript_skill.py | none | Already fixed. Line 24 reads 'parsed = SkillLoader().load_skill(SKILL_DIR, validate=True)'. The test already exercises validation; subsequent asserts check name/version/category. |
| 61 | no-fix | tests/skills/test_c_skill.py | none | Line 32 reads: assert 'get_skill_file(name="c", path="references/memory-and-lifetime.md")' in parsed.content -- no redundant parentheses around parsed.content. Already fixed. |
| 63 | no-fix | tests/skills/test_elixir_skill.py | none | Line 3 already contains 'from __future__ import annotations' before other imports (Path at line 5). Already fixed. |
| 65 | no-fix | tests/skills/test_python_skill.py | none | test_synced_python_skill_is_searchable(temp_db: HubDatabase) already exists at lines 61-77, calling sync_bundled_skills, SkillSearch, SearchConfig with required imports present (lines 9,12,13). Already added. |
| 67 | no-fix | tests/skills/test_python_skill.py | none | Line 7 has 'import pytest' and line 16 has 'pytestmark = pytest.mark.unit'. Both already present. Already fixed. |
| 94 | no-fix | tests/skills/test_c_skill.py | none | Line 32 already reads: assert 'get_skill_file(name="c", path="references/memory-and-lifetime.md")' in parsed.content -- no unnecessary parentheses around parsed.content. Already in the suggested form. |
| 96 | no-fix | tests/skills/test_json_skill.py | none | Module already imports pytest (line 7) and defines 'pytestmark = pytest.mark.unit' (line 10) after imports. Marker already present. |
| 98 | no-fix | tests/skills/test_python_skill.py | none | Module already imports pytest (line 7) and defines 'pytestmark = pytest.mark.unit' at line 16. Unit marker already present. |
| 136 | no-fix | tests/skills/test_elixir_skill.py | none | Already fixed. Line 24 now reads SkillLoader().load_skill(SKILL_DIR, validate=True), enabling validation exactly as suggested. validate=False no longer present. |
| 138 | no-fix | tests/skills/test_json_skill.py | none | Already implemented. Line 7 has import pytest and line 11 has pytestmark = pytest.mark.unit at module level, exactly as requested. |
| 140 | no-fix | tests/skills/test_python_skill.py | none | Already marked unit. Line 7 import pytest; line 16 pytestmark = pytest.mark.unit applies the unit marker to all tests in the module (covers test_python_skill_parses_with_references), equivalent/better than per-function marker. |
| 169 | no-fix | tests/skills/scenarios/python/strict-typed-config-boundaries.yaml | none | Already fixed. Line 37 command already reads '... && GOBBY_TEST_PROTECT=1 uv run pytest tests/test_config.py -q'. The prefix is present. |
| 171 | no-fix | tests/skills/test_c_skill.py | none | Already fixed. Line 32 reads: assert 'get_skill_file(name="c", path="references/memory-and-lifetime.md")' in parsed.content - no parentheses around parsed.content, matching line 31's style. |
| 173 | no-fix | tests/skills/test_elixir_skill.py | none | Already fixed. Line 24 calls SkillLoader().load_skill(SKILL_DIR, validate=True) in test_elixir_skill_parses_with_references (def at line 22). |
| 175 | no-fix | tests/skills/test_json_skill.py | none | Already fixed. Line 7 has 'import pytest' and line 11 has 'pytestmark = pytest.mark.unit', categorizing the JSON skill tests as unit tests. |
| 177 | no-fix | tests/skills/test_python_skill.py | none | Already addressed. Module-level pytestmark = pytest.mark.unit at line 16 (import pytest line 7) marks test_python_skill_parses_with_references (line 22) as a unit test; it still calls load_skill(SKILL_DIR, validate=False). |
| 179 | no-fix | tests/skills/test_python_skill_validation_guidance.py | none | Already fixed. Lines 9-11 already build the path via Path(__file__).resolve().parents[2] / 'src/gobby/install/shared/skills/python/SKILL.md' with Path imported (line 3). No hardcoded path remains. |

#### tests/mcp_proxy — 16 findings (6 fix / 10 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 19 | no-fix | tests/mcp_proxy/tools/spawn_agent/test_dedup.py | none | No duplicate. L191 is the single 'mock_task.claimed_by_session_id = None'; L192 is 'mock_task.stages = []'. The separate claimed_task.claimed_by_session_id at L195 is a different mock object. |
| 21 | no-fix | tests/mcp_proxy/tools/tasks/test_complete_stage.py | none | No duplicate. L163 is the single 'assert refreshed.claimed_by_session_id is None'; L164 is a distinct 'assert child_vars["task_claimed"] is False'. No repeated assertion at the cited range. |
| 23 | no-fix | tests/mcp_proxy/tools/tasks/test_fail_stage.py | none | No duplicate. L87 is the single 'assert refreshed.claimed_by_session_id is None'; L88 is a distinct 'assert child_vars["task_claimed"] is False'. No repeated assertion at the cited range. |
| 59 | no-fix | tests/mcp_proxy/test_mcp_proxy_stdio.py | none | test_call_tool_treats_wait_for_summary_as_wait_tool at line 930 already has @pytest.mark.asyncio decorator directly above it (line 929). Already fixed. |
| 90 | no-fix | tests/mcp_proxy/test_mcp_proxy_stdio.py | none | test_call_tool_treats_wait_for_summary_as_wait_tool at line 930 already has @pytest.mark.asyncio on line 929. Cited line 916 belongs to a different test. Already decorated. |
| 134 | no-fix | tests/mcp_proxy/test_mcp_proxy_stdio.py | none | Already fixed. test_call_tool_treats_wait_for_summary_as_wait_tool at line 930 has @pytest.mark.asyncio at line 929; import pytest at line 11, pytestmark=pytest.mark.unit at line 29. |
| 167 | no-fix | tests/mcp_proxy/test_mcp_proxy_stdio.py | none | Already has marker. test_call_tool_treats_wait_for_summary_as_wait_tool at line 930 is preceded by @pytest.mark.asyncio at line 929. import pytest present; asyncio_mode='auto' also makes it redundant. |
| 210 | no-fix | tests/mcp_proxy/tools/test_task_sync.py | none | Test at lines 815-838 already has a docstring stating the security invariant ('Commit/diff helpers must resolve project_path before Git helper cwd use'). The extra 'intentional fragility' note is marginal cosmetic, no correctness impact. |
| 285 | no-fix | tests/mcp_proxy/tools/test_handoff_coverage.py | none | get_handoff_context resolves project via get_project_context() (_handoff.py L224-225), not caller-session lookup. Tests already patch get_project_context and assert find_parent called with project_id='proj-1' and list not called (L129-130). session_context_for_test wrapping would test nothing the code uses. Suggestion based on wrong model. |
| 340 | **fix** | tests/mcp_proxy/tools/test_task_sync.py | none | test_task_sync_git_helper_calls_follow_repo_path_resolution still uses inspect.getsource+ast.parse to assert source line ordering (_get_task_and_repo_path before task_manager.link_commit etc). Brittle; replace with behavioral test spying _get_task_and_repo_path to raise and asserting helpers not called. Symbols exist. |
| 427 | no-fix | tests/mcp_proxy/tools/test_parallel_dispatch.py | none | Line 447 '/tmp/proj-1' is only a mock return value (mock_ctx.return_value dict project_path). spawn_agent_impl is patched out (mock_impl_patch.side_effect=mock_impl), so the path never touches the filesystem. False positive; no platform dependency exists. |
| 552 | **fix** | tests/mcp_proxy/tools/spawn_agent/test_dedup.py | none | Line 163 `async def test_...(self, db) -&gt; None` lacks a type hint. Add `db: HubDatabase` and import HubDatabase from gobby.storage.hub.protocol. Note: CodeRabbit's claim that HubDatabase is already imported / typed elsewhere in THIS file is false; the import must be added. |
| 556 | **fix** dup→#552 | tests/mcp_proxy/tools/spawn_agent/test_factory.py | none | Line 648 `async def test_task_id_supports_hash_n_format(self, mock_runner, agent_body, db) -&gt; None` has bare `db`. HubDatabase is imported (line 14) and used at 210/263/305/895. Change to `db: HubDatabase` for consistency. |
| 718 | **fix** dup→#552 | tests/mcp_proxy/tools/spawn_agent/test_factory.py | none | Line 648 `test_task_id_supports_hash_n_format(self, mock_runner, agent_body, db)` leaves db (and mock_runner/agent_body) untyped. The conftest db fixture returns HubDatabase, so annotate `db: HubDatabase`. |
| 848 | **fix** dup→#552 | tests/mcp_proxy/tools/spawn_agent/test_dedup.py | none | Still present: line 163 'async def test_merge_worker_spawn_ignores_parent_merge_orchestrator_run(self, db) -&gt; None' has untyped db. Fixture is db(temp_db: HubDatabase) -&gt; HubDatabase (conftest.py:17), so annotate db: HubDatabase (import HubDatabase). |
| 852 | **fix** dup→#552 | tests/mcp_proxy/tools/spawn_agent/test_factory.py | none | Still present: line 648 db param untyped (mock_runner/agent_body also untyped but finding scopes only db). Fixture type is HubDatabase (conftest.py:17). Annotate db: HubDatabase. Trivial test type-hint fix. |

#### docs — 14 findings (5 fix / 9 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 80 | no-fix | docs/reviews/skills.md | none | MD022 already satisfied: headings at lines 22, 30, 38, 46, 53, 60, 67 each have a blank line before (21/29/...) and after (23/31/...). No surrounding-blank-line violation in current file. |
| 113 | no-fix | docs/reviews/TEMPLATE.md | none | markdownlint-cli2 reports 0 MD022 errors for TEMPLATE.md. The ### [IMPORTANT] heading (line 27) is already preceded by a blank line (26). Cited 25-26 issue no longer applies. |
| 115 | no-fix | docs/reviews/TEMPLATE.md | none | 0 MD022 errors. The ### [NIT] heading (line 34) is already preceded by a blank line (33). The blank-line-before-heading issue cited at 31-32 no longer exists. |
| 117 | no-fix | docs/reviews/hooks.md | none | markdownlint-cli2 reports 0 MD022 errors for hooks.md. Spot-checked: ### heading at line 29 has blank lines above (28) and below (30). All ### headings already separated. |
| 145 | no-fix | docs/reviews/TEMPLATE.md | none | Already satisfied. Headings at lines 19, 27, 34 are each followed by a blank line (20, 28, 35) before the bulleted content, so no MD022 violation exists. |
| 216 | no-fix | docs/guides/code-index.md | none | `gcode blast-radius --help` shows only --depth (no --limit). The --limit in gcode_gateway.py line 254 is for the internal `graph blast-radius` subcommand, not the documented top-level `gcode blast-radius`. Docs at line 149 are correct; adding --limit would mislead. |
| 218 | no-fix | docs/guides/code-index.md | none | Duplicate of finding 216. `gcode blast-radius --help` confirms only --depth; --limit applies only to internal `graph blast-radius` invoked by the gateway. Docs line 149 are accurate; documenting --limit on the standalone command would be wrong. |
| 223 | no-fix | docs/architecture/coding-standards.md | none | pyproject.toml line 151 sets asyncio_mode = 'auto', so the explicit @pytest.mark.asyncio marker genuinely IS optional for tests to run; the docs (lines 595-596) are factually correct and the example at line 601 already shows the marker. |
| 225 | **fix** | docs/architecture/coding-standards.md | none | Line 34 shows `uv run pytest --cov=gobby ...` without the GOBBY_TEST_PROTECT=1 prefix that CLAUDE.md mandates for running pytest. Prepend GOBBY_TEST_PROTECT=1 so docs reflect the required test-isolation prefix. |
| 227 | **fix** | docs/architecture/source-tree.md | none | Line 34 is a naked ``` opening fence (also lines 12, 52, 179, 191, 206, 228) wrapping directory trees. Adding ```text language label is the correct, harmless MD040 fix. No markdownlint config exists, so value is low but the fix is accurate. |
| 229 | **fix** | docs/guides/mcp-tools.md | none | models.py:145-147 validates `transport in (http, websocket, sse)` requires url. Docs table line 111 says url required 'For http/ws', omitting sse. Update cell to 'For http/ws/sse'. |
| 294 | **fix** dup→#227 | docs/architecture/source-tree.md | none | Fenced blocks at L12,34,56,183,195,228 use bare ``` with no language (tree listings), violating MD040. Adding ```text is correct and harmless. Note: markdownlint is not enforced in CI/pre-commit (lint job runs only ruff), so purely cosmetic doc consistency. |
| 296 | no-fix | docs/guides/README.md | none | Link target exists: docs/archive/droid.md is present on disk. From docs/guides/README.md the relative path ../archive/droid.md resolves to docs/archive/droid.md (exists). The link is not broken; removing the row would drop a valid reference. False positive. |
| 966 | **fix** | docs/guides/mcp-tools.md | none | Section header (line 313) says '40 tools' but the section documents 41 tool rows; registry table row (line 167) says 45. Counts inconsistent. Fix: reconcile header to 41 and align/verify the registry 45 count. |

#### src/gobby/install — 14 findings (4 fix / 10 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 40 | no-fix | src/gobby/install/shared/workflows/rules/skill-discovery/require-java-skill.yaml | none | Already simplified. The reason field (now line 19) is concise: 'Load the java skill before editing Java files: call get_skill(name="java") on gobby-skills, then continue.' No long MCP discovery progression string exists. |
| 47 | no-fix | src/gobby/install/shared/skills/javascript/references/async.md | none | Already fixed. fetchJson now does 'if (signal?.aborted) { controller.abort(); } else { signal?.addEventListener("abort", ..., { once: true }); }', short-circuiting on an already-aborted signal while keeping the listener for future aborts. |
| 49 | no-fix | src/gobby/install/shared/skills/python/references/testing.md | none | Already fixed. In test_async_fetch, 'response = await client.get("/users/1")' and 'assert response.status_code == 200' are correctly indented inside the 'async with AsyncClient(app=app) as client:' block. |
| 51 | no-fix | src/gobby/install/shared/workflows/rules/skill-discovery/require-ruby-skill.yaml | none | Already fixed. Line 15 reads canonical_file_path.endswith('sorbet/config') (no leading slash), exactly the suffix variant the finding recommended, so repo-root 'sorbet/config' paths are matched. |
| 53 | no-fix | src/gobby/install/shared/workflows/rules/skill-discovery/require-rust-skill.yaml | none | **CONFLICTS with #190.** Line 24 already uses .endswith(('.cargo/config', '.cargo/config.toml')) with NO required leading slash. Root-level '.cargo/config' and nested '/project/.cargo/config' both match. The finding's premise (requires preceding '/') is false. |
| 55 | no-fix | src/gobby/install/shared/workflows/rules/skill-discovery/require-yaml-skill.yaml | none | Current yaml template (line 23) only matches '.yamllint' plus yaml/yml extensions. No '.clang-format' or '.clang-tidy' entries exist in the condition. Already addressed. |
| 84 | **fix** | src/gobby/install/shared/workflows/rules/task-enforcement/completion-readiness.yaml | none | Doc snippet at lines 35-37 omits real optional params. Verified signature in _verification.py:41-49 has stage_name and session_id. Add them so the docs reflect the full signature available to agents. |
| 122 | no-fix | src/gobby/install/shared/skills/python/references/testing.md | none | Lines 95-97 already indent `response = await client.get(...)` and the assert inside the `async with AsyncClient(app=app) as client:` block. Valid async syntax; the cited mis-indentation no longer exists. |
| 124 | no-fix | src/gobby/install/shared/workflows/rules/skill-discovery/require-dart-skill.yaml | none | Purely cosmetic readability nit on a template `when:` expression which is a single boolean string; the safe evaluator has no local-variable binding for the suggested named intermediate checks. Current condition is correct and functional. |
| 147 | no-fix | src/gobby/install/shared/skills/python/SKILL.md | Bundled coderabbit skill treats findings as leads; verify current code before acting. | Already fixed. Line 24 reads `Tests: targeted GOBBY_TEST_PROTECT=1 uv run pytest &lt;tests&gt;`; line 25 references repo package manager (uv). The only pytest example is already protected; no second unprotected occurrence (file is 102 lines). |
| 190 | **fix** | src/gobby/install/shared/workflows/rules/skill-discovery/require-rust-skill.yaml | none | **CONFLICTS with #53.** Confirmed: L24 endswith(('.cargo/config', '.cargo/config.toml')) lacks leading slash, so paths like x.cargo/config falsely match. Restore '/.cargo/config' and '/.cargo/config.toml' to require a directory boundary (canonical_file_path is absolute). |
| 192 | no-fix | src/gobby/install/version_pins.py | Memory ebd74307: CodeRabbit guidance to change MANAGED_BIN_VERSION_PINS is wrong; keep Gobby's chosen pins (these are minimum supported versions, not release tags). | Stale + wrong premise. Module docstring: 'Minimum supported versions for Gobby-managed native binaries' — not exact release tags. Cited content stale: 'gloc' removed, gsqz now 0.4.6 (was 0.1.4), gcode 1.0.0. Memory says CodeRabbit pin advice is wrong for Gobby. |
| 298 | **fix** | src/gobby/install/shared/prompts/memory/recall_query_synthesize.md | none | File starts with prompt body, no YAML front matter; every sibling in prompts/memory/ starts with ---. Add front matter to match convention. Correct fields are description + required_variables (user_prompt, max_query_chars) like siblings, NOT name/version. Update bundled_content_manifest.json hash too. |
| 1014 | **fix** | src/gobby/install/shared/workflows/agents/plan-adversary.yaml | none | load_skill status_message (221-232) restates the load-both-skills-via-MCP directive twice (explicit proxy path plus an authoritative re-explanation). Consolidate to one concise instruction while keeping the gobby-skills MCP load and plan-review/proportionality roles. |

#### src/gobby/servers — 14 findings (11 fix / 3 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 198 | **fix** | src/gobby/servers/routes/configuration_import_export.py | none | Confirmed. L372 request.config.get('databases', {}).get('falkordb', {}); config is dict[str,Any] so 'databases' may be non-dict (e.g. str), raising AttributeError uncaught by the try/except ValueError (L382), yielding 500 not 422. Validate databases is dict; else raise ValueError. |
| 200 | **fix** | src/gobby/servers/websocket/chat/local_openai_warmup.py | none | Confirmed order-dependent. _match_local_generation_endpoint (L204-215) returns the FIRST endpoint with positive score + matching origin (return inside loop L214). Switch to best_score/best_endpoint selection like sibling _select_lm_studio_model (L230+) already does. |
| 247 | **fix** dup→#198 | src/gobby/servers/routes/configuration_import_export.py | none | Line 372 `request.config.get('databases',{}).get('falkordb',{})` raises AttributeError if databases is a non-dict value; the surrounding try only catches ValueError (382), so it escapes as 500 not 422. Guard with isinstance(databases, dict) first. |
| 249 | **fix** dup→#200 | src/gobby/servers/websocket/chat/local_openai_warmup.py | none | _match_local_generation_endpoint (204-215) returns first positive-score/origin-match by dict/config order, not best score, and ignores ties. Sibling _select_lm_studio_model (230-277) already does best_score+ambiguous-&gt;None. Apply same pattern. |
| 314 | **fix** | src/gobby/servers/provider_model_defaults.py | none | The three Gemini dicts in DROID_MODEL_CATALOG (lines 154-173) are exact duplicates of GEMINI_MODEL_CATALOG (lines 8-28). Extract a shared GEMINI_MODELS constant and reference it in both catalogs to keep edits in one place. |
| 316 | **fix** dup→#198 | src/gobby/servers/routes/configuration_import_export.py | none | Line 372 request.config.get('databases', {}).get('falkordb', {}) raises AttributeError if 'databases' is present but not a dict (config is dict[str,Any], values unconstrained). The except at line 382 catches only ValueError, so it escapes to a 500. Assign databases first and branch None/dict/else-&gt;422. |
| 318 | **fix** | src/gobby/servers/routes/mcp/hooks.py | none | Fast-path at lines 542-544 returns hardcoded {continue:True,decision:approve} for already-processed envelopes, discarding the original gating decision; a replayed PreToolUse/Stop envelope is auto-approved. Marker storage (envelope_dedupe.py) stores only id+timestamp, so persisting/returning the recorded response (and fail-closed for gating types) is the correct fix. |
| 320 | **fix** | src/gobby/servers/routes/mcp/hooks.py | none | Dedupe is check-then-set TOCTOU: is_envelope_processed checked at line 542, mark_envelope_processed called only after handlers via mark_processed_and_return (lines 502-512). Concurrent duplicates can both run side effects. Needs an atomic claim-and-set storing a terminal result for replays (envelope_dedupe currently has no claim/result API). |
| 322 | **fix** dup→#200 | src/gobby/servers/websocket/chat/local_openai_warmup.py | none | _match_local_generation_endpoint (lines 209-214) returns the first endpoint with positive score and matching origin, letting a looser match shadow a better one. Should track best_endpoint by highest positive score (as _select_lm_studio_model already does at lines 234-236) and return that. |
| 375 | no-fix | src/gobby/servers/routes/source_control.py | none | list_branch_commits wraps helper.list_commits(branch_name, limit=limit) in try/except Exception (line 385) that logs and falls back to git log with --max-count={min(limit,100)}. A ValueError from _github_page_limit is caught gracefully, not unhandled. |
| 389 | no-fix | src/gobby/servers/routes/rules.py | none | LocalWorkflowDefinitionManager.list_all defaults include_deleted=False and adds `deleted_at IS NULL` when False, so the rename conflict check already excludes soft-deleted rules. Adding include_deleted=False is purely cosmetic; no functional bug exists. |
| 710 | **fix** | src/gobby/servers/routes/mcp/endpoints/server.py | list_mcp_servers proxy output is compacted in server_list.py, but this HTTP route is a separate endpoint that returns full config incl. secrets. | **CONFLICTS with #836 — see Open Question 1.** list_mcp_servers (now ~173-174) returns config.env and config.headers (API keys/tokens) and `/api/mcp/` is in _PUBLIC_PREFIXES (auth.py:41-42), so secrets leak unauthenticated. Drop env/headers from the response (preferred over removing the public prefix). |
| 836 | no-fix | src/gobby/servers/routes/mcp/endpoints/server.py | Memory 70789859: list_mcp_servers output shaping is intentional per API contract. | **CONFLICTS with #710 — see Open Question 1.** By design env/headers hold $secret:NAME references, not raw creds; secrets resolve only at connection time via resolve_secrets_in_config (secrets.py) without mutating stored config. Web edit UI (McpTabActions.ts 66-67, McpServerFields.tsx 147) needs these. Masking would break editing. SecretsAuthSection.tsx 223 documents 'agents never see raw values'. |
| 840 | **fix** | src/gobby/servers/routes/mcp/endpoints/server.py | none | Still present: lines 113/115 do bool(body.get('enabled',True)) / bool(body.get('requires_oauth',False)). MCPServerConfig is a plain dataclass with no coercion, so bool('false') silently becomes True. Add isinstance(bool) validation (or strict parse) and reject non-bool values for both fields. |

#### src/gobby/sessions — 13 findings (9 fix / 4 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 100 | no-fix | src/gobby/sessions/summarize.py | none | qwen no longer routes to ClaudeTranscriptParser. Lines 579-582 have an explicit 'elif source == "qwen"' branch instantiating a dedicated QwenTranscriptParser. Finding is stale. |
| 202 | **fix** | src/gobby/sessions/transcripts/typed_json.py | none | Confirmed. L382 casts thoughts to list[dict] then _extract_thought_parts (L56-66) calls thought.get(...) with no isinstance guard; L427 'for tc in tool_calls' calls tc.get(...) directly. Non-dict entries (None/str) raise AttributeError. Guard each element with isinstance(elem, dict). |
| 204 | **fix** dup→#202 | src/gobby/sessions/transcripts/typed_json.py | none | parse_session_json (now lines 316-328) still iterates data.get('messages',[]) with no isinstance(messages,list) guard, and _parse_session_message calls msg.get('type') directly, so a non-dict msg raises AttributeError. Add list/dict shape guards. |
| 251 | **fix** | src/gobby/sessions/summarize.py | none | _source_hash_payload (641-656) passes last_turn_markdown/last_assistant_content only through _summary_source_text (just .strip(), line 403-405), not strip_injected_context. _digest_markdown_for_summary strips them (410-417). Injected-only changes alter hash, defeating noop reuse. |
| 253 | **fix** | src/gobby/sessions/transcripts/typed_json.py | none | Confirmed: _extract_thought_parts uses desc.lstrip("\\n").lstrip("\n").strip(). lstrip("\\n") strips ALL leading '\\' and 'n' chars, corrupting descriptions starting with 'n' (e.g. 'nice'-&gt;'ice'). Replace with explicit startswith("\\n") slice then lstrip("\n"). |
| 255 | **fix** | src/gobby/sessions/transcripts/typed_json.py | none | Confirmed: inline functionCall branch in parse_line list-content handling sets content_type='tool_use', tool_name, tool_input but never sets tool_use_id or self._last_tool_use_id, so tool_result correlation breaks. _next_tool_use_id helper exists to generate one. |
| 257 | **fix** | src/gobby/sessions/workspace_context.py | none | Confirmed: enrich_git_context(handoff_ctx: Any) accesses .git_status/.git_commits. Caller (summarize.py:207-211) passes analyzer.HandoffContext which has both fields. Replace Any with HandoffContext for a fully typed signature. |
| 259 | **fix** | src/gobby/sessions/workspace_context.py | none | Confirmed: resolve_session_workspace(session: Any) reads getattr(session,'terminal_context'). Caller passes a Session (has terminal_context). Type as Session (keep transcript_path: str\|None) for static verification. |
| 261 | no-fix | src/gobby/sessions/workspace_context.py | none | git-log block uses 'except Exception as e: logger.debug(...)' — intentional best-effort enrichment that degrades gracefully and already logs. Not a bare except (CLAUDE.md targets bare 'except:'). Narrowing risks letting unexpected errors break summary generation. |
| 263 | no-fix | src/gobby/sessions/workspace_context.py | none | git-status block uses 'except Exception as e: logger.debug(...)' — same intentional graceful-degradation pattern as the git-log block. Failures are logged, not swallowed. Broad catch is defensible for a non-critical enrichment helper. |
| 265 | no-fix | src/gobby/sessions/workspace_context.py | none | enrich_git_context already handles a missing/invalid cwd: create_subprocess_exec fails and is caught by the surrounding except, logged via logger.debug. Errors are NOT silently swallowed. Pre-validation is a minor nicety, not a correctness fix. |
| 324 | **fix** | src/gobby/sessions/transcripts/typed_json.py | none | Line 462 `if func_response:` only emits a tool_result when functionResponse is truthy, dropping valid empty outputs ('', {}, [], 0, False). Detect by key presence: track a found flag when 'functionResponse' is in the dict (lines 456-461) and emit ParsedMessage based on that flag, not truthiness. |
| 326 | **fix** dup→#255 | src/gobby/sessions/transcripts/typed_json.py | none | Inline functionCall branch (lines ~276-279) sets content_type/tool_name/tool_input but never assigns tool_use_id or updates self._last_tool_use_id, so ParsedMessage gets tool_use_id=None and following tool_results can't pair. Use _next_tool_use_id helper (exists) and set _last_tool_use_id. |

#### tests/memory — 12 findings (5 fix / 7 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 856 | no-fix | tests/memory/test_graph_edge_weighting.py | none | **Split verdict (see #1200).** Partly stale, partly non-convention. All five named functions (lines 86-114) ALREADY have '-&gt; None' return hints. @pytest.mark.unit is missing, but it is not a required convention (only 1 of 38 files in tests/memory uses it) and addopts has no --strict-markers (pyproject 157-160). No change warranted. |
| 960 | **fix** | tests/memory/test_search_ranking.py | none | No `pytestmark = pytest.mark.unit` between imports (lines 9-13) and class _Storage (line 16). `import pytest` already present (line 9). Sibling tests/memory files all set this marker. Add it. |
| 1058 | **fix** | tests/memory/test_recall_benchmark_e2e.py | none | The assertion loop (lines 549-561) only covers flags_off and flags_on; the cluster_expansion adoption gate is only printed (lines 527-530), never asserted, so a cluster_expansion regression passes silently. Replace the print with an assert on production_recall&gt;graph_off and production_mrr&gt;=graph_off-1e-9. |
| 1196 | no-fix | tests/memory/test_dream.py | none | Sort key (line 1071) maps None-&gt;"" which sorts before any ISO timestamp, exactly matching production ORDER BY last_dreamed_at ASC NULLS FIRST (memories.py:551). last_dreamed_at is only None or ISO string, so behavior is already equivalent. |
| 1200 | **fix** | tests/memory/test_graph_edge_weighting.py | Prior coderabbit marker/asyncio finding (task-15722) was deemed non-promotable; asyncio_mode='auto' handles async tests without the decorator. | **Split verdict: #1200/1204/1208 say fix, #856/1212/1216/1220/1224 say no-fix — see Conflicts.** File lacks any markers (16 async tests, no module pytestmark). Add module-level `pytestmark = pytest.mark.unit` (sibling tests/memory convention). The suggested @pytest.mark.asyncio is unnecessary (asyncio_mode='auto'); per-function decorators would be inconsistent. |
| 1204 | **fix** dup→#1200 | tests/memory/test_graph_edge_weighting.py | Prior coderabbit marker/asyncio finding (task-15722) non-promotable; asyncio_mode='auto' runs async tests without the decorator. | Same file/root cause as id 1200: no markers anywhere. Correct fix is one module-level `pytestmark = pytest.mark.unit`. @pytest.mark.asyncio is redundant under asyncio_mode='auto'; isolating decorators to test at line 407 would be inconsistent. |
| 1208 | **fix** dup→#1200 | tests/memory/test_graph_edge_weighting.py | Prior coderabbit marker/asyncio finding (task-15722) non-promotable; asyncio_mode='auto' runs async tests without the decorator. | Same file/root cause as id 1200/1204: test at line 424 plus 15 others lack markers. Fix via single module-level `pytestmark = pytest.mark.unit`; @pytest.mark.asyncio is redundant under asyncio_mode='auto'. |
| 1212 | no-fix | tests/memory/test_graph_edge_weighting.py | none | **Split verdict (see #1200).** pyproject sets asyncio_mode='auto' so @pytest.mark.asyncio is unnecessary; the 4 cited async tests pass (verified). No test in this file carries any marker (grep count=0), so a lone @pytest.mark.unit would be inconsistent. No --strict-markers enforcement. |
| 1216 | no-fix | tests/memory/test_graph_edge_weighting.py | none | **Split verdict (see #1200).** asyncio_mode='auto' makes @pytest.mark.asyncio redundant; test_cluster_expansion_runs_from_seed... passes as-is. Entire file is markerless (no pytestmark, 0 mark occurrences); adding a single unit marker contradicts the file's established style. |
| 1220 | no-fix | tests/memory/test_graph_edge_weighting.py | none | **Split verdict (see #1200).** test_service_wires_cluster_recall_flags_to_reader (sync, line 273) lacks a marker, but NO test in this file has any @pytest.mark/pytestmark. Markers aren't enforced (no --strict-markers). Adding one to one function would be incoherent with file convention. |
| 1224 | no-fix | tests/memory/test_graph_edge_weighting.py | none | **Split verdict (see #1200).** asyncio_mode='auto' makes @pytest.mark.asyncio redundant (test passes, verified). File has zero markers anywhere, so adding a unit/slow/integration marker only here would be inconsistent; no strict-markers policy mandates it. |
| 1228 | no-fix | tests/memory/test_knowledge_graph_clustering.py | none | Tests at lines 49-317 lack docstrings, but that matches the codebase norm (e.g. test_skill_discovery_rules.py has many docstring-less tests). Neither .coderabbit.yaml test instructions nor any project guideline require per-test docstrings. File already has module-level pytestmark=unit. |

#### .gobby — 11 findings (0 fix / 11 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 73 | no-fix | .gobby/wiki/.gwiki/research-session.json | none | File is untracked (git ls-files .gobby/wiki/ empty) and gitignored: .gitignore line 239 has '.gobby/wiki/.gwiki/'; git check-ignore confirms it's ignored. Machine-specific path is not committed. Already addressed. |
| 78 | no-fix dup→#73 | .gobby/wiki/.gwiki/research-session.json | none | File is NOT tracked by git (git ls-files empty) and .gobby/wiki/.gwiki/ is already gitignored at .gitignore:239. It is local-only state, not in source control, so the absolute path is harmless. |
| 111 | no-fix dup→#73 | .gobby/wiki/.gwiki/research-session.json | none | File is gitignored and untracked (git check-ignore matches, git ls-files empty). It is an ephemeral local research-session artifact, not committed; editing it has no repo effect and it regenerates. |
| 143 | no-fix dup→#73 | .gobby/wiki/.gwiki/research-session.json | none | False positive. File is gitignored (.gitignore:239 .gobby/wiki/.gwiki/) and untracked. It is a local runtime research-session snapshot with user-specific session_id/project_id; the absolute root path is correct local state, not portable config. |
| 221 | no-fix | .gobby/plans/completed/one-surface-activity-panel-migration-v2.md | none | Cited path .gobby/plans/one-surface-activity-panel-migration-v2.md no longer exists; file was moved to .gobby/plans/completed/ (archived/completed). The 22 implementation_domain:backend labels are historical; dispatch already ran, so editing has no operational effect. |
| 290 | no-fix dup→#221 | .gobby/plans/completed/one-surface-activity-panel-migration-v2.md | Memory 22f90fdd: For release review cleanup do NOT edit completed/abandoned plan docs to satisfy CodeRabbit; exclude archived plan paths instead. | Cited path .gobby/plans/one-surface-activity-panel-migration-v2.md no longer exists; plan moved to .gobby/plans/completed/ (commit b94679b62). Completed plan is finished work; per project policy do not retroactively edit archived plans for CodeRabbit. |
| 292 | no-fix dup→#221 | .gobby/plans/completed/one-surface-activity-panel-migration-v2.md | Memory 22f90fdd: Do NOT edit completed/abandoned plan docs for CodeRabbit feedback; exclude archived plan paths from CodeRabbit. | Same archived/completed plan as 290. Cited active path is gone (moved to completed/ in commit b94679b62). The implementation_domain: backend lines still exist there but the migration is done; editing a completed plan's manifest serves no purpose per project policy. |
| 820 | no-fix | .gobby/memories.jsonl | Memories are managed by the gobby-memory system; CLAUDE.md excludes .gobby/memories.json from CodeRabbit analysis. | memories.jsonl is a system-managed memory data file (2045 lines), not source code. Hand-editing memory entries to tag them historical is outside code-fix scope and contradicts the memory-system-managed approach; should be handled via the memory tooling, not a manual edit. |
| 824 | no-fix dup→#820 | .gobby/memories.jsonl | Memory 45199ca4: coderabbit skill treats findings as leads, verify current code; memories file is a synced artifact. | Stale. Line 1987 of memories.jsonl is now an embeddings-policy note, not the cited MemoryPage/KnowledgeGraph text. memories.jsonl is an auto-synced artifact (commit 2f0e3f75a 'gobby: sync tasks/memories'); commit 6a5c64a8 already excludes it from CodeRabbit. Editing it is futile. |
| 1100 | no-fix dup→#820 | .gobby/memories.jsonl | Review Lesson memory-jsonl-canonical-delete-stale: delete contradicted canonical memories via gobby-memory, never edit generated memories.jsonl directly. | Process guidance, not a code defect. The cited concern is already the project's canonical lesson (memory id 01d0fddd, recalled; also memory.jsonl L17). It prescribes a review workflow (use gobby-memory ops), not a current-code change; nothing to fix in a read-only/code sense. |
| 1104 | no-fix dup→#820 | .gobby/memories.jsonl | memory-jsonl-canonical-delete-stale: manage contradictions via canonical store, not jsonl line edits. | Stale/false: line numbers shifted. The #17038 memory (now L785) says the legacy integrations page subtree was deleted; cited L1499 is about HookManager/SQLite, not #17038. No memory states 'legacy page remains'; L1504 (#17036 added the Integrations Activity tab) is complementary, not contradictory. |

#### src/gobby/memory — 10 findings (7 fix / 3 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 86 | no-fix | src/gobby/memory/dream/apply.py | none | Current code (lines 197-198) gates on 'action.action == "refresh" and action.content' BEFORE calling _required_memory_id. There is no redundant early call when content is empty; validation already happens only after the content check. Already correct. |
| 243 | **fix** | src/gobby/memory/dream/apply.py | none | Line 212 `action.action=='refresh' and action.content` drops tag-only refreshes to cursor-advance branch (223-224). update_memory (memories.py:381-386) only sets content/tags when not None, so content=None tag-only is safe. Drop the content check. |
| 245 | no-fix | src/gobby/memory/recall.py | none | SessionSource is class SessionSource(str, Enum) (events.py:75). PARENT_USER_PROMPT_SOURCES holds enum members; str-Enum makes `'claude' in {SessionSource.CLAUDE}` True. Raw event.source check at line 131 works for both str and enum; only a cosmetic consistency nit. |
| 312 | **fix** | src/gobby/memory/recall.py | Memory aa5485c2 (promotable): 'Bound poll request timeouts by the user-visible overall timeout' directly supports sharing one timeout budget across synthesis+selection. | Synthesis wait_for (lines 316-318) and selection wait_for (lines 415-417) each use the full self.config.timeout, so a single recall can take ~2x budget. Share one budget: subtract synthesis elapsed and pass remaining to the selector wait_for. (Cited line range 345-381 is slightly off; symbols/issue match.) |
| 1018 | **fix** | src/gobby/memory/dream/service.py | none | _reconcile (lines 379-384) logs `logger.warning("Memory dream reconcile failed: %s", exc)` without exc_info=True and without run/totals context. Add exc_info=True and run correlation per structured-logging guideline. |
| 1022 | **fix** | src/gobby/memory/dream/service.py | none | Lines 232-233 `except asyncio.CancelledError: raise` re-raises without persisting a terminal status, so cancelled runs stay in started/running forever. record_run_failure helper already exists; persist a terminal status before re-raising. |
| 1026 | no-fix | src/gobby/memory/dream/truth_digest.py | none | _CONFIG_ALLOWLIST is hub_backend/daemon_port/bind_host — none match _SECRET_PATTERN, so the path-name check (lines 78-79) currently filters nothing. It is defense-in-depth against future allowlist edits; the value guard at line 84 is the real filter. No defect, no allowlisted attr is blocked. |
| 1164 | **fix** | src/gobby/memory/dream/cron.py | none | Line 39 still annotates daemon_config as Any. Add TYPE_CHECKING import of DaemonConfig and annotate `daemon_config: "DaemonConfig \| None" = None` for type safety (value is consumed by build_current_truth_digest). |
| 1168 | **fix** | src/gobby/memory/dream/planner.py | Memory dream replaced legacy memory cleanup; payloads here derive from user memory candidates, so logging full actions can leak content. | Lines 139-146 and 149-156 log full invalid_actions/raw_actions in extra. Replace with safe metadata (len()/type) while keeping project_id and candidate_ids. |
| 1172 | **fix** | src/gobby/memory/services/knowledge_graph/reader.py | none | Sole call site (line 429) passes _find_cluster_entity_keys(seed_keys, seed_keys, ...); source_keys (expansion) and seed_keys (exclusion) are always identical. Consolidate to one parameter throughout signature/usages. |

#### tests/communications — 10 findings (0 fix / 10 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 399 | no-fix | tests/communications/adapters/test_slack.py | none | Line 301 `assert adapter.verify_webhook(payload, headers, "") is True` is correct and clear in context; adding an explanatory comment is a pure cosmetic nit, no defect in current code. |
| 401 | no-fix | tests/communications/test_maintenance.py | none | autouse=True fixture (lines 8-13) is intentional and harmless for this single-loop module; the only other test uses the mock. Removing autouse or adding a docstring is optional style, not a defect. |
| 403 | no-fix | tests/communications/test_manager.py | none | pyproject.toml:151 sets asyncio_mode="auto", so bare `async def test_update_channel_enabled_reinitializes_and_refreshes_runtime_state` (line 1145) runs correctly without @pytest.mark.asyncio. Decorator is redundant. |
| 405 | no-fix | tests/communications/test_manager.py | none | asyncio_mode="auto" (pyproject.toml:151) runs `async def test_add_channel_returns_inactive_with_init_error_on_adapter_failure` (line 805) without the decorator. Sibling tests in file already mix decorated/undecorated; not a defect. |
| 407 | no-fix | tests/communications/test_manager.py | none | asyncio_mode="auto" (pyproject.toml:151) handles bare `async def test_update_channel_disable_stops_runtime_traffic` (line 1123). The @pytest.mark.asyncio decorator is not required and would be redundant. |
| 409 | no-fix | tests/communications/test_manager.py | none | asyncio_mode="auto" (pyproject.toml:151) runs `async def test_remove_channel_deletes_inactive_db_row_by_name` (line 1021) correctly without @pytest.mark.asyncio. Decorator unnecessary. |
| 411 | no-fix | tests/communications/test_reactions.py | none | asyncio_mode="auto" (pyproject.toml:151) executes bare `async def test_handle_reaction_lookups_run_off_event_loop` (line 126) as a coroutine. @pytest.mark.asyncio is redundant. |
| 413 | no-fix | tests/communications/test_router.py | none | asyncio_mode="auto" (pyproject.toml:151) runs `async def test_router_loads_rules_off_event_loop` (line 80) without the decorator. No change needed. |
| 415 | no-fix | tests/communications/test_session_bridging.py | none | asyncio_mode="auto" (pyproject.toml:151) handles bare `async def test_inbound_identity_and_store_calls_run_off_event_loop` (line 347). @pytest.mark.asyncio is unnecessary. |
| 417 | no-fix | tests/communications/test_webhook_verification.py | none | asyncio_mode="auto" already covers async tests so @pytest.mark.asyncio is not needed. The @pytest.mark.unit marker is an unenforced cosmetic convention applied inconsistently across siblings; absence is not a defect. |

#### tests/ai — 9 findings (2 fix / 7 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 132 | no-fix | tests/ai/test_text_generation.py | none | Already fixed/moved. Test test_text_generation_service_falls_back_between_named_local_endpoints now at line 561 with @pytest.mark.asyncio at line 560; pytest imported (pytestmark unit at line 39). Cited 283-323 no longer holds the test. |
| 157 | no-fix | tests/ai/test_text_generation.py | none | False positive. pyproject.toml line 151 sets asyncio_mode='auto' so async tests run without the marker; function (now ~line 561) also already has @pytest.mark.asyncio directly above it (line 560). import pytest present (line 10). |
| 158 | no-fix | tests/ai/test_text_generation.py | none | Symbol gone. test_text_generation_service_resolves_bare_local_candidate_to_named_endpoint no longer exists anywhere in tests/ai (renamed/removed). Also asyncio_mode='auto' (pyproject.toml:151) makes marker unnecessary regardless. |
| 161 | no-fix | tests/ai/test_text_generation.py | none | False positive. asyncio_mode='auto' (pyproject.toml:151) auto-runs async tests; json_candidates_do_not_fallback_to_profile_defaults (now ~line 981) already has @pytest.mark.asyncio at line 980. import pytest present (line 10). |
| 163 | no-fix | tests/ai/test_text_generation.py | none | False positive. asyncio_mode='auto' (pyproject.toml:151) auto-runs async tests; profile_only_expands_profile_defaults (now line 775) already has @pytest.mark.asyncio at line 774. import pytest present (line 10). |
| 165 | no-fix | tests/ai/test_text_generation.py | none | False positive. asyncio_mode='auto' (pyproject.toml:151) auto-runs async tests; candidate_list_is_exhaustive_for_unavailable_override (now ~line 863) already has @pytest.mark.asyncio at line 862. import pytest present (line 10). |
| 208 | **fix** | tests/ai/test_text_generation.py | Review lesson 'preserve-explicit-zero-limit' (promotable): explicit empty/zero defaults must be kept via `is not None`, not `or` fallbacks. | FakeCodexAppServerClient.__init__ moved to ~1480; line 1499 self.events = events or [...] still overwrites an explicitly passed empty list (also 1455 events, 1498 thread_ids). Use events if events is not None else [...]. |
| 336 | no-fix | tests/ai/test_capability_registry.py | none | select() path for named local already covered: main loop calls registry.select(TEXT_GENERATE, provider='local:lm-studio'/'local:ollama'); test_select_named_local_provider_does_not_match_other_endpoints and test_select_reports_unavailable_provider_reason cover error paths; binding(...,'local') is None asserted. Cited 169/262-268 lines no longer map. |
| 1054 | **fix** | tests/ai/test_vision_extraction.py | none | Line 89 asserts result.ocr_text is None and result.text == 'extracted:/tmp/image.png' (line 85, non-None), so line 90 `assert result.ocr_text != result.text` is logically guaranteed and adds no value. Remove it. |

#### tests/workflows — 8 findings (4 fix / 4 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 69 | no-fix | tests/workflows/test_skill_discovery_rules.py | none | The yaml-skill CONDITION (lines 2218-2234) contains no '.clang-format'/'.clang-tidy'; only yaml extensions and '.yamllint'. The clang entries (lines 965,1021,1110,1180) belong to C/C++ test sections, which is correct. Already fixed. |
| 71 | no-fix | tests/workflows/test_skill_discovery_rules.py | none | SKILL_DISCOVERY_RULES set (lines 58-76) already includes 'require-json-skill' (line 68) and 'require-yaml-skill' (line 76). Already added. |
| 214 | no-fix | tests/workflows/test_skill_discovery_rules.py | none | Condition already matches `.cargo/config`/`.cargo/config.toml` including the dir segment (lines 593-595), mirroring the rule template require-rust-skill.yaml. False-positive risk is negligible; pathlib isn't in the SafeExpressionEvaluator. Test would need rewriting both. |
| 350 | **fix** | tests/workflows/test_context_handoff_fencing.py | CodeRabbit findings are leads; verify current code (mem 45199ca4) | _inject_template still does '\n'.join(templates) (lines 26-31), so BEGIN/END fencing asserts can match across separate templates. Return a list and assert fencing per-template. |
| 352 | **fix** | tests/workflows/test_task_claim_state.py | none | resolve_target_task_id and task_edited_file_set have no direct unit tests anywhere; target_task_has_edits only appears as a rule string, not unit-tested. TestActiveTaskIdForEdit (103-123) covers only active_task_id_for_edit. Add direct tests. |
| 354 | **fix** | tests/workflows/test_task_commit_project_path_guardrail_rule.py | none | temp_db.execute UPDATE flipping source template-&gt;installed at lines 21-23 has no explaining comment. Add an inline comment noting it simulates post-installation state. |
| 356 | **fix** | tests/workflows/test_task_enforcement_rules.py | none | _status_gate_variables line 79 still uses 'claimed = claimed_tasks or {...}', treating {} as None. Change to 'if claimed_tasks is not None else {...}' so callers can pass {} for no claims; task_claimed=bool(claimed) stays. |
| 1236 | no-fix | tests/workflows/test_skill_discovery_rules.py | none | test_reset_skill_injection_clears_context7_nudge (line 135) lacks a docstring, but the 3080-line file is mixed-style: many tests (lines 3052,3055,3067,3070,3073) also have none. No project guideline mandates test docstrings; cosmetic and inconsistent to single out. |

#### tests/servers — 6 findings (3 fix / 3 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 25 | no-fix | tests/servers/routes/test_agent_spawn_routes.py | none | No duplicate. L160 is the single 'assert updated.claimed_by_session_id == data["conversation_id"]'; L161 is blank. Other identical asserts at L182/L214 are in separate test functions, not redundant. |
| 27 | no-fix | tests/servers/routes/test_configuration_routes.py | Configuration API routes decomposed: configuration_secrets.py holds password validation (matched project memory). | Already aligned. test_import_config_non_string_falkordb_password_rejected (line 1446-1447) asserts 422 and 'FalkorDB password must be a string'; configuration_secrets.py:45 raises that exact ValueError. The 400/'Invalid imported configuration' is the separate legacy requirepass test. |
| 92 | no-fix | tests/servers/test_plan_approval_response.py | none | Module has 'pytestmark = pytest.mark.unit' at line 17, so every test in the file is already categorized as unit. No per-test category marker needed. |
| 342 | **fix** | tests/servers/routes/test_configuration_routes.py | none | test_import_config_store_non_string_falkordb_password_rejected only asserts ConfigStore(postgres_db).get(FALKOR_PASSWORD_KEY) is None. Add SecretStore(postgres_db).get('falkordb_password') is None to confirm the secrets table also got no partial write on rejection. SecretStore exists at storage/secrets.py. |
| 344 | **fix** | tests/servers/routes/test_llm_routes.py | none | test_llm_status_returns_registry_snapshot only asserts 'local' not in providers. server_with_llm fixture configures local endpoint 'lm-studio' with vision_extract=True, so add positive assertion 'local:lm-studio' in providers for text_generate and vision_extract to catch the alias-not-published regression. |
| 429 | **fix** | tests/servers/routes/test_communications_routes.py | none | Test line 35 only asserts assert_awaited_once(). Route calls handle_inbound(channel_name, payload, headers, raw_body=body). Strengthen to assert_awaited_once_with on channel ('discord') and payload ({'type':1}); avoid asserting exact headers (brittle). |

#### src/gobby/cli — 5 findings (2 fix / 3 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 3 | no-fix | src/gobby/cli/init.py | none | Current init() (lines ~63-66) already emits 'Initialized project...', Project ID, Config path, then click.echo() blank line, THEN the 'Tip: For first-time setup...' line. Order already matches the request. |
| 5 | no-fix | src/gobby/cli/installers/codex.py | none | Already implemented. _is_codex_dispatcher_command (L461) handles str via _normalize_hook_command_part (backslash-&gt;slash) and Sequence by iterating/coercing parts, checking 'hook_dispatcher.py' and '--cli=codex'. Covers Windows paths and arg lists. |
| 7 | no-fix | src/gobby/cli/tasks/_utils/rendering.py | none | Already implemented. _build_rendered_row falls back to claimed_task_owner_map.get(task.id) (L121-122) when state owner is empty; crud.py builds that map via get_claimed_task_owners() which derives owners from active sessions' session_task. Matches the request. |
| 184 | **fix** | src/gobby/cli/install_setup.py | none | Confirmed: both 'import shutil' (L11) and 'from shutil import copy2' (L20) present; copy2 used once at L305. Drop the redundant from-import and change the single call site to shutil.copy2(...). Purely cosmetic. |
| 186 | **fix** | src/gobby/cli/tasks/_utils/claims.py | none | Confirmed: L90-91 except (RuntimeError, json.JSONDecodeError, KeyError) logs logger.debug(f'Failed to get claimed task owners: {e}') with no exc_info. Add exc_info=True to preserve traceback; keep return {}. |

#### src/gobby/workflows — 5 findings (5 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 277 | **fix** | src/gobby/workflows/condition_helpers.py | none | _statement_uses_tainted_cwd (L244-248) ast.walks the whole statement incl. nested bodies, so a re-assignment of project_path inside an if/with/for block followed by cwd=project_path yields a false positive (initial taint set includes project_path). Restrict walk to top-level nodes and let _nested handle inner blocks with intra-block taint. |
| 279 | **fix** | src/gobby/workflows/state_manager.py | none | record_edited_file L356 does json.loads(row['variables']) but session_variables.variables is JSONB (baseline schema L726), which psycopg returns as native dict -&gt; TypeError. get_variables() (L161-168) handles both dict and str. Reuse that decode logic. Same bug in L309/L409. |
| 334 | **fix** | src/gobby/workflows/task_claim_state.py | none | resolve_target_task_id uses raw_ref = str(task_ref) without .strip(); inputs like ' #12 ' or 'uuid-1\n' fail membership checks against _claimed_tasks/_task_edited_files. Add .strip() and normalize display_ref comparisons with str(display_ref).strip(). |
| 1046 | **fix** | src/gobby/workflows/definitions.py | none | acknowledge_variable: str \| None = None at line 127 sits under the `# block` group comment but has no field-specific documentation. Add an inline comment describing it as the session/workflow variable that, when set, lets an agent acknowledge/bypass the block. |
| 1050 | **fix** | src/gobby/workflows/engine/evaluation.py | none | Lines 363-364 `except Exception as e: logger.debug(f"Metrics recording failed: {e}")`. Keep the broad catch (metrics must never break rule eval) but add exc_info=True so the debug log carries the traceback. Narrowing exception types is not advisable here. |

#### tests/config — 4 findings (2 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 17 | no-fix | tests/config/test_app_config.py | none | Already correct. test_export_config_to_yaml_with_none_path_uses_default now lives at L1234, properly indented 4 spaces as a TestSaveConfig method with self/temp_dir params. Cited L1075-1077 is an unrelated different test. |
| 281 | **fix** | tests/config/test_app_config.py | none | TaskExpansionConfig.profile defaults to FeatureProfile.HIGH (config/tasks.py L106-109). Test sets DB row to 'feature_high' and asserts ==HIGH (L888), so it passes even if override is ignored - vacuous. Set in-memory default to a different profile to prove DB value overrides default. |
| 419 | no-fix | tests/config/test_communications.py | none | pytest.mark.unit is a non-enforced categorization marker; ~15 of 25 tests/config files lack pytestmark. Suite runs fine without it. Purely cosmetic consistency nit, not a defect. |
| 1192 | **fix** | tests/config/test_code_index_config.py | Prior coderabbit 'marker' finding (task-15722) was logged as a non-promotable, low-value review signal. | File has no pytestmark; every sibling tests/config/*.py uses module-level `pytestmark = pytest.mark.unit`. Add it after imports for convention consistency (--strict-markers is not enabled, so not a hard failure). |

#### tests/sessions — 4 findings (2 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 106 | no-fix | tests/sessions/test_summarize.py | none | Line 78 already reads `def __init__(self, session: MagicMock) -&gt; None:` with the -&gt; None annotation present. Suggestion already applied. |
| 212 | **fix** | tests/sessions/transcripts/test_gemini_thinking_collapse.py | 0.5.0 triage directive: findings should not be left as A-or-B; commit to parametrizing the fixture (one approach), not 'either/or'. | QwenTranscriptParser and GeminiTranscriptParser both subclass TypedJsonTranscriptParser, neither overrides collapse logic, and share ClassVar session_assistant_message_type='gemini'. Parametrize parser fixture to add Qwen coverage cheaply. |
| 287 | no-fix | tests/sessions/transcripts/test_gemini_thinking_collapse.py | Memory 962e9475: Qwen Code is a Gemini-CLI fork; behavior identical to gemini's. | QwenTranscriptParser inherits thoughts-&gt;thinking-block collapse unchanged from TypedJsonTranscriptParser (qwen.py only overrides _extract_usage). Qwen collapse IS exercised by test_qwen_native_session_matches_golden_fixture (session_expected.json includes the thinking block). No coverage gap of substance. |
| 346 | **fix** | tests/sessions/test_summarize.py | none | test_full_summary_uses_qwen_parser_for_qwen_source verifies the Qwen parser was selected but not that the Qwen template flowed to the LLM. generate_summary calls llm_service.call_feature(config, prompt,...) with prompt rendered from template 'Qwen:\n{...}'. Assert call_feature called once with prompt containing 'Qwen:\n'. |

#### src/gobby/communications — 3 findings (2 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 367 | **fix** | src/gobby/communications/adapters/telegram.py | Telegram polling/offset behavior context (mem b2f9b11d) | poll() appends every update_id to _pending_update_ids (line 304) but only acknowledges ones yielding messages (316). The head-of-queue loop (318-325) then stalls forever on a leading non-message update, refetching it. Append only when msg_list is non-empty. |
| 369 | **fix** | src/gobby/communications/outbound.py | none | send_proactive now returns CommsMessage but docstring is one line and omits the return type / platform_message_id guidance. Update docstring to document it returns CommsMessage and callers read message.platform_message_id. |
| 371 | no-fix | src/gobby/communications/webhook_verification.py | Bound poll/timeouts lesson (mem aa5485c2) - not contradicting | The sole caller handle_webhook (inbound.py) already has 'except TimeoutError as exc: raise ValueError(...)'. Timeout is intentionally surfaced and handled as verification failure; catch-and-return-False would be wrong. Finding's 'not handled' premise is false. |

#### src/gobby/hooks — 3 findings (1 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 9 | no-fix | src/gobby/hooks/factory.py | none | Already fixed. factory.py imports MemoryBackupManager (L33) and the create() param is annotated 'memory_sync_manager: MemoryBackupManager \| None' (L127); also at L468. No Any\|None remains. |
| 45 | **fix** | src/gobby/hooks/dispatchers/mcp.py | none | Comment is inaccurate. _project_memory_next_line_budget appends [""] itself to body_lines; _render_project_memory only joins OPEN_TAG/body/CLOSE_TAG and does not produce a trailing blank. Reword comment to say [""] reserves room for the next body line. |
| 231 | no-fix | src/gobby/hooks/event_handlers/_misc.py | none | No SessionStorageError class exists in codebase. update_session_status (_manager.py:315-326) already wraps everything in try/except Exception, logs exc_info=True, returns False; it never raises. The outer except is defensive last-resort. |

#### tests/storage — 3 findings (1 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 181 | no-fix | tests/storage/sessions/test_metadata.py | none | Already fixed. Test now uses CREATE OR REPLACE FUNCTION (line 678) and wraps assertions in try/finally (lines 700-719) that DROP TRIGGER IF EXISTS fail_summary_state_update and DROP FUNCTION IF EXISTS fail_summary_state_update_fn(). |
| 860 | **fix** | tests/storage/test_storage_tasks.py | none | Valid (shifted to lines 740-753). test_validation_status_error_round_trips sets validation_feedback='generation unavailable' but only asserts reloaded.validation_status=='error'. Add assert reloaded.validation_feedback=='generation unavailable' to confirm feedback round-trips. |
| 1232 | no-fix | tests/storage/test_storage_tasks.py | none | _planning_needs_review (lines 43-49) returns a Task and lacks '-&gt; Task', but mypy is scoped to src/ only ('uv run mypy src/'), not tests/. Adding it needs a new Task import. Other helpers here (e.g. _assert_stage_state's task param) are also loosely typed; trivial test-only nit. |

#### pyproject.toml — 2 findings (1 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 43 | no-fix | pyproject.toml | none | Already done. Line above 'aiohttp&gt;=3.14.0' carries the inline justification comment '# aiohttp &gt;=3.14.0 keeps the dependency beyond current aiohttp CVE advisories.' requires-python remains '&gt;=3.13'. Remaining asks are process advice, not code changes. |
| 82 | **fix** | pyproject.toml | none | Line 22 comment exists but is generic ('keeps the dependency beyond current aiohttp CVE advisories'). Expand to cite specific CVEs to match the style of fastmcp/python-multipart comments listing CVE IDs. |

#### src/gobby/ai — 2 findings (0 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 32 | no-fix | src/gobby/ai/_text_generation_service.py | none | Already fixed. text_generation.py is now a 40-line shim; logic moved to _text_generation_service.py. generate_json error path (line 186) already uses 'len(attempted_candidates) == 1', not len(candidates). No fallback_candidates concept exists. |
| 34 | no-fix | src/gobby/ai/_text_generation_service.py | none | Already fixed. generate_result error path (line 157) already uses 'len(attempted_candidates) == 1 and last_error is not None' instead of len(candidates). Cited text_generation.py:161-166 no longer exists (file is a 40-line shim). |

#### src/gobby/llm — 2 findings (0 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 233 | no-fix | src/gobby/llm/resolver.py, src/gobby/ai/registry.py, tests/llm/test_context_window.py | none | registry.py:541 'local'/'openai' are EMBEDDING providers in _embedding_binding, a separate namespace from SUPPORTED_PROVIDERS (claude/codex/gemini = LLM text). AIAdapterStyle.LOCAL exists. Test provider='local' expects None (correct). Finding conflates namespaces. |
| 300 | no-fix | src/gobby/llm/context_windows.py | none | gemini-3.5-flash = 1_048_576 (L96, L127) is the accurate 1Mi-token (2^20) window documented for Gemini flash models; lowering to 1_000_000 for 'consistency' reduces precision. It's a speculative future model entry with no authoritative source. Existing value is defensible; not a correctness issue. |

#### src/gobby/utils — 2 findings (0 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 275 | no-fix | src/gobby/utils/injected_context.py | none | strip_injected_context (the only caller) cleans transcript/summary/digest text in handoff and memory paths (handoff.py, _handoff.py, digest.py). Raising ValueError on a stray END sentinel — which can legitimately appear in user/code content — would break summary/handoff generation. parts.clear() is intentional tolerant cleanup; the suggestion is harmful. |
| 332 | no-fix | src/gobby/utils/injected_context.py | Memory 77fb175f confirms injected-context begin/end marker conventions are an established project convention. | parts.clear() on a lone END marker is intentional and tested: test_strip_lone_end_from_start asserts input 'injected\n{END}\nafter' -&gt; 'after', i.e. pre-marker content is deliberately discarded. Removing parts.clear() would break that test. |

#### tests/agents — 2 findings (2 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 395 | **fix** | tests/agents/test_kill.py | none | Test patches only gobby.agents.kill.os.kill; the non-Windows signal path uses _signal_process_group -&gt; os.killpg(os.getpgid(pid),sig). The recycled-pid path returns early so no signal fires, but the assertion is weak. Add @patch for os.killpg (and os.getpgid) to make the guard meaningful. |
| 397 | **fix** | tests/agents/tmux/test_wsl_compat.py | none | convert_windows_path_to_wsl docstring documents absolute drive and UNC paths but not that drive-relative (C:, C:Users\foo) and unrecognized formats pass through unchanged (regex requires a separator after the colon). Add a docstring note; tests already assert this behavior. |

#### tests/cli — 2 findings (0 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 13 | no-fix | tests/cli/test_validation_cli.py | none | Lines shifted; no duplicate. Cited L585-586 now hold a function signature/params. The only claimed_by_session_id=None assignment in that test is single at L599; L600 is .stages. No redundant assignment. |
| 15 | no-fix | tests/cli/test_validation_cli.py | none | Lines shifted; no duplicate. Cited L539-540 now hold a docstring and 'mock_task = MagicMock()'. The only claimed_by_session_id=None is single at L554; L555 is .stages. No redundant assignment. |

#### tests/fixtures — 2 findings (0 fix / 2 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 283 | no-fix | tests/fixtures/transcripts/qwen/session.json | Memory d0ab4d1c (#15811): Qwen no longer subclasses Gemini but intentionally keeps Gemini-compatible message types via TypedJsonTranscriptParser. | session_assistant_message_type ClassVar = 'gemini' (typed_json.py L81); QwenTranscriptParser does NOT override it. session_expected.json L35 parses msg-2 (type=gemini) as role=assistant. Changing to 'qwen' would drop the message (L360 returns []) and break the golden test. False positive. |
| 338 | no-fix | tests/fixtures/transcripts/qwen/session.json | Memory d0ab4d1c (task #15811): QwenTranscriptParser subclasses TypedJsonTranscriptParser and intentionally keeps Gemini-compatible payloads. | type:'gemini' in the qwen fixture is intentional. qwen.py docstring: 'Qwen currently stores Gemini-compatible JSON/JSONL transcript payloads.' Changing to 'qwen' would make the fixture less representative of real Qwen transcripts. |

#### .github — 1 findings (0 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 816 | no-fix | .github/workflows/ci.yml | Project deliberately maintains pip-audit ignores/exclusions for unfixable CVEs (FastAPI exclusion #15213); torch ignore is an intentional maintainer decision. | Current comment (line 393) explicitly states CVE-2025-3000 affects torch &lt;=2.12.0 with 'no fix available', updated after the finding. torch is pinned ==2.11.0 (&lt;=2.12.0). The ignore is a deliberate decision contradicting the finding's 'fixed in 2.10.0' premise. |

#### .gitignore — 1 findings (0 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 74 | no-fix | .gitignore | none | .gitignore line 239 already contains '.gobby/wiki/.gwiki/' (matching '.gobby/wiki/meta/health/' style at line 240). git check-ignore confirms gwiki artifacts are ignored. Already added. |

#### GEMINI.md — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1108 | **fix** | GEMINI.md | none | Confirmed: GEMINI.md L5-7 and L9-11 duplicate the '## Guiding Principles' heading and 'These are enforced by hooks, rules and workflows.' line back-to-back before the numbered list. Remove the second occurrence (L9-11). |

#### src/gobby/config — 1 findings (0 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 188 | no-fix | src/gobby/config/feature_base.py | normalize_feature_candidate intentionally only canonicalizes Claude family aliases (#15652); non-claude candidates preserved verbatim. | Harmful. normalize_feature_candidate (L55-70) deliberately returns non-claude candidates unchanged; test L117-121 requires preserving 'google/gemma-4-26b-a4b-qat' case/form. Lowercasing arbitrary provider model IDs would corrupt case-sensitive identifiers. |

#### src/gobby/github_triage — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 373 | **fix** | src/gobby/github_triage/service.py | none | AUTO_CLOSE_DUPLICATE_SCORE = 0.97 at line 43 lacks a type annotation. Annotate as 'AUTO_CLOSE_DUPLICATE_SCORE: float = 0.97' to satisfy strict mypy / type-hint guidelines. |

#### src/gobby/runner_init — 1 findings (0 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1030 | no-fix | src/gobby/runner_init/orchestration.py | none | Every cron-registration block in this file (lines 274,291,311,331,341) uses `except Exception as e:` with logging as a deliberate fail-soft so one subsystem cannot abort daemon startup. Narrowing only the code-index block is inconsistent and fragile; guideline targets bare except, not logged broad catches. |

#### src/gobby/runner_lifecycle_shutdown.py — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1176 | **fix** | src/gobby/runner_lifecycle_shutdown.py, src/gobby/runner_lifecycle_subsystems.py | none | _code_index_startup_prune_task is set in subsystems.py (375/377) but never cancelled in _cancel_periodic_tasks (shutdown.py 296-340). Add _cancel_runner_task(runner, '_code_index_startup_prune_task') to prevent the leak. |

#### src/gobby/runner_maintenance.py — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 387 | **fix** | src/gobby/runner_maintenance.py | none | Lines 387-393 still mix styles: deleted_messages log uses f-string, deleted_attachments log uses %-style. Unify to one style (prefer %-style lazy logging for both). |

#### src/gobby/shutdown_intent.py — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 88 | **fix** | src/gobby/shutdown_intent.py | none | Line 102 still uses 'except OSError:'. json.dumps()/write_text() can raise non-OSError (TypeError, UnicodeError); broadening to 'except Exception:' ensures partial-marker cleanup runs for all failures, matching the inner 'except Exception' at line 109. |

#### tests/ (new test for src) — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1136 | **fix** | tests/ (new test for src/gobby/code_index/_storage/search_helpers.py) | none | Confirmed no test file references rows_by_ids or make_snippet (grep -rln across tests/ empty). Add coverage: rows_by_ids empty/single/multi IDs and placeholder behavior; make_snippet match, no-match fallback at 0, and start/end boundary cases. |

#### tests/build_pipeline — 1 findings (0 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 11 | no-fix | tests/build_pipeline/test_controls.py | none | No duplicate. L174 is the single 'assert updated.claimed_by_session_id is None'; L175 is a distinct 'assert updated.unattended is True'. No repeated assertion exists at the cited range. |

#### tests/code_index — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1188 | **fix** | tests/code_index/test_context.py | none | Test at line 101 sets graph_enabled=False AND embedding_enabled=False (line 105) but name only says 'graph_disabled'. Rename to ..._when_graph_and_embedding_disabled to match the config. |

#### tests/dispatch — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 421 | **fix** | tests/dispatch/test_dispatcher.py | none | Lines 682-685 still have two consecutive `if is_first_spawn:` blocks. Consolidate into one block holding both first_spawn_started.set() and `await release_first_spawn.wait()`; behavior unchanged, clearer. |

#### tests/integrations — 1 findings (1 fix / 0 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 425 | **fix** | tests/integrations/test_linear_graphql.py | none | Test at line 12-13 has only @pytest.mark.asyncio; it is mock-based unit work. unit marker is defined in pyproject and used in 1126 test files. Not strictly enforced (no --strict-markers requiring it) but adding @pytest.mark.unit aligns with documented categorization. |

#### tests/test_runner_shutdown.py — 1 findings (0 fix / 1 no-fix / 0 fence)

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 348 | no-fix | tests/test_runner_shutdown.py | none | pyproject.toml sets asyncio_mode = 'auto', so pytest-asyncio auto-collects async tests without @pytest.mark.asyncio. The two tests already run under auto mode (the whole file relies on it); import pytest is already present. Decorator is unnecessary. |
