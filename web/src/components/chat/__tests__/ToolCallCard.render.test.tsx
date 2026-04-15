import { describe, expect, it } from 'vitest'
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
})
