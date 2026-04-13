import { useState, useEffect, useCallback, useRef } from 'react'

export interface UsageTotals {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  session_count: number
}

export interface UsageData {
  hours: number
  totals: UsageTotals
  by_source: Record<string, UsageTotals>
  by_model: Record<string, UsageTotals>
}

export function useUsage(hours: number, projectId?: string) {
  const [data, setData] = useState<UsageData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const fetchUsage = useCallback(async () => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    try {
      let url = `/api/admin/usage?hours=${hours}`
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
    fetchUsage()
      .catch((err) => { console.error('Usage fetch failed:', err) })
      .finally(() => setIsLoading(false))
    const interval = setInterval(fetchUsage, 30_000)
    return () => {
      clearInterval(interval)
      abortRef.current?.abort()
    }
  }, [fetchUsage])

  return { data, isLoading, error }
}
