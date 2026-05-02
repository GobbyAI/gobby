import { useState, useEffect, useCallback } from 'react'
import type { Channel, CommsMessage, MessageFilters } from '../../hooks/useIntegrations'
import { PlatformIcon } from './IntegrationsPage'
import type { ChannelType } from '../../hooks/useIntegrations'
import { FILTER_SELECT_CLS, FORM_CANCEL_CLS, MESSAGE_FILTER_BAR_CLS } from './styles'
import { cn } from '../../lib/utils'

const CONTAINER_CLS = 'flex flex-1 flex-col overflow-hidden'

const LIST_CLS = 'flex-1 overflow-y-auto'
const EMPTY_CLS = 'flex flex-1 items-center justify-center p-10 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

const ROW_CLS =
  'cursor-pointer border-b border-[var(--border)] px-3 py-2.5 transition-colors duration-100 hover:bg-[rgba(255,255,255,0.03)]'
const ROW_EXPANDED_CLS = 'bg-[var(--bg-secondary)]'

const SUMMARY_CLS = 'flex items-center gap-2 text-[length:var(--text-xs)]'
const TIMESTAMP_CLS = 'min-w-[50px] whitespace-nowrap text-[var(--text-secondary)]'
const CHANNEL_CLS = 'flex items-center gap-1 whitespace-nowrap text-[var(--text-secondary)]'

const DIRECTION_CLS = 'w-4 text-center font-semibold'
const DIRECTION_INBOUND_CLS = 'text-[var(--color-success-foreground)]'
const DIRECTION_OUTBOUND_CLS = 'text-[var(--color-info)]'

const CONTENT_CLS = 'flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[var(--text-primary)]'

const STATUS_BADGE_CLS = 'whitespace-nowrap rounded-lg px-1.5 py-0.5 text-[length:var(--text-2xs)] font-medium'
const STATUS_BG: Record<string, string> = {
  sent: 'bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]',
  error: 'bg-[var(--color-error-soft)] text-[var(--color-error)]',
  pending: 'bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]',
}

const DETAIL_CLS = 'mt-2.5 border-t border-[var(--border)] pt-2.5'
const FULL_CONTENT_CLS =
  'm-0 mb-2 max-h-[200px] overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--bg-primary)] p-2.5 text-[length:var(--text-xs)]'
const ERROR_CLS =
  'mb-2 rounded bg-[var(--color-error-soft)] px-2.5 py-1.5 text-[length:var(--text-xs)] text-[var(--color-error)]'
const META_CLS = 'flex flex-col gap-1 text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const METADATA_JSON_CLS =
  'mx-0 mt-1 max-h-[150px] overflow-y-auto whitespace-pre-wrap rounded border border-[var(--border)] bg-[var(--bg-primary)] p-2 text-[length:var(--text-2xs)]'

const PAGINATION_CLS = 'flex items-center justify-center gap-3 border-t border-[var(--border)] py-3'
const PAGE_INFO_CLS = 'text-[length:var(--text-xs)] text-[var(--text-secondary)]'

interface MessageListProps {
  channels: Channel[]
  messages: CommsMessage[]
  filters: MessageFilters
  onFiltersChange: (filters: Partial<MessageFilters>) => void
  onFetchMessages: (filters?: Partial<MessageFilters>) => void
}

export function MessageList({ channels, messages, filters, onFiltersChange, onFetchMessages }: MessageListProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null)

  // Fetch on mount and when filters change
  useEffect(() => {
    onFetchMessages()
  }, [filters.channelId, filters.direction, filters.offset, onFetchMessages])

  const handleFilterChange = useCallback((update: Partial<MessageFilters>) => {
    onFiltersChange({ ...update, offset: 0 })
  }, [onFiltersChange])

  const channelMap = new Map(channels.map(c => [c.id, c]))

  const formatTime = (iso: string) => {
    try {
      const d = new Date(iso)
      const now = new Date()
      const diff = now.getTime() - d.getTime()
      const mins = Math.floor(diff / 60000)
      if (mins < 1) return 'just now'
      if (mins < 60) return `${mins}m ago`
      const hours = Math.floor(mins / 60)
      if (hours < 24) return `${hours}h ago`
      const days = Math.floor(hours / 24)
      return `${days}d ago`
    } catch {
      return iso
    }
  }

  const hasMore = messages.length >= filters.limit

  return (
    <div className={CONTAINER_CLS}>
      {/* Filter bar */}
      <div className={MESSAGE_FILTER_BAR_CLS}>
        <select
          className={FILTER_SELECT_CLS}
          value={filters.channelId || ''}
          onChange={e => handleFilterChange({ channelId: e.target.value || null })}
        >
          <option value="">All Channels</option>
          {channels.map(ch => (
            <option key={ch.id} value={ch.id}>{ch.name}</option>
          ))}
        </select>
        <select
          className={FILTER_SELECT_CLS}
          value={filters.direction || ''}
          onChange={e => handleFilterChange({ direction: (e.target.value || null) as MessageFilters['direction'] })}
        >
          <option value="">All Directions</option>
          <option value="inbound">Inbound</option>
          <option value="outbound">Outbound</option>
        </select>
      </div>

      {/* Message list */}
      {messages.length === 0 ? (
        <div className={EMPTY_CLS}>No messages yet</div>
      ) : (
        <div className={LIST_CLS}>
          {messages.map(msg => {
            const ch = channelMap.get(msg.channel_id)
            const isExpanded = expandedId === msg.id

            return (
              <div
                key={msg.id}
                className={cn(ROW_CLS, isExpanded && ROW_EXPANDED_CLS)}
                onClick={() => setExpandedId(isExpanded ? null : msg.id)}
              >
                <div className={SUMMARY_CLS}>
                  <span className={TIMESTAMP_CLS}>{formatTime(msg.created_at)}</span>
                  {ch && (
                    <span className={CHANNEL_CLS}>
                      <PlatformIcon type={ch.channel_type as ChannelType} size={12} />
                      {' '}{ch.name}
                    </span>
                  )}
                  <span className={cn(DIRECTION_CLS, msg.direction === 'inbound' ? DIRECTION_INBOUND_CLS : DIRECTION_OUTBOUND_CLS)}>
                    {msg.direction === 'inbound' ? '↓' : '↑'}
                  </span>
                  <span className={CONTENT_CLS}>
                    {msg.content.length > 120 ? msg.content.slice(0, 120) + '...' : msg.content}
                  </span>
                  <span className={cn(STATUS_BADGE_CLS, STATUS_BG[msg.status] ?? '')}>
                    {msg.status}
                  </span>
                </div>

                {isExpanded && (
                  <div className={DETAIL_CLS}>
                    <pre className={FULL_CONTENT_CLS}>{msg.content}</pre>
                    {msg.error && (
                      <div className={ERROR_CLS}>Error: {msg.error}</div>
                    )}
                    <div className={META_CLS}>
                      {msg.platform_message_id && (
                        <span>Platform ID: {msg.platform_message_id}</span>
                      )}
                      {msg.platform_thread_id && (
                        <span>Thread: {msg.platform_thread_id}</span>
                      )}
                      {msg.session_id && (
                        <span>Session: {msg.session_id}</span>
                      )}
                      {Object.keys(msg.metadata_json).length > 0 && (
                        <details>
                          <summary>Metadata</summary>
                          <pre className={METADATA_JSON_CLS}>
                            {JSON.stringify(msg.metadata_json, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Pagination */}
      {(filters.offset > 0 || hasMore) && (
        <div className={PAGINATION_CLS}>
          <button
            className={FORM_CANCEL_CLS}
            disabled={filters.offset === 0}
            onClick={() => onFiltersChange({ offset: Math.max(0, filters.offset - filters.limit) })}
          >
            Previous
          </button>
          <span className={PAGE_INFO_CLS}>
            Showing {filters.offset + 1}-{filters.offset + messages.length} messages
          </span>
          <button
            className={FORM_CANCEL_CLS}
            disabled={!hasMore}
            onClick={() => onFiltersChange({ offset: filters.offset + filters.limit })}
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
