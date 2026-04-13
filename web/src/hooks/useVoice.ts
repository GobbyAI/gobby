import { useState, useCallback, useRef, useEffect } from 'react'
import { MicVAD, utils } from '@ricky0123/vad-web'
import type { VoiceInputMode } from './useSettings'
import { parseVoiceStatus, type RawVoiceStatus } from './voiceStatus'

const MAX_AUDIO_QUEUE_SIZE = 50
const MIN_PTT_DURATION_MS = 250

interface VoiceOptions {
  sttEnabled: boolean
  ttsEnabled: boolean
  voiceInputMode: VoiceInputMode
}

interface VoiceState {
  voiceAvailable: boolean
  voiceReady: boolean
  voiceLoading: boolean
  isListening: boolean
  isSpeechDetected: boolean
  isRecording: boolean
  isTranscribing: boolean
  isSpeaking: boolean
  voiceError: string | null
}

export interface UseVoiceReturn extends VoiceState {
  startRecording: () => Promise<void>
  stopRecording: () => Promise<void>
  cancelRecording: () => void
  handleVoiceMessage: (data: Record<string, unknown>) => void
  handleBinaryMessage: (data: ArrayBuffer) => void
  stopTTS: () => void
}

interface TTSMeta {
  sampleRate: number
  format: string
  chunkIndex: number
}

interface RecordingContext {
  ctx: AudioContext
  stream: MediaStream
  source: MediaStreamAudioSourceNode
  processor: ScriptProcessorNode
  sampleRate: number
}

function createVoiceRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    try {
      return crypto.randomUUID()
    } catch {
      // Ignore secure context failures and fall back below.
    }
  }
  return `voice-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function useVoice(
  wsRef: React.RefObject<WebSocket | null>,
  conversationId: string,
  conversationSwitchKey: number,
  opts: VoiceOptions,
  socketConnected: boolean,
): UseVoiceReturn {
  const { sttEnabled, ttsEnabled, voiceInputMode } = opts

  const [voiceAvailable, setVoiceAvailable] = useState(false)
  const [voiceReady, setVoiceReady] = useState(false)
  const [voiceLoading, setVoiceLoading] = useState(false)
  const [sttAvailable, setSttAvailable] = useState(false)
  const [isListening, setIsListening] = useState(false)
  const [isSpeechDetected, setIsSpeechDetected] = useState(false)
  const [isRecording, setIsRecording] = useState(false)
  const [isTranscribing, setIsTranscribing] = useState(false)
  const [isSpeaking, setIsSpeaking] = useState(false)
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [statusVoiceError, setStatusVoiceError] = useState<string | null>(null)

  const vadRef = useRef<MicVAD | null>(null)
  const recCtxRef = useRef<RecordingContext | null>(null)
  const samplesRef = useRef<Float32Array[]>([])
  const recordingStartRef = useRef<number | null>(null)
  const errorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const statusPollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollCancelledRef = useRef(false)
  const warmupKeyRef = useRef<string | null>(null)

  const conversationIdRef = useRef(conversationId)
  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

  const audioContextRef = useRef<AudioContext | null>(null)
  const audioQueueRef = useRef<AudioBuffer[]>([])
  const isPlayingRef = useRef(false)
  const currentSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const pendingTTSMetaRef = useRef<TTSMeta | null>(null)
  const mountedRef = useRef(true)
  const playErrorCountRef = useRef(0)

  const setTransientError = useCallback((msg: string, ms = 3000) => {
    if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
    setVoiceError(msg)
    errorTimerRef.current = setTimeout(() => setVoiceError(null), ms)
  }, [])

  const getAudioContext = useCallback((): AudioContext | null => {
    try {
      if (!audioContextRef.current || audioContextRef.current.state === 'closed') {
        audioContextRef.current = new AudioContext()
      }
      if (audioContextRef.current.state === 'suspended') {
        audioContextRef.current.resume().catch(() => {})
      }
      return audioContextRef.current
    } catch (err) {
      console.error('Voice: Failed to create AudioContext:', err)
      return null
    }
  }, [])

  const playNextChunk = useCallback(function playNextChunk() {
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
    if (!ttsEnabled) return

    const ctx = getAudioContext()
    if (!ctx) return

    try {
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

      if (!isPlayingRef.current) {
        isPlayingRef.current = true
        playNextChunk()
      }
    } catch (err) {
      console.error('Voice: Failed to queue audio chunk:', err)
    }
  }, [getAudioContext, playNextChunk, ttsEnabled])

  const stopTTS = useCallback(() => {
    try {
      currentSourceRef.current?.stop()
    } catch {
      // Already stopped.
    }
    currentSourceRef.current = null
    audioQueueRef.current = []
    isPlayingRef.current = false
    pendingTTSMetaRef.current = null
    if (mountedRef.current) setIsSpeaking(false)

    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'tts_stop',
        conversation_id: conversationIdRef.current,
      }))
    }
  }, [wsRef])

  const tearDownRecording = useCallback(() => {
    const rec = recCtxRef.current
    if (!rec) return

    try {
      rec.processor.disconnect()
    } catch {
      // noop
    }
    try {
      rec.source.disconnect()
    } catch {
      // noop
    }
    try {
      rec.ctx.close().catch(() => {})
    } catch {
      // noop
    }
    rec.stream.getTracks().forEach((track) => track.stop())
    recCtxRef.current = null
    recordingStartRef.current = null
  }, [])

  const cancelRecording = useCallback(() => {
    tearDownRecording()
    samplesRef.current = []
    if (mountedRef.current) setIsRecording(false)
  }, [tearDownRecording])

  const sendVoiceAudio = useCallback((audio: Float32Array, sampleRate: number) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('Voice: WebSocket not open, discarding audio')
      setTransientError('Connection lost — try again')
      return false
    }

    try {
      if (mountedRef.current) setIsTranscribing(true)
      const wavBuffer = utils.encodeWAV(audio, 1, sampleRate, 1, 16)
      const base64 = utils.arrayBufferToBase64(wavBuffer)

      ws.send(JSON.stringify({
        type: 'voice_audio',
        conversation_id: conversationIdRef.current,
        audio_data: base64,
        mime_type: 'audio/wav',
        request_id: createVoiceRequestId(),
      }))
      return true
    } catch (err) {
      console.error('Voice: Failed to encode/send audio:', err)
      if (mountedRef.current) setIsTranscribing(false)
      setTransientError('Failed to process audio')
      return false
    }
  }, [setTransientError, wsRef])

  const stopRecording = useCallback(async () => {
    const rec = recCtxRef.current
    if (!rec) return

    const startedAt = recordingStartRef.current
    tearDownRecording()

    const buffers = samplesRef.current
    samplesRef.current = []
    if (mountedRef.current) setIsRecording(false)

    const totalLength = buffers.reduce((sum, chunk) => sum + chunk.length, 0)
    if (totalLength === 0) return

    const flattened = new Float32Array(totalLength)
    let offset = 0
    for (const chunk of buffers) {
      flattened.set(chunk, offset)
      offset += chunk.length
    }

    const sampleDurationMs = (totalLength / rec.sampleRate) * 1000
    const elapsedDurationMs = startedAt ? performance.now() - startedAt : 0
    const durationMs = Math.max(sampleDurationMs, elapsedDurationMs)

    if (durationMs < MIN_PTT_DURATION_MS) {
      setTransientError('Too short — hold longer')
      return
    }

    sendVoiceAudio(flattened, rec.sampleRate)
  }, [sendVoiceAudio, setTransientError, tearDownRecording])

  const handleBinaryMessage = useCallback((data: ArrayBuffer) => {
    const meta = pendingTTSMetaRef.current
    if (!meta || !ttsEnabled) return
    pendingTTSMetaRef.current = null
    queueAudioChunk(data, meta.sampleRate)
  }, [queueAudioChunk, ttsEnabled])

  const startStatusPolling = useCallback(() => {
    if (statusPollRef.current) clearTimeout(statusPollRef.current)

    const syncVoiceStatus = async () => {
      try {
        const res = await fetch('/api/voice/status')
        const data = res.ok ? (await res.json() as RawVoiceStatus) : null
        if (pollCancelledRef.current) return

        const parsed = parseVoiceStatus(data, window.isSecureContext)
        setSttAvailable(parsed.sttAvailable)
        setVoiceAvailable(parsed.voiceAvailable)
        setVoiceReady(parsed.voiceReady)
        setVoiceLoading(parsed.voiceLoading)
        setStatusVoiceError(parsed.warmupError)

        if (parsed.voiceReady) {
          warmupKeyRef.current = null
        }

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
  }, [])

  useEffect(() => {
    pollCancelledRef.current = false
    startStatusPolling()

    return () => {
      pollCancelledRef.current = true
      if (statusPollRef.current) clearTimeout(statusPollRef.current)
    }
  }, [startStatusPolling])

  useEffect(() => {
    if (!ttsEnabled) {
      stopTTS()
    }
  }, [stopTTS, ttsEnabled])

  useEffect(() => {
    const wantsVoice = sttEnabled || ttsEnabled
    if (!wantsVoice) {
      warmupKeyRef.current = null
      return
    }
    if (!socketConnected || voiceReady || voiceLoading) return

    const warmupKey = `${conversationId}:${socketConnected}:${sttEnabled}:${ttsEnabled}`
    if (warmupKeyRef.current === warmupKey) return

    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    warmupKeyRef.current = warmupKey
    setVoiceLoading(true)
    ws.send(JSON.stringify({
      type: 'voice_prepare',
      conversation_id: conversationId,
    }))
    startStatusPolling()
  }, [
    conversationId,
    socketConnected,
    startStatusPolling,
    sttEnabled,
    ttsEnabled,
    voiceLoading,
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

  useEffect(() => {
    if (!sttEnabled || voiceInputMode !== 'vad' || !voiceReady || !sttAvailable) {
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
      setIsListening(false)
      setIsSpeechDetected(false)
      return
    }

    let cancelled = false

    const startVAD = async () => {
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
            stopTTS()
          },
          onSpeechEnd: (audio: Float32Array) => {
            setIsSpeechDetected(false)
            sendVoiceAudio(audio, 16000)
          },
          onVADMisfire: () => {
            setIsSpeechDetected(false)
          },
        })

        if (cancelled) {
          vad.destroy()
          return
        }

        vadRef.current = vad
        vad.start()
        setIsListening(true)
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Microphone access denied'
        setTransientError(msg)
        console.error('Failed to start VAD:', err)
      }
    }

    void startVAD()

    return () => {
      cancelled = true
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
      setIsListening(false)
      setIsSpeechDetected(false)
    }
  }, [sendVoiceAudio, setTransientError, startStatusPolling, sttAvailable, sttEnabled, stopTTS, voiceInputMode, voiceReady])

  const startRecording = useCallback(async () => {
    if (!sttEnabled || voiceInputMode !== 'ptt' || isRecording) return
    if (!window.isSecureContext) {
      setTransientError('Microphone requires HTTPS or localhost')
      return
    }

    stopTTS()
    setVoiceError(null)

    let stream: MediaStream | null = null
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      })

      const ctx = new AudioContext({ sampleRate: 16000 })
      const source = ctx.createMediaStreamSource(stream)
      const processor = ctx.createScriptProcessor(4096, 1, 1)
      samplesRef.current = []
      recordingStartRef.current = performance.now()

      processor.onaudioprocess = (event) => {
        samplesRef.current.push(new Float32Array(event.inputBuffer.getChannelData(0)))
      }

      source.connect(processor)
      processor.connect(ctx.destination)

      recCtxRef.current = {
        ctx,
        stream,
        source,
        processor,
        sampleRate: ctx.sampleRate,
      }
      setIsRecording(true)
    } catch (err) {
      stream?.getTracks().forEach((track) => track.stop())
      tearDownRecording()
      samplesRef.current = []
      setIsRecording(false)
      const msg = err instanceof Error ? err.message : 'Microphone access denied'
      setTransientError(msg)
      console.error('Failed to start recording:', err)
    }
  }, [isRecording, setTransientError, stopTTS, sttEnabled, tearDownRecording, voiceInputMode])

  useEffect(() => {
    if (!sttEnabled || voiceInputMode !== 'ptt') {
      cancelRecording()
    }
  }, [cancelRecording, sttEnabled, voiceInputMode])

  const prevSwitchKeyRef = useRef(conversationSwitchKey)
  useEffect(() => {
    if (prevSwitchKeyRef.current !== conversationSwitchKey) {
      prevSwitchKeyRef.current = conversationSwitchKey
      stopTTS()
      cancelRecording()
    }
  }, [cancelRecording, conversationSwitchKey, stopTTS])

  useEffect(() => {
    return () => {
      mountedRef.current = false
      cancelRecording()
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
      try {
        currentSourceRef.current?.stop()
      } catch {
        // noop
      }
      audioQueueRef.current = []
      try {
        audioContextRef.current?.suspend()
      } catch {
        // noop
      }
      if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
      if (statusPollRef.current) clearTimeout(statusPollRef.current)
    }
  }, [cancelRecording])

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
        if (mountedRef.current) {
          setTransientError('No speech detected — try speaking louder or closer to the mic')
        }
      } else if (status === 'transcribing') {
        if (mountedRef.current) setIsTranscribing(true)
        if (mountedRef.current) setVoiceError(null)
      }
    } else if (type === 'tts_audio') {
      if (!ttsEnabled) return
      pendingTTSMetaRef.current = {
        sampleRate: (data.sample_rate as number) || 24000,
        format: (data.format as string) || 'pcm_s16le',
        chunkIndex: (data.chunk_index as number) || 0,
      }
    } else if (type === 'tts_status') {
      const status = data.status as string
      if (status === 'idle') {
        pendingTTSMetaRef.current = null
      }
    }
  }, [setTransientError, ttsEnabled])

  return {
    voiceAvailable,
    voiceReady,
    voiceLoading,
    isListening,
    isSpeechDetected,
    isRecording,
    isTranscribing,
    isSpeaking,
    voiceError: statusVoiceError ?? voiceError,
    startRecording,
    stopRecording,
    cancelRecording,
    handleVoiceMessage,
    handleBinaryMessage,
    stopTTS,
  }
}
