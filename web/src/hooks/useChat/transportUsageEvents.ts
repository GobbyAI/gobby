import {
  buildContextUsageFromTotals,
  computeContextUsageFromSessionData,
} from "./contextUsage";
import type {
  SessionUsageUpdatedMessage,
  TokenEventMessage,
} from "./transportEventTypes";
import type { UseChatTransportParams } from "./transportTypes";

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function optionalNumber(value: unknown): number | undefined {
  return isFiniteNumber(value) ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSessionUsageUpdatedMessage(
  data: unknown,
): data is SessionUsageUpdatedMessage {
  return (
    isRecord(data) &&
    data.type === "session_usage_updated" &&
    typeof data.session_id === "string" &&
    (data.updated_at === undefined || typeof data.updated_at === "string") &&
    [
      data.usage_input_tokens,
      data.usage_output_tokens,
      data.usage_cache_creation_tokens,
      data.usage_cache_read_tokens,
      data.context_window,
      data.context_used_tokens,
      data.context_usage_ratio,
      data.last_prompt_input_tokens,
      data.last_prompt_uncached_input_tokens,
      data.last_prompt_cache_read_tokens,
      data.last_prompt_cache_creation_tokens,
      data.last_completion_output_tokens,
    ].every((value) => value === undefined || value === null || isFiniteNumber(value))
    && (
      data.context_usage_source === undefined ||
      data.context_usage_source === null ||
      typeof data.context_usage_source === "string"
    )
    && (
      data.context_usage_confidence === undefined ||
      data.context_usage_confidence === null ||
      typeof data.context_usage_confidence === "string"
    )
  );
}

function isTokenEventMessage(data: unknown): data is TokenEventMessage {
  if (
    !isRecord(data) ||
    data.type !== "token_event" ||
    typeof data.session_id !== "string" ||
    typeof data.event_at !== "string"
  ) {
    return false;
  }
  const numericFields = [
    data.input_tokens,
    data.output_tokens,
    data.cache_creation_tokens,
    data.cache_read_tokens,
    data.context_window,
  ];
  if (
    !numericFields.every(
      (value) => value === undefined || value === null || isFiniteNumber(value),
    )
  ) {
    return false;
  }
  if (data.session_totals === undefined || data.session_totals === null) {
    return true;
  }
  if (!isRecord(data.session_totals)) {
    return false;
  }
  return [
    data.session_totals.input_tokens,
    data.session_totals.output_tokens,
    data.session_totals.cache_creation_tokens,
    data.session_totals.cache_read_tokens,
  ].every((value) => value === undefined || isFiniteNumber(value));
}

export function handleSessionUsageUpdated(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  if (!isSessionUsageUpdatedMessage(data)) return;
  const update = data;
  const visibleSessionId = ctx.viewingSessionIdRef.current ?? ctx.dbSessionIdRef.current;
  if (update.session_id === visibleSessionId) {
    ctx.markSessionUsageFresh(update.session_id, update.updated_at);
    ctx.setContextUsage((prev) =>
      computeContextUsageFromSessionData({
        usage_input_tokens: prev.totalInputTokens,
        usage_output_tokens: prev.outputTokens,
        usage_cache_read_tokens: prev.cacheReadTokens,
        usage_cache_creation_tokens: prev.cacheCreationTokens,
        context_window: prev.contextWindow,
        context_usage_ratio: prev.contextUsageRatio,
        context_usage_source: prev.contextUsageSource,
        context_usage_confidence: prev.contextUsageConfidence,
        ...update,
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
  if (!isTokenEventMessage(data)) return;
  const eventData = data;
  const visibleSessionId = ctx.viewingSessionIdRef.current ?? ctx.dbSessionIdRef.current;
  const sessionTotals = eventData.session_totals;
  if (eventData.session_id === visibleSessionId && sessionTotals) {
    ctx.markSessionUsageFresh(eventData.session_id, eventData.event_at);
    ctx.setContextUsage((prev) =>
      buildContextUsageFromTotals({
        totalInputTokens:
          optionalNumber(sessionTotals.input_tokens) ?? prev.totalInputTokens,
        outputTokens:
          optionalNumber(sessionTotals.output_tokens) ?? prev.outputTokens,
        cacheReadTokens:
          optionalNumber(sessionTotals.cache_read_tokens) ?? prev.cacheReadTokens,
        cacheCreationTokens:
          optionalNumber(sessionTotals.cache_creation_tokens) ??
          prev.cacheCreationTokens,
        contextWindow:
          typeof eventData.context_window === "number"
            ? eventData.context_window
            : prev.contextWindow,
      }),
    );
  }
}
