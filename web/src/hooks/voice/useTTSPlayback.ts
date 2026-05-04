import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

const MAX_AUDIO_QUEUE_SIZE = 50

interface TTSMeta {
  sampleRate: number
  format: string
  chunkIndex: number
}

interface TTSPlaybackOptions {
  wsRef: RefObject<WebSocket | null>
  conversationIdRef: RefObject<string>
  ttsEnabled: boolean
  setTransientError: (msg: string, ms?: number) => void
}

interface TTSPlaybackReturn {
  isSpeaking: boolean
  prepareTTSPlayback: () => void
  stopTTS: () => void
  handleBinaryMessage: (data: ArrayBuffer) => void
  handleTTSAudioMeta: (data: Record<string, unknown>) => void
  clearPendingTTSMeta: () => void
}

export function useTTSPlayback({
  wsRef,
  conversationIdRef,
  ttsEnabled,
  setTransientError,
}: TTSPlaybackOptions): TTSPlaybackReturn {
  const [isSpeaking, setIsSpeaking] = useState(false)

  const audioContextRef = useRef<AudioContext | null>(null)
  const audioQueueRef = useRef<AudioBuffer[]>([])
  const isPlayingRef = useRef(false)
  const playbackEpochRef = useRef(0)
  const resumePlaybackPendingRef = useRef(false)
  const resumeContextRef = useRef<AudioContext | null>(null)
  const resumePromiseRef = useRef<Promise<AudioContext | null> | null>(null)
  const currentSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const pendingTTSMetaRef = useRef<TTSMeta | null>(null)
  const mountedRef = useRef(true)
  const playErrorCountRef = useRef(0)

  const getAudioContext = useCallback((): AudioContext | null => {
    try {
      if (!audioContextRef.current || audioContextRef.current.state === 'closed') {
        audioContextRef.current = new AudioContext()
      }
      return audioContextRef.current
    } catch (err) {
      console.error('Voice: Failed to create AudioContext:', err)
      return null
    }
  }, [])

  const ensureAudioContextRunning = useCallback((ctx: AudioContext): Promise<AudioContext | null> => {
    if (ctx.state === 'running') {
      return Promise.resolve(ctx)
    }
    if (ctx.state !== 'suspended') {
      return Promise.resolve(null)
    }

    if (!resumePromiseRef.current || resumeContextRef.current !== ctx) {
      resumeContextRef.current = ctx
      const resumePromise: Promise<AudioContext | null> = ctx.resume()
        .then(() => (ctx.state === 'running' ? ctx : null))
        .catch((err) => {
          console.error('Voice: Failed to resume AudioContext:', err)
          return null
        })
        .finally(() => {
          if (resumePromiseRef.current === resumePromise) {
            resumePromiseRef.current = null
            resumeContextRef.current = null
          }
        })
      resumePromiseRef.current = resumePromise
    }

    return resumePromiseRef.current
  }, [])

  const playNextChunk = useCallback(function playNextChunk() {
    if (audioQueueRef.current.length === 0) {
      isPlayingRef.current = false
      if (mountedRef.current) setIsSpeaking(false)
      return
    }

    const ctx = getAudioContext()
    if (!ctx) {
      isPlayingRef.current = false
      if (mountedRef.current) setIsSpeaking(false)
      return
    }

    if (ctx.state !== 'running') {
      if (!resumePlaybackPendingRef.current) {
        const playbackEpoch = playbackEpochRef.current
        resumePlaybackPendingRef.current = true
        void ensureAudioContextRunning(ctx).then((runningCtx) => {
          resumePlaybackPendingRef.current = false
          if (!mountedRef.current) return
          if (playbackEpoch !== playbackEpochRef.current) return
          if (!runningCtx || runningCtx.state !== 'running') {
            isPlayingRef.current = false
            if (mountedRef.current) setIsSpeaking(false)
            return
          }
          playNextChunk()
        })
      }
      return
    }

    const buffer = audioQueueRef.current.shift()
    if (!buffer) {
      isPlayingRef.current = false
      if (mountedRef.current) setIsSpeaking(false)
      return
    }

    try {
      const source = ctx.createBufferSource()
      source.buffer = buffer
      source.connect(ctx.destination)
      source.onended = playNextChunk
      source.start()
      currentSourceRef.current = source
      playErrorCountRef.current = 0
    } catch (err) {
      console.error('Voice: Failed to play audio chunk:', err)
      playErrorCountRef.current += 1
      if (playErrorCountRef.current >= 3) {
        audioQueueRef.current = []
        isPlayingRef.current = false
        playErrorCountRef.current = 0
        if (mountedRef.current) setIsSpeaking(false)
        return
      }
      setTimeout(playNextChunk, 0)
    }
  }, [ensureAudioContextRunning, getAudioContext])

  const queueAudioChunk = useCallback((pcmData: ArrayBuffer, sampleRate: number) => {
    if (!ttsEnabled) return

    const ctx = getAudioContext()
    if (!ctx) return

    try {
      if (audioQueueRef.current.length >= MAX_AUDIO_QUEUE_SIZE) {
        console.warn('Voice: Audio queue full, dropping incoming chunk')
        setTransientError('Audio dropped — connection too slow')
        return
      }

      const int16 = new Int16Array(pcmData)
      const float32 = new Float32Array(int16.length)
      for (let i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / 32768
      }

      const audioBuffer = ctx.createBuffer(1, float32.length, sampleRate)
      audioBuffer.getChannelData(0).set(float32)

      audioQueueRef.current.push(audioBuffer)
      if (mountedRef.current) setIsSpeaking(true)

      if (!isPlayingRef.current) {
        isPlayingRef.current = true
        playNextChunk()
      }
    } catch (err) {
      console.error('Voice: Failed to queue audio chunk:', err)
    }
  }, [getAudioContext, playNextChunk, setTransientError, ttsEnabled])

  const stopLocalPlayback = useCallback(() => {
    try {
      currentSourceRef.current?.stop()
    } catch {
      // Already stopped.
    }
    currentSourceRef.current = null
    audioQueueRef.current = []
    isPlayingRef.current = false
    playbackEpochRef.current += 1
    resumePlaybackPendingRef.current = false
    pendingTTSMetaRef.current = null
    if (mountedRef.current) setIsSpeaking(false)
  }, [])

  const stopTTS = useCallback(() => {
    stopLocalPlayback()

    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'tts_stop',
        conversation_id: conversationIdRef.current,
      }))
    }
  }, [conversationIdRef, stopLocalPlayback, wsRef])

  const prepareTTSPlayback = useCallback(() => {
    const ctx = getAudioContext()
    if (ctx) {
      void ensureAudioContextRunning(ctx)
    }
  }, [ensureAudioContextRunning, getAudioContext])

  const handleBinaryMessage = useCallback((data: ArrayBuffer) => {
    const meta = pendingTTSMetaRef.current
    if (!meta || !ttsEnabled) return
    pendingTTSMetaRef.current = null
    queueAudioChunk(data, meta.sampleRate)
  }, [queueAudioChunk, ttsEnabled])

  const handleTTSAudioMeta = useCallback((data: Record<string, unknown>) => {
    if (!ttsEnabled) return
    pendingTTSMetaRef.current = {
      sampleRate: (data.sample_rate as number) || 24000,
      format: (data.format as string) || 'pcm_s16le',
      chunkIndex: (data.chunk_index as number) || 0,
    }
  }, [ttsEnabled])

  const clearPendingTTSMeta = useCallback(() => {
    pendingTTSMetaRef.current = null
  }, [])

  useEffect(() => {
    if (!ttsEnabled) {
      stopTTS()
    }
  }, [stopTTS, ttsEnabled])

  useEffect(() => {
    return () => {
      mountedRef.current = false
      stopLocalPlayback()
      try {
        audioContextRef.current?.suspend()
      } catch {
        // noop
      }
    }
  }, [stopLocalPlayback])

  return {
    isSpeaking,
    prepareTTSPlayback,
    stopTTS,
    handleBinaryMessage,
    handleTTSAudioMeta,
    clearPendingTTSMeta,
  }
}
