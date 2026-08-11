import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { TabBar } from '../TabBar'

const tabs = [
  { id: 'overview', label: 'Overview' },
  { id: 'changes', label: 'Changes' },
  { id: 'settings', label: 'Settings' },
]

describe('TabBar', () => {
  it('renders coarse-pointer tab and close targets as sibling buttons', () => {
    const onTabChange = vi.fn()
    const onTabClose = vi.fn()

    render(
      <TabBar
        tabs={tabs}
        activeTab="overview"
        onTabChange={onTabChange}
        onTabClose={onTabClose}
      />,
    )

    const tab = screen.getByRole('tab', { name: 'Overview' })
    const closeButton = screen.getByRole('button', { name: 'Close Overview' })

    expect(tab).toHaveClass('pointer-coarse:before:min-h-11', 'pointer-coarse:before:min-w-11')
    expect(closeButton).toHaveClass(
      'pointer-coarse:before:min-h-11',
      'pointer-coarse:before:min-w-11',
    )
    expect(tab).not.toContainElement(closeButton)
    expect(tab.parentElement).toContainElement(closeButton)

    fireEvent.click(closeButton)
    expect(onTabClose).toHaveBeenCalledWith('overview')
    expect(onTabChange).not.toHaveBeenCalled()
  })

  it('exposes tab semantics and makes only the active tab tabbable', () => {
    render(<TabBar tabs={tabs} activeTab="changes" onTabChange={vi.fn()} />)

    const tablist = screen.getByRole('tablist')
    const renderedTabs = within(tablist).getAllByRole('tab')

    expect(renderedTabs[0]).toHaveAttribute('aria-selected', 'false')
    expect(renderedTabs[0]).toHaveAttribute('tabindex', '-1')
    expect(renderedTabs[1]).toHaveAttribute('aria-selected', 'true')
    expect(renderedTabs[1]).toHaveAttribute('tabindex', '0')
    expect(renderedTabs[2]).toHaveAttribute('aria-selected', 'false')
    expect(renderedTabs[2]).toHaveAttribute('tabindex', '-1')
  })

  it('moves focus and activates tabs with arrow, Home, and End keys', () => {
    const onTabChange = vi.fn()
    render(<TabBar tabs={tabs} activeTab="overview" onTabChange={onTabChange} />)

    const renderedTabs = screen.getAllByRole('tab')

    renderedTabs[0].focus()
    fireEvent.keyDown(renderedTabs[0], { key: 'ArrowRight' })
    expect(renderedTabs[1]).toHaveFocus()
    expect(onTabChange).toHaveBeenLastCalledWith('changes')

    fireEvent.keyDown(renderedTabs[1], { key: 'End' })
    expect(renderedTabs[2]).toHaveFocus()
    expect(onTabChange).toHaveBeenLastCalledWith('settings')

    fireEvent.keyDown(renderedTabs[2], { key: 'ArrowRight' })
    expect(renderedTabs[0]).toHaveFocus()
    expect(onTabChange).toHaveBeenLastCalledWith('overview')

    fireEvent.keyDown(renderedTabs[0], { key: 'ArrowLeft' })
    expect(renderedTabs[2]).toHaveFocus()
    expect(onTabChange).toHaveBeenLastCalledWith('settings')

    fireEvent.keyDown(renderedTabs[2], { key: 'Home' })
    expect(renderedTabs[0]).toHaveFocus()
    expect(onTabChange).toHaveBeenLastCalledWith('overview')
  })
})
