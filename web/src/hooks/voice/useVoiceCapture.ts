import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { MicVAD, utils } from '@ricky0123/vad-web'
import type { VoiceInputMode } from '../useSettings'

const MIN_PTT_DURATION_MS = 250
const VOICE_AUDIO_TARGET_SAMPLE_RATE = 16_000
export const VOICE_AUDIO_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
const VOICE_TRANSCRIPTION_BACKEND_TIMEOUT_MS = 120_000
export const VOICE_TRANSCRIPTION_WATCHDOG_MS = VOICE_TRANSCRIPTION_BACKEND_TIMEOUT_MS + 5_000
const VOICE_CAPTURE_WORKLET_URL = '/audio-worklets/voice-capture-processor.js'
const DEFAULT_ONNX_WASM_BASE_PATH = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.24.1/dist/'
const ONNX_WASM_BASE_PATH =
  import.meta.env.VITE_VAD_ONNX_WASM_BASE_PATH || DEFAULT_ONNX_WASM_BASE_PATH
const MIN_VAD_DURATION_MS = 350
const MIN_VAD_RMS = 0.012
const MIN_VAD_PEAK = 0.06
const VAD_FRAME_OPTIONS = {
  positiveSpeechThreshold: 0.65,
  negativeSpeechThreshold: 0.45,
  minSpeechFrames: 6,
  redemptionFrames: 12,
  preSpeechPadFrames: 2,
  submitUserSpeechOnPause: false,
} as const
const VOICE_CONVERSATION_LOG_PREFIX_LENGTH = 8

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
  ensureConversationId?: () => Promise<string | null>
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

function logVoice(event: string, details: Record<string, unknown> = {}) {
  const cleanDetails = Object.fromEntries(
    Object.entries(details).filter(([, value]) => value !== undefined),
  )
  console.info(`[gobby:voice] ${event}`, cleanDetails)
}

function conversationLogPrefix(conversationId: string): string | null {
  return conversationId ? conversationId.slice(0, VOICE_CONVERSATION_LOG_PREFIX_LENGTH) : null
}

function encodedPayloadBytes(value: string): number {
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(value).byteLength
  }
  return value.length
}

function downsamplePcm(
  audio: Float32Array,
  inputSampleRate: number,
  outputSampleRate: number,
): Float32Array {
  if (inputSampleRate <= outputSampleRate || audio.length === 0) return audio

  const ratio = inputSampleRate / outputSampleRate
  const outputLength = Math.max(1, Math.round(audio.length / ratio))
  const output = new Float32Array(outputLength)

  for (let index = 0; index < outputLength; index += 1) {
    const sourceIndex = index * ratio
    const leftIndex = Math.floor(sourceIndex)
    const rightIndex = Math.min(leftIndex + 1, audio.length - 1)
    const weight = sourceIndex - leftIndex
    output[index] = audio[leftIndex] + (audio[rightIndex] - audio[leftIndex]) * weight
  }

  return output
}

function normalizePttAudio(
  audio: Float32Array,
  sampleRate: number,
): { audio: Float32Array; sampleRate: number; resampled: boolean } {
  if (sampleRate <= VOICE_AUDIO_TARGET_SAMPLE_RATE) {
    return { audio, sampleRate, resampled: false }
  }
  return {
    audio: downsamplePcm(audio, sampleRate, VOICE_AUDIO_TARGET_SAMPLE_RATE),
    sampleRate: VOICE_AUDIO_TARGET_SAMPLE_RATE,
    resampled: true,
  }
}

function getAudioStats(audio: Float32Array, sampleRate: number) {
  let peak = 0
  let sumSquares = 0
  for (const sample of audio) {
    const abs = Math.abs(sample)
    if (abs > peak) peak = abs
    sumSquares += sample * sample
  }
  const rms = audio.length > 0 ? Math.sqrt(sumSquares / audio.length) : 0
  const durationMs = sampleRate > 0 ? (audio.length / sampleRate) * 1000 : 0
  return { durationMs, peak, rms }
}

function isLikelyVadSpeech(audio: Float32Array, sampleRate: number) {
  const stats = getAudioStats(audio, sampleRate)
  return {
    accepted:
      stats.durationMs >= MIN_VAD_DURATION_MS &&
      stats.rms >= MIN_VAD_RMS &&
      stats.peak >= MIN_VAD_PEAK,
    stats,
  }
}

export function useVoiceCapture({
  wsRef,
  conversationIdRef,
  projectIdRef,
  sttEnabled,
  voiceInputMode,
  voiceReady,
  sttAvailable,
  ensureConversationId,
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
    const hadRecording = recCtxRef.current !== null
      || recordingStartRef.current !== null
      || samplesRef.current.length > 0
    if (hadRecording) {
      logVoice('ptt_cancel', {
        bufferedChunks: samplesRef.current.length,
        sampleRate: recCtxRef.current?.sampleRate,
      })
    }
    tearDownRecording()
    samplesRef.current = []
    if (mountedRef.current) setIsRecording(false)
  }, [tearDownRecording])

  const sendVoiceAudio = useCallback(async (audio: Float32Array, sampleRate: number) => {
    const requestId = createVoiceRequestId()
    const ws = wsRef.current
    const wsState = ws?.readyState ?? 'missing'

    if (!audio.length) {
      logVoice('send_failed', { requestId, reason: 'empty_audio', sampleRate, wsState })
      resetTranscriptionRequest()
      setTransientError('No audio captured — try again')
      return false
    }

    if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
      logVoice('send_failed', { requestId, reason: 'invalid_sample_rate', sampleRate, wsState })
      resetTranscriptionRequest()
      setTransientError('Failed to process audio')
      return false
    }

    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logVoice('send_failed', { requestId, reason: 'websocket_not_open', sampleRate, wsState })
      console.warn('Voice: WebSocket not open, discarding audio')
      resetTranscriptionRequest()
      setTransientError('Connection lost — try again')
      return false
    }

    let conversationId = conversationIdRef.current
    if (!conversationId && ensureConversationId) {
      try {
        conversationId = await ensureConversationId() ?? conversationIdRef.current
      } catch (err) {
        logVoice('send_failed', {
          requestId,
          reason: 'session_create_failed',
          sampleRate,
          wsState,
        })
        console.error('Voice: Failed to create chat session before audio send:', err)
        resetTranscriptionRequest()
        setTransientError('Failed to create chat session')
        return false
      }
    }
    if (!conversationId) {
      logVoice('send_failed', { requestId, reason: 'missing_conversation', sampleRate, wsState })
      resetTranscriptionRequest()
      setTransientError('Missing conversation')
      return false
    }
    const conversationPrefix = conversationLogPrefix(conversationId)

    let base64: string
    try {
      const wavBuffer = utils.encodeWAV(audio, 1, sampleRate, 1, 16)
      base64 = utils.arrayBufferToBase64(wavBuffer)
      if (!base64) {
        logVoice('send_failed', {
          requestId,
          conversationPrefix,
          reason: 'empty_encoded_audio',
          sampleRate,
          wsState,
        })
        resetTranscriptionRequest()
        setTransientError('No audio captured — try again')
        return false
      }
    } catch (err) {
      logVoice('send_failed', {
        requestId,
        conversationPrefix,
        reason: 'encode_error',
        sampleRate,
        audioSamples: audio.length,
        wsState,
      })
      console.error('Voice: Failed to encode audio:', err)
      resetTranscriptionRequest()
      setTransientError('Failed to process audio')
      return false
    }

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

    const payloadJson = JSON.stringify(payload)
    const payloadBytes = encodedPayloadBytes(payloadJson)
    logVoice('send_attempt', {
      requestId,
      conversationPrefix,
      sampleRate,
      audioSamples: audio.length,
      payloadBytes,
      wsState,
    })
    if (payloadBytes > VOICE_AUDIO_MAX_PAYLOAD_BYTES) {
      logVoice('send_failed', {
        requestId,
        conversationPrefix,
        reason: 'payload_too_large',
        sampleRate,
        payloadBytes,
        maxPayloadBytes: VOICE_AUDIO_MAX_PAYLOAD_BYTES,
        wsState,
      })
      resetTranscriptionRequest()
      setTransientError('Audio clip too long — try a shorter recording')
      return false
    }

    try {
      ws.send(payloadJson)
      startTranscriptionRequest(requestId)
      logVoice('send_ok', {
        requestId,
        conversationPrefix,
        sampleRate,
        payloadBytes,
        wsState,
      })
      return true
    } catch (err) {
      logVoice('send_failed', {
        requestId,
        conversationPrefix,
        reason: 'websocket_send_error',
        sampleRate,
        payloadBytes,
        wsState,
      })
      console.error('Voice: Failed to send audio:', err)
      resetTranscriptionRequest()
      setTransientError('Failed to send audio')
      return false
    }
  }, [
    conversationIdRef,
    ensureConversationId,
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
    if (totalLength === 0) {
      logVoice('ptt_stop', { sampleRate: rec.sampleRate, inputSamples: 0 })
      await sendVoiceAudio(new Float32Array(), rec.sampleRate)
      return
    }

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
      logVoice('ptt_stop', {
        inputSampleRate: rec.sampleRate,
        inputSamples: flattened.length,
        durationMs: Math.round(durationMs),
        reason: 'too_short',
      })
      setTransientError('Too short — hold longer')
      return
    }

    const normalized = normalizePttAudio(flattened, rec.sampleRate)
    logVoice('ptt_stop', {
      inputSampleRate: rec.sampleRate,
      outputSampleRate: normalized.sampleRate,
      inputSamples: flattened.length,
      outputSamples: normalized.audio.length,
      durationMs: Math.round(durationMs),
      resampled: normalized.resampled,
    })
    await sendVoiceAudio(normalized.audio, normalized.sampleRate)
  }, [sendVoiceAudio, setTransientError, tearDownRecording])

  useEffect(() => {
    if (!sttEnabled || voiceInputMode !== 'vad' || !voiceReady || !sttAvailable) {
      if (sttEnabled && voiceInputMode === 'vad') {
        logVoice('vad_waiting', { voiceReady, sttAvailable })
      }
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
        logVoice('vad_start', {})
        const vad = await MicVAD.new({
          baseAssetPath: '/',
          onnxWASMBasePath: ONNX_WASM_BASE_PATH,
          ...VAD_FRAME_OPTIONS,
          onSpeechStart: () => {
            logVoice('vad_speech_start', {})
            if (mountedRef.current) setIsSpeechDetected(true)
            onBargeIn()
          },
          onSpeechEnd: (audio: Float32Array) => {
            const speechGate = isLikelyVadSpeech(audio, VOICE_AUDIO_TARGET_SAMPLE_RATE)
            logVoice('vad_speech_end', {
              audioSamples: audio.length,
              sampleRate: VOICE_AUDIO_TARGET_SAMPLE_RATE,
              durationMs: Math.round(speechGate.stats.durationMs),
              rms: Number(speechGate.stats.rms.toFixed(4)),
              peak: Number(speechGate.stats.peak.toFixed(4)),
            })
            if (mountedRef.current) setIsSpeechDetected(false)
            if (!speechGate.accepted) {
              logVoice('vad_reject', {
                reason: 'low_energy_or_too_short',
                durationMs: Math.round(speechGate.stats.durationMs),
                rms: Number(speechGate.stats.rms.toFixed(4)),
                peak: Number(speechGate.stats.peak.toFixed(4)),
              })
              return
            }
            void sendVoiceAudio(audio, VOICE_AUDIO_TARGET_SAMPLE_RATE)
          },
          onVADMisfire: () => {
            logVoice('vad_misfire', {})
            if (mountedRef.current) setIsSpeechDetected(false)
          },
        })

        if (cancelled) {
          vad.destroy()
          return
        }

        vadRef.current = vad
        vad.start()
        logVoice('vad_ready', {})
        if (mountedRef.current) setIsListening(true)
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Microphone access denied'
        logVoice('vad_error', { error: msg })
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
      logVoice('ptt_error', { reason: 'insecure_context' })
      setTransientError('Microphone requires HTTPS or localhost')
      return
    }

    onBargeIn()
    clearTransientError()
    logVoice('ptt_start', {})

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
      logVoice('ptt_started', { sampleRate: ctx.sampleRate })
      if (mountedRef.current) setIsRecording(true)
    } catch (err) {
      stream?.getTracks().forEach((track) => track.stop())
      tearDownRecording()
      samplesRef.current = []
      if (mountedRef.current) setIsRecording(false)
      const msg = err instanceof Error ? err.message : 'Microphone access denied'
      logVoice('ptt_error', { error: msg })
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
