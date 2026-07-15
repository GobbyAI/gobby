# Review resolution: web hooks (`web/src/hooks/`)

- **Original review:** commit `b2b7d283b` on branch `0.5.0`
- **Remediation epic:** `#16967`
- **Residual sweep:** `#16905`
- **Status:** no open residuals in the scope of `#16905`

## Current residual verification

The original review described the pre-extraction `useChat` implementation and
referenced files and line numbers that no longer represent the active transport
and action-control code. Its resolved findings have been removed instead of
remaining as an inaccurate current defect list.

The final reproducible residuals were verified against the extracted hook
modules and fixed as follows:

- `web/src/hooks/useChat/handlers.ts` refreshes tool name, server, and derived
  tool type when a later status frame supplies authoritative metadata.
- `web/src/hooks/useChat/sessionViewing.ts` clears the transcript loading state
  when a pending viewed-session request is abandoned.
- `web/src/hooks/useChat/transportProxyEvents.ts` correlates delivery notices
  with the pending client message so an unrelated acknowledgement cannot clear
  the active queued-message notice.
- `web/src/hooks/useChat/transportConversationEvents.ts` applies a remote
  `chat_cleared` event to the matching active conversation and resets its
  streaming state.

Focused coverage lives in:

- `web/src/hooks/__tests__/useChat.streaming.test.ts`
- `web/src/hooks/__tests__/useChat.proxy.test.ts`
- `web/src/hooks/__tests__/useChat.sessions.test.ts`
- `web/src/hooks/__tests__/useChat.messages.test.ts`

The original full audit remains available at commit `b2b7d283b`; the `#16967`
task and commit history record its separately remediated findings. This current
record is intentionally limited to the residuals assigned to `#16905` and does
not assert the status of findings outside that scope.
