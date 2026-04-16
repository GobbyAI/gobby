import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import App from '../App'
import { useChat } from '../hooks/useChat'
import { useSessions } from '../hooks/useSessions'

// Mock all hooks used by App.tsx
vi.mock('../hooks/useAuth', () => ({
  useAuth: vi.fn(() => ({
    authRequired: false,
    authenticated: true,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  })),
}))

const mockSendProjectChange = vi.fn()
const mockSetProjectIdRef = vi.fn()

function makeChatHookState() {
  return {
    messages: [],
    conversationId: 'conv-123',
    conversationSwitchKey: 0,
    sessionRef: null,
    dbSessionId: null,
    currentBranch: null,
    worktreePath: null,
    isConnected: true,
    isStreaming: false,
    isThinking: false,
    isLoadingMessages: false,
    contextUsage: { totalInputTokens: 0, outputTokens: 0, contextWindow: null },
    sendMessage: vi.fn(),
    sendMode: vi.fn(),
    sendProjectChange: mockSendProjectChange,
    setProjectIdRef: mockSetProjectIdRef,
    sendWorktreeChange: vi.fn(),
    stopStreaming: vi.fn(),
    clearHistory: vi.fn(),
    deleteConversation: vi.fn(),
    respondToQuestion: vi.fn(),
    respondToApproval: vi.fn(),
    planPendingApproval: false,
    approvePlan: vi.fn(),
    requestPlanChanges: vi.fn(),
    switchConversation: vi.fn(),
    startNewChat: vi.fn(),
    continueSessionInChat: vi.fn(),
    setOnModeChanged: vi.fn(),
    setOnPlanReady: vi.fn(),
    addSystemMessage: vi.fn(),
    viewSession: vi.fn(),
    clearViewingSession: vi.fn(),
    viewingSessionId: null,
    viewingSessionMeta: null,
    attachToViewed: vi.fn(),
    detachFromSession: vi.fn(),
    attachedSessionId: null,
    attachedSessionMeta: null,
    wsRef: { current: null },
    handleVoiceMessageRef: { current: null },
    handleBinaryMessageRef: { current: null },
    canvasSurfaces: new Map(),
    canvasPanel: null,
    onCanvasInteraction: vi.fn(),
    setOnChatDeleted: vi.fn(),
    activeAgent: 'default',
    sendAgentChange: vi.fn(),
  }
}

function makeSessionsHookState() {
  return {
    projects: [{ id: 'p1', name: 'Personal' }],
    sessions: [],
    isLoading: false,
    refresh: vi.fn(),
    setFilters: vi.fn(),
    filteredSessions: [],
  }
}

vi.mock('../hooks/useChat', () => ({
  useChat: vi.fn(() => makeChatHookState()),
}))

vi.mock('../hooks/useVoice', () => ({
  useVoice: vi.fn(() => ({
    handleVoiceMessage: vi.fn(),
    handleBinaryMessage: vi.fn(),
  })),
}))

vi.mock('../hooks/useSettings', () => ({
  useSettings: vi.fn(() => ({
    settings: {
      fontSize: 16,
      model: 'gpt-4',
      chatMode: 'plan',
      theme: 'dark',
      defaultChatMode: 'plan',
      sttEnabled: false,
      ttsEnabled: false,
      voiceInputMode: 'ptt',
    },
    updateFontSize: vi.fn(),
    updateModel: vi.fn(),
    updateChatMode: vi.fn(),
    updateTheme: vi.fn(),
    updateDefaultChatMode: vi.fn(),
    updateSttEnabled: vi.fn(),
    updateTtsEnabled: vi.fn(),
    updateVoiceInputMode: vi.fn(),
    resetSettings: vi.fn(),
  })),
}))

vi.mock('../hooks/useTerminal', () => ({
  useTerminal: vi.fn(() => ({ agents: [], refreshAgents: vi.fn() })),
}))

vi.mock('../hooks/useTmuxSessions', () => ({
  useTmuxSessions: vi.fn(() => ({})),
}))

vi.mock('../hooks/useMcp', () => ({
  useMcp: vi.fn(() => ({ servers: [], toolsByServer: {}, fetchToolSchema: vi.fn() })),
}))

vi.mock('../hooks/useSkills', () => ({
  useSkills: vi.fn(() => ({ skills: [] })),
}))

vi.mock('../hooks/useColonAutocomplete', () => ({
  useColonAutocomplete: vi.fn(() => ({
    paletteItems: [],
    filterInput: vi.fn(),
    parseColonCommand: vi.fn(),
    resolveInjectContext: vi.fn(),
  })),
}))

vi.mock('../hooks/useSessions', () => ({
  useSessions: vi.fn(() => makeSessionsHookState()),
}))

vi.mock('../hooks/useAgentDefinitions', () => ({
  useAgentDefinitions: vi.fn(() => ({})),
}))

// Mock CSS imports
vi.mock('./App.css', () => ({}))

// Mock lazy components
vi.mock('./components/dashboard/DashboardPage', () => ({ DashboardPage: () => <div>Dashboard</div> }))
vi.mock('./components/chat/ChatPage', () => ({ ChatPage: () => <div>Chat</div> }))
vi.mock('./components/sessions/SessionsPage', () => ({ SessionsPage: () => <div>Sessions</div> }))

// Mock window.matchMedia
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation(query => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(), // Deprecated
    removeListener: vi.fn(), // Deprecated
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

describe('App wiring', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const storage = new Map<string, string>()
    Object.defineProperty(globalThis, 'localStorage', {
      value: {
        getItem: vi.fn((key: string) => storage.get(key) ?? null),
        setItem: vi.fn((key: string, value: string) => {
          storage.set(key, value)
        }),
        removeItem: vi.fn((key: string) => {
          storage.delete(key)
        }),
        clear: vi.fn(() => {
          storage.clear()
        }),
      },
      configurable: true,
      writable: true,
    })
    // Mock fetch for UI settings
    globalThis.fetch = vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ selectedProjectId: 'p1' })
    })) as any
  })

  it('calls sendProjectChange and setProjectIdRef when effectiveProjectId is set', async () => {
    await act(async () => {
      render(<App />)
    })

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalled()
      expect(mockSendProjectChange).toHaveBeenCalled()
    })
  })

  it('preserves an explicit fresh local conversation instead of restoring the most recent session', async () => {
    const switchConversation = vi.fn()
    const startNewChat = vi.fn()

    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      conversationId: 'local-new-chat',
      switchConversation,
      startNewChat,
    } as any)
    vi.mocked(useSessions).mockReturnValue({
      ...makeSessionsHookState(),
      filteredSessions: [
        {
          id: 'db-session-1',
          external_id: 'server-session-1',
          project_id: 'p1',
          session_type: 'web_chat',
        },
      ],
    } as any)

    localStorage.setItem('gobby-conversation-id', 'local-new-chat')

    await act(async () => {
      render(<App />)
    })

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalled()
    })

    expect(switchConversation).not.toHaveBeenCalled()
    expect(startNewChat).not.toHaveBeenCalled()
  })
})
