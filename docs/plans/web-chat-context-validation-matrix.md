# Web Chat Context Validation Matrix

Date: 2026-04-15

Task refs: `#11764`, `#11766`

## Scope

- Session types: server-owned web chat, observed tmux session, attached tmux session
- Providers/models exercised in automated coverage: Claude/Sonnet, Codex/GPT-5.4, canonical Claude model ids via provider-model normalization
- Runtime modes: APC and app-server share the same hydrated session payload shape once data reaches the web client
- Flows: fresh chat, continue in chat, hard reload, observe, attach, swap

## Source Of Truth

The web client gets context/token usage from three places:

1. Live web chat turns use the WebSocket `done` message `usage` payload plus `context_window`.
2. Observed or attached terminal sessions hydrate from `attach_to_session_result` fields such as `usage_input_tokens`, `usage_output_tokens`, `usage_cache_*`, and `context_window`.
3. Restored read-only session views hydrate from `GET /api/sessions/:id`, which persists the same usage and context-window fields in session storage.

That means the context pie chart and tooltip are only as correct as the persisted session record or the attach result currently feeding `useChat`.

## Matrix

| Flow | Session shape | Evidence | Status | Notes / gaps |
| --- | --- | --- | --- | --- |
| Fresh response updates context usage | Web chat | `web/src/hooks/__tests__/useChat.test.ts` asserts `done` message usage hydration | Pass | Current automated fixture uses Codex/GPT-5.4 |
| Continue in chat preserves provider/model | tmux -> web chat | `useChat.test.ts` asserts `continue_in_chat` keeps source provider/model | Pass | Covers resumed-session identity path |
| Hard reload restores watched session without hydrating parked main chat | Observed tmux | `useChat.test.ts` case `restores a watched session on mount without hydrating the parked main chat over it` | Pass | This is the regression for `#11759` |
| Attach result hydrates usage without changing main chat identity | Attached tmux | `useChat.test.ts` case `hydrates context usage from attach_to_session_result without changing the main chat id` | Pass | This is the core parity path behind `#11765` |
| Swap keeps terminal sessions read-only until resume/attach | Observed tmux | `web/src/components/chat/__tests__/ChatPage.test.tsx` verifies read-only swapped terminal behavior | Pass | Prevents misleading editable state |
| Disabled input copy reflects observe/resume state | Observed tmux | `ChatPage.test.tsx` and `ChatInput.test.tsx` cover read-only and unavailable copy | Pass | Fixes stale generic `Connecting...` fallback from `#11769` |
| Observing bar shows session-scoped provider/model/mode metadata | Observed tmux | `AgentStatusBar.test.tsx` covers provider badge, model, and explicit `Mode:` label | Pass | Fixes remaining clarity/styling work from `#11768` |
| Pie chart percentage and tooltip breakdown match hydrated values | Shared UI surface | `ContextUsageIndicator.test.tsx` covers percentage and tooltip math | Pass | Uses the same component for all chat shapes |
| APC vs app-server runtime split | All | Code inspection of `useChat` hydration paths | Partial | No explicit front-end test parameterized by runtime mode; no distinct client branch found after hydration |
| Gemini / Qwen fixture coverage | All | Current web tests | Partial | Provider-model normalization and picker coverage exist, but usage-hydration fixtures are still Claude/Codex-heavy |

## Existing Task Mapping

- `#11759`: already covered by the watched-session reload regression test and current restore logic
- `#11765`: already covered by attach/result usage hydration and persisted session usage paths
- `#11768`: observing-bar clarity/styling issue, not a token math defect
- `#11769`: disabled-input copy issue, not a token math defect

## Follow-Up

- No new user-facing bug reproduced from this matrix.
- The remaining gap is coverage breadth, not a known broken path:
  - add runtime-parameterized tests if APC and app-server start diverging at the payload layer
  - add Gemini/Qwen usage-hydration fixtures if provider-specific accounting differences show up later
