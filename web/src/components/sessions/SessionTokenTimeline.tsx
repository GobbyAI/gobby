import { useMemo } from 'react'
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis } from 'recharts'

import type { TokenEvent } from '../../types/tokens'
import { formatTokens } from '../../utils/formatTime'

interface Props {
  events: TokenEvent[]
}

export function SessionTokenTimeline({ events }: Props) {
  const data = useMemo(
    () =>
      [...events]
        .sort((a, b) => new Date(a.event_at).getTime() - new Date(b.event_at).getTime())
        .map((event) => ({
          timestamp: event.event_at,
          label: new Date(event.event_at).toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
          }),
          input_tokens: event.input_tokens + event.cache_creation_tokens + event.cache_read_tokens,
        })),
    [events],
  )

  if (data.length === 0) {
    return (
      <div className="flex h-[120px] items-center justify-center rounded-md border border-border bg-background/40 text-xs text-muted-foreground">
        No token events yet.
      </div>
    )
  }

  return (
    <div className="rounded-md border border-border bg-background/40 p-2">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-[0.04em] text-muted-foreground">
        Session Input Pressure
      </div>
      <ResponsiveContainer width="100%" height={120}>
        <AreaChart data={data} margin={{ top: 5, right: 4, left: 0, bottom: 0 }}>
          <XAxis dataKey="label" hide />
          <Tooltip
            formatter={(value) => [formatTokens(Number(value)), 'Input tokens']}
            labelFormatter={(label, payload) =>
              payload?.[0]?.payload?.timestamp ? new Date(payload[0].payload.timestamp).toLocaleString() : label
            }
            contentStyle={{
              background: 'var(--bg-secondary)',
              border: '1px solid var(--border)',
              fontSize: 12,
            }}
          />
          <Area
            type="monotone"
            dataKey="input_tokens"
            stroke="var(--accent)"
            fill="var(--accent-soft)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
