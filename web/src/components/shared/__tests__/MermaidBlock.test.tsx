import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

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

const { initializeMock, renderMock } = vi.hoisted(() => ({
  initializeMock: vi.fn(),
  renderMock: vi.fn(),
}))

vi.mock('mermaid', () => ({
  default: { initialize: initializeMock, render: renderMock },
}))

vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children, language }: { children: string; language: string }) => (
    <pre data-testid="syntax-highlighter" data-language={language}>
      {children}
    </pre>
  ),
}))

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  oneDark: {},
  oneLight: {},
}))

vi.mock('../../../lib/utils', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../lib/utils')>()
  return {
    ...actual,
    resolveCssVar: (varName: string) => `resolved(${varName})`,
  }
})

// Re-imported per test after vi.resetModules() so the module-level lazy
// singleton and initialized-theme state never leak between tests.
async function importMermaidBlock() {
  const mod = await import('../MermaidBlock')
  return mod.MermaidBlock
}

const DIAGRAM = 'graph TD;\n  A-->B;\n'

beforeEach(() => {
  vi.resetModules()
  initializeMock.mockClear()
  renderMock.mockReset()
  renderMock.mockResolvedValue({ svg: '<svg data-testid="mermaid-svg"><g /></svg>' })
  document.documentElement.removeAttribute('data-theme')
})

afterEach(() => {
  cleanup()
})

it('renders a language-mermaid fence as themed SVG with strict security', async () => {
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-mermaid">{DIAGRAM}</MermaidBlock>)

  expect(await screen.findByTestId('mermaid-svg')).toBeTruthy()
  expect(renderMock).toHaveBeenCalledTimes(1)
  expect(renderMock.mock.calls[0]?.[1]).toBe('graph TD;\n  A-->B;')

  expect(initializeMock).toHaveBeenCalledTimes(1)
  const config = initializeMock.mock.calls[0]?.[0]
  expect(config).toMatchObject({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
  })
  expect(config.themeVariables).toMatchObject({
    darkMode: true,
    background: 'resolved(--bg-secondary)',
    primaryColor: 'resolved(--bg-secondary)',
    textColor: 'resolved(--text-primary)',
    primaryTextColor: 'resolved(--text-primary)',
    lineColor: 'resolved(--border)',
    primaryBorderColor: 'resolved(--accent)',
  })
})

it('initializes with light themeVariables when the light theme is active', async () => {
  document.documentElement.setAttribute('data-theme', 'light')
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-mermaid">{DIAGRAM}</MermaidBlock>)

  await screen.findByTestId('mermaid-svg')
  expect(initializeMock.mock.calls[0]?.[0]?.themeVariables).toMatchObject({ darkMode: false })
})

it('re-initializes and re-renders when the resolved theme changes', async () => {
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-mermaid">{DIAGRAM}</MermaidBlock>)
  await screen.findByTestId('mermaid-svg')
  expect(initializeMock).toHaveBeenCalledTimes(1)

  act(() => {
    document.documentElement.setAttribute('data-theme', 'light')
  })

  await waitFor(() => expect(initializeMock).toHaveBeenCalledTimes(2))
  expect(initializeMock.mock.calls[1]?.[0]?.themeVariables).toMatchObject({ darkMode: false })
  await waitFor(() => expect(renderMock).toHaveBeenCalledTimes(2))
})

it('falls back to a highlighted code block with a note when rendering fails', async () => {
  renderMock.mockRejectedValue(new Error('parse error'))
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-mermaid">{DIAGRAM}</MermaidBlock>)

  expect(await screen.findByText(/diagram failed to render/i)).toBeTruthy()
  const fallback = screen.getByTestId('syntax-highlighter')
  expect(fallback.getAttribute('data-language')).toBe('mermaid')
  expect(fallback.textContent).toContain('A-->B')
  expect(screen.queryByTestId('mermaid-svg')).toBeNull()
})

it('delegates non-mermaid fences to CodeBlockInner unchanged', async () => {
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-python">{'print("hi")\nprint("bye")\n'}</MermaidBlock>)

  expect(screen.getByText('python')).toBeTruthy()
  expect(screen.getByTestId('syntax-highlighter').textContent).toContain('print("hi")')
  expect(renderMock).not.toHaveBeenCalled()
  expect(initializeMock).not.toHaveBeenCalled()
})

it('shows a loading skeleton while the diagram renders', async () => {
  let resolveRender: (value: { svg: string }) => void = () => {}
  renderMock.mockReturnValue(
    new Promise<{ svg: string }>((resolve) => {
      resolveRender = resolve
    }),
  )
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-mermaid">{DIAGRAM}</MermaidBlock>)

  expect(await screen.findByRole('status')).toBeTruthy()

  resolveRender({ svg: '<svg data-testid="mermaid-svg"><g /></svg>' })
  expect(await screen.findByTestId('mermaid-svg')).toBeTruthy()
  expect(screen.queryByRole('status')).toBeNull()
})

it('constrains diagram height until the expand toggle is pressed', async () => {
  const user = userEvent.setup()
  const MermaidBlock = await importMermaidBlock()
  render(<MermaidBlock className="language-mermaid">{DIAGRAM}</MermaidBlock>)

  const svg = await screen.findByTestId('mermaid-svg')
  const scroller = svg.parentElement as HTMLElement
  expect(scroller.className).toContain('overflow-auto')
  expect(scroller.className).toContain('max-h-')

  await user.click(screen.getByRole('button', { name: /expand/i }))
  expect(scroller.className).not.toContain('max-h-')
  expect(screen.getByRole('button', { name: /collapse/i })).toBeTruthy()
})

it('initializes mermaid once for multiple diagrams under the same theme', async () => {
  const MermaidBlock = await importMermaidBlock()
  render(
    <>
      <MermaidBlock className="language-mermaid">{'graph TD;\n  A-->B;\n'}</MermaidBlock>
      <MermaidBlock className="language-mermaid">{'graph TD;\n  C-->D;\n'}</MermaidBlock>
    </>,
  )

  await waitFor(() => expect(renderMock).toHaveBeenCalledTimes(2))
  expect(initializeMock).toHaveBeenCalledTimes(1)
  expect(renderMock.mock.calls[0]?.[0]).not.toBe(renderMock.mock.calls[1]?.[0])
  expect(await screen.findAllByTestId('mermaid-svg')).toHaveLength(2)
})
