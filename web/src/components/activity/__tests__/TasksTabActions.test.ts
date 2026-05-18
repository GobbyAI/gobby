import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  claimTaskForSession,
  extractResponseErrorMessage,
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
})
