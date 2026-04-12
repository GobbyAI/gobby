import { describe, expect, it } from 'vitest'
import { countTasksByBucket, getCanonicalTaskState, getTaskBucket, matchesTaskBucketFilter } from '../taskState'

describe('taskState helpers', () => {
  it('derives ready and in-progress buckets from canonical ownership', () => {
    expect(getTaskBucket({ status: 'open' })).toBe('ready')
    expect(
      getTaskBucket({
        status: 'open',
        state: { owner_session_id: 'sess-1', is_claimed: true },
      })
    ).toBe('in_progress')
  })

  it('derives review, merge-ready, blocked, and closed buckets from canonical state', () => {
    expect(getTaskBucket({ lifecycle_stage: 'needs_review' })).toBe('review')
    expect(getTaskBucket({ state: { is_merge_ready: true } })).toBe('merge_ready')
    expect(getTaskBucket({ state: { is_blocked: true, is_escalated: true } })).toBe('blocked')
    expect(getTaskBucket({ state: { is_closed: true } })).toBe('closed')
  })

  it('falls back to compat projection when canonical state is missing', () => {
    const state = getCanonicalTaskState({
      status: 'in_progress',
      assignee: 'legacy-owner',
    })

    expect(state.owner_session_id).toBe('legacy-owner')
    expect(state.is_claimed).toBe(true)
    expect(state.lifecycle_stage).toBe('in_progress')
  })

  it('counts tasks by canonical bucket', () => {
    expect(
      countTasksByBucket([
        { status: 'open' },
        { claimed_by_session_id: 'sess-1' },
        { lifecycle_stage: 'needs_review' },
        { state: { is_merge_ready: true } },
        { state: { is_escalated: true, is_blocked: true } },
        { closed_at: '2026-04-12T00:00:00Z' },
      ])
    ).toEqual({
      ready: 1,
      in_progress: 1,
      review: 1,
      merge_ready: 1,
      blocked: 1,
      closed: 1,
    })
  })

  it('supports legacy and canonical filter names', () => {
    const reviewTask = { lifecycle_stage: 'needs_review' }
    const mergeReadyTask = { state: { is_merge_ready: true } }
    const blockedTask = { state: { is_escalated: true, is_blocked: true } }

    expect(matchesTaskBucketFilter(reviewTask, 'review')).toBe(true)
    expect(matchesTaskBucketFilter(reviewTask, 'needs_review')).toBe(true)
    expect(matchesTaskBucketFilter(mergeReadyTask, 'in_review')).toBe(true)
    expect(matchesTaskBucketFilter(mergeReadyTask, 'review_approved')).toBe(true)
    expect(matchesTaskBucketFilter(blockedTask, 'blocked')).toBe(true)
    expect(matchesTaskBucketFilter(blockedTask, 'escalated')).toBe(true)
  })
})
