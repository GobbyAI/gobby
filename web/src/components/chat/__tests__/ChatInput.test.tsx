import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { ChatInput } from '../ChatInput'

vi.mock('../ModeSelector', () => ({
  ModeSelector: ({ mode }: { mode: string }) => <div data-testid="mode-selector">{mode}</div>,
}))
vi.mock('../ContextUsageIndicator', () => ({
  ContextUsageIndicator: () => <div data-testid="context-usage" />,
}))
vi.mock('../BranchIndicator', () => ({
  BranchIndicator: () => <div data-testid="branch-indicator" />,
}))
vi.mock('../ActiveAgentIndicator', () => ({
  ActiveAgentIndicator: () => <div data-testid="agent-indicator" />,
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
  onSend?: (message: string, files?: unknown) => void
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

  it('shows connecting placeholder when disabled', () => {
    render(<ChatInput {...defaultProps} disabled={true} />)

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveAttribute('aria-label', 'Message input — connecting')
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

  it('calls onSend when Enter is pressed', async () => {
    const onSend = vi.fn()
    render(<ChatInput {...defaultProps} onSend={onSend} />)

    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, 'Hello world')
    await userEvent.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('Hello world', undefined)
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
    render(
      <ChatInput {...defaultProps} onModeChange={vi.fn()} mode="accept_edits" />,
    )

    expect(screen.getByTestId('mode-selector')).toBeTruthy()
    expect(screen.getByText('accept_edits')).toBeTruthy()
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

    expect(onSend).toHaveBeenCalledWith('Hello', undefined)
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

  it('formats known provider labels with canonical casing', () => {
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

    expect(screen.getByText('OpenAI local')).toBeTruthy()
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

    expect(onSend).toHaveBeenCalledWith('/plan', undefined)
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
