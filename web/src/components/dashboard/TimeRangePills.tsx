import { SegmentedControl } from '../ui/SegmentedControl'
import type { TimeRange } from './timeRangeUtils'

const RANGES: { value: TimeRange; label: string }[] = [
  { value: '1h', label: '1h' },
  { value: '6h', label: '6h' },
  { value: '12h', label: '12h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All' },
]

interface Props {
  value: TimeRange
  onChange: (range: TimeRange) => void
}

export function TimeRangePills({ value, onChange }: Props) {
  return (
    <SegmentedControl<TimeRange>
      value={value}
      onChange={onChange}
      options={RANGES}
      ariaLabel="Time range"
    />
  )
}
