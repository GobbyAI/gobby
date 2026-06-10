# Review: adapters

- **Scope:** `src/gobby/adapters/` — Claude (`claude_code.py`, `claude_contract.py`,
  `base.py`, `capabilities.py`, `degradation.py`), Codex app-server (`codex_impl/`:
  client, app_server_adapter, hooks_adapter, item_normalization, types, shared), ACP
  clients for Gemini/Qwen/Grok (`acp_client.py`, `acp_client_requests.py`,
  `acp_hook_adapter.py`, `grok_acp_client.py`, `gemini_acp_client.py`,
  `qwen_acp_client.py`, the thin `grok.py`/`gemini.py`/`qwen.py` wrappers), Droid
  (`droid.py`, `droid_contract.py`), and plan-mode keystroke automation
  (`plan_keystrokes.py`, `plan_options.py`). Cross-seam reads into hooks events/normalization,
  the chat backends (`servers/websocket/chat/backends/`), the plan-approval handlers, and
  tests. **Split boundary:** hooks-side normalization and the route/installer delivery seam
  were reviewed in #15778; the transcript parsers in #15779.
- **Reviewer:** Claude Fable 5 — 4-agent parallel fan-out, all Blockers synthesizer-verified.
- **Commit / branch:** `0.5.0` @ HEAD `849538a02` (working tree clean at review time).
- **Summary:** 6 Blocker · 13 Important · 6 Nit — the Claude adapter (the one fully-native
  hook path) is solid, but the protocol-bridge adapters (Codex app-server, ACP for
  Gemini/Qwen/Grok) lose enforcement at the bridge: tool/prompt block decisions never
  reach the CLI in the managed paths, the Codex JSON-RPC client mis-correlates approvals by
  id, and neither bridge drains subprocess stderr (a deadlock under verbose CLIs). Block
  semantics diverge per adapter (`block` honored on Claude/Codex/Droid, dropped on ACP).

## Findings

### [BLOCKER] Codex JSON-RPC id-space collision mis-routes approval requests and corrupts our request results
- **Where:** `codex_impl/client.py:899-918` (`_read_loop`) — verified: `if "id" in message:` → `future = self._pending_requests.get(request_id)` (`:900-903`); if a future exists and isn't done it's treated as a response to *our* outgoing request, only falling to `_handle_incoming_request` in the `elif "method"` branch.
- **Failure mode:** Codex assigns ids for its **outbound** requests (approval requests like `item/commandExecution/requestApproval`) from its own counter, independent of our client counter (both start small and increment → collision likely). When a Codex approval arrives with an `id` equal to one of our in-flight outgoing request ids, the loop resolves *our* future with the approval's `result` (→ `{}`) and never routes the approval to `_handle_incoming_request`. Net: (a) the approval is never answered → Codex blocks forever; (b) our legitimate request resolves with a bogus empty result. This is the only tool-gating path for daemon-managed app-server Codex sessions, so a collision silently hangs/bypasses enforcement. Per JSON-RPC 2.0 a request always has `method` and a response never does — disambiguation must key on `method`, not id-map membership. The one test uses non-colliding ids (1 vs 42), so the collision is unverified.
- **Minimal fix:** Order the dispatch request → response → notification: `if "method" in message and "id" in message: handle_incoming_request; elif "id" in message: resolve future; elif "method": notification`. Add a colliding-id test.
- **Confidence:** high (verified).

### [BLOCKER] Codex app-server stderr is piped but never drained → process deadlock
- **Where:** `codex_impl/client.py:160` (`stderr=subprocess.PIPE`); no reader exists (verified: the only stderr access is post-exit). Same shape in `acp_client.py:262` (drained only at `_read_exit_stderr:432`).
- **Failure mode:** A long-lived app-server writing diagnostics fills the ~64KB OS stderr pipe buffer; the process then blocks on its next stderr write → stops emitting stdout → the whole client wedges (every `_send_request` times out, no gating, no session tracking). Manifests as intermittent after enough stderr volume. The ACP `acp_client.py:42-48` comment claims it widens the "stdout/stderr reader limit" but that limit applies only to the (unread) reader, not the OS buffer — stderr is never read during the session.
- **Minimal fix:** `stderr=DEVNULL`/log file if unused, or a background drain task alongside the read loop.
- **Confidence:** high (verified both clients).

### [BLOCKER] Codex BEFORE_AGENT (UserPromptSubmit) gate cannot block in the app-server path
- **Where:** `codex_impl/app_server_adapter.py:107` (`turn/started` → `BEFORE_AGENT`) dispatched via `_handle_notification:309-337` as fire-and-forget (`loop.create_task(self._dispatch_hook_event(...))`); the computed `HookResponse` is only logged (`_log_notification_result`).
- **Failure mode:** For Codex, `BEFORE_AGENT` maps to `UserPromptSubmit` (`hooks/events.py:189-194`), a blocking hook on every other CLI. In the WS adapter `turn/started` flows only through the notification path, so any UserPromptSubmit-level block silently no-ops for daemon-managed Codex sessions — a gate that fires on Claude/Gemini does nothing here. (The separate chat backend `backends/codex.py:586` does honor lifecycle block; this gap is the daemon `CodexAdapter`.)
- **Minimal fix:** Route `turn/started` through a blocking path that calls `interrupt_turn` on `deny`/`block`, or explicitly downgrade BEFORE_AGENT to non-gating with a degradation record.
- **Confidence:** med-high (dispatch and mapping confirmed; intent is the one open assumption).

### [BLOCKER] ACP pre-tool block/deny is never enforced in managed web-chat sessions (Gemini/Qwen/Grok)
- **Where:** `servers/websocket/chat/backends/acp_session.py:135` (verified: `await self._apply_pre_tool_lifecycle(...)` — return value ignored), `backends/base.py` `_apply_pre_tool_lifecycle` (only queues deferred context, no action); the other seam `acp_client_requests.py:134-174` (`_handle_request_permission_request` auto-approves, never consults Gobby).
- **Failure mode:** A managed ACP chat tool_call fires `_apply_pre_tool_lifecycle`, but its `HookResponse` (which may carry `decision="block"`) only queues context — the tool is never declined; `session/request_permission` unconditionally auto-selects an allow option. The auto-approve docstring explicitly claims tools are gated "through Gobby's lifecycle/hook systems," but that path does not block. Codex's chat backend (`backends/codex.py:586-587`) declines on block — ACP is the outlier. Any `before_tool` rule/workflow block is silently lost.
- **Minimal fix:** Capture the `_apply_pre_tool_lifecycle` return; on `decision in ("deny","block")` suppress the tool_call or reject the `session/request_permission` answer. The two enforcement points must agree.
- **Confidence:** high (verified end-to-end; Codex parity confirms ACP is the outlier).

### [BLOCKER] ACP `terminal/create` executes the command before any block can be applied
- **Where:** `acp_client_requests.py:212-238` (`_handle_terminal_create_request` yields a tool_call StreamEvent then *unconditionally* `await _run_terminal_create(...)` → `asyncio.create_subprocess_shell(command)`).
- **Failure mode:** The agent-supplied command runs regardless of any pre-tool decision; there is no path for the consumer's lifecycle to signal back and skip execution. Combined with the previous finding, arbitrary shell commands an ACP agent requests bypass all Gobby tool gating. For Grok (`--always-approve --no-leader`) the CLI's own prompt is disabled too, so Gobby is the only intended gate and it is absent.
- **Minimal fix:** Consult the pre-tool lifecycle decision before running; on block, return a denied/cancelled terminal result and skip execution.
- **Confidence:** high on the unconditional path; med on per-CLI reachability.

### [BLOCKER] ACP subprocess stderr is never drained — verbose CLI deadlocks mid-turn
- **Where:** `acp_client.py:262` (`stderr=PIPE`), read only post-exit at `:432`; no background drainer.
- **Failure mode:** Node CLIs (Gemini/Qwen) are chatty on stderr (deprecation/telemetry/progress). When >~64KB is written during a turn, the child blocks on `write(stderr)` → agent hangs → prompt timeout. The 16 MiB reader limit protects only stdout. Manifests as flaky prompt timeouts.
- **Minimal fix:** Background task draining `self._process.stderr` (ring-buffered) for the process lifetime; cancel in `stop()`.
- **Confidence:** med-high (mechanism certain; depends on per-CLI stderr volume).

### [IMPORTANT] `decision="block"` from workflows yields `continue:True` on ACP — inconsistent with every other adapter
- **Where:** `acp_hook_adapter.py:323` — verified `should_continue = response.decision != "deny"`; emits `{"decision": response.decision, "continue": should_continue}`. Every other adapter treats `decision in ("deny","block")` as denied (`claude_code.py:321`, `codex_impl/hooks_adapter.py:174`, `droid.py:173`). The dominant block path emits `decision="block"`, not `"deny"`.
- **Failure mode:** The ACP adapter relies entirely on the CLI honoring a verbatim `decision:"block"` while sending `continue:True`; if the CLI only acts on `continue` or the documented `"allow"|"deny"` (the docstring at `:301-313` says Gemini expects those), the most common block decision is a no-op for standalone ghook Gemini/Qwen/Grok sessions. (AfterAgent's `continue:True` is intentional per the retry design.)
- **Minimal fix:** Compute `is_denied = decision in ("deny","block")` and map to the field the CLI actually enforces; reconcile the docstring.
- **Confidence:** med (block-loss conditional on the CLI's real decision-field semantics).

### [IMPORTANT] Codex read loop fully serialized behind each approval — head-of-line blocking, potential deadlock
- **Where:** `codex_impl/client.py:917` (`await self._handle_incoming_request(message)` inline in `_read_loop`; the handler awaits the full hook pipeline, which may block on rule eval/DB/human input).
- **Failure mode:** While an approval is evaluated, the single reader is parked: no responses to other outgoing requests, no notifications, no other approvals serviced. A second approval queues behind the first; if the handler awaits anything depending on a Codex stdout message, it deadlocks.
- **Minimal fix:** Dispatch incoming requests on a separate task so the read loop keeps draining.
- **Confidence:** high (serialization); med (deadlock depends on handler internals).

### [IMPORTANT] Codex process death leaves outgoing requests hanging until per-call timeout; no reconnect
- **Where:** `codex_impl/client.py:886-889` (read loop sets ERROR and breaks on process death but doesn't cancel `_pending_requests`; only `stop()` cancels them).
- **Failure mode:** After the app-server dies, every in-flight `_send_request` blocks for its full 60s timeout before raising; the client is permanently ERROR with no restart.
- **Minimal fix:** Fail all pending futures with `ConnectionError` on detected death; decide a reconnect policy.
- **Confidence:** high.

### [IMPORTANT] Codex `block` emits `"cancel"` for command/file/mcp approvals — wrong enum vs the chat backend's `decline`
- **Where:** `codex_impl/app_server_adapter.py:681-682` (`block` → `decision = "cancel"`); the documented approval enum is `accept|acceptForSession|decline|...` and the chat backend uses `"decline"` (`backends/codex.py:448-451`). `"cancel"` is the elicitation `action` value, not an approval response.
- **Failure mode:** If Codex rejects or mishandles the unknown decision string, a `block` could be lost (fail-open); concretely inconsistent between the two Codex approval handlers for the same Gobby decision.
- **Minimal fix:** Map `block` → `"decline"` for the command/file/mcp shapes; reserve `"cancel"` for elicitation.
- **Confidence:** med (inconsistency certain; fail-open consequence depends on Codex tolerance).

### [IMPORTANT] Codex `run_turn` notification handlers are not thread/turn-scoped — cross-delivery under concurrency
- **Where:** `codex_impl/client.py:611-680` (`on_event` registered for shared `turn/*`/`item/*` with no threadId filter; `turn_completed` set on *any* `turn/completed`).
- **Failure mode:** Two concurrent `run_turn` calls receive each other's events and either's completion ends both iterators early; a completion arriving during the drain/removal window is lost.
- **Minimal fix:** Filter `on_event` by threadId/turnId; set `turn_completed` only on a matching turn.
- **Confidence:** med (real bug; bounded by whether concurrent `run_turn` is exercised).

### [IMPORTANT] ACP prompt/request timeout leaves the client started with a polluted stdout buffer → next request mis-correlates
- **Where:** `acp_client.py:586-589` and `:425-428` (timeout raises with no drain/reset); consumers (`servers/websocket/chat/_streaming.py:155-166`, `backends/acp.py:182-200`) don't `stop()` on timeout. The protocol uses **no request-id correlation** — responses are matched by "next line with an `id`" under `_io_lock`.
- **Failure mode:** After a timeout, the late `session/prompt` response still arrives on stdout and is consumed as the response to a subsequent `initialize`/`session/new`/`session/prompt` — wrong result returned to a waiter. Timeouts are routine (default 120s).
- **Minimal fix:** On timeout, `stop()` or drain stdout to a clean boundary before reuse; validate the response `id` against the sent `id`.
- **Confidence:** high (no teardown/id-matching); med on late-frame frequency.

### [IMPORTANT] ACP setup-phase rejects legitimate client requests; `_read_stream` swallows CancelledError; `start()` leaks the subprocess on handshake failure
- **Where:** `acp_client.py:446-457` (during `initialize`/`session/new`/`session/load`, inbound `{id, method}` client requests get `-32601` instead of routing to `handle_client_request` like the prompt path); `:583-584` (`except asyncio.CancelledError: return` suppresses cancellation — a cancelled prompt looks like a clean completion); `:267-305` (post-spawn handshake has no try/except — a direct `start()` caller that doesn't wrap in try/stop leaks a running subprocess + `_started=True`; the managed backend is covered).
- **Failure mode:** A CLI issuing `session/request_permission`/`terminal/create` during session restore gets a hard error (the docstring notes this makes Node CLIs spiral with `[object Object]`); cancellation doesn't propagate; fd/process leak on the failure path for non-backend callers.
- **Minimal fix:** Route setup-phase client requests through `handle_client_request`; re-raise CancelledError; wrap the handshake in try/except that calls `stop()` and re-raises.
- **Confidence:** high (code); med on operational reach.

### [IMPORTANT] Gemini/Qwen `tool_call`/`tool_result` session updates are not normalized — events dropped, lifecycle never fires
- **Where:** `acp_client.py:642-736` (base `_normalize_notification` has no `tool_call` branch → emits raw `update`) vs `grok_acp_client.py:75-98` (Grok override maps `toolCallId`→`call_id`, `title`→`tool_name`, `rawInput`→`tool_input`). Consumer `acp_session.py:220-238` requires the structured keys.
- **Failure mode:** For Gemini/Qwen a `tool_call` update yields `event_type="tool_call"` but raw `data` → `_translate_event` finds no `tool_name` → returns None → the ToolCallEvent is dropped: no UI surfacing, and `_apply_pre_tool_lifecycle` never runs (so before_tool hooks don't even fire for two primary CLIs). Compounds the ACP enforcement Blocker. No test covers Gemini/Qwen tool_call normalization.
- **Minimal fix:** Lift Grok's mapping into the base normalizer; add fixture-backed tests.
- **Confidence:** high on the field mismatch; med on whether every Gemini version emits `tool_call` via `session/update`.

### [IMPORTANT] Claude PreToolUse `updatedInput` emitted without `permissionDecision:"allow"` is silently dropped
- **Where:** `claude_code.py:386-387` — `if response.modified_input is not None and permission_decision != "deny": hook_output["updatedInput"] = response.modified_input`. A `rewrite_input` effect that omits `auto_approve` (default False, `workflows/definitions.py:154`) yields `modified_input` set, `permission_decision=None` → `updatedInput` written with no `permissionDecision`, which Claude Code's `PermissionResultAllow(updated_input=...)` contract requires.
- **Failure mode:** A user-authored input-rewrite rule that forgets `auto_approve: true` silently no-ops; the agent runs the original command. The bundled `compress-bash-output.yaml` sets `auto_approve: true` so shipped config is safe, but the adapter doesn't enforce the coupling. No PreToolUse `updatedInput` round-trip test exists.
- **Minimal fix:** Force `permissionDecision = "allow"` when `modified_input` is set and not denied; add the round-trip test.
- **Confidence:** med (high on the code path; the live-CLI requirement is inferred from repo SDK docs).

### [IMPORTANT] Droid `handle_native` derives `hook_type` differently from `translate_to_hook_event` — flattened payloads lose the contract
- **Where:** `droid.py:50-58` (`translate_to_hook_event` falls back to top-level `hook_event_name`) vs `:226-228` (`handle_native` looks for `hook_event_name` only *under* `input_data`).
- **Failure mode:** For a flattened payload (top-level `hook_event_name`, no `input_data`), the inbound translation resolves the event but `handle_native` passes `hook_type=None` → `get_droid_contract(None)` is None → `decision_style=NONE` → a `block` on what should be PreToolUse/UserPromptSubmit collapses to a `systemMessage`-only response. Not exercised by current ingress (always populates `hook_type`), but the flattened branch exists deliberately.
- **Minimal fix:** Share one `_resolve_hook_type(native_event)` helper across both halves.
- **Confidence:** med (divergence certain; reachability gated by ingress normalization).

### [IMPORTANT] Plan-mode: static-menu sources send keystrokes with no live-pane state guard
- **Where:** `servers/websocket/handlers/plan_approval.py:295-317` (capture gated on `registry.requires_pane(source)`) + `plan_keystrokes.py:479-492` (only `claude` registers a pane-aware resolver; codex/droid/gemini/grok/qwen are static).
- **Failure mode:** For static sources, a stale web-UI Approve/Reject click injects the digit/Escape blind into whatever the pane currently shows (agent still generating, menu dismissed, different prompt). Claude is protected (non-matching pane → `PLAN_KEYSTROKES_UNMAPPED`); the five static sources are not. Severity is below Blocker only because static sequences are single keys with no trailing Enter, so a misfire usually lands a stray character rather than submitting the wrong thing.
- **Minimal fix:** Capture and require a menu-presence assertion before dispatch for all sources, mirroring the Claude guard.
- **Confidence:** high (the asymmetry is structural).

### [NIT] Adapter observability and contract-enforcement gaps
- **Where:** `capabilities.py:471-472` (unknown/unmapped hooks record zero dropped-field telemetry — the most likely place for silent loss emits nothing); `llm/sdk_utils.py:91-113` via `degradation.py:94-118` (additionalContext truncation is a naive tail-cut; `contributor_sizes` is logged but never used to reserve a budget for the safety-relevant `response.context`); `base.py:146` (`source: SessionSource` is a bare annotation, not `@abstractmethod` — a subclass that forgets it fails only at first access).

### [NIT] Codex/ACP small items
- **Where:** `codex_impl/client.py:456-522` (`start_turn` leaks `_pending_turn_prompts_by_thread` on send failure — bounded), `:316-319` (dead `asyncio.run` fallback in `_handle_notification`); `acp_client.py:42-48` (misleading comment ties the 16 MiB reader limit to stderr); `capabilities.py:396` (`DroidDecisionStyle.value == ProviderDecisionStyle.PRE_TOOL_USE` works only because both are StrEnum — fragile if either becomes a plain Enum, silently dropping droid PreToolUse permission fields).

### [NIT] Plan-approval entry-point drift
- **Where:** `plan_keystrokes.py:158-172` (Path B requires an explicit `option_id` on approve → `INVALID_PLAN_DECISION`) vs `plan_approval.py:419-423` (Path A tolerates a missing `option_id` with a generic approve). A client sending bare `{"decision":"approve"}` works against managed sessions but is rejected against attached terminal sessions. Fails loud, not wrong-action.

## Systemic patterns

1. **Enforcement is lost at the protocol bridge, not at the rule engine.** Every adapter Blocker is a block decision that the rule engine computed correctly but the bridge dropped: Codex BEFORE_AGENT fire-and-forget, ACP pre-tool return ignored, ACP terminal/create run-before-decision, Codex approval mis-correlated. The fully-native Claude path (where the CLI's own protocol carries the decision) is clean; the bridges that must *translate* a decision back into a CLI-native action are where it leaks.
2. **`decision == "block"` is handled inconsistently across adapters.** Claude/Codex/Droid/broadcaster all treat `("deny","block")` as denied; the ACP adapter alone special-cases only `"deny"`. Any new block-producing path silently differs on ACP.
3. **Two divergent handlers per integration.** Codex has the daemon WS `CodexAdapter` and the chat `backends/codex.py` with subtly different block mappings (`cancel` vs `decline`, BEFORE_AGENT honored only in chat); ACP has the pre-tool lifecycle and `session/request_permission` seams, neither acting on a block; Droid and the plan-approval flow each have two entry points with divergent fallback policies. Consolidating onto one translation path per integration removes a class of these bugs.
4. **Hand-rolled JSON-RPC framing/correlation under-specifies the duplex case.** Codex disambiguates request vs response by id-map membership (the id-collision Blocker); ACP uses no id correlation at all (the stale-buffer mis-correlation). Both treat a bidirectional JSON-RPC stream as a simple request/response queue.
5. **Subprocess stderr is piped but never drained** in both protocol-bridge clients — the same deadlock, twice.
6. **Two parallel hook_type derivations per adapter** (`translate_to_hook_event` vs `handle_native`) invite drift; Droid's already diverge.

## Verified non-bugs (cleared — don't re-chase)

- **Claude event mapping is complete and correct** — every native hook maps to the right `HookEventType` with the right decision style; no required hook silently falls to NOTIFICATION; PascalCase and kebab both resolve. STOP→turn_end / UserPromptSubmit→turn_start fidelity holds.
- **Claude block envelope shapes per decision style are correct** and single-channel (`DECISION_STYLES_ALLOWED_TO_CONTINUE_ON_DENY` membership re-verified); no Claude block is emitted in a shape the CLI ignores except the input-rewrite case filed above.
- **`PostToolUseFailure` → `is_failure` is robust** (set from the native hook name OR `is_error`, independent of the shared heuristic) — the failed-shell-evidence rescue path is intact.
- **Codex approval auto-approve sets fail closed** for non-safe tools on handler exceptions (only read-only discovery fails open); `_fail_closed_approval_response` returns the safest per-method shape; `model/list` pagination guards repeated cursors.
- **The `routes/mcp/hooks.py` choice of `CodexHooksAdapter` (not the WS adapter) for HTTP hooks is correct and commented** (different payload shapes).
- **ACP framing handles large single-line frames (16 MiB) and partial lines** (NDJSON via `readline()`); `send()`/`stop()` lifecycle release the lock and escalate terminate→kill correctly; Grok's tool mapping matches the fixture shape.
- **plan_keystrokes does NOT inherit the agents-review multi-line paste bug** — every menu selection is a single key (digit via non-bracketed paste with no trailing newline; navigation via raw `send-keys`), all via `create_subprocess_exec` (no shell, no trailing-`;` hazard). Capture failure fails safe for Claude. `plan_options.serialize()` withholds `post_plan_chat_mode` and re-resolves it server-side (no client spoof).
- **`%s` placeholders are correct** per repo convention; JSON-RPC `id`/`$param` styles are protocol-correct (not the stale SQL `$N` note).
