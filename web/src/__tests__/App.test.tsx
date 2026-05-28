import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act, waitFor, screen, fireEvent } from "@testing-library/react";

import App from "../App";
import { useChat } from "../hooks/useChat";
import { useSessionCatalog } from "../hooks/useSessionCatalog";

const chatPagePropsSpy = vi.hoisted(() => vi.fn());
const sidebarPropsSpy = vi.hoisted(() => vi.fn());

vi.mock("../hooks/useAuth", () => ({
  useAuth: vi.fn(() => ({
    authRequired: false,
    authenticated: true,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  })),
}));

const mockSendProjectChange = vi.fn();
const mockSetProjectIdRef = vi.fn();

function makeChatHookState() {
  return {
    messages: [],
    conversationId: "conv-123",
    conversationSwitchKey: 0,
    sessionRef: null,
    sessionTitle: null,
    dbSessionId: null,
    currentBranch: null,
    worktreePath: null,
    isConnected: true,
    isReconnecting: false,
    isStreaming: false,
    isThinking: false,
    isLoadingMessages: false,
    transportError: null,
    contextUsage: { totalInputTokens: 0, outputTokens: 0, contextWindow: null },
    sendMessage: vi.fn(),
    sendMode: vi.fn(),
    sendProjectChange: mockSendProjectChange,
    projectIdRef: { current: null },
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
    switchProvider: vi.fn(),
    continueSessionInChat: vi.fn(),
    setOnModeChanged: vi.fn(),
    setOnPlanReady: vi.fn(),
    addSystemMessage: vi.fn(),
    viewSession: vi.fn(),
    clearViewingSession: vi.fn(),
    mainSessionMeta: null,
    observeSession: vi.fn(),
    viewingSessionId: null,
    viewingSessionMeta: null,
    isContinuingSession: false,
    attachToViewed: vi.fn(),
    detachFromSession: vi.fn(),
    attachedSessionId: null,
    attachedSessionMeta: null,
    sessionInteractionMode: "none",
    proxyDeliveryNotice: null,
    wsRef: { current: null },
    handleVoiceMessageRef: { current: null },
    handleBinaryMessageRef: { current: null },
    canvasSurfaces: new Map(),
    canvasPanel: null,
    onCanvasInteraction: vi.fn(),
    setOnChatDeleted: vi.fn(),
    clearTransportError: vi.fn(),
    activeAgent: "default",
    sendAgentChange: vi.fn(),
    selectedProvider: "claude",
    setSelectedProvider: vi.fn(),
  };
}

function makeProjectsHookState() {
  return {
    allProjects: [
      {
        id: "personal",
        name: "_personal",
        display_name: "_personal",
        repo_path: null,
        github_url: null,
        github_repo: null,
        linear_team_id: null,
        linear_project_id: null,
        approval_rules: [],
        validation_detection: null,
        created_at: "2026-04-01T00:00:00Z",
        updated_at: "2026-04-01T00:00:00Z",
        session_count: 0,
        open_task_count: 0,
        last_activity_at: null,
      },
      {
        id: "repo-project",
        name: "gobby",
        display_name: "gobby",
        repo_path: "/tmp/gobby",
        github_url: null,
        github_repo: null,
        linear_team_id: null,
        linear_project_id: null,
        approval_rules: [],
        validation_detection: null,
        created_at: "2026-04-01T00:00:00Z",
        updated_at: "2026-04-01T00:00:00Z",
        session_count: 0,
        open_task_count: 0,
        last_activity_at: null,
      },
      {
        id: "hidden",
        name: "_orphaned",
        display_name: "_orphaned",
        repo_path: null,
        github_url: null,
        github_repo: null,
        linear_team_id: null,
        linear_project_id: null,
        approval_rules: [],
        validation_detection: null,
        created_at: "2026-04-01T00:00:00Z",
        updated_at: "2026-04-01T00:00:00Z",
        session_count: 0,
        open_task_count: 0,
        last_activity_at: null,
      },
    ],
  };
}

function makeSessionCatalogState() {
  return {
    sessions: [],
    isLoading: false,
    isLoadingMore: false,
    error: null,
    refresh: vi.fn(),
    loadMore: vi.fn(),
    hasMore: false,
    removeSession: vi.fn(),
    markSessionDeleting: vi.fn(),
    confirmSessionDeleted: vi.fn(),
    restoreSession: vi.fn(),
    deletingIds: new Set<string>(),
    renameSession: vi.fn(),
  };
}

vi.mock("../hooks/useChat", () => ({
  useChat: vi.fn(() => makeChatHookState()),
}));

vi.mock("../hooks/useVoice", () => ({
  useVoice: vi.fn(() => ({
    voiceAvailable: false,
    voiceReady: false,
    voiceLoading: false,
    isListening: false,
    isSpeechDetected: false,
    isRecording: false,
    isTranscribing: false,
    isSpeaking: false,
    voiceError: null,
    handleVoiceMessage: vi.fn(),
    handleBinaryMessage: vi.fn(),
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    cancelRecording: vi.fn(),
    stopTTS: vi.fn(),
  })),
}));

vi.mock("../hooks/useSettings", () => ({
  useSettings: vi.fn(() => ({
    settings: {
      fontSize: 16,
      model: "gpt-4",
      chatMode: "plan",
      theme: "dark",
      defaultChatMode: "plan",
      postPlanChatMode: "plan",
      sttEnabled: false,
      ttsEnabled: false,
      voiceInputMode: "ptt",
    },
    updateFontSize: vi.fn(),
    updateModel: vi.fn(),
    updateChatMode: vi.fn(),
    updateTheme: vi.fn(),
    updateDefaultChatMode: vi.fn(),
    updatePostPlanChatMode: vi.fn(),
    updateSttEnabled: vi.fn(),
    updateTtsEnabled: vi.fn(),
    updateVoiceInputMode: vi.fn(),
    resetSettings: vi.fn(),
  })),
}));

vi.mock("../hooks/useProjects", () => ({
  useProjects: vi.fn(() => makeProjectsHookState()),
}));

vi.mock("../hooks/useSessionCatalog", () => ({
  useSessionCatalog: vi.fn(() => makeSessionCatalogState()),
}));

vi.mock("../hooks/useMcp", () => ({
  useMcp: vi.fn(() => ({ servers: [], toolsByServer: {}, fetchToolSchema: vi.fn() })),
}));

vi.mock("../hooks/useSkills", () => ({
  useSkills: vi.fn(() => ({ skills: [] })),
}));

vi.mock("../hooks/useColonAutocomplete", () => ({
  useColonAutocomplete: vi.fn(() => ({
    paletteItems: [],
    filterInput: vi.fn(),
    parseColonCommand: vi.fn(),
    resolveInjectContext: vi.fn(),
  })),
}));

vi.mock("../hooks/useAgentDefinitions", () => ({
  useAgentDefinitions: vi.fn(() => ({
    definitions: [],
    globalDefs: [],
    projectDefs: [],
    showScopeToggle: false,
    hasGlobal: false,
    hasProject: false,
  })),
}));

vi.mock("../components/Sidebar", () => ({
  Sidebar: (props: unknown) => {
    sidebarPropsSpy(props);
    return null;
  },
}));

vi.mock("../components/ProjectSelector", () => ({
  ProjectSelector: () => <div data-testid="project-selector" />,
}));

vi.mock("../components/dashboard/DashboardPage", () => ({
  DashboardPage: () => <div>Dashboard</div>,
}));

vi.mock("../components/chat/ChatPage", () => ({
  ChatPage: (props: unknown) => {
    chatPagePropsSpy(props);
    return <div>Chat</div>;
  },
}));

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

describe("App wiring", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useChat).mockReturnValue(makeChatHookState() as never);
    vi.mocked(useSessionCatalog).mockReturnValue(makeSessionCatalogState() as never);
    window.location.hash = "";

    const storage = new Map<string, string>();
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: vi.fn((key: string) => storage.get(key) ?? null),
        setItem: vi.fn((key: string, value: string) => {
          storage.set(key, value);
        }),
        removeItem: vi.fn((key: string) => {
          storage.delete(key);
        }),
        clear: vi.fn(() => {
          storage.clear();
        }),
      },
      configurable: true,
      writable: true,
    });

    globalThis.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({}),
      }),
    ) as unknown as typeof fetch;
  });

  it("uses projects from useProjects to resolve the effective project", async () => {
    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalledWith("repo-project");
      expect(mockSendProjectChange).toHaveBeenCalledWith("repo-project");
    });
  });

  it("shows transport errors in the app toast", async () => {
    const clearTransportError = vi.fn();
    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      transportError: {
        id: 1,
        message: "Transport message handling failed; reconnecting",
      },
      clearTransportError,
    } as never);

    await act(async () => {
      render(<App />);
    });

    const toast = await screen.findByRole("button", {
      name: "Dismiss notification: Transport message handling failed; reconnecting",
    });
    expect(toast).toHaveTextContent(
      "Transport message handling failed; reconnecting",
    );

    fireEvent.click(toast);

    expect(clearTransportError).toHaveBeenCalledOnce();
  });

  it("hydrates and persists selected provider via UI settings", async () => {
    const setSelectedProvider = vi.fn();
    const chatState = {
      ...makeChatHookState(),
      selectedProvider: "claude",
      setSelectedProvider,
    };
    vi.mocked(useChat).mockReturnValue(chatState as never);
    const fetchMock = vi.fn((_url: string | URL | Request, init?: RequestInit) => {
      if (!init) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ selectedProvider: "codex" }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });
    }) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const { rerender } = render(<App />);

    await waitFor(() => {
      expect(setSelectedProvider).toHaveBeenCalledWith("codex");
    });

    vi.clearAllMocks();
    vi.mocked(useChat).mockReturnValue({
      ...chatState,
      selectedProvider: "gemini",
    } as never);
    rerender(<App />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/config/ui-settings",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ selectedProvider: "gemini" }),
        }),
      );
    });
  });

  it("preserves an explicit fresh draft instead of restoring the most recent session", async () => {
    const switchConversation = vi.fn();
    const startNewChat = vi.fn();

    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      conversationId: "local-new-chat",
      switchConversation,
      startNewChat,
    } as never);

    vi.mocked(useSessionCatalog).mockReturnValue({
      ...makeSessionCatalogState(),
      sessions: [
        {
          id: "db-session-1",
          ref: "#101",
          external_id: "server-session-1",
          source: "claude",
          project_id: "repo-project",
          title: "Existing chat",
          status: "active",
          model: "sonnet",
          message_count: 1,
          created_at: "2026-04-01T00:00:00Z",
          updated_at: "2026-04-01T00:01:00Z",
          seq_num: 101,
          summary_markdown: null,
          digest_markdown: null,
          git_branch: "main",
          usage_input_tokens: 0,
          usage_output_tokens: 0,
          had_edits: false,
          agent_depth: 0,
          chat_mode: null,
          agent_run_id: null,
          parent_session_id: null,
          session_type: "web_chat",
          terminal_context: null,
        },
      ],
    } as never);

    localStorage.setItem("gobby-fresh-chat-draft", "1");

    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalled();
    });

    expect(switchConversation).not.toHaveBeenCalled();
    expect(startNewChat).not.toHaveBeenCalled();
  });

  it("restores a valid persisted DB session even when a fresh draft marker exists", async () => {
    const switchConversation = vi.fn();

    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      switchConversation,
    } as never);

    vi.mocked(useSessionCatalog).mockReturnValue({
      ...makeSessionCatalogState(),
      sessions: [
        {
          id: "db-session-1",
          ref: "#101",
          external_id: "server-session-1",
          source: "claude",
          project_id: "repo-project",
          title: "Existing chat",
          status: "active",
          model: "sonnet",
          message_count: 1,
          created_at: "2026-04-01T00:00:00Z",
          updated_at: "2026-04-01T00:01:00Z",
          seq_num: 101,
          summary_markdown: null,
          digest_markdown: null,
          git_branch: "main",
          usage_input_tokens: 0,
          usage_output_tokens: 0,
          had_edits: false,
          agent_depth: 0,
          chat_mode: null,
          agent_run_id: null,
          parent_session_id: null,
          session_type: "web_chat",
          terminal_context: null,
        },
      ],
    } as never);

    localStorage.setItem("gobby-db-session-id", "db-session-1");
    localStorage.setItem("gobby-conversation-id", "db-session-1");
    localStorage.setItem("gobby-fresh-chat-draft", "1");

    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(switchConversation).toHaveBeenCalledWith("db-session-1", {
        preserveViewing: false,
      });
    });
  });

  it("does not fallback to a recent web chat while a viewed session is active", async () => {
    const switchConversation = vi.fn();
    const startNewChat = vi.fn();

    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      viewingSessionId: "terminal-1",
      switchConversation,
      startNewChat,
    } as never);

    vi.mocked(useSessionCatalog).mockReturnValue({
      ...makeSessionCatalogState(),
      sessions: [
        {
          id: "db-session-1",
          ref: "#101",
          external_id: "server-session-1",
          source: "claude",
          project_id: "repo-project",
          title: "Existing chat",
          status: "active",
          model: "sonnet",
          message_count: 1,
          created_at: "2026-04-01T00:00:00Z",
          updated_at: "2026-04-01T00:01:00Z",
          seq_num: 101,
          summary_markdown: null,
          digest_markdown: null,
          git_branch: "main",
          usage_input_tokens: 0,
          usage_output_tokens: 0,
          had_edits: false,
          agent_depth: 0,
          chat_mode: null,
          agent_run_id: null,
          parent_session_id: null,
          session_type: "web_chat",
          terminal_context: null,
        },
      ],
    } as never);

    localStorage.setItem("gobby-fresh-chat-draft", "1");

    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalled();
    });

    expect(switchConversation).not.toHaveBeenCalled();
    expect(startNewChat).not.toHaveBeenCalled();
  });

  it("falls back to the most recent web chat when persisted main-chat storage points at a non-web-chat session", async () => {
    const switchConversation = vi.fn();

    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      switchConversation,
    } as never);

    vi.mocked(useSessionCatalog).mockReturnValue({
      ...makeSessionCatalogState(),
      sessions: [
        {
          id: "db-session-1",
          ref: "#101",
          external_id: "server-session-1",
          source: "claude",
          project_id: "repo-project",
          title: "Existing chat",
          status: "active",
          model: "sonnet",
          message_count: 1,
          created_at: "2026-04-01T00:00:00Z",
          updated_at: "2026-04-01T00:01:00Z",
          seq_num: 101,
          summary_markdown: null,
          digest_markdown: null,
          git_branch: "main",
          usage_input_tokens: 0,
          usage_output_tokens: 0,
          had_edits: false,
          agent_depth: 0,
          chat_mode: null,
          agent_run_id: null,
          parent_session_id: null,
          session_type: "web_chat",
          terminal_context: null,
        },
      ],
    } as never);

    localStorage.setItem("gobby-db-session-id", "terminal-session");
    localStorage.setItem("gobby-conversation-id", "terminal-session");

    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(switchConversation).toHaveBeenCalledWith("db-session-1");
    });
  });

  it("lands on chat for a stale #sessions hash", async () => {
    window.location.hash = "#sessions";

    await act(async () => {
      render(<App />);
    });

    expect(await screen.findByText("Chat")).toBeInTheDocument();
    await waitFor(() => {
      expect(window.location.hash).toBe("#chat");
    });
  });

  it("lands on chat for a stale #terminals hash", async () => {
    window.location.hash = "#terminals";

    await act(async () => {
      render(<App />);
    });

    expect(await screen.findByText("Chat")).toBeInTheDocument();
    await waitFor(() => {
      expect(window.location.hash).toBe("#chat");
    });
  });

  it("routes the legacy #mcp hash to Chat with the MCP activity tab requested", async () => {
    window.location.hash = "#mcp";

    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(chatPagePropsSpy).toHaveBeenCalled();
    });
    const props = chatPagePropsSpy.mock.calls[
      chatPagePropsSpy.mock.calls.length - 1
    ]?.[0] as {
      requestedActivityTab?: string | null;
      mcp?: unknown;
    };

    expect(props.requestedActivityTab).toBe("mcp");
    expect(props.mcp).toBeTruthy();
    await waitFor(() => {
      expect(window.location.hash).toBe("#chat");
    });
  });

  it("omits MCP from sidebar navigation", async () => {
    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(sidebarPropsSpy).toHaveBeenCalled();
    });
    const props = sidebarPropsSpy.mock.calls[
      sidebarPropsSpy.mock.calls.length - 1
    ]?.[0] as {
      items: Array<{ id: string; label: string }>;
    };

    expect(props.items.map((item) => item.id)).not.toContain("mcp");
    expect(props.items.map((item) => item.label)).not.toContain("MCP");
  });

  it("keeps parked web chat session catalog entries wired while viewing a terminal", async () => {
    const webSession = {
      id: "web-main",
      ref: "#101",
      external_id: "server-session-1",
      source: "claude",
      project_id: "repo-project",
      title: "Parked chat",
      status: "active",
      model: "sonnet",
      message_count: 1,
      created_at: "2026-04-01T00:00:00Z",
      updated_at: "2026-04-01T00:01:00Z",
      seq_num: 101,
      summary_markdown: null,
      digest_markdown: null,
      git_branch: "main",
      usage_input_tokens: 0,
      usage_output_tokens: 0,
      had_edits: false,
      agent_depth: 0,
      chat_mode: null,
      agent_run_id: null,
      parent_session_id: null,
      session_type: "web_chat",
      terminal_context: null,
    };
    const terminalSession = {
      ...webSession,
      id: "terminal-1",
      ref: "#202",
      external_id: "terminal-ext-1",
      source: "codex",
      title: "Observed terminal",
      seq_num: 202,
      session_type: "terminal",
      terminal_context: { tmux_pane: "%44" },
    };

    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      conversationId: "web-main",
      dbSessionId: "web-main",
      viewingSessionId: "terminal-1",
      viewingSessionMeta: {
        ref: "#202",
        source: "codex",
        title: "Observed terminal",
        status: "active",
        model: "gpt-5.4",
        externalId: "terminal-ext-1",
        sessionType: "terminal",
      },
    } as never);
    vi.mocked(useSessionCatalog).mockReturnValue({
      ...makeSessionCatalogState(),
      sessions: [webSession, terminalSession],
    } as never);

    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(chatPagePropsSpy).toHaveBeenCalled();
    });
    const props = chatPagePropsSpy.mock.calls[
      chatPagePropsSpy.mock.calls.length - 1
    ]?.[0] as {
      chat: { dbSessionId: string | null; viewingSessionId: string | null };
      conversations: { activeSessionId: string | null };
      allProjectSessions: Array<{ id: string }>;
      activitySessions: Array<{ id: string }>;
    };

    expect(props.chat.dbSessionId).toBe("web-main");
    expect(props.chat.viewingSessionId).toBe("terminal-1");
    expect(props.conversations.activeSessionId).toBe("web-main");
    expect(props.allProjectSessions.map((session) => session.id)).toContain("web-main");
    expect(props.activitySessions.map((session) => session.id)).toContain("web-main");
  });
});
