import type { VoiceInputMode } from "../../hooks/useSettings";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { MicIcon, SpeakerIcon } from "./ChatInputIcons";

interface ChatInputVoiceControlsProps {
  disabled?: boolean;
  sttEnabled?: boolean;
  ttsEnabled?: boolean;
  voiceInputMode?: VoiceInputMode;
  isRecording?: boolean;
  isSpeaking?: boolean;
  voiceLoading?: boolean;
  voiceReady?: boolean;
  prepareTTSPlayback?: () => void;
  stopTTS?: () => void;
  onSttEnabledChange?: (enabled: boolean) => void;
  onTtsEnabledChange?: (enabled: boolean) => void;
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void;
}

function getMicLabel(
  sttEnabled: boolean,
  voiceInputMode: VoiceInputMode,
): string {
  if (!sttEnabled) return "Microphone off; enable push to talk";
  if (voiceInputMode === "ptt")
    return "Microphone in push to talk; switch to VAD";
  return "Microphone in VAD; turn microphone off";
}

export function ChatInputVoiceControls({
  disabled = false,
  sttEnabled = false,
  ttsEnabled = false,
  voiceInputMode = "ptt",
  isRecording = false,
  isSpeaking = false,
  voiceLoading = false,
  voiceReady = false,
  prepareTTSPlayback,
  stopTTS,
  onSttEnabledChange,
  onTtsEnabledChange,
  onVoiceInputModeChange,
}: ChatInputVoiceControlsProps) {
  const showTtsControl = Boolean(onTtsEnabledChange);
  const showSttControl = Boolean(onSttEnabledChange && onVoiceInputModeChange);
  const ttsWarming = ttsEnabled && voiceLoading && !voiceReady;
  const micLabel = getMicLabel(sttEnabled, voiceInputMode);
  const voiceModeLabel = voiceInputMode === "vad" ? "VAD" : "PTT";

  if (!showTtsControl && !showSttControl) return null;

  const handleTtsClick = () => {
    if (isSpeaking) {
      stopTTS?.();
      return;
    }
    if (!ttsEnabled) {
      prepareTTSPlayback?.();
    }
    onTtsEnabledChange?.(!ttsEnabled);
  };

  const handleMicClick = () => {
    if (isRecording) return;
    if (!sttEnabled) {
      onVoiceInputModeChange?.("ptt");
      onSttEnabledChange?.(true);
      return;
    }
    if (voiceInputMode === "ptt") {
      onVoiceInputModeChange?.("vad");
      return;
    }
    onSttEnabledChange?.(false);
  };

  return (
    <>
      {showTtsControl && (
        <Button
          size="icon"
          variant="ghost"
          // Permit "stop speaking" while composer controls are disabled.
          disabled={disabled && !isSpeaking}
          onClick={handleTtsClick}
          title={
            isSpeaking
              ? "Stop speaking"
              : ttsWarming
                ? "Text-to-speech warming up"
                : ttsEnabled
                  ? "Disable text-to-speech"
                  : "Enable text-to-speech"
          }
          aria-label={
            ttsWarming ? "Text-to-speech warming up" : "Toggle text-to-speech"
          }
          aria-pressed={ttsEnabled || isSpeaking}
          aria-busy={ttsWarming}
          className={cn(
            "chat-input-voice-toggle text-[var(--text-secondary)] hover:text-[var(--text-primary)]",
            (ttsEnabled || isSpeaking) &&
              "chat-input-voice-toggle--active text-accent",
            ttsWarming &&
              "chat-input-voice-toggle--warming animate-pulse text-accent",
          )}
        >
          <SpeakerIcon muted={!ttsEnabled && !isSpeaking} />
        </Button>
      )}
      {showSttControl && (
        <div className="chat-input-voice-mic inline-flex min-w-0 shrink-0 items-center gap-1">
          <Button
            size="icon"
            variant="ghost"
            disabled={disabled || isRecording}
            onClick={handleMicClick}
            title={micLabel}
            aria-label={micLabel}
            aria-pressed={sttEnabled}
            className={cn(
              "chat-input-voice-toggle text-[var(--text-secondary)] hover:text-[var(--text-primary)]",
              sttEnabled && "chat-input-voice-toggle--active text-accent",
            )}
          >
            <MicIcon muted={!sttEnabled} />
          </Button>
          {sttEnabled && (
            <span
              className={cn(
                "chat-input-voice-mode min-w-7 text-[length:var(--text-2xs)] leading-none font-bold text-[var(--text-muted)]",
                isRecording && "chat-input-voice-mode--disabled opacity-50",
              )}
              aria-disabled={isRecording || undefined}
            >
              {voiceModeLabel}
            </span>
          )}
        </div>
      )}
    </>
  );
}
