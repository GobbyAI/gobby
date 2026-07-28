import { useState } from 'react'
import { SegmentedControl } from '../ui/SegmentedControl'
import { TIERS, type TierId } from './tierPreviewConfig'

const TIER_OPTIONS = (Object.keys(TIERS) as TierId[]).map((id) => ({
  value: id,
  label: TIERS[id].label,
}))

/**
 * Dev-only annotation surface: renders the app in a centered, tier-sized
 * same-origin iframe so in-page annotation tools (Drawbridge) keep the full
 * window for their own UI. Width-based media queries and useIsMobile resolve
 * against the iframe viewport; pointer/hover queries still resolve against
 * the host device, so coarse-pointer styling does not apply in the iframe.
 */
export function TierPreview() {
  const [tier, setTier] = useState<TierId>('portrait')
  const { width, height } = TIERS[tier]
  const fills = width === null

  return (
    <div className="flex h-screen flex-col bg-[var(--bg-primary)] font-[var(--font-sans)]">
      <header className="flex shrink-0 items-center gap-4 border-b border-[var(--border)] px-4 py-2">
        <span className="text-[length:var(--text-base)] font-[var(--font-weight-semibold)] text-[var(--text-primary)]">
          Tier preview
        </span>
        <SegmentedControl
          value={tier}
          onChange={setTier}
          options={TIER_OPTIONS}
          ariaLabel="Preview tier"
          controlHeight="sm"
        />
        <span
          data-testid="tier-size"
          className="font-[var(--font-mono)] text-[length:var(--text-sm)] text-[var(--text-secondary)]"
        >
          {fills ? 'fill' : `${width}×${height}`}
        </span>
      </header>
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-4">
        <iframe
          title="Gobby tier preview"
          src="/"
          data-testid="tier-frame"
          className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]"
          style={
            fills
              ? { width: '100%', height: '100%' }
              : { width: `${width}px`, height: `${height}px` }
          }
        />
      </div>
    </div>
  )
}
