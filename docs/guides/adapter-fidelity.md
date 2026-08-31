# Adapter Fidelity

Gobby aims for policy portability with provider-specific fidelity. Rules and
workflows should target normalized `HookEvent` and `HookResponse` behavior, but
adapters must still respect each CLI's native hook names, response fields, and
context channels.

The executable source of truth is `src/gobby/adapters/capabilities.py`.
New providers must declare capabilities before adapter behavior is added.
Before adding first-class CLI support, verify that the provider has proven hooks,
transcripts/session identity, and web-chat streaming/control.
AGY demonstrates that these surfaces can combine native hooks, a dedicated JSONL
parser, and a custom subprocess backend without requiring ACP.

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
| AGY CLI | Five PascalCase events: `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop` | `PreInvocation`/`PostInvocation` context via `injectSteps.ephemeralMessage`; system messages via `injectSteps.userMessage` | `PreToolUse` honors `allow`/`deny`/`ask`, `deny_unless_prior_grant`, and argument `overwrite`; `PostInvocation` honors `terminationBehavior`. `force_ask` is schema-present but unmeasured and Gobby never emits it. `permissionOverrides` is not honored (headless auto-deny wins), and `injectSteps.toolCall` is fatal and never emitted. | Not supported | AGY 1.1.18 floor; custom `AgyWebChatBackend` stream-json transport; managed spawn and 6.1 interactive dispatch are proven |
| Qwen CLI | `qwen_contract.py` current PascalCase hook names | Event-specific `hookSpecificOutput.additionalContext` | `PreToolUse.permissionDecision`, structured `PermissionRequest.decision`, and top-level stop/subagent/todo decisions | Not supported | Dedicated terminal adapter; ACP remains the web-chat transport only |
| Factory Droid | PascalCase Droid hook names | `additionalContext` on Droid-supported context hooks; no context channel on `PreToolUse` | `PreToolUse.permissionDecision` | Not supported | Current behavior is intentionally standalone, not inherited from Claude |
| Grok | snake_case hooks in `GROK_EVENT_MAP`; ACP `0.2.51` init reports `loadSession`, `cancelRewind`, `_meta.x.ai/fs_notify`, and `availableCommands` (`compact`, `context`, `session-info`) | Observe stdout is ignored; PreToolUse uses deny reason/`updatedInput`; Stop/SubagentStop allow omits `decision`; briefing keep-working uses additionalContext-only; policy gates use `block` + reason | `pre_tool_use` permission control (`permission_decision`, `auto_approve`, modified input); live ACP model discovery reads `_meta.modelState.availableModels` | Not supported | Static fallback defaults to `grok-composer-2.5-fast` (200k ctx) with `grok-build` (512k ctx) secondary; reasoning efforts are `low`, `medium`, `high`, `xhigh`, `max` |

AGY's `force_ask` disposition is intentionally separate from its honored decisions:
the field exists in the 1.1.24 response schema, but headless mode auto-denies before
a live proof can be collected. The adapter mapping therefore forbids emitting it.

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
4. Keep version floors and live negative evidence explicit when a provider's
   schema contains fields that Gobby deliberately does not emit.

A broader universal response-translation layer is future work. The current
slice records today's provider facts so that future extraction has a stable
contract instead of another set of hard-coded special cases.

_Last verified: 2026-08-30_
