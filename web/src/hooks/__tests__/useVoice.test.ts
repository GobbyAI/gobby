import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useVoice } from '../useVoice'
import { parseVoiceStatus } from '../voiceStatus'
import {
  VOICE_AUDIO_MAX_PAYLOAD_BYTES,
  VOICE_TRANSCRIPTION_WATCHDOG_MS,
} from '../voice/useVoiceCapture'
import {
  resetVoicePrepareCacheForTests,
  seedVoicePrepareCacheForTests,
  useVoiceStatus,
  voicePrepareCacheKeysForTests,
} from '../voice/useVoiceStatus'

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
let mockAudioContextInitialState: AudioContextState = 'running'
let deferredAudioContextResume: { promise: Promise<void>; resolve: () => void } | null = null
let audioContextResumeCalls = 0

function deferAudioContextResume() {
  let resolve!: () => void
  const promise = new Promise<void>((innerResolve) => {
    resolve = innerResolve
  })
  deferredAudioContextResume = { promise, resolve }
  return deferredAudioContextResume
}

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
  state: AudioContextState = mockAudioContextInitialState
  destination = {}
  audioWorklet = {
    addModule: vi.fn(async () => {}),
  }

  constructor(_opts?: AudioContextOptions) {}

  resume() {
    audioContextResumeCalls += 1
    const markRunning = () => {
      this.state = 'running'
    }
    if (deferredAudioContextResume) {
      return deferredAudioContextResume.promise.then(markRunning)
    }
    markRunning()
    return Promise.resolve()
  }

  suspend() {
    this.state = 'suspended'
    return Promise.resolve()
  }

  close() {
    this.state = 'closed'
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
  let projectIdRef: { current: string | null }
  let getUserMediaMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    resetVoicePrepareCacheForTests()
    vi.spyOn(console, 'info').mockImplementation(() => {})
    lastVADConfig = null
    lastWorkletNode = null
    startedSources = []
    mockAudioContextInitialState = 'running'
    deferredAudioContextResume = null
    audioContextResumeCalls = 0

    wsRef = {
      current: {
        readyState: 1,
        send: vi.fn(),
      },
    }
    projectIdRef = { current: null }

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
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  const sentPayloads = () => wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw)) ?? []
  const voicePreparePayloads = () => (
    sentPayloads().filter((payload) => payload.type === 'voice_prepare')
  )
  const voiceAudioPayloads = () => (
    sentPayloads().filter((payload) => payload.type === 'voice_audio')
  )
  const voiceLogCalls = () => vi.mocked(console.info).mock.calls
  const expectVoiceLog = (event: string, details: Record<string, unknown> = {}) => {
    expect(voiceLogCalls()).toEqual(expect.arrayContaining([
      [`[gobby:voice] ${event}`, expect.objectContaining(details)],
    ]))
  }

  const submitPttAudio = async (result: { current: ReturnType<typeof useVoice> }) => {
    await act(async () => {
      await result.current.startRecording()
    })

    act(() => {
      lastWorkletNode?.port.onmessage?.(workletMessage(new Float32Array(16_000).fill(0.25)))
    })

    await act(async () => {
      await result.current.stopRecording()
    })
  }

  it('preserves voice loading before a configured TTS provider is available', () => {
    const parsed = parseVoiceStatus({
      enabled: true,
      stt_enabled: false,
      tts_enabled: true,
      stt_available: false,
      tts_available: false,
      voice_ready: false,
      voice_loading: true,
    }, true)

    expect(parsed.ttsConfigEnabled).toBe(true)
    expect(parsed.ttsAvailable).toBe(false)
    expect(parsed.voiceAvailable).toBe(true)
    expect(parsed.voiceLoading).toBe(true)
  })

  it('throttles same-target voice_prepare sends across rerender and remount', async () => {
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

    const renderStatus = () => renderHook(() => useVoiceStatus({
      wsRef: wsRef as any,
      conversationId: 'conv-throttle',
      socketConnected: true,
      sttEnabled: true,
      ttsEnabled: true,
    }))

    const first = renderStatus()

    await waitFor(() => {
      expect(voicePreparePayloads()).toHaveLength(1)
    })

    first.rerender()
    expect(voicePreparePayloads()).toHaveLength(1)

    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>
    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
    const fetchCallsBeforeRemount = fetchMock.mock.calls.length

    first.unmount()
    renderStatus()

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(fetchCallsBeforeRemount)
    })
    expect(voicePreparePayloads()).toHaveLength(1)
  })

  it('prunes warmup cache by oldest send timestamp', async () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(20_000)
    seedVoicePrepareCacheForTests([
      ['conv-refreshed:true:true', 10_000],
      ...Array.from({ length: 127 }, (_, index) => [
        `conv-filler-${index}:true:true`,
        index,
      ] as const),
    ])
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

    renderHook(() => useVoiceStatus({
      wsRef: wsRef as any,
      conversationId: 'conv-extra',
      socketConnected: true,
      sttEnabled: true,
      ttsEnabled: true,
    }))

    await waitFor(() => {
      expect(voicePreparePayloads()).toHaveLength(1)
    })

    const cacheKeys = voicePrepareCacheKeysForTests()
    expect(cacheKeys).toContain('conv-refreshed:true:true')
    expect(cacheKeys).toContain('conv-extra:true:true')
    expect(cacheKeys).not.toContain('conv-filler-0:true:true')
    expect(cacheKeys).toHaveLength(128)
    nowSpy.mockRestore()
  })

  it('sends voice_prepare immediately when requested voice targets change', async () => {
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
      ({ ttsEnabled }) => useVoiceStatus({
        wsRef: wsRef as any,
        conversationId: 'conv-target-change',
        socketConnected: true,
        sttEnabled: true,
        ttsEnabled,
      }),
      { initialProps: { ttsEnabled: false } },
    )

    await waitFor(() => {
      expect(voicePreparePayloads()).toHaveLength(1)
    })

    rerender({ ttsEnabled: true })

    await waitFor(() => {
      expect(voicePreparePayloads()).toHaveLength(2)
    })
    expect(voicePreparePayloads()[1]).toEqual(expect.objectContaining({
      conversation_id: 'conv-target-change',
      stt_enabled: true,
      tts_enabled: true,
    }))
  })

  it('sends voice_prepare immediately when conversationId changes', async () => {
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
      ({ conversationId }) => useVoiceStatus({
        wsRef: wsRef as any,
        conversationId,
        socketConnected: true,
        sttEnabled: true,
        ttsEnabled: true,
      }),
      { initialProps: { conversationId: 'conv-first' } },
    )

    await waitFor(() => {
      expect(voicePreparePayloads()).toHaveLength(1)
    })

    rerender({ conversationId: 'conv-second' })

    await waitFor(() => {
      expect(voicePreparePayloads()).toHaveLength(2)
    })
    expect(voicePreparePayloads()[1]).toEqual(expect.objectContaining({
      conversation_id: 'conv-second',
      stt_enabled: true,
      tts_enabled: true,
    }))
  })

  it('does not mark preparing messages without voice_loading as locally loading', async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        enabled: false,
        stt_enabled: false,
        tts_enabled: false,
        stt_available: false,
        tts_available: false,
        voice_ready: false,
        voice_loading: false,
      }),
    })) as any

    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-preparing-no-loading',
      0,
      projectIdRef,
      { sttEnabled: false, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled()
    })
    expect(result.current.voiceLoading).toBe(false)

    act(() => {
      result.current.handleVoiceMessage({
        type: 'voice_status',
        status: 'preparing',
      })
    })

    expect(result.current.voiceLoading).toBe(false)
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
        projectIdRef,
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
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/voice/status?want_stt=true&want_tts=true')
  })

  it('scopes mic-only warmup and status polling to STT', async () => {
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

    renderHook(() => useVoice(
      wsRef as any,
      'conv-stt-only',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await waitFor(() => {
      const payloads = wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))
      expect(payloads).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: 'voice_prepare',
          conversation_id: 'conv-stt-only',
          stt_enabled: true,
          tts_enabled: false,
        }),
      ]))
    })
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/voice/status?want_stt=true')
  })

  it('omits disabled voice targets from status polling', async () => {
    renderHook(() => useVoice(
      wsRef as any,
      'conv-no-voice',
      0,
      projectIdRef,
      { sttEnabled: false, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith('/api/voice/status')
    })
  })

  it('records PTT audio, normalizes to 16 kHz, and logs the send lifecycle', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-ptt',
      0,
      projectIdRef,
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

    const [[encodedAudio]] = voiceMocks.mockEncodeWAV.mock.calls as unknown as [[Float32Array]]
    expect(encodedAudio.length).toBe(Math.round(16_000 / 3))
    expect(voiceMocks.mockEncodeWAV).toHaveBeenCalledWith(expect.any(Float32Array), 1, 16_000, 1, 16)

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
    const voiceAudio = payloads?.find((payload) => payload.type === 'voice_audio')
    expect(voiceAudio).not.toHaveProperty('project_id')
    expectVoiceLog('ptt_start')
    expectVoiceLog('ptt_started', { sampleRate: 48_000 })
    expectVoiceLog('ptt_stop', {
      inputSampleRate: 48_000,
      outputSampleRate: 16_000,
      resampled: true,
    })
    expectVoiceLog('send_attempt', {
      conversationPrefix: 'conv-ptt',
      sampleRate: 16_000,
    })
    expectVoiceLog('send_ok', {
      conversationPrefix: 'conv-ptt',
      sampleRate: 16_000,
    })
  })

  it('uses the supplied attached session id for PTT voice audio payloads', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'term-attached',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)

    expect(voiceAudioPayloads()).toEqual([
      expect.objectContaining({
        type: 'voice_audio',
        conversation_id: 'term-attached',
      }),
    ])
  })

  it('includes the selected project in PTT voice audio payloads', async () => {
    projectIdRef.current = 'project-ptt'
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-ptt-project',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await act(async () => {
      await result.current.startRecording()
    })

    act(() => {
      lastWorkletNode?.port.onmessage?.(workletMessage(new Float32Array(16_000).fill(0.25)))
    })

    await act(async () => {
      await result.current.stopRecording()
    })

    const payloads = wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'voice_audio',
        conversation_id: 'conv-ptt-project',
        project_id: 'project-ptt',
      }),
    ]))
  })

  it('clears PTT transcribing state when transcription succeeds', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-transcribe-success',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)

    const voiceAudio = voiceAudioPayloads()[0]
    expect(voiceAudio.request_id).toEqual(expect.any(String))
    expect(result.current.isTranscribing).toBe(true)

    act(() => {
      result.current.handleVoiceMessage({
        type: 'voice_transcription',
        conversation_id: 'conv-transcribe-success',
        request_id: voiceAudio.request_id,
        text: 'hello',
      })
    })

    expect(result.current.isTranscribing).toBe(false)
  })

  it.each([
    ['empty', undefined],
    ['error', 'STT failed'],
  ])('clears PTT transcribing state on voice_status:%s', async (status, error) => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      `conv-transcribe-${status}`,
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)

    const voiceAudio = voiceAudioPayloads()[0]
    expect(result.current.isTranscribing).toBe(true)

    act(() => {
      result.current.handleVoiceMessage({
        type: 'voice_status',
        conversation_id: `conv-transcribe-${status}`,
        request_id: voiceAudio.request_id,
        status,
        error,
      })
    })

    expect(result.current.isTranscribing).toBe(false)
  })

  it('does not clear a newer transcription from a stale terminal event', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-transcribe-stale',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)
    const firstRequestId = voiceAudioPayloads()[0].request_id

    await submitPttAudio(result)
    const secondRequestId = voiceAudioPayloads()[1].request_id
    expect(secondRequestId).not.toBe(firstRequestId)
    expect(result.current.isTranscribing).toBe(true)

    act(() => {
      result.current.handleVoiceMessage({
        type: 'voice_status',
        request_id: firstRequestId,
        status: 'error',
        error: 'old failure',
      })
    })

    expect(result.current.isTranscribing).toBe(true)

    act(() => {
      result.current.handleVoiceMessage({
        type: 'voice_transcription',
        request_id: secondRequestId,
        text: 'fresh result',
      })
    })

    expect(result.current.isTranscribing).toBe(false)
  })

  it('does not leave PTT transcribing state stuck for local send failures', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const missingConversation = renderHook(() => useVoice(
      wsRef as any,
      '',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(missingConversation.result)
    expect(missingConversation.result.current.isTranscribing).toBe(false)
    expect(voiceAudioPayloads()).toHaveLength(0)
    expectVoiceLog('send_failed', { reason: 'missing_conversation' })

    missingConversation.unmount()
    wsRef.current!.readyState = 3

    const closedSocket = renderHook(() => useVoice(
      wsRef as any,
      'conv-closed-socket',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(closedSocket.result)
    expect(closedSocket.result.current.isTranscribing).toBe(false)
    expect(voiceAudioPayloads()).toHaveLength(0)
    expect(warnSpy).toHaveBeenCalledWith('Voice: WebSocket not open, discarding audio')
    expectVoiceLog('send_failed', { reason: 'websocket_not_open' })
  })

  it('resets PTT transcribing state when WebSocket send throws', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    wsRef.current!.send.mockImplementation((raw: string) => {
      const payload = JSON.parse(raw)
      if (payload.type === 'voice_audio') {
        throw new Error('send failed')
      }
    })

    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-send-throws',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)

    expect(result.current.isTranscribing).toBe(false)
    await waitFor(() => {
      expect(result.current.voiceError).toBe('Failed to send audio')
    })
    expect(errorSpy).toHaveBeenCalledWith('Voice: Failed to send audio:', expect.any(Error))
    expectVoiceLog('send_failed', { reason: 'websocket_send_error' })
  })

  it('clears stale PTT transcribing state after the watchdog timeout', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-transcribe-watchdog',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)
    expect(result.current.isTranscribing).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(VOICE_TRANSCRIPTION_WATCHDOG_MS - 1)
    })
    expect(result.current.isTranscribing).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(result.current.isTranscribing).toBe(false)
  })

  it('discards short PTT captures without sending audio', async () => {
    const nowSpy = vi.spyOn(performance, 'now')
    nowSpy.mockReturnValueOnce(0).mockReturnValueOnce(100)

    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-short',
      0,
      projectIdRef,
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
    expectVoiceLog('ptt_stop', { reason: 'too_short' })
  })

  it('surfaces empty PTT captures without sending audio', async () => {
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-empty',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await act(async () => {
      await result.current.startRecording()
    })

    await act(async () => {
      await result.current.stopRecording()
    })

    await waitFor(() => {
      expect(result.current.voiceError).toBe('No audio captured — try again')
    })
    expect(voiceAudioPayloads()).toHaveLength(0)
    expectVoiceLog('ptt_stop', { inputSamples: 0 })
    expectVoiceLog('send_failed', { reason: 'empty_audio' })
  })

  it('blocks oversized PTT payloads before WebSocket send', async () => {
    voiceMocks.mockArrayBufferToBase64.mockReturnValueOnce(
      'a'.repeat(VOICE_AUDIO_MAX_PAYLOAD_BYTES),
    )
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-oversized',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'ptt' },
      true,
    ))

    await submitPttAudio(result)

    await waitFor(() => {
      expect(result.current.voiceError).toBe('Audio clip too long — try a shorter recording')
    })
    expect(voiceAudioPayloads()).toHaveLength(0)
    expectVoiceLog('send_attempt', { conversationPrefix: 'conv-ove' })
    expectVoiceLog('send_failed', { reason: 'payload_too_large' })
  })

  it('keeps VAD auto-submit and barge-in behavior', async () => {
    projectIdRef.current = 'project-vad'
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-vad',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'vad' },
      true,
    ))

    await waitFor(() => {
      expect(voiceMocks.mockMicVADNew).toHaveBeenCalledTimes(1)
      expect(result.current.isListening).toBe(true)
    })
    expect(lastVADConfig).toEqual(expect.objectContaining({
      positiveSpeechThreshold: 0.65,
      negativeSpeechThreshold: 0.45,
      minSpeechFrames: 6,
      redemptionFrames: 12,
      preSpeechPadFrames: 2,
      submitUserSpeechOnPause: false,
    }))
    expectVoiceLog('vad_start')
    expectVoiceLog('vad_ready')

    act(() => {
      lastVADConfig?.onSpeechStart?.()
      lastVADConfig?.onVADMisfire?.()
      lastVADConfig?.onSpeechStart?.()
      lastVADConfig?.onSpeechEnd?.(new Float32Array(8_000).fill(0.2))
    })

    expect(voiceMocks.mockEncodeWAV).toHaveBeenCalledWith(expect.any(Float32Array), 1, 16_000, 1, 16)
    const payloads = wsRef.current?.send.mock.calls.map(([raw]) => JSON.parse(raw))
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'tts_stop', conversation_id: 'conv-vad' }),
      expect.objectContaining({
        type: 'voice_audio',
        conversation_id: 'conv-vad',
        project_id: 'project-vad',
      }),
    ]))
    expectVoiceLog('vad_speech_start')
    expectVoiceLog('vad_misfire')
    expectVoiceLog('vad_speech_end', {
      audioSamples: 8_000,
      sampleRate: 16_000,
    })
    expectVoiceLog('send_ok', {
      conversationPrefix: 'conv-vad',
      sampleRate: 16_000,
    })
  })

  it('creates a chat session before VAD auto-submit from a fresh chat', async () => {
    const ensureConversationId = vi.fn(async () => 'fresh-session-id')
    renderHook(() => useVoice(
      wsRef as any,
      '',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'vad' },
      true,
      ensureConversationId,
    ))

    await waitFor(() => {
      expect(voiceMocks.mockMicVADNew).toHaveBeenCalledTimes(1)
    })

    act(() => {
      lastVADConfig?.onSpeechEnd?.(new Float32Array(8_000).fill(0.2))
    })

    await waitFor(() => {
      expect(ensureConversationId).toHaveBeenCalledTimes(1)
      expect(voiceAudioPayloads()).toEqual([
        expect.objectContaining({
          type: 'voice_audio',
          conversation_id: 'fresh-session-id',
        }),
      ])
    })
    expectVoiceLog('send_ok', {
      conversationPrefix: 'fresh-se',
      sampleRate: 16_000,
    })
  })

  it('rejects low-energy VAD segments before STT submission', async () => {
    renderHook(() => useVoice(
      wsRef as any,
      'conv-vad-quiet',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'vad' },
      true,
    ))

    await waitFor(() => {
      expect(voiceMocks.mockMicVADNew).toHaveBeenCalledTimes(1)
    })

    act(() => {
      lastVADConfig?.onSpeechEnd?.(new Float32Array(8_000).fill(0.002))
    })

    expect(voiceAudioPayloads()).toHaveLength(0)
    expectVoiceLog('vad_reject', { reason: 'low_energy_or_too_short' })
  })

  it('logs VAD startup failures', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    voiceMocks.mockMicVADNew.mockRejectedValueOnce(new Error('VAD denied'))

    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-vad-error',
      0,
      projectIdRef,
      { sttEnabled: true, ttsEnabled: false, voiceInputMode: 'vad' },
      true,
    ))

    await waitFor(() => {
      expect(result.current.voiceError).toBe('VAD denied')
    })
    expect(errorSpy).toHaveBeenCalledWith('Failed to start VAD:', expect.any(Error))
    expect(result.current.isListening).toBe(false)
    expectVoiceLog('vad_error', { error: 'VAD denied' })
  })

  it('drops incoming TTS audio when the playback queue is full', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-tts',
      0,
      projectIdRef,
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

  it('waits for a suspended AudioContext before starting queued TTS chunks', async () => {
    mockAudioContextInitialState = 'suspended'
    const resume = deferAudioContextResume()
    const { result } = renderHook(() => useVoice(
      wsRef as any,
      'conv-tts',
      0,
      projectIdRef,
      { sttEnabled: false, ttsEnabled: true, voiceInputMode: 'ptt' },
      true,
    ))

    act(() => {
      result.current.prepareTTSPlayback()
      for (const marker of [1, 2]) {
        result.current.handleVoiceMessage({
          type: 'tts_audio',
          sample_rate: 24_000,
          format: 'pcm_s16le',
          chunk_index: marker,
        })
        result.current.handleBinaryMessage(pcmChunk(marker))
      }
    })

    expect(audioContextResumeCalls).toBe(1)
    expect(startedSources).toHaveLength(0)

    await act(async () => {
      resume.resolve()
      await resume.promise
      await Promise.resolve()
    })

    await waitFor(() => {
      expect(startedSources).toHaveLength(1)
    })
    expect(Math.round((startedSources[0].buffer?.getChannelData(0)[0] ?? 0) * 32768)).toBe(1)

    act(() => {
      startedSources[0].onended?.(new Event('ended'))
    })

    expect(startedSources).toHaveLength(2)
    const playedMarkers = startedSources.map((source) => {
      const sample = source.buffer?.getChannelData(0)[0] ?? 0
      return Math.round(sample * 32768)
    })
    expect(playedMarkers).toEqual([1, 2])
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
        projectIdRef,
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
