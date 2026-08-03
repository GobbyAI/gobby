Plan artifact: `.gobby/plans/agy-full-integration.md`

# AGY 1.1.9 Integration and Provider Consistency

**Plan ID:** agy-full-integration

## Overview
`kind: framing`

AGY (Antigravity CLI) is Gobby's only hook-only provider: it has a hook adapter but no
transcript parser, no web-chat backend, no spawn path, and no tool-chat binding.
`docs/research/cli-integration-matrix.md:124` records it as **Blocked** on the premise that
upstream exposes neither parseable transcripts nor a machine transport. **That premise is
now false.** AGY 1.1.9 ships `--output-format stream-json`, `--conversation` resume, and
per-conversation JSONL transcripts, all verified against the installed binary.

Investigating that unblock exposed a second problem: the reason AGY was easy to leave
behind is that provider integration in Gobby is not uniform. Transcript parsers are
dispatched from five separate places, two of which silently fall back to the Claude parser.
Transcript discovery is hook-reported for three providers and disk-derived for two.
`critical_hooks` differs arbitrarily per CLI. Web chat never received the SRT sandbox
migration that spawn got. This epic therefore does two things: it makes AGY a complete
integration, and it normalizes the seams that made AGY's absence invisible.

**Verified against the installed 1.1.9 binary** (not assumed):

- Flags `--conversation`, `--output-format stream-json`, `--disable-slash-commands`,
  `--print-timeout`, `--model`, `--effort`, `--project`, `--add-dir` all exist.
- NDJSON constants `step_update`, `agent_response`, `text_delta`, `tool_info`,
  `permission_mode`, `num_turns` are present in the binary's string table.
- Transcripts exist at `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`.
  Across 66 local conversations, `step_index` is unique, dense and monotonic in every one —
  the file is append-only, so incremental parsing is safe.
- The real record set is **15** `source/type` combinations, not the 4 the source brief listed.
  Tool records include `VIEW_FILE` (50), `MCP_TOOL` (46), `LIST_DIRECTORY` (39),
  `GREP_SEARCH` (36), `SEARCH_WEB` (5), `CODE_ACTION` (2) and a `GENERIC` fallback (25).
  A parser keyed only on `RUN_COMMAND` would drop roughly 78% of tool records.
- Undocumented fields exist: `truncated_fields` (AGY self-truncates `content` or `tool_calls`),
  a string `error`, and `thinking` on `PLANNER_RESPONSE` only.
- AGY's binary embeds its own hook documentation, which is authoritative. It supports
  **exactly five** hook events and **no `SessionStart`**. All hook payload keys are
  **camelCase (protojson)**: `conversationId`, `workspacePaths`, `transcriptPath`,
  `artifactDirectoryPath`, `modelName`, `stepIdx`.
- `PreToolUse` accepts `decision: allow|deny|ask|force_ask`, `permissionOverrides`, and an
  `overwrite` object that rewrites tool arguments before execution.

## Constraints
`kind: framing`

- **AGY floor is 1.1.9.** Older versions stay unavailable with an actionable upgrade message.
  AGY becomes the first version-gated provider CLI; reuse the existing `get_cli_version`
  (`src/gobby/servers/provider_model_discovery.py`) and `is_at_least_version`
  (`src/gobby/install/bin_freshness_models.py`) helpers rather than inventing a mechanism.
- **`--dangerously-skip-permissions` is the house pattern for spawn.** Claude uses it, Qwen
  `--approval-mode yolo`, Grok `--always-approve`, Codex `--ask-for-approval never`, Droid
  `--auto`. AGY matches them, with SRT as the boundary. The source brief made this flag
  conditional on "Gobby deny/block decisions remain fail-closed" — **that precondition holds
  for no provider today** (`crates/ghook/tests/contract.rs:199-216` asserts `"should fail
  open"`), so it is not an AGY-specific gate. It is addressed in 2.3 instead.
- **No new monoliths.** Five touched production files carry a measured line budget:
  `sandbox.py` (822) and `spawn_executor.py` (746) both gain AGY code;
  `adapters/acp_client.py` (955) and `hooks/event_handlers/_session_start/flow.py` (966)
  sit within 45 and 34 lines of the ceiling before the 3.1 ACP-SRT and the 2.2/4.1
  session-start work touches them; and `servers/websocket/chat/_session.py` (988) has 12
  lines of headroom before 3.1's post-hydration launch seam lands in it. If any of the
  five projects at or above 1,000 lines, load the `decompose-monolith` skill and decompose
  in the same task — 3.1 and 4.1 name the concrete extraction targets.
- **No raw local transcripts or account data in fixtures.** Every fixture is scrubbed and
  minimal, derived from verified shapes.
- **Gate 0 blocks everything.** No implementation begins until P1 resolves the open contract
  questions. If `--conversation` does not resume on 1.1.9, P5 and P6 are re-planned, not forced.
  The branch rule is explicit: when a 1.1 probe disproves a contract a downstream section
  consumes, the affected sections are revised and pass a fresh reviewed round before they
  expand — their acceptance items are rewritten to the recorded contract, or converted to
  typed deferrals with open `deferred-from` tasks for any surface Gobby abandons. Downstream
  acceptance is therefore always evaluated against the plan state matching the recorded
  Gate 0 contracts, never against a disproven assumption. The rule has an enforcement
  mechanism, not just prose: every other leaf in the manifest depends on P1 directly or
  transitively, so no downstream leaf can dispatch before 1.1 closes — and 1.1's close is
  gated by the contract checkpoint in acceptance 1.1.11, which requires that for every
  disproven contract the affected sections are revised, pass a fresh adversary round, and
  the derived leaf tasks are updated to the revised acceptance *before* 1.1 closes. The
  already-derived manifest therefore cannot leak a disproven contract into execution.
- Web chat currently applies **no** SRT and only Codex threads a provider-native policy, so
  the migration in P3 changes real behavior for Claude, Grok, Qwen and Droid — not just AGY.
- **Shared NDJSON stream limit is reused, not moved.** `ACP_STREAM_READER_LIMIT_BYTES`
  (16 MiB, `src/gobby/adapters/acp_client.py:86`) stays where it is; Droid and AGY import it.
  No constant relocation or rename.

## P1: Contract Gate
`kind: framing`

**Goal**: Settle every unverifiable claim against the live binary before any code is written.

### 1.1 Probe the AGY 1.1.9 live contract [category: test]
`kind: deliverable`

Targets:
- `tests/fixtures/provider_contracts/agy/README.md`
- `tests/fixtures/provider_contracts/agy/hook-payloads.jsonl`
- `tests/fixtures/provider_contracts/agy/transcript-manifest.json::*` — scope-reason: JSON fixture regenerated wholesale from live 1.1.9 probes
- `tests/fixtures/provider_contracts/agy/stream-json-samples.ndjson`

Run scripted probes in a throwaway workspace and record results in the fixture README.
Seven questions are open and each one changes downstream design:

1. **Does `--conversation <id>` actually resume on 1.1.9?** The earlier probe plan
   (task 15038, `task-15038-agy-grok-contract-probe.md:17`) observed it **timing out**
   on 1.0.1. If it still fails, the web-chat backend cannot be resumable and P5 must be
   re-planned around `--continue` or single-turn sessions.
2. **What does `transcriptPath` literally contain?** AGY's embedded docs say
   `<workspace>/.gemini/antigravity-cli/transcript.jsonl` (workspace-local). Only the
   `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` form was
   observed on disk, and no workspace-local file exists in this repo. The parser and the
   discovery fallback both depend on which is real.
3. **cwd behavior.** Confirm the reported bug: `init.cwd` matches the requested cwd, yet a
   tool call lacking an explicit `Cwd` executes under AGY's application-data directory.
   Determine whether `--project`, `--add-dir`, or the `PreToolUse` `overwrite` field is the
   correct fix.
4. **Image input.** AGY has real image plumbing (`image_paths`/`imagePaths` protobuf field,
   `attachments`, png/jpeg/gif/webp mime handling) but `--help` exposes no image flag.
   Test whether print mode can reach it via an `@path` mention or by asking AGY to view an
   image file. This decides whether `VISION_EXTRACT` is enabled or stays unavailable.
5. **Launch-security flags.** Record `--help` evidence and live argv outcomes for AGY's
   `--sandbox` values and `--dangerously-skip-permissions` in both print and terminal modes,
   including the exact accepted value that disables AGY's native sandbox when SRT is the
   enforcing boundary. These flags define the security boundary consumed by 3.2 and 6.1;
   neither section may embed a flag form this probe did not record.
6. **Live cancellation contract.** Interrupt an active AGY turn in print mode and record
   the exact mechanism (signal or API), the process-tree exit behavior, the partial-stream
   outcome on stdout, whether orphan children remain, and whether the conversation id from
   the interrupted turn still resumes afterward. 5.2 cancellation, 5.2 id-preservation and
   5.3 websocket-interrupt acceptance consume this record; none of them may invent
   interruption semantics this probe did not observe.
7. **Network and state footprint.** Record the exact domains AGY contacts during a
   print-mode turn and the filesystem roots it reads and writes under
   `~/.gemini/antigravity-cli/` — credentials, per-conversation brain state, transcripts.
   3.2's sandbox-policy entries embed only values this probe recorded.

Capture a scrubbed NDJSON sample covering: `init`, resumed turn, assistant `text_delta`,
tool `ACTIVE`/`DONE`/`ERROR`, malformed line, unsuccessful `result`, and a >64 KiB tool output.
Also capture scrubbed live transcript records for one zero-exit and one nonzero-exit
`RUN_COMMAND`, preserving the exact structured fields (`exit_code`, `status`, `error`) and
provenance — 4.2's validation-evidence parity consumes these as the provider-proven payload
shapes required by #18381.

**Acceptance:**

- 1.1.1 - Resume behavior on 1.1.9 is recorded with the exact command and observed output. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.2 - The literal `transcriptPath` value from a live hook invocation is recorded, resolving the workspace-local vs `brain/` ambiguity. file: `tests/fixtures/provider_contracts/agy/transcript-manifest.json`.
- 1.1.3 - cwd behavior for a tool call without explicit `Cwd` is characterized, with the chosen remedy named. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.4 - Image-input support is determined by live test, deciding the `VISION_EXTRACT` binding. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.5 - Hook payloads are re-captured live in camelCase, replacing the snake_case `shape_only_not_live_proven` records. file: `tests/fixtures/provider_contracts/agy/hook-payloads.jsonl`.
- 1.1.6 - A scrubbed stream-json NDJSON sample covers init, resume, text delta, tool lifecycle, malformed line, failure result, and a >64 KiB tool output. file: `tests/fixtures/provider_contracts/agy/stream-json-samples.ndjson`.
- 1.1.7 - The accepted syntax and values for `--sandbox` and `--dangerously-skip-permissions`, including the value that disables AGY's native sandbox, are recorded from live probes in both print and terminal modes. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.8 - Active-turn cancellation is probed live, recording the mechanism, process-tree exit, partial-stream outcome, orphan cleanup, and post-interrupt resumability of the conversation id. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.9 - The domains AGY contacts and the `~/.gemini/antigravity-cli/` roots it reads and writes during a live turn are recorded, sourcing 3.2's sandbox-policy entries. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.10 - Scrubbed live zero-exit and nonzero-exit `RUN_COMMAND` transcript records preserve the exact structured fields and provenance. file: `tests/fixtures/provider_contracts/agy/transcript-manifest.json`.
- 1.1.11 - A contract-outcome table in the fixture README maps every probe question to confirmed or disproven, and for each disproven contract the affected downstream sections are revised, pass a fresh reviewed round, and their derived leaf tasks are updated to the revised acceptance — or converted to typed deferrals with open `deferred-from` tasks — before this task closes. file: `tests/fixtures/provider_contracts/agy/README.md`.

## P2: Provider Consistency Foundation
`kind: framing`

**Goal**: Collapse the divergent seams so AGY is added once, not five times.

### 2.1 Unify transcript parser dispatch to one registry [category: refactor] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/__init__.py::get_parser`
- `src/gobby/sessions/transcript_parsing.py::_get_parser`
- `src/gobby/sessions/transcript_processing.py::TranscriptProcessingMixin._process_session_transcript`
- `src/gobby/sessions/summary_context.py::_build_summary_prompt_context`
- `src/gobby/cli/tokens.py::_load_session_messages`
- `src/gobby/sessions/transcript_index.py::*` — scope-reason: four direct _get_parser call sites migrate to the shared registry
- `src/gobby/sessions/transcript_reader.py::*` — scope-reason: three direct _get_parser call sites migrate to the shared registry
- `src/gobby/sessions/transcript_window.py::*` — scope-reason: the direct _get_parser call site migrates to the shared registry
- `tests/sessions/test_transcript_parsers.py::*` — scope-reason: registry and unknown-source tests re-anchor from _get_parser to the shared registry entry point, and the frozen registry assertion gains the agy entry in 4.2
- `tests/sessions/transcripts/test_droid_parser.py::*` — scope-reason: droid parser tests import _get_parser and migrate to the registry entry point

Five independent source-to-parser maps exist: `PARSER_REGISTRY` plus `get_parser` in
`transcripts/__init__.py`; a duplicate if/elif `_get_parser` in `transcript_parsing.py`; and
three more inline chains — in `TranscriptProcessingMixin._process_session_transcript`,
`_build_summary_prompt_context`, and `_load_session_messages`. Two of those **default to the
Claude parser for unknown sources**, so a new provider silently mis-parses rather than
failing loudly.

Collapse to the single `PARSER_REGISTRY` + `get_parser` entry point. Delete `_get_parser` and
the three inline chains, routing all callers through the registry. Preserve the existing
`droid` special case (it alone takes `transcript_path`) by generalizing the signature rather
than keeping a branch. Unknown sources must raise, never fall back to Claude.

Deleting `_get_parser` reaches beyond the five maps: it has direct runtime consumers in
`transcript_index.py` (four call sites), `transcript_reader.py` (three), and
`transcript_window.py` (one), plus test imports in `test_transcript_parsers.py` and
`test_droid_parser.py`. Every one of those callers migrates to `transcripts.get_parser`
in this deliverable — a deletion that leaves any of them on the removed symbol is an
import error, not a refactor.

**Acceptance:**

- 2.1.1 - `_get_parser` is deleted and its callers route through the shared registry. file: `src/gobby/sessions/transcript_parsing.py`.
- 2.1.2 - The inline parser chain is removed from `_process_session_transcript` and its caller routes through the registry. symbol: `TranscriptProcessingMixin._process_session_transcript`. file: `src/gobby/sessions/transcript_processing.py`.
- 2.1.3 - An unknown source raises rather than silently returning the Claude parser. symbol: `get_parser`. file: `src/gobby/sessions/transcripts/__init__.py`.
- 2.1.4 - The inline parser chain is removed from `_build_summary_prompt_context` and an unknown source raises there. symbol: `_build_summary_prompt_context`. file: `src/gobby/sessions/summary_context.py`.
- 2.1.5 - The inline parser chain is removed from `_load_session_messages` and an unknown source raises there. symbol: `_load_session_messages`. file: `src/gobby/cli/tokens.py`.
- 2.1.6 - The direct `_get_parser` call sites in `transcript_index.py`, `transcript_reader.py`, and `transcript_window.py` migrate to the shared registry, and the droid-path and unknown-source regressions are re-anchored to the registry entry point. test: `tests/sessions/test_transcript_parsers.py`.

### 2.2 Normalize transcript discovery to hook-first with disk fallback [category: refactor] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/transcripts.py::derive_transcript_path`
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::SessionStartMixin._derive_transcript_path`
- `src/gobby/sessions/transcript_paths.py::find_transcript_on_disk`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `src/gobby/agents/watchdog/transcript_resolver.py::*` — scope-reason: late-recovery caller adopts the split discovery contract with explicit caller context
- `src/gobby/tasks/transcript_evidence.py::*` — scope-reason: validation-evidence recovery caller adopts the split discovery contract
- `tests/hooks/test_transcript_path_derivation.py::*` — scope-reason: derivation tests gain usable/pending/invalid classification, bounded-retry, and fallback cases
- `tests/sessions/test_transcript_reader.py::*` — scope-reason: reader-side discovery tests move to the split contract

Discovery is inconsistent: claude/codex/droid read `transcript_path` from the hook payload,
qwen/grok derive it on disk, agy has neither. `derive_transcript_path` handles only qwen and
grok and returns `None` for everything else; `find_transcript_on_disk` carries a per-CLI
if/elif with no agy branch.

Give every provider the same two-stage contract — hook-reported first, disk-derived fallback —
with per-provider derivation expressed as data rather than branching control flow.

Hook-first is a usability test, not a truthiness test. A hook-reported path is **usable**
only if it exists and is readable. AGY in particular reports `transcriptPath` before the
file exists, so a reported-but-absent path is **pending**: it gets a bounded recheck on
subsequent hook events rather than blocking session start, and only a usable path is
persisted on the session. A malformed or unreadable path is **invalid** and falls through
to disk derivation immediately. Disk fallback is bounded: the per-provider table yields
direct candidate paths — never an unbounded directory traversal on the synchronous hook
path.

The classifier must own the value at the real caller. `handle_session_start`
(`flow.py:337`) currently accepts any truthy `input_data["transcript_path"]` directly and
calls the derivation helper only when the reported value is falsy — so a classifier that
lives solely inside the helpers never governs the primary path. Every hook-reported path
routes through the classifier before selection or persistence, at both session-start
acceptance sites in `flow.py`. `flow.py` (966 lines) is inside the Constraints line
budget; if this routing projects it at or above 1,000, decompose in the same task.

Discovery is two contracts, not one. `find_transcript_on_disk` is shared by synchronous
session-start handling, the agent watchdog (`watchdog/transcript_resolver.py`), the
transcript reader's thread-offloaded recovery, and validation-evidence recovery
(`tasks/transcript_evidence.py`). The synchronous hook path gets bounded direct
candidates; the late-recovery callers keep discovery through an explicit contract that
carries the caller context (source, external id) they already pass — a helper-only
rewrite may neither retain blocking traversal on the hook path nor strand a recovery
caller.

**Acceptance:**

- 2.2.1 - Every provider resolves through one hook-first/disk-fallback path. symbol: `derive_transcript_path`. file: `src/gobby/hooks/event_handlers/_session_start/transcripts.py`.
- 2.2.2 - Per-provider disk derivation is table-driven rather than an if/elif chain. symbol: `find_transcript_on_disk`. file: `src/gobby/sessions/transcript_paths.py`.
- 2.2.3 - Hook-reported paths are classified usable, pending, or invalid; pending paths get a bounded recheck without blocking session start, and only usable paths are persisted. symbol: `derive_transcript_path`. file: `src/gobby/hooks/event_handlers/_session_start/transcripts.py`.
- 2.2.4 - Disk fallback derives bounded direct candidates from the per-provider table, with no unbounded traversal on the synchronous hook path. symbol: `find_transcript_on_disk`. file: `src/gobby/sessions/transcript_paths.py`.
- 2.2.5 - Session-start flow routes every hook-reported path through the classifier before selection or persistence — a truthy but absent or unreadable path is never persisted directly. symbol: `handle_session_start`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 2.2.6 - The watchdog, transcript-reader, and validation-evidence recovery callers retain discovery through the split contract with explicit caller context, with usable/pending/invalid, bounded-retry, and fallback cases tested. test: `tests/hooks/test_transcript_path_derivation.py`.

### 2.3 Reconcile critical_hooks and document the fail-open reality [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/ghook/src/cli_config.rs::CliConfig::for_cli`
- `crates/ghook/src/cli_config.rs::agy_uses_antigravity_hook_contract`
- `crates/ghook/src/cli_config.rs::droid_recognized_with_no_critical_hooks`
- `crates/ghook/src/action.rs::action_from_failure`
- `crates/ghook/src/action.rs::action_from_failure_blocks_critical_hooks`
- `crates/ghook/tests/contract.rs::*` — scope-reason: contract tests asserting per-CLI critical-hook and fail-open behavior are updated wholesale to the revised policy
- `docs/guides/sandboxing.md`

`critical_hooks` (declared per CLI in `CliConfig::for_cli`) is arbitrary: claude 3, qwen 4,
grok 3, codex 2, agy 1, droid 0 — and droid short-circuits in `action_from_failure`
(`crates/ghook/src/action.rs`) *before* the criticality check, so adding entries for droid
would do nothing. AGY's single entry is `SessionStart`, which **AGY cannot emit** (its five
events are `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`), making it
dead configuration.

Normalize the policy to session-lifecycle events only, drop AGY's unreachable `SessionStart`
entry, and fix droid's short-circuit so its declaration is meaningful. The final
critical-hook set is stated per provider, in each CLI's native casing, derived from the
proven hook vocabularies (`claude_contract.py`, `CODEX_EVENT_MAP`, `QWEN_HOOK_CONTRACTS`,
grok's snake_case map, `DROID_HOOK_CONTRACTS`, `agy_contract.py`):

| CLI | Final critical set | Delta from today |
| --- | --- | --- |
| claude | `session-start`, `session-end`, `pre-compact` | unchanged |
| codex | `SessionStart`, `SessionEnd`, `PreCompact` | drops `Stop`; gains `SessionEnd`, `PreCompact` |
| qwen | `SessionStart`, `SessionEnd`, `PreCompact` | drops `Stop` |
| grok | `session_start`, `session_end`, `pre_compact` | unchanged |
| droid | `SessionStart`, `SessionEnd`, `PreCompact` | was empty and short-circuited; vocabulary proven in `DROID_HOOK_CONTRACTS` |
| agy | ∅ | drops unreachable `SessionStart`; none of AGY's five events is session-lifecycle, and the turn-level `PreInvocation` carrying 4.1's synthetic registration stays noncritical |

Turn-level events (`Stop` and every tool or prompt hook) are never critical: a daemon
outage must not block every turn. Then state the honest posture in the sandboxing guide:
**no CLI fails closed on `PreToolUse`**, so a daemon outage degrades every permission
denial to allow. This is currently true, tested, and undocumented.

**Acceptance:**

- 2.3.1 - AGY's unreachable `SessionStart` critical hook is removed. symbol: `CliConfig::for_cli`. file: `crates/ghook/src/cli_config.rs`.
- 2.3.2 - Droid's short-circuit no longer bypasses the criticality check. symbol: `action_from_failure`. file: `crates/ghook/src/action.rs`.
- 2.3.3 - The fail-open behavior of `PreToolUse` is documented explicitly. behavior: "PreToolUse denial degrades to allow when the daemon is unreachable" in `docs/guides/sandboxing.md`.
- 2.3.4 - `CliConfig::for_cli` declares exactly the final matrix above for all six CLIs, and a per-provider assertion pins each row. symbol: `CliConfig::for_cli`. file: `crates/ghook/src/cli_config.rs`.
- 2.3.5 - For every provider, a daemon-down contract test proves critical lifecycle hooks block and noncritical events fail open. test: `crates/ghook/tests/contract.rs`.

### 2.4 Share stream-reader limits and dedupe provider constants [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/backends/droid.py::DroidWebChatBackend`
- `src/gobby/servers/websocket/chat/backends/droid.py::DroidManagedChatSession`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.health`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.create_session`
- `src/gobby/ai/registry_builder.py::_tool_chat_binding`
- `src/gobby/ai/registry_builder.py::_tool_chat_adapter_style`
- `tests/servers/websocket/chat/test_droid_backend.py::*` — scope-reason: existing droid backend tests gain the inactivity-timeout expiry, reset-on-activity, and cancellation cases

Three concrete duplications. `WebChatRuntimeManager.health` and
`WebChatRuntimeManager.create_session` each hardcode a **divergent copy** of
`AGY_UNAVAILABLE_REASON` (dropping "or agent spawning") instead of importing the constant
from `providers/registry.py`. `registry_builder.py` contains an unreachable agy tool-chat
branch, dead because the binding path already returns `None` for agy. And the ACP client
sets a 16 MiB `StreamReader` limit (`ACP_STREAM_READER_LIMIT_BYTES`,
`src/gobby/adapters/acp_client.py:86`) with a read timeout, while Droid inherits asyncio's
64 KiB default with no timeout — a >64 KiB NDJSON line raises `LimitOverrunError` and kills
the turn.

Import the shared constant into the runtime manager, delete the dead agy branch, and have
Droid's subprocess creation (`DroidWebChatBackend.attach_session`) pass
`ACP_STREAM_READER_LIMIT_BYTES` as the reader limit with a read timeout on its
`readline()` loops. The constant stays defined in `acp_client.py`; both NDJSON backends
import it.

The timeout is a contract, not a number. Reuse the ACP precedent —
`DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS` (120 s, `acp_client.py:55`) — as an **inactivity**
timeout on each `readline()`, reset by every received line, with an env override following
the existing `GOBBY_<PROVIDER>_ACP_PROMPT_TIMEOUT_SECONDS` pattern. On expiry: emit exactly
one terminal error event for the turn, terminate the owned subprocess tree, remove the
session handle, and leave the session reconnectable — never a silent hang, an orphaned
Droid process, or a dead handle that blocks a new attach.

**Acceptance:**

- 2.4.1 - The runtime manager imports `AGY_UNAVAILABLE_REASON` instead of duplicating it. symbol: `WebChatRuntimeManager.health`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 2.4.2 - The unreachable agy tool-chat branch is deleted. symbol: `_tool_chat_binding`. file: `src/gobby/ai/registry_builder.py`.
- 2.4.3 - Droid's NDJSON reader uses the shared 16 MiB limit and the inactivity timeout, reset by received lines. file: `src/gobby/servers/websocket/chat/backends/droid.py`.
- 2.4.4 - Timeout expiry emits one terminal error, terminates the owned process tree, removes the handle, and leaves the session reconnectable, with focused tests covering expiry, reset-on-activity, and cancellation. test: `tests/servers/websocket/chat/test_droid_backend.py`.

### 2.5 AGY version-gate foundation [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/providers/version_gate.py`
- `src/gobby/servers/_app_lifecycle.py::lifespan`
- `src/gobby/runner_init/services.py::*` — scope-reason: the version probe publishes before support-dependent service construction in the same init seam
- `src/gobby/servers/provider_models.py::ProviderModelCatalog`
- `tests/providers/test_version_gate.py`

The 1.1.9 floor has two consumers with incompatible seams: `get_cli_version` is async,
while capability-registry construction and `WebChatRuntimeManager` health are synchronous —
and 5.3 cannot gate on a deliverable (6.2) that depends on 5.3. Break both problems with
one earlier foundation: an async startup probe in the new module
`src/gobby/providers/version_gate.py` resolves the installed AGY version exactly once,
reusing `get_cli_version` and `is_at_least_version`, and publishes an immutable support
record (installed version, required floor, supported flag, actionable upgrade message
naming both versions). Synchronous consumers — registry build, runtime health, spawn
gating — read the record; none of them await, subprocess, or re-probe. A missing or
unparseable binary yields an unsupported record with a truthful reason, never an exception
at read time. 5.3 and 6.2 both consume this record and depend on this deliverable.

The record has a concrete initialization owner, and it must run **before** any consumer
freezes its value. The FastAPI lifespan is too late: `runner_init/services.py:69` builds
`ToolChatService` — and with it the capability registry — during runner initialization,
before the server (and its lifespan) starts, so a lifespan-published record would leave a
supported AGY frozen unavailable in the already-built registry. Publication therefore
happens in runner initialization, before `build_daemon_tool_chat_service` constructs any
support-dependent service; the lifespan *asserts* publication at startup rather than
performing it. Before publication the module exposes a fail-closed sentinel —
unsupported, reason "version probe has not run" — so a read at any time returns a
truthful record and never raises or blocks. Every consumer reads this one record:
registry build, runtime health, spawn gating, and `ProviderModelCatalog` — whose AGY
sub-floor detection moves onto the record *here*, not in 6.3: the catalog currently
imports `get_cli_version` and launches its own probe, which would violate the
exactly-one-probe guarantee on every refresh. None re-probe, so the daemon performs
exactly one AGY version subprocess call per startup, catalog refreshes included.

**Acceptance:**

- 2.5.1 - An async startup probe resolves the AGY version once and publishes an immutable support record readable from synchronous consumers. file: `src/gobby/providers/version_gate.py`.
- 2.5.2 - Below the 1.1.9 floor, and when the binary is absent or unparseable, the record is unsupported with a message naming the installed and required versions. file: `src/gobby/providers/version_gate.py`.
- 2.5.3 - Focused tests cover supported, sub-floor, absent-binary and unparseable-output records, and prove sync consumers never trigger a re-probe. test: `tests/providers/test_version_gate.py`.
- 2.5.4 - Record publication precedes support-dependent service construction: the probe completes before `build_daemon_tool_chat_service` builds the registry, the lifespan asserts publication at startup, and pre-publication reads return the fail-closed sentinel. file: `src/gobby/runner_init/services.py`.
- 2.5.5 - `ProviderModelCatalog` reads the AGY support record and never launches its own AGY version probe; a daemon-construction test proves the installed `ToolChatService` registry sees the published record and a catalog refresh triggers no second probe. symbol: `ProviderModelCatalog`. file: `src/gobby/servers/provider_models.py`.

## P3: Web-Chat SRT Migration
`kind: framing`

**Goal**: Bring web chat under the same sandbox boundary spawn already has.

### 3.1 Wrap web-chat backends in SRT [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox.py::*` — scope-reason: web_chat_sandbox_config and the versioned complete web_chat_sandbox_policy_hash are reworked in the module that also gains the agy resolver in 3.2
- `src/gobby/agents/srt_runtime.py::prepare_sandbox_launch`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.create_session`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.start`
- `src/gobby/config/app.py::DaemonConfig`
- `src/gobby/servers/websocket/chat/backends/droid.py::DroidWebChatBackend.attach_session`
- `src/gobby/servers/websocket/chat/backends/acp.py::ACPWebChatBackend`
- `src/gobby/adapters/acp_client.py::*` — scope-reason: the ACP subprocess launch moves to session-owned lifetime and gains SRT argv wrapping for the grok/qwen backends
- `src/gobby/servers/websocket/chat/backends/codex.py::*` — scope-reason: the app-server launch moves to session-owned lifetime; provider-native policy threading is replaced by SRT with the CLI's own sandbox pinned off
- `src/gobby/servers/chat_session.py::*` — scope-reason: the Claude SDK session gains SRT via a daemon-emitted executable shim assigned to ClaudeAgentOptions.cli_path
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: ChatSessionMixin._create_chat_session_inner and the session lifecycle owners gain the post-hydration SRT launch seam
- `tests/servers/websocket/chat/test_launch_contracts.py`

SRT adoption (commit `af1908b3e`) reached only the spawn path — the spawn and resume
executors and the tmux spawner. `prepare_sandbox_launch` is called from exactly two places,
both spawn. There are **zero** SRT references under `src/gobby/servers/websocket/`. Web chat
defaults to `backend="provider-native"` with `allow_network=True`, and of the backends only
Codex threads a real policy (`CodexSandboxResolver.thread_sandbox_policy`) — Claude, Droid
and the ACP backends store `sandbox_config` and never apply it. The web-chat sandbox config
is effectively decorative, feeding only a policy hash for resume invalidation.

Route web-chat subprocess launches through `prepare_sandbox_launch` and flip the
`web_chat_sandbox` default (declared on `DaemonConfig`) to `backend="srt"` with a
**bounded network policy**: `allow_network=False` plus the explicit allowed domains and
scoped Git/package capabilities web chat needs. `prepare_sandbox_launch` hard-refuses
`backend="srt"` with `allow_network=True` (`srt_runtime.py:309` raises an SRT lockout), so
the current `allow_network=True` value cannot survive the flip — the default must be a
policy SRT preflight accepts. Unrestricted networking under SRT is out of scope; wanting it
back means a separate SRT contract change with its fail-closed test updated first.

Process ownership changes with the boundary. Today Codex's app-server and the grok/qwen ACP
servers start as warm daemon-shared subprocesses before any session project path exists,
while SRT preparation requires that path — so wrapping current startup cannot confine
concurrent projects. Kill the warm shared start: every web-chat subprocess becomes
**session-owned**, torn down when the session ends. The launch point is the session's
**asynchronous post-hydration seam**, not `create_session`: `WebChatRuntimeManager.create_session`
is synchronous and runs before `_session.py` resolves the session's `project_path`, so a
launch there would either block the event loop or confine the wrong workspace. The SRT
preparation and subprocess launch belong in the awaited session-start path —
`ChatSessionMixin._create_chat_session_inner` (`_session.py:340`) through
`ManagedChatSessionBase.start`/backend attach — after the final project path (including
worktree paths) is known. `create_session` stays the synchronous orchestration entry and
performs no subprocess launch; a failed start cleans up the partially-launched process and
handle. `_session.py` is 988 lines — the Constraints line budget applies, with same-task
decomposition if the seam work projects it at or above 1,000. Launch surfaces:

- **Droid**: the subprocess spawn in `DroidWebChatBackend.attach_session` (already
  per-session; gains the SRT wrap).
- **Grok/Qwen (ACP)**: the ACP server subprocess launched by `acp_client.py`, moved from
  shared-warm to session-owned lifetime.
- **Codex**: the app-server subprocess, moved from shared-warm to session-owned lifetime;
  the provider-native policy threading is superseded.
- **Claude**: the Agent SDK session in `chat_session.py`. `ClaudeAgentOptions.cli_path`
  (consumed by `SubprocessCLITransport`) accepts one executable path while `SandboxLaunch`
  exposes a wrapped argv vector, so the daemon emits a fresh per-launch executable shim
  that execs the SRT-wrapped argv with `"$@"` appended, and `cli_path` points at that shim.
  The shim is private API of the sandbox layer, written at launch time by the same daemon
  that computed the policy (no installed-binary skew), and removed on session teardown,
  disconnect, and failed start.

Preserve `policy_mismatch_reason` resume-invalidation semantics — existing sessions carrying
the old policy hash must surface the mismatch, never silently run under a different boundary.
For that guarantee to hold across this migration, `web_chat_sandbox_policy_hash` must cover
the **complete** normalized effective policy — backend, network fields, domains, Git/package
capabilities, socket allowances — with an explicit hash version; today it omits backend and
material network fields, so a provider-native session could retain its hash across the SRT
flip and resume silently under a different boundary.
Follow the established nesting precedent: when SRT is enforced, pin the CLI's own sandbox
off, exactly as the spawn path does for Claude (`--settings '{"sandbox": {"enabled":
false}}'`) and Codex (`sandbox_mode="danger-full-access"`).

Pin the launch seam with a parameterized launch-contract test matrix covering the five
incumbent providers — Claude, Codex, Droid, and the ACP pair (Grok/Qwen). For each path it
asserts: `prepare_sandbox_launch` contributes exactly one SRT wrapper (no double wrapping),
the bounded network policy is represented in the SRT policy and accepted by preflight, the
provider-native sandbox is disabled when SRT enforces, the explicit provider-native backend
remains usable, and a stale policy hash refuses resume. The AGY row joins this matrix in
5.2, where `AgyWebChatBackend` exists. This matrix is the concrete anchor for the V2
cross-provider regression item.

Line budget: `acp_client.py` is 955 lines with the ACP launch-ownership change landing in
it. If it projects at or above 1,000, extract the subprocess launch/lifetime seam into its
own module within this task, per the `decompose-monolith` constraint.

**Acceptance:**

- 3.1.1 - Web-chat subprocess launches are wrapped by `prepare_sandbox_launch` at the asynchronous post-hydration seam, and no subprocess launch occurs in synchronous `create_session`. symbol: `ChatSessionMixin._create_chat_session_inner`. file: `src/gobby/servers/websocket/chat/_session.py`.
- 3.1.2 - The `web_chat_sandbox` default is `backend="srt"` with a bounded network policy that SRT preflight accepts. symbol: `DaemonConfig`. file: `src/gobby/config/app.py`.
- 3.1.3 - Sessions carrying a stale sandbox policy hash surface a mismatch rather than resuming under a changed boundary. symbol: `WebChatRuntimeManager.policy_mismatch_reason`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 3.1.4 - A CLI's own sandbox flag is pinned off when SRT is the enforcing boundary. file: `src/gobby/servers/websocket/chat/backends/codex.py`.
- 3.1.5 - A parameterized launch-contract matrix over the five incumbent providers pins exactly one SRT wrapper, bounded-network-policy representation, native-sandbox-off, provider-native usability, and stale-hash refusal per provider. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 3.1.6 - `web_chat_sandbox_policy_hash` covers the complete normalized effective policy with an explicit version, and changing the backend, a domain, a Git/package capability, or a socket allowance each produces a stale-resume refusal in tests. symbol: `web_chat_sandbox_policy_hash`. file: `src/gobby/agents/sandbox.py`.
- 3.1.7 - Codex and ACP subprocesses are session-owned: launched at the async post-hydration seam under the session's final project path and policy, torn down with the session, with failed-start cleanup, and tests covering first start, resume, failure, teardown, concurrent sessions, and two projects receiving distinct filesystem confinement under final worktree paths. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 3.1.8 - The Claude shim execs the SRT-wrapped argv, SDK-appended arguments pass through exactly one wrapper, and the shim is cleaned up on teardown, disconnect, and failed start. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 3.1.9 - `acp_client.py` remains below 1,000 lines, or its launch seam is decomposed in the same task. file: `src/gobby/adapters/acp_client.py`.
- 3.1.10 - `_session.py` remains below 1,000 lines, or the launch-seam work is decomposed in the same task. file: `src/gobby/servers/websocket/chat/_session.py`.

### 3.2 Add the AGY sandbox resolver [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox.py::*` — scope-reason: the new AgySandboxResolver class lands alongside the existing resolver hierarchy and get_sandbox_resolver gains the agy entry in the same multi-symbol edit
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: the module-level provider maps (_PROVIDER_DOMAINS, _PROVIDER_AUTH_PATHS, _PROVIDER_AUTH_READ_ONLY_PATHS, _PROVIDER_CREDENTIAL_ENV) gain agy entries together
- `src/gobby/agents/provider_capabilities.py::*` — scope-reason: the AGY row lands in the module-level PROVIDER_CAPABILITIES table that provider_supports_sandbox consults
- `tests/agents/test_sandbox.py::*` — scope-reason: reachability and capability-gate cases for the agy resolver join the sandbox suite

`SandboxResolver` has subclasses for Claude, Codex, Qwen and Grok but none for AGY, so the
provider-native path has nothing to resolve. Add `AgySandboxResolver` modeled on
`GrokSandboxResolver` (the smallest, at 12 lines), returning AGY's `--sandbox` flag for the
provider-native path, in the exact form recorded by 1.1.7 — never an unproven syntax. Under
SRT this resolver is not applied, per the nesting rule in 3.1.

A resolver class nobody can reach is dead code: `get_sandbox_resolver` (the closed factory
at `sandbox.py:547`) and the provider sandbox-capability gate must both learn the agy entry
in this task, or the provider-native path still raises for AGY with the class present.
That gate is concrete: `get_sandbox_resolver` refuses any provider for which
`provider_supports_sandbox` returns False, and that predicate reads the module-level
`PROVIDER_CAPABILITIES` table in `provider_capabilities.py`. The AGY row of that table
therefore lands **here**, not in 6.1 — otherwise acceptance 3.2.3 is unsatisfiable until a
downstream dependent completes. 6.1 consumes the completed capability row; spawn stays
gated meanwhile by `SPAWN_CAPABLE_PROVIDERS` and the `execute_spawn` rejection it removes.

The resolver alone does not make an AGY launch viable under SRT. `sandbox_policy.py`
supplies each provider's network domains (`_PROVIDER_DOMAINS`), credential and state roots
(`_PROVIDER_AUTH_PATHS`, `_PROVIDER_AUTH_READ_ONLY_PATHS`), and masked credential env vars
(`_PROVIDER_CREDENTIAL_ENV`) — and it has **no agy entries**, so an AGY launch would pass
wrapper preflight yet run with no upstream network access and no access to
`~/.gemini/antigravity-cli` credentials, state, or transcripts. Add the agy entries using
exactly the domains and read/write roots recorded by 1.1's network/state probe (acceptance
1.1.9) — never guessed values. The 5.2 launch-contract row proves them at the launch seam.

Check the projected line count: `sandbox.py` is 822 lines. If this pushes it toward 1,000,
decompose the resolvers into their own module within this task.

**Acceptance:**

- 3.2.1 - `AgySandboxResolver` exists and returns AGY's `--sandbox` for provider-native. symbol: `SandboxResolver`. file: `src/gobby/agents/sandbox.py`.
- 3.2.2 - `sandbox.py` remains below 1,000 lines, or the resolvers are decomposed. file: `src/gobby/agents/sandbox.py`.
- 3.2.3 - `get_sandbox_resolver("agy")` returns `AgySandboxResolver`, and the provider sandbox-capability gate admits agy, using the live-proven flag form. symbol: `get_sandbox_resolver`. file: `src/gobby/agents/sandbox.py`.
- 3.2.4 - `sandbox_policy.py` gains agy entries for provider domains, credential/state read and write roots, and credential env masking, using only probe-recorded values from 1.1.9. file: `src/gobby/agents/sandbox_policy.py`.
- 3.2.5 - The AGY `PROVIDER_CAPABILITIES` row lands in this deliverable, `provider_supports_sandbox("agy")` returns True, and reachability tests pin `get_sandbox_resolver("agy")` through the capability gate. test: `tests/agents/test_sandbox.py`.

## P4: AGY Hook and Transcript Layer
`kind: framing`

**Goal**: Make AGY sessions visible to Gobby — registered, parsed, and summarizable.

### 4.1 Correct the AGY hook contract to camelCase and synthesize SESSION_START [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/adapters/agy.py::AgyAdapter`
- `src/gobby/adapters/agy_contract.py::*` — scope-reason: the module-level hook contract tables (not indexed symbols) gain camelCase payload-key metadata
- `src/gobby/adapters/capabilities.py::_agy_capabilities`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `tests/adapters/test_agy.py::*` — scope-reason: existing AGY adapter tests gain the two-phase dispatch, injectSteps, and repeated-invocation cases
- `tests/hooks/test_pending_message_provider_contracts.py::*` — scope-reason: AGY pending-message delivery cases join the provider contract suite

Two defects. First, AGY sends **camelCase protojson** payloads — `conversationId`,
`transcriptPath`, `workspacePaths`, `stepIdx`, `toolCall` — but the adapter dual-reads
camelCase only for session id, hook name and tool name. `transcriptPath` is read nowhere in
`src/` or `crates/`, and the committed fixture uses snake_case pointing at a stale `.pb` path.

Second, AGY has **no `SessionStart` hook**, so `flow.py:337` — which reads `transcript_path`
only during session start — never runs. This, not the casing, is why AGY sessions have no
transcript. Follow the Codex precedent for the event shape: `codex_impl/app_server_adapter.py:110`
maps `thread/started` to `SESSION_START` and `:607` constructs the event with
`data["transcript_path"]` and `cwd` populated. Synthesize that event from `PreInvocation`,
mapping `conversationId` to session id, `transcriptPath` into `data["transcript_path"]`,
and `workspacePaths[0]` to `cwd` — but **never by remapping**. `PreInvocation` is AGY's
`BEFORE_AGENT` carrier: per-turn rules and `inject_context` dispatch on it, and a remap
that turns it into `SESSION_START` would suppress that path on every turn. Handling is
**two-phase**, and the executable seam is an `AgyAdapter.handle_native` override:
`translate_to_hook_event` returns one `HookEvent` and `BaseAdapter.handle_native`
(`adapters/base.py:193`) invokes `HookManager.handle` exactly once, so translation alone
cannot dispatch two events. The override processes the idempotent synthetic
`SESSION_START` side effect first, then processes the original `PreInvocation` as
`BEFORE_AGENT` exactly once, and returns *that* original event's translated response —
including `injectSteps` — back to AGY. The synthetic phase's response is **merged, never
discarded**: `handle_session_start` composes startup context and a system message and
marks the session `context_injected=True`, so dropping the synthetic `SESSION_START`
response would lose the startup context permanently — later events correctly see it as
already delivered and suppress it. The override merges the synthetic response's `context`
and `system_message` into the original `BEFORE_AGENT` response, preserves the original
event's decision, translates once to `injectSteps`, and marks startup context injected
only after successful emission. Each hook fires in a separate `ghook`
process, so the adapter has no process-local "first event" state to consult: it emits the
synthetic phase unconditionally, and idempotency lives at the session registration
boundary — `handle_session_start` keyed by provider plus `conversationId`. Repeated
synthetic `SESSION_START` events for one conversation must yield one canonical session,
one startup-context injection, and one transcript association, while every `PreInvocation`
still receives its own `BEFORE_AGENT` dispatch — first and repeated invocations are both
tested for both phases.

Line budget: `flow.py` is 966 lines and the registration-idempotency work lands in
`handle_session_start`. If it projects at or above 1,000, extract the idempotency keying
into a helper module within this task, per the `decompose-monolith` constraint.

Also correct `_agy_capabilities`: it declares `ContextChannel.NONE`, but 1.1.9's
`PreInvocation` and `PostInvocation` both accept `injectSteps` with `userMessage` and
`ephemeralMessage` payloads. Gobby's `inject_context` rule action currently cannot reach AGY
despite the CLI supporting it. Advertising the channel is not enough — the response side
must emit it. `AgyAdapter.translate_from_hook_response` currently emits only `decision`,
`reason` and `updatedInput`, so an injected payload would still be dropped. Map unified
`HookResponse` context into the live-proven `injectSteps` structure (`userMessage` and
`ephemeralMessage` payloads) on `PreInvocation` and `PostInvocation` responses, applying the
existing adapter context-truncation helper, with `injectSteps` as the explicit transport.

**Acceptance:**

- 4.1.1 - camelCase `transcriptPath`, `conversationId` and `workspacePaths` are read from AGY payloads. symbol: `AgyAdapter.translate_to_hook_event`. file: `src/gobby/adapters/agy.py`.
- 4.1.2 - An `AgyAdapter.handle_native` override dispatches the synthetic `SESSION_START` without process-local first-event state, dispatches the original `PreInvocation` as `BEFORE_AGENT` exactly once, and returns the original event's translated response with the synthetic phase's startup `context`/`system_message` merged into it — on first and repeated invocations. symbol: `AgyAdapter`. file: `src/gobby/adapters/agy.py`.
- 4.1.3 - AGY declares a context channel supporting `injectSteps` rather than `NONE`. symbol: `_agy_capabilities`. file: `src/gobby/adapters/capabilities.py`.
- 4.1.4 - The stale `tool_outcome` provenance stamp `agy.provider_contract_unproven` is replaced with live-proven outcomes. file: `src/gobby/adapters/agy.py`.
- 4.1.5 - Unified `HookResponse` context translates to `injectSteps` `userMessage`/`ephemeralMessage` on `PreInvocation` and `PostInvocation` responses. symbol: `AgyAdapter.translate_from_hook_response`. file: `src/gobby/adapters/agy.py`.
- 4.1.6 - An `inject_context` rule's payload reaches the emitted AGY hook response. test: `tests/adapters/test_agy.py`.
- 4.1.7 - Repeated `PreInvocation` events for one conversation yield one canonical session, one startup-context injection, and one transcript association. symbol: `handle_session_start`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 4.1.8 - A per-turn `BEFORE_AGENT` rule (including `inject_context`) fires on every `PreInvocation`, not only the first, with the synthetic `SESSION_START` phase active. test: `tests/adapters/test_agy.py`.
- 4.1.9 - `flow.py` remains below 1,000 lines, or the idempotency keying is decomposed in the same task. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 4.1.10 - Startup context reaches AGY exactly once: merged into the first `PreInvocation`'s `injectSteps` response, marked injected only after successful emission, and never re-delivered on repeated invocations. test: `tests/adapters/test_agy.py`.
- 4.1.11 - AGY pending-message delivery joins the provider contract suite. test: `tests/hooks/test_pending_message_provider_contracts.py`.

### 4.2 Add the AGY transcript parser [category: code] (depends: 2.1, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/agy.py`
- `src/gobby/sessions/transcripts/__init__.py::get_parser`
- `src/gobby/sessions/processor_transcripts.py::*` — scope-reason: the codex-only parser-state persistence gate admits agy
- `tests/sessions/test_transcript_parsers.py::*` — scope-reason: the frozen registry assertion TestParserRegistry.test_registry_has_correct_parsers gains the agy entry
- `tests/sessions/test_agy_transcript_parser.py`
- `tests/tasks/test_agy_validation_evidence.py`

Add `AgyTranscriptParser` in the new module `src/gobby/sessions/transcripts/agy.py`,
subclassing `BaseTranscriptParser`, registered in `PARSER_REGISTRY`, and modeled on the
droid parser (the newest). The frozen registry assertion in
`TestParserRegistry.test_registry_has_correct_parsers` gains the agy entry. The record
shapes are verified:

Common fields are `step_index`, `source`, `type`, `status`, `created_at`. Records carry
`content`, `tool_calls`, `thinking`, `truncated_fields`, `error`, or `exit_code`.

- `USER_EXPLICIT/USER_INPUT` → user message from `content`.
- `MODEL/PLANNER_RESPONSE` → assistant message from `content`, thinking from `thinking`
  (this type alone carries it), or tool calls from `tool_calls` as `{name, args}` elements.
  A record carries `content` or `tool_calls`, not both.
- **Any other `MODEL/*` type is a tool result.** Do not hardcode `RUN_COMMAND`. The observed
  set is `RUN_COMMAND`, `VIEW_FILE`, `MCP_TOOL`, `LIST_DIRECTORY`, `GREP_SEARCH`, `SEARCH_WEB`,
  `CODE_ACTION`, and `GENERIC` as the fallback for tools without a dedicated type. Keying on
  `RUN_COMMAND` alone drops ~78% of tool records. Treat unknown `MODEL/*` types as tool
  results rather than discarding them, so a future AGY tool type degrades gracefully.
- `SYSTEM/*` (`CONVERSATION_HISTORY`, `CHECKPOINT`, `SYSTEM_MESSAGE`, `EPHEMERAL_MESSAGE`,
  `ERROR_MESSAGE`) is bookkeeping — skip, but do not treat as malformed.

Tool calls pair to results by `step_index` order: a `PLANNER_RESPONSE` bearing `tool_calls`
is followed by its result record. AGY emits no tool-call ID, so derive a stable one from
conversation id plus `step_index`.

Handle `truncated_fields`: AGY self-truncates and names the affected fields, so a truncated
`tool_calls` may be structurally incomplete and must not raise. `status` is `DONE`, `ERROR`
or `RUNNING`; because the file is append-only with no rewrites, a `RUNNING` record is
permanently stale and marks an interrupted step.

Incremental correctness must survive daemon reconstruction. Parser-state sidecar
persistence is currently Codex-only — `processor_transcripts.py:220` snapshots
`parser.snapshot_state()` only when the parser source is `codex` — and the base snapshot
carries no call/result correlation. Because AGY pairs a `PLANNER_RESPONSE` bearing
`tool_calls` with a *later* result record by `step_index` order, a restart whose saved
cursor falls between the two would otherwise lose the pending tool-call ID. The gate
admits agy, and `AgyTranscriptParser` implements `snapshot_state`/`hydrate_state` carrying
the pending correlation, proven by a restart test that appends the result record after a
saved tool-call boundary.

Every parsing behavior above is pinned by a focused test module,
`tests/sessions/test_agy_transcript_parser.py`, built on scrubbed fixtures from 1.1 —
registry membership alone proves nothing about parsing.

Parsing is also not the finish line: open tasks **#18381** and **#18677** require AGY
command outcomes to flow through the unified validation-evidence pipeline with full
provider parity. Definitive AGY tool outcomes must travel native result →
`ParsedToolEvent` (`transcripts/base.py`) → normalized outcome → stored
`TranscriptEvidence` (`tasks/transcript_evidence.py`) → readiness → close-time context,
with success, failure, nonterminal, contradictory, unstructured, and provenance-free cases
each behaving per the fail-closed matrix the other five providers already satisfy. This
deliverable, together with 4.1's live-proven provenance and the V2 parity run, **supersedes
#18381 and #18677**; close both with a supersession reference to this plan when the
approved manifest is applied.

Parity includes recovery, not only isolated outcomes: the live-captured zero- and
nonzero-exit records from 1.1 (acceptance 1.1.10) are the payload fixtures, and a
sequential case must prove a definitive failure holds readiness fail-closed until a later
correlated definitive success restores readiness and close-time context.

**Acceptance:**

- 4.2.1 - `AgyTranscriptParser` is registered in `PARSER_REGISTRY`. symbol: `get_parser`. file: `src/gobby/sessions/transcripts/__init__.py`.
- 4.2.2 - All eight `MODEL/*` tool record types parse as tool results, including unknown types. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.3 - `thinking` on `PLANNER_RESPONSE` is parsed distinctly from `content`. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.4 - Records naming `truncated_fields` parse without raising. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.5 - Malformed lines and unknown record types are tolerated with stable ordering preserved. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.6 - Tool-call IDs are derived stably from conversation id plus `step_index`. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.7 - Incremental reads resume correctly on an append-only file. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.8 - Focused fixture-backed tests cover every record class, unknown `MODEL/*` tools, `truncated_fields`, malformed lines, stable tool-call IDs, interrupted `RUNNING` records, and append-only incremental reads. test: `tests/sessions/test_agy_transcript_parser.py`.
- 4.2.9 - Success, failure, nonterminal, contradictory, unstructured, and provenance-free AGY outcomes flow through `ParsedToolEvent`, stored `TranscriptEvidence`, readiness, and close-time context with the same fail-closed behavior as the five incumbent providers. test: `tests/tasks/test_agy_validation_evidence.py`.
- 4.2.10 - A sequential case proves a definitive AGY failure keeps readiness fail-closed and a later correlated definitive success restores readiness and close-time context, using the 1.1.10 live-captured payload shapes. test: `tests/tasks/test_agy_validation_evidence.py`.
- 4.2.11 - Parser state persists and rehydrates across daemon restart: the codex-only persistence gate admits agy, `AgyTranscriptParser.snapshot_state`/`hydrate_state` carry pending tool-call correlation, and a restart test appends the result record after a saved tool-call boundary. test: `tests/sessions/test_agy_transcript_parser.py`.

## P5: AGY Streaming Web Chat
`kind: framing`

**Goal**: A resumable streaming AGY web-chat backend on the subprocess protocol.

### 5.1 Add the AGY stream normalizer [category: code] (depends: P1, 2.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/backends/agy_stream.py`
- `tests/servers/websocket/chat/test_agy_stream.py`

Add the new module `src/gobby/servers/websocket/chat/backends/agy_stream.py` translating
AGY NDJSON into the shared `StreamEvent` vocabulary (`init`, `content_delta`, `result`,
`error`) defined in `src/gobby/adapters/acp_stream.py`, mirroring `parse_droid_stream_line`
in the sibling `droid_stream.py`. `acp_stream.py` itself is unchanged — the new module only
imports from it.

Verified record shapes: `{"event":"init","conversation_id":...,"init":{"cwd":...,"tools":[...],"permission_mode":...}}`;
`step_update` records with `conversation_id`, `step_index`, `state`, `step_type`; assistant
output as `step_type="agent_response"` with `text_delta`; tools as `step_type="tool"` with
`state` in `ACTIVE|DONE|ERROR`, `tool_name`, and `tool_info` containing `name`, `parameters`,
then `output` or a structured `error`; and a terminal `result` with `conversation_id`,
`status`, `response`, `num_turns`, duration and cumulative usage.

Two correctness requirements. **Do not emit assistant text twice** — `result.response`
repeats what `agent_response.text_delta` already streamed. And derive tool-call IDs from
conversation id plus `step_index`, since AGY emits none.

**Acceptance:**

- 5.1.1 - `init`, `step_update` and `result` records map onto shared `StreamEvent` types. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.2 - Assistant text is not duplicated between `text_delta` and `result.response`. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.3 - Tool `ACTIVE`/`DONE`/`ERROR` transitions produce correct call and result events. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.4 - Malformed lines and unknown record types are tolerated without terminating the turn. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.5 - Focused tests cover init, text-delta, result deduplication, tool `ACTIVE`/`DONE`/`ERROR` lifecycle, and malformed-line branches. test: `tests/servers/websocket/chat/test_agy_stream.py`.

### 5.2 Add the AGY web-chat backend [category: code] (depends: 5.1, 3.1, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/backends/agy.py`
- `tests/servers/websocket/chat/test_agy_backend.py`
- `tests/servers/websocket/chat/test_launch_contracts.py`

Add `AgyWebChatBackend` and `AgyManagedChatSession` in the new module
`src/gobby/servers/websocket/chat/backends/agy.py`, subclassing `ManagedChatSessionBase`
plus `ManagedWebChatPermissionsMixin` and satisfying `ChatSessionProtocol`. Model on
`DroidWebChatBackend`, the closest analogue.

One subprocess per turn:

```
agy --print "<prompt>" --output-format stream-json --disable-slash-commands --print-timeout 60s
```

Resume with `--conversation <agy-conversation-id>` — **contingent on 1.1.1 proving resume
works on 1.1.9**. If 1.1.1 records resume as unsupported, the Gate 0 branch rule in
Constraints applies: 5.2 is revised and re-reviewed before expansion, with resume
acceptance rewritten to the recorded contract — never implemented against an unproven
flag. Store the upstream AGY conversation id **separately** from Gobby's canonical
session and conversation identity; they are different namespaces and conflating them will
corrupt session resume. Persist it in the existing chat-session metadata used for session
reconstruction — no new store — so resume survives websocket reattachment and runtime
reconstruction, not only a continuously live managed session. Cancellation or a failed
result preserves the last confirmed usable id rather than clearing or overwriting it.

Required behaviors: streaming text, tool lifecycle events, error and non-zero exit handling,
cancellation, per-session locking via `ManagedChatSessionBase._lock`, `--model` and `--effort`
arguments, stderr redaction through a dedicated drain task, and the shared
`ACP_STREAM_READER_LIMIT_BYTES` reader limit from 2.4. Do **not** copy
Droid's unbounded `readline()`. Cancellation implements exactly the mechanism, cleanup and
resumability the 1.1.8 probe recorded — no invented interruption semantics.

The read timeout is the 2.4 contract, restated so no implementer invents a different one:
`DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS` (120 s) as a **per-line inactivity** clock on each
`readline()`, reset by every received line, with env override
`GOBBY_AGY_ACP_PROMPT_TIMEOUT_SECONDS`. On expiry: exactly one terminal error event for
the turn, termination of the owned subprocess tree, lock release, the last confirmed
usable conversation id preserved, and a session that remains reconstructable — never a
silent hang, a duplicate terminal error, or a locked orphaned session.

This backend also contributes the **AGY row to the 3.1 launch-contract matrix**: exactly one
SRT wrapper, the bounded network policy represented and accepted, AGY's native `--sandbox`
pinned off in the 1.1.7-recorded form when SRT enforces, and a stale policy hash refusing
resume.

**Acceptance:**

- 5.2.1 - A first turn spawns the documented argv and streams assistant text. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.2 - A subsequent turn resumes with `--conversation` carrying the upstream id. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.3 - The upstream AGY conversation id is persisted in existing chat-session metadata, distinct from Gobby's session identity. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.4 - A concurrent turn on a locked session is rejected. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.5 - Cancellation terminates the subprocess and releases the lock, using the mechanism and cleanup recorded by 1.1.8. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.6 - Model and effort selections reach the argv. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.7 - stderr is redacted and a non-zero exit surfaces as an error event. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.8 - A tool output above 64 KiB is read without `LimitOverrunError`. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.9 - After teardown and reconstruction of the managed session, the next turn's argv carries the same `--conversation` id. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.10 - Cancellation or a failed result preserves the last confirmed usable conversation id, matching the post-interrupt resumability recorded by 1.1.8. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.11 - The AGY row joins the launch-contract matrix: exactly one SRT wrapper, bounded-network-policy representation, native `--sandbox` off in the 1.1.7 form, stale-hash refusal, and the 3.2.4 sandbox-policy entries proven at the launch seam — credentials readable, state and transcript roots writable, probe-recorded domains granted, everything else refused. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 5.2.12 - Timeout expiry and reset follow the 2.4 inactivity contract: exactly one terminal error, owned process-tree cleanup, lock release, preserved confirmed conversation id, and a reconstructable session, with expiry and reset-on-activity tests. test: `tests/servers/websocket/chat/test_agy_backend.py`.

### 5.3 Integrate AGY into WebChatRuntimeManager [category: code] (depends: 5.2, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.create_session`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.health`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.start`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.stop`
- `tests/servers/websocket/chat/test_agy_backend.py`

Replace the two hardcoded AGY rejections — the `RuntimeError` in `create_session` and the
unavailable `ProviderBackendHealth` in `health` — with real startup health, session creation,
shutdown and provider status. AGY is already accepted as a valid provider slug in
`_message_ingress.py:25`, `_session.py:49` and `routes/sessions/core.py:209`, so these two
sites are the last gate. Gate availability on the immutable support record from 2.5 — the
synchronous health path reads the record and never re-probes.

**Acceptance:**

- 5.3.1 - `create_session(provider="agy")` returns a live session instead of raising. symbol: `WebChatRuntimeManager.create_session`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 5.3.2 - `health("agy")` reports real backend health gated on the 2.5 support record. symbol: `WebChatRuntimeManager.health`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 5.3.3 - An AGY session streams text and tool events, resumes, and interrupts over the websocket, with interrupt behavior matching the 1.1.8 record. test: `tests/servers/websocket/chat/test_agy_backend.py`.

## P6: AGY Spawn, Capabilities, and Catalog
`kind: framing`

**Goal**: Turn on the remaining AGY surfaces and retire the stale metadata.

### 6.1 Enable AGY terminal spawning [category: code] (depends: P4, 3.2, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: multi-symbol edit in which execute_spawn loses its AGY rejection and gains dispatch while the new _spawn_agy_terminal helper lands alongside the five existing spawners
- `src/gobby/agents/spawners/command_builder.py::build_cli_command`
- `src/gobby/mcp_proxy/tools/spawn_agent/_provider_resolution.py::*` — scope-reason: the AGY entry lands in the module-level SPAWN_CAPABLE_PROVIDERS frozenset, which is module data rather than an indexed symbol
- `tests/agents/test_spawn_executor.py::*` — scope-reason: existing spawn-executor tests gain the AGY spawner, cwd-remedy, linkage, and version-gate cases
- `tests/agents/spawners/test_command_builder.py::*` — scope-reason: AGY argv cases join the builder suite
- `tests/mcp_proxy/tools/spawn_agent/test_provider_resolution.py::*` — scope-reason: AGY provider-selection cases join the resolution suite

Remove the early-return rejection in `execute_spawn` and add `_spawn_agy_terminal` alongside
the five existing spawners. Add AGY to `SPAWN_CAPABLE_PROVIDERS`; the `PROVIDER_CAPABILITIES`
row landed in 3.2 and is consumed here.

Spawn is an executable entry point that bypasses the capability registry — explicit
provider selection, inherited provider, and agent-configured provider all reach
`execute_spawn` without touching 6.2's metadata gate. `execute_spawn` therefore reads the
immutable 2.5 support record **before any side effect** — sandbox preparation, session
creation, terminal allocation, process start. Sub-floor, absent-binary, unparseable, and
pre-publication records refuse the spawn with the record's actionable upgrade message.

Pass `--dangerously-skip-permissions` under SRT, matching every other provider, and pin AGY's
own `--sandbox` off when SRT is enforcing, per the nesting precedent — both flags in the
exact forms recorded by 1.1.7. Wire project cwd,
parent/child session linkage via `ChildSessionConfig`, workflow variables through
`SessionVariableManager.merge_variables`, and the terminal env vars from
`get_terminal_env_vars`.

Apply the cwd remedy chosen in 1.1.3. If tool calls without an explicit `Cwd` still execute
under AGY's application-data directory, do **not** claim project isolation — surface the
limitation instead.

Check the projected line count: `spawn_executor.py` is 746 lines and each existing spawner is
roughly 100. If it projects at or above 1,000, load the `decompose-monolith` skill and
decompose within this task.

**Acceptance:**

- 6.1.1 - The AGY spawn rejection is removed and `_spawn_agy_terminal` exists. symbol: `execute_spawn`. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.2 - `build_cli_command` produces AGY argv. symbol: `build_cli_command`. file: `src/gobby/agents/spawners/command_builder.py`.
- 6.1.3 - AGY's own `--sandbox` is pinned off when SRT enforces. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.4 - A spawned AGY agent receives the intended project cwd, and a regression test pins tool-call cwd behavior. test: `tests/agents/test_spawn_executor.py`.
- 6.1.5 - Parent/child linkage and workflow variables are wired. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.6 - `spawn_executor.py` remains below 1,000 lines, or is decomposed via the `decompose-monolith` skill in the same task. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.7 - `execute_spawn` reads the 2.5 support record before any side effect; sub-floor, absent-binary, unparseable, and pre-publication records refuse the spawn with the actionable upgrade message, across explicit, inherited, agent-configured, and default provider selection. test: `tests/mcp_proxy/tools/spawn_agent/test_provider_resolution.py`.
- 6.1.8 - AGY argv construction is pinned in the builder suite, including the 1.1.7-recorded flag forms. test: `tests/agents/spawners/test_command_builder.py`.

### 6.2 Gate AGY capabilities on version 1.1.9 [category: code] (depends: 6.1, 5.3, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/ai/registry_builder.py::_agy_unavailable_bindings`
- `src/gobby/ai/registry_builder.py::_tool_chat_adapter_style`
- `src/gobby/ai/registry_builder.py::_tool_chat_binding`
- `src/gobby/ai/_tool_chat_builder.py::_daemon_tool_chat_adapter_factories`
- `src/gobby/ai/_tool_chat_service.py::ToolChatService`
- `src/gobby/ai/_tool_chat_agy.py`
- `src/gobby/providers/registry.py::*` — scope-reason: the module-level provider table entry for AGY and the AGY_UNAVAILABLE_REASON constant are module data rather than indexed symbols
- `tests/ai/test_agy_tool_chat_contract.py`

Flip `ProviderMetadata("agy")` to `supports_web_chat=True`, `supports_agent_spawn=True`,
`live_model_discovery=True` and drop `AGY_UNAVAILABLE_REASON` from it. Replace
`_agy_unavailable_bindings` with real `WEB_CHAT` and `AGENT_SPAWN` bindings, and add a
`TOOL_CHAT` binding — but **not** through the current factory map.
`_daemon_tool_chat_adapter_factories` resolves `AIAdapterStyle.CLI` globally to
`DroidSpawnToolChatAdapter` (`_tool_chat_builder.py:70`), so binding AGY as bare CLI style
would hand AGY prompts to Droid's command and JSON-RPC protocol. Make the CLI-style factory
provider-aware and add a dedicated `AgyToolChatAdapter` in the new module
`src/gobby/ai/_tool_chat_agy.py`, speaking the 5.1 stream-json transport and implementing
the full ToolRuntime contract: controlled tools, `ToolLoopLimits`, timeouts, cancellation,
and normalized results. If AGY's print mode cannot provide the controlled-tool bridge, the
`TOOL_CHAT` binding stays unavailable with that narrow reason — never a Droid-routed
binding.

The factory map is not the whole seam. `ToolChatService._adapter_for_style` caches
constructed adapters keyed solely by `AIAdapterStyle` from zero-argument factories
(`_tool_chat_service.py:240-252`), so a provider-aware factory alone still cross-routes on
first use: whichever CLI-style adapter is constructed first — Droid's or AGY's — is
returned for the other provider from then on. Adapter selection and the cache become
provider-aware: the resolved `CapabilityBinding` flows into selection and the cache is
keyed by `(adapter_style, provider)` — with both first-use orders proven in one service
instance.

Gate all of it on the immutable support record from 2.5. AGY is the **first version-gated
provider CLI**, so this establishes the pattern: below the floor, capabilities stay
unavailable with the record's upgrade message naming the installed and required versions.
Registry build reads the record; it never probes.

`VISION_EXTRACT` follows 1.1.4: enable only if image input was live-proven, otherwise keep it
unavailable with a narrow, truthful reason — not the current blanket "no documented machine
transport" text.

Advertisement alone is unverifiable, so one executable contract test proves the wiring:
resolve the `TOOL_CHAT` binding for a supported version and drive a scrubbed fake AGY
subprocess through init, text delta, tool lifecycle and result records, asserting prompt,
model and effort argv propagation, normalized output, non-zero-exit handling, and that
versions below 1.1.9 never advertise the binding.

**Acceptance:**

- 6.2.1 - Installed AGY 1.1.9 advertises web chat and agent spawn; tool chat is advertised when the controlled-tool bridge is proven, and otherwise stays unavailable with that narrow reason — never Droid-routed. symbol: `_agy_unavailable_bindings`. file: `src/gobby/ai/registry_builder.py`.
- 6.2.2 - AGY below 1.1.9 stays unavailable with a message naming installed and required versions. symbol: `ProviderMetadata`. file: `src/gobby/providers/registry.py`.
- 6.2.3 - `AGY_UNAVAILABLE_REASON` no longer gates a capable installation. file: `src/gobby/providers/registry.py`.
- 6.2.4 - `VISION_EXTRACT` state matches the 1.1.4 finding with a narrow reason. symbol: `_agy_unavailable_bindings`. file: `src/gobby/ai/registry_builder.py`.
- 6.2.5 - A registry-to-transport contract test drives the `TOOL_CHAT` binding end-to-end against a fake AGY subprocess, and sub-1.1.9 never advertises it. test: `tests/ai/test_agy_tool_chat_contract.py`.
- 6.2.6 - The AGY `TOOL_CHAT` binding resolves to `AgyToolChatAdapter` — never `DroidSpawnToolChatAdapter` — with controlled tools, `ToolLoopLimits`, timeouts and cancellation enforced. symbol: `_daemon_tool_chat_adapter_factories`. file: `src/gobby/ai/_tool_chat_builder.py`.
- 6.2.7 - Adapter selection and caching are provider-aware: the cache is keyed by adapter style plus provider, and both first-use orders — Droid then AGY, and AGY then Droid — resolve the correct adapter in one service instance. symbol: `ToolChatService`. file: `src/gobby/ai/_tool_chat_service.py`.

### 6.3 Move the AGY model catalog to live discovery [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/provider_model_defaults.py::*` — scope-reason: the static AGY catalog table is module-level data rewritten wholesale to the 1.1.9 model set
- `src/gobby/ai/_agy_models.py::*` — scope-reason: catalog-wide refresh to the 1.1.9 model set touches the effort maps, defaults, and alias tables together
- `src/gobby/servers/provider_models.py::ProviderModelCatalog`
- `src/gobby/servers/provider_models.py::_static_provider_models`
- `src/gobby/servers/provider_model_discovery.py::*` — scope-reason: AGY live-discovery parsing joins the module's per-provider discovery seams, which currently encode AGY as static-only
- `src/gobby/ai/registry_builder.py::_tool_chat_adapter_style`
- `src/gobby/ai/registry_builder.py::_agy_unavailable_bindings`
- `tests/ai/test_capability_registry.py::test_daemon_registry_reports_text_generate_provider_bindings`
- `tests/servers/test_provider_models.py::*` — scope-reason: the drift test is re-pointed from version-pinned skipping to installed-binary exercise and gains live-discovery fallback coverage
- `tests/servers/routes/test_providers.py::*` — scope-reason: provider-route tests gain the AGY static-to-live source and availability transition cases
- `tests/ai/test_text_generation.py::*` — scope-reason: default-effort normalization cases pin the fixture-recorded AGY efforts through the text-generation consumer
- `tests/fixtures/provider_contracts/agy/agy_models_v1.0.10.txt`
- `tests/fixtures/provider_contracts/agy/agy_models_v1.1.9.txt`

The catalog is pinned to `agy-1.0.10-static` across eight sites (two in
`registry_builder.py`, five in `provider_model_defaults.py`, one in
`test_capability_registry.py`), and the live drift test skips unless the installed binary
reports **exactly** `1.0.10` — so drift is structurally invisible to CI. Live 1.1.9
`agy models` returns 11 display strings versus the 8 currently encoded, and
`gemini-3.6-flash-{high,medium,low}` is entirely new while `claude-opus-4-6-thinking`
changed shape.

Parse `agy models` at catalog refresh with a static fallback refreshed to 1.1.9 for when the
binary is absent or the call fails. The parsing itself lands in
`provider_model_discovery.py`, whose per-provider seams currently encode AGY as
static-only. Retire the `agy-1.0.10-static` label at all eight sites,
supersede the version-pinned fixture with `agy_models_v1.1.9.txt`, and re-point the drift
test so it exercises the installed binary rather than skipping. The static→live transition
is pinned by focused consumer tests, not just data: supported live discovery, sub-floor
fallback to static, command-failure/cache fallback, and the source-label and availability
transitions visible through the provider routes.

The cache is a migration hazard, not just a fallback. `ProviderModelCatalog.refresh`
falls back to the last-good cache on discovery failure and `load_cache` accepts old cache
versions without any provider-version compatibility check — so an existing 1.0.10-era AGY
cache entry survives the 1.1.9 migration and overrides the refreshed static catalog on the
first failed live discovery. Define AGY cache compatibility against the immutable 2.5
support record: invalidate cached AGY entries whose recorded source is retired
(`agy-1.0.10-static`) or whose catalog predates the 1.1.9 floor, and reuse a cached entry
only when it is compatible with the installed support record.

Sub-floor detection reads the immutable 2.5 support record — the catalog never probes the
version itself.

Also reconcile the `GEMINI_FAMILY_MODELS` vs `AGY_MODELS` default-effort mismatch for
`gemini-3.5-flash` (`medium` vs `low`). The canonical default is the one the live 1.1.9
binary reports, recorded in the `agy_models_v1.1.9.txt` fixture; if the binary reports no
default, the named default is `medium`, matching the gemini family table. #19483 tracks
the broader default-effort audit across providers; this plan closes only the
`gemini-3.5-flash` mismatch.

**Acceptance:**

- 6.3.1 - `agy models` output is parsed at catalog refresh with a static fallback. symbol: `ProviderModelCatalog`. file: `src/gobby/servers/provider_models.py`.
- 6.3.2 - The `agy-1.0.10-static` label is removed from all eight sites across `registry_builder.py`, `provider_model_defaults.py`, and `test_capability_registry.py`, with a repository-wide absence assertion for the retired label. file: `src/gobby/servers/provider_model_defaults.py`.
- 6.3.3 - The model fixture reflects 1.1.9's 11 display strings. file: `tests/fixtures/provider_contracts/agy/agy_models_v1.1.9.txt`.
- 6.3.4 - The drift test exercises the installed binary instead of skipping off-version. test: `tests/servers/test_provider_models.py`.
- 6.3.5 - Focused tests cover supported live discovery, sub-floor static fallback, command-failure/cache fallback, and the source/availability transitions through the provider routes. test: `tests/servers/routes/test_providers.py`.
- 6.3.6 - `GEMINI_FAMILY_MODELS` and `AGY_MODELS` agree on the canonical `gemini-3.5-flash` default effort, with a parity test pinning both consumers to the fixture-recorded value. test: `tests/servers/test_provider_models.py`.
- 6.3.7 - Cached AGY entries with a retired source label or a pre-1.1.9 catalog are invalidated at load and refresh; a cached entry is reused only when compatible with the 2.5 support record, proven by a migration test loading an old-shape cache before a failed live refresh. test: `tests/servers/test_provider_models.py`.
- 6.3.8 - Default-effort normalization through the text-generation consumer matches the fixture-recorded AGY efforts. test: `tests/ai/test_text_generation.py`.

## P7: Documentation
`kind: framing`

**Goal**: Correct the record only after the gates pass.

### 7.1 Update the CLI integration matrix and AGY docs [category: docs] (depends: P5, P6)
`kind: deliverable`

Targets:
- `docs/research/cli-integration-matrix.md`
- `docs/research/cli-integration-matrix-claude-code.md`

Move AGY from **Blocked** to **FULL** — only after every preceding gate passes. Rewrite "the
agy trap" and "the agy lesson" sections: the claim that "upstream must add transcripts + ACP"
is now wrong on both counts. AGY persists parseable JSONL transcripts and exposes a
stream-json subprocess transport; ACP was never required. Correct the companion matrix, which
records transcripts as "binary protobuf, no parser" — that described 1.0.11 and is stale.

The matrix row is three readiness surfaces plus a status column, and each cell is named
explicitly: Hook, Transcript, Web-chat, and Status. Every cell reflects a Gate 0-proven
surface; a surface deferred under the Constraints branch rule keeps a truthful status
instead of FULL.

The wiki concept page (`wiki/knowledge/concepts/agy.md`) is gitignored generated output
(`/wiki/` in `.gitignore`) — it is not a plan target. It regenerates from the corrected
durable docs via the wiki pipeline after this deliverable lands; no direct edit is made
or committed.

**Acceptance:**

- 7.1.1 - The AGY row reads Hook=Full, Transcript=JSONL, Web-chat=custom stream-json, and Status=FULL — the three readiness surfaces plus the status column, each cell named. file: `docs/research/cli-integration-matrix.md`.
- 7.1.2 - The "upstream must add transcripts + ACP" framing is corrected. behavior: "AGY exposes JSONL transcripts and a stream-json transport" in `docs/research/cli-integration-matrix.md`.
- 7.1.3 - The stale binary-protobuf transcript claim is corrected. file: `docs/research/cli-integration-matrix-claude-code.md`.

## V2 End-to-End Verification
`kind: verification`

End-to-end acceptance for the epic:

- Focused pytest runs, each prefixed `GOBBY_TEST_PROTECT=1`, over the AGY parser, stream
  normalizer, backend, spawn, capability registry and provider routes. **Never the full suite.**
- Scoped `uv run ruff check` and `uv run mypy` over every touched path.
- `uv run gobby test-types audit` against the baseline where test types changed.
- `cargo test` for the `ghook` contract changes in 2.3.
- A session integration test proving hook transcript registration, parsing, summary/digest
  eligibility and context tracking for an AGY session.
- Route and websocket tests proving an AGY session creates, streams text and tool events,
  resumes, and interrupts — interrupt behavior matching the 1.1.8 cancellation record.
- Validation-evidence provider parity for AGY (4.2.9): the six-outcome case matrix through
  `ParsedToolEvent`, stored `TranscriptEvidence`, readiness, and close-time context, at
  parity with the five incumbent providers — the run that discharges superseded tasks
  #18381 and #18677.
- Cross-provider regression proving the P2 and P3 refactors did not change Claude, Codex,
  Grok, Qwen or Droid behavior — this is the main risk the consistency work carries. The
  3.1 launch-contract matrix over the five incumbents, extended by the 5.2 AGY row, is the
  concrete anchor at the launch seam; parser-dispatch and discovery regressions are pinned
  by the existing provider parser and session-start suites.
- Rebuild and **reinstall** `~/.gobby/bin/ghook` after 2.3; a committed Rust change is not
  live until the binary is reinstalled.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: f5a39a5f-a33d-44bd-9223-d16a2c354cff
- enhancer_session: 152c20fb-ee47-43c1-9654-cc2b386d7a9b
- converged: false
- suggestions_presented: 6
- accepted:
  - E1 / better / AGY-native injectSteps response translation in 4.1 with an inject_context test
  - E2 / better / Gate 0 launch-security probe for --sandbox and --dangerously-skip-permissions, consumed by 3.2 and 6.1
  - E3 / better / idempotent synthetic SESSION_START at the registration boundary keyed by provider plus conversationId
  - E4 / better / parameterized cross-provider launch-contract test matrix pinning the SRT launch seam
  - E5 / better / executable registry-to-transport TOOL_CHAT contract test with sub-1.1.9 non-advertisement
  - E6 / better / upstream AGY conversation id persisted in existing chat-session metadata with reconstruction acceptance
- declined: none
- resolution_notes: All six suggestions accepted by the user and folded in. 1.1 gained
  probe question 5 and acceptance 1.1.7; 3.2 and 6.1 now consume the recorded flag forms.
  4.1 gained response-side injectSteps translation (4.1.5, 4.1.6), the statelessness
  rewording of 4.1.2, the handle_session_start idempotency target, and 4.1.7. 3.1 gained
  the launch-contract matrix (3.1.5) with the V2 cross-provider item re-anchored to it.
  6.2 gained the executable TOOL_CHAT contract test (6.2.5). 5.2 gained persistence-based
  resume (5.2.3 reworded, 5.2.9, 5.2.10).

**Round 2** `kind: verification`

- reviewer_run: 43c0dca7-e6ce-4279-8d3c-4bfbf5619f3f
- reviewer_session: d1a7f8b5-dbe3-4d43-beb9-4c1186e7b984
- verdict: needs_review
- findings:
- agy-r2-srt-network-policy / blocking / SRT default with allow_network=True is rejected by prepare_sandbox_launch preflight
- agy-r2-policy-hash-completeness / blocking / web-chat policy hash omits backend and network fields, allowing silent boundary-change resume
- agy-r2-workspace-process-ownership / blocking / warm daemon-shared Codex/ACP subprocesses cannot give per-session SRT confinement
- agy-r2-claude-executable-seam / blocking / cli_path takes one executable while SandboxLaunch yields argv; a shim is required
- agy-r2-preinvocation-dual-dispatch / blocking / remapping PreInvocation to SESSION_START suppresses its native BEFORE_AGENT dispatch
- agy-r2-agy-tool-chat-adapter / blocking / AIAdapterStyle.CLI resolves globally to DroidSpawnToolChatAdapter, misrouting AGY
- agy-r2-agy-matrix-sequencing / blocking / 3.1's AGY matrix row required the downstream 5.2 backend out of order
- agy-r2-version-gate-boundary / blocking / 5.3-6.2 gate cycle plus async get_cli_version vs sync registry construction
- agy-r2-resolver-reachability / blocking / AgySandboxResolver without get_sandbox_resolver wiring stays unreachable
- agy-r2-spawner-target-scope / blocking / _spawn_agy_terminal fell outside 6.1's declared Targets
- agy-r2-validation-evidence-parity / blocking / governing tasks require AGY outcomes through validation evidence, readiness, close-time parity
- agy-r2-live-cancellation-contract / blocking / Gate 0 lacked a live cancellation/interrupt probe consumed by 5.2/5.3
- agy-r2-critical-hook-policy / blocking / 2.3 never named the final critical-hook set per provider
- agy-r2-parser-consumer-acceptance / nit / 2.1.2 claimed three consumers but cited one file
- agy-r2-source-ceiling-inventory / blocking / acp_client.py (955) and flow.py (966) lacked line-budget gates
- agy-r2-parser-test-seam / blocking / 4.2 pinned registry membership but no parser behavior tests
- agy-r2-model-consumer-inventory / blocking / 6.3 omitted the discovery seam and static-to-live consumer tests
- resolution_notes: All 17 findings accepted by the user and repaired in place. 1.1 gained
  the cancellation probe (question 6, 1.1.8). 2.1 split consumer acceptance (2.1.2 reworded,
  2.1.4, 2.1.5). 2.3 gained the six-provider final critical-hook matrix (2.3.4, 2.3.5). New
  deliverable 2.5 breaks the version-gate cycle with an async startup probe publishing an
  immutable support record; 5.3 and 6.2 now depend on it. 3.1 was reworked: bounded network
  default, complete versioned policy hash (3.1.6), session-owned Codex/ACP processes per
  user direction (3.1.7), daemon-emitted Claude shim per user direction (3.1.8), matrix
  limited to the five incumbents with the AGY row moved to 5.2 (5.2.11), and acp_client.py
  line gate (3.1.9). 3.2 wired get_sandbox_resolver (3.2.3). 4.1 became two-phase dispatch
  preserving BEFORE_AGENT (4.1.2 reworded, 4.1.8) with the flow.py line gate (4.1.9). 4.2
  gained the focused parser test module (4.2.8) and validation-evidence parity superseding
  tasks #18381/#18677 (4.2.9), to be closed with a supersession reference at manifest
  application. 6.1 widened to a justified wildcard target per user direction. 6.2 gained
  the dedicated AgyToolChatAdapter with provider-aware CLI dispatch (6.2.6) per user
  direction. 6.3 gained the discovery seam and consumer-transition tests (6.3.5).
  Constraints now budget all four near-ceiling files via the decompose-monolith skill.

```json plan-review-round
{"evidence_id":"8f25e215-0c04-4ce8-84ea-b3513510618d","plan_hash":"1a515b37e0589a348b7044dc58f4b5bdede5039fb778878179559c45ba30016e","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1df93d2f4b653145e69445bb0a1df84218fc3b406b5945659deea9e36bde54e8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":17,"total":23},"evidence_id":"8f25e215-0c04-4ce8-84ea-b3513510618d","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"0f22da58ce27d154cbbbf353676377a976dc21b8176107269e7aa5b80df66b19","status":"valid"},"source_digest":"a45834b3c15e742dde4d116f54f6e0328bb6982098e263b28ed62189bdfa5b0f","version":1},"findings":[{"category":"unhandled-edge","check_key":"srt-unrestricted-network-policy","description":"The planned default makes every enabled web-chat SRT launch fail before a provider starts; acceptance simultaneously requires the rejected flag to remain represented.","finding_id":"agy-r2-srt-network-policy","fix":"Define a bounded default using allow_network=False plus explicit provider/API domains or scoped capabilities. If unrestricted networking is required, add a separate SRT contract change and update the existing fail-closed test before adopting the default.","location":"P3 / §3.1","prevention":"Cross-check each proposed default configuration against runtime preflight branches and focused contract tests.","principle":"A planned default must satisfy every runtime precondition of the backend it enables.","root_cause":"Section 3.1 flips web chat to SRT while retaining allow_network=True, a combination prepare_sandbox_launch rejects before launch.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"sandbox-policy-hash-completeness","description":"A provider-native session can retain the same stored hash after switching to SRT, and other permission changes can also resume silently under a different boundary.","finding_id":"agy-r2-policy-hash-completeness","fix":"Add web_chat_sandbox_policy_hash to Targets; hash the complete normalized effective policy with an explicit version, and add stale-resume tests for backend, domains, Git/package capabilities, and socket allowances.","location":"P3 / §3.1","prevention":"Enumerate every security-relevant policy field and verify that changing each field changes the persisted hash.","principle":"Resume identity must cover the complete effective security boundary.","root_cause":"The current web-chat hash omits backend and material network/socket fields, while 3.1 relies on that hash to reject stale sessions after the SRT migration.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"workspace-scoped-process-ownership","description":"Wrapping current startup cannot provide distinct filesystem confinement for concurrent projects, and create_session is downstream of the actual shared-process launch.","finding_id":"agy-r2-workspace-process-ownership","fix":"Re-plan Codex and ACP as session-owned or workspace-and-policy-keyed process pools. Add lifecycle, teardown, concurrent-session, and two-project isolation acceptance before claiming per-session SRT.","location":"P3 / §3.1","prevention":"For every sandboxed process, record launch owner, workspace/policy key, sharing rule, and teardown boundary.","principle":"A process-scoped sandbox policy must be resolved before process launch from all workspaces that process can serve.","root_cause":"Codex and ACP currently start warm daemon-shared subprocesses before a session project path is known, while SRT preparation requires that path.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"claude-srt-executable-seam","description":"The proposed cli_path assignment cannot preserve the SRT runner, settings, violation path, provider executable, and SDK-appended Claude arguments.","finding_id":"agy-r2-claude-executable-seam","fix":"Specify a private executable shim API that execs SandboxLaunch.wrap([claude, \"$@\"]) and define cleanup on disconnect and failed start. Add an argv contract test proving SDK-appended arguments pass through exactly one wrapper.","location":"P3 / §3.1","prevention":"Validate every wrapper against the consumer API's concrete input type and argument-appending behavior.","principle":"An integration plan must bridge the exact interface types at each launch boundary.","root_cause":"ClaudeAgentOptions.cli_path accepts one executable path, while SandboxLaunch exposes a wrapped argv vector and produces no executable launcher.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"native-hook-dual-dispatch","description":"The plan would suppress per-turn before-agent rules and inject_context behavior while trying to register the session.","finding_id":"agy-r2-preinvocation-dual-dispatch","fix":"Define two-phase handling: process an idempotent synthetic SESSION_START side effect, then process the original PreInvocation as BEFORE_AGENT exactly once and translate that response to injectSteps. Test first and repeated invocations.","location":"P4 / §4.1","prevention":"For every synthesized event, test both the side effect and continued execution of the original event path on first and repeated invocations.","principle":"Synthesizing lifecycle work from a native hook must preserve the native hook's original semantic dispatch.","root_cause":"The AGY adapter and HookManager process one HookEvent; remapping PreInvocation to SESSION_START replaces its existing BEFORE_AGENT path.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"provider-specific-tool-chat-adapter","description":"Advertising AGY as CLI tool chat routes it through Droid's command and JSON-RPC protocol; the AGY stream normalizer alone does not implement ToolRuntime policy or loop limits.","finding_id":"agy-r2-agy-tool-chat-adapter","fix":"Expand Targets to provider-aware CLI dispatch and a dedicated AGY tool-chat adapter covering controlled tools, ToolLoopLimits, timeouts, cancellation, and normalized results. Keep TOOL_CHAT unavailable if AGY cannot provide the controlled-tool bridge.","location":"P6 / §6.2","prevention":"Trace every new binding through service selection, factory lookup, concrete adapter, executable, wire protocol, cancellation, and tool-policy enforcement.","principle":"A capability binding must route to an adapter that implements the bound provider's executable and protocol.","root_cause":"Tool-chat factories are keyed by adapter style, and AIAdapterStyle.CLI currently resolves globally to DroidSpawnToolChatAdapter.","section_id":"6.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"consumer-before-producer-dependency","description":"Section 3.1 cannot complete its AGY matrix acceptance without implementing section 5.2 out of order.","finding_id":"agy-r2-agy-matrix-sequencing","fix":"Limit 3.1 to Claude, Codex, Droid, Grok, and Qwen. Add the AGY row to 5.2 with SRT, native-sandbox-off, permission-flag, network-policy, and stale-hash assertions.","location":"P3 / §3.1","prevention":"For each acceptance matrix row, verify its implementation target exists in the deliverable or in a completed dependency.","principle":"A deliverable cannot require acceptance against an artifact created only by a downstream dependent deliverable.","root_cause":"Section 3.1 requires an AGY launch-contract row, while 5.2 creates AgyWebChatBackend and depends on 3.1.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"async-version-gate-boundary","description":"The plan contains a semantic dependency cycle and no executable seam for resolving the AGY version once across runtime health and capability bindings.","finding_id":"agy-r2-version-gate-boundary","fix":"Create an earlier version-gate foundation after P1. Specify an async startup probe that injects an immutable support record into synchronous runtime/registry consumers, or enumerate the full async builder migration. Make 5.3 and 6.2 depend on it.","location":"P6 / §6.2","prevention":"Trace prerequisite direction and sync/async types through every consumer before fixing phase dependencies.","principle":"A shared prerequisite must precede every consumer and expose a callable interface compatible with those consumers.","root_cause":"Section 5.3 consumes the 6.2 version gate while 6.2 depends on 5.3; get_cli_version is async while registry construction is sync.","section_id":"6.2","severity":"blocking"},{"category":"traceability","check_key":"resolver-factory-reachability","description":"The provider-native path can still raise for AGY even after the resolver class exists.","finding_id":"agy-r2-resolver-reachability","fix":"Target get_sandbox_resolver and the AGY sandbox capability entry in 3.2; add acceptance that get_sandbox_resolver(\"agy\") returns AgySandboxResolver using the live-proven flag.","location":"P3 / §3.2","prevention":"Sweep constructor tables, registries, capability predicates, fakes, and exhaustive dispatch for each new implementation.","principle":"Adding an implementation must also update every closed registry or factory that makes it reachable.","root_cause":"Section 3.2 adds AgySandboxResolver while omitting get_sandbox_resolver and the provider sandbox-capability gate.","section_id":"3.2","severity":"blocking"},{"category":"gobby-format","check_key":"new-symbol-target-coverage","description":"The helper that carries most AGY spawn behavior is outside the declared exact-symbol target scope.","finding_id":"agy-r2-spawner-target-scope","fix":"Use a justified spawn_executor.py::* target for the multi-symbol edit, or place the AGY spawner in a new focused module and target that module explicitly.","location":"P6 / §6.1","prevention":"Compare every planned new or changed symbol with the Targets block before review completion.","principle":"Every changed production scope must be represented by a concrete, valid Target.","root_cause":"Section 6.1 adds _spawn_agy_terminal inside an existing symbol-bearing file but targets only execute_spawn.","section_id":"6.1","severity":"blocking"},{"category":"missing-requirement","check_key":"validation-evidence-provider-parity","description":"Definitive AGY outcomes can parse without proving the validation and close-time behavior required by the governing tasks.","finding_id":"agy-r2-validation-evidence-parity","fix":"Add 4.1/4.2 acceptance and V2 tests for success, failure, nonterminal, contradictory, unstructured, and provenance-free AGY outcomes through ParsedToolEvent, TranscriptEvidence, readiness, and close-time context.","location":"P4 / §4.2","prevention":"Trace each governing task criterion through normalization, storage, readiness, close evaluation, and parity tests.","principle":"A provider integration must satisfy its governing open requirements through every downstream evidence consumer.","root_cause":"Open tasks #18381 and #18677 require native result through stored validation evidence, readiness, and close-time parity; the plan stops at adapter provenance and transcript parsing.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"live-cancellation-contract","description":"The backend must invent interruption semantics without live evidence about partial output, child cleanup, exit behavior, or whether the last conversation id remains resumable.","finding_id":"agy-r2-live-cancellation-contract","fix":"Add a live active-turn cancellation probe recording the signal/API, process-tree exit, partial stream outcome, orphan cleanup, and resumability. Make 5.2 cancellation and 5.3 websocket interrupt acceptance consume it.","location":"P1 / §1.1","prevention":"Map every downstream live-provider assumption back to a recorded Gate 0 probe and acceptance item.","principle":"A contract gate must probe every provider behavior later treated as a correctness requirement.","root_cause":"Gate 0 omits cancellation and interrupt behavior while 5.2 and 5.3 require process termination, cleanup, and resumability.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"critical-hook-policy-matrix","description":"AGY removal, Droid short-circuit removal, and documentation can all pass while Claude, Qwen, Codex, Grok, and Droid remain inconsistent or Droid retains an empty declaration.","finding_id":"agy-r2-critical-hook-policy","fix":"State the exact final set for all six providers and add cli_config plus contract.rs assertions for each provider's critical lifecycle hooks and noncritical failure behavior.","location":"P2 / §2.3","prevention":"Record an explicit before/after provider matrix and test every row under daemon-down lifecycle and noncritical events.","principle":"A normalization task must state the canonical state it converges every variant toward.","root_cause":"Section 2.3 describes session-lifecycle-only policy but never names the final critical-hook set for each provider.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"parser-consumer-acceptance-parity","description":"summary_context.py and cli/tokens.py can escape a bounded criteria review despite being explicit targets.","finding_id":"agy-r2-parser-consumer-acceptance","fix":"Split 2.1.2 into three acceptance items, or cite transcript_processing.py, summary_context.py, and cli/tokens.py together with unknown-source tests.","location":"P2 / §2.1","prevention":"For multi-consumer acceptance, use one item per consumer or attach an artifact reference for each.","principle":"Acceptance evidence should name every consumer claimed by the criterion.","root_cause":"Acceptance 2.1.2 claims three inline chains but cites only transcript_processing.py.","section_id":"2.1","severity":"nit"},{"category":"gobby-format","check_key":"source-ceiling-all-targets","description":"The ACP launch and AGY idempotency work have only 44 and 33 lines of headroom, so the plan can violate the enforced ceiling during ordinary implementation.","finding_id":"agy-r2-source-ceiling-inventory","fix":"Add projected line-count gates for acp_client.py and flow.py to Constraints and sections 3.1/4.1, with concrete extraction targets when either projection reaches 1000.","location":"P3 / §3.1","prevention":"Measure current and projected line counts for every targeted production file and add same-task decomposition where projection reaches 1000.","principle":"The production source-size ceiling applies to every touched eligible file.","root_cause":"The plan budgets sandbox.py and spawn_executor.py while omitting acp_client.py at 955 lines and session-start flow.py at 966 lines.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"parser-behavior-test-seam","description":"The plan can register AgyTranscriptParser while leaving its substantive parsing and recovery behavior unverified.","finding_id":"agy-r2-parser-test-seam","fix":"Add a focused AGY parser test module target and tests for all record classes, unknown MODEL tools, truncated_fields, malformed lines, stable IDs, interrupted RUNNING records, and append-only incremental reads.","location":"P4 / §4.2","prevention":"Map each parser acceptance item to a fixture-backed focused test target before expansion.","principle":"Behavior-rich parser acceptance requires focused executable tests for every record and recovery branch.","root_cause":"Section 4.2 targets only a registry-membership assertion while specifying tool variants, truncation, malformed input, stable IDs, and incremental reads.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"live-model-consumer-inventory","description":"The static catalog can be refreshed while live discovery and provider-route behavior remain stale or untested.","finding_id":"agy-r2-model-consumer-inventory","fix":"Add provider_model_discovery.py and focused provider-model and provider-route tests to Targets; cover supported live discovery, sub-floor fallback, command failure/cache fallback, and source/availability transitions.","location":"P6 / §6.3","prevention":"Sweep discovery parsers, caches, provider routes, source labels, availability flags, fixtures, and exhaustive tests for each provider-state transition.","principle":"Changing a provider from static/unavailable to live/available requires updating discovery and every downstream contract that exhaustively represents that state.","root_cause":"Section 6.3 omits the discovery implementation seam and focused model/route tests that currently encode AGY as static and unavailable.","section_id":"6.3","severity":"blocking"}],"reviewer_run":"43c0dca7-e6ce-4279-8d3c-4bfbf5619f3f","reviewer_session":"d1a7f8b5-dbe3-4d43-beb9-4c1186e7b984","round":2,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 3** `kind: verification`

- reviewer_run: 927aed1d-5481-42c8-95d1-4f9a84342380
- reviewer_session: 1e611975-e0e6-4c63-96fc-e2d05faa25eb
- verdict: needs_review
- findings:
- agy-r3-gate0-unresolved-contracts / blocking / downstream acceptance unconditionally required resume, tool-chat advertisement, and FULL status Gate 0 leaves open
- agy-r3-transcript-fallback-recovery / blocking / hook-first discovery lacked usable/pending/invalid path states and bounded disk fallback
- agy-r3-droid-timeout-contract / blocking / 2.4's read timeout named no clock, terminal event, cleanup, or reconnect semantics
- agy-r3-test-target-inventory / blocking / six acceptance-named focused test files were absent from their deliverables' Targets
- agy-r3-version-record-lifecycle / blocking / the 2.5 support record had no startup initialization owner, sentinel, or catalog injection
- agy-r3-agy-srt-policy-inventory / blocking / sandbox_policy.py provider domain/auth maps have no agy entries and were untargeted
- agy-r3-dual-dispatch-entrypoint / blocking / two-phase dispatch was anchored to translate_to_hook_event, which cannot dispatch two events
- agy-r3-agy-outcome-recovery / blocking / parity omitted live zero/nonzero shell payload capture and failure-to-success readiness recovery
- agy-r3-model-default-acceptance / blocking / the gemini-3.5-flash default-effort reconciliation had no acceptance item or named value
- agy-r3-readiness-surface-parity / nit / "FULL across all four surfaces" did not match the matrix's three-surfaces-plus-status schema
- resolution_notes: All 10 findings accepted in unattended mode; the coordinator verified
  the entrypoint, timeout-constant, startup-owner, and sandbox-map claims against the code
  index before voting. Constraints gained the explicit Gate 0 branch rule; 6.2.1 was
  rewritten to the deterministic tool-chat branch and 7.1 to enumerated matrix cells
  (7.1.1 reworded). 1.1 gained the network/state-footprint probe (question 7, 1.1.9) and
  live zero/nonzero shell capture (1.1.10). 2.2 gained usable/pending/invalid path states
  with bounded fallback (2.2.3, 2.2.4). 2.4 adopted the ACP inactivity-timeout contract
  with terminal-error, cleanup, and reconnect semantics (2.4.3 reworded, 2.4.4). 2.5 named
  the daemon lifespan as initialization owner with a fail-closed sentinel and single-probe
  guarantee (2.5.4); 6.3 now reads that record for sub-floor detection. 3.2 targeted
  sandbox_policy.py and its agy map entries (3.2.4), proven at the launch seam by the
  extended 5.2.11. 4.1 anchored two-phase dispatch to an AgyAdapter.handle_native override
  (4.1.2 reworded). 4.2 gained the failure-then-success readiness recovery case (4.2.10).
  6.3 named the canonical gemini-3.5-flash default resolution with consumer parity (6.3.6)
  and narrowed the #19483 attribution. All six acceptance-named focused test files were
  added to their deliverables' Targets (2.4, 2.5, 3.1, 4.1, 5.2, 5.3, 6.1, 6.2).

```json plan-review-round
{"evidence_id":"3ebc7865-e5a4-4c69-abd5-7e194efc2483","plan_hash":"690eb839e2524c5df6060af059ac4d22102f13251ae6523f9c89a292bda45ba3","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"b62f3a97561c8909c11f12249b9ec3023446c0b323119202c5814f21ccbbe9a1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":10,"total":12},"evidence_id":"3ebc7865-e5a4-4c69-abd5-7e194efc2483","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"18ee97c0958051e891d97f1e68232245a9958234cd26fb8946dfc7b93a70d67b","status":"valid"},"source_digest":"3dd92865e89abba6f88ab5070ed139358e933d1c1011d7df82c36105fa4874ef","version":1},"findings":[{"category":"bad-sequencing","check_key":"resolved-contract-before-expansion","description":"A failed resume probe makes 5.2.2, 5.2.9, and FULL documentation impossible, while 6.2 permits TOOL_CHAT to remain unavailable but 6.2.1 requires it to be advertised.","finding_id":"agy-r3-gate0-unresolved-contracts","fix":"Run the six live probes before the next approval round, record the observed contracts in the artifact, and rewrite 5.2, 6.2, and 7.1 to one deterministic result; use typed deferrals for any unsupported surface.","location":"P1 / §1.1; consumers §5.2, §6.2, §7.1","prevention":"Resolve every live-provider branch before handoff, or represent the unavailable branch as a typed deferral with an open task.","principle":"An execution plan must make downstream acceptance satisfiable under one resolved contract.","root_cause":"Gate 0 leaves resume and controlled TOOL_CHAT feasibility open while later sections unconditionally require resumable TOOL_CHAT and FULL status.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"hook-path-usability-fallback","description":"A truthy unusable hook path can suppress disk fallback indefinitely, while an unbounded fallback scan can block the hook request path.","finding_id":"agy-r3-transcript-fallback-recovery","fix":"Define usable, pending, and invalid path states; add bounded retry for delayed creation; persist only usable paths; and use direct bounded candidates or offload capped traversal before disk fallback.","location":"P2 / §2.2","prevention":"Test preferred-path usability, delayed creation, permanent absence, unreadability, traversal errors, and bounded lookup latency.","principle":"Fallback selection must test usability and bound recovery work, not merely test whether a preferred value is nonempty.","root_cause":"The plan defines hook-first selection without states for pending, absent, stale, or unreadable hook paths, and expands a blocking disk traversal into synchronous session-start handling.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ndjson-timeout-cleanup-contract","description":"Different reasonable timeout implementations can either kill active long turns, wait forever on periodic output, or leave a Droid subprocess and session handle orphaned.","finding_id":"agy-r3-droid-timeout-contract","fix":"Specify initialization and active-turn timeout values and reset rules, require one terminal error, terminate the owned process tree, remove the handle, leave the session reconnectable, and add focused timeout/cancellation tests.","location":"P2 / §2.4","prevention":"For every timeout, specify the clock boundary, caller-visible error, owned-resource cleanup, and post-timeout state, then test each transition.","principle":"A new stream timeout needs explicit clock, outcome, cleanup, and recovery semantics.","root_cause":"Section 2.4 requests a read timeout without naming its source or value, per-line versus whole-turn behavior, terminal event, process-tree cleanup, or reconnect state.","section_id":"2.4","severity":"blocking"},{"category":"gobby-format","check_key":"target-inventory-all-changed-tests","description":"Six focused test paths are required by acceptance but absent from applicable Targets: test_version_gate.py, test_launch_contracts.py, test_agy.py, test_agy_backend.py, test_spawn_executor.py, and test_agy_tool_chat_contract.py.","finding_id":"agy-r3-test-target-inventory","fix":"Add each test file to every deliverable that changes it, including the shared launch-contract and AGY backend test files in all sections that add distinct cases.","location":"§2.5, §3.1, §4.1, §5.2, §5.3, §6.1, §6.2","prevention":"Compare every file and test artifact named by acceptance with each deliverable's exact Targets block before resubmission.","principle":"Every file a deliverable changes must appear in that deliverable's Targets inventory.","root_cause":"Round 2 repairs added focused test acceptance references without adding the corresponding changed test files to Targets.","section_id":"2.5","severity":"blocking"},{"category":"bad-sequencing","check_key":"async-version-record-initialization","description":"The record can be read before publication or bypassed by model discovery, producing inconsistent AGY availability and duplicate version probes.","finding_id":"agy-r3-version-record-lifecycle","fix":"Target the concrete daemon startup owner, await record publication before exposing AGY, define a fail-closed uninitialized sentinel, inject the same record into every consumer including ProviderModelCatalog, and test exactly-once execution.","location":"P2 / §2.5; consumers §5.3, §6.2, §6.3","prevention":"For shared startup records, inventory every consumer and test pre-initialization, concurrent initialization, failure, and exactly-once reads.","principle":"A shared async prerequisite needs an initialization owner and barrier before synchronous or lazy consumers can read it.","root_cause":"The new module defines a once-only probe but no targeted daemon startup integration point publishes the record before health, registry, spawn, session, and model-catalog consumers execute.","section_id":"2.5","severity":"blocking"},{"category":"traceability","check_key":"srt-provider-policy-inventory","description":"AGY SRT web-chat and spawn launches can pass wrapper preflight yet lack upstream network access and access to ~/.gemini/antigravity-cli credentials, transcripts, and writable state.","finding_id":"agy-r3-agy-srt-policy-inventory","fix":"Add sandbox_policy.py to Targets, define live-proven AGY domains and exact read/write state roots, and extend launch-contract tests to prove authentication, transcript/state writes, and bounded network access.","location":"P3 / §3.1 and §3.2; consumers §5.2 and §6.1","prevention":"Sweep provider domains, credentials, read/write exceptions, executable resolution, socket grants, fakes, and launch tests for every new SRT provider.","principle":"A new sandboxed provider must be added to every closed policy inventory that supplies its network, credentials, and writable runtime state.","root_cause":"AGY is absent from sandbox_policy.py provider domain, auth-read, and auth-write maps, and that file is absent from the plan's Targets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"native-hook-dual-dispatch-entrypoint","description":"The repaired text preserves both SESSION_START and BEFORE_AGENT conceptually, but the current adapter contract has no executable seam that can perform both dispatches.","finding_id":"agy-r3-dual-dispatch-entrypoint","fix":"Specify and target an AgyAdapter.handle_native override, or a deliberate base-contract extension, that dispatches synthetic SESSION_START first, dispatches the original PreInvocation once, and returns only the original event's translated response.","location":"P4 / §4.1","prevention":"Trace synthesized events through the concrete adapter entrypoint, return type, dispatch count, response ownership, and first/repeated-event tests.","principle":"A two-event semantic repair must name an entrypoint whose interface can dispatch both events in order.","root_cause":"translate_to_hook_event returns one HookEvent and BaseAdapter.handle_native invokes HookManager.handle exactly once, while the plan anchors both phases to translation.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"validation-evidence-recovery-sequence","description":"The plan can satisfy isolated success and failure cases while still violating #18381's provider-proven payload requirement and failure-to-success readiness recovery.","finding_id":"agy-r3-agy-outcome-recovery","fix":"Add scrubbed live zero- and nonzero-exit AGY shell records with exact structured fields and provenance, plus a sequential test proving failure remains fail-closed and a later correlated definitive success restores readiness and close-time context.","location":"P1 / §1.1 and P4 / §4.2","prevention":"Trace governing validation criteria through live capture, normalization, storage, readiness transitions, and close-time context, including recovery sequences.","principle":"Superseding governing tasks requires explicit acceptance for every live evidence and state-transition criterion they carry.","root_cause":"The six-outcome parity list omits live representative zero/nonzero shell payload capture and does not assert failure followed by later definitive success restoring readiness.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"model-default-acceptance-parity","description":"The model catalog can pass all five criteria while AGY_MODELS and provider_model_defaults continue to disagree; task #19483 is broader planning work and does not close this bounded gap.","finding_id":"agy-r3-model-default-acceptance","fix":"Add an acceptance item naming the canonical gemini-3.5-flash default effort and test both consumers agree; remove or precisely qualify the #19483 attribution.","location":"P6 / §6.3","prevention":"Map every body-level behavior claim to an acceptance item and focused consumer-parity test.","principle":"Every stated behavioral change needs a bounded acceptance item covering all named consumers.","root_cause":"Section 6.3 promises to reconcile gemini-3.5-flash default effort but acceptance 6.3.1 through 6.3.5 never names the chosen value or cross-consumer parity.","section_id":"6.3","severity":"blocking"},{"category":"traceability","check_key":"documentation-readiness-schema-parity","description":"An implementer cannot tell whether the fourth item means Status, spawn, or tool chat, so different edits can satisfy the wording.","finding_id":"agy-r3-readiness-surface-parity","fix":"Enumerate Hook=Full, Transcript=JSONL, Web-chat=custom stream-json, and Status=FULL, or explicitly revise the matrix schema before adding another surface.","location":"P7 / §7.1","prevention":"Compare documentation criteria with the current table schema and enumerate every changed cell.","principle":"Documentation acceptance should name exact cells in the governing schema.","root_cause":"The plan says FULL across four surfaces while the matrix defines three readiness surfaces plus a status column.","section_id":"7.1","severity":"nit"}],"reviewer_session":"1e611975-e0e6-4c63-96fc-e2d05faa25eb","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 4** `kind: verification`

- reviewer_run: fa6d2a5e-49c4-4fd8-b43a-16ea1c7d0104
- reviewer_session: d3da2185-662d-4c74-928c-b049decb0f85
- verdict: needs_review
- findings:
- agy-r4-gate0-preexpansion-contracts / blocking / the prose Gate 0 branch rule had no enforcement mechanism over the already-derived 17-leaf manifest
- agy-r4-parser-dispatch-caller-inventory / blocking / deleting _get_parser stranded direct callers in transcript index, reader, window, and two test modules
- agy-r4-transcript-classifier-entrypoint / blocking / flow.py accepts truthy hook paths before derivation, so a helper-scoped classifier never governs the primary path
- agy-r4-transcript-recovery-contract-split / blocking / find_transcript_on_disk also serves watchdog, reader, and validation-evidence recovery beyond the hook path
- agy-r4-version-record-lifecycle-order / blocking / lifespan publication lands after ToolChatService registry construction and the model catalog self-probes
- agy-r4-resolver-symbol-target-scope / blocking / AgySandboxResolver fell outside 3.2's exact-symbol Targets
- agy-r4-resolver-capability-sequencing / blocking / get_sandbox_resolver's gate reads PROVIDER_CAPABILITIES, whose AGY row was owned downstream in 6.1
- agy-r4-agy-policy-dependency / blocking / 5.2.11 consumed 3.2.4's policy entries without a 3.2 dependency edge
- agy-r4-webchat-srt-launch-boundary / blocking / synchronous create_session precedes project-path hydration; the async start seam was untargeted
- agy-r4-synthetic-start-response-merge / blocking / discarding the synthetic SESSION_START response loses startup context permanently
- agy-r4-agy-sidecar-hydration / blocking / parser-state sidecar persistence is codex-only and drops pending AGY call/result correlation across restart
- agy-r4-agy-read-timeout-contract / blocking / 5.2's read timeout named no clock, reset, terminal, cleanup, or reconnect semantics
- agy-r4-spawn-version-gate / blocking / execute_spawn bypasses the registry gate, so sub-floor AGY binaries could spawn
- agy-r4-tool-chat-provider-aware-cache / blocking / ToolChatService caches adapters by style alone, cross-routing first-use Droid/AGY
- agy-r4-model-cache-version-compatibility / blocking / a 1.0.10-era cache survives migration and overrides static fallback on failed discovery
- agy-r4-adjacent-test-target-inventory / blocking / pending-message, stream-normalizer, builder, resolution, and text-generation test targets were missing
- agy-r4-model-label-consumer-acceptance / nit / 6.3.2 cited one of the three files carrying the retired label
- agy-r4-wiki-generated-target / blocking / the wiki concept page is gitignored generated output and cannot be a durable Target
- resolution_notes: All 18 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — flow.py:337 truthy
  acceptance, runner_init/services.py:69 pre-lifespan construction, the style-keyed
  adapter cache at _tool_chat_service.py:240-252, the codex-only parser-state gate at
  processor_transcripts.py:220, provider_supports_sandbox reading PROVIDER_CAPABILITIES,
  versionless cache acceptance in provider_models.py, /wiki/ in .gitignore, _session.py
  at 988 lines, and the synchronous create_session at runtime_manager.py:244. Repairs:
  Constraints gained the enforceable Gate 0 checkpoint mechanism (1.1.11) and the
  five-file line budget including _session.py. 2.1 inventoried the direct _get_parser
  callers (2.1.6). 2.2 moved classification to the session-start caller and split the
  discovery contract for late-recovery callers (2.2.5, 2.2.6). 2.5 moved record
  publication into runner initialization before service construction and pulled the
  catalog's version read into scope (2.5.4 reworded, 2.5.5). 3.1 re-anchored
  session-owned launches to the async post-hydration seam with _session.py budgeted
  (3.1.1 and 3.1.7 reworded, 3.1.10). 3.2 took the justified wildcard sandbox.py target,
  the PROVIDER_CAPABILITIES row, and reachability tests (3.2.5). 4.1 merged the synthetic
  response into the BEFORE_AGENT reply (4.1.2 reworded, 4.1.10) and added pending-message
  coverage (4.1.11). 4.2 gained sidecar hydration (4.2.11). 5.1 gained its focused test
  module (5.1.5). 5.2 gained the 3.2 dependency edge and the restated inactivity-timeout
  contract (5.2.12). 6.1 gained the 2.5 dependency and the spawn-time record gate (6.1.7,
  6.1.8). 6.2 made adapter selection and caching provider-aware (6.2.7). 6.3 gained cache
  compatibility (6.3.7), the three-file label acceptance (6.3.2 reworded), and
  text-generation effort parity (6.3.8). 7.1 dropped the generated wiki target.

```json plan-review-round
{"evidence_id":"18ac26e4-7cb2-4d0d-a979-08b0aab7d31f","plan_hash":"144777c0e77782d5639cc8f721e3814f93c41f2eff64033f1505284149829862","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2460c1291eb0a0678256e868acc7c3b854ab58e1dd1b01e91c15dd0981ac2267","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":18,"total":29},"evidence_id":"18ac26e4-7cb2-4d0d-a979-08b0aab7d31f","lanes":[{"candidate_count":11,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":10,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"905a64e3dfb88ea131a89dcc9e89b07af744bdabe36bad3c73fb4b34d28f583f","status":"valid"},"source_digest":"4c88066b9c7732b1ad1f17e74e79e96bb7d2cf4715a727b64fe81126052b9851","version":1},"findings":[{"category":"bad-sequencing","check_key":"resolved-contract-before-expansion","description":"Resume, cwd, sandbox flags, cancellation, network/state, image input, and controlled-tool feasibility remain open while downstream leaves require concrete outcomes including `--conversation` resume and `Status=FULL`.","finding_id":"agy-r4-gate0-preexpansion-contracts","fix":"Run the live probes as a prerequisite outside this implementation manifest, then rewrite and re-review every consumer against the recorded results. Convert any abandoned surface to a typed deferral with an open `deferred-from` task before deriving the 17-leaf manifest.","location":"P1 / §1.1; consumers §3.2, §5.2, §6.1, §6.2, §7.1","prevention":"Before approval, verify each live-dependent acceptance item against recorded evidence or represent it as a valid typed deferral.","principle":"Every expanded leaf must have one satisfiable contract before the manifest is approved.","root_cause":"The server derives all 17 leaves in one manifest before Gate 0 runs; the prose promise to revise and re-review affected sections has no mechanism to stop those already-expanded downstream leaves.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"parser-registry-caller-inventory","description":"Deleting `_get_parser` as written leaves runtime calls and tests in `transcript_index.py`, `transcript_reader.py`, `transcript_window.py`, `tests/sessions/test_transcript_parsers.py`, and `tests/sessions/transcripts/test_droid_parser.py` on a removed symbol.","finding_id":"agy-r4-parser-dispatch-caller-inventory","fix":"Add those files to 2.1 Targets, migrate every caller to `transcripts.get_parser`, and add focused unknown-source and Droid-path regressions before deleting `_get_parser`.","location":"P2 / §2.1","prevention":"Sweep imports, call sites, re-exports, fakes, and tests before deleting a shared helper.","principle":"Deleting a shared dispatch helper requires migrating every production caller and affected test in the same deliverable.","root_cause":"Section 2.1 inventories five maps but omits direct `_get_parser` consumers in transcript index, reader, window, and their parser tests.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"hook-path-usability-fallback","description":"The round-three classifier repair can be implemented inside its targeted helpers while session-start flow continues persisting reported-but-absent or unreadable paths.","finding_id":"agy-r4-transcript-classifier-entrypoint","fix":"Add `flow.py` and `tests/hooks/test_transcript_path_derivation.py` to 2.2 Targets; route every reported path through the classifier before persistence; test usable, pending, invalid, retry, and fallback. Add the same-task line-budget/decomposition gate because `flow.py` is already 966 lines.","location":"P2 / §2.2; interaction with §4.1","prevention":"Trace raw input through selection, validation, persistence, retry, and fallback at the actual caller.","principle":"A path classifier must own the value before any caller selects or persists it.","root_cause":"`handle_session_start` accepts a truthy hook path directly before invoking `derive_transcript_path`, so usable/pending/invalid classification never governs the primary AGY path.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"transcript-discovery-caller-contract","description":"A helper-only rewrite can either retain blocking traversal on the hook path or remove discovery needed by watchdog, transcript-reader, and validation-evidence recovery.","finding_id":"agy-r4-transcript-recovery-contract-split","fix":"Split bounded hook-time resolution from late recovery, or extend the resolver contract with explicit project/cwd context and update every caller. Target the watchdog, transcript reader, validation-evidence consumer, and focused tests for all provider layouts.","location":"P2 / §2.2","prevention":"Inventory every caller of a shared recovery primitive and test each caller's latency, context, and fallback behavior.","principle":"A synchronous hook lookup and asynchronous late recovery need contracts appropriate to their latency and available context.","root_cause":"`find_transcript_on_disk` is shared by session start, watchdog, transcript reader, and validation-evidence recovery, but section 2.2 replaces it with direct candidates using a signature whose callers lack project/cwd context.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"async-version-record-initialization","description":"A supported AGY installation can remain frozen unavailable in the already-built tool-chat registry, and catalog refresh can violate the exactly-one-version-subprocess acceptance.","finding_id":"agy-r4-version-record-lifecycle-order","fix":"Move AGY support-record publication before support-dependent service construction, or atomically rebuild every retained holder before serving. Move the AGY `ProviderModelCatalog` version read into 2.5, forbid its re-probe, and add a daemon-construction test covering the installed `ToolChatService` plus catalog refresh.","location":"P2 / §2.5; consumers §6.2 and §6.3","prevention":"Map startup construction order and every probe/read consumer; test pre-publication, supported startup, exactly-once execution, and retained-service visibility.","principle":"A shared support record must be published before any long-lived consumer freezes its value, and every version consumer must read that record.","root_cause":"FastAPI lifespan runs after `ToolChatService` builds and retains its registry, while `ProviderModelCatalog` still launches its own AGY version probe until downstream 6.3.","section_id":"2.5","severity":"blocking"},{"category":"gobby-format","check_key":"new-symbol-target-coverage","description":"`AgySandboxResolver` falls outside the manifest-owned edit scope, so an implementing leaf cannot add the class without an out-of-scope change.","finding_id":"agy-r4-resolver-symbol-target-scope","fix":"Use a justified `src/gobby/agents/sandbox.py::*` target for the multi-symbol edit, or place `AgySandboxResolver` in a new focused module and target that file explicitly.","location":"P3 / §3.2","prevention":"Compare every planned new or changed symbol with the exact Targets block before review completion.","principle":"Every new symbol in an existing symbol-bearing file must be inside the declared Target scope.","root_cause":"Section 3.2 creates `AgySandboxResolver` while targeting only three existing symbols in `sandbox.py`.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"resolver-factory-reachability","description":"Section 3.2 cannot satisfy `get_sandbox_resolver(\"agy\")` before a dependent leaf adds the required capability entry.","finding_id":"agy-r4-resolver-capability-sequencing","fix":"Move the AGY `PROVIDER_CAPABILITIES` entry and `tests/agents/test_sandbox.py` reachability/gate cases into 3.2; let 6.1 consume the completed capability record.","location":"P3 / §3.2; downstream §6.1","prevention":"Check factory guards, capability tables, constructors, and focused tests in the same deliverable as a new implementation.","principle":"An implementation must be reachable at the completion boundary of the deliverable that introduces it.","root_cause":"`get_sandbox_resolver` rejects providers absent from `PROVIDER_CAPABILITIES`, but the AGY row is owned by downstream 6.1 even though acceptance 3.2.3 requires the resolver to work.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"consumer-before-producer-dependency","description":"The AGY launch-contract row can execute before its required domain, credential, state-root, and environment policy entries exist.","finding_id":"agy-r4-agy-policy-dependency","fix":"Add 3.2 to 5.2's dependencies and retain the 5.2 launch-contract row as downstream proof of the completed policy inventory.","location":"P5 / §5.2; producer §3.2","prevention":"Resolve each cross-section acceptance reference to an explicit dependency edge.","principle":"Acceptance cannot consume an artifact from an unordered sibling deliverable.","root_cause":"Acceptance 5.2.11 requires the AGY sandbox-policy entries produced by 3.2.4, while 5.2 depends only on 5.1 and 3.1.","section_id":"5.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"workspace-scoped-process-ownership","description":"The plan cannot launch session-owned SRT subprocesses during `create_session` without blocking the event loop or using the wrong workspace, and changing the real caller would be out of manifest scope.","finding_id":"agy-r4-webchat-srt-launch-boundary","fix":"Make `ManagedChatSessionBase.start`/backend attach the SRT preparation owner after project hydration, or make creation async and move it after resolution. Target `ChatSessionMixin._create_chat_session_inner` and lifecycle owners, test first start/resume/failure with final worktree paths, and decompose `_session.py` in this task because it is already 988 lines.","location":"P3 / §3.1; consumers §5.2 and §5.3","prevention":"Record process owner, async boundary, workspace source, launch point, teardown point, and failed-start cleanup for every backend.","principle":"A workspace-scoped asynchronous sandbox launch must occur after the final project path is known.","root_cause":"`WebChatRuntimeManager.create_session` is synchronous and runs before `_session.py` resolves `project_path`; awaited `session.start` is the later executable seam but is absent from Targets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"synthetic-hook-response-merge","description":"AGY receives only the original `BEFORE_AGENT` response; startup context is lost while later events correctly suppress it as already delivered.","finding_id":"agy-r4-synthetic-start-response-merge","fix":"Merge synthetic startup `context`/`system_message` and required metadata into the original `BEFORE_AGENT` HookResponse, preserve the original decision, translate once to `injectSteps`, and mark startup injected only after successful emission. Test exact-once startup payload on first and repeated `PreInvocation`.","location":"P4 / §4.1","prevention":"Trace response ownership, merge policy, persistent markers, and first/repeated-event tests for every synthesized event.","principle":"When one native hook carries two logical events, caller-visible outputs from both phases must be delivered exactly once before emission is recorded.","root_cause":"The plan discards the synthetic `SESSION_START` HookResponse while `handle_session_start` composes startup context/system message and marks that context injected.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"append-only-sidecar-hydration","description":"An AGY append after daemon restart invalidates the saved cursor or loses the pending tool-call ID when the boundary falls between `PLANNER_RESPONSE` and its result.","finding_id":"agy-r4-agy-sidecar-hydration","fix":"Target `processor_lifecycle.py` and the sidecar seam; admit verified append-only AGY growth, implement `AgyTranscriptParser.snapshot_state`/`hydrate_state`, and add a restart test that appends the result after a saved tool-call boundary.","location":"P4 / §4.2","prevention":"Test restart at every multi-record correlation boundary with an appended record after the saved cursor.","principle":"Incremental transcript parsing must preserve cursor validity and parser correlation state across daemon reconstruction.","root_cause":"Current enlarged-file sidecar hydration is Codex-only, and base parser snapshots omit the pending AGY call/result correlation state.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ndjson-timeout-cleanup-contract","description":"Implementers can choose incompatible timeout behavior that kills active long turns, hangs on silence, duplicates terminal errors, or leaves a locked/orphaned session.","finding_id":"agy-r4-agy-read-timeout-contract","fix":"State whether AGY reuses `DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS` as a per-line inactivity timeout and name its override. Add expiry/reset tests requiring one terminal error, process-tree cleanup, lock release, preserved confirmed conversation ID, and reconstructable state.","location":"P5 / §5.2","prevention":"For every timeout, specify source/value, per-line or whole-turn clock, reset events, error count, resource cleanup, and reconnect state.","principle":"A stream timeout needs an explicit clock, reset rule, terminal outcome, cleanup, and recoverable post-timeout state.","root_cause":"Section 5.2 requires a read timeout but acceptance covers only large lines, cancellation, and nonzero exit.","section_id":"5.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"spawn-support-record-gate","description":"A sub-1.1.9 or unparseable AGY binary can start even while capability metadata truthfully reports it unavailable.","finding_id":"agy-r4-spawn-version-gate","fix":"Make 6.1 depend on 2.5 and read the support record in `execute_spawn` before sandbox/session/terminal/process side effects. Add `test_provider_resolution.py`, `test_command_builder.py`, and spawn-executor cases for explicit, inherited, agent-configured, and default selection with the actionable upgrade reason.","location":"P6 / §6.1; prerequisite §2.5 and metadata §6.2","prevention":"Trace version support through every advertised and executable entry point and test supported, sub-floor, absent, unparseable, and pre-publication records.","principle":"Every executable entry point for a version-gated provider must enforce the initialized support record before side effects.","root_cause":"Section 6.1 enables static provider resolution and direct `execute_spawn`; section 6.2 gates registry metadata only, while explicit and agent-selected spawns bypass that registry.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"provider-specific-tool-chat-adapter","description":"The first CLI adapter cached can be reused for the other provider, so AGY can still route to Droid or Droid to AGY despite the dedicated adapter.","finding_id":"agy-r4-tool-chat-provider-aware-cache","fix":"Target `_tool_chat_service.py` and its tests; pass the resolved `CapabilityBinding` into selection and key factories/cache by `(adapter_style, provider)`, or add a distinct exhaustive style. Test Droid→AGY and AGY→Droid in one service instance.","location":"P6 / §6.2","prevention":"Sweep service selection, factory signature, cache key, binding propagation, and both provider orders whenever one style gains multiple protocols.","principle":"A provider-specific binding must select and cache an adapter by provider-aware identity.","root_cause":"`ToolChatService` accepts zero-argument factories and caches solely by `AIAdapterStyle`; changing only the builder factory leaves first-use Droid/AGY cross-routing.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"model-cache-version-compatibility","description":"An existing 1.0.10 cache can survive the 1.1.9 migration and override the refreshed static catalog on the first failed live discovery.","finding_id":"agy-r4-model-cache-version-compatibility","fix":"Define AGY cache compatibility, invalidate pre-1.1.9 and retired-source entries, and reuse cache only when compatible with the immutable support record. Add a migration test loading a real old-cache shape before failed refresh.","location":"P6 / §6.3","prevention":"Test upgrade, downgrade, retired source labels, old schemas, discovery failure, and cache/static selection.","principle":"A cached provider catalog may override fallback only when its version and schema are compatible with the installed support record.","root_cause":"Current refresh prefers any cached models after discovery failure and accepts old cache shapes without checking AGY CLI/catalog version.","section_id":"6.3","severity":"blocking"},{"category":"gobby-format","check_key":"target-inventory-all-changed-tests","description":"Missing Targets remain for `tests/hooks/test_pending_message_provider_contracts.py` (4.1), a focused `test_agy_stream.py` (5.1), command-builder and provider-resolution tests (6.1), and `tests/ai/test_text_generation.py` (6.3).","finding_id":"agy-r4-adjacent-test-target-inventory","fix":"Add those test paths to their owning Targets and acceptance: AGY pending-message delivery, stream init/delta/result dedupe/tool/malformed branches, AGY argv/provider selection, and fixture-driven default-effort normalization.","location":"§4.1, §5.1, §6.1, §6.3","prevention":"After each target change, sweep adjacent unit, registry, consumer, exhaustive, and integration suites and compare all changed tests with Targets.","principle":"Every changed contract and behavior-rich new parser must name its affected focused test artifacts in the owning deliverable.","root_cause":"The round-three repair added acceptance-named tests but missed adjacent suites and 5.1 has no focused stream-normalizer test target.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"model-label-consumer-acceptance-parity","description":"A bounded review can pass 6.3.2 while stale source labels remain in two of the three affected files.","finding_id":"agy-r4-model-label-consumer-acceptance","fix":"Split 6.3.2 by consumer or cite all three files and add a repository-wide absence assertion for the retired label.","location":"P6 / §6.3","prevention":"Split multi-consumer acceptance or attach an artifact reference for every claimed consumer.","principle":"Acceptance evidence should identify every consumer claimed by a repository-wide transition.","root_cause":"The eight retired `agy-1.0.10-static` occurrences span registry builder, provider defaults, and a capability test, while 6.3.2 cites one file.","section_id":"6.3","severity":"nit"},{"category":"gobby-format","check_key":"durable-doc-target","description":"Direct edits to the AGY concept page cannot be committed reliably and can be overwritten by wiki regeneration.","finding_id":"agy-r4-wiki-generated-target","fix":"Remove the generated page from direct Targets or target its durable source and specify the `gwiki` regeneration/verification command. Add acceptance for the durable AGY knowledge source.","location":"P7 / §7.1","prevention":"Check target tracking status, generator ownership, source-of-truth location, and acceptance for every generated documentation target.","principle":"A documentation leaf must target a durable source or specify the generator that owns derived output.","root_cause":"`wiki/knowledge/concepts/agy.md` is ignored generated output, and 7.1 gives it neither source/regeneration workflow nor acceptance.","section_id":"7.1","severity":"blocking"}],"reviewer_session":"d3da2185-662d-4c74-928c-b049decb0f85","round":4,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```
