# Droid CLI Integration for Gobby 0.4.0

---

## SESSION RESUME STATE (post-compress handoff — 2026-04-22)

This plan was drafted in Gobby session `#3290` through an adversarial review loop that iterated 5 delegated rounds against plan-adversary (codex gpt-5.4, xhigh reasoning). Findings progression: R1=4 → R2=7 → R3=3 → R4=2 → R5=2. Budget exhausted at R5 with 2 blocking findings that have now been folded into this plan (see below). User directive: **fix R5, restart loop with fresh budget**.

### Pre-compress cleanup already applied (by the session that wrote this)

- **`#12625` (attempt 2) is already closed** as `obsolete` with `changes_summary` describing the R5-round-5 exhaustion and pointing at this plan. Do NOT re-close it.
- **`#12285` (parent epic) is escalated** with reason mentioning the paused adversarial loop and directing the resumer to de-escalate and create a new attempt. The `interactive:planning-in-progress:a691e28c-d7dc-4227-94b7-a8fe87057f3c` lock label is preserved on `#12285`. DO NOT remove the lock until the adversarial loop terminates (approved / escalated / user-aborted).
- **R5-F1 and R5-F2 are applied** in Phases 4.2 and 6.6 item #3 of this plan. No additional plan edits needed before spawning round 1 of the new attempt.

### What the post-compress agent needs to do, in order

1. **De-escalate `#12285`** back to `open`:
   ```
   gobby-tasks de_escalate_task
     task_id=#12285
     reason="Session resumed post-compress; restarting adversarial loop with fresh 5-round budget and R5 findings applied to plan."
     target_status="open"
   ```
   Then claim it: `gobby-tasks claim_task #12285` (you'll need the claim to edit `.gobby/plans/task-12285-plan.md` during delegated revision rounds).

2. **Create a fresh planning attempt epic** under `#12285`:
   ```
   gobby-tasks create_task
     parent_task_id=#12285
     task_type=epic
     category=planning
     title="Interactive plan for #12285 (droid CLI integration) — attempt 3"
     labels=["interactive:planning", "planning-round:0"]
     description="Third adversarial attempt. Plan artifact (unchanged from attempt 2): .gobby/plans/task-12285-plan.md. R5 findings applied in place. Fully delegated — no per-round user wake until terminal state."
   ```
   Capture the new epic's `#N` — that's your new `planning_task_id`.

3. **Set / restore session variables** (some may already be set from pre-compress; overwrite to be safe):
   - `plan_review_mode = "delegated"`
   - `plan_parent_ref = "#12285"`
   - `planning_task_id = "#<new-epic-N>"` (from step 2)
   - `artifact_path = ".gobby/plans/task-12285-plan.md"`
   - `max_rounds = 5`
   - `current_round = 0`
   - `interactive_lock_label = "interactive:planning-in-progress:a691e28c-d7dc-4227-94b7-a8fe87057f3c"` (should still exist on #12285; re-add if missing)

4. **Verify parent lock** — `#12285` should still carry `interactive:planning-in-progress:a691e28c-d7dc-4227-94b7-a8fe87057f3c`. If absent, re-add. `#12285` is claimed by this session (`a691e28c-d7dc-4227-94b7-a8fe87057f3c`); DO NOT release the claim until the adversarial loop terminates.

5. **Spawn plan-adversary round 1** of the new attempt (delegated, no user wake):
   ```
   gobby-agents spawn_agent
     agent="plan-adversary"
     task_id="#<new-epic-N>"
     parent_session_id="#3290"
     prompt="Plan artifact: .gobby/plans/task-12285-plan.md
   Parent task: #12285
   Display round: 1

   Third attempt after prior budget exhaustion. All R1-R5 findings from attempt 2 (4+7+3+2+2=18 blocking findings total) have been folded into the plan in place. Apply plan-review heuristics exactly. Delegated mode — call mark_task_review_rejected or mark_task_review_approved (or escalate_task for needs_requirements)."
   ```
   Persist returned `run_id` to session var `adversary_run_id`.

6. **Block on completion** via `gobby-workflows wait_for_completion(completion_id=<run_id>, timeout=1500)`. This is the correct tool — it lives on the `gobby-workflows` server, NOT `gobby-agents`. Timeout 1500s (25 min) is sufficient; prior rounds took 7-14 min.

7. **Interpret result** per /gobby plan skill Step 7.6:
   - Task status `review_approved` → proceed to **Step 8 below**.
   - Task status `open` with `## Adversary Findings — Round N` appended → read latest round's findings, revise the plan in place (`artifact_path` is the ONLY file to edit during delegated revision), bump `current_round`, loop back to step 5 with incremented display round. Continue until approved, escalated, or `current_round + 1 >= max_rounds` (new budget = 5 rounds for this attempt).
   - Task status `escalated` with `escalation_reason` starting `needs_requirements:` → terminal for delegated mode; surface to user and stop.
   - Any other terminal state → treat as adversary crash; surface and stop.

8. **On approval — run expand-task pipeline** per /gobby plan skill Step 8:
   ```
   gobby-workflows run_pipeline
     name="expand-task"
     inputs={"task_id": "#12285", "plan_file": ".gobby/plans/task-12285-plan.md"}
   ```
   Wait via `wait_for_completion(execution_id)`. On success: `close_task(planning_task_id, reason="Interactive planning complete; expansion launched")`, run terminal cleanup (remove lock label from `#12285`, clear all session vars). On failure: surface error, offer user retry / escalate.

9. **Budget exhaustion (new 5-round run)** → /gobby plan skill Step 9 terminal-interrupt flow. Surface remaining findings, offer bypass / abort / restart.

### What was changed in the plan to address R5

- **Phase 4.2** rewritten. No longer just a registry registration. Now covers all four parser-selection call sites: `sessions/transcripts/__init__.py`, `sessions/lifecycle.py:456-466`, `sessions/transcript_reader.py:290-304`, `sessions/summarize.py:317-333`. Explicit droid branches at each, with verification that all three integration paths (backfill, transcript-reader, summary) route droid sessions through `DroidTranscriptParser`. Includes an out-of-scope note about a follow-up refactor to consolidate the dispatch.
- **Phase 6.7 item #3** (`provider_models.py`) rewritten. No longer punts on droid model discovery. Concrete contract: add a new `_discover_droid_models` async method returning a hardcoded list matching the complete `Available Models` block printed by `droid exec --help` on v0.106.0 (claude-opus-4-5-20251101, claude-opus-4-6, claude-opus-4-6-fast, claude-opus-4-7, claude-sonnet-4-5-20250929, claude-sonnet-4-6, claude-haiku-4-5-20251001, gpt-5.2, gpt-5.2-codex, gpt-5.4, gpt-5.4-fast, gpt-5.4-mini, gpt-5.3-codex, gpt-5.3-codex-fast, minimax-m2.5, minimax-m2.7, gpt-5.1-codex-max), with a docstring pointer to `docs.factory.ai/cli` for refresh guidance and an inline note explaining why droid uses a static list while other providers discover dynamically.

### What was changed in the plan to address Round 1 (attempt 3)

Round 1 returned 4 blocking findings (F1-F4) against the post-R5 plan; all have been folded in place before round 2 spawns:

- **F1 (missing-requirement, web-chat surface)** — Phase 6.7's "complete list" claim was scoped too broadly. Fix: keep §6.7 for session-source/spawn-provider allowlists; add a new §6.8 that builds a real `DroidWebChatBackend` + `DroidManagedChatSession` mirroring the Codex/Gemini/Qwen pattern and wires droid into every web-chat allowlist (`runtime_manager.py`, `_PROVIDER_DEFS`, `_messaging.py`, `CHAT_PROVIDERS`, `PROVIDER_LABELS`, `PROVIDER_SORT_ORDER`, `_session.py:44`, `session_config.py`, `plan_approval.py` recovery loop). Droid ships with full web-chat parity in 0.4.0 per the plan overview's "no v1/v1.1 split" directive.
- **F2 (traceability, model catalog)** — resume-state R5 bullet corrected: the canonical droid model ID is `claude-opus-4-7` (no `-via-factory` suffix — that suffix is a label string, not the ID). §6.7.3.c expanded from a 4-model cherry-pick to the full `droid exec --help` Available Models list so it matches §5.1 acceptance and the empirical CLI output exactly.
- **F3 (gobby-format, §5.3)** — `### 5.3 Drop nominal AGENTS.md template — out of scope [category: refactor]` removed as a numbered section; its rationale is now a prose note under the Phase 5 preamble. Former §5.4 renumbered to §5.3 (dependency graph unaffected — §5.4 depended on §3.2, not §5.3).
- **F4 (gobby-format, §6.1)** — §6.1 upstream ghook PR recategorized from `[category: code]` to `[category: manual]` because its targets live in `github.com/GobbyAI/gobby-cli` (outside this repo); acceptance criteria updated to reflect manual PR/release verification. The in-repo ghook-version gate originally embedded inside §6.1 was split out in Round 2 into its own §6.2 (see Round 2 notes below).

### What was changed in the plan to address Round 2 (attempt 3)

Round 2 returned 4 blocking findings (F1-F4) against the post-Round-1 plan; all have been folded in place before round 3 spawns:

- **F1 (unhandled-edge, Phase 4 sidecar-token contract)** — §4.1 previously keyed the sidecar lookup off `self.session_id`, but the live call sites in §4.2 pass Gobby's internal session id, not droid's native UUID (which is the JSONL/sidecar filename stem). Fix: `DroidTranscriptParser.__init__` now accepts a required-by-convention `transcript_path: Path | str | None = None` kwarg; the sidecar is derived as `transcript_path.with_suffix(".settings.json")`. The `_maybe_load_sidecar_from_session_id` filesystem-scan fallback is removed. §4.2 rewritten so every live call site (`lifecycle.py`, `transcript_reader.py::_get_parser` extended signature, `summarize.py`, `processor.py` if applicable) threads `transcript_path=session.transcript_path` into the droid parser constructor. New acceptance tests prove the contract when Gobby internal id ≠ droid native UUID.
- **F2 (gobby-format, §6.8 monolith)** — `src/gobby/servers/websocket/chat/provider_backends.py` is already 1,420 lines (task #12096 tracks splitting it). The Round 1 plan extended it further with droid classes, violating the "under 1,000 lines" rule. Fix: droid web-chat classes now live in a new dedicated module `src/gobby/servers/websocket/chat/droid_backend.py`; `runtime_manager.py` imports from there. This also sets the per-provider-module precedent that #12096 can later align the other providers to, without coupling the two efforts.
- **F3 (traceability, §6.1 split)** — §6.1's manual-category task previously smuggled an in-repo code deliverable (`install_droid` ghook-version gate) into its acceptance criteria, where expansion could lose it. Fix: §6.1 stripped to pure upstream-PR scope; new §6.2 "Add ghook-version gate to install_droid [category: code] (depends: 3.2)" covers the in-repo deliverable with concrete target files (`src/gobby/cli/installers/droid.py`, `tests/cli/installers/test_droid.py`) and four unit-test cases. All subsequent Phase 6 sections renumbered by +1 (old 6.2→6.3, 6.3→6.4, ..., old 6.7→6.8); cross-references updated throughout the plan.
- **F4 (traceability, §1.2 ↔ §6.8 normalization interface)** — §1.2 explicitly forbids a `canonicalize_mcp_tool_name` helper (triple-underscore logic stays inline in `normalize_mcp_fields`), but Round 1's §6.8 vaguely referenced "the canonical-tool-name function." Fix: §6.8 now includes the concrete 5-line `_droid_tool_name_adapter` wrapper that calls `normalize_mcp_fields({"tool_name": raw})` and returns the rewritten name. No new helper module, no duplication of §1.2's logic, and a dedicated unit test covers the wrapper contract.

### What was changed in the plan to address Round 3 (attempt 3)

Round 3 hit a known-class workflow bug — the plan-adversary's `mark_task_review_rejected` calls at 04:17:15 and 04:18:16 were both blocked by `step-enforcement:plan-adversary-steps/terminate` because the workflow had already auto-transitioned `review → terminate` at 04:16:43 despite no verdict tool having been called successfully. Bug filed as **#12650** (regression of #12617 class). Findings were extracted from the codex rollout (`~/.codex/sessions/2026/04/22/rollout-2026-04-22T23-01-32-019db880-0a3d-7cf0-93ac-7c877be9f297.jsonl`) and applied in place; the task description will not reflect the Round 3 findings history because the verdict tool never persisted.

Round 3 produced 5 blocking findings (F1-F5); all folded into the plan:

- **F1 (traceability, §6.7.3 model catalog)** — §6.7.3.c's hardcoded list had 17 IDs and wrong reasoning metadata. Actual `droid exec --help` v0.106.0 reports 24 IDs (missing: `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `glm-5.1`, `glm-5`, `kimi-k2.6`, `kimi-k2.5`, `glm-4.7` deprecated) and per-model reasoning differences (e.g. `claude-opus-4-7` supports `xhigh`, `gemini-3-flash-preview` supports `minimal`, GLM variants have no reasoning). Fix: §6.7.3.c rewritten to include all 24 models verbatim with correct `reasoning_levels` + `reasoning_default` per the `Model details` section. Acceptance ID set expanded to 24 entries; new drift-detection integration test references a committed baseline fixture at `tests/fixtures/droid/droid_exec_help_v0.106.0.txt`.
- **F2 (unhandled-edge, §4.2 registry path)** — `src/gobby/sessions/transcripts/__init__.py` has a `PARSER_REGISTRY`-backed `get_parser(source, session_id)` helper consumed by `SessionMessageProcessor.register_session`; Round 2's §4.2 only said to re-export `DroidTranscriptParser` from this file and did not update `PARSER_REGISTRY` or `get_parser`'s signature. Fix: §4.2 item #1 rewritten to add `droid` to `PARSER_REGISTRY`, extend `get_parser` to accept `transcript_path` and pass it through only for droid (other parser constructors would error on the kwarg), and update `SessionMessageProcessor.register_session` to thread `transcript_path` in its `get_parser` call.
- **F3 (bad-sequencing, §6.8 backend interface)** — Round 2's §6.8 specified `DroidWebChatBackend` methods as `attach_session(conversation_id, ...)` / `detach_session(conversation_id)` / `send_message(conversation_id, text, ...)` / `switch_model(conversation_id, new_model)`, but the actual `ManagedChatSessionBase` contract is session-object-first (`backend.attach_session(self, ...)` etc.). An agent implementing §6.8 literally would produce a backend incompatible with `runtime_manager.py`. Fix: §6.8's `DroidWebChatBackend` method section rewritten to take `session: DroidManagedChatSession` as the first positional arg everywhere, mirroring `GeminiWebChatBackend` at `provider_backends.py:841-908`. Internal keying by `session.conversation_id` stays as an implementation detail. New interface-compatibility regression test added.
- **F4 (gobby-format, §6.8 self-containment)** — Round 2's §6.8 referenced "droid's stream-json stdout/stdin protocol" abstractly without concrete event shapes; an agent that receives only §6.8 cannot deterministically implement the event translator. Fix: added a new item #0 at the top of §6.8 specifying a stream-json fixture-capture procedure (8 named fixtures under `tests/fixtures/droid/stream_json/`: `session_init`, `text_response`, `tool_call`, `permission_request`, `thinking`, `error`, `malformed`, `eof`) plus a 12-row event-translation table mapping each droid event type to a Gobby `StreamEvent`. The implementing agent fills in exact field names by reading the captured fixtures; the mapping contract is fixed in the table.
- **F5 (missing-requirement, §6.6 source-theme surfaces)** — §6.6's target list missed `web/src/components/shared/sourceTheme.ts` (the canonical `SOURCE_COLORS`/`SOURCE_LABELS`/`PROVIDER_COLORS` maps consumed by Dashboard, Resume Session modal, task SessionViewer, and SourceIcon), `web/src/components/shared/sourceIconUtils.ts` (`SourceType` union), and `web/src/components/tasks/SessionViewer.tsx` (local duplicate `SOURCE_LABELS`). If §6.6 were implemented as originally written, droid would render as a raw/default source on Dashboard / Resume Session / SessionViewer. Fix: §6.6 expanded with items #6-#9 covering the three missed files and a grep-based regression check; acceptance criteria now require single-source-of-truth color consistency between `sourceTheme.ts` and `SessionsPage.css`, droid entries in all three files, and Dashboard/Resume-Session/SessionViewer smoke screenshots. Test updates renumbered to item #10; `runner_broadcasting.py` check moved to #11.

### What was changed in the plan to address Round 4 (attempt 3)

Round 4 returned 4 blocking findings; verdict persisted correctly this time (no regression of the #12650 workflow bug). All findings applied in place:

- **F1 (traceability, §§6.7-6.8 model count)** — Round 3's 24-model fix to §6.7.3 was only partially propagated. Stale "17 IDs" / "17-model list" / "17 models" references survived in §6.7 acceptance (line 1787), §6.8 `_filter_models_for_web_chat` description (line 1887), §6.8 API integration test (line 1904), and §6.8 acceptance (line 1909). Fix: grep-replaced every remaining `17` reference with `24`, aligned §6.7 + §6.8 on the same 24-ID catalog from §6.7.3.d.
- **F2 (bad-sequencing, §6.8 `DroidManagedChatSession.switch_model`)** — the session-side `switch_model(model: str)` bullet still called `DroidWebChatBackend.switch_model(conversation_id, model)` even though Round 2's backend rewrite changed the backend signature to `switch_model(self, session, new_model)`. Fix: the session-side bullet now calls `await self._backend.switch_model(self, model)`, passing the session object itself — matches `ManagedChatSessionBase` contract and the backend signature documented in the same section. Internal `conversation_id` map key inside the backend stays as an implementation detail.
- **F3 (weak-testability, §6.8 API contract)** — §6.8 told frontend and integration tests to use `GET /api/providers/models?provider=droid`, but the real route at `src/gobby/servers/routes/providers.py:209` is a grouped `/api/providers/models` with no `provider` query parameter — it returns `{"providers": [...]}`. Fix: §6.8 rewritten to target the existing grouped route; frontend filters via `web/src/lib/providerModels.ts::getModelsForProvider("droid")` against the grouped response (matching how claude/codex/gemini/qwen are consumed today). No new backend query parameter is added.
- **F4 (gobby-format, §6.8 stream-json event contract)** — Round 3's event-translation table used invented Gobby-side class names (`TextDelta`, `ThinkingDelta`, `ToolCall`, `ApprovalRequest`, `TurnComplete`, `SessionIdentified`, `ErrorEvent`) that don't exist in the live code. Actual types: `gobby.adapters.gemini_acp_client.StreamEvent(event_type: str, data: dict)` for backend streams (event_type ∈ `{"init", "content_delta", "result", "error"}`), converted in `_translate_event` to `gobby.llm.claude_models` ChatEvents (`TextChunk`, `ToolCallEvent`, `ToolResultEvent`, `DoneEvent`, `ThinkingEvent`). Fix: §6.8 replaced with a **two-stage** translation table — Stage 1 droid-line → `StreamEvent`, Stage 2 `StreamEvent` → `ChatEvent`. All class names reference the actual live types; mirror `GeminiManagedChatSession._translate_event` at `provider_backends.py:479-541`. Fixture path standardized on `tests/fixtures/droid/stream_json/<name>.jsonl` (directory layout) everywhere — the stale glob-style `stream_json_*.jsonl` reference in item #8 tests was corrected.

### Key references (if compression strips them)

- User preference: every CLI adapter standalone — no cross-CLI inheritance (memory ID in gobby-memory, search tag `adapters`/`architecture`).
- Plan-adversary deadlock recovery: bug was filed as task `#12617`, landed pre-R2. If future adversary runs fail to persist verdicts, check the Codex rollout at `~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl` for `mark_task_review_*` tool calls.
- Follow-up task `#12615` tracks removing vestigial `gobby agents spawn` CLI — NOT in scope for this droid integration.
- `.gobby/plans/task-12285-plan.md` is the canonical artifact path; `/Users/josh/.claude/plans/cosmic-noodling-swan.md` is a stale scratch copy from before the formal attempt — ignore.

---

## Overview

Integrate Factory AI's `droid` CLI as a first-class adapter in Gobby's 0.4.0 "go loud" release alongside Claude Code, Gemini, Codex, and Qwen. Full-surface implementation covering hook adapter, contract table, installer, transcript parser, upstream ghook router update, and agent-spawning runtime. No v1/v1.1 split — the public announcement carries every capability Gobby offers for the existing CLIs.

Empirical findings from droid v0.106.0 informed the design: hooks probe at `~/.factory/hooks/hooks.json` (not `settings.json.hooks` as docs claim); transcript is JSONL with Anthropic-SDK-native content blocks (`text`, `thinking`, `tool_use`, `tool_result`); MCP tools namespace with triple-underscore (`gobby___list_mcp_servers`); token usage lives in a sidecar `<uuid>.settings.json` beside the JSONL.

## Constraints

- **Every CLI adapter stands alone** — `DroidAdapter` extends `BaseAdapter` directly, never `ClaudeCodeAdapter`. Same rule for contract enums, installer helpers, and the transcript parser. Coding CLIs diverge upstream; inheritance across CLI adapters turns every upstream change into a coordinated-update problem.
- **Reuse shared CLI-agnostic helpers** in `src/gobby/adapters/base.py` (`build_first_hook_session_metadata_lines`, `normalize_adapter_response_reason`, `system_message_has_session_banner`) and `src/gobby/hooks/normalization.py`. Divergence lives in the adapter layer, not in the base/shared layer.
- **Hook config path is `~/.factory/hooks/hooks.json`** (empirically confirmed via `~/.factory/logs/droid-log-single.log` probing that path at startup). `GOBBY_DROID_HOOKS_FILE` env override exists as an escape hatch. Do NOT write both the dedicated file and `settings.json.hooks` — double-write risks double-dispatch.
- **MCP registration via file write**, not `droid mcp add`. The CLI's commander.js parser drops nested `--flags` even with the `--` separator and writes `disabled:true, args:[]` entries. Use `configure_mcp_server_json` / `remove_mcp_server_json` round-trip helpers matching the Claude installer pattern.
- **Vestigial `gobby agents spawn` CLI command is not extended** with droid support. Task #12615 removes it separately. Droid wires into the `spawn_agent` MCP tool path (`src/gobby/mcp_proxy/tools/spawn_agent/`) which is the canonical programmatic surface.
- **No shell-script ghook fallback.** The ghook Rust binary at `github.com/GobbyAI/gobby-cli` gets a proper `--cli=droid` route as part of this release.
- **Subagent and cloud-sync coexistence** — leave droid's Custom Droids (`~/.factory/droids/*.md`) and cloud-session-sync feature untouched. Gobby-spawned droid sessions behave like any other droid session, not like Custom Droids.
- **DO NOT RUN THE FULL PYTEST SUITE.** Target-path pytest only.

## Phase 1: Session source, normalization, and contract foundations

**Goal**: Land the droid-specific type scaffolding and shared-layer extensions that every downstream phase depends on.

### 1.1 Add SessionSource.DROID enum value [category: code]

Target: `src/gobby/hooks/events.py` (around line 74–82 where `SessionSource` is defined)

Add `DROID = "droid"` to the `SessionSource` StrEnum. Without this enum value, every downstream source-switch — including `src/gobby/sessions/processor.py:454` and any web-chat / broadcasting / telemetry path that branches on `source` — silently mis-routes droid events to the fallback (`PIPELINE` or `UNKNOWN`). Update the class/module docstring where the event-name mapping is documented to include droid's PascalCase hook names alongside Claude's kebab-case and Codex's JSON-RPC method names.

Acceptance criteria:
- `SessionSource.DROID` resolves to `"droid"`.
- `SessionSource("droid")` round-trips.
- `mypy` passes.
- Any existing `match`/`if/elif` dispatcher in `src/gobby/sessions/processor.py` and `src/gobby/runner_broadcasting.py` that exhaustively enumerates `SessionSource` members gains a droid branch (or a defaulting fall-through documented inline).

### 1.2 Extend MCP tool-name normalization for droid triple-underscore [category: code]

Target: `src/gobby/hooks/normalization.py` inside `normalize_mcp_fields` (function at line ~564, immediately BEFORE the existing `mcp__` prefix parse at line ~604; the existing Gemini single-underscore branch is at lines ~594-602 — add droid handling INLINE in the same style, not as a new standalone helper).

Droid serializes MCP tools on the wire as `<server>___<tool>` (e.g. `gobby___list_mcp_servers`) — triple underscore separator, no `mcp__` prefix. Claude Code uses `mcp__<server>__<tool>`. The rule engine, observers, and web-chat path all operate on the Claude-shaped canonical form. Add a normalization branch that detects the droid shape and rewrites to the canonical form before downstream consumers see it.

**There is NO existing `canonicalize_mcp_tool_name` helper** — the Gemini logic is inline inside `normalize_mcp_fields`. Mirror that pattern. Do not introduce a new module-level helper.

Implementation — add inline after the Gemini single-underscore block, before the `mcp__` prefix parse:

```python
# Normalize triple-underscore droid MCP prefix (Factory droid) to canonical
# double-underscore form.  Droid sends <server>___<tool>; canonical is
# mcp__<server>__<tool>.  Server names never contain underscores (the CLI
# rejects names with `_`), so the first triple-underscore after a bare
# identifier delimits server/tool unambiguously.
if not tool_name.startswith("mcp__") and "___" in tool_name:
    server, _, tool = tool_name.partition("___")
    if server and tool and "_" not in server:
        canonical = f"mcp__{server}__{tool}"
        data["tool_name"] = canonical
        tool_name = canonical
```

Place immediately after the Gemini block so both normalizations run before the `mcp__` prefix parse picks up the canonical form.

Acceptance criteria:
- `gobby___list_mcp_servers` → `mcp__gobby__list_mcp_servers`.
- Existing `mcp__gobby__list_mcp_servers` passes through unchanged.
- Gemini single-underscore `mcp_gobby_list_mcp_servers` still canonicalizes correctly (regression guard).
- `Read`, `Execute`, and other native PascalCase droid tool names pass through unchanged.
- `mcp_server` / `mcp_tool` fields populated correctly downstream in rule engine, observers, and web-chat paths.
- Unit test in `tests/hooks/test_normalization.py` covering the triple-underscore case + idempotency + regression.

### 1.3 Add DroidHookContract table and supporting types [category: code]

Target: `src/gobby/adapters/droid_contract.py` (new file)

Mirror the shape of `src/gobby/adapters/claude_contract.py`. This is the real abstraction layer — not the raw `EVENT_MAP` — and drives the adapter's `translate_from_hook_response` dispatch.

Required exports:

```python
from dataclasses import dataclass
from enum import Enum

from gobby.hooks.events import HookEventType


class DroidDecisionStyle(Enum):
    """How a droid hook event returns a blocking decision.

    Standalone enum — do not import ClaudeDecisionStyle. The 'every adapter
    stands alone' rule extends to its supporting types because droid and
    Claude Code can diverge upstream.
    """
    TOP_LEVEL_BLOCK = "top_level_block"        # decision:"block" + reason
    PRE_TOOL_USE = "pre_tool_use"              # hookSpecificOutput.permissionDecision
    NONE = "none"                              # no blocking; continue always


@dataclass(frozen=True)
class DroidHookContract:
    """Per-event metadata driving translate_from_hook_response."""
    hook_event_name: str          # Canonical PascalCase name for hookSpecificOutput.hookEventName
    event_type: HookEventType     # Unified HookEventType the adapter maps to
    decision_style: DroidDecisionStyle
    allows_additional_context: bool


DROID_PASCAL_HOOK_NAMES: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
)


DROID_HOOK_CONTRACTS: dict[str, DroidHookContract] = {
    "PreToolUse": DroidHookContract(
        hook_event_name="PreToolUse",
        event_type=HookEventType.BEFORE_TOOL,
        decision_style=DroidDecisionStyle.PRE_TOOL_USE,
        allows_additional_context=False,
    ),
    "PostToolUse": DroidHookContract(
        hook_event_name="PostToolUse",
        event_type=HookEventType.AFTER_TOOL,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    "UserPromptSubmit": DroidHookContract(
        hook_event_name="UserPromptSubmit",
        # HookEventType has no USER_PROMPT_SUBMIT member; BEFORE_AGENT is the
        # canonical unified event for user-prompt-submission hooks, matching
        # how Claude Code's UserPromptSubmit is handled (see CLAUDE_EVENT_MAP
        # in src/gobby/adapters/claude_contract.py and the HookEventType
        # definition at src/gobby/hooks/events.py:19-71).
        event_type=HookEventType.BEFORE_AGENT,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=True,
    ),
    "Notification": DroidHookContract(
        hook_event_name="Notification",
        event_type=HookEventType.NOTIFICATION,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=False,
    ),
    "Stop": DroidHookContract(
        hook_event_name="Stop",
        event_type=HookEventType.STOP,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=False,
    ),
    "SubagentStop": DroidHookContract(
        hook_event_name="SubagentStop",
        event_type=HookEventType.SUBAGENT_STOP,
        decision_style=DroidDecisionStyle.TOP_LEVEL_BLOCK,
        allows_additional_context=False,
    ),
    "PreCompact": DroidHookContract(
        hook_event_name="PreCompact",
        event_type=HookEventType.PRE_COMPACT,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=False,
    ),
    "SessionStart": DroidHookContract(
        hook_event_name="SessionStart",
        event_type=HookEventType.SESSION_START,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=True,
    ),
    "SessionEnd": DroidHookContract(
        hook_event_name="SessionEnd",
        event_type=HookEventType.SESSION_END,
        decision_style=DroidDecisionStyle.NONE,
        allows_additional_context=False,
    ),
}


DROID_EVENT_MAP: dict[str, HookEventType] = {
    name: contract.event_type for name, contract in DROID_HOOK_CONTRACTS.items()
}


DROID_HOOK_EVENT_NAME_MAP: dict[str, str] = {
    name: contract.hook_event_name for name, contract in DROID_HOOK_CONTRACTS.items()
}


def get_droid_contract(hook_type: str | None) -> DroidHookContract | None:
    """Resolve a PascalCase droid hook name to its contract."""
    if not hook_type:
        return None
    return DROID_HOOK_CONTRACTS.get(hook_type)
```

Acceptance criteria:
- `len(DROID_PASCAL_HOOK_NAMES) == len(DROID_HOOK_CONTRACTS) == 9`.
- Every entry in `DROID_PASCAL_HOOK_NAMES` appears as a key in `DROID_HOOK_CONTRACTS`.
- `DroidDecisionStyle` is a standalone enum defined in this file, not imported from `claude_contract`.
- `DROID_EVENT_MAP` and `DROID_HOOK_EVENT_NAME_MAP` are derived from the contract table, not hand-maintained.
- `get_droid_contract("PreToolUse").decision_style == DroidDecisionStyle.PRE_TOOL_USE`.
- Every `HookEventType` referenced by `DROID_HOOK_CONTRACTS` exists in `src/gobby/hooks/events.py:19-71`. Explicit map: PreToolUse/PostToolUse → BEFORE_TOOL/AFTER_TOOL; UserPromptSubmit → BEFORE_AGENT (NOT a USER_PROMPT_SUBMIT value — that member does not exist); Notification → NOTIFICATION; Stop → STOP; SubagentStop → SUBAGENT_STOP; PreCompact → PRE_COMPACT; SessionStart → SESSION_START; SessionEnd → SESSION_END. Unit test asserts each mapping resolves without AttributeError.

## Phase 2: Droid adapter

**Goal**: Translate native droid hook payloads into the unified `HookEvent` / `HookResponse` model and expose the adapter to ghook via a daemon HTTP route.

### 2.1 Implement DroidAdapter [category: code] (depends: 1.1, 1.2, 1.3)

Target: `src/gobby/adapters/droid.py` (new file)

Standalone adapter extending `BaseAdapter` directly. Mirrors the shape of `src/gobby/adapters/claude_code.py` but owns its own implementation.

Structure:

```python
"""Droid CLI adapter for hook translation.

Translates between Factory AI droid's hook payload format and Gobby's unified
HookEvent/HookResponse models. Standalone — does not inherit from any other
CLI adapter.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.droid_contract import (
    DROID_EVENT_MAP,
    DROID_HOOK_EVENT_NAME_MAP,
    DroidDecisionStyle,
    get_droid_contract,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import compress_and_truncate

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)


class DroidAdapter(BaseAdapter):
    """Adapter for Factory droid CLI hook translation.

    Droid's hook payload is delivered on stdin as JSON:
        {
            "session_id": "<uuid>",
            "transcript_path": "<absolute path>",
            "cwd": "<absolute path>",
            "permission_mode": "off|spec|auto-low|auto-medium|auto-high",
            "hook_event_name": "PreToolUse|PostToolUse|...",
            # ...event-specific fields like tool_name, tool_input, tool_response,
            # prompt, message, source, reason, trigger, custom_instructions
        }

    Responses are JSON on stdout with optional `hookSpecificOutput` containing
    `permissionDecision`, `additionalContext`, `updatedInput`, or top-level
    `decision:"block"` per the contract table.
    """

    source = SessionSource.DROID

    EVENT_MAP: dict[str, HookEventType] = dict(DROID_EVENT_MAP)
    HOOK_EVENT_NAME_MAP: dict[str, str] = dict(DROID_HOOK_EVENT_NAME_MAP)

    def __init__(self, hook_manager: "HookManager | None" = None) -> None:
        self._hook_manager = hook_manager

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert droid native payload → unified HookEvent.

        The HTTP route at src/gobby/servers/routes/mcp/hooks.py hands the adapter
        an OUTER envelope of shape {"hook_type": "<PascalCase>", "input_data":
        <inner droid stdin JSON>, "source": "droid"}. The droid-native payload
        (session_id, transcript_path, cwd, permission_mode, hook_event_name,
        event-specific fields) lives under "input_data".

        Mirror GeminiAdapter.translate_to_hook_event at src/gobby/adapters/gemini.py:244
        for the envelope unwrap + direct-invocation fallback.
        """
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data", {}) or {}

        # Direct-invocation fallback (unit tests, ghook payloads that skip
        # the dispatcher envelope): if input_data is empty but the raw payload
        # looks like the inner droid stdin schema, treat native_event AS
        # input_data and recover hook_type from hook_event_name.
        if not input_data and "hook_event_name" in native_event:
            input_data = native_event
            hook_type = hook_type or native_event.get("hook_event_name", "")

        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)
        session_id = input_data.get("session_id", "")
        normalized_data = self._normalize_event_data(input_data)

        is_failure = normalized_data.get("is_error", False)
        metadata = {"is_failure": is_failure} if is_failure else {}

        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=datetime.now(UTC),
            machine_id=None,  # droid does not emit machine_id
            cwd=input_data.get("cwd"),
            data=normalized_data,
            metadata=metadata,
        )

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        from gobby.hooks.normalization import normalize_tool_fields
        return normalize_tool_fields(dict(input_data))

    def _build_additional_context(
        self, response: HookResponse, *, hook_type: str | None
    ) -> str | None:
        contract = get_droid_contract(hook_type)
        if not contract or not contract.allows_additional_context:
            return None

        parts: list[str] = []
        session_start = contract.hook_event_name == "SessionStart"

        if response.system_message and session_start:
            parts.append(response.system_message)

        if response.context:
            parts.append(response.context)

        if response.metadata:
            lines = build_first_hook_session_metadata_lines(
                response.metadata,
                include_session_id_line=not (
                    session_start
                    and system_message_has_session_banner(response.system_message)
                ),
            )
            if lines:
                parts.append("\n".join(lines))

        if not parts:
            return None
        return compress_and_truncate("\n\n".join(parts))[0]

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert unified HookResponse → droid expected format.

        Output shape:
        {
            "continue": true|false,
            "stopReason": "<reason when continue=false>",
            "systemMessage": "<warning shown to user>",
            "decision": "block",             # for TOP_LEVEL_BLOCK style on deny
            "reason": "<reason for block>",
            "hookSpecificOutput": {
                "hookEventName": "<PascalCase name>",
                "permissionDecision": "allow|deny|ask",   # for PRE_TOOL_USE
                "permissionDecisionReason": "<reason>",
                "updatedInput": { ... },                  # for PRE_TOOL_USE
                "additionalContext": "<injected context>",
            }
        }
        """
        contract = get_droid_contract(hook_type)
        hook_event_name = contract.hook_event_name if contract else "Unknown"
        additional_context = self._build_additional_context(response, hook_type=hook_type)

        result: dict[str, Any] = {"continue": True}
        if response.system_message and hook_event_name != "SessionStart":
            result["systemMessage"] = response.system_message

        def ensure_hook_output() -> dict[str, Any]:
            return cast(dict[str, Any], result.setdefault(
                "hookSpecificOutput", {"hookEventName": hook_event_name}
            ))

        if additional_context:
            ensure_hook_output()["additionalContext"] = additional_context

        is_denied = response.decision in ("deny", "block")
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=logger,
        )
        decision_style = contract.decision_style if contract else DroidDecisionStyle.NONE

        if decision_style == DroidDecisionStyle.TOP_LEVEL_BLOCK and is_denied:
            result["decision"] = "block"
            if normalized_reason:
                result["reason"] = normalized_reason
        elif decision_style == DroidDecisionStyle.PRE_TOOL_USE:
            permission_decision: str | None = response.permission_decision
            if not permission_decision:
                if response.auto_approve:
                    permission_decision = "allow"
                elif response.decision == "ask":
                    permission_decision = "ask"
                elif is_denied:
                    permission_decision = "deny"

            if permission_decision or response.modified_input is not None or normalized_reason:
                hook_output = ensure_hook_output()
                if permission_decision:
                    hook_output["permissionDecision"] = permission_decision
                    if normalized_reason:
                        hook_output["permissionDecisionReason"] = normalized_reason
                if response.modified_input is not None:
                    hook_output["updatedInput"] = response.modified_input
        elif decision_style == DroidDecisionStyle.NONE and is_denied:
            # No decision slot; surface as hard stop
            result["continue"] = False
            if normalized_reason:
                result["stopReason"] = normalized_reason

        # Cleanup: drop empty hookSpecificOutput that only has hookEventName
        hook_output = result.get("hookSpecificOutput")
        if isinstance(hook_output, dict) and hook_output == {"hookEventName": hook_event_name}:
            result.pop("hookSpecificOutput", None)

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        hook_event = self.translate_to_hook_event(native_event)
        # hook_type comes from the OUTER envelope (native_event["hook_type"]),
        # not from the inner input_data["hook_event_name"]. Matches the unified
        # route's source dispatch contract; mirrors GeminiAdapter.handle_native.
        hook_type = native_event.get("hook_type") or (
            (native_event.get("input_data") or {}).get("hook_event_name")
        )
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)
```

Behavioral specs:
- PreToolUse returns `hookSpecificOutput.permissionDecision` for `allow|deny|ask`, never top-level `decision:"block"`. Droid matches Claude Code's contract here.
- PostToolUse, UserPromptSubmit, Stop, SubagentStop use top-level `decision:"block"` with `reason` when the HookResponse signals deny.
- SessionStart injects `additionalContext` via `hookSpecificOutput.additionalContext` when the HookResponse carries `system_message`/`context`/`metadata`.
- Notification, PreCompact, SessionEnd cannot block; a denied HookResponse on these events falls through to `continue:true` with a `systemMessage` warning (or `continue:false`+`stopReason` as a last resort for `NONE` style).
- Empty `hookSpecificOutput` (only `hookEventName`) is dropped from the result.

Acceptance criteria:
- `translate_to_hook_event` round-trips each of the 9 event types into the correct `HookEventType`.
- `translate_from_hook_response` produces the exact JSON shape per the contract table (test with golden fixtures per event).
- Source on emitted `HookEvent` is `SessionSource.DROID`.
- `handle_native` is the single public entry point; HTTP route calls only this method.

### 2.2 Register DroidAdapter and extend unified hooks route [category: code] (depends: 2.1)

**Important architectural correction**: Gobby hook ingress is unified at `POST /api/hooks/execute` in `src/gobby/servers/routes/mcp/hooks.py`. There is NO per-CLI route pattern. ghook POSTs every CLI's hooks to the single endpoint with a `source` field in the request body. The endpoint selects the adapter by `source` at lines 373-393. **Do not introduce `/hooks/droid`** — extend the source dispatch instead.

Targets:
- `src/gobby/adapters/__init__.py` — add `from gobby.adapters.droid import DroidAdapter` and re-export `DROID_PASCAL_HOOK_NAMES` from `droid_contract` for installer consumption.
- `src/gobby/servers/routes/mcp/hooks.py` at lines 373-393 — add droid to the source dispatch:

  ```python
  # Insert import alongside the existing adapter imports (lines 367-371):
  from gobby.adapters.droid import DroidAdapter

  # Add a branch after the codex branch (currently around line 388):
  elif source == "droid":
      adapter = DroidAdapter(hook_manager=hook_manager)
  ```

  Also update the fail-message on line 392 to include droid in the supported list: `"Unsupported source: {source}. Supported: claude, gemini, qwen, codex, droid"`.

Verify no changes needed to `HOLD_OPEN_HOOK_TYPE_MAP` at line 35 — droid's `PreToolUse` hook name already matches the Claude-style key. If droid sends `AskUserQuestion` hook events (it doesn't in v0.106.0 per docs), revisit.

Behavior notes (inherited from the unified endpoint):
- Malformed payload → HTTP 400 via `_normalize_hook_request`.
- Missing `hook_type` or `source` → HTTP 400.
- Adapter exception → graceful error response via `_graceful_error_response` (continue=True with additionalContext explaining the error). Droid does not trigger a hook-failure warning because of this.
- The endpoint's web-chat hold-open branch (via `_maybe_hold_open`) runs regardless of source — droid sessions that happen to be web-chat type get the same PreToolUse hold-open treatment.

Acceptance criteria:
- `POST /api/hooks/execute` with `{"hook_type":"PreToolUse","source":"droid","input_data":{...}}` returns a well-formed `DroidAdapter` response.
- Unit test in `tests/servers/routes/test_hooks.py` (or equivalent existing test module) covers the droid dispatch branch end-to-end.
- Unknown `source` still 400's with a message that includes droid in the supported list.
- No new route is registered; only the existing `/api/hooks/execute` endpoint handles droid.

## Phase 3: Installer

**Goal**: `gobby install` detects droid, writes hook registrations, and registers the Gobby MCP proxy with atomic, round-trippable edits.

### 3.1 Add droid hooks template [category: config]

Target: `src/gobby/install/droid/hooks-template.json` (new file)

Mirror `src/gobby/install/claude/hooks-template.json`, trimmed to droid's 9 events. The file uses placeholder command strings rewritten at install time by `rewrite_hook_template_commands` to reference the ghook binary with `--cli=droid`.

Shape:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "__GOBBY_HOOK_COMMAND__" }
        ]
      }
    ],
    "PostToolUse": [ /* same shape */ ],
    "UserPromptSubmit": [ /* same shape without matcher */ ],
    "Notification": [ /* no matcher */ ],
    "Stop": [ /* no matcher */ ],
    "SubagentStop": [ /* no matcher */ ],
    "PreCompact": [ /* no matcher */ ],
    "SessionStart": [ /* no matcher */ ],
    "SessionEnd": [ /* no matcher */ ]
  }
}
```

Use `"matcher": "*"` only on PreToolUse/PostToolUse (per droid docs — other events don't support matchers). For events without matcher support, omit the `matcher` field entirely and flatten to `{"hooks":[{...}]}` shape.

Acceptance criteria:
- Template is valid JSON and round-trips through `rewrite_hook_template_commands` without schema errors.
- Template includes exactly the 9 events in `DROID_PASCAL_HOOK_NAMES`.
- Test in `tests/install/test_droid_template.py` asserts shape and event coverage.

### 3.2 Implement install_droid / uninstall_droid [category: code] (depends: 1.3, 3.1)

Target: `src/gobby/cli/installers/droid.py` (new file)

Match the live installer contract used by `_run_standard_cli_install` at `src/gobby/cli/_install_prompts.py:361-376`. Pattern-match on `src/gobby/cli/installers/claude.py:101-160`; reuse CLI-name-generic helpers from `src/gobby/cli/installers/hook_commands.py` (`build_hook_command`, `rewrite_hook_template_commands`) and MCP helpers from `src/gobby/cli/installers/mcp_config.py`.

Required signatures (must match what `_run_standard_cli_install` dispatches with and what `_echo_install_details` consumes):

```python
def install_droid(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Install Gobby integration for Factory droid (hooks + MCP registration).

    Args:
        project_path: Project root (Path). For global mode, hooks land at
            ~/.factory/hooks/hooks.json; for project mode, at
            <project_path>/.factory/hooks/hooks.json.
        mode: "global" (default) or "project". Matches the convention in
            install_claude / install_gemini / install_qwen.

    Returns a dict with keys: success, error, hooks_installed,
    workflows_installed, commands_installed, mcp_configured,
    mcp_already_configured, (optional) plugins_installed, project_hooks_cleaned.
    """


def uninstall_droid(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Remove Gobby entries from droid's hooks and MCP config.

    Returns a dict with keys: success, error, hooks_removed, files_removed.
    """
```

Implementation details:

- **Hook file path**: write `~/.factory/hooks/hooks.json` (global) or `<project_path>/.factory/hooks/hooks.json` (project). Empirically confirmed from `~/.factory/logs/droid-log-single.log` in droid v0.106.0 probing that exact path; docs pointing at `settings.json.hooks` are stale/forward-looking. `GOBBY_HOOKS_DIR` and `GOBBY_DROID_HOOKS_FILE` env overrides honored. Do NOT also write to `~/.factory/settings.json` under a `hooks` key — dual registration risks double-dispatch.

- **Atomic write pattern**: mirror `installers/claude.py:255-279` — write to temp file in the same directory, then rename over the original. Create a `.{unix_timestamp}.backup` file before overwrite (matches the pattern in `configure_mcp_server_json` at `mcp_config.py:269-278`).

- **Hook command construction**: `build_hook_command(cli_name="droid", hook_event_name=<PascalCase>, ...)` from `hook_commands.py:59`. `rewrite_hook_template_commands(template_json, cli_name="droid", ...)` rewrites the placeholder commands from `src/gobby/install/droid/hooks-template.json` (Phase 3.1) into the final dispatch lines. ghook binary is shared across all CLIs and receives `--cli=droid`; the adapter (Phase 2.1) is the only droid-specific layer in the ingress path.

- **MCP registration**: `configure_mcp_server_json(settings_path=<mcp.json path>, server_name="gobby")` from `mcp_config.py:226`. `remove_mcp_server_json(...)` on uninstall. Both do merge-add / remove-only-named-server with backup — never rewrite the full file. **One extension required**: droid's mcp.json expects a `type: "stdio"` field per its JSON schema (the current helper writes only `command`/`args`). Extend `configure_mcp_server_json` with an optional `extra_server_fields: dict[str, Any] | None = None` kwarg that merges into the server entry at `mcp_config.py:289-297`. Droid installer passes `{"type": "stdio"}`; Claude/Gemini/Qwen call sites in `installers/{claude,gemini,qwen}.py` pass `None` (no behavior change). Cover the new kwarg in the existing mcp_config test suite.

- **Settings-hierarchy warning**: before writing, inspect `<project_path>/.factory/settings.json` and `.factory/settings.local.json`. If either has `hooks: []` / `hooks: {}`, emit a warning to stderr: `"Project-level hooks config at {path} is empty; it overrides user-level and will silently disable Gobby droid hooks. Add Gobby's hook entries to the project-level file or remove the empty hooks key."` Non-blocking.

- **Port-conflict probe — OMITTED from installer.** Droid's daemon default port is not printed in `droid daemon --help` output, and any behavior that depends on reverse-engineering the port from a local binary would be nondeterministic across machines (two different agents could implement two different behaviors and both claim compliance). Port-collision handling is instead deferred to the `docs/cli-integrations/droid.md` troubleshooting section (Phase 6.3): document the symptoms (Gobby daemon fails to bind, or droid daemon fails to bind) and the override path (`~/.gobby/bootstrap.yaml` ports). If a verified droid daemon default port is confirmed post-release, a follow-up task can add a deterministic probe.

- **`_CLI_INSTALL_META` registration**: add an entry to `src/gobby/cli/_install_prompts.py:343-358`:
  ```python
  "droid": (
      "Droid CLI",
      "~/.factory/hooks/hooks.json",
      ".factory/hooks/hooks.json",
      "~/.factory/mcp.json",
  ),
  ```

- **`CLI_VALIDATION_CONFIGS` registration**: add a droid entry to `src/gobby/install/shared/hooks/validate_settings.py:49`:
  ```python
  "droid": ValidationConfig(
      cli_name="Factory droid",
      settings_dir=".factory/hooks",
      settings_file="hooks.json",
      required_hooks=DROID_PASCAL_HOOK_NAMES,
      nested=True,
      check_enable_hooks=False,
  ),
  ```
  Import `DROID_PASCAL_HOOK_NAMES` from `gobby.adapters.droid_contract` alongside the existing `CLAUDE_PASCAL_HOOK_NAMES` import at `validate_settings.py:32`.

Acceptance criteria (tested in `tests/cli/installers/test_droid.py`):
- Install with `mode="global"` writes `~/.factory/hooks/hooks.json` with 9 entries referencing `ghook --cli=droid`.
- Install with `mode="project"` writes `<project_path>/.factory/hooks/hooks.json`.
- Install writes `~/.factory/mcp.json` gobby entry with `type:"stdio"`, `command=<gobby_bin>`, `args=["mcp-server"]`.
- Install preserves pre-existing non-Gobby MCP servers (fixture with 2 pre-existing servers).
- `GOBBY_HOOKS_DIR` redirects the hooks write; `GOBBY_DROID_HOOKS_FILE` redirects the hooks.json path specifically.
- Backup `.{timestamp}.backup` file exists after each write.
- Uninstall removes only Gobby MCP and hook entries, preserves everything else.
- Idempotent install: second run returns `already_configured=True`; file content unchanged.
- Return dict matches `_echo_install_details` expectations (all required keys populated).
- Warning emitted on stderr when project-level `.factory/settings.json` has empty hooks block.
- `validate_settings.py --cli=droid` validates a freshly-installed hooks.json successfully.

### 3.3 Wire droid detector, CLI flag, and install/uninstall dispatch [category: code] (depends: 3.2)

**Important surface correction**: `gobby install` and `gobby uninstall` expose per-CLI boolean flags (`--claude`, `--gemini`, `--codex`, `--qwen`), NOT a generic `--cli <name>` option. Adding droid means adding a `--droid` flag and wiring it through the existing boolean-flag flow in `src/gobby/cli/install.py`.

Targets:

1. **`src/gobby/cli/_detectors.py`** — add `_is_droid_cli_installed()` mirroring the pattern at lines 7-25:
   ```python
   def _is_droid_cli_installed() -> bool:
       """Return True if Factory droid CLI is available in PATH."""
       return shutil.which("droid") is not None
   ```
   Do NOT use `~/.factory` presence as a signal — that trips dev machines with stale dirs.

2. **`src/gobby/cli/install.py`**:
   - Import `_is_droid_cli_installed` and `install_droid` / `uninstall_droid` at the top of the file (alongside the existing imports at lines 17-22 and 41-55).
   - Add `@click.option("--droid", "droid_flag", is_flag=True, help="Install Droid CLI hooks only")` to the `install` command after the `--codex` option (currently at lines 92-97).
   - Add `droid_flag: bool` to the `install()` function signature after `codex_flag`.
   - Add `droid_flag` to the "no flags specified" check at lines 180-187, and add `if _is_droid_cli_installed(): clis_to_install.append("droid")` to the auto-detect block after the `_is_codex_cli_installed()` check at line 204.
   - Add `if droid_flag: clis_to_install.append("droid")` to the explicit-flags branch at lines 222-230.
   - Update the "No supported AI coding CLIs detected" help text at lines 212-220 to mention droid: `"  - Droid CLI:   curl -fsSL https://app.factory.ai/cli | sh"`.
   - Add `"droid": install_droid` to the `_standard_installers` dict at line 280.
   - Mirror the same changes in the `uninstall` command: add `--droid` option (after `--codex` at lines 348-353), add `droid_flag` param, add droid to the "no flags" check (line 414-422), add droid to the auto-detect block (lines 427-446) by checking `~/.factory/hooks/hooks.json` existence, add the explicit-flags branch (lines 461-469), and add `"droid": uninstall_droid` to `_standard_uninstallers` at line 488.

3. **`src/gobby/cli/installers/__init__.py`**:
   - Add `from .droid import install_droid, uninstall_droid` alongside the existing installer imports (lines 8-16).
   - Add `"install_droid"` and `"uninstall_droid"` to `__all__` (lines 32-45 block) under a new `# Droid` comment.

Acceptance criteria:
- `uv run gobby install` with droid in PATH auto-detects and runs `install_droid(project_path, mode="global")`.
- `uv run gobby install --droid` runs only droid install.
- `uv run gobby uninstall --droid` runs only droid uninstall.
- `uv run gobby install` with no droid in PATH and no explicit `--droid` does NOT run the droid installer.
- The "No supported AI coding CLIs detected" message lists Droid CLI.
- `tests/cli/test_install.py` covers the `--droid` flag end-to-end with a fixture droid binary in PATH.

## Phase 4: Transcript parser

**Goal**: Parse droid JSONL transcripts (and their sidecar settings files) into Gobby's unified `ParsedMessage` / `ParsedToolEvent` model.

### 4.1 Implement DroidTranscriptParser [category: code] (depends: 1.1, 1.2)

Target: `src/gobby/sessions/transcripts/droid.py` (new file)

**Extends `BaseTranscriptParser`** from `src/gobby/sessions/transcripts/base.py:198` (not the `TranscriptParser` Protocol at `:106`). Pattern-match on `ClaudeTranscriptParser` at `src/gobby/sessions/transcripts/claude.py:24` — especially its `_expand_line(line, index) -> list[ParsedMessage]` pattern at `:271`, since each droid transcript line expands into one ParsedMessage per content block.

**Critical contract facts (from `base.py`)**:
- `ParsedMessage` fields: `index`, `role`, `content`, `content_type` (one of `text`, `thinking`, `tool_use`, `tool_result`), `tool_name`, `tool_input`, `tool_result`, `timestamp`, `raw_json`, `usage`, `tool_use_id`, `model`, `message_id`. Each content block becomes its OWN `ParsedMessage` — do NOT aggregate multiple blocks into one record.
- `TokenUsage` fields: `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`. **No `thinking_tokens` field.** Droid's sidecar `thinkingTokens` is dropped at parse time (file a follow-up task if richer accounting is required; do NOT extend `TokenUsage` as part of droid scope).
- `ParsedToolEvent` is for Codex-style MCP tool-call lifecycle events (`phase`, `call_id`, `server`, `tool`, `arguments`, `timestamp`, `raw_json`, `result`, `error`, `duration_ns`). Droid's in-message `tool_use`/`tool_result` blocks are NOT lifecycle events — they map to `ParsedMessage` with `content_type="tool_use"` / `"tool_result"` respectively, matching how Claude Code's parser handles its own tool blocks. No `ParsedToolEvent` emissions from droid transcripts.
- Method signatures:
  - `parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None` — single block per call.
  - `parse_lines(self, lines: list[str], start_index: int = 0) -> list[ParsedMessage | ParsedToolEvent]` — default implementation in `BaseTranscriptParser.parse_lines` at `base.py:212-232` iterates and calls `parse_line(line, start_index + i)`. For droid we need multi-output-per-line expansion, so override `parse_lines` explicitly (see Claude parser at `:600`).
  - `extract_last_messages(self, turns: list[dict[str, Any]], num_pairs: int = 2) -> list[dict[str, Any]]` — operates on parsed turn DICTS (not raw lines), returns `[{"role": ..., "content": ...}, ...]`.
  - `extract_turns_since_clear(self, turns: list[dict[str, Any]], max_turns: int | None = None) -> list[dict[str, Any]]`.
  - `is_session_boundary(self, turn: dict[str, Any]) -> bool`.

**Empirical transcript schema** (observed from `~/.factory/sessions/-Users-josh-Projects-gobby/<uuid>.jsonl`):

Two record types: `session_start` (lifecycle, line 1) and `message`. `message` records carry `message.role` and `message.content[]` blocks. Content blocks:
- `{"type":"text","text":"..."}` — user prompts, assistant responses, `<system-reminder>`-wrapped injections.
- `{"type":"thinking","signature":"","signatureProvider":"anthropic","thinking":"..."}` — assistant reasoning. Empty `signature` is valid (observed for non-Anthropic models routed through Factory normalization).
- `{"type":"tool_use","id":"<call-id>","name":"<tool-name>","input":{...}}`.
- `{"type":"tool_result","tool_use_id":"<matching-id>","is_error":false,"content":"<string>"}`.

**Sidecar** `<droid-uuid>.settings.json` beside the JSONL carries per-session metadata:
```json
{"model":"<id>","reasoningEffort":"high","tokenUsage":
  {"inputTokens":N,"outputTokens":N,"cacheCreationTokens":N,"cacheReadTokens":N,"thinkingTokens":N}}
```
Token usage lives in the sidecar — NOT embedded in transcript records. Sidecar is side-read once per `parse_lines` entry and the resulting `TokenUsage` attaches to the LAST assistant `ParsedMessage` only (never to every assistant message; session totals would double-count).

**Sidecar locator contract — `transcript_path`, NOT `session_id`**. The sidecar filename is keyed off droid's native session UUID, which is ALSO the JSONL stem. Gobby's internal `session_id` is a separate identifier and is NOT interchangeable with droid's native UUID. Therefore `DroidTranscriptParser` accepts an explicit `transcript_path: Path | str | None = None` constructor kwarg and derives the sidecar path as `transcript_path.with_suffix(".settings.json")`. Callers (live lifecycle/transcript-reader/summarize sites — see §4.2) MUST pass the session's transcript path when constructing the parser. When `transcript_path` is unset (unit tests, future callers), `_load_sidecar(path)` can still be invoked directly; no filesystem scan is attempted, and usage stays `None`. There is NO `_maybe_load_sidecar_from_session_id` fallback — that heuristic was removed after R2 because Gobby's internal session_id differs from droid's native UUID and the scan would silently drop token usage in every live path.

**Tool-name canonicalization**: parser stores the RAW `tool_name` from droid's content block (e.g. `gobby___list_mcp_servers`). Canonicalization to `mcp__gobby__list_mcp_servers` happens downstream in `src/gobby/hooks/normalization.py::normalize_mcp_fields` (Phase 1.2) when `SessionMessageProcessor` synthesizes BEFORE_TOOL/AFTER_TOOL HookEvents from transcript tool events. The parser does NOT import or reference any `canonicalize_*` helper — no such helper exists, and creating one is out of scope here.

**Implements**:

```python
"""Transcript parser for Factory droid JSONL session files.

Each droid session lives in its own JSONL file — there is no in-file /clear
equivalent, so is_session_boundary returns False for every turn.

Token usage for the session lives in a sidecar <uuid>.settings.json file
beside the JSONL, NOT in the transcript records themselves. The parser
side-reads the sidecar once per parse_lines() entry and attaches usage to
the last assistant ParsedMessage only.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    ParsedToolEvent,
    TokenUsage,
)

logger = logging.getLogger(__name__)

# Strip <system-reminder>, <command-name>, <command-message> blocks.
# Multiple occurrences handled with non-greedy DOTALL matching.
_INJECTED_BLOCK_PATTERN = re.compile(
    r"<(system-reminder|command-name|command-message)>.*?</\1>",
    re.DOTALL,
)


def _strip_injected_blocks(text: str) -> str:
    """Remove injected system/command blocks; return trimmed remainder."""
    return _INJECTED_BLOCK_PATTERN.sub("", text).strip()


def _parse_timestamp(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


class DroidTranscriptParser(BaseTranscriptParser):
    """Parse Factory droid v0.106.0+ JSONL transcripts.

    Each droid transcript line of type="message" can expand into multiple
    ParsedMessage records (one per content block), matching the per-block
    expansion pattern used by ClaudeTranscriptParser._expand_line at
    src/gobby/sessions/transcripts/claude.py:271.
    """

    def __init__(
        self,
        session_id: str | None = None,
        transcript_path: Path | str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(cli_name="droid", session_id=session_id, logger_instance=logger_instance)
        # session_id is Gobby's internal id — used for DB linkage, NOT for
        # locating the sidecar. The sidecar is keyed off droid's native UUID,
        # which is the JSONL filename stem. Callers pass transcript_path so the
        # parser can derive the sidecar path deterministically.
        self._transcript_path: Path | None = Path(transcript_path) if transcript_path else None
        # Sidecar token usage is loaded once per parse_lines() call and attached
        # to the last assistant block; these fields are transient per-parse state.
        self._sidecar_usage: TokenUsage | None = None
        self._sidecar_model: str | None = None
        self._sidecar_loaded_for: Path | None = None

    def _load_sidecar(self, jsonl_path: Path) -> None:
        """Side-read <droid-uuid>.settings.json beside the JSONL."""
        if self._sidecar_loaded_for == jsonl_path:
            return
        sidecar_path = jsonl_path.with_suffix(".settings.json")
        self._sidecar_loaded_for = jsonl_path
        self._sidecar_usage = None
        self._sidecar_model = None
        try:
            raw = sidecar_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.debug("Droid sidecar not present at %s (mid-session read)", sidecar_path)
            return
        except OSError as exc:
            logger.warning("Droid sidecar read error at %s: %s", sidecar_path, exc)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Droid sidecar parse error at %s: %s", sidecar_path, exc)
            return
        usage_raw = data.get("tokenUsage") or {}
        self._sidecar_usage = TokenUsage(
            input_tokens=int(usage_raw.get("inputTokens", 0) or 0),
            output_tokens=int(usage_raw.get("outputTokens", 0) or 0),
            cache_creation_tokens=int(usage_raw.get("cacheCreationTokens", 0) or 0),
            cache_read_tokens=int(usage_raw.get("cacheReadTokens", 0) or 0),
        )
        # droid's thinkingTokens has no home on TokenUsage; intentionally dropped.
        # File a follow-up if richer accounting is needed.
        self._sidecar_model = data.get("model")

    def _expand_line(self, line: str, index: int) -> list[ParsedMessage]:
        """Expand a single droid JSONL line into zero-or-more ParsedMessages.

        session_start and unknown record types return []. message records
        expand into one ParsedMessage per content block (text/thinking/tool_use/tool_result).
        User messages whose text blocks are entirely injected system/command
        content are dropped after strip.
        """
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            self.error_log.log_malformed_line(index, self.session_id, line, str(exc))
            return []

        rec_type = record.get("type")
        if rec_type == "session_start":
            return []
        if rec_type != "message":
            self.error_log.log_unknown_block(index, self.session_id, rec_type or "<missing>", record)
            return []

        message_obj = record.get("message") or {}
        role = message_obj.get("role") or ""
        content_blocks = message_obj.get("content") or []
        timestamp = _parse_timestamp(record.get("timestamp"))
        message_id = record.get("id")

        out: list[ParsedMessage] = []
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                raw_text = block.get("text", "")
                text = _strip_injected_blocks(raw_text) if role == "user" else raw_text
                if not text:
                    continue
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content=text,
                        content_type="text",
                        tool_name=None,
                        tool_input=None,
                        tool_result=None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            elif block_type == "thinking":
                content = block.get("thinking", "")
                if not content:
                    continue
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content=content,
                        content_type="thinking",
                        tool_name=None,
                        tool_input=None,
                        tool_result=None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            elif block_type == "tool_use":
                # Store RAW tool name; canonicalization happens downstream in
                # normalize_mcp_fields when SessionMessageProcessor synthesizes
                # BEFORE_TOOL HookEvents from these records.
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content="",
                        content_type="tool_use",
                        tool_name=block.get("name"),
                        tool_input=block.get("input") or {},
                        tool_result=None,
                        tool_use_id=block.get("id"),
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            elif block_type == "tool_result":
                content_val = block.get("content")
                result_payload: dict[str, Any]
                if isinstance(content_val, str):
                    result_payload = {"content": content_val, "is_error": bool(block.get("is_error"))}
                elif isinstance(content_val, dict):
                    result_payload = {**content_val, "is_error": bool(block.get("is_error"))}
                else:
                    result_payload = {"content": content_val, "is_error": bool(block.get("is_error"))}
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content="",
                        content_type="tool_result",
                        tool_name=None,
                        tool_input=None,
                        tool_result=result_payload,
                        tool_use_id=block.get("tool_use_id"),
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            else:
                self.error_log.log_unknown_block(index, self.session_id, block_type or "<missing>", block)
        return out

    def parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None:
        """Single-record protocol API — returns the first expanded block, or None.

        Streaming callers that need every block should call _expand_line directly.
        This matches the per-call contract of BaseTranscriptParser.parse_line.
        """
        expanded = self._expand_line(line, index)
        return expanded[0] if expanded else None

    def parse_lines(
        self,
        lines: list[str],
        start_index: int = 0,
    ) -> list[ParsedMessage | ParsedToolEvent]:
        """Parse all lines and expand per-block.

        Signature MUST match BaseTranscriptParser.parse_lines exactly
        (src/gobby/sessions/transcripts/base.py:212) so live call sites in
        src/gobby/sessions/lifecycle.py:483 and
        src/gobby/sessions/transcript_reader.py:318,336,349,545 — which call
        parser.parse_lines(lines, start_index=0) with no extra kwargs —
        keep working. Sidecar resolution happens via self._transcript_path
        (set at construction by callers in §4.2), or the caller can call
        _load_sidecar(path) directly before parse_lines.

        Sequential-index reassignment: each expanded block gets its OWN
        monotonically-increasing index via a current_index counter — matches
        ClaudeTranscriptParser.parse_lines at
        src/gobby/sessions/transcripts/claude.py:600-626. Downstream storage
        and ordering depend on unique per-block indices; sharing the raw
        line index across all blocks from a multi-block line breaks them.
        """
        # Load the sidecar from the caller-supplied transcript_path if set.
        # If transcript_path is unset (e.g. unit tests that exercise
        # parse_lines directly without constructor path), usage stays None —
        # degrades gracefully. Callers can also invoke _load_sidecar(path)
        # explicitly for deterministic fixture-based testing.
        if self._transcript_path is not None:
            self._load_sidecar(self._transcript_path)

        results: list[ParsedMessage | ParsedToolEvent] = []
        current_index = start_index
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            expanded = self._expand_line(line, current_index)
            for msg in expanded:
                msg.index = current_index
                current_index += 1
            results.extend(expanded)

        # Attach sidecar usage to the LAST assistant ParsedMessage only
        # (attaching to every assistant message would double-count).
        if self._sidecar_usage is not None:
            for record in reversed(results):
                if isinstance(record, ParsedMessage) and record.role == "assistant":
                    record.usage = self._sidecar_usage
                    break
        return results

    def extract_last_messages(
        self, turns: list[dict[str, Any]], num_pairs: int = 2
    ) -> list[dict[str, Any]]:
        """Return the last N user/assistant pairs as {"role","content"} dicts.

        Iterates over RAW turn dicts (not lines); strips injected blocks from
        user text content before returning.
        """
        messages: list[dict[str, Any]] = []
        for turn in reversed(turns):
            if turn.get("type") != "message":
                continue
            msg = turn.get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            blocks = msg.get("content") or []
            text_parts: list[str] = []
            for block in blocks:
                if block.get("type") != "text":
                    continue
                raw = block.get("text", "")
                stripped = _strip_injected_blocks(raw) if role == "user" else raw
                if stripped:
                    text_parts.append(stripped)
            if not text_parts:
                continue
            messages.append({"role": role, "content": "\n\n".join(text_parts)})
            if len(messages) >= num_pairs * 2:
                break
        return list(reversed(messages))

    def extract_turns_since_clear(
        self, turns: list[dict[str, Any]], max_turns: int | None = None
    ) -> list[dict[str, Any]]:
        """Droid has no in-file /clear; return every turn (optionally capped)."""
        if max_turns is None:
            return list(turns)
        return list(turns)[-max_turns:]

    def is_session_boundary(self, turn: dict[str, Any]) -> bool:
        """Every droid session is its own JSONL file — no in-file boundaries."""
        return False
```

Acceptance criteria (tested in `tests/sessions/transcripts/test_droid_parser.py`):
- Captured fixture `dbf95187-5fa4-43a0-b207-8c24f412baf7.jsonl` + `.settings.json` (redacted absolute paths) checked in under `tests/sessions/transcripts/fixtures/droid/`. Unit test path loads the sidecar explicitly via `parser._load_sidecar(<fixture_jsonl_path>)` before `parser.parse_lines(lines, start_index=0)` — the base-parser signature is unchanged.
- Constructor-path test: instantiate `DroidTranscriptParser(session_id="<gobby-internal-id>", transcript_path=<fixture_jsonl_path>)` where `<gobby-internal-id>` is DIFFERENT from the droid native UUID (`dbf95187-5fa4-43a0-b207-8c24f412baf7`). Call `parse_lines(lines, start_index=0)` and assert the sidecar loaded correctly and `TokenUsage` attached to the last assistant message — proving sidecar resolution is keyed off `transcript_path`, NOT off `session_id`.
- Unset `transcript_path` test: instantiate with only `session_id="..."`, call `parse_lines(...)`, assert every `ParsedMessage.usage is None` and no exception raised — confirms graceful degradation when callers forget to pass a path. The parser does NOT scan `~/.factory/sessions/` looking for JSONLs; there is no `_maybe_load_sidecar_from_session_id` method.
- Each `message` line expands into ONE `ParsedMessage` per content block; line 2 of the captured fixture (user message with 3 text blocks) produces 1 `ParsedMessage` with `content_type="text"` after `<system-reminder>` blocks are stripped from text blocks 1 and 2 (which are injected-only and drop out), leaving only the actual user prompt text.
- Line 3 (assistant message with thinking + text + two tool_use blocks) produces 4 `ParsedMessage` records: `content_type` values `thinking`, `text`, `tool_use`, `tool_use` in source order. Tool-use `tool_name` stored RAW: `"gobby___list_mcp_servers"` and `"Read"` respectively — NO canonicalization in the parser.
- Line 4 (user message with two `tool_result` blocks) produces 2 `ParsedMessage` records with `content_type="tool_result"`, `tool_use_id` matching the prior tool_use IDs, `tool_result` dict with `content` + `is_error` keys.
- Assistant `thinking` block with empty `signature` and `signatureProvider: "anthropic"` parses without raising.
- Sidecar `tokenUsage` (`inputTokens:22571, outputTokens:384, cacheCreationTokens:0, cacheReadTokens:26112`) attaches as `TokenUsage` to the LAST `ParsedMessage` whose `role="assistant"`. First assistant record has `usage is None`; last has populated `TokenUsage` with all 4 fields. `thinkingTokens` from sidecar is silently dropped (no `thinking_tokens` field on `TokenUsage`).
- Missing sidecar → every `ParsedMessage.usage is None`; no exception raised.
- `is_session_boundary(turn)` returns `False` for every turn dict passed.
- `extract_last_messages(turns, num_pairs=2)` returns up to 4 entries ordered chronologically, each `{"role","content"}`; `<system-reminder>` blocks stripped from user content; user turns that become empty after stripping are skipped.
- `parse_line(line, index)` returns the first expanded block (or `None`) for single-record callers; streaming consumers needing every block call `_expand_line` directly.

### 4.2 Wire DroidTranscriptParser into all parser-selection call sites [category: code] (depends: 4.1)

**Critical scope correction**: registering `DroidTranscriptParser` in a single registry module is NOT sufficient. Parser selection is hardcoded via if/elif/default-to-Claude branches at three additional live call sites. Every site needs an explicit droid branch or droid sessions will silently route through the Claude parser.

Targets (all four locations required — NOT optional):

1. **`src/gobby/sessions/transcripts/__init__.py`** — this is the registry-backed parser entry point used by `SessionMessageProcessor.register_session` and anywhere else that resolves parsers by source name. Current shape:
   ```python
   from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
   # ...
   PARSER_REGISTRY: dict[str, type[TranscriptParser]] = {
       "claude": ClaudeTranscriptParser,
       "gemini": GeminiTranscriptParser,
       "qwen": QwenTranscriptParser,
       "codex": CodexTranscriptParser,
   }

   def get_parser(source: str, session_id: str | None = None) -> TranscriptParser:
       parser_cls = PARSER_REGISTRY.get(source, ClaudeTranscriptParser)
       return parser_cls(session_id=session_id)
   ```
   Required changes:
   - Import `DroidTranscriptParser` and add it to `__all__`.
   - Add `"droid": DroidTranscriptParser` to `PARSER_REGISTRY`.
   - Extend `get_parser` signature to accept `transcript_path: str | Path | None = None`. Pass it through ONLY for droid (other parser constructors don't accept it and would error). Pattern:
     ```python
     def get_parser(
         source: str,
         session_id: str | None = None,
         transcript_path: str | Path | None = None,
     ) -> TranscriptParser:
         parser_cls = PARSER_REGISTRY.get(source, ClaudeTranscriptParser)
         if parser_cls is DroidTranscriptParser:
             return parser_cls(session_id=session_id, transcript_path=transcript_path)
         return parser_cls(session_id=session_id)
     ```
   - Update `tests/sessions/test_transcript_parsers.py::TestParserRegistry` (existing class at line ~2002) to include a droid test: `get_parser("droid", session_id="x", transcript_path="/tmp/fixture.jsonl")` returns a `DroidTranscriptParser` whose `_transcript_path` equals the passed path; `get_parser("droid")` (no transcript_path) returns a parser with `_transcript_path is None`.
   - Add `SessionMessageProcessor.register_session` (in `src/gobby/sessions/processor.py` — verify exact signature at implementation time) to the call-site updates below; if it currently calls `get_parser(source, session_id=session_id)`, extend to `get_parser(source, session_id=session_id, transcript_path=<session.transcript_path or equivalent field>)`.

Each call site has access to the session's transcript path (it's how it resolves the JSONL to read). That path MUST be threaded into `DroidTranscriptParser(..., transcript_path=...)` so sidecar-token usage attaches correctly. Without it, token accounting silently drops on every live path.

2. **`src/gobby/sessions/lifecycle.py:456-466`** — the `parser` selection block in the transcript-usage-backfill flow. Current:
   ```python
   parser: Any = ClaudeTranscriptParser(session_id=session_id)
   if session.source == "gemini":
       parser = GeminiTranscriptParser(session_id=session_id)
   elif session.source == "qwen":
       parser = QwenTranscriptParser(session_id=session_id)
   elif session.source == "codex":
       parser = CodexTranscriptParser(session_id=session_id)
   # Default (claude or unknown) uses Claude transcript format
   ```
   Add:
   ```python
   elif session.source == "droid":
       parser = DroidTranscriptParser(
           session_id=session_id,
           transcript_path=session.transcript_path,  # droid native JSONL
       )
   ```
   and `from gobby.sessions.transcripts.droid import DroidTranscriptParser` to the imports at the top of the file. Verify the `session` object here exposes `transcript_path` (e.g. a `Session` record field); if it's a different attribute name in this module (e.g. `transcript_file_path`, `jsonl_path`), match the real attribute — do NOT guess; grep the `Session`/`SessionRecord` model.

3. **`src/gobby/sessions/transcript_reader.py:290-304`** — the `_get_parser(source, session_id)` helper. Current:
   ```python
   def _get_parser(source: str, session_id: str | None = None) -> TranscriptParser:
       from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
       from gobby.sessions.transcripts.codex import CodexTranscriptParser
       from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
       from gobby.sessions.transcripts.qwen import QwenTranscriptParser

       if source == "gemini":
           return GeminiTranscriptParser(session_id=session_id)
       elif source == "qwen":
           return QwenTranscriptParser(session_id=session_id)
       elif source == "codex":
           return CodexTranscriptParser(session_id=session_id)
       else:
           return ClaudeTranscriptParser(session_id=session_id)
   ```
   Required changes:
   - Extend the helper signature to accept an optional `transcript_path`:
     ```python
     def _get_parser(
         source: str,
         session_id: str | None = None,
         transcript_path: str | Path | None = None,
     ) -> TranscriptParser:
         ...
         elif source == "droid":
             from gobby.sessions.transcripts.droid import DroidTranscriptParser
             return DroidTranscriptParser(
                 session_id=session_id,
                 transcript_path=transcript_path,
             )
         else:
             return ClaudeTranscriptParser(session_id=session_id)
     ```
   - Update every `_get_parser(...)` call site in `transcript_reader.py` (and anywhere else grep finds it) to pass `transcript_path=<the file being read>`. Existing non-droid parsers ignore the kwarg; only droid uses it. Safe to thread through unconditionally.
   - The `else` branch remains Claude for the `unknown` fallback.

4. **`src/gobby/sessions/summarize.py:317-333`** — the parser selection block in the summary-generation flow. Current shape matches lifecycle.py's (if/elif default-to-Claude, with lazy imports inside each branch). Add a droid branch:
   ```python
   elif getattr(session, "source", None) == "droid":
       from gobby.sessions.transcripts.droid import DroidTranscriptParser
       parser = DroidTranscriptParser(
           session_id=getattr(session, "id", None),
           transcript_path=getattr(session, "transcript_path", None),
       )
   ```
   Note: existing branches at this site construct parsers without kwargs — droid's sidecar lookup REQUIRES the path, so droid's branch deliberately adds them. Use `getattr` since the session object shape in this module may differ from the lifecycle-manager `Session` record.

5. **`src/gobby/sessions/processor.py`** — the incremental `SessionMessageProcessor.register_session` / per-record parse path. Grep for `ClaudeTranscriptParser`/`GeminiTranscriptParser` construction sites in this module (line numbers shift across revisions; don't hardcode). Each droid branch added here must also pass `transcript_path=<session.transcript_path>` for the same reason. If no such construction site exists (i.e. processor delegates back to `_get_parser` from `transcript_reader`), this target is subsumed by item #3.

Acceptance criteria:
- `grep -n "DroidTranscriptParser" src/gobby/sessions/` returns matches in all four files (`__init__.py`, `lifecycle.py`, `transcript_reader.py`, `summarize.py`), plus `processor.py` if item #5's grep finds a construction site there.
- `grep -n 'source == "droid"\|source == .droid.' src/gobby/sessions/` finds all three explicit dispatch branches (plus processor.py if applicable).
- Every `DroidTranscriptParser(...)` construction in `src/gobby/sessions/` passes both `session_id=` and `transcript_path=` (verified by a lint/grep check) — zero construction sites may omit `transcript_path`. Unit test: regression assertion that greps the source tree and fails if any `DroidTranscriptParser(` call does not include `transcript_path=`.
- Integration test for token-usage fidelity across paths where Gobby internal session_id differs from droid native UUID:
  - Seed DB with a droid session: `Session.id = "gobby-<uuid-1>"`, `Session.transcript_path = "<tmp>/dbf95187-...jsonl"` (fixture copied), sidecar at `<tmp>/dbf95187-....settings.json`.
  - Transcript-usage backfill path (lifecycle.py) populates `TokenUsage` on the last assistant message (inputTokens/outputTokens/cache_* match the fixture sidecar).
  - TranscriptReader read path (transcript_reader.py) populates `TokenUsage` on the last assistant `ParsedMessage` identically.
  - Summary-generation path (summarize.py) runs without raising and the parser instance carries the sidecar model field (sampled by injecting a spy on `_load_sidecar`).
  - Incremental processor path (processor.py) — if item #5 applies, same assertion; else assert processor delegates to `transcript_reader._get_parser` with `transcript_path` threaded.
- Unit test on `_get_parser("droid", transcript_path="/tmp/<droid-uuid>.jsonl")` returns a `DroidTranscriptParser` instance whose `_transcript_path` attribute equals the passed path.
- Import order does not cycle (droid parser imports `events` and `normalization`, not other parsers).

**Optional refactor (out of scope for this release, but worth noting)**: the three parser-selection sites duplicate the same if/elif chain. A follow-up could consolidate them onto a single `gobby.sessions.transcripts.get_parser(source, session_id=None)` registry helper, which would remove the need to patch three files for each new CLI. File that as a separate refactor task if desired; do NOT bundle with 0.4.0 droid work.

## Phase 5: Agent-spawning runtime

**Goal**: Make droid a first-class provider for the `spawn_agent` MCP tool so pipelines, workflows, and delegating agents can launch droid sessions inside Gobby-managed isolation.

**Phase 5 note — AGENTS.md template intentionally NOT shipped.** An earlier draft proposed writing an `AGENTS.md` into each spawned droid worktree, citing "droid reads AGENTS.md natively per the v0.106.0 startup log." Empirically, this claim was imprecise: droid's startup env-check preamble shell-cats a list of files (including `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `QWEN.md`, `AUTH.md`) into the session's first user message as a single `<system-reminder>` block — it does NOT have a first-class project-root-AGENTS.md loader the way Claude has for CLAUDE.md. The YAML-frontmatter loader at `~/.factory/droids/*.md` is for Custom Droids (subagents), not project context. Gobby-context injection into spawned droid sessions happens through the `prompt` argument passed to `prepare_terminal_spawn` (§5.1). That prompt is the first message droid receives; any task-specific Gobby context (task ref, validation criteria, description, guiding principles) should be appended there at spawn time by the caller (typically the skill or workflow that originated the spawn). No filesystem template is needed. If empirical testing after §5.1 lands reveals that droid ignores the initial prompt for some class of invocation, revisit in a follow-up task: either (a) include Gobby context in the `prompt` string every time, or (b) write a minimal `AGENTS.md` into the isolation root so droid's env-check preamble cats it into the first user message. Neither is in scope for this release. No numbered task encodes this decision — it lives here as prose so expansion does not emit an empty refactor task.

### 5.1 Add droid to execute_spawn dispatch and command-builder [category: code] (depends: 2.1, 4.2)

**Important architectural correction**: Gobby's agent-spawning path is tmux-based, not subprocess-based. The canonical flow is `spawn_agent_impl` (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`) → `execute_spawn(request: SpawnRequest)` in `src/gobby/agents/spawn_executor.py:99-117` → provider-specific `_spawn_<provider>_terminal(request)` function → `TmuxSpawner().spawn(command, cwd, env)`. There is NO `src/gobby/agents/definitions.py` (the file does not exist; `src/gobby/agents/CLAUDE.md` references it but is stale). There is NO `src/gobby/agents/runtimes/` subdirectory. Droid wires into the existing `execute_spawn` dispatcher alongside `_spawn_claude_terminal`, `_spawn_gemini_terminal`, `_spawn_qwen_terminal`, `_spawn_codex_terminal`.

Targets:

1. **`src/gobby/agents/spawn_executor.py`**: add a droid branch to `execute_spawn` before the default-claude fallthrough (currently lines 111-117), and add a new `_spawn_droid_terminal(request: SpawnRequest) -> SpawnResult` function mirroring `_spawn_codex_terminal` / `_spawn_gemini_terminal` closest. The function:
   - Calls `prepare_terminal_spawn(session_manager=..., ..., source="droid", ..., prompt=request.prompt, ...)` from `src/gobby/agents/spawn.py:82`. This creates the child session, sets env vars (including `GOBBY_SESSION_ID`), and records agent lifecycle state.
   - Calls `build_cli_command(cli="droid", prompt=request.prompt, auto_approve=True, model=request.model, reasoning_effort=request.effective_reasoning_effort, ...)` from `src/gobby/agents/spawners/command_builder.py:10` (extended in target #2 below). Command returned is the `droid exec` invocation.
   - Maps `request.api_base` / `request.api_token` to droid-specific env vars. Confirmed via `strings ~/.local/bin/droid`: droid honors `FACTORY_API_KEY` (API key, per `droid exec --help` authentication section) and `FACTORY_API_BASE_URL` (custom API base URL). Set `env["FACTORY_API_KEY"] = request.api_token` when non-None; set `env["FACTORY_API_BASE_URL"] = request.api_base` when non-None. These are the verified names — NOT `FACTORY_API_BASE`.
   - Sets `GOBBY_MACHINE_ID` env var when `request.machine_id` is set (mirror claude/gemini/qwen pattern).
   - Calls `pre_approve_directory("droid", request.cwd)` — droid-specific trust handling added in target #3 below.
   - Uses `TmuxSpawner()` (already imported) to launch the command.
   - Returns `SpawnResult` with `message=f"Droid agent spawned in terminal with session {gobby_session_id}"`.

2. **`src/gobby/agents/spawners/command_builder.py`** at lines 64-98: add an `elif cli == "droid":` branch:
   ```python
   elif cli == "droid":
       # Droid exec flags. Verified against `droid exec --help` on v0.106.0:
       #   --auto <low|medium|high>       — autonomy level (derived from auto_approve)
       #   -r, --reasoning-effort <level> — separate reasoning knob
       #   --input-format stream-json     — multi-turn JSON I/O
       #   --cwd <path>                   — workspace resolution hint
       #   -m, --model <id>               — model override
       # `-s, --session-id <id>` is for RESUMING an existing droid session and
       # requires a session droid already knows about — do NOT thread Gobby's
       # pre-created UUID through it (droid rejects or ignores unknown IDs).
       # `--skip-permissions-unsafe` is NOT used; autonomy is surfaced via --auto.
       command.extend(["exec", "--input-format", "stream-json"])
       if working_directory:
           command.extend(["--cwd", working_directory])
       if model:
           command.extend(["--model", model])
       if reasoning_effort:
           command.extend(["--reasoning-effort", reasoning_effort])
       # --auto mapping derives from auto_approve alone; the interactive
       # read-only default (no flag) is safe but blocks on tool approvals,
       # which is not what spawned Gobby agents want.
       autonomy = "high" if auto_approve else "low"
       command.extend(["--auto", autonomy])
       if mode == "interactive":
           # stream-json is already set above for agent mode; no additional flags
           pass
   ```

   **Do NOT enable droid's native `-w, --worktree` flag from Gobby's spawner** — Gobby owns the isolation via `request.cwd`, and that's what gets passed to `TmuxSpawner.spawn(cwd=...)` and droid's `--cwd` argument.

   **Do NOT pass `session_id` to droid's `--session-id`** — droid uses `-s/--session-id` to CONTINUE an existing droid session (per `droid exec --help`: `-s, --session-id <id>  Existing session to continue (requires a prompt)`). Gobby's pre-created session UUID isn't a droid session until droid creates one. Session linkage for spawned droid agents happens via stream-json stdout's first session event (droid emits its own session UUID at startup), which Gobby captures and associates with the child session record — mirror the Gemini/Qwen late-link pattern at `_spawn_gemini_terminal` in `spawn_executor.py:241-358`.

   Update the docstring at `command_builder.py:32-51` to include droid in the supported `cli` values and document the command shape: `droid exec --input-format stream-json --cwd <path> [--model <id>] [--reasoning-effort <level>] --auto <low|high>`.

3. **`src/gobby/agents/trust.py`**: add a minimal no-op handling for droid. Droid with `--auto high` (our default for agent mode) skips permission prompts, so there is no filesystem trust record to write — but we still want `pre_approve_directory("droid", cwd)` to be callable without raising. Extend the function at `trust.py:36-58` to recognize `"droid"` as a no-op branch with a debug-level log entry. If Factory introduces a directory-trust file in the future, add support then; for now, `--auto high` handles the prompt suppression. Document this decision inline.

Acceptance criteria (tested in `tests/agents/test_spawn_executor.py` extending the existing test module):
- `execute_spawn(request_with_provider_droid)` dispatches to `_spawn_droid_terminal`, not the default claude fallthrough.
- `build_cli_command(cli="droid", prompt="...", working_directory="/tmp/wt", model="claude-opus-4-7", reasoning_effort="high", auto_approve=True)` produces `["droid", "exec", "--input-format", "stream-json", "--cwd", "/tmp/wt", "--model", "claude-opus-4-7", "--reasoning-effort", "high", "--auto", "high", "<prompt>"]` (prompt last; `--reasoning-effort` and `--auto` are independent flags).
- `auto_approve=False` produces `--auto low`.
- `--worktree` flag does NOT appear in the constructed command.
- `--session-id` flag does NOT appear in the constructed command (Gobby does not pass its pre-created UUID to droid).
- `pre_approve_directory("droid", "/tmp/wt")` returns without raising and logs a debug message.
- `_spawn_droid_terminal` returns a failure `SpawnResult` when `session_manager` is missing (mirror claude/gemini/qwen behavior at `spawn_executor.py:129-136`).
- `_spawn_droid_terminal` sets `env["FACTORY_API_KEY"]` from `request.api_token` and `env["FACTORY_API_BASE_URL"]` from `request.api_base` when each is non-None.
- Integration test (marked `integration`) spawning an actual `droid exec` against a fixture workspace, asserting the tmux session registers and the child session gets a droid-native session_id captured from the first stream-json event (skip if droid binary not in PATH).

### 5.2 Validate and surface droid provider in spawn_agent [category: code] (depends: 5.1)

**There is NO central provider enum to update.** `spawn_agent_impl` at `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:118-124` treats `provider` as a free-form string with a default of `"claude"` for `None`/`"inherit"`. `execute_spawn` at `spawn_executor.py:111-117` branches on the string explicitly and falls through to `_spawn_claude_terminal` for unknown values (which is a silent-mis-spawn hazard for typos). Adding droid means ensuring the dispatch branch added in 5.1 catches `"droid"` before the default.

Targets:

1. **`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`** at lines 94 and `_factory.py` — update the docstring/description for the `provider` param from `"claude/gemini/qwen/codex"` to include droid: `"AI provider (claude/gemini/qwen/codex/droid). Defaults to claude when unset or 'inherit'."`

2. **Pre-spawn existence check (optional but recommended)**: in `_spawn_droid_terminal` (Phase 5.1), fail early with a friendly error if `shutil.which("droid")` returns `None`. Current agent flows assume the binary is present and surface tmux launch failures as `SpawnResult.success=False, error=...`. Add an explicit early-return at the top of `_spawn_droid_terminal`:
   ```python
   if shutil.which("droid") is None:
       return SpawnResult(
           success=False,
           run_id=request.run_id,
           child_session_id=None,
           status="failed",
           error="droid CLI not found in PATH. Install droid first: see docs/cli-integrations/droid.md",
       )
   ```
   Mirror this pattern for other CLIs in a follow-up refactor if desired (out of scope here).

Acceptance criteria:
- `spawn_agent(prompt="...", provider="droid", isolation="worktree")` via the MCP tool spawns a droid agent in a worktree (integration test, marked `integration`, skipped when droid binary absent).
- `spawn_agent(prompt="...", provider="droid")` with droid not in PATH returns a `SpawnResult.success=False` whose error message names droid and points at the docs page.
- Agent record in DB has `source="droid"`.
- Unknown `provider` values (e.g. typo `"driod"`) still fall through to claude — unchanged behavior, not droid's problem to fix here. Note the existing hazard in a comment at `execute_spawn` but do not widen scope.

### 5.3 Handle isolation hook inheritance [category: code] (depends: 3.2)

Target: `src/gobby/agents/isolation.py`

Spawned droid agents inherit hooks from user-global `~/.factory/hooks/hooks.json` because `--cwd <isolation-path>` does NOT change droid's user-settings resolution root (droid reads user-settings from `~/.factory/` independent of `cwd`). No per-isolation hook copy is needed by default.

Behavior: verify during 5.1 integration test that a spawned droid under a worktree still triggers Gobby hooks (e.g., a PreToolUse block fires and is enforced). If droid's settings hierarchy surprises the integration test (e.g., isolation-internal `.factory/` overrides), implement a per-spawn hook-copy step that writes a merged `<isolation>/.factory/hooks/hooks.json` and documents the reason for the deviation inline.

Default path is "no copy, inherit from global" — only switch to per-spawn copy if empirically forced.

Acceptance criteria:
- Integration test (marked `integration`, co-located with the spawn-executor test from Phase 5.1) spawns droid under a worktree and verifies a PreToolUse hook fires against the Gobby daemon (skipped when droid binary absent).
- If the inherit-from-global path fails the integration test, the code in `src/gobby/agents/isolation.py` switches to a per-spawn hook-copy step that writes `<isolation>/.factory/hooks/hooks.json` merged with Gobby's hook entries, with an inline docstring explaining why the per-spawn copy is required.

Note: there is NO `src/gobby/agents/runtimes/` directory and NO test path at `tests/agents/runtimes/test_droid_runtime.py`. Spawn-executor tests live alongside the existing spawn-executor tests at `tests/agents/test_spawn_executor.py` (or whichever module currently owns the per-provider `_spawn_*_terminal` coverage). Add droid coverage to that existing module rather than a new path.

## Phase 6: ghook upstream and release materials

**Goal**: Land the ghook Rust-binary update and the release-facing docs/README/CHANGELOG entries. This phase is partially parallelizable with phases 2–5.

### 6.1 Upstream ghook --cli=droid route [category: manual]

Target: Upstream PR to `github.com/GobbyAI/gobby-cli` (crates/ghook). **Out-of-repo work — category=manual because the code lives in a separate repository and verification requires merged-PR + published-binary checks rather than an in-repo diff.** Concrete target paths inside `github.com/GobbyAI/gobby-cli` (grep the upstream repo to confirm current layout before opening the PR; these are expected but not locally verifiable from this repo):

- `crates/ghook/src/cli.rs` (or equivalent) — the `--cli=<name>` argument parser / enum.
- `crates/ghook/src/main.rs` (or dispatch module, e.g. `crates/ghook/src/dispatch.rs`) — the per-CLI branch that selects the route handler.
- `crates/ghook/src/routes/droid.rs` (new file) — droid route handler (mirror the claude route's file).
- `crates/ghook/src/routes/mod.rs` — register the new droid module.
- `crates/ghook/tests/droid_route.rs` (new file) — unit/integration tests for the droid route.
- `crates/ghook/Cargo.toml` — bump `version` to the next ghook release.
- `CHANGELOG.md` (upstream) — add a droid-route entry.

Behavior the PR must implement:

- Reads droid's stdin JSON payload per the v0.106.0 spec: top-level fields `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, plus event-specific fields (`tool_name`, `tool_input`, `tool_response`, `prompt`, `message`, `source`, `reason`, `trigger`, `custom_instructions` as applicable).
- POSTs to the **unified** `POST /api/hooks/execute` endpoint on the Gobby daemon (see `src/gobby/servers/routes/mcp/hooks.py:320` in this repo). **There is no per-CLI route.** The request body is the legacy flat shape: `{"hook_type": "<PascalCase name>", "input_data": <the full droid stdin payload>, "source": "droid"}`. The `input_data` payload is passed through to `DroidAdapter.handle_native` unchanged (Phase 2.1).
- Writes the daemon's JSON response to stdout; non-2xx responses → exit 1 with stderr diagnostic.
- Exit-code 2 on `continue:false` per droid's blocking protocol, mirroring the Claude route.
- Same structure as the existing claude route handler — branch on `--cli=droid` next to `--cli=claude` in ghook's dispatch table and reuse the shared HTTP POST + response-translation logic.

Acceptance criteria (manual verification):

- Upstream PR in `github.com/GobbyAI/gobby-cli` is merged, with reviewer sign-off and green CI. PR link captured in the 0.4.0 release notes.
- A released ghook binary contains the droid route. Manually verify: `ghook --cli=droid --daemon-port 60887 < <fixture-stdin-json>` POSTs `{hook_type, input_data, source: "droid"}` to `/api/hooks/execute` and exits 0 on allowed, 1 on HTTP error, 2 on `continue:false`.
- `gobby install` on 0.4.0 pulls the bumped binary (or the gobby bootstrapper detects the old binary — the in-repo ghook-version gate lives in §6.2 and is what surfaces the mismatch to the user).
- `src/gobby/cli/installers/hook_commands.py::build_hook_command(cli_name="droid", ...)` invocation — tested in this repo — produces a command whose runtime behavior matches the upstream route (verified post-release by running a real droid session end-to-end).

The in-repo ghook-version gate — which is verifiable in this repo and NOT in the upstream PR — is split out as a separate task in §6.2.

### 6.2 Add ghook-version gate to install_droid [category: code] (depends: 3.2)

Target: `src/gobby/cli/installers/droid.py` (the file created in Phase 3.2) plus `tests/cli/installers/test_droid.py` for coverage.

When the installed `ghook` binary reports a version older than the release that contains the droid route (§6.1), `install_droid` must still write `hooks.json` (non-blocking) but emit a prominent warning pointing the user to `gobby update` or a manual ghook binary refresh.

Implementation notes:

- Determine the minimum required ghook version by reading a module-level constant in `droid.py` (e.g. `_MIN_GHOOK_VERSION_FOR_DROID = "<version>"`). Set this to the ghook release tag that contains the merged §6.1 PR; update it in the same commit that lands that release (cross-dep: sequencing described below).
- Reuse the existing ghook-version probe if one exists under `src/gobby/utils/` — grep before implementing. If none exists (likely), add a small helper alongside `install_droid` that runs `ghook --version` via `subprocess.run(..., timeout=5)` and parses the first semver-shaped token from stdout. The helper MUST handle: (a) binary absent → return `None`, (b) non-zero exit → return `None`, (c) unparseable output → log a warning and return `None`. In all three failure modes `install_droid` still writes `hooks.json`.
- Compare versions with `packaging.version.Version` (already a transitive dep via `pyproject.toml`). Use the standard `<` comparison.
- Warning shape (stderr + `logger.warning`): `"ghook <installed-version> does not support droid yet; hooks.json written but runtime routing will fail until ghook >= <min> is installed. Run: gobby update"`. Match the copy convention of other installer warnings (grep `installers/claude.py` for precedent).

Sequencing / cross-repo note: this in-repo gate can land ahead of the upstream §6.1 PR. Until §6.1 lands in a ghook release, every `install_droid` run will warn — that's expected and communicates the pre-release status to the user. Once §6.1 ships, bump `_MIN_GHOOK_VERSION_FOR_DROID` in a follow-up commit that coincides with the ghook-binary version in `install_droid`'s expectations.

Acceptance criteria:

- `install_droid` with an outdated ghook binary (mocked version probe returning `Version("0.0.1")`) logs the warning exactly once, still writes `hooks.json`, and `install_droid` returns its normal success result (does not raise).
- `install_droid` with an up-to-date ghook binary (mocked version probe returning `Version("<min>")` or higher) writes `hooks.json` with no warning; `caplog` asserts zero `WARNING` records matching the ghook-version message.
- `install_droid` with ghook missing (`shutil.which("ghook") is None`) logs the "ghook not found" warning AND writes `hooks.json`. Existing installers' behavior is the baseline — if `install_claude` refuses to write when ghook is absent, match that instead; verify empirically and document the chosen behavior inline.
- `install_droid` with ghook present but `--version` returns garbage logs an "unparseable" warning, still writes `hooks.json`.
- Unit tests in `tests/cli/installers/test_droid.py` cover all four cases above via `unittest.mock.patch` on the version-probe helper.
- The module-level constant `_MIN_GHOOK_VERSION_FOR_DROID` is defined, type-annotated (`Final[str]`), and documented with a comment explaining it must be bumped to match the released §6.1 ghook tag.

### 6.3 Add README and docs page for droid [category: docs]

Targets:
- `README.md` — add droid to the supported-CLIs list with install snippet mirroring Claude's.
- `docs/cli-integrations/droid.md` (new) — full user-facing page covering:
  - Installation: `curl -fsSL https://...droid-install | sh` or equivalent, then `uv run gobby install`.
  - Supported hook events (all 9 per `DROID_PASCAL_HOOK_NAMES`).
  - Config file locations, with the docs-vs-binary discrepancy note (docs say `settings.json.hooks`; Gobby installer writes to `hooks/hooks.json` because that's what the binary probes).
  - MCP registration (what Gobby writes to `mcp.json` and how to preserve user's other servers).
  - `spawn_agent` usage: how to launch a droid agent via the MCP tool, autonomy-level mapping, isolation behavior.
  - Token accounting caveat: droid stores session token usage in a sidecar `<uuid>.settings.json`, not in the transcript — Gobby's parser reads both.
  - Troubleshooting: project-level hooks override warning, port-conflict with `droid daemon`, cloud-mirror coexistence note.

Acceptance criteria:
- README copy-pastes cleanly; install snippet works end-to-end.
- `docs/cli-integrations/droid.md` renders in whatever docs toolchain the project uses (check by grepping `docs/` for the toolchain hint — mkdocs/sphinx/hugo).
- Screenshot or transcript excerpt showing a working `spawn_agent` invocation.

### 6.4 CHANGELOG entry [category: docs]

Target: `CHANGELOG.md`

Add a 0.4.0 entry under "Added" covering:
- Droid CLI adapter, installer, transcript parser, agent-spawning runtime.
- MCP tool-name normalization supporting triple-underscore `<server>___<tool>` naming.
- ghook `--cli=droid` route.
- New `SessionSource.DROID` enum value.

Under "Deprecated" (or "Notice"):
- Mention follow-up task #12615 removing vestigial `gobby agents spawn` CLI in a future release.

Acceptance criteria:
- CHANGELOG entry sits above prior entries in chronological order.
- Linked PRs and task refs included (task #12285 for this work, #12615 for the CLI removal note).

### 6.5 Audit shared templates for CLI-gated cases needing droid [category: refactor]

Target: `src/gobby/install/shared/` (rules, workflows, agents, pipelines, agent templates)

Grep the shared-templates tree for any rule/workflow/agent that switches on `source` or a specific CLI name (`"claude"`, `"gemini"`, `"codex"`, `"qwen"`). For each, decide:
- Does droid belong in the match? (e.g., a rule that fires on all interactive CLIs should include droid.)
- Does droid need exclusion? (e.g., a rule specific to Claude Code's permissions UX should stay Claude-only.)

Update the ones that should include droid. Document the ones explicitly excluded in an inline YAML comment for future maintainers.

Acceptance criteria:
- Grep `rg 'source.*claude\|source.*gemini\|source.*codex' src/gobby/install/shared/ -l` yields a reviewed list; each file has had an explicit droid decision.
- No silent mis-routing: every source-dispatch rule/workflow has an explicit droid branch (included or commented exclusion).

### 6.6 Update web UI session-source rendering [category: code] (depends: 1.1)

Targets (concrete files, verified to exist in the current tree):

1. **`web/src/types/sessions.ts`** — the `KNOWN_SOURCES` constant at line 41 currently reads `["claude", "gemini", "qwen", "codex"] as const`. Add `"droid"` to the tuple. Without this, the Sessions page filter dropdown (which enumerates from `KNOWN_SOURCES`) never offers droid as a selectable source even after the label switches are updated. This is the single load-bearing UI edit — everything else flows from it.

2. **`web/src/components/shared/SourceIcon.tsx`** at lines 1-19 — the logo import block and the `codex` special-case branch. Add:
   - `import droidLogo from "../../assets/provider-logos/droid-logo.svg"` (add the asset file if missing; grab Factory's brand mark).
   - `droid: droidLogo` entry in the logo map at lines 13-15.
   - Optionally a special-case render branch analogous to the codex branch at line 19 if droid needs custom rendering (e.g. different container class). Default path is the logo map, which is preferred.
   - Add a test in `web/src/components/shared/__tests__/SourceIcon.test.tsx` that renders a droid-source session and asserts the image src resolves correctly.

3. **`web/src/components/sessions/SessionsPage.tsx`** at lines 26-28 — add a `case 'droid': return 'Droid'` to the display-label switch. Also add a `case 'qwen': return 'Qwen'` — the existing switch is missing it; since Phase 6.6 already touches this file, fixing the qwen omission is in scope (it's a one-line bugfix in the same function).

4. **`web/src/components/sessions/SessionSidebar.tsx`**:
   - Line 33-36 (display-label switch): add a droid case returning `Droid`.
   - Line 43 onwards (CSS dot class switch): add a droid case returning `session-source-dot droid`.

5. **`web/src/components/sessions/SessionsPage.css`** — add a `.session-source-dot.droid` rule with a distinct color (Factory's brand accent — check `factory.ai` for the current color; pick something that doesn't collide with the existing claude/gemini/codex/qwen dot colors). Keep the selected color in a single source of truth: set it here, then re-use it from `sourceTheme.ts` (item #8 below) so the CSS dot, the React source-icon color, and the provider-color map all reference the same value.

6. **`web/src/components/shared/sourceTheme.ts`** — the shared source-theme lookup consumed by the dashboard, resume-session modal, task session viewer, and the `SourceIcon` component. Current shape:
   ```ts
   export const SOURCE_COLORS: Record<string, string> = {
     claude: '#c084fc', gemini: '#4ade80', qwen: '#f59e0b', codex: '#3b82f6',
     pipeline: '#737373', cron: '#a3a3a3', unknown: '#737373', default: '#737373',
   }
   export const SOURCE_LABELS: Record<string, string> = {
     claude: 'Claude', gemini: 'Gemini', qwen: 'Qwen', codex: 'Codex',
     pipeline: 'Pipeline', cron: 'Cron',
     claude_code: 'Claude Code', /* ... */ 
   }
   export const PROVIDER_COLORS: Record<string, string> = {
     inherit: '#9ca3af', claude: SOURCE_COLORS.claude, /* ... */
   }
   ```
   Add `droid: '<picked-color>'` to `SOURCE_COLORS`, `droid: 'Droid'` to `SOURCE_LABELS`, and `droid: SOURCE_COLORS.droid` to `PROVIDER_COLORS`. This is the canonical theme file — every surface that maps a source name to a color/label consumes from here (grep confirms `SourceIcon.tsx` imports `SOURCE_COLORS` from this file).

7. **`web/src/components/shared/sourceIconUtils.ts`** — `SourceType` union type `'claude' | 'gemini' | 'qwen' | 'codex' | (string & {})`. Add `'droid'` as an explicit union member so TypeScript narrows correctly on `droid` source checks (the `(string & {})` pass-through still allows unknown sources at runtime but loses IDE autocomplete for droid). Add a corresponding test in `web/src/components/shared/__tests__/sourceIconUtils.test.tsx` if the file exists; otherwise, the type assertion is covered by the `SourceIcon.test.tsx` droid case.

8. **`web/src/components/tasks/SessionViewer.tsx`** — a local `SOURCE_LABELS` map at line 55 (separate from the shared one in `sourceTheme.ts`) drives the task-session-viewer label. Add a `droid: 'Droid'` entry. Cross-ref note: the duplication of SOURCE_LABELS between `sourceTheme.ts` and this file pre-dates this plan and is out of scope; file a small refactor task to deduplicate if the plan reviewer insists, but do NOT bundle the dedup with 0.4.0 droid work — adding the `droid` entry to both maps is sufficient and matches how the other four sources already work.

9. **Dashboard and resume-modal surfaces** — grep the web tree for consumers of `SOURCE_COLORS` / `SOURCE_LABELS` / `PROVIDER_COLORS` (`grep -rln 'SOURCE_COLORS\|SOURCE_LABELS\|PROVIDER_COLORS' web/src`). Every consumer automatically picks up the new droid entry via the shared maps — NO per-file edits required. The following surfaces are expected consumers and must be smoke-tested (step #11 below): Dashboard (session cards + provider health pills), Resume Session modal (session list + source dot), task session viewer.

10. **Test updates** (retarget to the actual rendered surfaces):
    - `web/src/components/sessions/__tests__/SessionSidebar.test.tsx` — add droid fixture assertions for label text and dot CSS class.
    - `web/src/components/shared/__tests__/SourceIcon.test.tsx` — add droid fixture assertion for icon resolution and color (SOURCE_COLORS.droid).
    - `web/src/components/sessions/__tests__/SessionDetail.test.tsx` — do NOT bundle droid assertions here unless SessionDetail.tsx exposes a label/dot surface that touches source; SessionDetail renders `SourceIcon`, so the `SourceIcon` test already covers this path transitively.
    - Add a SessionsPage filter-options test asserting the source-filter dropdown includes a droid entry after the KNOWN_SOURCES update.
    - Add a `sourceTheme.test.ts` (or extend an existing test) asserting SOURCE_COLORS.droid, SOURCE_LABELS.droid, and PROVIDER_COLORS.droid are all defined and non-empty.
    - Dashboard and Resume Session modal Jest test suites — if existing tests snapshot session cards/rows with source labels, add a droid fixture entry so the snapshot includes droid and re-record snapshots.
    - Task `SessionViewer.test.tsx` (if present) — add droid label assertion for the local SOURCE_LABELS path.

11. **`src/gobby/runner_broadcasting.py`** — grep during implementation; if source-keyed dispatch exists, add a droid branch. If the module is CLI-agnostic (source passed through as an opaque string), no changes required.

Acceptance criteria:
- `KNOWN_SOURCES` in `web/src/types/sessions.ts` includes `droid` and the tuple type propagates (type-checker passes).
- `SourceIcon` renders a droid-source session with the droid logo and SOURCE_COLORS.droid color.
- SOURCE_COLORS.droid, SOURCE_LABELS.droid, PROVIDER_COLORS.droid are all defined in `sourceTheme.ts` and match the CSS `.session-source-dot.droid` color (single-source-of-truth assertion).
- `sourceIconUtils.ts::SourceType` union includes `droid` as an explicit member.
- `SessionViewer.tsx`'s local SOURCE_LABELS map has a droid entry (`droid: 'Droid'`).
- SessionsPage filter dropdown includes a droid entry (Jest test asserts).
- SessionSidebar renders droid sessions with label `Droid` and CSS class `session-source-dot droid`.
- `SessionsPage.css` has a `.session-source-dot.droid` rule.
- Qwen display label added to `SessionsPage.tsx` alongside droid (incidental fix — same function was missing qwen).
- `runner_broadcasting.py` has a droid branch OR is verified CLI-agnostic (note in PR description).
- Grep regression check: `grep -rn droid web/src/components/shared/sourceTheme.ts web/src/components/shared/sourceIconUtils.ts web/src/components/tasks/SessionViewer.tsx` returns at least one match per file.
- Manual smoke: start the daemon, spawn a droid agent via the MCP `spawn_agent` tool, open the web UI, confirm the session appears with the droid icon, droid label, droid dot color across: Sessions page (list + filter), Sessions sidebar, Dashboard (session card + provider health if visible), Resume Session modal, task session viewer. Screenshot each surface for the PR.

### 6.7 Add droid to session-source and spawn-provider allowlists [category: code] (depends: 1.1)

Scope: session-source enumeration, session-stats, spawn-provider dispatch, provider-model discovery, plan-approval recovery loop, WebSocket session config, web-chat provider validation, and worktree installer dispatch. Web-chat *runtime wiring* (new backend class, `runtime_manager.py` registration, web-client allowlists) is split into §6.8 because it's implementation work, not an allowlist edit — but every allowlist touched by §6.8 gets its droid entry added here, so §6.7 + §6.8 together cover the full web-chat surface.

Targets (complete list, verified via ripgrep on the current tree). Every location hardcodes a provider or source allowlist that must include droid; each entry below names the file, the line, and the exact change.

1. **`src/gobby/mcp_proxy/tools/sessions/_crud.py:19`** — `_SUPPORTED_SESSION_SOURCES = ("claude", "gemini", "qwen", "codex")`. Add `"droid"` to the tuple. Drives the `by_source` breakdown emitted by the `session_stats` MCP tool at lines 224-256.

2. **`src/gobby/servers/routes/sessions/core.py:208,211`** — provider-validation set and error message. Current: `if provider not in {"claude", "gemini", "qwen", "codex"}: ... detail="Invalid provider. Must be one of: claude, gemini, qwen, codex"`. Add droid to both the set and the error message.

3. **`src/gobby/servers/provider_models.py:24,266-272`** — `_PROVIDERS = ("claude", "gemini", "qwen", "codex")` tuple at line 24 and a per-provider dispatch at lines 266-272. Line 33's `_QWEN_AUTH_TYPES` is qwen-specific and unrelated — DO NOT modify.

   Required changes, concrete:

   a. Add `"droid"` to the `_PROVIDERS` tuple at line 24.

   b. Add a droid branch to the dispatch at lines 266-272:
      ```python
      if provider == "droid":
          return await self._discover_droid_models()
      ```

   c. Add a new `_discover_droid_models` method that returns a **hardcoded model list** mirroring the complete `Available Models` + `Model details` blocks printed by `droid exec --help` on droid v0.106.0. The list changes when Factory ships new models; stale-when-Factory-updates is acceptable given the refresh procedure documented below and the integration test that diffs against the live `droid exec --help` output. Shape mirrors whatever the other `_discover_*_models` methods return (`list[dict[str, Any]]` per the existing return annotation at line 266-ish).

      The canonical source is the empirical output of `droid exec --help` (see `Available Models` and `Model details` sections). The 24 model IDs below are verbatim from v0.106.0 with reasoning-level data from `Model details`:

      - `claude-opus-4-5-20251101` — Claude Opus 4.5 — reasoning: `[off, low, medium, high]`, default `off`
      - `claude-opus-4-6` — Claude Opus 4.6 — reasoning: `[off, low, medium, high, max]`, default `high`
      - `claude-opus-4-6-fast` — Claude Opus 4.6 Fast Mode — reasoning: `[off, low, medium, high, max]`, default `high`
      - `claude-opus-4-7` — Claude Opus 4.7 (droid default) — reasoning: `[off, low, medium, high, xhigh, max]`, default `high`
      - `claude-sonnet-4-5-20250929` — Claude Sonnet 4.5 — reasoning: `[off, low, medium, high]`, default `off`
      - `claude-sonnet-4-6` — Claude Sonnet 4.6 — reasoning: `[off, low, medium, high, max]`, default `high`
      - `claude-haiku-4-5-20251001` — Claude Haiku 4.5 — reasoning: `[off, low, medium, high]`, default `off`
      - `gpt-5.2` — GPT-5.2 — reasoning: `[off, low, medium, high, xhigh]`, default `low`
      - `gpt-5.2-codex` — GPT-5.2-Codex — reasoning: `[low, medium, high, xhigh]`, default `medium`
      - `gpt-5.4` — GPT-5.4 — reasoning: `[low, medium, high, xhigh]`, default `medium`
      - `gpt-5.4-fast` — GPT-5.4 Fast Mode — reasoning: `[low, medium, high, xhigh]`, default `medium`
      - `gpt-5.4-mini` — GPT-5.4 Mini — reasoning: `[low, medium, high, xhigh]`, default `high`
      - `gpt-5.3-codex` — GPT-5.3-Codex — reasoning: `[low, medium, high, xhigh]`, default `medium`
      - `gpt-5.3-codex-fast` — GPT-5.3-Codex Fast Mode — reasoning: `[low, medium, high, xhigh]`, default `medium`
      - `gemini-3.1-pro-preview` — Gemini 3.1 Pro — reasoning: `[low, medium, high]`, default `high`
      - `gemini-3-flash-preview` — Gemini 3 Flash — reasoning: `[minimal, low, medium, high]`, default `high`
      - `glm-5.1` — Droid Core (GLM-5.1) — reasoning NOT supported (`[none]`)
      - `glm-5` — Droid Core (GLM-5) — reasoning NOT supported (`[none]`)
      - `kimi-k2.6` — Droid Core (Kimi K2.6) — reasoning: `[off, high]`, default `high`
      - `kimi-k2.5` — Droid Core (Kimi K2.5) — reasoning: `[off, high]`, default `high`
      - `minimax-m2.5` — Droid Core (MiniMax M2.5) — reasoning: `[low, medium, high]`, default `high`
      - `minimax-m2.7` — Droid Core (MiniMax M2.7) — reasoning: `[high]`, default `high`
      - `glm-4.7` — Droid Core (GLM-4.7) (deprecated) — reasoning NOT supported (`[none]`)
      - `gpt-5.1-codex-max` — GPT-5.1-Codex-Max (deprecated) — reasoning: `[low, medium, high, xhigh]`, default `medium`

      Refresh procedure (document inline in the method docstring): run `droid exec --help` on the target droid version, diff its `Available Models` + `Model details` sections against this function's return value, and update entries verbatim. Fixture: commit the captured `droid exec --help` output as `tests/fixtures/droid/droid_exec_help_v0.106.0.txt` so the drift-detection test below has a stable reference baseline.

      Illustrative code shape (align keys to the actual `_discover_*_models` return shape at implementation time):

      ```python
      async def _discover_droid_models(self) -> list[dict[str, Any]]:
          """Return Factory droid's model catalog as exposed by droid v0.106.0.

          Hardcoded against the `Available Models` / `Model details` blocks
          printed by `droid exec --help`. Factory does not expose a public
          model-discovery API as of this release. Refresh procedure: run
          `droid exec --help` against the target droid version, diff against
          the baseline fixture at tests/fixtures/droid/droid_exec_help_v0.106.0.txt,
          and update entries verbatim. Stale-when-Factory-updates is
          acceptable provided the drift integration test below re-runs on
          each release bump.

          Other providers discover models via per-CLI queries
          (see _discover_claude_models / _discover_gemini_models /
          _discover_qwen_models / _discover_codex_models). Droid is the
          only provider that uses a static list; the tradeoff is documented
          here. See https://docs.factory.ai/cli for upstream catalog notes.
          """
          return [
              {"id": "claude-opus-4-7", "label": "Claude Opus 4.7",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high", "xhigh", "max"],
               "reasoning_default": "high"},
              {"id": "claude-opus-4-6", "label": "Claude Opus 4.6",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high", "max"],
               "reasoning_default": "high"},
              {"id": "claude-opus-4-6-fast", "label": "Claude Opus 4.6 Fast",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high", "max"],
               "reasoning_default": "high"},
              {"id": "claude-opus-4-5-20251101", "label": "Claude Opus 4.5",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high"],
               "reasoning_default": "off"},
              {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high", "max"],
               "reasoning_default": "high"},
              {"id": "claude-sonnet-4-5-20250929", "label": "Claude Sonnet 4.5",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high"],
               "reasoning_default": "off"},
              {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high"],
               "reasoning_default": "off"},
              {"id": "gpt-5.4", "label": "GPT-5.4",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "medium"},
              {"id": "gpt-5.4-fast", "label": "GPT-5.4 Fast",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "medium"},
              {"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "high"},
              {"id": "gpt-5.3-codex", "label": "GPT-5.3-Codex",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "medium"},
              {"id": "gpt-5.3-codex-fast", "label": "GPT-5.3-Codex Fast",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "medium"},
              {"id": "gpt-5.2", "label": "GPT-5.2",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "low", "medium", "high", "xhigh"],
               "reasoning_default": "low"},
              {"id": "gpt-5.2-codex", "label": "GPT-5.2-Codex",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "medium"},
              {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high"],
               "reasoning_default": "high"},
              {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["minimal", "low", "medium", "high"],
               "reasoning_default": "high"},
              {"id": "minimax-m2.7", "label": "Droid Core (MiniMax M2.7)",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["high"],
               "reasoning_default": "high"},
              {"id": "minimax-m2.5", "label": "Droid Core (MiniMax M2.5)",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high"],
               "reasoning_default": "high"},
              {"id": "kimi-k2.6", "label": "Droid Core (Kimi K2.6)",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "high"],
               "reasoning_default": "high"},
              {"id": "kimi-k2.5", "label": "Droid Core (Kimi K2.5)",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["off", "high"],
               "reasoning_default": "high"},
              {"id": "glm-5.1", "label": "Droid Core (GLM-5.1)",
               "provider": "droid", "reasoning_supported": False,
               "reasoning_levels": [],
               "reasoning_default": None},
              {"id": "glm-5", "label": "Droid Core (GLM-5)",
               "provider": "droid", "reasoning_supported": False,
               "reasoning_levels": [],
               "reasoning_default": None},
              {"id": "glm-4.7", "label": "Droid Core (GLM-4.7) (deprecated)",
               "provider": "droid", "reasoning_supported": False,
               "reasoning_levels": [],
               "reasoning_default": None},
              {"id": "gpt-5.1-codex-max", "label": "GPT-5.1-Codex-Max (deprecated)",
               "provider": "droid", "reasoning_supported": True,
               "reasoning_levels": ["low", "medium", "high", "xhigh"],
               "reasoning_default": "medium"},
          ]
      ```

      **Note**: the actual return-dict schema must match the shape of the other `_discover_*_models` methods in the same file — inspect one of them at implementation time and align field names exactly (`id`/`label`/etc. above is illustrative; the real shape may differ). If the method needs to be sync rather than async, match the existing signatures. `reasoning_levels` precision per model is empirical from `droid exec --help`'s `Model details` section — copy the exact strings (including `off`, `max`) rather than normalizing to Gobby's internal levels.

   d. Acceptance:
      - `provider_models.get_available_models(provider="droid")` (or whatever the public entry point is — trace from the dispatch at 266-272) returns a list of 24 entries with IDs matching exactly the `Available Models` block of `droid exec --help` v0.106.0. No `ValueError: Unknown provider: droid`.
      - Unit test in `tests/servers/test_provider_models.py` asserts the returned ID set equals `{"claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-6-fast", "claude-opus-4-5-20251101", "claude-sonnet-4-6", "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001", "gpt-5.4", "gpt-5.4-fast", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.3-codex-fast", "gpt-5.2", "gpt-5.2-codex", "gemini-3.1-pro-preview", "gemini-3-flash-preview", "minimax-m2.7", "minimax-m2.5", "kimi-k2.6", "kimi-k2.5", "glm-5.1", "glm-5", "glm-4.7", "gpt-5.1-codex-max"}` — exactly 24 entries.
      - Per-model `reasoning_levels` assertions for sample IDs: `claude-opus-4-7` includes `xhigh` and `max`; `gemini-3-flash-preview` includes `minimal`; `glm-5.1` / `glm-5` / `glm-4.7` have `reasoning_supported=False` and empty `reasoning_levels`; `minimax-m2.7` has `reasoning_levels == ["high"]` (single-level).
      - The list's dict shape matches the shape of the other providers' discovery output exactly (same dict keys, same ordering convention).
      - Drift-detection integration test (marked `integration`, skipped when droid binary absent) invokes `droid exec --help` and diffs its `Available Models` IDs AND per-model reasoning levels against the hardcoded list. Test uses the committed fixture at `tests/fixtures/droid/droid_exec_help_v0.106.0.txt` as a stable baseline — when droid ships a new version, the test fails loudly and the maintainer updates both the fixture and `_discover_droid_models` in lockstep.
      - §5.1 acceptance-criteria examples use `claude-opus-4-7` as the canonical default model ID — reconciled with this catalog so the build_cli_command test and the provider dropdown agree.

4. **`src/gobby/servers/websocket/handlers/plan_approval.py:178`** — loop `for source in ("claude", "gemini", "qwen", "codex")`. Add droid. This loop is the web-chat session recovery compat path (look up older web-chat sessions by external_id); §6.8 makes droid a real web-chat provider, so droid web-chat sessions must be recoverable here.

5. **`src/gobby/servers/websocket/handlers/session_config.py:336`** — `valid_providers = {"claude", "gemini", "qwen", "codex"}`. Add droid. Required for WebSocket `session_config` to accept droid as a valid provider for web-chat session creation (§6.8 wires the backend that services this provider).

6. **`src/gobby/servers/websocket/chat/_session.py:44`** — normalized provider check `if normalized in {"claude", "gemini", "qwen", "codex"}`. Add droid. The other `or "claude"` defaults at lines 348, 374, 384, 603, 708 are fallback-to-claude-when-None and require no change.

7. **`src/gobby/mcp_proxy/tools/worktrees/_helpers.py:137-145,150`** — installer dispatch dict at 137 (`"claude": ("gobby.cli.installers.claude", "install_claude", True), ...`) and `Literal["claude", "gemini", "qwen", "codex"]` type at 145. Add `"droid": ("gobby.cli.installers.droid", "install_droid", True)` to the dict (installs_mcp=True since droid registers an MCP server per Phase 3.2). Add `"droid"` to the Literal. Update the "codex always returns False" note at line 150 if droid has similar constraints — for droid, `install_droid` DOES register both hooks and MCP, so True is correct.

8. **`src/gobby/mcp_proxy/tools/worktrees/_create.py:48,61`** — `Literal["claude", "gemini", "qwen", "codex"] | None` type at line 48 and docstring at line 61. Add droid to both.

9. **`src/gobby/cli/extensions.py:79-80`** — `type=click.Choice(["claude", "gemini", "qwen", "codex"])`. Add droid to the Choice list.

10. **`src/gobby/cli/sessions.py:54`** — `--source` option help string `"Filter by source (claude, gemini, qwen, codex)"`. Add droid to the help string. Docs-only edit; the underlying filter already accepts any string.

11. **`src/gobby/sessions/token_tracker.py::SessionTokenTracker.get_usage_summary`** — verified CLI-agnostic (aggregates from DB rows; sources appear naturally). NO code change. Add an inline verification note in the PR description.

Acceptance criteria:
- All 10 code/config edits above are applied; unit tests in each flagged module still pass.
- Ripgrep at release-prep time returns zero additional hardcoded 4-element CLI-source/provider tuples missing droid (search patterns: `_SUPPORTED_SESSION_SOURCES`, `_PROVIDERS`, the literal 4-tuple `"claude", "gemini", "qwen", "codex"`, and the frozenset `{"claude", "gemini", "qwen", "codex"}`).
- `session_stats` MCP tool returns a non-zero `by_source.droid` count after a fixture droid session exists in the DB.
- `provider_models.py` returns a non-error response for `provider="droid"`; the returned model list matches §6.7.3.d exactly (24 IDs).
- `gobby sessions list --source droid` CLI command filters correctly.
- WebSocket chat session config accepts `provider="droid"` without the "Invalid provider" error.
- Worktree MCP tool `worktrees/_create.py` accepts `provider="droid"` and routes to `install_droid` through the `_helpers.py` dispatch dict.
- Plan-approval recovery compat loop finds a droid web-chat session by external_id fallback.

### 6.8 Add DroidWebChatBackend and wire droid into the web-chat runtime [category: code] (depends: 2.1, 4.1, 6.7)

**Goal**: ship droid as a first-class interactive web-chat provider alongside claude/codex/gemini/qwen. Without this, `provider="droid"` on the WebSocket chat path has no runtime backend and falls through to claude while the DB/UI report droid — a visible inconsistency with the 0.4.0 "every capability Gobby offers" promise.

Runtime model: droid's `droid exec --input-format stream-json` emits a multi-turn stream-json protocol on stdout (verified empirically against v0.106.0) and accepts user messages on stdin — the same shape as Gemini's ACP subprocess and Qwen's subprocess patterns. The backend spawns one `droid exec` child per managed session, streams stdout events into the Gobby web-chat event bus, and multiplexes user messages back on stdin. Model override is `--model <id>`; reasoning is `--reasoning-effort <level>`; autonomy is `--auto <level>` (web chat uses `--auto low` so the user sees approval prompts inline — mirroring Claude SDK permission_mode). On session switch_model, the backend tears down the child and respawns with the new `--model` argument (mirror `GeminiWebChatBackend.switch_model` at `provider_backends.py:908`).

**Prerequisite — capture stream-json fixtures before implementation**. §6.8 depends on concrete stream-json event shapes that must be captured empirically against droid v0.106.0. The implementing agent performs this capture as item #0 below before writing the backend; no separate phase task because the capture is a 15-minute scripted procedure, not a research effort, and its output (fixture files) is checked in with the rest of the §6.8 deliverable.

Targets (concrete files, verified to exist in the current tree):

0. **Fixture capture (prerequisite)** — write the following fixtures into `tests/fixtures/droid/stream_json/` by running droid against a controlled prompt. All fixtures live as line-delimited JSON (`.jsonl`) files alongside a `README.md` that documents capture commands and droid version:
   - `session_init.jsonl` — first N lines from `droid exec --input-format stream-json --auto low --model claude-opus-4-7 "echo hi"` capturing the session-id init event droid emits at startup.
   - `text_response.jsonl` — a simple prompt-and-answer turn: `echo '{"type":"user","content":[{"type":"text","text":"say hi"}]}' | droid exec --input-format stream-json --auto low`. Captures assistant text deltas, turn-end, and final-message events.
   - `tool_call.jsonl` — prompt that triggers a built-in tool (e.g. Read): capture tool_use and tool_result events.
   - `permission_request.jsonl` — prompt that triggers a permission prompt under `--auto low` (e.g. a file write). Captures the permission_request event Gobby must translate into the web-chat approval flow.
   - `thinking.jsonl` — a reasoning-capable model with a prompt that elicits thinking (`claude-opus-4-7 --reasoning-effort high`). Captures thinking-block deltas if droid streams them distinctly from text.
   - `error.jsonl` — invalid input (e.g. unknown tool name) to capture the error event shape.
   - `malformed.jsonl` — a hand-crafted line that is not valid JSON, checked in as-is. Used by the parser's robustness test.
   - `eof.jsonl` — empty file; used by the subprocess-close test to assert graceful handling of EOF before any events.

   **Two-stage event translation** — droid's raw stream-json lines are first normalized into `gobby.adapters.gemini_acp_client.StreamEvent(event_type: str, data: dict[str, Any])` (the backend-stream abstraction shared with Gemini/Qwen), then `DroidManagedChatSession._translate_event` converts each `StreamEvent` into the appropriate `gobby.llm.claude_models` chat-event dataclass (`TextChunk`, `ThinkingEvent`, `ToolCallEvent`, `ToolResultEvent`, `DoneEvent`) emitted on the web-chat stream. This matches how `GeminiManagedChatSession._translate_event` at `provider_backends.py:479-541` handles its upstream.

   Stage 1 — droid stream-json line → `StreamEvent`. The implementing agent reads the captured fixtures under `tests/fixtures/droid/stream_json/` to confirm exact droid field names, but the mapping contract uses the Gobby-side `event_type` vocabulary defined in `gemini_acp_client.py:103` (`"init"`, `"content_delta"`, `"result"`, `"error"`):

   | droid stream-json line shape (exact `type` verified from fixture) | Gobby `StreamEvent.event_type` | `StreamEvent.data` payload keys |
   |---|---|---|
   | session-start / init (first event in `session_init.jsonl`) | `"init"` | `{"native_session_id": <droid-uuid>, "model": <id>, "raw": <original-json>}` |
   | Assistant text delta (e.g. `content_block_delta` type=text in `text_response.jsonl`) | `"content_delta"` | `{"kind": "text", "text": <str>, "raw": <original>}` |
   | Assistant thinking delta (per `thinking.jsonl`, if droid streams it distinctly) | `"content_delta"` | `{"kind": "thinking", "text": <str>, "raw": <original>}` |
   | Assistant tool_use (per `tool_call.jsonl`) | `"content_delta"` | `{"kind": "tool_use", "tool_use_id": <id>, "name": <canonical-via-adapter>, "input": <dict>, "raw": <original>}` |
   | Assistant tool_result (per `tool_call.jsonl`) | `"content_delta"` | `{"kind": "tool_result", "tool_use_id": <matching-id>, "content": <str-or-dict>, "is_error": <bool>, "raw": <original>}` |
   | Permission request (per `permission_request.jsonl`) | `"content_delta"` | `{"kind": "permission_request", "tool": <canonical-name>, "input": <dict>, "request_id": <id-if-present>, "raw": <original>}` |
   | Turn-end / assistant-message-complete | `"result"` | `{"turn_id": <id>, "raw": <original>}` |
   | Droid `error` event (per `error.jsonl`) | `"error"` | `{"message": <str>, "code": <str-or-None>, "raw": <original>}` |
   | Malformed JSON line (per `malformed.jsonl`) | (skip — logged at WARNING, NOT emitted as a StreamEvent) | — |
   | EOF before `"result"` (per `eof.jsonl`) | `"error"` | `{"message": "droid subprocess exited unexpectedly", "code": "eof", "raw": null}` |
   | Unknown droid `type` value | `"error"` | `{"message": "unhandled droid event: <type>", "code": "unhandled", "raw": <original>}` |

   Stage 2 — `StreamEvent` → `ChatEvent` inside `DroidManagedChatSession._translate_event`. Mirror `GeminiManagedChatSession._translate_event` at `provider_backends.py:479`:

   | `StreamEvent.event_type` + `data.kind` | emits `ChatEvent` dataclass from `gobby.llm.claude_models` |
   |---|---|
   | `"init"` | (no ChatEvent; capture `native_session_id` onto the managed session for session-linking; emit nothing on the user-visible stream) |
   | `"content_delta"` + `kind="text"` | `TextChunk(text=data["text"])` |
   | `"content_delta"` + `kind="thinking"` | `ThinkingEvent(thinking=data["text"], ...)` — match the constructor signature at `claude_models.py:143`; use a `ThinkingEvent` per streamed delta or aggregate, whichever matches Gemini's handling |
   | `"content_delta"` + `kind="tool_use"` | `ToolCallEvent(name=data["name"], input=data["input"], tool_use_id=data["tool_use_id"])` — match `claude_models.py:72` |
   | `"content_delta"` + `kind="tool_result"` | `ToolResultEvent(tool_use_id=data["tool_use_id"], content=data["content"], is_error=data["is_error"])` — match `claude_models.py:89` |
   | `"content_delta"` + `kind="permission_request"` | routed through `ManagedChatSessionBase._apply_pre_tool_lifecycle` → `has_pending_approval` + `provide_approval(...)`; blocks subprocess stdin write-back until `ManagedChatSessionBase.provide_approval()` resolves (mirror Codex pattern at `provider_backends.py:1117-1260`) |
   | `"result"` | `DoneEvent(...)` — match `claude_models.py:107` |
   | `"error"` | `DoneEvent(error=data["message"], ...)` OR an upstream `_log_upstream_error_event` call per the Gemini pattern; the exact shape depends on how `GeminiManagedChatSession` currently surfaces errors at `provider_backends.py:479-541` — mirror that exactly rather than inventing a new shape |

   **Fixture layout is single-source**: all stream-json fixtures live under `tests/fixtures/droid/stream_json/<name>.jsonl` (one JSONL file per named fixture) plus `tests/fixtures/droid/stream_json/README.md` documenting capture commands and droid version. Any prior plan text referencing `tests/fixtures/droid/stream_json_*.jsonl` (glob-style, flat layout) is obsolete — use the directory form consistently in the task body, the code, and the test assertions.

   **If the captured fixtures reveal field names that don't fit the Stage 1 mapping above** (e.g. droid uses a field name this table didn't anticipate), update both the mapping and the translator in the same commit and note the deviation in the PR body. Do NOT ship a backend whose stream-json parser "works empirically" without updating this contract — the two-stage table is the authoritative reference.

1. **`src/gobby/servers/websocket/chat/droid_backend.py`** — **new dedicated module**. The existing `provider_backends.py` is already 1,420 lines and on the wrong side of the project's "keep files under 1,000 lines" rule. Task #12096 tracks splitting that monolith across per-provider modules (claude/codex/gemini/qwen); extending it further for droid would worsen the debt. The droid backend ships in its own module from day one, setting the template #12096 will eventually align the other providers to.

   Imports (concrete): `from gobby.servers.websocket.chat.provider_backends import (ManagedChatSessionBase, ProviderBackendHealth)` — these are the shared primitives that must NOT be duplicated. Any other helper reused (`_error_message`, `_extract_text`, `_log_upstream_error_event` at `provider_backends.py:73-140`) is imported from `provider_backends.py` — do NOT copy-paste those helpers. If a droid-specific helper is needed (e.g. a stream-json event translator), keep it in `droid_backend.py` private to the module.

   Add two new top-level classes mirroring the Qwen/Codex pattern:

   a. `DroidManagedChatSession(ManagedChatSessionBase)` — per-session wrapper. Methods to implement (signatures mirror `CodexManagedChatSession` at `provider_backends.py:560` and `GeminiManagedChatSession` at `provider_backends.py:331`):
      - `_web_chat_source` → `"droid"`.
      - `_provider_label` → `"droid"`.
      - `_tool_name_adapter` → concrete contract: a function `(tool_name: str) -> str` that wraps `normalize_mcp_fields` on a minimal event dict (`{"tool_name": raw_name}`) and returns `result["tool_name"]`. **Do NOT import or reference any `canonicalize_mcp_tool_name` helper — no such helper exists and §1.2 explicitly forbids adding one.** The wrapping is trivial (≤5 lines) and lives inline in `droid_backend.py`:
        ```python
        from gobby.hooks.normalization import normalize_mcp_fields

        def _droid_tool_name_adapter(raw_tool_name: str) -> str:
            """Canonicalize droid's <server>___<tool> form to mcp__<server>__<tool>.

            Wraps §1.2's normalize_mcp_fields on a minimal event-data dict
            and returns the rewritten tool_name (or the original if no
            rewrite applies). Kept inline here by design — §1.2 forbids a
            module-level canonicalize_mcp_tool_name helper.
            """
            event_data = {"tool_name": raw_tool_name}
            normalize_mcp_fields(event_data)
            return event_data.get("tool_name", raw_tool_name)
        ```
        `DroidManagedChatSession._tool_name_adapter` references this module-private function. §1.2's normalization rule already covers triple-underscore rewriting + pass-through for canonical `mcp__` and native PascalCase names; no additional logic needed here.
      - `send_message(text, ...)` — writes a stream-json input event to the backend's per-session stdin pipe, then `await` the backend's response-drain coroutine. Mirror Codex's `send_message` at `provider_backends.py:638` for approval-request fan-out and Claude-style pre/post-tool lifecycle hooks.
      - `interrupt()` — SIGINT the subprocess, mark pending turn as cancelled.
      - `drain_pending_response()` — consume buffered events from the backend since the last `send_message`.
      - `switch_model(model: str)` — call `await self._backend.switch_model(self, model)` (pass the session object itself, not a bare conversation_id). Matches `ManagedChatSessionBase` contract and the backend's own `switch_model(self, session, new_model)` signature (item b below). Internal map key inside the backend is `session.conversation_id`, but that's implementation detail — the public call here uses the session object.
      - Integrate `_apply_pre_tool_lifecycle` / `_apply_post_tool_lifecycle` (base class) so Gobby hook events fire for droid just like the other providers.
      - Plan-mode + question + approval hooks: match whatever subset the Codex/Gemini classes support. Droid's approval protocol is `--auto low` surfacing tool-approval prompts in the stream-json output as `permission_request` events (verify empirically against v0.106.0 during implementation; if the event shape differs, document and adapt in the `_translate_event` equivalent).

   b. `DroidWebChatBackend` — top-level backend. **Method signatures mirror the managed-session-object contract used by `GeminiWebChatBackend` at `provider_backends.py:749`** — NOT a `conversation_id`-keyed interface. The base class `ManagedChatSessionBase` calls `backend.attach_session(self, ...)`, `backend.detach_session(self)`, `backend.send_message(self, prompt, ...)`, `backend.switch_model(self, new_model)` — the first positional argument is always the managed session object. The backend may internally key subprocess state by `session.conversation_id`, but the public interface must accept the session object. Methods to implement:
      - `__init__(*, sandbox_config: SandboxConfig, default_model: str | None = None)` — store config, initialize per-session child map keyed by `session.conversation_id` (internal detail).
      - `start(*, background: bool = False)` / `stop()` — no-op at startup (droid children are spawned per session). Include a `shutil.which("droid")` probe; record health as `available=False, startup_error="droid CLI not found in PATH"` if absent so `_probe_providers` at `src/gobby/servers/routes/providers.py:172` surfaces it correctly.
      - `health() -> ProviderBackendHealth` — returns `available=True` when the droid binary is present, else `available=False` with the startup error.
      - `attach_session(self, session: DroidManagedChatSession, *, model: str | None = None, reasoning_effort: str | None = None) -> None` — spawns a `droid exec --input-format stream-json --auto low --cwd <sandbox_cwd> --model <id> [--reasoning-effort <level>]` subprocess, wires stdout to a stream-json parser, stdin to a writer, stores the handle keyed by `session.conversation_id`. Mirror `GeminiWebChatBackend.attach_session` at `provider_backends.py:841`.
      - `detach_session(self, session: DroidManagedChatSession) -> None` — closes stdin, terminates the child, awaits exit. Mirror `GeminiWebChatBackend.detach_session` at `provider_backends.py:885`.
      - `send_message(self, session: DroidManagedChatSession, prompt: str, ...) -> AsyncIterator[StreamEvent]` (or whatever the concrete return type is in `GeminiWebChatBackend.send_message` at `provider_backends.py:888` — match exactly). Writes a stream-json user-input event to the subprocess's stdin and yields translated events.
      - `switch_model(self, session: DroidManagedChatSession, new_model: str) -> None` — tear down the current child for `session.conversation_id` and respawn with the new `--model` argument; re-establish the event stream. Mirror `GeminiWebChatBackend.switch_model` at `provider_backends.py:908`.
      - Reuse the shared sandbox-config pattern (`self._sandbox_config = sandbox_config`) and policy-hash propagation so web-chat policy-mismatch handling covers droid sessions uniformly (see `WebChatRuntimeManager.policy_mismatch_reason` at `runtime_manager.py:70`).

      **Interface-compatibility acceptance**: a regression test asserts the public method signatures match the ones the base class calls. Specifically: call `DroidManagedChatSession.start()` / `.stop()` / `.switch_model(m)` / `.send_message(p)` against a fake backend whose methods record their arguments; assert each recorded call received the session object as its first positional arg, not a bare `conversation_id` string. This prevents regressions where the backend is accidentally given a `conversation_id`-first API.

2. **`src/gobby/servers/websocket/chat/runtime_manager.py`** — wire `DroidWebChatBackend` into the runtime manager:
   - Import: add `from gobby.servers.websocket.chat.droid_backend import (DroidManagedChatSession, DroidWebChatBackend)` as a NEW import statement after the existing `provider_backends` import block at `runtime_manager.py:17-26`. Do NOT add these names to the `provider_backends` import line — they live in the new module.
   - Constructor at `runtime_manager.py:32-58`: add `self._droid_backend = DroidWebChatBackend(sandbox_config=self._sandbox_config.model_copy(deep=True))`.
   - `start(background=...)` at `runtime_manager.py:97-107`: add `await self._droid_backend.start(background=background)` alongside the other backends.
   - `stop()` at `runtime_manager.py:109-113`: add `await self._droid_backend.stop()` before or alongside the others (order does not matter — stop is idempotent).
   - `health(provider)` at `runtime_manager.py:115-125`: add `if provider == "droid": return self._droid_backend.health()` before the fallback.
   - `health_snapshot()` at `runtime_manager.py:127-134`: add `"droid": self.health("droid").to_dict()` to the returned dict.
   - `create_session(provider=..., ...)` at `runtime_manager.py:136-...`: add a `if provider == "droid": return DroidManagedChatSession(...)` branch before the fallback.

3. **`src/gobby/servers/routes/providers.py:78-83`** — `_PROVIDER_DEFS` list. Add `("droid", "droid")`. This is what drives the `/api/providers` response; without it, the web UI's new-chat picker never offers droid.

4. **`src/gobby/servers/routes/providers.py`** — if `_SUPPORTED_WEB_CHAT_CODEX_MODELS` (line 14) has a codex-specific allowlist-filter applied in `_filter_models_for_web_chat` (line 157), verify whether droid needs an equivalent. Expected answer: no — droid exposes its full catalog for web chat (the 24 models from §6.7.3); only codex has the curated subset. If `_filter_models_for_web_chat` hardcodes codex handling, add droid as a pass-through case that returns all models unchanged.

5. **`src/gobby/servers/websocket/chat/_messaging.py:216`** — `provider not in {"claude", "codex", "gemini", "qwen"}` rejection set. Add droid. Without this, the WebSocket chat create-session handler rejects droid with `"Invalid provider 'droid'"` before the runtime manager gets the request.

6. **`web/src/hooks/useChat.ts:31`** — `CHAT_PROVIDERS = new Set(["claude", "gemini", "qwen", "codex"])`. Add `"droid"`. The `isChatProvider` type-guard at line 218 reuses this set, so this one edit unlocks provider-restore on page reload and provider-validation on new-chat submit.

7. **`web/src/lib/providerModels.ts`**:
   - `PROVIDER_LABELS` at line 40: add `droid: "Droid"` entry.
   - `PROVIDER_SORT_ORDER` at line 48: add `"droid"` to the tuple. Position: after `"claude"` so the provider picker surfaces it adjacent to the Anthropic-family option users are most likely to compare against.
   - The existing `fetchProviderModelCatalog()` + `getModelsForProvider(catalog, provider)` pair is the entry point the frontend already uses for all providers — droid consumes it unchanged. **Do NOT add a filtered `/api/providers/models?provider=droid` query variant** — no such API contract exists, §6.8 uses the grouped route throughout, and item 7 previously drifted from that. If droid requires provider-specific label parsing (e.g. stripping the "Droid Core (...)" prefix for the picker), add a `parseDroidModelInfo` branch to whatever per-provider parse helper already exists for claude/codex/gemini/qwen; do NOT introduce a new API query.

8. **Tests** (mandatory — the backend is new and there's no existing coverage to delegate to):
   - Python unit tests in a new file `tests/servers/websocket/chat/test_droid_backend.py` (not `test_provider_backends.py` — droid classes live in their own module, tests follow) for `DroidWebChatBackend` (attach/detach/send/switch_model/health; use a fake subprocess that emits canned stream-json fixtures so the tests don't require the droid binary). Fixtures live under `tests/fixtures/droid/stream_json/<name>.jsonl` (directory layout matching item #0's capture procedure).
   - Python integration test in `tests/servers/websocket/chat/test_droid_backend.py` under an `@pytest.mark.integration` marker that spawns a real `droid exec --input-format stream-json` child against a minimal workspace and asserts a round-trip send/receive. Skip when `shutil.which("droid")` is None.
   - Unit test `test_droid_tool_name_adapter`: asserts `_droid_tool_name_adapter("gobby___list_mcp_servers")` → `"mcp__gobby__list_mcp_servers"`, `"mcp__gobby__list"` passes through, `"Read"` passes through. Covers the §1.2 wrapper contract without duplicating the normalize_mcp_fields test suite.
   - `WebChatRuntimeManager` tests in `tests/servers/websocket/chat/test_runtime_manager.py` (create if absent): `create_session(provider="droid")` returns a `DroidManagedChatSession`; `health("droid")` returns the droid backend's health; `health_snapshot()` contains a `droid` key.
   - Jest test extending the existing `useChat` / `providerModels` suites: `isChatProvider("droid")` returns true; `PROVIDER_LABELS.droid === "Droid"`; `PROVIDER_SORT_ORDER` includes `"droid"`; provider picker renders a droid entry when `/api/providers` mocks return one.
   - API integration test (Python, marked `integration`): GET `/api/providers` includes a droid entry with health; GET `/api/providers/models` (grouped route — no `provider` query parameter, per `src/gobby/servers/routes/providers.py:209`) returns a `{"providers": [...]}` dict whose entries include a droid group whose model list exactly matches §6.7.3.d (24 IDs). Frontend selects via `web/src/lib/providerModels.ts::getModelsForProvider("droid")` against that grouped response — matching how claude/codex/gemini/qwen are already consumed.

Acceptance criteria:
- Creating a WebSocket web-chat session with `provider="droid"` creates a `DroidManagedChatSession` and a backing `droid exec --input-format stream-json` subprocess. `_session.py` routing confirms the session is NOT a Claude fallback (assert `session._provider_label == "droid"`).
- `GET /api/providers` returns a droid entry alongside claude/gemini/qwen/codex, with health reflecting `shutil.which("droid")`.
- `GET /api/providers/models` (grouped route, no query parameter) returns a `{"providers": [...]}` response whose droid group contains exactly the 24 IDs in §6.7.3.d with the same dict shape as other providers. This is the existing route contract — do NOT add a `?provider=X` query variant; the frontend filters via `getModelsForProvider` on the grouped response.
- `WebChatRuntimeManager.health_snapshot()` contains a `"droid"` key.
- Sending a message on a droid web-chat session streams the model's response back through the existing event bus; user approval prompts (when droid emits `permission_request` in `--auto low`) surface to the web UI using the same `ManagedChatSessionBase` plan/question/approval hooks.
- `switch_model` tears down and respawns the droid child with the new `--model` flag; the first message after switch uses the new model (assert by inspecting the child's command-line args via `ps` or a wrapped spawner fixture).
- Sandbox-config policy mismatch handling works for droid sessions identically to codex/gemini/qwen (cover with a unit test feeding a session with a stale `sandbox_policy_hash`).
- Web UI smoke: start the daemon, open the web app, create a new chat with provider=droid and a model selected from the dropdown, send a message, see streamed response; reload the page and confirm the session restores with provider=droid retained (via `isChatProvider` / localStorage path). Screenshot attached to the PR.
- No hardcoded `{"claude", "gemini", "qwen", "codex"}` allowlist remains in the web-chat path after this phase — grep proves zero occurrences.
- `src/gobby/servers/websocket/chat/droid_backend.py` exists and `wc -l` reports it under 500 lines (implementation budget). `provider_backends.py` line count is unchanged (no droid classes added there). Grep confirms: `grep -l "class DroidWebChatBackend\|class DroidManagedChatSession" src/gobby/servers/websocket/chat/provider_backends.py` returns empty; the same grep against `droid_backend.py` returns the file.
- Follow-up cross-ref: task #12096 remains the owner of the broader `provider_backends.py` split across claude/codex/gemini/qwen modules; this phase explicitly does NOT widen scope into that split and only establishes the per-provider-module precedent.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
