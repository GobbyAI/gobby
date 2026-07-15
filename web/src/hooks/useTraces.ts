import { useState, useEffect, useCallback, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface TraceRecord {
  id: string
  project_id: string
  trace_id: string
  root_span_name: string
  status: 'OK' | 'ERROR' | 'UNSET'
  start_time_ns: number
  end_time_ns: number
  duration_ms: number
  timestamp: string
}

export interface SpanRecord {
  id: string
  trace_id: string
  span_id: string
  parent_id: string | null
  name: string
  kind: string
  status: 'OK' | 'ERROR' | 'UNSET'
  start_time_ns: number
  end_time_ns: number
  attributes_json: string | null
  events_json: string | null
}

interface TraceFilters {
  status?: string
  session_id?: string
}

export function useTraces(projectId?: string) {
  const [traces, setTraces] = useState<TraceRecord[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filters, setFilters] = useState<TraceFilters>({})
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null)
  const refetchTimerRef = useRef<number | null>(null)

  const fetchTraces = useCallback(async () => {
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    if (filters.status) params.set('status', filters.status)
    if (filters.session_id) params.set('session_id', filters.session_id)

    try {
      const res = await fetch(`/api/traces?${params}`)
      if (res.ok) {
        const data = await res.json()
        setTraces(data.traces || [])
        setError(null)
      } else {
        console.error('Failed to fetch traces:', res.status, res.statusText)
        setError(`Failed to fetch traces (${res.status})`)
      }
    } catch (e) {
      console.error('Failed to fetch traces:', e)
      setError(e instanceof Error ? e.message : 'Failed to fetch traces')
    } finally {
      setIsLoading(false)
    }
  }, [projectId, filters])

  useEffect(() => {
    setIsLoading(true)
    fetchTraces()
    return () => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current)
    }
  }, [fetchTraces])

  useWebSocketEvent('trace_event', useCallback(() => {
    if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current)
    refetchTimerRef.current = window.setTimeout(() => {
      fetchTraces()
    }, 500)
  }, [fetchTraces]))

  return {
    traces,
    isLoading,
    error,
    filters,
    setFilters,
    fetchTraces,
    selectedTraceId,
    setSelectedTraceId,
  }
}

export function useTraceDetail(traceId: string | null) {
  const [spans, setSpans] = useState<SpanRecord[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const refetchTimerRef = useRef<number | null>(null)
  const requestGenerationRef = useRef(0)

  const fetchDetail = useCallback(async () => {
    const requestGeneration = ++requestGenerationRef.current
    if (!traceId) {
      setSpans([])
      setIsLoading(false)
      return
    }
    setIsLoading(true)
    try {
      const res = await fetch(`/api/traces/${encodeURIComponent(traceId)}`)
      if (requestGeneration !== requestGenerationRef.current) return
      if (res.ok) {
        const data = await res.json()
        if (requestGeneration !== requestGenerationRef.current) return
        setSpans(data.spans || [])
      } else {
        console.error('Failed to fetch trace detail:', res.status, res.statusText)
        setSpans([])
      }
    } catch (e) {
      if (requestGeneration !== requestGenerationRef.current) return
      console.error('Failed to fetch trace detail:', e)
    } finally {
      if (requestGeneration === requestGenerationRef.current) setIsLoading(false)
    }
  }, [traceId])

  useEffect(() => {
    fetchDetail()
    return () => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current)
    }
  }, [fetchDetail])

  useWebSocketEvent('trace_event', useCallback((data: any) => {
    if (data?.trace_id === traceId) {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current)
      refetchTimerRef.current = window.setTimeout(() => {
        fetchDetail()
      }, 500)
    }
  }, [traceId, fetchDetail]))

  return {
    spans,
    isLoading,
    fetchDetail,
  }
}
