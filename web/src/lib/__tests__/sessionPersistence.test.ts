import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createMockLocalStorage, type MockLocalStorageInstance } from '../../test/mocks/localStorage'
import {
  CONVERSATION_ID_STORAGE_KEY,
  DB_SESSION_ID_STORAGE_KEY,
  FRESH_CHAT_DRAFT_STORAGE_KEY,
  REASONING_PREFERENCES_STORAGE_KEY,
  VIEWING_SESSION_ID_STORAGE_KEY,
  VIEWING_SESSION_MODE_STORAGE_KEY,
  clearFreshChatDraft,
  hasFreshChatDraft,
  loadPersistedConversationId,
  loadPersistedDbSessionId,
  loadPersistedViewingSessionId,
  loadPersistedViewingSessionMode,
  loadReasoningPreferences,
  markFreshChatDraft,
  savePersistedViewingSessionId,
  savePersistedViewingSessionMode,
} from '../sessionPersistence'

describe('sessionPersistence', () => {
  let storage: MockLocalStorageInstance

  beforeEach(() => {
    storage = createMockLocalStorage()
  })

  afterEach(() => {
    storage.restore()
  })

  it('loads the db session id as the preferred conversation id', () => {
    localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, 'legacy-conversation')
    localStorage.setItem(DB_SESSION_ID_STORAGE_KEY, 'db-session')

    expect(loadPersistedConversationId()).toBe('db-session')
  })

  it('falls back to the legacy conversation id', () => {
    localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, 'legacy-conversation')

    expect(loadPersistedConversationId()).toBe('legacy-conversation')
  })

  it('loads the persisted db session id directly', () => {
    localStorage.setItem(DB_SESSION_ID_STORAGE_KEY, 'db-session')

    expect(loadPersistedDbSessionId()).toBe('db-session')
  })

  it('loads, marks, and clears the fresh-chat draft flag', () => {
    expect(hasFreshChatDraft()).toBe(false)

    markFreshChatDraft()
    expect(localStorage.getItem(FRESH_CHAT_DRAFT_STORAGE_KEY)).toBe('1')
    expect(hasFreshChatDraft()).toBe(true)

    clearFreshChatDraft()
    expect(localStorage.getItem(FRESH_CHAT_DRAFT_STORAGE_KEY)).toBeNull()
    expect(hasFreshChatDraft()).toBe(false)
  })

  it('loads and saves the viewing session id', () => {
    expect(loadPersistedViewingSessionId()).toBeNull()

    savePersistedViewingSessionId('view-session')
    expect(localStorage.getItem(VIEWING_SESSION_ID_STORAGE_KEY)).toBe('view-session')
    expect(loadPersistedViewingSessionId()).toBe('view-session')
  })

  it.each([null, ''])('removes the viewing session id for %s', (id) => {
    localStorage.setItem(VIEWING_SESSION_ID_STORAGE_KEY, 'view-session')

    savePersistedViewingSessionId(id)

    expect(localStorage.getItem(VIEWING_SESSION_ID_STORAGE_KEY)).toBeNull()
  })

  it.each(['observe', 'proxy'] as const)('loads and saves viewing mode %s', (mode) => {
    savePersistedViewingSessionMode(mode)

    expect(localStorage.getItem(VIEWING_SESSION_MODE_STORAGE_KEY)).toBe(mode)
    expect(loadPersistedViewingSessionMode()).toBe(mode)
  })

  it('removes viewing mode when saving none', () => {
    localStorage.setItem(VIEWING_SESSION_MODE_STORAGE_KEY, 'observe')

    savePersistedViewingSessionMode('none')

    expect(localStorage.getItem(VIEWING_SESSION_MODE_STORAGE_KEY)).toBeNull()
    expect(loadPersistedViewingSessionMode()).toBe('none')
  })

  it.each(['', 'manual', 'invalid'])('falls back to none for malformed viewing mode %s', (mode) => {
    if (mode) {
      localStorage.setItem(VIEWING_SESSION_MODE_STORAGE_KEY, mode)
    }

    expect(loadPersistedViewingSessionMode()).toBe('none')
  })

  it('loads reasoning preferences from valid JSON objects', () => {
    localStorage.setItem(
      REASONING_PREFERENCES_STORAGE_KEY,
      JSON.stringify({ codex: 'high', claude: 'none' }),
    )

    expect(loadReasoningPreferences()).toEqual({ codex: 'high', claude: 'none' })
  })

  it.each(['not-json', 'null', '"high"', '42'])(
    'returns empty reasoning preferences for malformed storage %s',
    (raw) => {
      localStorage.setItem(REASONING_PREFERENCES_STORAGE_KEY, raw)

      expect(loadReasoningPreferences()).toEqual({})
    },
  )

  it.each([
    ['conversation id', loadPersistedConversationId, null],
    ['db session id', loadPersistedDbSessionId, null],
    ['fresh chat draft', hasFreshChatDraft, false],
    ['viewing session id', loadPersistedViewingSessionId, null],
    ['viewing session mode', loadPersistedViewingSessionMode, 'none'],
    ['reasoning preferences', loadReasoningPreferences, {}],
  ])('handles throwing getItem for %s', (_name, loader, expected) => {
    storage.getItem.mockImplementation(() => {
      throw new Error('storage unavailable')
    })

    expect(loader()).toEqual(expected)
  })

  it.each([
    ['fresh draft marker', markFreshChatDraft],
    ['viewing session id', () => savePersistedViewingSessionId('view-session')],
    ['viewing session mode', () => savePersistedViewingSessionMode('observe')],
  ])('ignores throwing setItem for %s', (_name, saver) => {
    storage.setItem.mockImplementation(() => {
      throw new Error('quota exceeded')
    })

    expect(() => saver()).not.toThrow()
  })

  it.each([
    ['fresh draft marker', clearFreshChatDraft],
    ['viewing session id', () => savePersistedViewingSessionId(null)],
    ['viewing session mode', () => savePersistedViewingSessionMode('none')],
  ])('ignores throwing removeItem for %s', (_name, remover) => {
    storage.removeItem.mockImplementation(() => {
      throw new Error('storage unavailable')
    })

    expect(() => remover()).not.toThrow()
  })
})
