import type { ContentBlock, TokenUsage } from '../../types/chat'

export type MessageSource = 'session' | 'chat' | null

export interface SessionMessage {
  id: string
  role: string
  content: string
  content_type?: string
  tool_name?: string
  tool_input?: string
  tool_result?: string
  tool_use_id?: string
  timestamp: string
  message_index?: number
  content_blocks?: ContentBlock[]
  model?: string | null
  usage?: TokenUsage | null
}

export interface TranscriptStatus {
  session_id: string
  live_exists: boolean
  archive_exists: boolean
  availability: 'live' | 'archive_only' | 'missing'
  content_state: 'messages' | 'empty' | 'unparseable' | 'missing'
  session_source?: string | null
  detected_source?: string | null
  source_mismatch: boolean
  raw_record_count: number
  parsed_message_count: number
}

export interface MessageLoadResult {
  mapped: SessionMessage[]
  totalCount: number
  renderedTotal: number
  returnedCount: number
  degradedReason: string | null
  ok: boolean
  status: number
}

export interface LoadSessionDetailOptions {
  showLoading?: boolean
  clearOnError?: boolean
  errorMessage?: string
}
