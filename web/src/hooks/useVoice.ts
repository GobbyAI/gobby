import { useState, useCallback, useRef, useEffect } from 'react'
import { MicVAD, utils } from '@ricky0123/vad-web'

const MAX_AUDIO_QUEUE_SIZE = 50

interface VoiceState {
  voiceMode: boolean
  voiceAvailable: boolean
  voiceReady: boolean
  voiceLoading: boolean
  isListening: boolean
  isSpeechDetected: boolean
  isTranscribing: boolean
  isSpeaking: boolean
  voiceError: string | null
}

export interface UseVoiceReturn extends VoiceState {
  toggleVoiceMode: () => void
  handleVoiceMessage: (data: Record<string, unknown>) => void
  handleBinaryMessage: (data: ArrayBuffer) => void
  stopTTS: () => void
}

interface TTSMeta {
  sampleRate: number
  format: string
  chunkIndex: number
}

export function useVoice(
  wsRef: React.RefObject<WebSocket | null>,
  conversationId: string,
): UseVoiceReturn {
  const [voiceMode, setVoiceMode] = useState(false)
  const [voiceAvailable, setVoiceAvailable] = useState(false)
  const [voiceReady, setVoiceReady] = useState(false)
  const [voiceLoading, setVoiceLoading] = useState(false)
  const [isListening, setIsListening] = useState(false)
  const [isSpeechDetected, setIsSpeechDetected] = useState(false)
  const [isTranscribing, setIsTranscribing] = useState(false)
  const [isSpeaking, setIsSpeaking] = useState(false)
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [statusVoiceError, setStatusVoiceError] = useState<string | null>(null)

  const vadRef = useRef<MicVAD | null>(null)
  const errorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const statusPollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Stable ref for conversationId so long-lived callbacks (e.g. VAD onSpeechEnd)
  // always see the latest value without re-subscribing.
  const conversationIdRef = useRef(conversationId)
  conversationIdRef.current = conversationId

  const voiceModeRef = useRef(false)

  // --- TTS playback state ---
  const audioContextRef = useRef<AudioContext | null>(null)
  const audioQueueRef = useRef<AudioBuffer[]>([])
  const isPlayingRef = useRef(false)
  const currentSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const pendingTTSMetaRef = useRef<TTSMeta | null>(null)
  const mountedRef = useRef(true)
  const playErrorCountRef = useRef(0)

  // Set a voice error that auto-clears after a delay
  const setTransientError = useCallback((msg: string, ms = 3000) => {
    if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
    setVoiceError(msg)
    errorTimerRef.current = setTimeout(() => setVoiceError(null), ms)
  }, [])

  // --- TTS audio playback ---

  const getAudioContext = useCallback((): AudioContext | null => {
    try {
      if (!audioContextRef.current || audioContextRef.current.state === 'closed') {
        audioContextRef.current = new AudioContext()
      }
      // Resume if suspended (browser autoplay policy)
      if (audioContextRef.current.state === 'suspended') {
        audioContextRef.current.resume().catch(() => {})
      }
      return audioContextRef.current
    } catch (err) {
      console.error('Voice: Failed to create AudioContext:', err)
      return null
    }
  }, [])

  const playNextChunk = useCallback(() => {
    const buffer = audioQueueRef.current.shift()
    if (!buffer) {
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
        console.warn('Voice: Too many consecutive playback errors, stopping TTS')
        audioQueueRef.current = []
        isPlayingRef.current = false
        playErrorCountRef.current = 0
        if (mountedRef.current) setIsSpeaking(false)
        return
      }
      setTimeout(playNextChunk, 0)
    }
  }, [getAudioContext])

  const queueAudioChunk = useCallback((pcmData: ArrayBuffer, sampleRate: number) => {
    const ctx = getAudioContext()
    if (!ctx) return

    try {
      // Convert PCM int16 little-endian to float32 for AudioBuffer
      const int16 = new Int16Array(pcmData)
      const float32 = new Float32Array(int16.length)
      for (let i = 0; i < int16.length; i++) {
        float32[i] = int16[i] / 32768
      }

      const audioBuffer = ctx.createBuffer(1, float32.length, sampleRate)
      audioBuffer.getChannelData(0).set(float32)

      while (audioQueueRef.current.length >= MAX_AUDIO_QUEUE_SIZE) {
        audioQueueRef.current.shift()
      }
      audioQueueRef.current.push(audioBuffer)
      if (mountedRef.current) setIsSpeaking(true)

      // Start playback if not already playing
      if (!isPlayingRef.current) {
        isPlayingRef.current = true
        playNextChunk()
      }
    } catch (err) {
      console.error('Voice: Failed to queue audio chunk:', err)
    }
  }, [getAudioContext, playNextChunk])

  const stopTTS = useCallback(() => {
    // Stop current playback
    try {
      currentSourceRef.current?.stop()
    } catch {
      // Already stopped
    }
    currentSourceRef.current = null

    // Clear queue
    audioQueueRef.current = []
    isPlayingRef.current = false
    pendingTTSMetaRef.current = null
    if (mountedRef.current) setIsSpeaking(false)

    // Tell backend to stop synthesizing
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'tts_stop',
        conversation_id: conversationIdRef.current,
      }))
    }
  }, [wsRef])

  // Handle binary WebSocket messages (TTS audio data)
  const handleBinaryMessage = useCallback((data: ArrayBuffer) => {
    const meta = pendingTTSMetaRef.current
    if (!meta) {
      // No pending metadata — stale or out-of-order binary frame, ignore
      return
    }
    pendingTTSMetaRef.current = null
    queueAudioChunk(data, meta.sampleRate)
  }, [queueAudioChunk])

  // Check voice availability on mount (STT availability via /api/voice/status)
  useEffect(() => {
    // getUserMedia requires a secure context (HTTPS or localhost).
    // On plain HTTP (e.g. Tailscale IP), navigator.mediaDevices is undefined.
    if (!window.isSecureContext) {
      console.warn('Voice: disabled — requires secure context (HTTPS or localhost)')
      setVoiceAvailable(false)
      setVoiceReady(false)
      setVoiceLoading(false)
      return
    }

    let cancelled = false

    const syncVoiceStatus = async () => {
      try {
        const res = await fetch('/api/voice/status')
        const data = res.ok ? await res.json() : null
        if (cancelled) return

        const ttsRequired = Boolean(data?.enabled && data?.tts_enabled)
        const available = Boolean(data?.enabled && data?.stt_available && (!ttsRequired || data?.tts_available))
        const ready = Boolean(available && data?.voice_ready)
        const loading = Boolean(available && data?.voice_loading)
        const warmupError =
          (data?.stt_warmup_error as string | undefined) ||
          (data?.tts_warmup_error as string | undefined) ||
          null

        setVoiceAvailable(available)
        setVoiceReady(ready)
        setVoiceLoading(loading)
        setStatusVoiceError(warmupError)

        if (loading) {
          statusPollRef.current = setTimeout(syncVoiceStatus, 1000)
        }
      } catch (err) {
        console.error('Voice status check failed:', err)
        if (cancelled) return
        setVoiceAvailable(false)
        setVoiceReady(false)
        setVoiceLoading(false)
      }
    }

    void syncVoiceStatus()

    return () => {
      cancelled = true
      if (statusPollRef.current) clearTimeout(statusPollRef.current)
    }
  }, [])

  const toggleVoiceMode = useCallback(async () => {
    const newMode = !voiceMode
    setVoiceError(null)

    if (newMode && (voiceLoading || !voiceReady)) {
      setTransientError(statusVoiceError || 'Voice is still loading')
      return
    }

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'voice_mode_toggle',
        conversation_id: conversationId,
        enabled: newMode,
      }))
    }

    if (newMode) {
      // Enable: create VAD, start listening
      try {
        const vad = await MicVAD.new({
          baseAssetPath: '/',
          onnxWASMBasePath: 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.24.1/dist/',
          positiveSpeechThreshold: 0.6,
          negativeSpeechThreshold: 0.35,
          minSpeechFrames: 6,
          redemptionFrames: 12,
          preSpeechPadFrames: 10,
          submitUserSpeechOnPause: false,

          onSpeechStart: () => {
            setIsSpeechDetected(true)
            // Barge-in: stop TTS when user starts speaking
            stopTTS()
          },

          onSpeechEnd: (audio: Float32Array) => {
            setIsSpeechDetected(false)

            try {
              const ws = wsRef.current
              if (!ws || ws.readyState !== WebSocket.OPEN) {
                console.warn('Voice: WebSocket not open, discarding audio')
                setTransientError('Connection lost — try again')
                return
              }

              if (mountedRef.current) setIsTranscribing(true)

              // Encode to WAV (16kHz mono 16-bit) and send
              const wavBuffer = utils.encodeWAV(audio, 1, 16000, 1, 16)
              const base64 = utils.arrayBufferToBase64(wavBuffer)

              ws.send(JSON.stringify({
                type: 'voice_audio',
                conversation_id: conversationIdRef.current,
                audio_data: base64,
                mime_type: 'audio/wav',
                request_id: crypto.randomUUID?.() || `voice-${Date.now()}-${Math.random().toString(36).slice(2)}`,
              }))
            } catch (err) {
              console.error('Voice: Failed to encode/send audio:', err)
              if (mountedRef.current) setIsTranscribing(false)
              if (mountedRef.current) setTransientError('Failed to process audio')
            }
          },

          onVADMisfire: () => {
            setIsSpeechDetected(false)
          },
        })

        vadRef.current = vad
        vad.start()
        voiceModeRef.current = true
        setVoiceMode(true)
        setIsListening(true)
      } catch (err) {
        if (vadRef.current) {
          vadRef.current.destroy()
          vadRef.current = null
        }
        voiceModeRef.current = false
        const msg = err instanceof Error ? err.message : 'Microphone access denied'
        setVoiceError(msg)
        console.error('Failed to start VAD:', err)
      }
    } else {
      // Disable: destroy VAD, stop TTS, cleanup
      voiceModeRef.current = false
      stopTTS()
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
      setVoiceMode(false)
      setIsListening(false)
      setIsSpeechDetected(false)
      setIsTranscribing(false)
    }
  }, [voiceMode, voiceLoading, voiceReady, statusVoiceError, wsRef, conversationId, setTransientError, stopTTS])

  // Stop TTS when switching conversations (skip initial mount)
  const prevConversationIdRef = useRef(conversationId)
  useEffect(() => {
    if (prevConversationIdRef.current !== conversationId) {
      prevConversationIdRef.current = conversationId
      stopTTS()
    }
  }, [conversationId, stopTTS])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      mountedRef.current = false
      voiceModeRef.current = false
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
      // Stop TTS playback
      try {
        currentSourceRef.current?.stop()
      } catch { /* noop */ }
      audioQueueRef.current = []
      try {
        audioContextRef.current?.suspend()
      } catch { /* noop */ }
      if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
      if (statusPollRef.current) clearTimeout(statusPollRef.current)
    }
  }, [])

  const handleVoiceMessage = useCallback((data: Record<string, unknown>) => {
    const type = data.type as string

    if (type === 'voice_transcription') {
      if (mountedRef.current) setIsTranscribing(false)
      if (mountedRef.current) setVoiceError(null)
    } else if (type === 'voice_status') {
      const status = data.status as string
      if (status === 'error') {
        if (mountedRef.current) setVoiceError(data.error as string || 'Voice error')
        if (mountedRef.current) setIsTranscribing(false)
      } else if (status === 'empty') {
        if (mountedRef.current) setIsTranscribing(false)
        if (mountedRef.current) setTransientError('No speech detected — try speaking louder or closer to the mic')
      } else if (status === 'transcribing') {
        if (mountedRef.current) setIsTranscribing(true)
        if (mountedRef.current) setVoiceError(null)
      }
    } else if (type === 'tts_audio') {
      // Store metadata — next binary frame is the audio data
      pendingTTSMetaRef.current = {
        sampleRate: (data.sample_rate as number) || 24000,
        format: (data.format as string) || 'pcm_s16le',
        chunkIndex: (data.chunk_index as number) || 0,
      }
    } else if (type === 'tts_status') {
      const status = data.status as string
      if (status === 'idle') {
        // Backend stopped TTS — clear any pending meta
        pendingTTSMetaRef.current = null
      }
    }
  }, [setTransientError])

  return {
    voiceMode,
    voiceAvailable,
    voiceReady,
    voiceLoading,
    isListening,
    isSpeechDetected,
    isTranscribing,
    isSpeaking,
    voiceError: statusVoiceError ?? voiceError,
    toggleVoiceMode,
    handleVoiceMessage,
    handleBinaryMessage,
    stopTTS,
  }
}
