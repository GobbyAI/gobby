import type { VoiceInputMode } from "../../hooks/useSettings";
import { cn } from "../../lib/utils";

interface VoiceStatusBarProps {
  voiceLoading?: boolean;
  isListening: boolean;
  isSpeechDetected: boolean;
  isRecording: boolean;
  isTranscribing: boolean;
  voiceInputMode: VoiceInputMode;
  voiceError?: string | null;
}

export function VoiceStatusBar({
  voiceLoading = false,
  isListening,
  isSpeechDetected,
  isRecording,
  isTranscribing,
  voiceInputMode,
  voiceError,
}: VoiceStatusBarProps) {
  const isPttRecording = voiceInputMode === "ptt" && isRecording;
  const hasVisibleStatus = Boolean(
    voiceLoading ||
    isListening ||
    isPttRecording ||
    isTranscribing ||
    voiceError,
  );

  return (
    <div
      className={cn(
        "voice-status-bar flex min-h-6 shrink-0 items-center gap-2 bg-[var(--bg-primary)] px-3 text-[length:var(--text-xs)] leading-none text-[var(--text-muted)]",
        !hasVisibleStatus && "voice-status-bar--idle pointer-events-none",
      )}
      data-testid="voice-status-bar"
      role={hasVisibleStatus ? "status" : undefined}
      aria-live={hasVisibleStatus ? "polite" : undefined}
      aria-hidden={hasVisibleStatus ? undefined : true}
    >
      {voiceLoading ? (
        <>
          <SpinnerIcon />
          <span className="text-muted-foreground">Warming voice...</span>
        </>
      ) : isTranscribing ? (
        <>
          <SpinnerIcon />
          <span className="text-muted-foreground">Transcribing...</span>
        </>
      ) : isPttRecording ? (
        <>
          <span
            className="h-2 w-2 animate-pulse rounded-full bg-destructive-foreground"
            data-voice-motion
          />
          <span className="text-destructive-foreground">Recording...</span>
        </>
      ) : isListening && isSpeechDetected ? (
        <>
          <div
            className="flex h-3 items-end gap-0.5"
            role="status"
            aria-live="polite"
            aria-label="Microphone listening"
          >
            {[8, 11, 6, 10].map((h, i) => (
              <span
                key={i}
                aria-hidden="true"
                className="w-0.5 animate-pulse rounded-full bg-success-foreground"
                data-voice-motion
                style={{ height: `${h}px`, animationDelay: `${i * 0.1}s` }}
              />
            ))}
          </div>
          <span className="text-success-foreground">Listening...</span>
        </>
      ) : isListening ? (
        <>
          <span
            className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent"
            data-voice-motion
          />
          <span className="text-muted-foreground">Ready — speak to send</span>
        </>
      ) : null}
      {voiceError && (
        <span className="ml-auto text-destructive-foreground">
          {voiceError}
        </span>
      )}
    </div>
  );
}

function SpinnerIcon() {
  return (
    <svg
      className="animate-spin"
      data-voice-motion
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    >
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </svg>
  );
}
