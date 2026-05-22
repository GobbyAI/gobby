# Adapter Fidelity

Gobby aims for policy portability with provider-specific fidelity. Rules and
workflows should target normalized `HookEvent` and `HookResponse` behavior, but
adapters must still respect each CLI's native hook names, response fields, and
context channels.

The executable source of truth is `src/gobby/adapters/capabilities.py`.
New providers must declare capabilities before adapter behavior is added.

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
| Gemini CLI | PascalCase Gemini hook names | `hookSpecificOutput.additionalContext` | Top-level allow/block decisions; `BeforeModel.llm_request` and `BeforeToolSelection.toolConfig` modifications | Not supported | Qwen shares this response shape today |
| Qwen CLI | Gemini-compatible PascalCase hook names | `hookSpecificOutput.additionalContext` | Same current behavior as Gemini | Not supported | Distinct source for sessions, storage, and telemetry |
| Factory Droid | PascalCase Droid hook names | `additionalContext` on Droid-supported context hooks; no context channel on `PreToolUse` | `PreToolUse.permissionDecision` | Not supported | Current behavior is intentionally standalone, not inherited from Claude |
| AGY | TBD | TBD | TBD | TBD | Fill only after real hook behavior is observed and tested |
| Grok | TBD | TBD | TBD | TBD | Fill only after real hook behavior is observed and tested |

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
4. Keep AGY and Grok documentation as `TBD` until backed by code and tests.

A broader universal response-translation layer is future work. The current
slice records today's provider facts so that future extraction has a stable
contract instead of another set of hard-coded special cases.
