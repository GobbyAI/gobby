import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearProviderModelCache } from "../../../lib/providerModels";
import type { SessionObservationMeta } from "../../../types/chat";
import type { GobbySession } from "../../../types/sessions";
import {
  createChat,
  createConversations,
  createVoice,
} from "./chatPageTestSetup";
import { useChatPagePlans } from "../useChatPagePlans";
import { useChatPageCommandPalette } from "../useChatPageCommandPalette";
import { useChatPageProviderState } from "../useChatPageProviderState";
import { useChatPageSessionRouting } from "../useChatPageSessionRouting";
import { useChatPageVoiceStatus } from "../useChatPageVoiceStatus";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function makeSession(overrides: Partial<GobbySession>): GobbySession {
  return {
    id: "session-1",
    ref: "#1",
    external_id: "external-1",
    source: "codex",
    project_id: "proj-1",
    title: "Session",
    status: "active",
    model: "gpt-5.4",
    message_count: 1,
    created_at: "2026-05-04T12:00:00Z",
    updated_at: "2026-05-04T12:01:00Z",
    seq_num: 1,
    summary_markdown: null,
    handoff_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: false,
    sandbox_policy_hash: null,
    ...overrides,
  };
}

function terminalMeta(
  overrides: Partial<SessionObservationMeta> = {},
): SessionObservationMeta {
  return {
    ref: "#52",
    source: "codex",
    title: "Terminal",
    status: "active",
    model: "gpt-5.4",
    externalId: "term-52",
    sessionType: "terminal",
    ...overrides,
  };
}

function stubProviderFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/providers/models")) {
        return new Response(
          JSON.stringify({
            providers: [
              {
                provider: "claude",
                available: true,
                models: [
                  {
                    canonical_model: "sonnet",
                    display_name: "Sonnet",
                    aliases: [],
                    available: true,
                    hidden: false,
                    is_default: true,
                    context_length: { value: null, source: "unknown" },
                    max_output_tokens: { value: null, source: "unknown" },
                    latency_class: null,
                    reasoning: {
                      status: "known",
                      supported_efforts: ["low", "medium", "high"],
                      default_effort: "medium",
                    },
                    input_modalities: null,
                    supports_tools: null,
                    routes: {},
                    provenance: {},
                  },
                ],
                source: "live",
                refresh: { generation: 1, sources: [] },
              },
              {
                provider: "codex",
                available: true,
                models: [
                  {
                    canonical_model: "gpt-5.4",
                    display_name: "gpt-5.4",
                    aliases: [],
                    available: true,
                    hidden: false,
                    is_default: true,
                    context_length: { value: null, source: "unknown" },
                    max_output_tokens: { value: null, source: "unknown" },
                    latency_class: null,
                    reasoning: {
                      status: "known",
                      supported_efforts: ["auto", "high"],
                      default_effort: "auto",
                    },
                    input_modalities: null,
                    supports_tools: null,
                    routes: {},
                    provenance: {},
                  },
                ],
                source: "live",
                refresh: { generation: 1, sources: [] },
              },
            ],
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          providers: [
            { name: "claude", available: true },
            { name: "codex", available: true },
          ],
        }),
        { headers: { "Content-Type": "application/json" } },
      );
    }),
  );
}

describe("useChatPageCommandPalette", () => {
  function renderPaletteHook(
    confirm: (options: {
      title: string;
      description?: string;
      confirmLabel?: string;
      destructive?: boolean;
    }) => Promise<boolean>,
    onDeleteSession: (session: GobbySession) => void,
  ) {
    return renderHook(() =>
      useChatPageCommandPalette({
        allProjectSessions: [],
        activityPanelChatSessionId: null,
        conversations: {
          ...createConversations(),
          onDeleteSession,
        },
        confirm,
        handleSwapSession: vi.fn(),
        toggleFromChat: vi.fn(),
        toggleFromPanel: vi.fn(),
      }),
    );
  }

  it("does not delete a session when confirmation is declined", async () => {
    const confirm = vi.fn(async () => false);
    const onDeleteSession = vi.fn();
    const { result } = renderPaletteHook(confirm, onDeleteSession);
    const session = makeSession({ ref: "#42" });

    await result.current.handleCommandPaletteDeleteSession(session);

    expect(confirm).toHaveBeenCalledWith({
      title: "Delete session?",
      description: "This will permanently delete #42.",
      confirmLabel: "Delete",
      destructive: true,
    });
    expect(onDeleteSession).not.toHaveBeenCalled();
  });

  it("deletes a session after confirmation", async () => {
    const confirm = vi.fn(async () => true);
    const onDeleteSession = vi.fn();
    const { result } = renderPaletteHook(confirm, onDeleteSession);
    const session = makeSession({ ref: "#42" });

    await result.current.handleCommandPaletteDeleteSession(session);

    expect(onDeleteSession).toHaveBeenCalledWith(session);
  });
});

describe("useChatPageSessionRouting", () => {
  it("swaps to a web chat by selecting the target session and parking the current chat", () => {
    const showTab = vi.fn();
    const dismissOnMobile = vi.fn();
    const targetSession = makeSession({ id: "web-chat-2" });
    const conversations = {
      ...createConversations(),
      sessions: [targetSession],
      onSelectSession: vi.fn(),
    };
    const { result } = renderHook(() =>
      useChatPageSessionRouting({
        chat: createChat({ dbSessionId: "db-session-1" }),
        conversations,
        showTab,
        dismissOnMobile,
      }),
    );

    act(() => {
      result.current.handleSwapSession({
        sessionId: "web-chat-2",
        sessionType: "web_chat",
        agentRunId: null,
      });
    });

    expect(showTab).toHaveBeenCalledWith("sessions");
    expect(conversations.onSelectSession).toHaveBeenCalledWith(targetSession);
    expect(result.current.focusSessionId).toBe("db-session-1");
    expect(dismissOnMobile).toHaveBeenCalledTimes(1);
  });

  it("reports a missing web chat target without parking the current chat", () => {
    const showTab = vi.fn();
    const dismissOnMobile = vi.fn();
    const addSystemMessage = vi.fn();
    const conversations = {
      ...createConversations(),
      sessions: [],
      onSelectSession: vi.fn(),
    };
    const { result } = renderHook(() =>
      useChatPageSessionRouting({
        chat: createChat({
          dbSessionId: "db-session-1",
          addSystemMessage,
        }),
        conversations,
        showTab,
        dismissOnMobile,
      }),
    );

    act(() => {
      result.current.handleSwapSession({
        sessionId: "missing-web-chat",
        sessionType: "web_chat",
        agentRunId: null,
      });
    });

    expect(addSystemMessage).toHaveBeenCalledWith(
      "This chat session is no longer available.",
    );
    expect(showTab).not.toHaveBeenCalled();
    expect(conversations.onSelectSession).not.toHaveBeenCalled();
    expect(dismissOnMobile).not.toHaveBeenCalled();
    expect(result.current.focusSessionId).toBeNull();
  });

  it("swaps terminal targets into observe mode", () => {
    const showTab = vi.fn();
    const viewSession = vi.fn();
    const observeSession = vi.fn();
    const { result } = renderHook(() =>
      useChatPageSessionRouting({
        chat: createChat({
          dbSessionId: "db-session-1",
          viewSession,
          observeSession,
        }),
        conversations: createConversations(),
        showTab,
        dismissOnMobile: vi.fn(),
      }),
    );

    act(() => {
      result.current.handleSwapSession({
        sessionId: "terminal-2",
        sessionType: "terminal",
        agentRunId: null,
      });
    });

    expect(showTab).toHaveBeenCalledWith("sessions");
    expect(viewSession).toHaveBeenCalledWith("terminal-2", {
      forceRefresh: true,
    });
    expect(observeSession).toHaveBeenCalledWith("terminal-2", "observe");
  });

  it("resumes activity sessions with auto fallback context", async () => {
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const { result } = renderHook(() =>
      useChatPageSessionRouting({
        chat: createChat({
          dbSessionId: "db-session-1",
          continueSessionInChat,
        }),
        conversations: createConversations(),
        projectId: "proj-1",
        showTab: vi.fn(),
        dismissOnMobile: vi.fn(),
      }),
    );

    await act(async () => {
      await result.current.handleResumeSessionFromActivity("resume-target");
    });

    expect(continueSessionInChat).toHaveBeenCalledWith(
      "resume-target",
      "proj-1",
      { fallbackContext: "auto" },
    );
  });

  it("parks the current web chat when starting a new chat", () => {
    const showTab = vi.fn();
    const conversations = {
      ...createConversations(),
      onNewChat: vi.fn(),
    };
    const { result } = renderHook(() =>
      useChatPageSessionRouting({
        chat: createChat({ dbSessionId: "web-chat-4993" }),
        conversations,
        showTab,
        dismissOnMobile: vi.fn(),
      }),
    );

    act(() => {
      result.current.handleNewChat("default");
    });

    expect(showTab).toHaveBeenCalledWith("sessions");
    expect(result.current.focusSessionId).toBe("web-chat-4993");
    expect(conversations.onNewChat).toHaveBeenCalledWith("default");
  });
});

describe("useChatPageProviderState", () => {
  beforeEach(() => {
    localStorage.clear();
    clearProviderModelCache();
    stubProviderFetch();
  });

  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
    clearProviderModelCache();
  });

  it("keeps fresh chat provider controls enabled with activity watch state", async () => {
    localStorage.setItem("gobby-watching-session-id", "activity-session-1");

    const { result } = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          dbSessionId: null,
          attachedSessionId: null,
          attachedSessionMeta: null,
          viewingSessionId: null,
          viewingSessionMeta: null,
          isConnected: true,
          isContinuingSession: false,
          sessionInteractionMode: "none",
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        confirm: vi.fn(async () => true),
      }),
    );

    expect(result.current.providerPickerDisabledReason).toBeNull();
    expect(result.current.showChatInput).toBe(true);
    expect(result.current.chatInputDisabled).toBe(false);
    await waitFor(() => {
      expect(result.current.providerModelCatalog).toHaveLength(2);
    });
  });

  it("reports proxy and autonomous provider disabled reasons", async () => {
    const proxy = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          attachedSessionId: "terminal-1",
          sessionInteractionMode: "proxy",
          viewingSessionMeta: terminalMeta(),
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        confirm: vi.fn(async () => true),
      }),
    );
    expect(proxy.result.current.providerPickerDisabledReason).toBe(
      "Attached session owns provider, model, and reasoning",
    );

    const autonomous = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          sessionInteractionMode: "observe",
          viewingSessionMeta: terminalMeta({ agentRunId: "run-1" }),
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        confirm: vi.fn(async () => true),
      }),
    );
    expect(autonomous.result.current.providerPickerDisabledReason).toBe(
      "Observing autonomous session",
    );
    expect(autonomous.result.current.canAttachViewedSession).toBe(true);
    expect(autonomous.result.current.canControlViewedSession).toBe(false);
    expect(autonomous.result.current.showChatInput).toBe(false);

    const autonomousProxy = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          attachedSessionId: "terminal-auto",
          sessionInteractionMode: "proxy",
          viewingSessionMeta: terminalMeta({ agentRunId: "run-1" }),
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        confirm: vi.fn(async () => true),
      }),
    );
    expect(autonomousProxy.result.current.providerPickerDisabledReason).toBe(
      "Cannot change provider on a pipeline-managed session",
    );
    expect(autonomousProxy.result.current.canAttachViewedSession).toBe(true);
    expect(autonomousProxy.result.current.canControlViewedSession).toBe(false);
    expect(autonomousProxy.result.current.showChatInput).toBe(true);
    await waitFor(() => {
      expect(proxy.result.current.providerModelCatalog).toHaveLength(2);
      expect(autonomous.result.current.providerModelCatalog).toHaveLength(2);
      expect(autonomousProxy.result.current.providerModelCatalog).toHaveLength(
        2,
      );
    });
  });

  it("derives effective provider, model, and reasoning from the catalog", async () => {
    const { result } = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          provider: "claude",
          mainSessionMeta: {
            ref: "#77",
            source: "claude",
            title: "Claude Session",
            status: "active",
            model: null,
            externalId: "session-77",
            sessionType: "web_chat",
          },
        }),
        mainSessionMeta: {
          ref: "#77",
          source: "claude",
          title: "Claude Session",
          status: "active",
          model: null,
          externalId: "session-77",
          sessionType: "web_chat",
        },
        currentModel: "gpt-5.4",
        reasoningPreferences: {},
        confirm: vi.fn(async () => true),
      }),
    );

    await waitFor(() => {
      expect(result.current.providerModelCatalog).toHaveLength(2);
      expect(result.current.effectiveInputProvider).toBe("claude");
      expect(result.current.effectiveInputModel).toBe("sonnet");
      expect(result.current.effectiveInputReasoning).toBe("medium");
    });
  });

  it("resumes the viewed terminal with selected provider state", async () => {
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const onModelChange = vi.fn();
    const onReasoningPreferenceChange = vi.fn();
    const { result } = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          viewingSessionId: "terminal-2",
          viewingSessionMeta: terminalMeta({
            reasoningEffort: "high",
            chatMode: "bypass",
          }),
          sessionInteractionMode: "observe",
          continueSessionInChat,
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        onModelChange,
        onReasoningPreferenceChange,
        projectId: "proj-1",
        confirm: vi.fn(async () => true),
      }),
    );

    await waitFor(() => {
      expect(result.current.effectiveInputModel).toBe("gpt-5.4");
    });

    act(() => {
      result.current.handleResumeViewedSession();
    });

    expect(onModelChange).toHaveBeenCalledWith("gpt-5.4");
    expect(onReasoningPreferenceChange).toHaveBeenCalledWith(
      "codex",
      "gpt-5.4",
      "high",
    );
    expect(continueSessionInChat).toHaveBeenCalledWith("terminal-2", "proj-1", {
      provider: "codex",
      model: "gpt-5.4",
      reasoningEffort: "high",
      chatMode: "bypass",
      fallbackContext: "auto",
    });
  });

  it("confirms provider changes before resuming attachable viewed terminals", async () => {
    const confirm = vi.fn(async () => true);
    const continuation = deferred<string>();
    const continueSessionInChat = vi.fn(() => continuation.promise);
    const onProviderChange = vi.fn();
    const onModelChange = vi.fn();
    const onReasoningPreferenceChange = vi.fn();
    const { result } = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          viewingSessionId: "terminal-2",
          viewingSessionMeta: terminalMeta({ canProxyAttach: true }),
          sessionInteractionMode: "observe",
          continueSessionInChat,
          onProviderChange,
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        onModelChange,
        onReasoningPreferenceChange,
        projectId: "proj-1",
        confirm,
      }),
    );

    let selectionPromise!: Promise<void>;
    await act(async () => {
      selectionPromise = result.current.handleSwappedSessionProviderSelection(
        "claude",
        "sonnet",
        "medium",
      );
      await Promise.resolve();
    });

    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Change provider?", destructive: true }),
    );
    expect(continueSessionInChat).toHaveBeenCalledWith("terminal-2", "proj-1", {
      provider: "claude",
      model: "sonnet",
      reasoningEffort: "medium",
      chatMode: null,
      fallbackContext: "auto",
    });
    expect(onProviderChange).not.toHaveBeenCalled();
    expect(onModelChange).not.toHaveBeenCalled();
    expect(onReasoningPreferenceChange).not.toHaveBeenCalled();

    continuation.resolve("continued-session");
    await act(async () => selectionPromise);

    expect(onProviderChange).toHaveBeenCalledWith("claude");
    expect(onModelChange).toHaveBeenCalledWith("sonnet");
    expect(onReasoningPreferenceChange).toHaveBeenCalledWith(
      "claude",
      "sonnet",
      "medium",
    );
  });

  it("reports provider resume failures without committing preferences", async () => {
    const failure = new Error("daemon unavailable");
    const continueSessionInChat = vi.fn(async () => {
      throw failure;
    });
    const addSystemMessage = vi.fn();
    const onProviderChange = vi.fn();
    const onModelChange = vi.fn();
    const onReasoningPreferenceChange = vi.fn();
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const { result } = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          viewingSessionId: "terminal-2",
          viewingSessionMeta: terminalMeta(),
          sessionInteractionMode: "observe",
          continueSessionInChat,
          addSystemMessage,
          onProviderChange,
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        onModelChange,
        onReasoningPreferenceChange,
        projectId: "proj-1",
        confirm: vi.fn(async () => true),
      }),
    );

    await act(async () => {
      await result.current.handleSwappedSessionProviderSelection(
        "claude",
        "sonnet",
        "medium",
      );
    });

    expect(errorSpy).toHaveBeenCalledWith(
      "Failed to change provider for viewed session:",
      failure,
    );
    expect(addSystemMessage).toHaveBeenCalledWith(
      "Failed to change provider. The terminal session is still running.",
    );
    expect(onProviderChange).not.toHaveBeenCalled();
    expect(onModelChange).not.toHaveBeenCalled();
    expect(onReasoningPreferenceChange).not.toHaveBeenCalled();
  });

  it("revalidates the viewed session after provider confirmation", async () => {
    const confirmation = deferred<boolean>();
    const confirm = vi.fn(() => confirmation.promise);
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const onProviderChange = vi.fn();
    const onModelChange = vi.fn();
    const onReasoningPreferenceChange = vi.fn();
    let chat = createChat({
      viewingSessionId: "terminal-2",
      viewingSessionMeta: terminalMeta({ canProxyAttach: true }),
      sessionInteractionMode: "observe",
      continueSessionInChat,
      onProviderChange,
    });
    const { result, rerender } = renderHook(() =>
      useChatPageProviderState({
        chat,
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        onModelChange,
        onReasoningPreferenceChange,
        projectId: "proj-1",
        confirm,
      }),
    );

    let selectionPromise!: Promise<void>;
    act(() => {
      selectionPromise = result.current.handleSwappedSessionProviderSelection(
        "claude",
        "sonnet",
        "medium",
      );
    });
    chat = createChat({
      viewingSessionId: "terminal-3",
      viewingSessionMeta: terminalMeta({ chatMode: "bypass" }),
      sessionInteractionMode: "observe",
      continueSessionInChat,
      onProviderChange,
    });
    rerender();

    confirmation.resolve(true);
    await act(async () => selectionPromise);

    expect(continueSessionInChat).not.toHaveBeenCalled();
    expect(onProviderChange).not.toHaveBeenCalled();
    expect(onModelChange).not.toHaveBeenCalled();
    expect(onReasoningPreferenceChange).not.toHaveBeenCalled();
  });

  it("uses refreshed viewed-session metadata after provider confirmation", async () => {
    const confirmation = deferred<boolean>();
    const continueSessionInChat = vi.fn(async () => "continued-session");
    let chat = createChat({
      viewingSessionId: "terminal-2",
      viewingSessionMeta: terminalMeta({
        canProxyAttach: true,
        chatMode: "plan",
      }),
      sessionInteractionMode: "observe",
      continueSessionInChat,
    });
    const { result, rerender } = renderHook(() =>
      useChatPageProviderState({
        chat,
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        projectId: "proj-1",
        confirm: vi.fn(() => confirmation.promise),
      }),
    );

    let selectionPromise!: Promise<void>;
    act(() => {
      selectionPromise = result.current.handleSwappedSessionProviderSelection(
        "claude",
        "sonnet",
        "medium",
      );
    });
    chat = createChat({
      viewingSessionId: "terminal-2",
      viewingSessionMeta: terminalMeta({ chatMode: "bypass" }),
      sessionInteractionMode: "observe",
      continueSessionInChat,
    });
    rerender();

    confirmation.resolve(true);
    await act(async () => selectionPromise);

    expect(continueSessionInChat).toHaveBeenCalledWith("terminal-2", "proj-1", {
      provider: "claude",
      model: "sonnet",
      reasoningEffort: "medium",
      chatMode: "bypass",
      fallbackContext: "auto",
    });
  });

  it("keeps autonomous viewed terminals out of resume and provider handoff", async () => {
    const confirm = vi.fn(async () => true);
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const onProviderChange = vi.fn();
    const onModelChange = vi.fn();
    const { result } = renderHook(() =>
      useChatPageProviderState({
        chat: createChat({
          viewingSessionId: "terminal-auto",
          viewingSessionMeta: terminalMeta({
            agentRunId: "run-auto-1",
            canProxyAttach: true,
          }),
          sessionInteractionMode: "observe",
          continueSessionInChat,
          onProviderChange,
        }),
        mainSessionMeta: null,
        currentModel: "sonnet",
        reasoningPreferences: {},
        onModelChange,
        onReasoningPreferenceChange: vi.fn(),
        projectId: "proj-1",
        confirm,
      }),
    );

    act(() => {
      result.current.handleResumeViewedSession();
    });
    await act(async () => {
      await result.current.handleSwappedSessionProviderSelection(
        "claude",
        "sonnet",
        "medium",
      );
    });

    expect(result.current.canAttachViewedSession).toBe(true);
    expect(result.current.canControlViewedSession).toBe(false);
    expect(continueSessionInChat).not.toHaveBeenCalled();
    expect(onProviderChange).not.toHaveBeenCalled();
    expect(onModelChange).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
  });
});

describe("useChatPagePlans", () => {
  it("does not create duplicate plans for identical content", async () => {
    let onPlanReady: ((content: string | null) => void) | null = null;
    const showTab = vi.fn();
    const { result } = renderHook(() =>
      useChatPagePlans({
        chat: createChat({ setOnPlanReady: (fn) => void (onPlanReady = fn) }),
        showTab,
        dismissOnMobile: vi.fn(),
      }),
    );

    await waitFor(() => expect(onPlanReady).toBeTruthy());
    act(() => {
      onPlanReady?.("# Plan\n\nStep 1");
      onPlanReady?.("# Plan\n\nStep 1");
    });

    await waitFor(() => {
      expect(result.current.plans.size).toBe(1);
    });
    expect(showTab).toHaveBeenCalledWith("plans");
  });

  it("adds revisions to the existing plan", async () => {
    let onPlanReady: ((content: string | null) => void) | null = null;
    const { result } = renderHook(() =>
      useChatPagePlans({
        chat: createChat({ setOnPlanReady: (fn) => void (onPlanReady = fn) }),
        showTab: vi.fn(),
        dismissOnMobile: vi.fn(),
      }),
    );

    await waitFor(() => expect(onPlanReady).toBeTruthy());
    act(() => {
      onPlanReady?.("# Plan\n\nStep 1");
    });
    await waitFor(() => {
      expect(result.current.plans.size).toBe(1);
    });
    act(() => {
      onPlanReady?.("# Plan\n\nStep 2");
    });

    await waitFor(() => {
      expect(result.current.activePlan?.versions).toHaveLength(2);
      expect(result.current.activePlan?.versions[1].content).toBe(
        "# Plan\n\nStep 2",
      );
    });
  });

  it("clears the Plans-panel pending banner when chat resolves the plan from the status bar (#15663)", async () => {
    // Repro: approving from the agent status-bar strip calls chat.onApprovePlan
    // directly (never handleApprovePlan), so the only signal the Plans panel
    // gets is chat.planPendingApproval flipping false via the backend
    // mode_changed. The local pendingPlanId marker must clear with it,
    // or the panel stays stuck on "Awaiting approval".
    let onPlanReady: ((content: string | null) => void) | null = null;
    const setOnPlanReady = (fn: (content: string | null) => void) =>
      void (onPlanReady = fn);
    const { result, rerender } = renderHook(
      ({ pending }: { pending: boolean }) =>
        useChatPagePlans({
          chat: createChat({ planPendingApproval: pending, setOnPlanReady }),
          showTab: vi.fn(),
          dismissOnMobile: vi.fn(),
        }),
      { initialProps: { pending: true } },
    );

    await waitFor(() => expect(onPlanReady).toBeTruthy());
    act(() => {
      onPlanReady?.("# Plan\n\nStep 1");
    });
    await waitFor(() => expect(result.current.planPendingApproval).toBe(true));

    // Backend resolves the approval (status-bar path): chat.planPendingApproval
    // goes false without the hook's handleApprovePlan ever running.
    rerender({ pending: false });

    await waitFor(() => expect(result.current.planPendingApproval).toBe(false));
  });

  it("exposes showPlanRef for reopening the plan", async () => {
    let onPlanReady: ((content: string | null) => void) | null = null;
    const showTab = vi.fn();
    const showPlanRef = { current: null as (() => void) | null };
    renderHook(() =>
      useChatPagePlans({
        chat: createChat({ setOnPlanReady: (fn) => void (onPlanReady = fn) }),
        showTab,
        dismissOnMobile: vi.fn(),
        showPlanRef,
      }),
    );

    await waitFor(() => expect(onPlanReady).toBeTruthy());
    act(() => {
      onPlanReady?.("# Plan\n\nStep 1");
    });
    await waitFor(() => expect(showPlanRef.current).toBeTruthy());

    act(() => {
      showPlanRef.current?.();
    });

    expect(showTab).toHaveBeenCalledWith("plans");
  });
});

describe("useChatPageVoiceStatus", () => {
  it("hides the voice status row while voice is disabled", () => {
    const { result } = renderHook(() => useChatPageVoiceStatus(createVoice()));

    expect(result.current.showVoiceStatusBar).toBe(false);
    expect(result.current.voiceStatusWarming).toBe(false);
  });

  it("shows VAD voice status while enabled", () => {
    const { result } = renderHook(() =>
      useChatPageVoiceStatus(
        createVoice({
          sttEnabled: true,
          voiceInputMode: "vad",
          voiceAvailable: true,
          voiceReady: true,
        }),
      ),
    );

    expect(result.current.voiceInputMode).toBe("vad");
    expect(result.current.showVoiceStatusBar).toBe(true);
    expect(result.current.voiceStatusWarming).toBe(false);
  });

  it("shows PTT recording state", () => {
    const { result } = renderHook(() =>
      useChatPageVoiceStatus(
        createVoice({
          sttEnabled: true,
          voiceInputMode: "ptt",
          isRecording: true,
        }),
      ),
    );

    expect(result.current.voiceInputMode).toBe("ptt");
    expect(result.current.showVoiceStatusBar).toBe(true);
  });

  it("shows loading or warming state", () => {
    const loading = renderHook(() =>
      useChatPageVoiceStatus(createVoice({ voiceLoading: true })),
    );
    expect(loading.result.current.showVoiceStatusBar).toBe(true);
    expect(loading.result.current.voiceStatusWarming).toBe(true);

    const warming = renderHook(() =>
      useChatPageVoiceStatus(
        createVoice({
          ttsEnabled: true,
          voiceAvailable: true,
          voiceReady: false,
        }),
      ),
    );
    expect(warming.result.current.showVoiceStatusBar).toBe(true);
    expect(warming.result.current.voiceStatusWarming).toBe(true);
  });

  it("keeps errors visible", () => {
    const { result } = renderHook(() =>
      useChatPageVoiceStatus(createVoice({ voiceError: "Mic unavailable" })),
    );

    expect(result.current.showVoiceStatusBar).toBe(true);
    expect(result.current.voiceStatusWarming).toBe(false);
  });
});
