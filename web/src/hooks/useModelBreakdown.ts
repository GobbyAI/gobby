import { useMemo } from 'react'

import { useUsage } from './useUsage'
import type { ModelBreakdown } from '../types/tokens'

export function useModelBreakdown(hours: number, projectId?: string) {
  const usage = useUsage(hours, projectId)

  const data = useMemo<ModelBreakdown[]>(() => {
    const byModel = usage.data?.by_model ?? {}
    const totalTokens = Object.values(byModel).reduce(
      (sum, entry) => sum + entry.input_tokens + entry.output_tokens,
      0,
    )

    return Object.entries(byModel)
      .map(([family, entry]) => {
        const spent = entry.input_tokens + entry.output_tokens
        return {
          family,
          inputTokens: entry.input_tokens,
          outputTokens: entry.output_tokens,
          cacheReadTokens: entry.cache_read_tokens,
          cacheCreationTokens: entry.cache_creation_tokens,
          sessionCount: entry.session_count,
          totalTokens: spent,
          percentage: totalTokens > 0 ? (spent / totalTokens) * 100 : 0,
          models: [],
        }
      })
      .sort((a, b) => b.totalTokens - a.totalTokens)
  }, [usage.data])

  return {
    data,
    isLoading: usage.isLoading,
    error: usage.error,
  }
}
