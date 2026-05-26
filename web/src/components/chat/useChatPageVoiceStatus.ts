import type { VoiceProps } from "../../types/chat";

export interface UseChatPageVoiceStatusResult {
  voiceInputMode: "ptt" | "vad";
  showVoiceStatusBar: boolean;
  voiceStatusWarming: boolean;
}

export function useChatPageVoiceStatus(
  voice: VoiceProps,
): UseChatPageVoiceStatusResult {
  const voiceInputMode = voice.voiceInputMode ?? "ptt";
  const wantsVoiceStatusSlot = Boolean(
    voice.ttsEnabled || (voice.sttEnabled && voiceInputMode === "vad"),
  );
  const isPttRecording = Boolean(
    voiceInputMode === "ptt" && voice.isRecording,
  );
  const showVoiceStatusBar = Boolean(
    wantsVoiceStatusSlot ||
      voice.voiceLoading ||
      voice.isListening ||
      isPttRecording ||
      voice.isTranscribing ||
      voice.voiceError,
  );
  const voiceStatusWarming = Boolean(
    voice.voiceLoading ||
      (wantsVoiceStatusSlot &&
        voice.voiceAvailable &&
        !voice.voiceReady &&
        !voice.voiceError),
  );

  return {
    voiceInputMode,
    showVoiceStatusBar,
    voiceStatusWarming,
  };
}
