import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Settings } from '../Settings'
import { useVoiceCapabilities } from '../../hooks/useVoiceCapabilities'

vi.mock('../../hooks/useVoiceCapabilities', () => ({
  useVoiceCapabilities: vi.fn(),
}))

const baseSettings = {
  fontSize: 16,
  model: 'opus',
  chatMode: 'plan' as const,
  theme: 'dark' as const,
  defaultChatMode: 'plan' as const,
  sttEnabled: false,
  ttsEnabled: false,
  voiceInputMode: 'ptt' as const,
}

describe('Settings voice section', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('hides the voice section when voice is disabled in config', () => {
    vi.mocked(useVoiceCapabilities).mockReturnValue({
      sttConfigEnabled: false,
      ttsConfigEnabled: false,
      sttAvailable: false,
      ttsAvailable: false,
      loading: false,
    })

    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onModelChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onSttEnabledChange={vi.fn()}
        onTtsEnabledChange={vi.fn()}
        onVoiceInputModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByText('Voice')).toBeNull()
  })

  it('renders STT and TTS toggles independently based on config', () => {
    vi.mocked(useVoiceCapabilities).mockReturnValue({
      sttConfigEnabled: true,
      ttsConfigEnabled: true,
      sttAvailable: true,
      ttsAvailable: true,
      loading: false,
    })

    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onModelChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onSttEnabledChange={vi.fn()}
        onTtsEnabledChange={vi.fn()}
        onVoiceInputModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Enable speech to text')).toBeTruthy()
    expect(screen.getByLabelText('Enable text to speech')).toBeTruthy()
  })

  it('shows the input mode selector only after STT is enabled', async () => {
    vi.mocked(useVoiceCapabilities).mockReturnValue({
      sttConfigEnabled: true,
      ttsConfigEnabled: false,
      sttAvailable: true,
      ttsAvailable: false,
      loading: false,
    })

    const onVoiceInputModeChange = vi.fn()
    const { rerender } = render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onModelChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onSttEnabledChange={vi.fn()}
        onTtsEnabledChange={vi.fn()}
        onVoiceInputModeChange={onVoiceInputModeChange}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByText('Push to Talk')).toBeNull()

    rerender(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={{ ...baseSettings, sttEnabled: true, voiceInputMode: 'vad' }}
        onFontSizeChange={vi.fn()}
        onModelChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onSttEnabledChange={vi.fn()}
        onTtsEnabledChange={vi.fn()}
        onVoiceInputModeChange={onVoiceInputModeChange}
        onReset={vi.fn()}
      />,
    )

    expect(screen.getByText('Push to Talk')).toBeTruthy()
    await userEvent.click(screen.getByText('Push to Talk'))
    expect(onVoiceInputModeChange).toHaveBeenCalledWith('ptt')
  })

  it('disables controls when the server config allows voice but runtime support is unavailable', () => {
    vi.mocked(useVoiceCapabilities).mockReturnValue({
      sttConfigEnabled: true,
      ttsConfigEnabled: true,
      sttAvailable: false,
      ttsAvailable: false,
      loading: false,
    })

    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onModelChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onSttEnabledChange={vi.fn()}
        onTtsEnabledChange={vi.fn()}
        onVoiceInputModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Enable speech to text')).toBeDisabled()
    expect(screen.getByLabelText('Enable text to speech')).toBeDisabled()
    expect(screen.getByText('Requires secure context and server-ready STT')).toBeTruthy()
  })
})
