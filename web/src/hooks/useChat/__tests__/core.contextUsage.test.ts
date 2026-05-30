import { describe, expect, it } from 'vitest'

import { computeContextUsageFromSessionData } from '../core'

describe('computeContextUsageFromSessionData', () => {
  it('prefers normalized snapshot fields over cumulative legacy usage', () => {
    const usage = computeContextUsageFromSessionData({
      context_window: 200_000,
      context_used_tokens: 140_000,
      context_usage_ratio: 0.7,
      context_usage_source: 'codex',
      context_usage_confidence: 'reported',
      last_prompt_input_tokens: 140_000,
      last_prompt_uncached_input_tokens: 10_000,
      last_prompt_cache_read_tokens: 120_000,
      last_prompt_cache_creation_tokens: 10_000,
      last_completion_output_tokens: 600,
      usage_input_tokens: 1_000,
      usage_output_tokens: 2_000,
      usage_cache_read_tokens: 3_000,
      usage_cache_creation_tokens: 4_000,
    })

    expect(usage.totalInputTokens).toBe(140_000)
    expect(usage.outputTokens).toBe(600)
    expect(usage.uncachedInputTokens).toBe(10_000)
    expect(usage.cacheReadTokens).toBe(120_000)
    expect(usage.cacheCreationTokens).toBe(10_000)
    expect(usage.contextUsageRatio).toBe(0.7)
    expect(usage.contextUsageSource).toBe('codex')
    expect(usage.contextUsageConfidence).toBe('reported')
  })

  it('recomputes context ratio from corrected session context window', () => {
    const usage = computeContextUsageFromSessionData({
      context_window: 258_400,
      usage_input_tokens: 129_200,
      usage_output_tokens: 400,
      usage_cache_read_tokens: 0,
      usage_cache_creation_tokens: 0,
    })

    expect(usage.contextWindow).toBe(258_400)
    expect(usage.contextUsageRatio).toBe(0.5)
  })
})
