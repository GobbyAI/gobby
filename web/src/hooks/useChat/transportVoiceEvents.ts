import { type VoiceTranscriptionMessage } from "./core";
import type { UseChatTransportParams } from "./transportTypes";

const VOICE_EVENT_TYPES = new Set([
  "voice_transcription",
  "voice_audio_chunk",
  "voice_status",
  "tts_audio",
  "tts_status",
]);

export function isVoiceTransportEvent(type: string) {
  return VOICE_EVENT_TYPES.has(type);
}

export function handleVoiceTransportEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  try {
    // When STT transcription arrives, inject it as a user message and
    // register the request_id so the assistant's response stream is accepted.
    if (data.type === "voice_transcription") {
      const voiceMsg = data as unknown as VoiceTranscriptionMessage;
      const text = typeof voiceMsg.text === "string" ? voiceMsg.text : "";
      const reqId =
        typeof voiceMsg.request_id === "string" ? voiceMsg.request_id : "";
      if (text && reqId) {
        const voiceConversationId =
          typeof voiceMsg.conversation_id === "string"
            ? voiceMsg.conversation_id
            : "";
        const attachedVoice =
          voiceConversationId === ctx.attachedSessionIdRef.current &&
          ctx.sessionInteractionModeRef.current === "proxy";
        if (!attachedVoice) {
          ctx.activeRequestIdRef.current = reqId;
        }
        ctx.setMessages((prev) => [
          ...prev,
          {
            id: `user-voice-${reqId}`,
            role: "user" as const,
            content: text,
            timestamp: new Date(),
          },
        ]);
        if (!attachedVoice) {
          ctx.setIsStreaming(true);
          ctx.setIsThinking(true);
        }
      }
    }
    ctx.handleVoiceMessageRef.current(data);
  } catch (err) {
    console.error("Voice message handling error:", err);
    ctx.setIsStreaming(false);
    ctx.setIsThinking(false);
  }
}
