import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

// Mock IntersectionObserver so LazyHighlighter renders SyntaxHighlighter immediately
vi.stubGlobal('IntersectionObserver', class {
  constructor(private callback: IntersectionObserverCallback) {}
  observe() {
    this.callback([{ isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }
  unobserve() {}
  disconnect() {}
})

import { Markdown } from '../Markdown'

describe('Markdown', () => {
  it('renders plain text', () => {
    render(<Markdown content="Hello world" id="test-1" />)
    expect(screen.getByText('Hello world')).toBeTruthy()
  })

  it('renders bold text', () => {
    render(<Markdown content="**bold text**" id="test-2" />)
    expect(screen.getByText('bold text')).toBeTruthy()
  })

  it('renders inline code', () => {
    render(<Markdown content="Use `vitest` for tests" id="test-3" />)
    expect(screen.getByText('vitest')).toBeTruthy()
  })

  it('renders links', () => {
    render(<Markdown content="[Click here](https://example.com)" id="test-4" />)
    const link = screen.getByText('Click here')
    expect(link).toBeTruthy()
    expect(link.closest('a')?.getAttribute('href')).toBe('https://example.com')
  })

  it('renders lists', () => {
    const { container } = render(<Markdown content={"- Item 1\n- Item 2\n- Item 3\n"} id="test-5" />)
    // ReactMarkdown renders list items inside ul/li
    expect(container.textContent).toContain('Item 1')
    expect(container.textContent).toContain('Item 2')
    expect(container.textContent).toContain('Item 3')
  })

  it('renders headings', () => {
    const { container } = render(<Markdown content={"# Heading 1\n\nSome text"} id="test-6" />)
    const heading = container.querySelector('h1')
    expect(heading).toBeTruthy()
    expect(heading?.textContent).toContain('Heading 1')
  })

  it('renders code blocks', () => {
    render(
      <Markdown
        content={'```python\nprint("hello")\n```'}
        id="test-7"
      />,
    )
    expect(screen.getByText(/print/)).toBeTruthy()
  })

  it('renders GFM tables', () => {
    const table = '| Col1 | Col2 |\n|------|------|\n| A | B |'
    const { container } = render(<Markdown content={table} id="test-8" />)
    expect(container.querySelector('table')).toBeTruthy()
    expect(screen.getByText('Col1')).toBeTruthy()
  })

  it('handles multiline content', () => {
    const { container } = render(
      <Markdown content="Paragraph 1\n\nParagraph 2" id="test-9" />,
    )
    // Should render content
    expect(container.textContent).toContain('Paragraph 1')
    expect(container.textContent).toContain('Paragraph 2')
  })

  it('strips permissions and collaboration wrapper tags', () => {
    const { container } = render(
      <Markdown
        content={
          '<permissions instructions>Filesystem sandboxing defines which files can be read or written.</permissions instructions><collaboration_mode>Collaboration Mode: Default</collaboration_mode>'
        }
        id="test-10"
      />,
    )

    expect(container.textContent).toContain(
      'Filesystem sandboxing defines which files can be read or written.',
    )
    expect(container.textContent).toContain('Collaboration Mode: Default')
    expect(container.textContent).not.toContain('permissions instructions')
    expect(container.textContent).not.toContain('collaboration_mode')
  })

  it('strips Codex protocol wrappers and metadata blocks', () => {
    const { container } = render(
      <Markdown
        content={
          '<turn_aborted>The user interrupted the previous turn.</turn_aborted>\n' +
          '<environment_context><shell>zsh</shell></environment_context>\n' +
          '<INSTRUCTIONS># Repository Guidelines</INSTRUCTIONS>\n' +
          '<skills_instructions>Safety first.</skills_instructions>'
        }
        id="test-11"
      />,
    )

    expect(container.textContent).toContain('The user interrupted the previous turn.')
    expect(container.textContent).toContain('Repository Guidelines')
    expect(container.textContent).toContain('Safety first.')
    expect(container.textContent).not.toContain('environment_context')
    expect(container.textContent).not.toContain('turn_aborted')
    expect(container.textContent).not.toContain('INSTRUCTIONS')
    expect(container.textContent).not.toContain('skills_instructions')
    expect(container.textContent).not.toContain('zsh')
  })
})
