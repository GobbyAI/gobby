import { useCallback, useEffect, useRef, useState } from 'react'
import type { VoiceInputMode } from './useSettings'
import type { RawVoiceStatus } from './voiceStatus'
import { useTTSPlayback } from './voice/useTTSPlayback'
import { useVoiceCapture } from './voice/useVoiceCapture'
import { useVoiceStatus } from './voice/useVoiceStatus'

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
  prepareTTSPlayback: () => void
  startRecording: () => Promise<void>
  stopRecording: () => Promise<void>
  cancelRecording: () => void
  handleVoiceMessage: (data: Record<string, unknown>) => void
  handleBinaryMessage: (data: ArrayBuffer) => void
  stopTTS: () => void
}

export function useVoice(
  wsRef: React.RefObject<WebSocket | null>,
  conversationId: string,
  conversationSwitchKey: number,
  opts: VoiceOptions,
  socketConnected: boolean,
): UseVoiceReturn {
  const { sttEnabled, ttsEnabled, voiceInputMode } = opts

  const [voiceError, setVoiceError] = useState<string | null>(null)
  const errorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const conversationIdRef = useRef(conversationId)
  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

  const setTransientError = useCallback((msg: string, ms = 3000) => {
    if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
    setVoiceError(msg)
    errorTimerRef.current = setTimeout(() => setVoiceError(null), ms)
  }, [])

  const clearTransientError = useCallback(() => {
    if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
    setVoiceError(null)
  }, [])

  const status = useVoiceStatus({
    wsRef,
    conversationId,
    socketConnected,
    sttEnabled,
    ttsEnabled,
  })

  const playback = useTTSPlayback({
    wsRef,
    conversationIdRef,
    ttsEnabled,
    setTransientError,
  })

  const capture = useVoiceCapture({
    wsRef,
    conversationIdRef,
    sttEnabled,
    voiceInputMode,
    voiceReady: status.voiceReady,
    sttAvailable: status.sttAvailable,
    setTransientError,
    clearTransientError,
    onBargeIn: playback.stopTTS,
  })

  const {
    clearPendingTTSMeta,
    handleBinaryMessage,
    handleTTSAudioMeta,
    isSpeaking,
    prepareTTSPlayback,
    stopTTS,
  } = playback
  const {
    cancelRecording,
    isListening,
    isRecording,
    isSpeechDetected,
    isTranscribing,
    setIsTranscribing,
    startRecording,
    stopRecording,
  } = capture
  const {
    applyVoiceStatus,
    markVoicePreparing,
    startStatusPolling,
    statusVoiceError,
    voiceAvailable,
    voiceLoading,
    voiceReady,
  } = status

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
      if (errorTimerRef.current) clearTimeout(errorTimerRef.current)
    }
  }, [])

  const handleVoiceMessage = useCallback((data: Record<string, unknown>) => {
    const type = data.type as string

    if (type === 'voice_transcription') {
      setIsTranscribing(false)
      setVoiceError(null)
    } else if (type === 'voice_status') {
      const voiceStatus = data.status as string
      if (voiceStatus === 'error') {
        setVoiceError(data.error as string || 'Voice error')
        setIsTranscribing(false)
      } else if (voiceStatus === 'empty') {
        setIsTranscribing(false)
        setTransientError('No speech detected — try speaking louder or closer to the mic')
      } else if (voiceStatus === 'transcribing') {
        setIsTranscribing(true)
        setVoiceError(null)
      } else if (voiceStatus === 'preparing') {
        markVoicePreparing()
        setVoiceError(null)
        startStatusPolling()
      }
      if ('voice_ready' in data || 'voice_loading' in data) {
        applyVoiceStatus(data as RawVoiceStatus)
      }
    } else if (type === 'tts_audio') {
      if (!ttsEnabled) return
      handleTTSAudioMeta(data)
    } else if (type === 'tts_status') {
      const ttsStatus = data.status as string
      if (ttsStatus === 'idle') {
        clearPendingTTSMeta()
      }
    }
  }, [
    applyVoiceStatus,
    clearPendingTTSMeta,
    handleTTSAudioMeta,
    markVoicePreparing,
    setIsTranscribing,
    setTransientError,
    startStatusPolling,
    ttsEnabled,
  ])

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
    prepareTTSPlayback,
    startRecording,
    stopRecording,
    cancelRecording,
    handleVoiceMessage,
    handleBinaryMessage,
    stopTTS,
  }
}
