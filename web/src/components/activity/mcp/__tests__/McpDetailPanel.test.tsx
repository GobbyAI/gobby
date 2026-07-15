import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { McpDetailPanel } from '../McpDetailPanel'

describe('McpDetailPanel', () => {
  it('blocks tool calls while a JSON argument is invalid', () => {
    const onCallTool = vi.fn()
    render(
      <McpDetailPanel
        selection={{ kind: 'tool', serverName: 'demo', toolName: 'run' }}
        server={null}
        tool={{ name: 'run', brief: 'Run demo' }}
        schema={{
          name: 'run',
          inputSchema: { properties: { payload: { type: 'object' } } },
        }}
        schemaLoading={false}
        argumentValues={{}}
        onArgumentValuesChange={vi.fn()}
        executing={false}
        executionResult={null}
        onCallTool={onCallTool}
        status={null}
        toolsByServer={{}}
      />,
    )

    const callButton = screen.getByRole('button', { name: 'Call tool' })
    fireEvent.change(screen.getByRole('textbox', { name: 'payload' }), {
      target: { value: '{' },
    })

    expect(callButton).toBeDisabled()
    fireEvent.click(callButton)
    expect(onCallTool).not.toHaveBeenCalled()
  })
})
