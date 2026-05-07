import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { ChatInput } from '../ChatInput'

vi.mock('../ModeSelector', () => ({
  ModeSelector: ({ mode, disabled }: { mode: string; disabled?: boolean }) => (
    <div data-testid="mode-selector" data-disabled={String(Boolean(disabled))}>
      {mode}
    </div>
  ),
}))
vi.mock('../ContextUsageIndicator', () => ({
  ContextUsageIndicator: () => <div data-testid="context-usage" />,
}))
vi.mock('../BranchIndicator', () => ({
  BranchIndicator: ({ disabled }: { disabled?: boolean }) => (
    <div data-testid="branch-indicator" data-disabled={String(Boolean(disabled))} />
  ),
}))
vi.mock('../ActiveAgentIndicator', () => ({
  ActiveAgentIndicator: ({ disabled }: { disabled?: boolean }) => (
    <div data-testid="agent-indicator" data-disabled={String(Boolean(disabled))} />
  ),
}))
vi.mock('./ui/Button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}))

function installPointerHelpers(button: HTMLButtonElement) {
  Object.assign(button, {
    setPointerCapture: vi.fn(),
    releasePointerCapture: vi.fn(),
    hasPointerCapture: vi.fn(() => true),
    getBoundingClientRect: () => ({
      left: 0,
      right: 36,
      top: 0,
      bottom: 36,
      width: 36,
      height: 36,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }),
  })
}

function PTTHarness({
  onSend = vi.fn(),
  onStopRecording = vi.fn(),
  onCancelRecording = vi.fn(),
}: {
  onSend?: (message: string, files?: unknown, options?: { reasoningEffort?: string | null }) => void
  onStopRecording?: () => void
  onCancelRecording?: () => void
}) {
  const [isRecording, setIsRecording] = useState(false)

  return (
    <ChatInput
      onSend={onSend}
      sttEnabled={true}
      voiceInputMode="ptt"
      isRecording={isRecording}
      startRecording={async () => setIsRecording(true)}
      stopRecording={async () => {
        onStopRecording()
        setIsRecording(false)
      }}
      cancelRecording={() => {
        onCancelRecording()
        setIsRecording(false)
      }}
    />
  )
}

function SttToggleHarness({
  callOrder,
}: {
  callOrder: string[]
}) {
  const [sttEnabled, setSttEnabled] = useState(false)

  return (
    <ChatInput
      onSend={vi.fn()}
      sttEnabled={sttEnabled}
      onSttEnabledChange={(enabled) => {
        callOrder.push(`toggle:${String(enabled)}`)
        setSttEnabled(enabled)
      }}
      startRecording={async () => {
        callOrder.push(`start:${String(sttEnabled)}`)
      }}
      stopRecording={vi.fn(async () => {})}
      cancelRecording={vi.fn()}
    />
  )
}

function DeferredSttEnableHarness({
  startRecording,
}: {
  startRecording: () => Promise<void>
}) {
  const [sttEnabled, setSttEnabled] = useState(false)

  return (
    <>
      <button type="button" onClick={() => setSttEnabled(true)}>
        Enable externally
      </button>
      <ChatInput
        onSend={vi.fn()}
        sttEnabled={sttEnabled}
        onSttEnabledChange={vi.fn()}
        startRecording={startRecording}
        stopRecording={vi.fn(async () => {})}
        cancelRecording={vi.fn()}
      />
    </>
  )
}

describe('ChatInput', () => {
  const defaultProps = {
    onSend: vi.fn(),
    onStop: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders textarea with placeholder', () => {
    render(<ChatInput {...defaultProps} />)

    const textarea = screen.getByRole('textbox')
    expect(textarea).toBeTruthy()
    expect(textarea).toHaveAttribute('aria-label', 'Message input')
  })

  it('shows an unavailable placeholder when disabled without state-specific copy', () => {
    render(<ChatInput {...defaultProps} disabled={true} />)

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveAttribute('placeholder', 'Message input unavailable...')
    expect(textarea).toHaveAttribute('aria-label', 'Message input — unavailable')
  })

  it('defaults to read-only copy when disabled while viewing a session', () => {
    render(<ChatInput {...defaultProps} disabled={true} viewingSession={true} />)

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveAttribute('placeholder', 'Read-only while watching this session...')
    expect(textarea).toHaveAttribute('aria-label', 'Message input — watching read only')
  })

  it('uses the provided disabled placeholder and aria label', () => {
    render(
      <ChatInput
        {...defaultProps}
        disabled={true}
        disabledPlaceholder="Resuming session in web chat..."
        disabledAriaLabel="Message input — resuming session"
      />,
    )

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveAttribute('placeholder', 'Resuming session in web chat...')
    expect(textarea).toHaveAttribute('aria-label', 'Message input — resuming session')
  })

  it('shows streaming placeholder when streaming', () => {
    render(<ChatInput {...defaultProps} isStreaming={true} />)

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveAttribute('aria-label', 'Message input — streaming')
  })

  it('renders the proxy delivery notice above the toolbar row', () => {
    const { container } = render(
      <ChatInput
        {...defaultProps}
        onModeChange={vi.fn()}
        proxyDeliveryNotice="Message queued until the session yields."
      />,
    )

    const notice = screen.getByText('Message queued until the session yields.')
    const toolbar = container.querySelector('.chat-input-toolbar')

    expect(notice).toBeTruthy()
    expect(toolbar).toBeTruthy()
    expect(container.querySelector('.chat-input-notice-slot')?.contains(notice)).toBe(true)
    expect(toolbar?.previousElementSibling).toContainElement(notice)
  })

  it('prepares browser TTS playback before enabling text-to-speech', async () => {
    const prepareTTSPlayback = vi.fn()
    const onTtsEnabledChange = vi.fn()

    render(
      <ChatInput
        {...defaultProps}
        ttsEnabled={false}
        prepareTTSPlayback={prepareTTSPlayback}
        onTtsEnabledChange={onTtsEnabledChange}
      />,
    )

    await userEvent.click(screen.getByLabelText('Toggle text-to-speech'))

    expect(prepareTTSPlayback).toHaveBeenCalledTimes(1)
    expect(onTtsEnabledChange).toHaveBeenCalledWith(true)
  })

  it('pulses the TTS toggle while voice is warming', () => {
    render(
      <ChatInput
        {...defaultProps}
        ttsEnabled={true}
        voiceLoading={true}
        voiceReady={false}
        onTtsEnabledChange={vi.fn()}
      />,
    )

    const button = screen.getByLabelText('Text-to-speech warming up')
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toHaveClass('chat-input-voice-toggle--warming')
  })

  it('calls onSend when Enter is pressed', async () => {
    const onSend = vi.fn()
    render(<ChatInput {...defaultProps} onSend={onSend} />)

    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, 'Hello world')
    await userEvent.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('Hello world', undefined, {
      reasoningEffort: 'auto',
      ttsEnabled: false,
    })
  })

  it('prepares playback before sending with enabled TTS intent', async () => {
    const callOrder: string[] = []
    const prepareTTSPlayback = vi.fn(() => {
      callOrder.push('prepare')
    })
    const onSend = vi.fn(() => {
      callOrder.push('send')
    })
    render(
      <ChatInput
        {...defaultProps}
        onSend={onSend}
        ttsEnabled={true}
        prepareTTSPlayback={prepareTTSPlayback}
      />,
    )

    await userEvent.type(screen.getByRole('textbox'), 'Speak this')
    await userEvent.keyboard('{Enter}')

    expect(callOrder).toEqual(['prepare', 'send'])
    expect(prepareTTSPlayback).toHaveBeenCalledTimes(1)
    expect(onSend).toHaveBeenCalledWith('Speak this', undefined, {
      reasoningEffort: 'auto',
      ttsEnabled: true,
    })
  })

  it('does not send empty messages', async () => {
    const onSend = vi.fn()
    render(<ChatInput {...defaultProps} onSend={onSend} />)

    await userEvent.keyboard('{Enter}')

    expect(onSend).not.toHaveBeenCalled()
  })

  it('allows Shift+Enter for newline (desktop)', async () => {
    const onSend = vi.fn()
    render(<ChatInput {...defaultProps} onSend={onSend} />)

    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, 'Hello')
    await userEvent.keyboard('{Shift>}{Enter}{/Shift}')

    expect(onSend).not.toHaveBeenCalled()
  })

  it('Escape stops streaming when streaming', async () => {
    const onStop = vi.fn()
    render(<ChatInput {...defaultProps} onStop={onStop} isStreaming={true} />)

    const textarea = screen.getByRole('textbox')
    fireEvent.keyDown(textarea, { key: 'Escape' })

    expect(onStop).toHaveBeenCalled()
  })

  it('renders mode selector when onModeChange provided', () => {
    const { container } = render(
      <ChatInput {...defaultProps} onModeChange={vi.fn()} mode="accept_edits" />,
    )

    expect(screen.getByTestId('mode-selector')).toBeTruthy()
    expect(screen.getByText('accept_edits')).toBeTruthy()

    // At default (non-compact) widths, ModeSelector lives in toolbar__left as
    // the first child. The chat-input-mode-row only renders at <=360px.
    const toolbarLeft = container.querySelector('.chat-input-toolbar__left')
    expect(toolbarLeft?.firstElementChild).toBe(screen.getByTestId('mode-selector'))
    expect(container.querySelector('.chat-input-mode-row')).toBeNull()
  })

  it('disables proxy-owned footer controls while leaving text entry enabled', () => {
    render(
      <ChatInput
        {...defaultProps}
        onModeChange={vi.fn()}
        mode="plan"
        modeDisabled={true}
        attachmentsDisabled={true}
        onAgentChange={vi.fn()}
        agentName="default"
        agentDefinitions={[{ name: 'default', source: 'project' } as any]}
        onWorktreeChange={vi.fn()}
        worktreePickerDisabled={true}
        currentBranch="main"
        agentPickerDisabled={true}
      />,
    )

    expect(screen.getByRole('textbox')).not.toBeDisabled()
    expect(screen.getByTestId('mode-selector')).toHaveAttribute('data-disabled', 'true')
    expect(screen.getByTestId('agent-indicator')).toHaveAttribute('data-disabled', 'true')
    expect(screen.getAllByTestId('branch-indicator')[0]).toHaveAttribute('data-disabled', 'true')
    expect(screen.getByTitle('Attached session owns attachments')).toBeDisabled()
  })

  it('shows a mic button in PTT mode with empty input', () => {
    render(
      <ChatInput
        {...defaultProps}
        sttEnabled={true}
        voiceInputMode="ptt"
        startRecording={vi.fn(async () => {})}
        stopRecording={vi.fn(async () => {})}
        cancelRecording={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Start push to talk')).toBeTruthy()
  })

  it('shows Stop as the only primary action while streaming', () => {
    render(
      <ChatInput
        {...defaultProps}
        isStreaming={true}
        sttEnabled={true}
        voiceInputMode="ptt"
        startRecording={vi.fn(async () => {})}
        stopRecording={vi.fn(async () => {})}
        cancelRecording={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Stop generating')).toBeTruthy()
    expect(screen.queryByLabelText('Send message')).toBeNull()
  })

  it('shows Send instead of Mic when there is text input', async () => {
    render(
      <ChatInput
        {...defaultProps}
        sttEnabled={true}
        voiceInputMode="ptt"
        startRecording={vi.fn(async () => {})}
        stopRecording={vi.fn(async () => {})}
        cancelRecording={vi.fn()}
      />,
    )

    await userEvent.type(screen.getByRole('textbox'), 'hello')

    expect(screen.getByLabelText('Send message')).toBeTruthy()
    expect(screen.queryByLabelText('Start push to talk')).toBeNull()
  })

  it('short tap latches recording and second tap stops it', () => {
    vi.useFakeTimers()
    const onStopRecording = vi.fn()
    render(<PTTHarness onStopRecording={onStopRecording} />)

    const button = screen.getByLabelText('Start push to talk') as HTMLButtonElement
    installPointerHelpers(button)

    fireEvent.pointerDown(button, { pointerId: 1, button: 0 })
    fireEvent.pointerUp(button, { pointerId: 1 })

    const recordingButton = screen.getByLabelText('Push to talk recording') as HTMLButtonElement
    installPointerHelpers(recordingButton)

    fireEvent.pointerDown(recordingButton, { pointerId: 2, button: 0 })
    fireEvent.pointerUp(recordingButton, { pointerId: 2 })

    expect(onStopRecording).toHaveBeenCalledTimes(1)
  })

  it('long press stops and sends on release', () => {
    vi.useFakeTimers()
    const onStopRecording = vi.fn()
    render(<PTTHarness onStopRecording={onStopRecording} />)

    const button = screen.getByLabelText('Start push to talk') as HTMLButtonElement
    installPointerHelpers(button)

    fireEvent.pointerDown(button, { pointerId: 1, button: 0 })
    vi.advanceTimersByTime(300)
    fireEvent.pointerUp(button, { pointerId: 1 })

    expect(onStopRecording).toHaveBeenCalledTimes(1)
  })

  it('dragging off during a held recording cancels it', () => {
    vi.useFakeTimers()
    const onCancelRecording = vi.fn()
    render(<PTTHarness onCancelRecording={onCancelRecording} />)

    const button = screen.getByLabelText('Start push to talk') as HTMLButtonElement
    installPointerHelpers(button)

    fireEvent.pointerDown(button, { pointerId: 1, button: 0 })
    vi.advanceTimersByTime(300)
    fireEvent.pointerMove(button, { pointerId: 1, clientX: 100, clientY: 100 })

    expect(onCancelRecording).toHaveBeenCalledTimes(1)
  })

  it('Escape cancels an in-flight recording', () => {
    const onCancelRecording = vi.fn()
    render(
      <ChatInput
        {...defaultProps}
        sttEnabled={true}
        voiceInputMode="ptt"
        isRecording={true}
        startRecording={vi.fn(async () => {})}
        stopRecording={vi.fn(async () => {})}
        cancelRecording={onCancelRecording}
      />,
    )

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(onCancelRecording).toHaveBeenCalledTimes(1)
  })

  it('waits for sttEnabled state to flip before starting recording from the toolbar toggle', async () => {
    const callOrder: string[] = []
    render(<SttToggleHarness callOrder={callOrder} />)

    await userEvent.click(screen.getByLabelText('Toggle microphone'))

    await waitFor(() => {
      expect(callOrder).toEqual(['toggle:true', 'start:true'])
    })
  })

  it('disables STT when the mic toggle is clicked while STT is on but idle', async () => {
    const onSttEnabledChange = vi.fn()
    render(
      <ChatInput
        {...defaultProps}
        sttEnabled={true}
        isRecording={false}
        startRecording={vi.fn(async () => {})}
        stopRecording={vi.fn(async () => {})}
        cancelRecording={vi.fn()}
        onSttEnabledChange={onSttEnabledChange}
      />,
    )

    await userEvent.click(screen.getByLabelText('Toggle microphone'))

    expect(onSttEnabledChange).toHaveBeenCalledWith(false)
  })

  it('clears the pending STT start when STT remains disabled', async () => {
    const startRecording = vi.fn(async () => {})
    render(<DeferredSttEnableHarness startRecording={startRecording} />)

    await userEvent.click(screen.getByLabelText('Toggle microphone'))
    await act(async () => {
      await Promise.resolve()
    })

    expect(startRecording).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Enable externally' }))

    await waitFor(() => {
      expect(startRecording).not.toHaveBeenCalled()
    })
  })

  it('stops recording before disabling STT when the mic is clicked mid-recording', async () => {
    const callOrder: string[] = []
    const stopRecording = vi.fn(async () => {
      callOrder.push('stop')
    })
    const onSttEnabledChange = vi.fn((enabled: boolean) => {
      callOrder.push(`toggle:${String(enabled)}`)
    })
    render(
      <ChatInput
        {...defaultProps}
        sttEnabled={true}
        isRecording={true}
        startRecording={vi.fn(async () => {})}
        stopRecording={stopRecording}
        cancelRecording={vi.fn()}
        onSttEnabledChange={onSttEnabledChange}
      />,
    )

    await userEvent.click(screen.getByLabelText('Toggle microphone'))

    await waitFor(() => {
      expect(callOrder).toEqual(['stop', 'toggle:false'])
    })
  })

  it('clears input after sending', async () => {
    const onSend = vi.fn()
    render(<ChatInput {...defaultProps} onSend={onSend} />)

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    await userEvent.type(textarea, 'Hello')
    await userEvent.keyboard('{Enter}')

    expect(textarea.value).toBe('')
  })

  it('shows command palette when input starts with /', async () => {
    const items = [
      { kind: 'command' as const, name: 'help', description: 'Show help', action: 'help' },
      { kind: 'command' as const, name: 'clear', description: 'Clear chat', action: 'clear' },
    ]

    render(<ChatInput {...defaultProps} paletteItems={items} />)

    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, '/')

    expect(screen.getByText('/help')).toBeTruthy()
    expect(screen.getByText('/clear')).toBeTruthy()
  })

  it('on mobile, Shift+Enter sends', async () => {
    const onSend = vi.fn()
    render(<ChatInput {...defaultProps} onSend={onSend} isMobile={true} />)

    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, 'Hello')
    await userEvent.keyboard('{Shift>}{Enter}{/Shift}')

    expect(onSend).toHaveBeenCalledWith('Hello', undefined, {
      reasoningEffort: 'auto',
      ttsEnabled: false,
    })
  })

  it('shows model selection in the toolbar for a single-provider setup', () => {
    render(
      <ChatInput
        {...defaultProps}
        provider="claude"
        availableProviders={['claude']}
        currentModel="local"
        onModelChange={vi.fn()}
        onSwitchProvider={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('Select model')).toBeTruthy()
  })

  it('keeps the collapsed provider trigger icon-only', () => {
    render(
      <ChatInput
        {...defaultProps}
        provider="openai"
        availableProviders={['openai']}
        currentModel="local"
        onModelChange={vi.fn()}
        onSwitchProvider={vi.fn()}
      />,
    )

    expect(screen.queryByText('OpenAI')).toBeNull()
    expect(screen.getByLabelText('Select provider')).toHaveAttribute('title', 'OpenAI')
    expect(screen.getByText('Local')).toBeTruthy()
    // Reasoning dropdown is hidden when no reasoning levels are supported
    // (only the disabled Auto option) — see ChatInputModelControls.
    expect(screen.queryByLabelText('Select reasoning effort')).toBeNull()
  })

  it('forwards non-local slash commands in proxy mode', async () => {
    const onSend = vi.fn()
    const onPaletteSelect = vi.fn()
    render(
      <ChatInput
        {...defaultProps}
        onSend={onSend}
        onPaletteSelect={onPaletteSelect}
        proxySlashMode={true}
        paletteItems={[
          { kind: 'command' as const, name: 'settings', description: 'Settings', action: 'open_settings' },
          { kind: 'command' as const, name: 'clear', description: 'Clear', action: 'clear_history' },
        ]}
      />,
    )

    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, '/plan')
    await userEvent.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('/plan', undefined, {
      reasoningEffort: 'auto',
      ttsEnabled: false,
    })
    expect(onPaletteSelect).not.toHaveBeenCalled()
  })

  it('renders the observe overlay and calls attach', async () => {
    const onAttachObservedSession = vi.fn()
    render(
      <ChatInput
        {...defaultProps}
        showObserveOverlay={true}
        onAttachObservedSession={onAttachObservedSession}
      />,
    )

    await userEvent.click(screen.getByText('Attach'))
    expect(onAttachObservedSession).toHaveBeenCalled()
  })
})
