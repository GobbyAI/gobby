import { useCallback, useEffect, useRef } from 'react'
import { cn } from '../../../lib/utils'

interface ResizeHandleProps {
  onResize: (value: number) => void
  panelWidth?: number
  panelHeight?: number
  minWidth?: number
  maxWidth?: number
  minHeight?: number
  maxHeight?: number
  direction?: 'horizontal' | 'vertical'
  horizontalAnchor?: 'left' | 'right'
}

export function ResizeHandle({
  onResize,
  panelWidth,
  panelHeight,
  minWidth = 300,
  maxWidth = 1400,
  minHeight = 20,
  maxHeight = 80,
  direction = 'horizontal',
  horizontalAnchor = 'right',
}: ResizeHandleProps) {
  const isVertical = direction === 'vertical'
  const currentValue = isVertical ? (panelHeight ?? 40) : (panelWidth ?? 600)
  const minVal = isVertical ? minHeight : minWidth
  const maxVal = isVertical ? maxHeight : maxWidth
  const horizontalDeltaMultiplier = horizontalAnchor === 'left' ? 1 : -1

  const isDragging = useRef(false)
  const startPos = useRef(0)
  const startValue = useRef(0)
  const containerRef = useRef<HTMLDivElement>(null)

  const cleanupRef = useRef<(() => void) | null>(null)

  const startDrag = useCallback((clientPos: number) => {
    isDragging.current = true
    startPos.current = clientPos
    startValue.current = currentValue

    const handleMove = (pos: number) => {
      if (!isDragging.current) return
      if (isVertical) {
        // For vertical: compute percentage delta relative to parent height
        const parent = containerRef.current?.parentElement
        if (!parent) return
        const parentHeight = parent.getBoundingClientRect().height
        const deltaPercent = ((pos - startPos.current) / parentHeight) * 100
        const newHeight = Math.max(minVal, Math.min(maxVal, startValue.current + deltaPercent))
        onResize(newHeight)
      } else {
        const delta = (pos - startPos.current) * horizontalDeltaMultiplier
        const newWidth = Math.max(minVal, Math.min(maxVal, startValue.current + delta))
        onResize(newWidth)
      }
    }

    const handleMouseMove = (ev: MouseEvent) => handleMove(isVertical ? ev.clientY : ev.clientX)
    const handleTouchMove = (ev: TouchEvent) => { ev.preventDefault(); handleMove(isVertical ? ev.touches[0].clientY : ev.touches[0].clientX) }

    const handleEnd = () => {
      isDragging.current = false
      cleanupRef.current = null
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleEnd)
      document.removeEventListener('touchmove', handleTouchMove)
      document.removeEventListener('touchend', handleEnd)
    }

    cleanupRef.current = handleEnd

    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleEnd)
    document.addEventListener('touchmove', handleTouchMove, { passive: false })
    document.addEventListener('touchend', handleEnd)
  }, [onResize, currentValue, minVal, maxVal, isVertical, horizontalDeltaMultiplier])

  // Cleanup drag listeners if component unmounts mid-drag
  useEffect(() => {
    return () => { cleanupRef.current?.() }
  }, [])

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    startDrag(isVertical ? e.clientY : e.clientX)
  }, [startDrag, isVertical])

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    e.preventDefault()
    startDrag(isVertical ? e.touches[0].clientY : e.touches[0].clientX)
  }, [startDrag, isVertical])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    const step = e.shiftKey ? (isVertical ? 10 : 50) : (isVertical ? 2 : 10)
    if (isVertical) {
      if (e.key === 'ArrowDown') { e.preventDefault(); onResize(Math.min(maxVal, currentValue + step)) }
      if (e.key === 'ArrowUp') { e.preventDefault(); onResize(Math.max(minVal, currentValue - step)) }
    } else {
      const increaseKey = horizontalAnchor === 'left' ? 'ArrowRight' : 'ArrowLeft'
      const decreaseKey = horizontalAnchor === 'left' ? 'ArrowLeft' : 'ArrowRight'
      if (e.key === increaseKey) { e.preventDefault(); onResize(Math.min(maxVal, currentValue + step)) }
      if (e.key === decreaseKey) { e.preventDefault(); onResize(Math.max(minVal, currentValue - step)) }
    }
  }, [onResize, currentValue, minVal, maxVal, isVertical, horizontalAnchor])

  return (
    <div
      ref={containerRef}
      className={cn(
        isVertical
          ? 'h-1 cursor-row-resize hover:bg-accent/50 active:bg-accent transition-colors shrink-0'
          : 'w-1 cursor-col-resize hover:bg-accent/50 active:bg-accent transition-colors shrink-0',
        'relative group',
        'focus-visible:outline-none focus-visible:bg-accent'
      )}
      onMouseDown={handleMouseDown}
      onTouchStart={handleTouchStart}
      onKeyDown={handleKeyDown}
      tabIndex={0}
      role="separator"
      aria-orientation={isVertical ? 'horizontal' : 'vertical'}
      aria-valuenow={currentValue}
      aria-valuemin={minVal}
      aria-valuemax={maxVal}
      aria-label="Resize panel"
    >
      {isVertical
        ? <div className="absolute inset-x-0 -top-1 -bottom-1" />
        : <div className="absolute inset-y-0 -left-1 -right-1" />
      }
    </div>
  )
}
