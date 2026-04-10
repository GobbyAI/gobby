import { useEffect, useState } from 'react'
import { parseVoiceStatus, type RawVoiceStatus } from './voiceStatus'

export interface VoiceCapabilities {
  sttConfigEnabled: boolean
  ttsConfigEnabled: boolean
  sttAvailable: boolean
  ttsAvailable: boolean
  loading: boolean
}

const DEFAULT_CAPABILITIES: VoiceCapabilities = {
  sttConfigEnabled: false,
  ttsConfigEnabled: false,
  sttAvailable: false,
  ttsAvailable: false,
  loading: true,
}

export function useVoiceCapabilities(): VoiceCapabilities {
  const [caps, setCaps] = useState<VoiceCapabilities>(DEFAULT_CAPABILITIES)

  useEffect(() => {
    let cancelled = false

    const loadCapabilities = async () => {
      try {
        const response = await fetch('/api/voice/status')
        const data = response.ok ? (await response.json() as RawVoiceStatus) : null
        if (cancelled) return

        const parsed = parseVoiceStatus(data, window.isSecureContext)
        setCaps({
          sttConfigEnabled: parsed.sttConfigEnabled,
          ttsConfigEnabled: parsed.ttsConfigEnabled,
          sttAvailable: parsed.sttAvailable,
          ttsAvailable: parsed.ttsAvailable,
          loading: false,
        })
      } catch {
        if (!cancelled) {
          setCaps({
            ...DEFAULT_CAPABILITIES,
            loading: false,
          })
        }
      }
    }

    void loadCapabilities()

    return () => {
      cancelled = true
    }
  }, [])

  return caps
}
