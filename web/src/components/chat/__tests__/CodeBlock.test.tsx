import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { CSSProperties } from 'react'

// Mock IntersectionObserver so LazyHighlighter renders SyntaxHighlighter immediately
vi.stubGlobal('IntersectionObserver', class {
  constructor(private callback: IntersectionObserverCallback) {}
  observe() {
    // Fire synchronously so the component re-renders within the same act() cycle
    this.callback([{ isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }
  unobserve() {}
  disconnect() {}
})

// Mock react-syntax-highlighter; expose wrap props so the mono overflow
// contract (#19186) is assertable.
vi.mock('react-syntax-highlighter', () => ({
  Prism: ({
    children,
    language,
    customStyle,
    wrapLongLines,
  }: {
    children: string
    language: string
    customStyle?: CSSProperties
    wrapLongLines?: boolean
  }) => (
    <pre
      data-testid="syntax-highlighter"
      data-language={language}
      data-wrap-long-lines={wrapLongLines ? 'true' : 'false'}
      style={customStyle}
    >
      {children}
    </pre>
  ),
}))

vi.mock('../../../hooks/useIsMobile', () => ({
  useIsMobile: vi.fn(() => false),
}))

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  oneDark: {},
  oneLight: {},
}))

// Mock cn utility
vi.mock('../../../lib/utils', () => ({
  cn: (...args: string[]) => args.filter(Boolean).join(' '),
}))

import { codeBlockComponents } from '../CodeBlock'
import { useIsMobile } from '../../../hooks/useIsMobile'

const CodeBlock = codeBlockComponents.code!
const TableComponent = codeBlockComponents.table!
const AnchorComponent = codeBlockComponents.a!
const ImageComponent = codeBlockComponents.img!

describe('CodeBlock', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(useIsMobile).mockReturnValue(false)
  })

  it('renders inline code when no language and no newlines', () => {
    render(<CodeBlock>{'hello'}</CodeBlock>)

    const code = screen.getByText('hello')
    expect(code.tagName).toBe('CODE')
  })

  it('renders code block with language', () => {
    render(
      <CodeBlock className="language-typescript">
        {'const x = 1;\nconst y = 2;'}
      </CodeBlock>,
    )

    expect(screen.getByTestId('syntax-highlighter')).toBeTruthy()
    expect(screen.getByTestId('syntax-highlighter').dataset.language).toBe('typescript')
  })

  it('wraps fences on the mobile tier per the mono overflow contract (#19186)', () => {
    vi.mocked(useIsMobile).mockReturnValue(true)
    render(
      <CodeBlock className="language-typescript">
        {'const x = 1;\nconst y = 2;'}
      </CodeBlock>,
    )

    const pre = screen.getByTestId('syntax-highlighter')
    expect(pre.dataset.wrapLongLines).toBe('true')
    expect(pre).toHaveStyle({ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' })
  })

  it('keeps the desktop scroll affordance on fences', () => {
    render(
      <CodeBlock className="language-typescript">
        {'const x = 1;\nconst y = 2;'}
      </CodeBlock>,
    )

    const pre = screen.getByTestId('syntax-highlighter')
    expect(pre.dataset.wrapLongLines).toBe('false')
    expect(pre.style.whiteSpace).toBe('')
    expect(pre.style.overflowWrap).toBe('')
  })

  it('shows language label in header', () => {
    render(
      <CodeBlock className="language-python">
        {'def foo():\n    pass'}
      </CodeBlock>,
    )

    expect(screen.getByText('python')).toBeTruthy()
  })

  it('shows "text" label when no language', () => {
    render(
      <CodeBlock>
        {'line 1\nline 2'}
      </CodeBlock>,
    )

    expect(screen.getByText('text')).toBeTruthy()
  })

  it('shows copy button', () => {
    render(
      <CodeBlock className="language-js">
        {'const x = 1;\nconst y = 2;'}
      </CodeBlock>,
    )

    expect(screen.getByTitle('Copy code')).toHaveClass(
      'pointer-coarse:min-h-11',
      'pointer-coarse:min-w-11',
    )
  })

  it('copies code on click', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    render(
      <CodeBlock className="language-js">
        {'const x = 1;\nconst y = 2;'}
      </CodeBlock>,
    )

    await userEvent.click(screen.getByTitle('Copy code'))
    expect(writeText).toHaveBeenCalledWith('const x = 1;\nconst y = 2;')
  })

  it('strips trailing newline from code string', () => {
    render(
      <CodeBlock className="language-js">
        {'const x = 1;\n'}
      </CodeBlock>,
    )

    expect(screen.getByTestId('syntax-highlighter').textContent).toBe('const x = 1;')
  })
})

describe('TableWrapper', () => {
  it('renders a table with overflow wrapper', () => {
    const { container } = render(
      <TableComponent>
        <tbody>
          <tr><td>cell</td></tr>
        </tbody>
      </TableComponent>,
    )

    expect(container.querySelector('table')).toBeTruthy()
    expect(container.querySelector('.overflow-x-auto')).toBeTruthy()
  })
})

describe('Anchor', () => {
  it('renders external links with target _blank', () => {
    render(<AnchorComponent href="https://example.com">Link</AnchorComponent>)

    const link = screen.getByText('Link')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('renders internal links without target _blank', () => {
    render(<AnchorComponent href="/page">Link</AnchorComponent>)

    const link = screen.getByText('Link')
    expect(link.getAttribute('target')).toBeNull()
  })
})

describe('ImageBlock', () => {
  it('renders image with alt text', () => {
    render(<ImageComponent src="test.png" alt="Test image" />)

    const img = screen.getByAltText('Test image')
    expect(img).toBeTruthy()
    expect(img.getAttribute('src')).toBe('test.png')
  })

  it('renders fallback alt text', () => {
    render(<ImageComponent src="test.png" />)

    expect(screen.getByAltText('Image')).toBeTruthy()
  })
})
