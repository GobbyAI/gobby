interface VoiceStatusBarProps {
  isListening: boolean
  isSpeechDetected: boolean
  isTranscribing: boolean
  voiceError?: string | null
}

export function VoiceStatusBar({
  isListening,
  isSpeechDetected,
  isTranscribing,
  voiceError,
}: VoiceStatusBarProps) {
  return (
    <div className="flex items-center gap-2 px-4 py-1.5 bg-muted border-b border-border text-xs">
      {isTranscribing ? (
        <>
          <SpinnerIcon />
          <span className="text-muted-foreground">Transcribing...</span>
        </>
      ) : isListening && isSpeechDetected ? (
        <>
          <div
            className="flex gap-0.5 items-end h-3"
            role="status"
            aria-live="polite"
            aria-label="Microphone listening"
          >
            {[8, 11, 6, 10].map((h, i) => (
              <span
                key={i}
                aria-hidden="true"
                className="w-0.5 bg-green-400 rounded-full animate-pulse"
                style={{ height: `${h}px`, animationDelay: `${i * 0.1}s` }}
              />
            ))}
          </div>
          <span className="text-green-400">Listening...</span>
        </>
      ) : isListening ? (
        <>
          <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
          <span className="text-muted-foreground">Ready — speak to send</span>
        </>
      ) : null}
      {voiceError && (
        <span className="text-destructive-foreground ml-auto">{voiceError}</span>
      )}
    </div>
  )
}

function SpinnerIcon() {
  return (
    <svg className="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </svg>
  )
}
