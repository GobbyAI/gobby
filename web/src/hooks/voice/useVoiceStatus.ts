import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { parseVoiceStatus, type RawVoiceStatus } from '../voiceStatus'

const VOICE_PREPARE_RETRY_MS = 30_000
const voicePrepareSentAt = new Map<string, number>()

function getVoicePrepareKey(conversationId: string, sttEnabled: boolean, ttsEnabled: boolean) {
  return `${conversationId}:${sttEnabled}:${ttsEnabled}`
}

interface VoiceStatusOptions {
  wsRef: RefObject<WebSocket | null>
  conversationId: string
  socketConnected: boolean
  sttEnabled: boolean
  ttsEnabled: boolean
}

interface VoiceStatusReturn {
  voiceAvailable: boolean
  voiceReady: boolean
  voiceLoading: boolean
  sttAvailable: boolean
  statusVoiceError: string | null
  startStatusPolling: () => void
  applyVoiceStatus: (data: RawVoiceStatus | null) => void
  markVoicePreparing: () => void
}

export function useVoiceStatus({
  wsRef,
  conversationId,
  socketConnected,
  sttEnabled,
  ttsEnabled,
}: VoiceStatusOptions): VoiceStatusReturn {
  const [voiceAvailable, setVoiceAvailable] = useState(false)
  const [voiceReady, setVoiceReady] = useState(false)
  const [voiceLoading, setVoiceLoading] = useState(false)
  const [sttAvailable, setSttAvailable] = useState(false)
  const [statusVoiceError, setStatusVoiceError] = useState<string | null>(null)

  const statusPollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollCancelledRef = useRef(false)

  const applyVoiceStatus = useCallback((data: RawVoiceStatus | null) => {
    const parsed = parseVoiceStatus(data, window.isSecureContext)
    setSttAvailable(parsed.sttAvailable)
    setVoiceAvailable(parsed.voiceAvailable)
    setVoiceReady(parsed.voiceReady)
    setVoiceLoading(parsed.voiceLoading)
    setStatusVoiceError(parsed.warmupError)
  }, [])

  const markVoicePreparing = useCallback(() => {
    setVoiceLoading(true)
    setVoiceReady(false)
  }, [])

  const startStatusPolling = useCallback(() => {
    if (statusPollRef.current) clearTimeout(statusPollRef.current)

    const syncVoiceStatus = async () => {
      try {
        const params = new URLSearchParams()
        if (sttEnabled) params.set('want_stt', 'true')
        if (ttsEnabled) params.set('want_tts', 'true')
        const query = params.toString()
        const res = await fetch(query ? `/api/voice/status?${query}` : '/api/voice/status')
        const data = res.ok ? (await res.json() as RawVoiceStatus) : null
        if (pollCancelledRef.current) return

        applyVoiceStatus(data)

        const parsed = parseVoiceStatus(data, window.isSecureContext)
        if (parsed.voiceLoading) {
          statusPollRef.current = setTimeout(syncVoiceStatus, 1000)
        }
      } catch (err) {
        console.error('Voice status check failed:', err)
        if (pollCancelledRef.current) return
        setSttAvailable(false)
        setVoiceAvailable(false)
        setVoiceReady(false)
        setVoiceLoading(false)
      }
    }

    void syncVoiceStatus()
  }, [applyVoiceStatus, sttEnabled, ttsEnabled])

  useEffect(() => {
    pollCancelledRef.current = false
    startStatusPolling()

    return () => {
      pollCancelledRef.current = true
      if (statusPollRef.current) clearTimeout(statusPollRef.current)
    }
  }, [startStatusPolling])

  useEffect(() => {
    const wantsVoice = sttEnabled || ttsEnabled
    if (!wantsVoice) {
      return
    }
    if (!socketConnected || voiceReady) return

    const warmupKey = getVoicePrepareKey(conversationId, sttEnabled, ttsEnabled)
    const lastSentAt = voicePrepareSentAt.get(warmupKey)
    const now = Date.now()
    if (lastSentAt !== undefined && now - lastSentAt < VOICE_PREPARE_RETRY_MS) return

    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    voicePrepareSentAt.set(warmupKey, now)
    setVoiceLoading(true)
    ws.send(JSON.stringify({
      type: 'voice_prepare',
      conversation_id: conversationId,
      stt_enabled: sttEnabled,
      tts_enabled: ttsEnabled,
    }))
    startStatusPolling()
  }, [
    conversationId,
    socketConnected,
    startStatusPolling,
    sttEnabled,
    ttsEnabled,
    voiceReady,
    wsRef,
  ])

  useEffect(() => {
    if (!socketConnected) return
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    ws.send(JSON.stringify({
      type: 'voice_mode_toggle',
      conversation_id: conversationId,
      enabled: ttsEnabled,
    }))
  }, [conversationId, socketConnected, ttsEnabled, wsRef])

  return {
    voiceAvailable,
    voiceReady,
    voiceLoading,
    sttAvailable,
    statusVoiceError,
    startStatusPolling,
    applyVoiceStatus,
    markVoicePreparing,
  }
}
