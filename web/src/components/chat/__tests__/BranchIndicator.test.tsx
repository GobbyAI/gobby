import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createMockFetch, type MockFetchInstance } from '../../../test/mocks/fetch'
import { BranchIndicator } from '../BranchIndicator'

const currentBranch = {
  name: 'current',
  is_current: true,
  is_remote: false,
  worktree_id: null,
}

const localBranch = {
  name: 'feature',
  is_current: false,
  is_remote: false,
  worktree_id: null,
}

const remoteBranch = {
  name: 'remote-only',
  is_current: false,
  is_remote: true,
  worktree_id: null,
}

let fetchMock: MockFetchInstance

function mockBranchPickerData(options?: { checkoutStatus?: number; checkoutBody?: unknown }) {
  fetchMock.mockJsonResponse(/\/api\/source-control\/status\?project_id=proj-1$/, {
    current_branch: 'current',
    repo_path: '/repo',
  })
  fetchMock.mockJsonResponse(/\/api\/source-control\/worktrees\?project_id=proj-1$/, {
    worktrees: [],
  })
  fetchMock.mockJsonResponse(/\/api\/source-control\/branches\?project_id=proj-1$/, {
    current_branch: 'current',
    branches: [currentBranch, localBranch, remoteBranch],
  })
  fetchMock.mockJsonResponse(
    /\/api\/source-control\/branches\/checkout\?project_id=proj-1$/,
    options?.checkoutBody ?? {
      success: true,
      current_branch: 'feature',
      repo_path: '/repo',
    },
    { status: options?.checkoutStatus ?? 200 }
  )
}

async function openBranchPicker(onWorktreeChange = vi.fn()) {
  const user = userEvent.setup()
  render(
    <BranchIndicator
      currentBranch="current"
      worktreePath="/repo"
      projectId="proj-1"
      onWorktreeChange={onWorktreeChange}
    />
  )

  await user.click(screen.getByRole('button', { name: /current/i }))
  await screen.findByRole('option', { name: /feature/i })
  return { user, onWorktreeChange }
}

describe('BranchIndicator', () => {
  beforeEach(() => {
    fetchMock = createMockFetch()
  })

  afterEach(() => {
    fetchMock.restore()
    vi.clearAllMocks()
  })

  it('checks out a local branch before switching chat to the main repo path', async () => {
    mockBranchPickerData()
    const { user, onWorktreeChange } = await openBranchPicker()

    await user.click(screen.getByRole('option', { name: /feature/i }))

    await waitFor(() => {
      expect(onWorktreeChange).toHaveBeenCalledWith('/repo')
    })

    const checkoutCall = fetchMock.fn.mock.calls.find(([input]) =>
      String(input).includes('/api/source-control/branches/checkout')
    )
    expect(checkoutCall?.[0]).toBe('/api/source-control/branches/checkout?project_id=proj-1')
    expect(checkoutCall?.[1]).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ branch_name: 'feature' }),
    })
  })

  it('does not render remote-only branches as switch targets', async () => {
    mockBranchPickerData()

    await openBranchPicker()

    expect(screen.getByRole('option', { name: /feature/i })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /remote-only/i })).not.toBeInTheDocument()
  })

  it('shows checkout errors without switching worktrees', async () => {
    mockBranchPickerData({
      checkoutStatus: 409,
      checkoutBody: { detail: 'dirty worktree' },
    })
    const { user, onWorktreeChange } = await openBranchPicker()

    await user.click(screen.getByRole('option', { name: /feature/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('dirty worktree')
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(onWorktreeChange).not.toHaveBeenCalled()
  })
})
