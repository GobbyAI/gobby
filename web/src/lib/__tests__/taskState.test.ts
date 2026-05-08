import { describe, expect, it } from 'vitest'
import {
  countTasksByState,
  getCanonicalTaskState,
  getTaskDisplayState,
  matchesTaskStateFilter,
} from '../taskState'

function stage(state: 'ready' | 'in_progress' | 'needs_review' | 'review_approved' | 'done') {
  return {
    name: 'build',
    display_name: 'Build',
    category: 'delivery',
    state,
    review_policy: 'required' as const,
    updated_at: '2026-05-02T00:00:00Z',
  }
}

describe('taskState helpers', () => {
  it('derives ready and in-progress states from ownership and stage rows', () => {
    expect(getTaskDisplayState({ stages: [stage('ready')] })).toBe('ready')
    expect(
      getTaskDisplayState({
        state: { owner_session_id: 'sess-1', is_claimed: true },
        stages: [stage('ready')],
      }),
    ).toBe('in_progress')
    expect(getTaskDisplayState({ stages: [stage('in_progress')] })).toBe('in_progress')
  })

  it('derives review, approved, blocked, and closed states from canonical data', () => {
    expect(getTaskDisplayState({ stages: [stage('needs_review')] })).toBe('needs_review')
    expect(getTaskDisplayState({ stages: [stage('review_approved')] })).toBe('review_approved')
    expect(getTaskDisplayState({ state: { is_blocked: true, is_escalated: true } })).toBe('blocked')
    expect(getTaskDisplayState({ state: { is_closed: true } })).toBe('closed')
  })

  it('uses canonical owner projection when stage rows are missing', () => {
    const state = getCanonicalTaskState({
      state: { owner_session_id: 'sess-1', is_claimed: true },
    })

    expect(state.owner_session_id).toBe('sess-1')
    expect(state.is_claimed).toBe(true)
    expect(getTaskDisplayState({ state: { owner_session_id: 'sess-1', is_claimed: true } })).toBe(
      'in_progress',
    )
  })

  it('counts tasks by display state', () => {
    expect(
      countTasksByState([
        { stages: [stage('ready')] },
        { claimed_by_session_id: 'sess-1' },
        { stages: [stage('needs_review')] },
        { stages: [stage('review_approved')] },
        { state: { is_escalated: true, is_blocked: true } },
        { closed_at: '2026-04-12T00:00:00Z' },
      ]),
    ).toEqual({
      ready: 1,
      in_progress: 1,
      needs_review: 1,
      review_approved: 1,
      blocked: 1,
      closed: 1,
    })
  })

  it('supports canonical and compatibility filter names', () => {
    const reviewTask = { stages: [stage('needs_review')] }
    const approvedTask = { stages: [stage('review_approved')] }
    const blockedTask = { state: { is_escalated: true, is_blocked: true } }

    expect(matchesTaskStateFilter(reviewTask, 'review')).toBe(true)
    expect(matchesTaskStateFilter(reviewTask, 'needs_review')).toBe(true)
    expect(matchesTaskStateFilter(approvedTask, 'in_review')).toBe(true)
    expect(matchesTaskStateFilter(approvedTask, 'review_approved')).toBe(true)
    expect(matchesTaskStateFilter(blockedTask, 'blocked')).toBe(true)
    expect(matchesTaskStateFilter(blockedTask, 'escalated')).toBe(true)
  })
})
