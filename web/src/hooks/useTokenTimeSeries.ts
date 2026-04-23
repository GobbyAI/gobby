import { useState, useEffect, useCallback, useRef } from 'react'
import type { TimeSeriesGranularity } from '../types/tokens'

export interface TokenBucket {
  timestamp: string
  tokens_spent: number
  tokens_saved: number
}

export interface TokenTimeSeriesData {
  hours: number
  granularity?: TimeSeriesGranularity
  buckets: TokenBucket[]
}

export function useTokenTimeSeries(
  hours: number,
  projectId?: string,
  granularity: TimeSeriesGranularity = '1h',
) {
  const [data, setData] = useState<TokenTimeSeriesData | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)

  const fetchData = useCallback(async () => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    try {
      let url = `/api/admin/tokens/timeseries?hours=${encodeURIComponent(hours)}&granularity=${encodeURIComponent(granularity)}`
      if (projectId) url += `&project_id=${encodeURIComponent(projectId)}`
      const resp = await fetch(url, { signal: controller.signal })
      if (!mountedRef.current) return
      if (resp.ok) {
        setData(await resp.json())
        setError(null)
      } else {
        setError(`HTTP ${resp.status}`)
        setData(null)
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return
      if (!mountedRef.current) return
      setError(String(e))
      setData(null)
    }
  }, [granularity, hours, projectId])

  useEffect(() => {
    mountedRef.current = true
    setIsLoading(true)
    fetchData()
      .catch((err) => { console.error('Token timeseries fetch failed:', err) })
      .finally(() => {
        if (mountedRef.current) setIsLoading(false)
      })
    const interval = setInterval(fetchData, 30_000)
    return () => {
      mountedRef.current = false
      clearInterval(interval)
      abortRef.current?.abort()
    }
  }, [fetchData])

  return { data, isLoading, error }
}
