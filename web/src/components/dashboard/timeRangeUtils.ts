export type TimeRange = '1h' | '6h' | '12h' | '24h' | '7d' | '30d' | 'all'

export function rangeToHours(range: TimeRange): number {
  const map: Record<TimeRange, number> = {
    '1h': 1,
    '6h': 6,
    '12h': 12,
    '24h': 24,
    '7d': 168,
    '30d': 720,
    all: 0,
  }

  return map[range]
}
