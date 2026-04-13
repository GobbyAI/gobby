import { useState, useEffect, useCallback, useRef } from 'react'

export interface SavingsData {
  days: number
  total_tokens_saved: number
  total_events: number
  categories: Record<string, {
    tokens_saved: number
    event_count: number
  }>
}

export function useSavings(hours: number, projectId?: string) {
  const [data, setData] = useState<SavingsData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const fetchSavings = useCallback(async () => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    try {
      let url = `/api/admin/savings?hours=${hours}`
      if (projectId) url += `&project_id=${encodeURIComponent(projectId)}`
      const resp = await fetch(url, { signal: controller.signal })
      if (resp.ok) {
        setData(await resp.json())
        setError(null)
      } else {
        setError(`HTTP ${resp.status}`)
        setData(null)
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return
      setError(String(e))
      setData(null)
    }
  }, [hours, projectId])

  useEffect(() => {
    setIsLoading(true)
    fetchSavings().then(() => setIsLoading(false))
    const interval = setInterval(fetchSavings, 30_000)
    return () => {
      clearInterval(interval)
      abortRef.current?.abort()
    }
  }, [fetchSavings])

  return { data, isLoading, error }
}
