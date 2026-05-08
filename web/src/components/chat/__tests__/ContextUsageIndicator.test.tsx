import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ContextUsageIndicator } from '../ContextUsageIndicator'

describe('ContextUsageIndicator', () => {
  it('renders the percentage and tooltip breakdown from hydrated usage data', () => {
    render(
      <ContextUsageIndicator
        totalInputTokens={250}
        outputTokens={30}
        contextWindow={1000}
        uncachedInputTokens={140}
        cacheReadTokens={90}
        cacheCreationTokens={20}
      />,
    )

    const indicator = screen.getByText('25%').closest('div')
    expect(indicator).toHaveClass('text-xs', 'text-muted-foreground')
    expect(indicator).toHaveAttribute(
      'title',
      [
        'Context: 250 / 1.0K tokens (25%)',
        '',
        'Input: 250',
        '  Cache read: 90',
        '  Cache write: 20',
        '  Uncached: 140',
        'Output: 30',
      ].join('\n'),
    )
  })

  it('shows a waiting tooltip before the first response hydrates usage', () => {
    render(
      <ContextUsageIndicator
        totalInputTokens={0}
        outputTokens={0}
        contextWindow={null}
      />,
    )

    const indicator = screen.getByText('0%').closest('div')
    expect(indicator).toHaveAttribute(
      'title',
      'Context usage: waiting for first response...',
    )
  })
})
