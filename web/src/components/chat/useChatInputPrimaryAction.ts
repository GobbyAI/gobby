import { useCallback, useEffect, useRef, type KeyboardEvent, type PointerEvent } from 'react'
import { cn } from '../../lib/utils'

export type ChatInputPrimaryButtonKind = 'stop' | 'mic-idle' | 'mic-recording' | 'send'

interface UseChatInputPrimaryActionOptions {
  cancelRecording?: () => void
  disabled: boolean
  hasInput: boolean
  hasPendingUploads: boolean
  hasUploadErrors: boolean
  isRecording: boolean
  isStreaming: boolean
  onStop?: () => void
  onSubmit: () => void
  pttEnabled: boolean
  startRecording?: () => Promise<void>
  sttEnabled: boolean
  stopRecording?: () => Promise<void>
}

export function useChatInputPrimaryAction({
  cancelRecording,
  disabled,
  hasInput,
  hasPendingUploads,
  hasUploadErrors,
  isRecording,
  isStreaming,
  onStop,
  onSubmit,
  pttEnabled,
  startRecording,
  sttEnabled,
  stopRecording,
}: UseChatInputPrimaryActionOptions) {
  const primaryButtonRef = useRef<HTMLButtonElement>(null)
  const holdTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const holdActiveRef = useRef(false)
  const latchedRef = useRef(false)
  const activePointerIdRef = useRef<number | null>(null)
  const activeKeyRef = useRef<string | null>(null)
  const pointerStartedWhileRecordingRef = useRef(false)

  const primaryButtonKind: ChatInputPrimaryButtonKind = isStreaming
    ? 'stop'
    : pttEnabled && (isRecording || !hasInput)
      ? isRecording
        ? 'mic-recording'
        : 'mic-idle'
      : 'send'

  const clearHoldTimer = useCallback(() => {
    if (holdTimerRef.current) {
      clearTimeout(holdTimerRef.current)
      holdTimerRef.current = null
    }
  }, [])

  const resetPTTGesture = useCallback(() => {
    clearHoldTimer()
    holdActiveRef.current = false
    activePointerIdRef.current = null
    activeKeyRef.current = null
    pointerStartedWhileRecordingRef.current = false
  }, [clearHoldTimer])

  useEffect(() => {
    if (!isRecording) {
      latchedRef.current = false
      resetPTTGesture()
    }
  }, [isRecording, resetPTTGesture])

  useEffect(() => {
    if (!pttEnabled) {
      latchedRef.current = false
      resetPTTGesture()
    }
  }, [pttEnabled, resetPTTGesture])

  useEffect(() => {
    if (!sttEnabled) {
      latchedRef.current = false
      resetPTTGesture()
    }
  }, [resetPTTGesture, sttEnabled])

  useEffect(() => {
    if (!isRecording || !cancelRecording) return

    const handleWindowKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      latchedRef.current = false
      resetPTTGesture()
      cancelRecording()
    }

    window.addEventListener('keydown', handleWindowKeyDown)
    return () => window.removeEventListener('keydown', handleWindowKeyDown)
  }, [cancelRecording, isRecording, resetPTTGesture])

  useEffect(() => {
    return () => clearHoldTimer()
  }, [clearHoldTimer])

  const handleMicPointerDown = useCallback(
    (event: PointerEvent<HTMLButtonElement>) => {
      if (disabled || primaryButtonKind === 'stop' || !startRecording) return
      if (event.button !== 0) return
      if (activeKeyRef.current !== null) return

      event.preventDefault()
      primaryButtonRef.current?.setPointerCapture(event.pointerId)
      activePointerIdRef.current = event.pointerId
      pointerStartedWhileRecordingRef.current = Boolean(isRecording && latchedRef.current)
      holdActiveRef.current = false
      clearHoldTimer()
      holdTimerRef.current = setTimeout(() => {
        holdActiveRef.current = true
      }, 250)

      if (!isRecording) {
        void startRecording()
      }
    },
    [clearHoldTimer, disabled, isRecording, primaryButtonKind, startRecording],
  )

  const handleMicPointerUp = useCallback(
    (event: PointerEvent<HTMLButtonElement>) => {
      if (activePointerIdRef.current === null || event.pointerId !== activePointerIdRef.current) {
        return
      }

      if (primaryButtonRef.current?.hasPointerCapture(event.pointerId)) {
        primaryButtonRef.current.releasePointerCapture(event.pointerId)
      }

      const wasHold = holdActiveRef.current
      const wasLatchedStopTap = pointerStartedWhileRecordingRef.current
      resetPTTGesture()

      if (wasHold || wasLatchedStopTap) {
        latchedRef.current = false
        void stopRecording?.()
        return
      }

      latchedRef.current = true
    },
    [resetPTTGesture, stopRecording],
  )

  const handleMicPointerMove = useCallback(
    (event: PointerEvent<HTMLButtonElement>) => {
      if (!holdActiveRef.current || !cancelRecording) return
      if (activePointerIdRef.current !== event.pointerId) return

      const rect = primaryButtonRef.current?.getBoundingClientRect()
      if (!rect) return

      const outside =
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom

      if (outside) {
        if (primaryButtonRef.current?.hasPointerCapture(event.pointerId)) {
          primaryButtonRef.current.releasePointerCapture(event.pointerId)
        }
        latchedRef.current = false
        resetPTTGesture()
        cancelRecording()
      }
    },
    [cancelRecording, resetPTTGesture],
  )

  const handleMicPointerCancel = useCallback(
    (event: PointerEvent<HTMLButtonElement>) => {
      if (activePointerIdRef.current === null || event.pointerId !== activePointerIdRef.current) {
        return
      }
      if (primaryButtonRef.current?.hasPointerCapture(event.pointerId)) {
        primaryButtonRef.current.releasePointerCapture(event.pointerId)
      }
      latchedRef.current = false
      resetPTTGesture()
      cancelRecording?.()
    },
    [cancelRecording, resetPTTGesture],
  )

  const handleMicKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key !== 'Enter' && event.key !== ' ') return
      if (event.repeat || activeKeyRef.current !== null) return
      if (activePointerIdRef.current !== null) return
      if (disabled || primaryButtonKind === 'stop' || !startRecording) return

      event.preventDefault()
      activeKeyRef.current = event.key
      if (!isRecording) {
        void startRecording()
      }
    },
    [disabled, isRecording, primaryButtonKind, startRecording],
  )

  const handleMicKeyUp = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key !== 'Enter' && event.key !== ' ') return
      if (activeKeyRef.current !== event.key) return

      event.preventDefault()
      activeKeyRef.current = null

      latchedRef.current = false
      void stopRecording?.()
    },
    [stopRecording],
  )

  const handlePrimaryButtonClick = useCallback(() => {
    if (primaryButtonKind === 'stop') {
      onStop?.()
      return
    }
    if (primaryButtonKind === 'send') {
      onSubmit()
    }
  }, [onStop, onSubmit, primaryButtonKind])

  const primaryButtonDisabled =
    primaryButtonKind === 'send'
      ? !isStreaming && (disabled || !hasInput || hasPendingUploads || hasUploadErrors)
      : primaryButtonKind === 'stop'
        ? false
        : disabled || !startRecording || !stopRecording || !cancelRecording

  const primaryButtonLabel =
    primaryButtonKind === 'stop'
      ? 'Stop generating'
      : primaryButtonKind === 'send'
        ? 'Send message'
        : primaryButtonKind === 'mic-recording'
          ? 'Push to talk recording'
          : 'Start push to talk'

  const primaryButtonClassName = cn(
    'chat-input-primary-button inline-flex h-[36px] w-[36px] self-end items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 pointer-coarse:h-11 pointer-coarse:w-11',
    primaryButtonKind === 'stop'
      ? 'border border-border bg-transparent text-foreground hover:bg-muted'
      : 'bg-accent text-accent-foreground hover:bg-accent-hover',
    primaryButtonKind === 'mic-recording' &&
      'ring-2 ring-red-500/70 ring-offset-2 ring-offset-background animate-pulse motion-reduce:animate-none',
  )

  return {
    handleMicPointerCancel,
    handleMicKeyDown,
    handleMicKeyUp,
    handleMicPointerDown,
    handleMicPointerMove,
    handleMicPointerUp,
    handlePrimaryButtonClick,
    primaryButtonClassName,
    primaryButtonDisabled,
    primaryButtonKind,
    primaryButtonLabel,
    primaryButtonRef,
  }
}
