import type { ContextUsage } from './chat'

export type TimeSeriesGranularity = '30m' | '1h' | '1d'

export interface TokenEvent {
  id?: number
  session_id: string
  project_id?: string | null
  message_id?: string | null
  source?: string | null
  origin?: string | null
  model?: string | null
  model_family?: string | null
  input_tokens: number
  output_tokens: number
  cache_creation_tokens: number
  cache_read_tokens: number
  context_window?: number | null
  event_at: string
  created_at?: string
  metadata?: Record<string, unknown> | null
  session_totals?: {
    input_tokens: number
    output_tokens: number
    cache_creation_tokens: number
    cache_read_tokens: number
    context_window?: number | null
  }
}

export interface ModelBreakdownLeaf {
  model: string
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheCreationTokens: number
  sessionCount: number
  totalTokens: number
}

export interface ModelBreakdown {
  family: string
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheCreationTokens: number
  sessionCount: number
  totalTokens: number
  percentage: number
  models: ModelBreakdownLeaf[]
}

export type { ContextUsage }
