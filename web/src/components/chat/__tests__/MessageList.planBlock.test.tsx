import { describe, it, expect, vi } from 'vitest'
import * as ReactMod from 'react'
import { render, screen } from '@testing-library/react'
import type { ChatMessage } from '../../../types/chat'

// Render Virtuoso's items + Footer inline so the Footer-hosted plan block is
// observable in jsdom.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ReactMod.forwardRef(function MockVirtuoso(
    {
      data,
      itemContent,
      components,
    }: {
      data: ChatMessage[]
      itemContent: (index: number, message: ChatMessage) => ReactMod.ReactNode
      components?: { Footer?: ReactMod.ComponentType }
    },
    ref: ReactMod.ForwardedRef<{ scrollToIndex: () => void }>,
  ) {
    ReactMod.useImperativeHandle(ref, () => ({ scrollToIndex: () => {} }), [])
    const Footer = components?.Footer
    return (
      <div data-testid="virtuoso">
        {data.map((m, i) => (
          <div key={m.id}>{itemContent(i, m)}</div>
        ))}
        {Footer ? <Footer /> : null}
      </div>
    )
  }),
}))

vi.mock('../MessageItem', () => ({
  MessageItem: ({ message }: { message: ChatMessage }) => (
    <div data-testid="message-item">{message.content}</div>
  ),
}))

import { MessageList } from '../MessageList'

function planTurn(): ChatMessage {
  return {
    id: 'm1',
    role: 'assistant',
    content: 'Here is my plan',
    timestamp: new Date(1_700_000_000_000),
  }
}

describe('MessageList plan-pending block', () => {
  it('renders the inline approval block after the plan turn when pending', () => {
    render(
      <MessageList
        messages={[planTurn()]}
        isStreaming={false}
        isThinking={false}
        planPendingApproval
        onApprovePlan={vi.fn()}
        onRequestPlanChanges={vi.fn()}
        onViewPlan={vi.fn()}
      />,
    )

    const block = screen.getByTestId('plan-pending-block')
    expect(block).toBeInTheDocument()

    // The block renders after the plan turn in DOM order.
    const items = screen.getAllByTestId('message-item')
    expect(items).toHaveLength(1)
    expect(
      items[0].compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('does not render the block when no plan is pending', () => {
    render(
      <MessageList
        messages={[planTurn()]}
        isStreaming={false}
        isThinking={false}
        planPendingApproval={false}
        onApprovePlan={vi.fn()}
        onRequestPlanChanges={vi.fn()}
      />,
    )

    expect(screen.queryByTestId('plan-pending-block')).not.toBeInTheDocument()
  })
})
