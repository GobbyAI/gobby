import type { ContextUsage } from "../../types/chat";

export function computeContextUsageFromSessionData(
  session: Record<string, unknown> | null,
): ContextUsage {
  const contextWindow = numberOrNull(session?.context_window);
  const normalizedTotalInput =
    numberOrNull(session?.context_used_tokens) ??
    numberOrNull(session?.last_prompt_input_tokens);
  const cacheReadTokens =
    numberOrNull(session?.last_prompt_cache_read_tokens) ??
    numberOrNull(session?.usage_cache_read_tokens) ??
    0;
  const cacheCreationTokens =
    numberOrNull(session?.last_prompt_cache_creation_tokens) ??
    numberOrNull(session?.usage_cache_creation_tokens) ??
    0;
  const legacyTotalInput = numberOrNull(session?.usage_input_tokens) ?? 0;
  const totalInputTokens = normalizedTotalInput ?? legacyTotalInput;
  const outputTokens =
    numberOrNull(session?.last_completion_output_tokens) ??
    numberOrNull(session?.usage_output_tokens) ??
    0;
  const uncachedInputTokens =
    numberOrNull(session?.last_prompt_uncached_input_tokens) ??
    Math.max(0, totalInputTokens - cacheReadTokens - cacheCreationTokens);
  const explicitRatio = numberOrNull(session?.context_usage_ratio);
  const contextUsageRatio =
    explicitRatio != null
      ? clampRatio(explicitRatio)
      : contextWindow && totalInputTokens > 0
        ? clampRatio(totalInputTokens / contextWindow)
        : null;

  return {
    totalInputTokens,
    outputTokens,
    contextWindow,
    uncachedInputTokens,
    cacheReadTokens,
    cacheCreationTokens,
    contextUsageRatio,
    contextUsageSource:
      typeof session?.context_usage_source === "string"
        ? session.context_usage_source
        : null,
    contextUsageConfidence:
      typeof session?.context_usage_confidence === "string"
        ? session.context_usage_confidence
        : null,
  };
}

export function hasSessionUsage(session: Record<string, unknown> | null): boolean {
  if (!session) return false;
  return (
    (typeof session.context_used_tokens === "number" &&
      session.context_used_tokens > 0) ||
    (typeof session.last_prompt_input_tokens === "number" &&
      session.last_prompt_input_tokens > 0) ||
    (typeof session.usage_input_tokens === "number" &&
      session.usage_input_tokens > 0) ||
    (typeof session.usage_output_tokens === "number" &&
      session.usage_output_tokens > 0) ||
    typeof session.context_window === "number"
  );
}

export function buildContextUsageFromTotals(params: {
  totalInputTokens?: number | null;
  outputTokens?: number | null;
  cacheReadTokens?: number | null;
  cacheCreationTokens?: number | null;
  contextWindow?: number | null;
}): ContextUsage {
  const totalInputTokens = params.totalInputTokens ?? 0;
  const outputTokens = params.outputTokens ?? 0;
  const cacheReadTokens = params.cacheReadTokens ?? 0;
  const cacheCreationTokens = params.cacheCreationTokens ?? 0;

  return {
    totalInputTokens,
    outputTokens,
    contextWindow: params.contextWindow ?? null,
    uncachedInputTokens: Math.max(
      0,
      totalInputTokens - cacheReadTokens - cacheCreationTokens,
    ),
    cacheReadTokens,
    cacheCreationTokens,
    contextUsageRatio:
      params.contextWindow && totalInputTokens > 0
        ? clampRatio(totalInputTokens / params.contextWindow)
        : null,
    contextUsageSource: "web_chat",
    contextUsageConfidence: totalInputTokens > 0 ? "reported" : null,
  };
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function clampRatio(value: number): number {
  return Math.max(0, Math.min(1, value));
}
