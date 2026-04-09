import { useEffect, useState } from 'react'

export function useNow(updateIntervalMs = 60_000): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (updateIntervalMs <= 0) return

    const intervalId = window.setInterval(() => {
      setNow(Date.now())
    }, updateIntervalMs)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [updateIntervalMs])

  return now
}
