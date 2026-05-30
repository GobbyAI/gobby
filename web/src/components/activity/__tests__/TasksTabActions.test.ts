import { afterEach, describe, expect, it, vi } from 'vitest'

import type { GobbyTask } from '../../../hooks/useTasks'
import {
  claimTaskForSession,
  extractResponseErrorMessage,
  taskActionRef,
} from '../TasksTabActions'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('TasksTabActions', () => {
  it('extracts JSON detail, JSON message, and text response errors', () => {
    expect(extractResponseErrorMessage('{"detail":"denied"}', 'Bad', 'Fallback', 400)).toBe(
      'denied',
    )
    expect(extractResponseErrorMessage('{"message":"missing"}', 'Bad', 'Fallback', 404)).toBe(
      'missing',
    )
    expect(extractResponseErrorMessage('plain failure', 'Bad', 'Fallback', 500)).toBe(
      'plain failure',
    )
    expect(extractResponseErrorMessage('', '', 'Fallback', 503)).toBe('Fallback (503)')
  })

  it('uses response body details for task action failures', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'claim denied' }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(claimTaskForSession('', 'task-1', 'sess-1')).rejects.toThrow('claim denied')
  })

  it('uses the trimmed task ref when present', () => {
    const task = { id: 'task-id', ref: '  #15356  ', seq_num: 15356 } as GobbyTask

    expect(taskActionRef(task)).toBe('#15356')
  })

  it('falls back to the task id instead of reconstructing refs from seq_num', () => {
    const task = { id: 'task-id', ref: '  ', seq_num: 15356 } as GobbyTask

    expect(taskActionRef(task)).toBe('task-id')
  })
})
