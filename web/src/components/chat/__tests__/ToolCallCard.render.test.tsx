import { describe, expect, it } from 'vitest'
import { fireEvent } from '@testing-library/react'
import type { ToolCall } from '../../../types/chat'
import { classifyTool } from '../../../types/chat'
import { renderWithProviders, screen } from '../../../test/helpers'
import { ToolCallCards } from '../ToolCallCard'

const DATA_URI = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='

function makeCall(overrides: Partial<ToolCall> & { id: string; tool_name: string }): ToolCall {
  return {
    server_name: 'builtin',
    status: 'completed',
    tool_type: classifyTool(overrides.tool_name),
    ...overrides,
  }
}

describe('ToolCallCard rendering', () => {
  it('renders exec_command like a bash card in collapsed headers', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'exec_command',
            arguments: { cmd: 'git status --short' },
          }),
        ]}
      />,
    )

    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('git status --short')).toBeInTheDocument()
  })

  it('suppresses internal write_stdin calls while neighboring tool calls still render (#19188)', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-stdin',
            tool_name: 'write_stdin',
            arguments: { session_id: 'sess-1', chars: 'q', yield_time_ms: 500 },
            result: { content: 'ok', kind: 'text', truncated: false },
          }),
          makeCall({
            id: 'tool-neighbor',
            tool_name: 'exec_command',
            arguments: { cmd: 'git status --short' },
          }),
        ]}
      />,
    )

    expect(screen.queryByText(/write_stdin/i)).toBeNull()
    expect(screen.queryByText('sess-1')).toBeNull()
    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('git status --short')).toBeInTheDocument()
  })

  it('does not render completed tool calls that only carry a null result', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-null',
            tool_name: 'exec_command',
            result: null as never,
          }),
        ]}
      />,
    )

    expect(container.textContent).toBe('')
    expect(screen.queryByText('Bash')).toBeNull()
    expect(screen.queryByText('Result')).toBeNull()
  })

  it('renders arguments without leaking Result null when a completed result is null', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-null-with-args',
            tool_name: 'exec_command',
            arguments: { cmd: 'true' },
            result: null as never,
          }),
        ]}
      />,
    )

    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('true')).toBeInTheDocument()
    expect(screen.queryByText('Result')).toBeNull()
    expect(screen.queryByText('null')).toBeNull()
  })

  it('renders malformed file_path payloads without crashing', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-read-malformed-path',
            tool_name: 'Read',
            status: 'completed',
            arguments: { file_path: { path: '/src/main.ts' } },
            result: { kind: 'text', content: '1→const value = true', truncated: false },
          }),
          makeCall({
            id: 'tool-write-malformed-path',
            tool_name: 'Write',
            status: 'calling',
            arguments: { file_path: ['/src/out.ts'], content: 'export {}' },
          }),
        ]}
      />,
    )

    fireEvent.click(screen.getByText('Read'))

    expect(screen.getAllByText('Arguments')).toHaveLength(2)
    expect(screen.getByText('Result')).toBeInTheDocument()
    expect(screen.getByText('const value = true')).toBeInTheDocument()
  })

  it('renders 3+ same-tool runs through the quieter ToolCallGroupHeader (canonical Bash name)', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'exec_command',
            arguments: { cmd: 'git status --short' },
          }),
          makeCall({
            id: 'tool-2',
            tool_name: 'exec_command',
            arguments: { cmd: 'git diff --stat' },
          }),
          makeCall({
            id: 'tool-3',
            tool_name: 'exec_command',
            arguments: { cmd: 'git log --oneline -5' },
          }),
        ]}
      />,
    )

    // Same-tool run of 3 collapses into one quieter group header (canonical "Bash" name + ×3 badge).
    // Group is expanded by default for non-Protocol tools, so the header "Bash" plus 3 inner cards
    // also showing "Bash" gives 4 occurrences total.
    expect(screen.getAllByText('Bash')).toHaveLength(4)
    expect(screen.getByText('×3')).toBeInTheDocument()
    expect(screen.getByText('×3').closest('.border-l')).toHaveClass(
      'border-success-foreground/40',
    )
    // No "N tool calls" outer wrapper text — the ToolChainGroup wrapper is gone.
    expect(screen.queryByText(/^\d+ tool calls?$/)).toBeNull()
    expect(container.querySelector('.border-l')).toBeTruthy()
  })

  it('marks grouped error and in-flight tool runs with stateful left borders', () => {
    const { rerender } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'exec_command',
            status: 'completed',
            arguments: { cmd: 'true' },
          }),
          makeCall({ id: 'tool-2', tool_name: 'exec_command', status: 'error' }),
          makeCall({
            id: 'tool-3',
            tool_name: 'exec_command',
            status: 'completed',
            arguments: { cmd: 'git status --short' },
          }),
        ]}
      />,
    )

    expect(screen.getByText('×3').closest('.border-l')).toHaveClass(
      'border-destructive-foreground/50',
    )

    rerender(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'exec_command',
            status: 'completed',
            arguments: { cmd: 'true' },
          }),
          makeCall({ id: 'tool-2', tool_name: 'exec_command', status: 'calling' }),
          makeCall({
            id: 'tool-3',
            tool_name: 'exec_command',
            status: 'completed',
            arguments: { cmd: 'git status --short' },
          }),
        ]}
      />,
    )

    expect(screen.getByText('×3').closest('.border-l')).toHaveClass('border-accent/50')
  })

  it('renders 2 same-tool calls flat with no grouping wrapper (threshold is 3)', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'exec_command',
            arguments: { cmd: 'git status --short' },
          }),
          makeCall({
            id: 'tool-2',
            tool_name: 'exec_command',
            arguments: { cmd: 'git diff --stat' },
          }),
        ]}
      />,
    )

    expect(screen.queryByText('×2')).toBeNull()
    expect(screen.queryByText(/^\d+ tool calls?$/)).toBeNull()
    expect(screen.getAllByText('Bash')).toHaveLength(2)
  })

  it('flattens MCP proxy wrappers in rendered tool results', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'call_tool',
            result: {
              content: {
                success: true,
                result: { success: true },
                response_time_ms: 42,
              },
              kind: 'json',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    // Unknown-type tool calls collapse by default; click the header to expand.
    fireEvent.click(screen.getByText('call_tool'))

    const resultPanel = screen.getByText('Result').parentElement
    expect(resultPanel).toHaveClass('min-w-0', 'max-w-full', 'overflow-hidden')

    // The MCP proxy envelope ({success, result, response_time_ms}) gets flattened
    // and rendered through JsonResultBlock — the inner `result` wrapper is gone.
    const code = container.querySelector('code')?.textContent ?? ''
    expect(code).toContain('"success": true')
    expect(code).toContain('"response_time_ms": 42')
    expect(code).not.toContain('"result":')
  })

  it('renders all MCP text blocks and indicates additional content blocks', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-mcp-content',
            tool_name: 'mcp__gobby__call_tool',
            result: {
              content: {
                content: [
                  { type: 'text', text: 'first result' },
                  { type: 'image', data: 'base64-image-data', mimeType: 'image/png' },
                  { type: 'text', text: 'second result' },
                ],
                is_error: false,
              },
              kind: 'json',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    expect(container.textContent).toContain('first result')
    expect(container.textContent).toContain('second result')
    expect(screen.getByText('+1 more blocks')).toBeInTheDocument()
  })

  it('renders Codex image output tool results inline', () => {
    renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-image',
            tool_name: 'mcp__image_gen__imagegen',
            tool_type: 'mcp',
            result: {
              content: {
                output: [{ type: 'input_image', image_url: DATA_URI }],
              },
              kind: 'json',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    const previewButton = screen.getByRole('button', {
      name: 'Open full-size tool result image',
    })
    const previewImage = screen.getByAltText('Tool result image')

    expect(previewImage).toHaveAttribute('src', DATA_URI)
    expect(previewButton.parentElement).toHaveClass('justify-center')

    fireEvent.click(previewButton)

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByAltText('Full-size tool result image')).toHaveAttribute(
      'src',
      DATA_URI,
    )
    expect(screen.getByRole('link', { name: 'Open' })).toHaveAttribute(
      'href',
      DATA_URI,
    )
    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
      'download',
      'tool-result-image.png',
    )
  })

  it('renders bash output envelopes as terminal text in the result panel', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'exec_command',
            arguments: {
              cmd: "UV_CACHE_DIR=/tmp/uv-cache uv run python - <<'PY'",
            },
            result: {
              content: {
                output:
                  'Chunk ID: 21a8f9\nWall time: 0.1813 seconds\nOutput:\nhash ok? True\n',
              },
              kind: 'json',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    const resultPanel = screen.getByText('Result').parentElement

    expect(screen.getByText('Result')).toBeInTheDocument()
    expect(resultPanel).toHaveClass('min-w-0', 'max-w-full', 'overflow-hidden')

    // The bash envelope renders verbatim as terminal text (gsqz wrapper
    // parsing was retired, so no metadata strip is synthesized).
    expect(container.textContent).toContain('Chunk ID: 21a8f9')

    // Body text renders without leaking the JSON envelope key.
    expect(container.textContent).toContain('hash ok? True')
    expect(container.textContent).not.toContain('"output"')
  })

  it('renders protocol tool calls with a protocol header and tag summary', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'protocol_context',
            tool_type: 'protocol',
            arguments: { tag: 'environment_context' },
            result: {
              content: { shell: 'zsh', timezone: 'America/Chicago' },
              kind: 'json',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    expect(screen.getByText('Protocol')).toBeInTheDocument()
    expect(screen.getByText('environment_context')).toBeInTheDocument()
    expect(container.querySelector('code')).toBeNull()

    fireEvent.click(screen.getByText('Protocol'))

    const code = container.querySelector('code')?.textContent ?? ''
    expect(code).toContain('"shell": "zsh"')
    expect(code).toContain('"timezone": "America/Chicago"')
  })

  it('collapses grouped protocol tool calls by default', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-1',
            tool_name: 'protocol_context',
            tool_type: 'protocol',
            arguments: { tag: 'system_instructions' },
            result: {
              content: 'System instructions',
              kind: 'text',
              truncated: false,
            },
          }),
          makeCall({
            id: 'tool-2',
            tool_name: 'protocol_context',
            tool_type: 'protocol',
            arguments: { tag: 'environment_context' },
            result: {
              content: { shell: 'zsh' },
              kind: 'json',
              truncated: false,
            },
          }),
          makeCall({
            id: 'tool-3',
            tool_name: 'protocol_context',
            tool_type: 'protocol',
            arguments: { tag: 'collaboration_mode' },
            result: {
              content: 'Default',
              kind: 'text',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    expect(screen.getByText('Protocol')).toBeInTheDocument()
    expect(container.querySelector('code')).toBeNull()

    fireEvent.click(screen.getByText('Protocol'))

    expect(container.textContent).toContain('system_instructions')
    expect(container.textContent).toContain('environment_context')
  })

  it('renders ACP tool metadata, diff content, terminal blocks, and raw output', () => {
    const { container } = renderWithProviders(
      <ToolCallCards
        toolCalls={[
          makeCall({
            id: 'tool-acp',
            tool_name: 'edit',
            tool_kind: 'edit',
            locations: [{ uri: 'file:///src/app.py', line: 12, column: 4 }],
            content_blocks: [
              {
                type: 'diff',
                path: 'src/app.py',
                old_text: 'old',
                new_text: 'new',
              },
              { type: 'terminal', terminal_id: 'term-1' },
              {
                type: 'resource_link',
                uri: 'file:///src/app.py',
                name: 'src/app.py',
              },
            ],
            raw_output: { stdout: 'ok' },
          }),
        ]}
      />,
    )

    expect(screen.getAllByText('edit')).toHaveLength(2)
    expect(screen.getByText('file:///src/app.py:12:4')).toBeInTheDocument()
    expect(screen.getAllByText('src/app.py').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Terminal term-1')).toBeInTheDocument()
    expect(container.textContent).toContain('"stdout": "ok"')
  })
})
