export interface RawVoiceStatus {
  enabled?: boolean
  stt_enabled?: boolean
  tts_enabled?: boolean
  stt_available?: boolean
  tts_available?: boolean
  voice_ready?: boolean
  voice_loading?: boolean
  stt_warmup_error?: string | null
  tts_warmup_error?: string | null
}

export interface ParsedVoiceStatus {
  sttConfigEnabled: boolean
  ttsConfigEnabled: boolean
  sttAvailable: boolean
  ttsAvailable: boolean
  voiceAvailable: boolean
  voiceReady: boolean
  voiceLoading: boolean
  warmupError: string | null
}

export function parseVoiceStatus(
  data: RawVoiceStatus | null,
  isSecureContext: boolean,
): ParsedVoiceStatus {
  const baseEnabled = Boolean(data?.enabled)
  const sttConfigEnabled = Boolean(baseEnabled && data?.stt_enabled)
  const ttsConfigEnabled = Boolean(baseEnabled && data?.tts_enabled)
  const sttAvailable = Boolean(sttConfigEnabled && isSecureContext && data?.stt_available)
  const ttsAvailable = Boolean(ttsConfigEnabled && data?.tts_available)
  const voiceAvailable = Boolean(sttAvailable || ttsAvailable)
  const voiceReady = Boolean(voiceAvailable && data?.voice_ready)
  const voiceLoading = Boolean(voiceAvailable && data?.voice_loading)
  const warmupError =
    (data?.stt_warmup_error as string | null | undefined) ??
    (data?.tts_warmup_error as string | null | undefined) ??
    null

  return {
    sttConfigEnabled,
    ttsConfigEnabled,
    sttAvailable,
    ttsAvailable,
    voiceAvailable,
    voiceReady,
    voiceLoading,
    warmupError,
  }
}
