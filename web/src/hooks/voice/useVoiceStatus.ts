import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { pruneTimeBoundLru } from '../../lib/timeBoundLru'
import { parseVoiceStatus, type RawVoiceStatus } from '../voiceStatus'

const VOICE_PREPARE_RETRY_MS = 30_000
const VOICE_PREPARE_CACHE_MAX_ENTRIES = 128
// Shared across hook instances because chat remounts, reconnects, and
// conversation switches can recreate the hook while the same WebSocket target is
// still warming. This only throttles prepare sends: same-tick races may
// duplicate one prepare, TTL expiry allows retry, and LRU pruning bounds growth.
const voicePrepareSentAt = new Map<string, number>()

function getVoicePrepareKey(conversationId: string, sttEnabled: boolean, ttsEnabled: boolean) {
  return `${conversationId}:${sttEnabled}:${ttsEnabled}`
}

function pruneVoicePrepareSentAt(now: number) {
  pruneTimeBoundLru(voicePrepareSentAt, now, {
    maxEntries: VOICE_PREPARE_CACHE_MAX_ENTRIES,
    ttlMs: VOICE_PREPARE_RETRY_MS,
  })
}

export function resetVoicePrepareCacheForTests(): void {
  voicePrepareSentAt.clear()
}

export function seedVoicePrepareCacheForTests(entries: Array<readonly [string, number]>): void {
  voicePrepareSentAt.clear()
  for (const [key, sentAt] of entries) {
    voicePrepareSentAt.set(key, sentAt)
  }
}

export function voicePrepareCacheKeysForTests(): string[] {
  return Array.from(voicePrepareSentAt.keys())
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
    const now = Date.now()
    pruneVoicePrepareSentAt(now)
    const lastSentAt = voicePrepareSentAt.get(warmupKey)
    if (lastSentAt !== undefined && now - lastSentAt < VOICE_PREPARE_RETRY_MS) return

    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    voicePrepareSentAt.set(warmupKey, now)
    pruneVoicePrepareSentAt(now)
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
