import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  act,
  waitFor,
  screen,
  fireEvent,
  within,
} from "@testing-library/react";

import App from "../App";
import { useChat } from "../hooks/useChat";
import { useSessionCatalog } from "../hooks/useSessionCatalog";
import { useAuth } from "../hooks/useAuth";
import { useIsMobile } from "../hooks/useIsMobile";
import { configurationClient } from "../api/config";

const chatPagePropsSpy = vi.hoisted(() => vi.fn());
const settingsOverlayRenderSpy = vi.hoisted(() => vi.fn());

vi.mock("../components/settings/SettingsOverlay", () => ({
  default: (props: Record<string, unknown>) => {
    settingsOverlayRenderSpy(props);
    return (
      <div
        role="dialog"
        aria-label="Settings overlay"
        data-testid="settings-overlay"
      />
    );
  },
}));

vi.mock("../hooks/useIsMobile", () => ({
  useIsMobile: vi.fn(() => false),
}));

vi.mock("../hooks/useAuth", () => ({
  useAuth: vi.fn(() => ({
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
      sttEnabled: false,
      ttsEnabled: false,
      voiceInputMode: "ptt",
      planPendingVariant: "info",
    },
    updateFontSize: vi.fn(),
    updateModel: vi.fn(),
    updateChatMode: vi.fn(),
    updateTheme: vi.fn(),
    updateDefaultChatMode: vi.fn(),
    updateSttEnabled: vi.fn(),
    updateTtsEnabled: vi.fn(),
    updateVoiceInputMode: vi.fn(),
    updatePlanPendingVariant: vi.fn(),
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
  useMcp: vi.fn(() => ({
    servers: [],
    toolsByServer: {},
    fetchToolSchema: vi.fn(),
  })),
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

vi.mock("../components/ProjectSelector", () => ({
  ProjectSelector: ({
    onProjectChange,
  }: {
    onProjectChange: (projectId: string) => void;
  }) => (
    <button type="button" onClick={() => onProjectChange("personal")}>
      Switch project
    </button>
  ),
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
    configurationClient.reset();
    vi.clearAllMocks();
    vi.mocked(useChat).mockReturnValue(makeChatHookState() as never);
    vi.mocked(useSessionCatalog).mockReturnValue(
      makeSessionCatalogState() as never,
    );
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

    globalThis.fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve(
            init?.method === "PATCH"
              ? {
                  committed: true,
                  revision: 1,
                  changed_keys: [],
                  apply_status: "applied",
                  pending_restart_keys: [],
                  failed_live_keys: {},
                }
              : {
                  revision: 0,
                  desired: { ui_settings: {} },
                  active: { ui_settings: {} },
                  secret_set: {},
                  pending_restart_keys: [],
                  failed_live_keys: {},
                },
          ),
      } as Response),
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

  it("opens SettingsOverlay exactly once and leaves the legacy settings branch unreachable", async () => {
    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(chatPagePropsSpy).toHaveBeenCalled();
    });
    const props = chatPagePropsSpy.mock.calls[
      chatPagePropsSpy.mock.calls.length - 1
    ]?.[0] as {
      paletteActions?: Array<{ id: string; onSelect: () => void }>;
    };

    act(() => {
      props.paletteActions
        ?.find((action) => action.id === "settings")
        ?.onSelect();
    });

    expect(await screen.findByTestId("settings-overlay")).toBeInTheDocument();
    expect(screen.getAllByTestId("settings-overlay")).toHaveLength(1);
    expect(settingsOverlayRenderSpy).toHaveBeenCalledOnce();
    expect(
      screen.queryByRole("button", { name: "Reset to Defaults" }),
    ).toBeNull();
  });

  it("clears a requested activity tab before switching projects", async () => {
    await act(async () => {
      render(<App />);
    });

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalledWith("repo-project");
    });

    const currentProps = () =>
      chatPagePropsSpy.mock.calls[
        chatPagePropsSpy.mock.calls.length - 1
      ]?.[0] as {
        projectId: string;
        requestedActivityTab: string | null;
        chat: { onPaletteSelect: (item: unknown) => void };
      };

    act(() => {
      currentProps().chat.onPaletteSelect({
        kind: "command",
        action: "open_mcp",
      });
    });
    expect(currentProps().requestedActivityTab).toBe("mcp");

    fireEvent.click(screen.getByRole("button", { name: "Switch project" }));

    await waitFor(() => {
      expect(currentProps().projectId).toBe("personal");
    });
    expect(currentProps().requestedActivityTab).toBeNull();
    expect(
      chatPagePropsSpy.mock.calls.some(([props]) => {
        const chatPageProps = props as {
          projectId: string;
          requestedActivityTab: string | null;
        };
        return (
          chatPageProps.projectId === "personal" &&
          chatPageProps.requestedActivityTab === "mcp"
        );
      }),
    ).toBe(false);
  });

  it("waits for persisted project hydration before syncing the project", async () => {
    let resolveSettings: ((response: Response) => void) | undefined;
    globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input === "/api/config/values" && !init) {
        return new Promise<Response>((resolve) => {
          resolveSettings = resolve;
        });
      }
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            committed: true,
            revision: 2,
            changed_keys: [],
            apply_status: "applied",
            pending_restart_keys: [],
            failed_live_keys: {},
          }),
      } as Response);
    }) as unknown as typeof fetch;

    render(<App />);

    expect(mockSetProjectIdRef).not.toHaveBeenCalled();
    expect(mockSendProjectChange).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(resolveSettings).toBeDefined();
    });
    await act(async () => {
      resolveSettings?.({
        ok: true,
        json: () =>
          Promise.resolve({
            revision: 1,
            desired: { ui_settings: { selectedProjectId: "personal" } },
            active: { ui_settings: { selectedProjectId: "personal" } },
            secret_set: {},
            pending_restart_keys: [],
            failed_live_keys: {},
          }),
      } as Response);
    });

    await waitFor(() => {
      expect(mockSetProjectIdRef).toHaveBeenCalledWith("personal");
      expect(mockSendProjectChange).toHaveBeenCalledWith("personal");
    });
    expect(mockSendProjectChange).not.toHaveBeenCalledWith("repo-project");
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
    const fetchMock = vi.fn(
      (_url: string | URL | Request, init?: RequestInit) => {
        if (!init) {
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({
                revision: 3,
                desired: { ui_settings: { selectedProvider: "codex" } },
                active: { ui_settings: { selectedProvider: "codex" } },
                secret_set: {},
                pending_restart_keys: [],
                failed_live_keys: {},
              }),
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              committed: true,
              revision: 4,
              changed_keys: ["ui_settings.selectedProvider"],
              apply_status: "applied",
              pending_restart_keys: [],
              failed_live_keys: {},
            }),
        } as Response);
      },
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const { rerender } = render(<App />);

    await waitFor(() => {
      expect(setSelectedProvider).toHaveBeenCalledWith("codex");
    });

    vi.clearAllMocks();
    vi.mocked(useChat).mockReturnValue({
      ...chatState,
      selectedProvider: "qwen",
    } as never);
    rerender(<App />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/config/values",
        expect.objectContaining({
          method: "PATCH",
        }),
      );
      const patchCall = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "PATCH",
      );
      expect(JSON.parse(String(patchCall?.[1]?.body))).toMatchObject({
        expected_revision: 3,
        values: { ui_settings: { selectedProvider: "qwen" } },
      });
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
          handoff_markdown: null,
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
          handoff_markdown: null,
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
          handoff_markdown: null,
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
          handoff_markdown: null,
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

  it("keeps chat rendered when back or forward navigation changes the hash", async () => {
    window.history.replaceState(null, "", "#sessions");

    await act(async () => {
      render(<App />);
    });

    expect(await screen.findByText("Chat")).toBeInTheDocument();
    expect(window.location.hash).toBe("#sessions");

    act(() => {
      window.history.replaceState(null, "", "#terminals");
      window.dispatchEvent(new PopStateEvent("popstate"));
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });

    expect(screen.getByText("Chat")).toBeInTheDocument();
    expect(window.location.hash).toBe("#terminals");
  });

  it("resets the backend-facing chat mode when New Chat is selected from the palette", async () => {
    const sendMode = vi.fn();
    const startNewChat = vi.fn();
    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      sendMode,
      startNewChat,
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
      paletteActions?: Array<{ id: string; onSelect: () => void }>;
    };

    const newChatAction = props.paletteActions?.find(
      (action) => action.id === "new-chat",
    );
    expect(newChatAction?.onSelect).toBeTypeOf("function");

    await act(async () => {
      newChatAction?.onSelect();
    });

    // handleStartNewChat must reset BOTH the UI radio and the backend-facing
    // currentModeRef (via sendMode). Before the fix it only reset the radio
    // (updateChatMode), so currentModeRef kept the prior session's mode and
    // seeded the next session through createWebChatSession() — the Plan-shown /
    // session-created-in-bypass desync (#15703). defaultChatMode is "plan".
    expect(startNewChat).toHaveBeenCalled();
    expect(sendMode).toHaveBeenCalledWith("plan");
  });

  it("restores the persisted mode when the active session arrives after the initial catalog load", async () => {
    const sendMode = vi.fn();
    vi.mocked(useChat).mockReturnValue({
      ...makeChatHookState(),
      dbSessionId: "db-session-1",
      sendMode,
    } as never);

    const { rerender } = render(<App />);

    expect(sendMode).not.toHaveBeenCalled();

    vi.mocked(useSessionCatalog).mockReturnValue({
      ...makeSessionCatalogState(),
      sessions: [
        {
          id: "db-session-1",
          project_id: "repo-project",
          session_type: "web_chat",
          chat_mode: "bypass",
        },
      ],
    } as never);

    rerender(<App />);

    await waitFor(() => {
      expect(sendMode).toHaveBeenCalledWith("bypass");
    });
  });

  it("shows a header Log out button that signs out when auth is enabled", async () => {
    const logout = vi.fn();
    vi.mocked(useAuth).mockReturnValue({
      authenticated: true,
      loading: false,
      login: vi.fn(),
      logout,
    } as never);
    try {
      await act(async () => {
        render(<App />);
      });

      const logoutButton = await screen.findByRole("button", {
        name: "Log out",
      });
      expect(logoutButton).toHaveClass(
        "w-8",
        "shrink-0",
        "pointer-coarse:before:min-w-11",
      );
      expect(logoutButton).not.toHaveClass(
        "app-logout-btn",
        "pointer-coarse:min-h-11",
        "pointer-coarse:min-w-11",
      );
      expect(
        document.querySelector('[aria-label="Toggle navigation menu"]'),
      ).toBeNull();

      await act(async () => {
        fireEvent.click(logoutButton);
      });
      expect(logout).toHaveBeenCalled();
    } finally {
      // Restore the suite default so later tests are unaffected;
      // clearAllMocks does not drop a mockReturnValue override.
      vi.mocked(useAuth).mockReturnValue({
        authenticated: true,
        loading: false,
        login: vi.fn(),
        logout: vi.fn(),
      } as never);
    }
  });

  it("collapses the header to a single settings entry at the mobile tier (#19185)", async () => {
    const logout = vi.fn();
    vi.mocked(useAuth).mockReturnValue({
      authenticated: true,
      loading: false,
      login: vi.fn(),
      logout,
    } as never);
    vi.mocked(useIsMobile).mockReturnValue(true);
    try {
      await act(async () => {
        render(<App />);
      });

      const header = screen.getByTestId("app-header");
      const settingsEntry = within(header).getByRole("button", {
        name: "Open settings",
      });
      expect(
        within(header).queryByRole("button", { name: "Log out" }),
      ).toBeNull();
      expect(
        within(header).queryByRole("button", {
          name: /Switch to (light|dark) theme/,
        }),
      ).toBeNull();

      // The settings surface owns theme and logout on mobile — the overlay
      // receives the header's logout handler.
      await act(async () => {
        fireEvent.click(settingsEntry);
      });
      expect(await screen.findByTestId("settings-overlay")).toBeInTheDocument();
      const overlayProps = settingsOverlayRenderSpy.mock.calls[
        settingsOverlayRenderSpy.mock.calls.length - 1
      ]?.[0] as {
        onLogout?: () => void;
      };
      expect(overlayProps.onLogout).toBe(logout);
    } finally {
      vi.mocked(useIsMobile).mockReturnValue(false);
      vi.mocked(useAuth).mockReturnValue({
        authenticated: true,
        loading: false,
        login: vi.fn(),
        logout: vi.fn(),
      } as never);
    }
  });

  it("keeps the three-button header cluster on the desktop tier (#19185)", async () => {
    vi.mocked(useAuth).mockReturnValue({
      authenticated: true,
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
    } as never);
    vi.mocked(useIsMobile).mockReturnValue(false);
    try {
      await act(async () => {
        render(<App />);
      });

      const header = screen.getByTestId("app-header");
      expect(
        within(header).getByRole("button", {
          name: /Switch to (light|dark) theme/,
        }),
      ).toBeInTheDocument();
      expect(
        within(header).getByRole("button", { name: "Open settings" }),
      ).toBeInTheDocument();
      expect(
        within(header).getByRole("button", { name: "Log out" }),
      ).toBeInTheDocument();
    } finally {
      vi.mocked(useAuth).mockReturnValue({
        authenticated: true,
        loading: false,
        login: vi.fn(),
        logout: vi.fn(),
      } as never);
    }
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
      handoff_markdown: null,
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
    expect(props.allProjectSessions.map((session) => session.id)).toContain(
      "web-main",
    );
    expect(props.activitySessions.map((session) => session.id)).toContain(
      "web-main",
    );
  });
});
