import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ActivityPanel } from '../ActivityPanel'
import {
  ACTIVITY_PANEL_DROPDOWN_TABS,
  ACTIVITY_PANEL_TABS,
} from '../ActivityPanelTabs'
vi.mock('../../shared/ResizeHandle', () => ({
  ResizeHandle: ({
    panelWidth,
    minWidth,
    maxWidth,
  }: {
    panelWidth?: number
    minWidth?: number
    maxWidth?: number
  }) => (
    <div
      data-testid="resize-handle"
      data-panel-width={panelWidth}
      data-min-width={minWidth}
      data-max-width={maxWidth}
    />
  ),
}))

vi.mock('../PlansTab', () => ({
  PlansTab: () => <div>Plans Tab</div>,
}))

vi.mock('../FileChangesTab', () => ({
  FileChangesTab: () => <div>Changes Tab</div>,
}))


vi.mock('../SessionsTab', () => ({
  SessionsTab: () => <div>Sessions Tab</div>,
}))

vi.mock('../PipelinesTab', () => ({
  PipelinesTab: () => <div>Pipelines Tab</div>,
}))

vi.mock('../TasksTab', () => ({
  TasksTab: () => <div>Tasks Tab</div>,
}))

vi.mock('../FilesTab', () => ({
  FilesTab: () => <div>Files Tab</div>,
}))

vi.mock('../CronTab', () => ({
  CronTab: () => <div>Cron Tab</div>,
}))

vi.mock('../TracesTab', () => ({
  TracesTab: () => <div>Traces Tab</div>,
}))

vi.mock('../ActivityMcpTab', () => ({
  ActivityMcpTab: () => <div>MCP Tab</div>,
}))

vi.mock('../AgentsTab', () => ({
  AgentsTab: () => <div>Agents Tab</div>,
}))

describe('ActivityPanel', () => {
  it('registers Terminal immediately after Sessions with the prompt icon', () => {
    const sessionsIndex = ACTIVITY_PANEL_TABS.findIndex((tab) => tab.id === 'sessions')
    const terminalIndex = ACTIVITY_PANEL_TABS.findIndex((tab) => tab.id === 'terminal')
    const terminalTab = ACTIVITY_PANEL_TABS[terminalIndex]

    expect(terminalIndex).toBe(sessionsIndex + 1)
    expect(terminalTab?.label).toBe('Terminal')

    const { container } = render(<>{terminalTab?.icon}</>)
    expect(container.querySelector('polyline')).toHaveAttribute('points', '4 17 10 11 4 5')
    expect(container.querySelector('line')).toHaveAttribute('x1', '12')
    expect(container.querySelector('line')).toHaveAttribute('y1', '19')
    expect(container.querySelector('line')).toHaveAttribute('x2', '20')
    expect(container.querySelector('line')).toHaveAttribute('y2', '19')
  })

  it('renders no panel content for the terminal tab (it lives in the bottom dock)', () => {
    render(
      <ActivityPanel
        mode="split"
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="terminal"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        sessions={[]}
        isMobile={false}
      />,
    )

    expect(screen.queryByText('Terminal Tab')).not.toBeInTheDocument()
    expect(screen.queryByRole('log')).not.toBeInTheDocument()
  })

  it('returns null in chat-only mode', () => {
    const { container } = render(
      <ActivityPanel
        mode="chat"
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={false}
      />,
    )

    expect(container.innerHTML).toBe('')
  })

  it('fills the available width in panel-only mode without a resize handle', () => {
    const { container } = render(
      <ActivityPanel
        mode="panel"
        onToggleChat={vi.fn()}
        panelWidth={480}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={false}
      />,
    )

    const panel = container.querySelector('.activity-panel') as HTMLElement
    expect(screen.queryByTestId('resize-handle')).toBeNull()
    expect(panel.style.width).toBe('100%')
    expect(panel.style.minWidth).toBe('320px')
    expect(panel.style.flex).toBe('1 1 auto')
  })

  it('keeps resize handle sizing props in split mode', () => {
    render(
      <ActivityPanel
        mode="split"
        onToggleChat={vi.fn()}
        panelWidth={400}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={false}
      />,
    )

    const handle = screen.getByTestId('resize-handle')
    expect(handle).toHaveAttribute('data-panel-width', '400')
    expect(handle).toHaveAttribute('data-min-width', '320')
    expect(handle).toHaveAttribute('data-max-width', '704')
  })

  it('uses a dropdown menu instead of the mobile icon strip', async () => {
    const onTabChange = vi.fn()

    render(
      <ActivityPanel
        mode={"split"}
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={onTabChange}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={true}
      />,
    )

    expect(screen.queryByRole('tablist')).toBeNull()
    expect(screen.getByRole('button', { name: /sessions/i })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /sessions/i }))

    expect(document.querySelector('.activity-panel-mobile-menu')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /pipelines/i }))

    expect(onTabChange).toHaveBeenCalledWith('pipelines')
    expect(document.querySelector('.activity-panel-mobile-menu')).toBeNull()
  })

  it('uses the same dropdown selector in the pinned desktop panel', async () => {
    const onTabChange = vi.fn()

    render(
      <ActivityPanel
        mode={"split"}
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={onTabChange}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={false}
      />,
    )

    expect(screen.queryByRole('tablist')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /sessions/i }))
    await userEvent.click(screen.getByRole('button', { name: /tasks/i }))

    expect(onTabChange).toHaveBeenCalledWith('tasks')
  })

  it('orders dropdown menu labels alphabetically while preserving the selected trigger', async () => {
    render(
      <ActivityPanel
        mode={"split"}
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={false}
      />,
    )

    expect(screen.getByRole('button', { name: /sessions/i })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /sessions/i }))

    const disclosure = document.querySelector('.activity-panel-mobile-menu')
    expect(disclosure).not.toBeNull()
    const disclosureQueries = within(disclosure as HTMLElement)
    expect(
      disclosureQueries.getAllByRole('button').map((item) => item.textContent),
    ).toEqual(ACTIVITY_PANEL_DROPDOWN_TABS.map((tab) => tab.label))
    expect(disclosureQueries.getByRole('button', { name: /sessions/i })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('lays the dropdown menu out as a two-column grid with left-aligned items', async () => {
    render(
      <ActivityPanel
        mode={"split"}
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        isMobile={true}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: /sessions/i }))

    const disclosure = document.querySelector('.activity-panel-mobile-menu')
    expect(disclosure).not.toBeNull()
    // Two fixed columns keep all 16 tabs (Terminal included) reachable on
    // phone widths; the scroll guard covers short viewports.
    expect(disclosure).toHaveClass('grid', 'grid-cols-2', 'overflow-y-auto')
    const items = within(disclosure as HTMLElement).getAllByRole('button')
    expect(
      within(disclosure as HTMLElement).getByRole('button', { name: /terminal/i }),
    ).toBeInTheDocument()
    for (const item of items) {
      expect(item).toHaveClass('justify-start')
      expect(item.className).not.toContain('justify-center')
    }
  })

  it('clamps the desktop panel between the activity and chat 320px floors', () => {
    const previousWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      writable: true,
      value: 960,
    })

    try {
      const { container } = render(
        <ActivityPanel
          mode={"split"}
          onToggleChat={vi.fn()}
          panelWidth={900}
          onWidthChange={vi.fn()}
          activeTab="sessions"
          onTabChange={vi.fn()}
          plans={new Map()}
          activePlan={null}
          onOpenPlan={vi.fn()}
          onSetPlanVersion={vi.fn()}
          isMobile={false}
        />,
      )

      const panel = container.querySelector('.activity-panel') as HTMLElement
      expect(panel.style.width).toBe('640px')
      expect(panel.style.minWidth).toBe('320px')
      expect(panel.style.maxWidth).toBe('calc(100vw - 320px)')

      const handle = screen.getByTestId('resize-handle')
      expect(handle).toHaveAttribute('data-panel-width', '640')
      expect(handle).toHaveAttribute('data-min-width', '320')
      expect(handle).toHaveAttribute('data-max-width', '640')
    } finally {
      Object.defineProperty(window, 'innerWidth', {
        configurable: true,
        writable: true,
        value: previousWidth,
      })
    }
  })

  it('renders file diffs under the Changes tab', () => {
    render(
      <ActivityPanel
        mode={"split"}
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="changes"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        changedFiles={[{ path: 'src/example.ts', status: 'M' }]}
        isMobile={false}
      />,
    )

    expect(screen.getByText('Changes Tab')).toBeInTheDocument()
  })

  it('renders MCP under the MCP tab', () => {
    render(
      <ActivityPanel
        mode={"split"}
        onToggleChat={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="mcp"
        onTabChange={vi.fn()}
        plans={new Map()}
        activePlan={null}
        onOpenPlan={vi.fn()}
        onSetPlanVersion={vi.fn()}
        mcp={{} as never}
        isMobile={false}
      />,
    )

    expect(screen.getByText('MCP Tab')).toBeInTheDocument()
  })
})
