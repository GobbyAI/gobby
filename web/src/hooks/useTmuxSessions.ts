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

export interface TmuxTarget {
  name: string
  socket: string
}

type PendingRequest =
  | {
      kind: 'attach'
      requestId: string
      generation: number
      target: TmuxTarget
    }
  | {
      kind: 'detach'
      requestId: string
      generation: number
      nextTarget: TmuxTarget | null
    }

interface TmuxSessionsResult {
  sessions: TmuxSession[]
  liveCliSessionIds: string[]
  connected: boolean
  sessionsLoaded: boolean
  attachedTarget: TmuxTarget | null
  streamingId: string | null
  isLoading: boolean
  sessionEnded: boolean
  requestPending: boolean
  attachError: string | null
  attachSession: (sessionName: string, socket: string) => void
  detachSession: () => void
  clearAttachError: () => void
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
  const [connected, setConnected] = useState(false)
  const [sessionsLoaded, setSessionsLoaded] = useState(false)
  const [attachedTarget, setAttachedTarget] = useState<TmuxTarget | null>(null)
  const [streamingId, setStreamingId] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [sessionEnded, setSessionEnded] = useState(false)
  const [requestPending, setRequestPending] = useState(false)
  const [attachError, setAttachError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<number | null>(null)
  const outputCallbackRef = useRef<((runId: string, data: string) => void) | null>(null)
  const attachedTargetRef = useRef<TmuxTarget | null>(null)
  const streamingIdRef = useRef<string | null>(null)
  const connectionGenerationRef = useRef(0)
  const requestCounterRef = useRef(0)
  const pendingRequestRef = useRef<PendingRequest | null>(null)
  const connectRef = useRef<() => void>(() => {})

  const updateAttachment = useCallback((target: TmuxTarget | null, streamId: string | null) => {
    attachedTargetRef.current = target
    streamingIdRef.current = streamId
    setAttachedTarget(target)
    setStreamingId(streamId)
  }, [])

  const clearPendingRequest = useCallback(() => {
    pendingRequestRef.current = null
    setRequestPending(false)
    setIsLoading(false)
  }, [])

  const dismissEndedSession = useCallback(() => {
    setSessionEnded(false)
    updateAttachment(null, null)
  }, [updateAttachment])

  const refreshSessions = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    wsRef.current.send(JSON.stringify({
      type: 'tmux_list_sessions',
      request_id: `refresh-${Date.now()}`,
    }))
  }, [])

  const beginAttachRequest = useCallback((target: TmuxTarget): boolean => {
    const ws = wsRef.current
    if (pendingRequestRef.current || !ws || ws.readyState !== WebSocket.OPEN) return false

    const generation = connectionGenerationRef.current
    const requestId = `attach-${generation}-${++requestCounterRef.current}`
    pendingRequestRef.current = { kind: 'attach', requestId, generation, target }
    setRequestPending(true)
    setIsLoading(true)
    setSessionEnded(false)
    setAttachError(null)
    ws.send(JSON.stringify({
      type: 'tmux_attach',
      request_id: requestId,
      session_name: target.name,
      socket: target.socket,
    }))
    return true
  }, [])

  const beginDetachRequest = useCallback((nextTarget: TmuxTarget | null): boolean => {
    const ws = wsRef.current
    const currentStreamingId = streamingIdRef.current
    if (
      pendingRequestRef.current
      || !ws
      || ws.readyState !== WebSocket.OPEN
      || !currentStreamingId
    ) return false

    const generation = connectionGenerationRef.current
    const requestId = `detach-${generation}-${++requestCounterRef.current}`
    pendingRequestRef.current = { kind: 'detach', requestId, generation, nextTarget }
    setRequestPending(true)
    setIsLoading(true)
    setAttachError(null)
    ws.send(JSON.stringify({
      type: 'tmux_detach',
      request_id: requestId,
      streaming_id: currentStreamingId,
    }))
    return true
  }, [])

  const handleMessage = useCallback((data: Record<string, unknown>) => {
    switch (data.type) {
      case 'tmux_sessions_list': {
        const newSessions = data.sessions as TmuxSession[]
        setSessions(newSessions)
        setLiveCliSessionIds((data.live_cli_session_ids as string[]) || [])
        setSessionsLoaded(true)
        const attached = attachedTargetRef.current
        if (
          attached
          && !newSessions.some(
            (session) => session.name === attached.name && session.socket === attached.socket,
          )
        ) {
          setSessionEnded(true)
        }
        setIsLoading(false)
        break
      }

      case 'tmux_attach_result': {
        const pending = pendingRequestRef.current
        if (
          !pending
          || pending.kind !== 'attach'
          || pending.requestId !== data.request_id
          || pending.generation !== connectionGenerationRef.current
        ) break

        if (data.success) {
          updateAttachment(pending.target, data.streaming_id as string)
        } else {
          setAttachError(typeof data.message === 'string' ? data.message : 'Attach failed')
        }
        clearPendingRequest()
        break
      }

      case 'tmux_detach_result': {
        const pending = pendingRequestRef.current
        if (
          !pending
          || pending.kind !== 'detach'
          || pending.requestId !== data.request_id
          || pending.generation !== connectionGenerationRef.current
        ) break

        const nextTarget = pending.nextTarget
        if (data.success) {
          updateAttachment(null, null)
          clearPendingRequest()
          if (nextTarget) beginAttachRequest(nextTarget)
        } else {
          setAttachError(typeof data.message === 'string' ? data.message : 'Detach failed')
          clearPendingRequest()
        }
        break
      }

      case 'error': {
        const pending = pendingRequestRef.current
        if (
          !pending
          || pending.requestId !== data.request_id
          || pending.generation !== connectionGenerationRef.current
        ) break

        setAttachError(typeof data.message === 'string' ? data.message : 'Terminal request failed')
        clearPendingRequest()
        break
      }

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
  }, [beginAttachRequest, clearPendingRequest, refreshSessions, updateAttachment])

  const connect = useCallback(() => {
    if (
      wsRef.current?.readyState === WebSocket.OPEN
      || wsRef.current?.readyState === WebSocket.CONNECTING
    ) return

    const isSecure = window.location.protocol === 'https:'
    const wsUrl = isSecure
      ? `wss://${window.location.host}/ws`
      : `ws://${window.location.host}/ws`

    const generation = ++connectionGenerationRef.current
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    const isCurrentConnection = () => (
      connectionGenerationRef.current === generation && wsRef.current === ws
    )

    ws.onopen = () => {
      if (!isCurrentConnection()) return
      setConnected(true)
      ws.send(JSON.stringify({
        type: 'subscribe',
        events: ['terminal_output', 'tmux_session_event', 'session_event'],
      }))
      // Fetch session list on connect
      ws.send(JSON.stringify({ type: 'tmux_list_sessions', request_id: 'init' }))
    }

    ws.onclose = () => {
      if (!isCurrentConnection()) return
      setConnected(false)
      setSessionsLoaded(false)
      updateAttachment(null, null)
      pendingRequestRef.current = null
      setRequestPending(false)
      setIsLoading(false)
      setAttachError(null)
      reconnectTimeoutRef.current = window.setTimeout(() => {
        if (!isCurrentConnection()) return
        reconnectTimeoutRef.current = null
        connectRef.current()
      }, 2000)
    }

    ws.onerror = (error) => {
      if (!isCurrentConnection()) return
      console.error('Tmux WebSocket error:', error)
    }

    ws.onmessage = (event) => {
      if (!isCurrentConnection()) return
      try {
        const data = JSON.parse(event.data)
        handleMessage(data)
      } catch (e) {
        console.error('Failed to parse tmux message:', e)
      }
    }
  }, [handleMessage, updateAttachment])

  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  const attachSession = useCallback((sessionName: string, socket: string) => {
    if (pendingRequestRef.current) return
    const target = { name: sessionName, socket }
    const currentTarget = attachedTargetRef.current
    if (currentTarget?.name === target.name && currentTarget.socket === target.socket) return
    if (currentTarget) {
      beginDetachRequest(target)
      return
    }
    beginAttachRequest(target)
  }, [beginAttachRequest, beginDetachRequest])

  const detachSession = useCallback(() => {
    beginDetachRequest(null)
  }, [beginDetachRequest])

  const clearAttachError = useCallback(() => setAttachError(null), [])

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
    const currentTarget = attachedTargetRef.current
    if (currentTarget?.name === sessionName && currentTarget.socket === socket) {
      updateAttachment(null, null)
    }
    wsRef.current.send(JSON.stringify({
      type: 'tmux_kill_session',
      request_id: `kill-${Date.now()}`,
      session_name: sessionName,
      socket,
    }))
  }, [updateAttachment])

  const sendInput = useCallback((data: string) => {
    const currentStreamingId = streamingIdRef.current
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !currentStreamingId) return
    wsRef.current.send(JSON.stringify({
      type: 'terminal_input',
      run_id: currentStreamingId,
      data,
    }))
  }, [])

  const resizeTerminal = useCallback((rows: number, cols: number) => {
    const currentStreamingId = streamingIdRef.current
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !currentStreamingId) return
    wsRef.current.send(JSON.stringify({
      type: 'tmux_resize',
      streaming_id: currentStreamingId,
      rows,
      cols,
    }))
  }, [])

  // Single consumer by design: TerminalTab owns the terminal output stream.
  const onOutput = useCallback((callback: (runId: string, data: string) => void) => {
    outputCallbackRef.current = callback
  }, [])

  useEffect(() => {
    connect()
    return () => {
      connectionGenerationRef.current += 1
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = null
      }
      pendingRequestRef.current = null
      const ws = wsRef.current
      wsRef.current = null
      ws?.close()
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
    connected,
    sessionsLoaded,
    attachedTarget,
    streamingId,
    isLoading,
    sessionEnded,
    requestPending,
    attachError,
    attachSession,
    detachSession,
    clearAttachError,
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
