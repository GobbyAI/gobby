import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock react-syntax-highlighter
vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children, language }: { children: string; language: string }) => (
    <pre data-testid="syntax-highlighter" data-language={language}>
      {children}
    </pre>
  ),
}))

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  oneDark: {},
}))

// Mock ArtifactContext
const mockOpenCodeAsArtifact = vi.fn()
vi.mock('../artifacts/ArtifactContext', () => ({
  useArtifactContext: () => ({
    openCodeAsArtifact: mockOpenCodeAsArtifact,
  }),
}))

// Mock cn utility
vi.mock('../../../lib/utils', () => ({
  cn: (...args: string[]) => args.filter(Boolean).join(' '),
}))

import { codeBlockComponents } from '../CodeBlock'

const CodeBlock = codeBlockComponents.code!
const TableComponent = codeBlockComponents.table!
const AnchorComponent = codeBlockComponents.a!
const ImageComponent = codeBlockComponents.img!

describe('CodeBlock', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

    expect(screen.getByTitle('Copy code')).toBeTruthy()
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

  it('shows open-in-panel button for long code blocks', () => {
    const longCode = Array.from({ length: 20 }, (_, i) => `line ${i}`).join('\n')
    render(
      <CodeBlock className="language-js">
        {longCode}
      </CodeBlock>,
    )

    expect(screen.getByTitle('Open in panel')).toBeTruthy()
  })

  it('does not show open-in-panel for short code blocks', () => {
    render(
      <CodeBlock className="language-js">
        {'const x = 1;\nconst y = 2;'}
      </CodeBlock>,
    )

    expect(screen.queryByTitle('Open in panel')).toBeNull()
  })

  it('calls openCodeAsArtifact when panel button clicked', async () => {
    const longCode = Array.from({ length: 20 }, (_, i) => `line ${i}`).join('\n')
    render(
      <CodeBlock className="language-python">
        {longCode}
      </CodeBlock>,
    )

    await userEvent.click(screen.getByTitle('Open in panel'))
    expect(mockOpenCodeAsArtifact).toHaveBeenCalledWith(
      'python',
      longCode,
      'python snippet',
    )
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
