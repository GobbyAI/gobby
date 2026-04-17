import { describe, expect, it } from 'vitest'
import { fireEvent } from '@testing-library/react'
import type { ToolCall } from '../../../types/chat'
import { classifyTool } from '../../../types/chat'
import { renderWithProviders, screen } from '../../../test/helpers'
import { ToolCallCards, ToolChainGroup } from '../ToolCallCard'

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

  it('uses canonical bash names in multi-call chain summaries', () => {
    renderWithProviders(
      <ToolChainGroup
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

    expect(screen.getByText('2 tool calls')).toBeInTheDocument()
    expect(screen.getByText('2 Bash')).toBeInTheDocument()
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
              content_type: 'json',
              truncated: false,
            },
          }),
        ]}
      />,
    )

    const code = container.querySelector('code')?.textContent ?? ''
    expect(code).toContain('"success": true')
    expect(code).toContain('"response_time_ms": 42')
    expect(code).not.toContain('"result":')
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
              content_type: 'json',
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
              content_type: 'text',
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
              content_type: 'json',
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
})
