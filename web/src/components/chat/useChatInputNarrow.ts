import { useEffect, useState, type RefObject } from 'react'

const NARROW_WIDTH_THRESHOLD = 479

export function useChatInputNarrow(metaRef: RefObject<HTMLDivElement>): boolean {
  const [isNarrow, setIsNarrow] = useState(false)

  useEffect(() => {
    const el = metaRef.current?.closest('.chat-column')
    if (!el) return
    setIsNarrow(el.getBoundingClientRect().width <= NARROW_WIDTH_THRESHOLD)
    if (typeof ResizeObserver === 'undefined') return
    const resizeObserver = new ResizeObserver(([entry]) => {
      setIsNarrow(entry.contentRect.width <= NARROW_WIDTH_THRESHOLD)
    })
    resizeObserver.observe(el)
    return () => resizeObserver.disconnect()
  }, [metaRef])

  return isNarrow
}
