import { useEffect, useRef, useState } from 'react'
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
 *
 * The iframe keeps its exact tier dimensions (transforms do not affect media
 * queries) and is scaled down to fit the stage, so the whole phone frame is
 * always visible without scrolling.
 */
export function TierPreview() {
  const [tier, setTier] = useState<TierId>('portrait')
  const [scale, setScale] = useState(1)
  const stageRef = useRef<HTMLDivElement>(null)
  const { width, height } = TIERS[tier]
  const fills = width === null || height === null

  useEffect(() => {
    const stage = stageRef.current
    if (fills || !stage || typeof ResizeObserver === 'undefined') {
      setScale(1)
      return
    }
    const fit = () => {
      const availW = stage.clientWidth - 32
      const availH = stage.clientHeight - 32
      if (availW <= 0 || availH <= 0) return
      setScale(Math.min(1, availW / width, availH / height))
    }
    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(stage)
    return () => observer.disconnect()
  }, [fills, width, height])

  const shrunk = !fills && scale < 1

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
          {shrunk ? ` @ ${Math.round(scale * 100)}%` : ''}
        </span>
      </header>
      <div
        ref={stageRef}
        className="flex min-h-0 flex-1 items-center justify-center overflow-hidden p-4"
      >
        {fills ? (
          <iframe
            title="Gobby tier preview"
            src="/"
            data-testid="tier-frame"
            className="h-full w-full rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]"
          />
        ) : (
          <div
            data-testid="tier-frame-wrap"
            className="shrink-0"
            style={{ width: width * scale, height: height * scale }}
          >
            <iframe
              title="Gobby tier preview"
              src="/"
              data-testid="tier-frame"
              className="rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]"
              style={{
                width: `${width}px`,
                height: `${height}px`,
                transform: `scale(${scale})`,
                transformOrigin: 'top left',
              }}
            />
          </div>
        )}
      </div>
    </div>
  )
}
