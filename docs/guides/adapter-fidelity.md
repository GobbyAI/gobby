# Adapter Fidelity

Gobby aims for policy portability with provider-specific fidelity. Rules and
workflows should target normalized `HookEvent` and `HookResponse` behavior, but
adapters must still respect each CLI's native hook names, response fields, and
context channels.

The executable source of truth is `src/gobby/adapters/capabilities.py`.
New providers must declare capabilities before adapter behavior is added.
Unsupported-provider research lives in
[`docs/research/cli-support-feature-matrix-codex-findings.md`](../research/cli-support-feature-matrix-codex-findings.md).
Use that matrix before adding first-class CLI support; a provider must have
proven hooks, transcripts/session identity, and web-chat streaming/control.
AGY is the negative-control case: hook install parity alone is not enough.

## Capability API

Use:

```python
from gobby.adapters.capabilities import get_provider_capabilities

capabilities = get_provider_capabilities("codex")
hook = capabilities.get_hook("PreToolUse")
```

Each hook capability declares:

- Native hook name and normalized `HookEventType`
- Response decision style
- Context channel: `additionalContext`, `systemMessage`, or `none`
- Reason formatting behavior
- Supported and unsupported `HookResponse` fields

## Current Providers

| Provider | Hook Contract | Context Routing | Tool / Permission Control | Elicitation | Notes |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `claude_contract.py` kebab-case native names | `additionalContext` on supported Claude hooks; startup banner is injected once | `PreToolUse`, `PermissionRequest`, retry, watch paths, worktree create | Supported on Claude elicitation hooks | Pre-tool rule-block reasons are compacted for terminal readability |
| Codex hooks.json | PascalCase hooks in `CodexHooksAdapter.EVENT_MAP` | `additionalContext` for `SessionStart`, `UserPromptSubmit`, `PostToolUse`; `systemMessage` for `PreToolUse`, `PermissionRequest`, compaction, and stop hooks | `PreToolUse` and `PermissionRequest`; tool-input rewrites are applied by dispatch enforcement where supported | Not supported by terminal hooks.json adapter | Unsupported response fields are dropped with telemetry |
| AGY CLI | PascalCase AGY hook names | Not supported by current AGY hook stdout | Compact `PreToolUse` allow/deny/ask decisions and `updatedInput` | Not supported | Hook install parity only; web chat, spawning, and live transport remain unavailable |
| Qwen CLI | `qwen_contract.py` current PascalCase hook names | Event-specific `hookSpecificOutput.additionalContext` | `PreToolUse.permissionDecision`, structured `PermissionRequest.decision`, and top-level stop/subagent/todo decisions | Not supported | Dedicated terminal adapter; ACP remains the web-chat transport only |
| Factory Droid | PascalCase Droid hook names | `additionalContext` on Droid-supported context hooks; no context channel on `PreToolUse` | `PreToolUse.permissionDecision` | Not supported | Current behavior is intentionally standalone, not inherited from Claude |
| AGY | No public live hook contract in AGY CLI `1.0.8` | No daemon-usable context channel; TUI hooks are not a stable subprocess protocol | No supported external tool approval, cancellation, streaming, or resume transport; hidden `agentapi` remains launcher-gated on `ANTIGRAVITY_LS_ADDRESS` | Not supported | Keep runtime surfaces unavailable until AGY CLI exposes ACP or the `google-antigravity` SDK evaluation proves a production daemon transport |
| Grok | snake_case hooks in `GROK_EVENT_MAP`; ACP `0.2.51` init reports `loadSession`, `cancelRewind`, `_meta.x.ai/fs_notify`, and `availableCommands` (`compact`, `context`, `session-info`) | Observe stdout is ignored; PreToolUse uses deny reason/`updatedInput`; Stop/SubagentStop allow omits `decision`; briefing keep-working uses additionalContext-only; policy gates use `block` + reason | `pre_tool_use` permission control (`permission_decision`, `auto_approve`, modified input); live ACP model discovery reads `_meta.modelState.availableModels` | Not supported | Static fallback defaults to `grok-composer-2.5-fast` (200k ctx) with `grok-build` (512k ctx) secondary; reasoning efforts are `low`, `medium`, `high`, `xhigh`, `max` |

## Degradation Telemetry

Lossy translations record `adapter_degradations_total` with provider, hook type,
response field, destination channel, and kind.

Current degradation kinds:

- `dropped_field`: populated `HookResponse` field has no native destination
- `rerouted_field`: context is moved to a different native channel
- `context_truncated`: context exceeds the adapter safety limit
- `reason_compacted`: Claude rule-block reason was shortened for display
- `empty_block_sentinel`: block/deny reached the adapter without a reason
- `graceful_error`: hook processing failed and returned a provider-shaped non-fatal response

## Adapter Rules

When adding or changing a provider adapter:

1. Add or update capability declarations first.
2. Add drift tests proving adapter behavior matches those declarations.
3. Route unsupported `HookResponse` fields through degradation telemetry.
4. Keep AGY unavailable until a documented subprocess or SDK contract proves
   session create/resume, streaming, cancellation, tool approval, transcripts,
   and model discovery.

A broader universal response-translation layer is future work. The current
slice records today's provider facts so that future extraction has a stable
contract instead of another set of hard-coded special cases.
