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
import { useChatPageArtifacts } from "../useChatPageArtifacts";
import { useChatPageProviderState } from "../useChatPageProviderState";
import { useChatPageSessionRouting } from "../useChatPageSessionRouting";
import { useChatPageVoiceStatus } from "../useChatPageVoiceStatus";

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
    digest_markdown: null,
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
                    value: "sonnet",
                    label: "Sonnet",
                    is_default: true,
                    reasoning: {
                      supported_efforts: ["low", "medium", "high"],
                      default_effort: "medium",
                    },
                  },
                ],
                source: "static",
              },
              {
                provider: "codex",
                available: true,
                models: [
                  {
                    value: "gpt-5.4",
                    label: "gpt-5.4",
                    is_default: true,
                    reasoning: {
                      supported_efforts: ["auto", "high"],
                      default_effort: "auto",
                    },
                  },
                ],
                source: "static",
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
    clearProviderModelCache();
    stubProviderFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearProviderModelCache();
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
    await waitFor(() => {
      expect(proxy.result.current.providerModelCatalog).toHaveLength(2);
      expect(autonomous.result.current.providerModelCatalog).toHaveLength(2);
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
    const continueSessionInChat = vi.fn(async () => "continued-session");
    const onProviderChange = vi.fn();
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
        onModelChange: vi.fn(),
        onReasoningPreferenceChange: vi.fn(),
        projectId: "proj-1",
        confirm,
      }),
    );

    await act(async () => {
      await result.current.handleSwappedSessionProviderSelection(
        "claude",
        "sonnet",
        "medium",
      );
    });

    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Change provider?", destructive: true }),
    );
    expect(onProviderChange).toHaveBeenCalledWith("claude");
    expect(continueSessionInChat).toHaveBeenCalledWith("terminal-2", "proj-1", {
      provider: "claude",
      model: "sonnet",
      reasoningEffort: "medium",
      chatMode: null,
      fallbackContext: "auto",
    });
  });
});

describe("useChatPageArtifacts", () => {
  it("does not create duplicate plan artifacts for identical content", async () => {
    let onPlanReady: ((content: string | null) => void) | null = null;
    const showTab = vi.fn();
    const { result } = renderHook(() =>
      useChatPageArtifacts({
        chat: createChat({ setOnPlanReady: (fn) => void (onPlanReady = fn) }),
        showTab,
        dismissOnMobile: vi.fn(),
        closeIfAutoOpened: vi.fn(),
      }),
    );

    await waitFor(() => expect(onPlanReady).toBeTruthy());
    act(() => {
      onPlanReady?.("# Plan\n\nStep 1");
      onPlanReady?.("# Plan\n\nStep 1");
    });

    await waitFor(() => {
      expect(result.current.artifacts.size).toBe(1);
    });
    expect(showTab).toHaveBeenCalledWith("plans");
  });

  it("adds plan revisions to the existing plan artifact", async () => {
    let onPlanReady: ((content: string | null) => void) | null = null;
    const { result } = renderHook(() =>
      useChatPageArtifacts({
        chat: createChat({ setOnPlanReady: (fn) => void (onPlanReady = fn) }),
        showTab: vi.fn(),
        dismissOnMobile: vi.fn(),
        closeIfAutoOpened: vi.fn(),
      }),
    );

    await waitFor(() => expect(onPlanReady).toBeTruthy());
    act(() => {
      onPlanReady?.("# Plan\n\nStep 1");
    });
    await waitFor(() => {
      expect(result.current.artifacts.size).toBe(1);
    });
    act(() => {
      onPlanReady?.("# Plan\n\nStep 2");
    });

    await waitFor(() => {
      expect(result.current.activeArtifact?.versions).toHaveLength(2);
      expect(result.current.activeArtifact?.versions[1].content).toBe(
        "# Plan\n\nStep 2",
      );
    });
  });

  it("exposes showPlanRef for reopening the plan artifact", async () => {
    let onPlanReady: ((content: string | null) => void) | null = null;
    const showTab = vi.fn();
    const showPlanRef = { current: null as (() => void) | null };
    renderHook(() =>
      useChatPageArtifacts({
        chat: createChat({ setOnPlanReady: (fn) => void (onPlanReady = fn) }),
        showTab,
        dismissOnMobile: vi.fn(),
        closeIfAutoOpened: vi.fn(),
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

  it("routes valid artifact events into the artifact panel", async () => {
    let onArtifactEvent:
      | ((
          type: string,
          content: string,
          language?: string,
          title?: string,
        ) => void)
      | null = null;
    const showTab = vi.fn();
    const { result } = renderHook(() =>
      useChatPageArtifacts({
        chat: createChat({
          setOnArtifactEvent: (fn) => void (onArtifactEvent = fn),
        }),
        showTab,
        dismissOnMobile: vi.fn(),
        closeIfAutoOpened: vi.fn(),
      }),
    );

    await waitFor(() => expect(onArtifactEvent).toBeTruthy());
    act(() => {
      onArtifactEvent?.("image", "data:image/png;base64,abc", "png", "Image");
      onArtifactEvent?.("unknown", "ignored");
    });

    await waitFor(() => {
      expect(result.current.artifacts.size).toBe(1);
      expect(result.current.activeArtifact?.title).toBe("Image");
    });
    expect(showTab).toHaveBeenCalledWith("artifacts");
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
