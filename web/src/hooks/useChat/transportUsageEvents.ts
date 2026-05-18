import {
  buildContextUsageFromTotals,
  type SessionUsageUpdatedMessage,
  type TokenEventMessage,
} from "./core";
import type { UseChatTransportParams } from "./transportTypes";

export function handleSessionUsageUpdated(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const update = data as unknown as SessionUsageUpdatedMessage;
  const visibleSessionId = ctx.viewingSessionIdRef.current ?? ctx.dbSessionIdRef.current;
  if (update.session_id === visibleSessionId) {
    ctx.markSessionUsageFresh(update.session_id, update.updated_at);
    ctx.setContextUsage((prev) =>
      buildContextUsageFromTotals({
        totalInputTokens: update.usage_input_tokens ?? prev.totalInputTokens,
        outputTokens: update.usage_output_tokens ?? prev.outputTokens,
        cacheReadTokens: update.usage_cache_read_tokens ?? prev.cacheReadTokens,
        cacheCreationTokens:
          update.usage_cache_creation_tokens ?? prev.cacheCreationTokens,
        contextWindow:
          typeof update.context_window === "number"
            ? update.context_window
            : prev.contextWindow,
      }),
    );
  }
  if (ctx.viewingSessionIdRef.current === update.session_id) {
    ctx.setViewingSessionMeta((prev) =>
      prev
        ? {
            ...prev,
            model: typeof update.model === "string" ? update.model : prev.model,
            contextWindow:
              typeof update.context_window === "number"
                ? update.context_window
                : prev.contextWindow,
          }
        : prev,
    );
  } else if (ctx.dbSessionIdRef.current === update.session_id) {
    ctx.setMainSessionMeta((prev) =>
      prev
        ? {
            ...prev,
            model: typeof update.model === "string" ? update.model : prev.model,
            contextWindow:
              typeof update.context_window === "number"
                ? update.context_window
                : prev.contextWindow,
          }
        : prev,
    );
  }
}

export function handleTokenEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const eventData = data as unknown as TokenEventMessage;
  const visibleSessionId = ctx.viewingSessionIdRef.current ?? ctx.dbSessionIdRef.current;
  const sessionTotals = eventData.session_totals;
  if (eventData.session_id === visibleSessionId && sessionTotals) {
    ctx.markSessionUsageFresh(eventData.session_id, eventData.event_at);
    ctx.setContextUsage((prev) =>
      buildContextUsageFromTotals({
        totalInputTokens: sessionTotals.input_tokens ?? prev.totalInputTokens,
        outputTokens: sessionTotals.output_tokens ?? prev.outputTokens,
        cacheReadTokens: sessionTotals.cache_read_tokens ?? prev.cacheReadTokens,
        cacheCreationTokens:
          sessionTotals.cache_creation_tokens ?? prev.cacheCreationTokens,
        contextWindow:
          typeof eventData.context_window === "number"
            ? eventData.context_window
            : prev.contextWindow,
      }),
    );
  }
}
