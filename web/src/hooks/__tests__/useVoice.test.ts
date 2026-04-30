import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useVoice } from '../useVoice'

const voiceMocks = vi.hoisted(() => ({
  mockMicVADNew: vi.fn(),
  mockEncodeWAV: vi.fn(() => new ArrayBuffer(8)),
  mockArrayBufferToBase64: vi.fn(() => 'encoded-audio'),
}))

let lastVADConfig: Record<string, any> | null = null
let lastWorkletNode: {
  connect: ReturnType<typeof vi.fn>
  disconnect: ReturnType<typeof vi.fn>
  port: {
    close: ReturnType<typeof vi.fn>
    onmessage: ((event: MessageEvent<Float32Array>) => void) | null
  }
} | null = null

interface StartedSource {
  buffer: AudioBuffer | null
  connect: ReturnType<typeof vi.fn>
  onended: ((event?: Event) => void) | null
  start: ReturnType<typeof vi.fn>
  stop: ReturnType<typeof vi.fn>
}

let startedSources: StartedSource[] = []

vi.mock('@ricky0123/vad-web', () => ({
  MicVAD: {
    new: (config: Record<string, unknown>) => voiceMocks.mockMicVADNew(config),
  },
  utils: {
    encodeWAV: voiceMocks.mockEncodeWAV,
    arrayBufferToBase64: voiceMocks.mockArrayBufferToBase64,
  },
}))

class MockAudioContext {
  sampleRate = 48_000
  state: AudioContextState = 'running'
  destination = {}
  audioWorklet = {
    addModule: vi.fn(async () => {}),
  }

  constructor(_opts?: AudioContextOptions) {}

  resume() {
    return Promise.resolve()
  }

  suspend() {
    return Promise.resolve()
  }

  close() {
    return Promise.resolve()
  }

  createMediaStreamSource() {
    return {
      connect: vi.fn(),
      disconnect: vi.fn(),
    } as unknown as MediaStreamAudioSourceNode
  }

  createBufferSource() {
    const source: StartedSource = {
      buffer: null as AudioBuffer | null,
      connect: vi.fn(),
      start: vi.fn(() => {
        startedSources.push(source)
      }),
      stop: vi.fn(),
      onended: null as ((event?: Event) => void) | null,
    }
    return source as unknown as AudioBufferSourceNode
  }

  createBuffer(_channels: number, length: number) {
    const buffer = new Float32Array(length)
    return {
      getChannelData: () => buffer,
    } as unknown as AudioBuffer
  }
}

class MockAudioWorkletNode {
  constructor(
    _ctx: BaseAudioContext,
    _name: string,
    _options?: AudioWorkletNodeOptions,
  ) {
    lastWorkletNode = {
      connect: this.connect,
      disconnect: this.disconnect,
      port: this.port,
    }
  }

  connect = vi.fn()
  disconnect = vi.fn()
  port = {
    close: vi.fn(),
    onmessage: null as ((event: MessageEvent<Float32Array>) => void) | null,
  }
}

function workletMessage(data: Float32Array): MessageEvent<Float32Array> {
  return { data } as MessageEvent<Float32Array>
}

function pcmChunk(marker: number): ArrayBuffer {
  const buffer = new ArrayBuffer(2)
  new Int16Array(buffer)[0] = marker
  return buffer
}

describe('useVoice', () => {
  let wsRef: { current: { readyState: number; send: ReturnType<typeof vi.fn> } | null }
  let getUserMediaMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    lastVADConfig = null
    lastWorkletNode = null
    startedSources = []

    wsRef = {
      current: {
        readyState: 1,
        send: vi.fn(),
      },
    }

    vi.stubGlobal('WebSocket', {
      OPEN: 1,
    })
    vi.stubGlobal('AudioContext', MockAudioContext)
    vi.stubGlobal('AudioWorkletNode', MockAudioWorkletNode)

    getUserMediaMock = vi.fn(async () => ({
      getTracks: () => [{ stop: vi.fn() }],
    }))

    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      value: true,
    })
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: {
        mediaDevices: {
          getUserMedia: getUserMediaMock,
        },
      },
    })

    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        enabled: true,
        stt_enabled: true,
        tts_enabled: true,
        stt_available: true,
        tts_available: true,
        voice_ready: true,
        voice_loading: false,
      }),
    })) as any

    voiceMocks.mockMicVADNew.mockImplementation(async (config: Record<string, unknown>) => {
      lastVADConfig = config
      return {
        start: vi.fn(),
        destroy: vi.fn(),
      }
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('warms voice and resends TTS state when the socket reconnects', async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        enabled: true,
        stt_enabled: true,
        tts_enabled: true,
        stt_available: true,
        tts_available: true,
        voice_ready: false,
        voice_loading: false,
      }),
    })) as any

    const { rerender } = renderHook(
      ({ connected }) => useVoice(
        wsRef as any,
        'conv-1',
        0,
        { sttEnabled: true, ttsEnabled: true, voiceInputMode: 'ptt' },
        connected,
      ),
      { initialProps: { connected: false } },
    )

    rerender({ connected: true })

    await waitFor(() => {
      const payloads = wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))
      expect(payloads).toEqual(expect.arrayContaining([
        expect.objectContaining({ type: 'voice_prepare', conversation_id: 'conv-1' }),
        expect.objectContaining({ type: 'voice_mode_toggle', conversation_id: 'conv-1', enabled: true }),
      ]))
    })
  })

  it('records PTT audio and sends WAV using the actual capture sample rate', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-ptt',
      0,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await act(async () => {
      await result.current.startRecording()
    })

    expect(getUserMediaMock).toHaveBeenCalledWith({
      audio: expect.objectContaining({ autoGainControl: true }),
    })
    expect(lastWorkletNode?.port.onmessage).toBeTruthy()

    act(() => {
      lastWorkletNode?.port.onmessage?.(workletMessage(new Float32Array(16_000).fill(0.25)))
    })

    await act(async () => {
      await result.current.stopRecording()
    })

    expect(voiceMocks.mockEncodeWAV).toHaveBeenCalledWith(expect.any(Float32Array), 1, 48_000, 1, 16)

    const payloads = wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'tts_stop', conversation_id: 'conv-ptt' }),
      expect.objectContaining({
        type: 'voice_audio',
        conversation_id: 'conv-ptt',
        audio_data: 'encoded-audio',
        mime_type: 'audio/wav',
      }),
    ]))
  })

  it('discards short PTT captures without sending audio', async () => {
    const nowSpy = vi.spyOn(performance, 'now')
    nowSpy.mockReturnValueOnce(0).mockReturnValueOnce(100)

    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-short',
      0,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await act(async () => {
      await result.current.startRecording()
    })

    act(() => {
      lastWorkletNode?.port.onmessage?.(workletMessage(new Float32Array([0.2, -0.1])))
    })

    await act(async () => {
      await result.current.stopRecording()
    })

    await waitFor(() => {
      expect(result.current.voiceError).toBe('Too short — hold longer')
    })
    expect(wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ type: 'voice_audio' })]),
    )
  })

  it('keeps VAD auto-submit and barge-in behavior', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-vad',
      0,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'vad' },
      true,
    ))

    await waitFor(() => {
      expect(voiceMocks.mockMicVADNew).toHaveBeenCalledTimes(1)
      expect(result.current.isListening).toBe(true)
    })

    act(() => {
      lastVADConfig?.onSpeechStart?.()
      lastVADConfig?.onSpeechEnd?.(new Float32Array([0.3, -0.2]))
    })

    expect(voiceMocks.mockEncodeWAV).toHaveBeenCalledWith(expect.any(Float32Array), 1, 16_000, 1, 16)
    const payloads = wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'tts_stop', conversation_id: 'conv-vad' }),
      expect.objectContaining({ type: 'voice_audio', conversation_id: 'conv-vad' }),
    ]))
  })

  it('drops incoming TTS audio when the playback queue is full', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-tts',
      0,
      { sttEnabled: false, ttsEnabled: true, voiceInputMode: 'ptt' },
      true,
    ))

    for (let index = 1; index <= 52; index++) {
      act(() => {
        result.current.handleVoiceMessage({
          type: 'tts_audio',
          sample_rate: 24_000,
          format: 'pcm_s16le',
          chunk_index: index,
        })
        result.current.handleBinaryMessage(pcmChunk(index))
      })
    }

    await waitFor(() => {
      expect(result.current.voiceError).toBe('Audio dropped — connection too slow')
    })
    expect(warnSpy).toHaveBeenCalledWith('Voice: Audio queue full, dropping incoming chunk')
    expect(startedSources).toHaveLength(1)

    for (let count = 0; count < 50; count++) {
      act(() => {
        startedSources[startedSources.length - 1].onended?.(new Event('ended'))
      })
    }

    expect(startedSources).toHaveLength(51)
    const playedMarkers = startedSources.map((source) => {
      const sample = source.buffer?.getChannelData(0)[0] ?? 0
      return Math.round(sample * 32768)
    })
    expect(playedMarkers).toEqual(Array.from({ length: 51 }, (_, index) => index + 1))
  })

  it('stops TTS and cancels recording on conversation switch', async () => {
    const trackStop = vi.fn()
    getUserMediaMock.mockResolvedValueOnce({
      getTracks: () => [{ stop: trackStop }],
    })

    const { result, rerender } = renderHook(
      ({ switchKey }) => useVoice(
        wsRef as any,
        'conv-switch',
        switchKey,
        { sttEnabled: true, ttsEnabled: true, voiceInputMode: 'ptt' },
        true,
      ),
      { initialProps: { switchKey: 0 } },
    )

    await act(async () => {
      await result.current.startRecording()
    })
    expect(result.current.isRecording).toBe(true)
    wsRef.current?.send.mockClear()

    act(() => {
      result.current.handleVoiceMessage({
        type: 'tts_audio',
        sample_rate: 24_000,
        format: 'pcm_s16le',
        chunk_index: 1,
      })
      result.current.handleBinaryMessage(pcmChunk(7))
    })
    expect(startedSources).toHaveLength(1)

    rerender({ switchKey: 1 })

    await waitFor(() => {
      expect(result.current.isRecording).toBe(false)
    })
    expect(startedSources[0].stop).toHaveBeenCalled()
    expect(trackStop).toHaveBeenCalled()
    expect(wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ type: 'tts_stop', conversation_id: 'conv-switch' }),
      ]),
    )
  })
})
