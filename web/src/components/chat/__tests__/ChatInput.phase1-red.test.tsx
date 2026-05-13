import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ChatInput } from '../ChatInput'

describe('ChatInput Phase 1 sizing contract', () => {
  it('renders a one-line input footprint with a 36px send button', () => {
    render(<ChatInput onSend={vi.fn()} />)

    const textarea = screen.getByRole('textbox', { name: 'Message input' })
    const sendButton = screen.getByRole('button', { name: 'Send message' })

    expect(textarea).toHaveClass('min-h-[36px]')
    expect(textarea).not.toHaveClass('min-h-[52px]')
    expect(sendButton).toHaveClass('h-[36px]', 'w-[36px]', 'self-end')
    expect(sendButton).not.toHaveClass('h-[52px]', 'w-[52px]', 'self-start')
  })

  it('keeps textarea auto-grow capped at 200px', () => {
    render(<ChatInput onSend={vi.fn()} />)

    const textarea = screen.getByRole('textbox', { name: 'Message input' })
    Object.defineProperty(textarea, 'scrollHeight', {
      configurable: true,
      value: 260,
    })

    fireEvent.change(textarea, { target: { value: 'one\ntwo\nthree\nfour\nfive' } })

    expect(textarea).toHaveStyle({ height: '200px' })
  })

  it('keeps speaker and microphone controls in the toolbar', () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        onSttEnabledChange={vi.fn()}
        onTtsEnabledChange={vi.fn()}
        onVoiceInputModeChange={vi.fn()}
        prepareTTSPlayback={vi.fn(async () => {})}
        startRecording={vi.fn(async () => {})}
        stopRecording={vi.fn(async () => {})}
      />,
    )

    expect(screen.getByRole('button', { name: 'Toggle text-to-speech' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Microphone off; enable push to talk' })).toBeInTheDocument()
  })
})
