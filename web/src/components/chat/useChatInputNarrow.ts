import { useEffect, useState, type RefObject } from 'react'

export function useChatInputNarrow(metaRef: RefObject<HTMLDivElement>) {
  const [isNarrow, setIsNarrow] = useState(false)

  useEffect(() => {
    const el = metaRef.current?.closest('.chat-column')
    if (!el) return
    setIsNarrow(el.getBoundingClientRect().width <= 479)
    if (typeof ResizeObserver === 'undefined') return
    const resizeObserver = new ResizeObserver(([entry]) => {
      setIsNarrow(entry.contentRect.width <= 479)
    })
    resizeObserver.observe(el)
    return () => resizeObserver.disconnect()
  }, [metaRef])

  return isNarrow
}
