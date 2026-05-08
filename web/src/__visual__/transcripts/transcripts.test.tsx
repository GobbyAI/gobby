import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

import claudeFixture from './fixtures/claude.json'
import codexFixture from './fixtures/codex.json'
import droidFixture from './fixtures/droid.json'
import geminiFixture from './fixtures/gemini.json'
import qwenFixture from './fixtures/qwen.json'

// LazyHighlighter waits for IntersectionObserver to mark a code block as
// visible before mounting Prism. jsdom never fires intersections, so we
// stub it to mark every block visible immediately and synchronously.
vi.stubGlobal('IntersectionObserver', class {
  constructor(private callback: IntersectionObserverCallback) {}
  observe(target: Element) {
    this.callback(
      [{ isIntersecting: true, target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    )
  }
  unobserve() {}
  disconnect() {}
})

// Prism's full token tree is enormous and orthogonal to the structural
// drift we care about here. Render the raw text with the language as a
// data attribute so the snapshot still records "this is highlighted code
// in language X" without baking in Prism's grammar internals.
vi.mock('react-syntax-highlighter', () => ({
  Prism: ({
    children,
    language,
    showLineNumbers,
    className,
  }: {
    children: string
    language?: string
    showLineNumbers?: boolean
    className?: string
  }) => (
    <pre
      data-testid="syntax-highlighter"
      data-language={language ?? ''}
      data-line-numbers={showLineNumbers ? 'true' : 'false'}
      className={className}
    >
      {children}
    </pre>
  ),
}))

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  oneDark: {},
}))

import type { ChatMessage } from '../../types/chat'
import { MessageItem } from '../../components/chat/MessageItem'

type Fixture = ChatMessage[]

// Fixtures ship as JSON, so timestamps land as ISO strings. Promote them
// to Date instances at load time so the fixtures match the ChatMessage
// contract the rest of the chat surface expects.
function loadFixture(raw: unknown): Fixture {
  const messages = raw as Array<ChatMessage & { timestamp: string | Date }>
  return messages.map((message) => ({
    ...message,
    timestamp:
      message.timestamp instanceof Date
        ? message.timestamp
        : new Date(message.timestamp),
  }))
}

const FIXTURES: Record<string, Fixture> = {
  claude: loadFixture(claudeFixture),
  codex: loadFixture(codexFixture),
  droid: loadFixture(droidFixture),
  gemini: loadFixture(geminiFixture),
  qwen: loadFixture(qwenFixture),
}

const THEMES = ['dark', 'light'] as const

// MessageList wraps MessageItems in a Virtuoso scroller. Virtuoso doesn't
// render its rows in jsdom, so the harness mirrors the outer wrapper
// classes that ship around the items in production and renders the items
// directly. Same MessageItem tree, same container shell.
function MessageListSurface({ messages }: { messages: ChatMessage[] }) {
  return (
    <div
      data-testid="surface-message-list"
      className="chat-scaled flex-1 min-h-0 overflow-x-hidden"
    >
      {messages.map((message) => (
        <MessageItem key={message.id} message={message} />
      ))}
    </div>
  )
}

// SessionsTab transcript mode renders MessageItems inside a plain
// scroller. Mirrors the wrapper at SessionsTab.tsx around the message map.
function SessionsTabSurface({ messages }: { messages: ChatMessage[] }) {
  return (
    <div
      data-testid="surface-sessions-tab"
      className="flex-1 overflow-y-auto chat-scaled"
    >
      {messages.map((message) => (
        <MessageItem key={message.id} message={message} />
      ))}
    </div>
  )
}

const SURFACES = {
  messageList: MessageListSurface,
  sessionsTab: SessionsTabSurface,
} as const

// toLocaleTimeString resolves against the system locale at render time,
// so the same Date can produce "12:00:00 PM" locally and "12:00:00" in
// CI. Pin a stable representation for snapshots.
const originalToLocaleTimeString = Date.prototype.toLocaleTimeString
beforeAll(() => {
  Date.prototype.toLocaleTimeString = function (this: Date): string {
    return this.toISOString().slice(11, 19)
  }
})
afterAll(() => {
  Date.prototype.toLocaleTimeString = originalToLocaleTimeString
})

describe('transcript visual regression', () => {
  for (const [provider, fixture] of Object.entries(FIXTURES)) {
    for (const [surfaceName, Surface] of Object.entries(SURFACES)) {
      for (const theme of THEMES) {
        it(`${provider} / ${surfaceName} / ${theme}`, () => {
          const { container } = render(
            <div data-theme={theme} className={theme}>
              <Surface messages={fixture} />
            </div>,
          )
          expect(container.innerHTML).toMatchSnapshot()
        })
      }
    }
  }
})
