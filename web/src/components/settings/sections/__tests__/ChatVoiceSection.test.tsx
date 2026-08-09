import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ChatVoiceSection } from '../ChatVoiceSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../SettingsSectionContext'
import type { UseSettingsReturn } from '../../../../hooks/useSettings'

// Minimal schema covering the rows the assertions touch: chat.profile reaches
// the shared FeatureProfile enum through a $ref, and bounded numbers carry
// their schema min/max — mirroring the real DaemonConfig shape.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ['feature_low', 'feature_mid', 'feature_high'],
      type: 'string',
    },
    ChatConfig: {
      type: 'object',
      properties: {
        profile: { $ref: '#/$defs/FeatureProfile' },
        default_mode: { type: 'string' },
        candidates: { type: 'array', items: { type: 'string' } },
        attachment_max_files_per_message: {
          type: 'integer',
          minimum: 1,
          maximum: 100,
        },
      },
    },
    VoiceConfig: {
      type: 'object',
      properties: {
        enabled: { type: 'boolean' },
        tts_enabled: { type: 'boolean' },
        tts_provider: { type: 'string' },
        tts_temperature: { type: 'number', minimum: 0.1, maximum: 1.0 },
        whisper_model_size: { type: 'string' },
        whisper_vocabulary: { type: 'array', items: { type: 'string' } },
        openai_compatible_audio: { type: 'array', items: { type: 'object' } },
      },
    },
  },
  type: 'object',
  properties: {
    chat: { $ref: '#/$defs/ChatConfig' },
    voice: { $ref: '#/$defs/VoiceConfig' },
  },
}

function makeConfigValues(): Record<string, unknown> {
  return {
    chat: {
      profile: 'feature_high',
      default_mode: 'plan',
      candidates: ['claude/sonnet'],
      attachment_max_file_bytes: 100_000_000,
      attachment_max_total_bytes_per_message: 2_000_000_000,
      attachment_max_files_per_message: 20,
      attachment_unbound_retention_hours: 24,
      attachment_gc_interval_minutes: 60,
    },
    voice: {
      enabled: true,
      tts_enabled: true,
      tts_provider: 'chatterbox',
      tts_reference_audio: '~/.gobby/voice/reference.wav',
      tts_reference_text: null,
      tts_temperature: 0.55,
      tts_chatterbox_max_generation_tokens: 1000,
      tts_clause_max_chars: 180,
      tts_device: 'auto',
      stt_enabled: true,
      transcription_timeout_seconds: 120,
      whisper_model_size: 'base',
      whisper_device: 'auto',
      whisper_compute_type: 'int8',
      whisper_prompt: 'Gobby',
      whisper_vocabulary: ['Gobby', 'MCP'],
      openai_compatible_audio: [
        {
          provider: 'whisperx',
          url: 'http://localhost:9000/v1',
          model: 'large-v3',
          transcription_enabled: true,
          translation_enabled: false,
          timeout_seconds: 120,
        },
      ],
    },
  }
}

function makeClientSettings(): UseSettingsReturn {
  return {
    settings: {
      sttEnabled: true,
      ttsEnabled: false,
      voiceInputMode: 'ptt',
      defaultChatMode: 'normal',
      model: 'opus',
    },
    updateSttEnabled: vi.fn(),
    updateTtsEnabled: vi.fn(),
    updateVoiceInputMode: vi.fn(),
    updateDefaultChatMode: vi.fn(),
  } as unknown as UseSettingsReturn
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    clientSettings: makeClientSettings(),
    ...overrides,
  }
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <ChatVoiceSection />
    </SettingsSectionContext.Provider>,
  )
}

describe('ChatVoiceSection', () => {
  it('wires client voice toggles and input mode to the shared useSettings instance', () => {
    const ctx = makeContext()
    renderSection(ctx)

    const stt = screen.getByRole('switch', { name: 'Speech-to-text enabled' })
    expect(stt).toBeChecked()
    fireEvent.click(stt)
    expect(ctx.clientSettings?.updateSttEnabled).toHaveBeenCalledWith(false)

    expect(
      screen.getByRole('switch', { name: 'Text-to-speech enabled' }),
    ).not.toBeChecked()

    const mode = screen.getByLabelText('Voice input mode') as HTMLSelectElement
    expect(mode).toHaveValue('ptt')
    expect(within(mode).getAllByRole('option')).toHaveLength(2)
    fireEvent.change(mode, { target: { value: 'vad' } })
    expect(ctx.clientSettings?.updateVoiceInputMode).toHaveBeenCalledWith('vad')
  })

  it('ports the legacy default-mode control to the client chat-mode select', () => {
    const ctx = makeContext()
    renderSection(ctx)

    const select = screen.getByRole('combobox', {
      name: 'Default chat mode',
    }) as HTMLSelectElement
    expect(select).toHaveValue('normal')
    expect(within(select).getAllByRole('option')).toHaveLength(3)
    fireEvent.change(select, { target: { value: 'bypass' } })
    expect(ctx.clientSettings?.updateDefaultChatMode).toHaveBeenCalledWith('bypass')
  })

  it('reads chat config rows from nested config by dotted path', () => {
    renderSection(makeContext())

    // Schema-enum profile select proves pickPaths nested traversal.
    const profile = screen.getByLabelText('Chat capability profile')
    expect(profile).toHaveValue('feature_high')
    expect(within(profile).getAllByRole('option')).toHaveLength(3)

    // Daemon default_mode is bounded by an explicit option list (no schema enum).
    const daemonMode = screen.getByLabelText('Daemon default chat mode')
    expect(daemonMode).toHaveValue('plan')
    expect(within(daemonMode).getAllByRole('option')).toHaveLength(4)

    expect(
      screen.getByLabelText('Maximum attachments per message'),
    ).toHaveValue(20)
  })

  it('reads voice config rows: bounded selects, numbers, and the vocabulary list', () => {
    renderSection(makeContext())

    expect(screen.getByRole('switch', { name: 'Voice features enabled' })).toBeChecked()

    const whisper = screen.getByLabelText('Whisper model size')
    expect(whisper).toHaveValue('base')
    expect(within(whisper).getAllByRole('option')).toHaveLength(4)

    expect(screen.getByLabelText('TTS provider')).toHaveValue('chatterbox')
    expect(screen.getByLabelText('TTS temperature')).toHaveValue(0.55)
    expect(screen.getByLabelText('Whisper vocabulary item 1')).toHaveValue('Gobby')
  })

  it('renders OpenAI-compatible audio bindings as structured editors', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Audio API URL (whisperx)')).toHaveValue(
      'http://localhost:9000/v1',
    )
    expect(screen.getByLabelText('Audio model (whisperx)')).toHaveValue('large-v3')
    expect(
      screen.getByRole('switch', { name: 'Transcription enabled (whisperx)' }),
    ).toBeChecked()
  })

  it('persists an edited config row through the section draft Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('Daemon default chat mode'), {
      target: { value: 'bypass' },
    })

    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ 'chat.default_mode': 'bypass' }),
    )
  })

  it('degrades gracefully when client settings are absent', () => {
    renderSection(makeContext({ clientSettings: undefined }))

    expect(screen.queryByRole('switch', { name: 'Speech-to-text enabled' })).toBeNull()
    expect(
      screen.getByText(/Voice and chat preferences are unavailable/i),
    ).toBeInTheDocument()
    // Config-backed controls still render without the client surface.
    expect(screen.getByLabelText('Chat capability profile')).toHaveValue('feature_high')
  })
})
