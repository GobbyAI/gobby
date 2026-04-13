import { useState, useEffect, useCallback, useRef } from 'react'

export interface TmuxSession {
  name: string
  socket: string
  pane_pid: number | null
  pane_dead: boolean
  pane_title: string | null
  window_name: string | null
  session_title: string | null
  gobby_session_id: string | null
  agent_managed: boolean
  agent_run_id: string | null
  attached_bridge: string | null
}

interface TmuxSessionsResult {
  sessions: TmuxSession[]
  liveCliSessionIds: string[]
  attachedSession: string | null
  streamingId: string | null
  isLoading: boolean
  sessionEnded: boolean
  attachSession: (sessionName: string, socket: string) => void
  detachSession: () => void
  refreshTerminal: (sessionName: string, socket: string) => void
  createSession: (name?: string, socket?: string) => void
  killSession: (sessionName: string, socket: string) => void
  refreshSessions: () => void
  dismissEndedSession: () => void
  sendInput: (data: string) => void
  resizeTerminal: (rows: number, cols: number) => void
  onOutput: (callback: (runId: string, data: string) => void) => void
}

export function useTmuxSessions(): TmuxSessionsResult {
  const [sessions, setSessions] = useState<TmuxSession[]>([])
  const [liveCliSessionIds, setLiveCliSessionIds] = useState<string[]>([])
  const [attachedSession, setAttachedSession] = useState<string | null>(null)
  const [streamingId, setStreamingId] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [sessionEnded, setSessionEnded] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<number | null>(null)
  const outputCallbackRef = useRef<((runId: string, data: string) => void) | null>(null)
  const attachedSessionRef = useRef<string | null>(null)
  const connectRef = useRef<() => void>(() => {})

  // Keep ref in sync so handleMessage can read current value
  useEffect(() => {
    attachedSessionRef.current = attachedSession
  }, [attachedSession])

  const dismissEndedSession = useCallback(() => {
    setSessionEnded(false)
    setStreamingId(null)
    setAttachedSession(null)
  }, [])

  const refreshSessions = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    wsRef.current.send(JSON.stringify({
      type: 'tmux_list_sessions',
      request_id: `refresh-${Date.now()}`,
    }))
  }, [])

  const handleMessage = useCallback((data: Record<string, unknown>) => {
    switch (data.type) {
      case 'tmux_sessions_list': {
        const newSessions = data.sessions as TmuxSession[]
        setSessions(newSessions)
        setLiveCliSessionIds((data.live_cli_session_ids as string[]) || [])
        const attached = attachedSessionRef.current
        if (attached && !newSessions.some((session) => session.name === attached)) {
          setSessionEnded(true)
        }
        setIsLoading(false)
        break
      }

      case 'tmux_attach_result':
        if (data.success) {
          setStreamingId(data.streaming_id as string)
          setAttachedSession(data.session_name as string)
        }
        setIsLoading(false)
        break

      case 'tmux_detach_result':
        if (data.success) {
          setStreamingId(null)
          setAttachedSession(null)
        }
        setIsLoading(false)
        break

      case 'tmux_create_result':
        if (data.success) {
          refreshSessions()
        }
        setIsLoading(false)
        break

      case 'tmux_kill_result':
        refreshSessions()
        setIsLoading(false)
        break

      case 'tmux_session_event':
        refreshSessions()
        break

      case 'session_event':
        if (data.event === 'session_updated') {
          refreshSessions()
        }
        break

      case 'terminal_output':
        if (outputCallbackRef.current) {
          outputCallbackRef.current(data.run_id as string, data.data as string)
        }
        break
    }
  }, [refreshSessions])

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const isSecure = window.location.protocol === 'https:'
    const wsUrl = isSecure
      ? `wss://${window.location.host}/ws`
      : `ws://${window.location.host}/ws`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      ws.send(JSON.stringify({
        type: 'subscribe',
        events: ['terminal_output', 'tmux_session_event', 'session_event'],
      }))
      // Fetch session list on connect
      ws.send(JSON.stringify({ type: 'tmux_list_sessions', request_id: 'init' }))
    }

    ws.onclose = () => {
      reconnectTimeoutRef.current = window.setTimeout(() => {
        connectRef.current()
      }, 2000)
    }

    ws.onerror = (error) => {
      console.error('Tmux WebSocket error:', error)
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        handleMessage(data)
      } catch (e) {
        console.error('Failed to parse tmux message:', e)
      }
    }
  }, [handleMessage])

  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  const attachSession = useCallback((sessionName: string, socket: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setIsLoading(true)
    setSessionEnded(false)
    wsRef.current.send(JSON.stringify({
      type: 'tmux_attach',
      request_id: `attach-${Date.now()}`,
      session_name: sessionName,
      socket,
    }))
  }, [])

  const detachSession = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !streamingId) return
    setIsLoading(true)
    wsRef.current.send(JSON.stringify({
      type: 'tmux_detach',
      request_id: `detach-${Date.now()}`,
      streaming_id: streamingId,
    }))
  }, [streamingId])

  const refreshTerminal = useCallback((sessionName: string, socket: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    wsRef.current.send(JSON.stringify({
      type: 'tmux_refresh_client',
      session_name: sessionName,
      socket: socket || 'default',
    }))
  }, [])

  const createSession = useCallback((name?: string, socket?: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setIsLoading(true)
    wsRef.current.send(JSON.stringify({
      type: 'tmux_create_session',
      request_id: `create-${Date.now()}`,
      name,
      socket: socket || 'default',
    }))
  }, [])

  const killSession = useCallback((sessionName: string, socket: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    setIsLoading(true)
    if (sessionName === attachedSession) {
      setStreamingId(null)
      setAttachedSession(null)
    }
    wsRef.current.send(JSON.stringify({
      type: 'tmux_kill_session',
      request_id: `kill-${Date.now()}`,
      session_name: sessionName,
      socket,
    }))
  }, [attachedSession])

  const sendInput = useCallback((data: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !streamingId) return
    wsRef.current.send(JSON.stringify({
      type: 'terminal_input',
      run_id: streamingId,
      data,
    }))
  }, [streamingId])

  const resizeTerminal = useCallback((rows: number, cols: number) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !streamingId) return
    wsRef.current.send(JSON.stringify({
      type: 'tmux_resize',
      streaming_id: streamingId,
      rows,
      cols,
    }))
  }, [streamingId])

  const onOutput = useCallback((callback: (runId: string, data: string) => void) => {
    outputCallbackRef.current = callback
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      wsRef.current?.close()
    }
  }, [connect])

  // Refresh session list when browser tab becomes visible (catches missed events)
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') {
        refreshSessions()
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [refreshSessions])

  return {
    sessions,
    liveCliSessionIds,
    attachedSession,
    streamingId,
    isLoading,
    sessionEnded,
    attachSession,
    detachSession,
    refreshTerminal,
    createSession,
    killSession,
    refreshSessions,
    dismissEndedSession,
    sendInput,
    resizeTerminal,
    onOutput,
  }
}
