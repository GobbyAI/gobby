import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
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
})
