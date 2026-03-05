import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ChatInput } from '../ChatInput'

// Mock sub-components to isolate ChatInput
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

describe('ChatInput', () => {
  const defaultProps = {
    onSend: vi.fn(),
    onStop: vi.fn(),
  }

  beforeEach(() => {
    vi.clearAllMocks()
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

  it('renders attach file button', () => {
    render(<ChatInput {...defaultProps} />)

    expect(screen.getByTitle('Attach file')).toBeTruthy()
  })

  it('renders voice button when voice is available', () => {
    render(
      <ChatInput {...defaultProps} voiceAvailable={true} onToggleVoice={vi.fn()} />,
    )

    expect(screen.getByTitle('Enable voice mode')).toBeTruthy()
  })

  it('shows voice listening indicator when voice mode active', () => {
    render(
      <ChatInput
        {...defaultProps}
        voiceMode={true}
        voiceAvailable={true}
        isListening={true}
        onToggleVoice={vi.fn()}
      />,
    )

    expect(screen.getByText('Ready — speak to send')).toBeTruthy()
  })

  it('shows speech detected indicator', () => {
    render(
      <ChatInput
        {...defaultProps}
        voiceMode={true}
        voiceAvailable={true}
        isListening={true}
        isSpeechDetected={true}
        onToggleVoice={vi.fn()}
      />,
    )

    expect(screen.getByText('Listening...')).toBeTruthy()
  })

  it('shows voice error', () => {
    render(<ChatInput {...defaultProps} voiceError="Mic not found" />)

    expect(screen.getByText('Mic not found')).toBeTruthy()
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
})
