import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { MicVAD, utils } from '@ricky0123/vad-web'
import type { VoiceInputMode } from '../useSettings'

const MIN_PTT_DURATION_MS = 250
const VOICE_TRANSCRIPTION_BACKEND_TIMEOUT_MS = 120_000
export const VOICE_TRANSCRIPTION_WATCHDOG_MS = VOICE_TRANSCRIPTION_BACKEND_TIMEOUT_MS + 5_000
const VOICE_CAPTURE_WORKLET_URL = '/audio-worklets/voice-capture-processor.js'
const DEFAULT_ONNX_WASM_BASE_PATH = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.24.1/dist/'
const ONNX_WASM_BASE_PATH =
  import.meta.env.VITE_VAD_ONNX_WASM_BASE_PATH || DEFAULT_ONNX_WASM_BASE_PATH

interface RecordingContext {
  ctx: AudioContext
  stream: MediaStream
  source: MediaStreamAudioSourceNode
  workletNode: AudioWorkletNode
  sampleRate: number
}

interface VoiceCaptureOptions {
  wsRef: RefObject<WebSocket | null>
  conversationIdRef: RefObject<string>
  projectIdRef?: RefObject<string | null>
  sttEnabled: boolean
  voiceInputMode: VoiceInputMode
  voiceReady: boolean
  sttAvailable: boolean
  setTransientError: (msg: string, ms?: number) => void
  clearTransientError: () => void
  onBargeIn: () => void
}

interface VoiceCaptureReturn {
  isListening: boolean
  isSpeechDetected: boolean
  isRecording: boolean
  isTranscribing: boolean
  markTranscriptionInProgress: (requestId?: unknown) => boolean
  finishTranscriptionRequest: (requestId?: unknown) => boolean
  resetTranscriptionRequest: () => void
  startRecording: () => Promise<void>
  stopRecording: () => Promise<void>
  cancelRecording: () => void
}

export interface VoiceAudioPayload {
  type: 'voice_audio'
  conversation_id: string
  audio_data: string
  mime_type: 'audio/wav'
  request_id: string
  project_id?: string
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

function requestIdFrom(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

export function useVoiceCapture({
  wsRef,
  conversationIdRef,
  projectIdRef,
  sttEnabled,
  voiceInputMode,
  voiceReady,
  sttAvailable,
  setTransientError,
  clearTransientError,
  onBargeIn,
}: VoiceCaptureOptions): VoiceCaptureReturn {
  const [isListening, setIsListening] = useState(false)
  const [isSpeechDetected, setIsSpeechDetected] = useState(false)
  const [isRecording, setIsRecording] = useState(false)
  const [isTranscribing, setIsTranscribing] = useState(false)

  const vadRef = useRef<MicVAD | null>(null)
  const recCtxRef = useRef<RecordingContext | null>(null)
  const samplesRef = useRef<Float32Array[]>([])
  const recordingStartRef = useRef<number | null>(null)
  const activeTranscriptionRequestRef = useRef<string | null>(null)
  const transcriptionWatchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const clearTranscriptionWatchdog = useCallback(() => {
    if (transcriptionWatchdogRef.current) {
      clearTimeout(transcriptionWatchdogRef.current)
      transcriptionWatchdogRef.current = null
    }
  }, [])

  const resetTranscriptionRequest = useCallback(() => {
    activeTranscriptionRequestRef.current = null
    clearTranscriptionWatchdog()
    if (mountedRef.current) setIsTranscribing(false)
  }, [clearTranscriptionWatchdog])

  const startTranscriptionRequest = useCallback((requestId: string) => {
    activeTranscriptionRequestRef.current = requestId
    clearTranscriptionWatchdog()
    if (mountedRef.current) setIsTranscribing(true)

    transcriptionWatchdogRef.current = setTimeout(() => {
      if (activeTranscriptionRequestRef.current !== requestId) return
      activeTranscriptionRequestRef.current = null
      transcriptionWatchdogRef.current = null
      if (mountedRef.current) setIsTranscribing(false)
      setTransientError('Transcription timed out — try again')
    }, VOICE_TRANSCRIPTION_WATCHDOG_MS)
  }, [clearTranscriptionWatchdog, setTransientError])

  const markTranscriptionInProgress = useCallback((requestId?: unknown) => {
    const incomingRequestId = requestIdFrom(requestId)
    const activeRequestId = activeTranscriptionRequestRef.current
    if (incomingRequestId && activeRequestId && incomingRequestId !== activeRequestId) {
      return false
    }

    startTranscriptionRequest(activeRequestId ?? incomingRequestId ?? createVoiceRequestId())
    return true
  }, [startTranscriptionRequest])

  const finishTranscriptionRequest = useCallback((requestId?: unknown) => {
    const incomingRequestId = requestIdFrom(requestId)
    const activeRequestId = activeTranscriptionRequestRef.current
    if (incomingRequestId && activeRequestId && incomingRequestId !== activeRequestId) {
      return false
    }

    resetTranscriptionRequest()
    return true
  }, [resetTranscriptionRequest])

  const tearDownRecording = useCallback(() => {
    const rec = recCtxRef.current
    if (!rec) return

    try {
      rec.workletNode.port.onmessage = null
      rec.workletNode.port.close()
    } catch {
      // noop
    }
    try {
      rec.workletNode.disconnect()
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
    const requestId = createVoiceRequestId()
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn('Voice: WebSocket not open, discarding audio')
      resetTranscriptionRequest()
      setTransientError('Connection lost — try again')
      return false
    }

    try {
      const conversationId = conversationIdRef.current
      if (!conversationId) {
        resetTranscriptionRequest()
        setTransientError('Missing conversation')
        return false
      }
      const wavBuffer = utils.encodeWAV(audio, 1, sampleRate, 1, 16)
      const base64 = utils.arrayBufferToBase64(wavBuffer)

      startTranscriptionRequest(requestId)
      const payload: VoiceAudioPayload = {
        type: 'voice_audio',
        conversation_id: conversationId,
        audio_data: base64,
        mime_type: 'audio/wav',
        request_id: requestId,
      }
      if (projectIdRef?.current) {
        payload.project_id = projectIdRef.current
      }

      ws.send(JSON.stringify(payload))
      return true
    } catch (err) {
      console.error('Voice: Failed to encode/send audio:', err)
      resetTranscriptionRequest()
      setTransientError('Failed to process audio')
      return false
    }
  }, [
    conversationIdRef,
    projectIdRef,
    resetTranscriptionRequest,
    setTransientError,
    startTranscriptionRequest,
    wsRef,
  ])

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

  useEffect(() => {
    if (!sttEnabled || voiceInputMode !== 'vad' || !voiceReady || !sttAvailable) {
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
      if (mountedRef.current) setIsListening(false)
      if (mountedRef.current) setIsSpeechDetected(false)
      return
    }

    let cancelled = false

    const startVAD = async () => {
      try {
        const vad = await MicVAD.new({
          baseAssetPath: '/',
          onnxWASMBasePath: ONNX_WASM_BASE_PATH,
          positiveSpeechThreshold: 0.6,
          negativeSpeechThreshold: 0.35,
          minSpeechFrames: 6,
          redemptionFrames: 12,
          preSpeechPadFrames: 10,
          submitUserSpeechOnPause: false,
          onSpeechStart: () => {
            if (mountedRef.current) setIsSpeechDetected(true)
            onBargeIn()
          },
          onSpeechEnd: (audio: Float32Array) => {
            if (mountedRef.current) setIsSpeechDetected(false)
            sendVoiceAudio(audio, 16000)
          },
          onVADMisfire: () => {
            if (mountedRef.current) setIsSpeechDetected(false)
          },
        })

        if (cancelled) {
          vad.destroy()
          return
        }

        vadRef.current = vad
        vad.start()
        if (mountedRef.current) setIsListening(true)
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
      if (mountedRef.current) setIsListening(false)
      if (mountedRef.current) setIsSpeechDetected(false)
    }
  }, [
    onBargeIn,
    sendVoiceAudio,
    setTransientError,
    sttAvailable,
    sttEnabled,
    voiceInputMode,
    voiceReady,
  ])

  const startRecording = useCallback(async () => {
    if (!sttEnabled || voiceInputMode !== 'ptt' || isRecording) return
    if (!window.isSecureContext) {
      setTransientError('Microphone requires HTTPS or localhost')
      return
    }

    onBargeIn()
    clearTransientError()

    let stream: MediaStream | null = null
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })

      const ctx = new AudioContext({ sampleRate: 16000 })
      const source = ctx.createMediaStreamSource(stream)
      if (!ctx.audioWorklet) {
        throw new Error('AudioWorklet is not available in this browser')
      }
      await ctx.audioWorklet.addModule(VOICE_CAPTURE_WORKLET_URL)
      const workletNode = new AudioWorkletNode(ctx, 'voice-capture-processor', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        channelCount: 1,
      })
      samplesRef.current = []
      recordingStartRef.current = performance.now()

      workletNode.port.onmessage = (event: MessageEvent<Float32Array>) => {
        samplesRef.current.push(new Float32Array(event.data))
      }

      source.connect(workletNode)
      workletNode.connect(ctx.destination)

      recCtxRef.current = {
        ctx,
        stream,
        source,
        workletNode,
        sampleRate: ctx.sampleRate,
      }
      if (mountedRef.current) setIsRecording(true)
    } catch (err) {
      stream?.getTracks().forEach((track) => track.stop())
      tearDownRecording()
      samplesRef.current = []
      if (mountedRef.current) setIsRecording(false)
      const msg = err instanceof Error ? err.message : 'Microphone access denied'
      setTransientError(msg)
      console.error('Failed to start recording:', err)
    }
  }, [
    clearTransientError,
    isRecording,
    onBargeIn,
    setTransientError,
    sttEnabled,
    tearDownRecording,
    voiceInputMode,
  ])

  useEffect(() => {
    if (!sttEnabled || voiceInputMode !== 'ptt') {
      cancelRecording()
    }
  }, [cancelRecording, sttEnabled, voiceInputMode])

  useEffect(() => {
    return () => {
      mountedRef.current = false
      clearTranscriptionWatchdog()
      cancelRecording()
      if (vadRef.current) {
        vadRef.current.destroy()
        vadRef.current = null
      }
    }
  }, [cancelRecording, clearTranscriptionWatchdog])

  return {
    isListening,
    isSpeechDetected,
    isRecording,
    isTranscribing,
    markTranscriptionInProgress,
    finishTranscriptionRequest,
    resetTranscriptionRequest,
    startRecording,
    stopRecording,
    cancelRecording,
  }
}
