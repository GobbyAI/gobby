export interface ActivityFilterFooterProps {
  onReset: () => void
  onApply: () => void
  resetDisabled?: boolean
  applyLabel?: string
}

export function ActivityFilterFooter({
  onReset,
  onApply,
  resetDisabled,
  applyLabel = 'Apply',
}: ActivityFilterFooterProps) {
  return (
    <div
      className="flex items-center justify-between border-t border-border px-2 py-1.5"
      style={{ background: 'var(--bg-secondary)' }}
    >
      <button
        type="button"
        className="btn btn-accent btn-sm"
        onClick={onReset}
        disabled={resetDisabled}
      >
        Reset
      </button>
      <button type="button" className="btn btn-accent btn-sm" onClick={onApply}>
        {applyLabel}
      </button>
    </div>
  )
}
