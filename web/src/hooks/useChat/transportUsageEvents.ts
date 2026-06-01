import {
  buildContextUsageFromTotals,
  computeContextUsageFromSessionData,
} from "./contextUsage";
import type {
  RawSessionUsageUpdatedMessage,
  RawTokenEventMessage,
  SessionUsageUpdatedMessage,
  TokenEventMessage,
} from "./transportEventTypes";
import type { UseChatTransportParams } from "./transportTypes";
import type { ContextUsage } from "../../types/chat";

function numericValue(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalNumeric(value: unknown): boolean {
  return value === undefined || value === null || numericValue(value) !== null;
}

function normalizedOptionalNumber(value: unknown): number | null | undefined {
  if (value === undefined || value === null) return value;
  return numericValue(value);
}

function omitUndefined<T extends object>(value: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(value).filter(([, fieldValue]) => fieldValue !== undefined),
  ) as Partial<T>;
}

function isRawSessionUsageUpdatedMessage(
  data: unknown,
): data is RawSessionUsageUpdatedMessage {
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
    ].every(isOptionalNumeric) &&
    (
      data.context_usage_source === undefined ||
      data.context_usage_source === null ||
      typeof data.context_usage_source === "string"
    ) &&
    (
      data.context_usage_confidence === undefined ||
      data.context_usage_confidence === null ||
      typeof data.context_usage_confidence === "string"
    )
  );
}

function isRawTokenEventMessage(data: unknown): data is RawTokenEventMessage {
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
  if (!numericFields.every(isOptionalNumeric)) {
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
    data.session_totals.context_window,
  ].every((value) => value === undefined || numericValue(value) !== null);
}

export function normalizeSessionUsageUpdatedMessage(
  data: unknown,
): SessionUsageUpdatedMessage | null {
  if (!isRawSessionUsageUpdatedMessage(data)) return null;
  return {
    type: "session_usage_updated",
    session_id: data.session_id,
    project_id: typeof data.project_id === "string" ? data.project_id : null,
    model: typeof data.model === "string" ? data.model : null,
    context_window: normalizedOptionalNumber(data.context_window),
    context_used_tokens: normalizedOptionalNumber(data.context_used_tokens),
    context_usage_ratio: normalizedOptionalNumber(data.context_usage_ratio),
    context_usage_source:
      typeof data.context_usage_source === "string" ? data.context_usage_source : null,
    context_usage_confidence:
      typeof data.context_usage_confidence === "string"
        ? data.context_usage_confidence
        : null,
    last_prompt_input_tokens: normalizedOptionalNumber(data.last_prompt_input_tokens),
    last_prompt_uncached_input_tokens: normalizedOptionalNumber(
      data.last_prompt_uncached_input_tokens,
    ),
    last_prompt_cache_read_tokens: normalizedOptionalNumber(
      data.last_prompt_cache_read_tokens,
    ),
    last_prompt_cache_creation_tokens: normalizedOptionalNumber(
      data.last_prompt_cache_creation_tokens,
    ),
    last_completion_output_tokens: normalizedOptionalNumber(
      data.last_completion_output_tokens,
    ),
    usage_input_tokens: normalizedOptionalNumber(data.usage_input_tokens),
    usage_output_tokens: normalizedOptionalNumber(data.usage_output_tokens),
    usage_cache_creation_tokens: normalizedOptionalNumber(
      data.usage_cache_creation_tokens,
    ),
    usage_cache_read_tokens: normalizedOptionalNumber(data.usage_cache_read_tokens),
    updated_at: data.updated_at,
  };
}

function normalizeSessionTotals(
  totals: RawTokenEventMessage["session_totals"],
): TokenEventMessage["session_totals"] {
  if (!totals) return undefined;
  return {
    input_tokens: normalizedOptionalNumber(totals.input_tokens) ?? undefined,
    output_tokens: normalizedOptionalNumber(totals.output_tokens) ?? undefined,
    cache_creation_tokens:
      normalizedOptionalNumber(totals.cache_creation_tokens) ?? undefined,
    cache_read_tokens: normalizedOptionalNumber(totals.cache_read_tokens) ?? undefined,
    context_window: normalizedOptionalNumber(totals.context_window) ?? undefined,
  };
}

export function normalizeTokenEventMessage(data: unknown): TokenEventMessage | null {
  if (!isRawTokenEventMessage(data)) return null;
  return {
    type: "token_event",
    session_id: data.session_id,
    project_id: typeof data.project_id === "string" ? data.project_id : null,
    message_id: typeof data.message_id === "string" ? data.message_id : null,
    source: typeof data.source === "string" ? data.source : null,
    origin: typeof data.origin === "string" ? data.origin : null,
    event_at: data.event_at,
    model: typeof data.model === "string" ? data.model : null,
    model_family: typeof data.model_family === "string" ? data.model_family : null,
    input_tokens: normalizedOptionalNumber(data.input_tokens),
    output_tokens: normalizedOptionalNumber(data.output_tokens),
    cache_creation_tokens: normalizedOptionalNumber(data.cache_creation_tokens),
    cache_read_tokens: normalizedOptionalNumber(data.cache_read_tokens),
    context_window: normalizedOptionalNumber(data.context_window),
    session_totals: normalizeSessionTotals(data.session_totals),
  };
}

function hasNormalizedContextPayload(data: SessionUsageUpdatedMessage): boolean {
  if (data.last_completion_output_tokens !== undefined) return true;
  return [
    data.context_used_tokens,
    data.context_usage_ratio,
    data.context_usage_source,
    data.context_usage_confidence,
    data.last_prompt_input_tokens,
    data.last_prompt_uncached_input_tokens,
    data.last_prompt_cache_read_tokens,
    data.last_prompt_cache_creation_tokens,
  ].some((value) => value !== undefined && value !== null);
}

function hasExistingNormalizedSnapshot(prev: ContextUsage): boolean {
  if (prev.contextUsageSource) {
    return prev.contextUsageSource !== "web_chat";
  }
  return Boolean(prev.contextUsageConfidence && prev.contextUsageRatio != null);
}

function previousUsagePayload(prev: ContextUsage): Record<string, unknown> {
  if (hasExistingNormalizedSnapshot(prev)) {
    return {
      context_used_tokens: prev.totalInputTokens,
      last_prompt_input_tokens: prev.totalInputTokens,
      last_prompt_uncached_input_tokens: prev.uncachedInputTokens,
      last_prompt_cache_read_tokens: prev.cacheReadTokens,
      last_prompt_cache_creation_tokens: prev.cacheCreationTokens,
      last_completion_output_tokens: prev.outputTokens,
      context_window: prev.contextWindow,
      context_usage_ratio: prev.contextUsageRatio,
      context_usage_source: prev.contextUsageSource,
      context_usage_confidence: prev.contextUsageConfidence,
    };
  }

  return {
    usage_input_tokens: prev.totalInputTokens,
    usage_output_tokens: prev.outputTokens,
    usage_cache_read_tokens: prev.cacheReadTokens,
    usage_cache_creation_tokens: prev.cacheCreationTokens,
    context_window: prev.contextWindow,
    context_usage_ratio: prev.contextUsageRatio,
    context_usage_source: prev.contextUsageSource,
    context_usage_confidence: prev.contextUsageConfidence,
  };
}

export function handleSessionUsageUpdated(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const update = normalizeSessionUsageUpdatedMessage(data);
  if (!update) return;
  const visibleSessionId = ctx.viewingSessionIdRef.current ?? ctx.dbSessionIdRef.current;
  if (update.session_id === visibleSessionId) {
    ctx.markSessionUsageFresh(update.session_id, update.updated_at);
    ctx.setContextUsage((prev) =>
      computeContextUsageFromSessionData({
        ...previousUsagePayload(prev),
        ...omitUndefined(update),
        ...(update.last_completion_output_tokens !== undefined
          ? { last_completion_output_tokens: update.last_completion_output_tokens }
          : {}),
        ...(hasExistingNormalizedSnapshot(prev) && !hasNormalizedContextPayload(update)
          ? {
              context_used_tokens: prev.totalInputTokens,
              last_prompt_input_tokens: prev.totalInputTokens,
              last_prompt_uncached_input_tokens: prev.uncachedInputTokens,
              last_prompt_cache_read_tokens: prev.cacheReadTokens,
              last_prompt_cache_creation_tokens: prev.cacheCreationTokens,
              last_completion_output_tokens: prev.outputTokens,
              context_usage_ratio: prev.contextUsageRatio,
              context_usage_source: prev.contextUsageSource,
              context_usage_confidence: prev.contextUsageConfidence,
            }
          : {}),
      }),
    );
  }
  if (ctx.viewingSessionIdRef.current === update.session_id) {
    ctx.setViewingSessionMeta((prev) =>
      prev
        ? {
            ...prev,
            model: typeof update.model === "string" ? update.model : prev.model,
            contextWindow: update.context_window ?? prev.contextWindow,
          }
        : prev,
    );
  } else if (ctx.dbSessionIdRef.current === update.session_id) {
    ctx.setMainSessionMeta((prev) =>
      prev
        ? {
            ...prev,
            model: typeof update.model === "string" ? update.model : prev.model,
            contextWindow: update.context_window ?? prev.contextWindow,
          }
        : prev,
    );
  }
}

export function handleTokenEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const eventData = normalizeTokenEventMessage(data);
  if (!eventData) return;
  const visibleSessionId = ctx.viewingSessionIdRef.current ?? ctx.dbSessionIdRef.current;
  if (eventData.session_id === visibleSessionId) {
    const totals = eventData.session_totals;
    ctx.markSessionUsageFresh(eventData.session_id, eventData.event_at);
    ctx.setContextUsage((prev) =>
      buildContextUsageFromTotals({
        totalInputTokens:
          totals?.input_tokens ?? eventData.input_tokens ?? prev.totalInputTokens,
        outputTokens: totals?.output_tokens ?? eventData.output_tokens ?? prev.outputTokens,
        cacheReadTokens:
          totals?.cache_read_tokens ?? eventData.cache_read_tokens ?? prev.cacheReadTokens,
        cacheCreationTokens:
          totals?.cache_creation_tokens ??
          eventData.cache_creation_tokens ??
          prev.cacheCreationTokens,
        contextWindow: totals?.context_window ?? eventData.context_window ?? prev.contextWindow,
        contextUsageSource: "token_event",
        contextUsageConfidence: "reported",
      }),
    );
  }
}
