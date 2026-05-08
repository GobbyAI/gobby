/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat transport intentionally closes over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { A2UISurfaceState } from "../../components/canvas/types";
import type { CanvasPanelState } from "../../components/canvas/hooks/useCanvasPanel";
import type { ChatMessage, SessionObservationMeta } from "../../types/chat";
import { normalizeChatMode } from "../../types/chat";
import { mapRenderedMessageToChatMessage } from "../../lib/chatMessageMapping";
import { canProxyAttachObservationMeta } from "../../lib/sessionProxyAttach";
import {
  type ApiMessage,
  type ChatError,
  type ChatStreamChunk,
  type ChatThinkingMessage,
  type ContextUsage,
  type ModelSwitchedMessage,
  type SessionUsageUpdatedMessage,
  type TokenEventMessage,
  type ToolStatusMessage,
  type VoiceTranscriptionMessage,
  type WebSocketMessage,
  buildContextUsageFromTotals,
  clearPendingProxyMessages,
  computeContextUsageFromSessionData,
  consumePendingProxyMessage,
  hasSessionUsage,
  isChatProvider,
  mapApiMessages,
  mapStoredChatMessage,
  normalizeSessionType,
  removePendingProxyMessageFromQueue,
  saveConversationId,
  saveDbSessionId,
  toSessionObservationMeta,
  uuid,
} from "./core";

type Setter<T> = Dispatch<SetStateAction<T>>;

interface UseChatTransportParams extends Record<string, any> {
  resolveAgentName: (agentRunId: string) => Promise<string | null>;
  setAttachedSessionMeta: Setter<SessionObservationMeta | null>;
  setCanvasPanel: Setter<CanvasPanelState | null>;
  setCanvasSurfaces: Setter<Map<string, A2UISurfaceState>>;
  setContextUsage: Setter<ContextUsage>;
  setMainSessionMeta: Setter<SessionObservationMeta | null>;
  setMessages: Setter<ChatMessage[]>;
  setViewingSessionMeta: Setter<SessionObservationMeta | null>;
  messagesRef: MutableRefObject<ChatMessage[]>;
}

export function useChatTransport(params: UseChatTransportParams) {
  const {
    activeRequestIdRef,
    applyMainSessionMeta,
    attachedSessionIdRef,
    attachedSessionMetaRef,
    clearContinuationRollback,
    clearContinuingSession,
    conversationIdRef,
    continuingSessionIdRef,
    continuationRollbackRef,
    currentModeRef,
    dbSessionIdRef,
    handleBinaryMessageRef,
    handleChatErrorRef,
    handleChatStreamRef,
    handleChatThinkingRef,
    handleModelSwitchedRef,
    handleToolStatusRef,
    handleVoiceMessageRef,
    lastSeqRef,
    lastServerModeTimestampRef,
    markSessionUsageFresh,
    messagesRef,
    observedSessionIdRef,
    observedSessionMetaRef,
    onArtifactEventRef,
    onChatClearedRef,
    onChatDeletedRef,
    onModeChangedRef,
    onPlanReadyRef,
    pendingMessagesRef,
    pendingPlanFeedbackRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    pendingSessionInteractionModeRef,
    planContentRef,
    preAttachContextUsageRef,
    reconnectTimeoutRef,
    resolveAgentName,
    restoreContinuationState,
    sendMessageRef,
    sessionInteractionModeRef,
    sessionRefRef,
    setActiveAgent,
    setAttachedSessionId,
    setAttachedSessionMeta,
    setCanvasPanel,
    setCanvasSurfaces,
    setContextUsage,
    setConversationId,
    setCurrentBranch,
    setDbSessionId,
    setIsConnected,
    setIsReconnecting,
    setIsStreaming,
    setIsThinking,
    setMainSessionMeta,
    setMessages,
    setObservedSessionId,
    setPlanPendingApproval,
    setProxyDeliveryNotice,
    setSelectedProvider,
    setSessionInteractionMode,
    setSessionRef,
    setSessionTitle,
    setViewingSessionId,
    setViewingSessionMeta,
    setWorktreePath,
    viewingSessionIdRef,
    viewingSessionMetaRef,
    wsRef,
  } = params;

  const connectRef = useRef<(() => void) | null>(null);

// Connect to WebSocket
const connect = useCallback(() => {
  if (wsRef.current?.readyState === WebSocket.OPEN) return;

  const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProtocol}//${window.location.host}/ws`;

  console.log("Connecting to WebSocket:", wsUrl);
  const ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer"; // For TTS audio binary frames
  wsRef.current = ws;

  ws.onopen = () => {
    console.log("WebSocket connected");
    setIsConnected(true);
    setIsReconnecting(false);

    // Flush queued messages that were sent while disconnected
    if (pendingMessagesRef.current.length > 0) {
      const queued = [...pendingMessagesRef.current];
      pendingMessagesRef.current = [];
      // Send each queued message after a brief delay to let WS fully initialize
      setTimeout(() => {
        for (const msg of queued) {
          sendMessageRef.current?.(
            msg.content,
            msg.model ?? null,
            undefined,
            msg.projectId,
            undefined,
            msg.reasoningEffort,
            msg.ttsEnabled,
          );
        }
      }, 500);
    }

    // Backfill missed messages on reconnect (lastSeqRef > 0 means we had messages before)
    if (lastSeqRef.current > 0 && conversationIdRef.current) {
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      const convId = conversationIdRef.current;
      const afterSeq = lastSeqRef.current;
      fetch(`${baseUrl}/api/chat/${convId}/messages?after_seq=${afterSeq}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (viewingSessionIdRef.current) return;
          if (!data?.messages?.length) return;
          const backfilled: ChatMessage[] = data.messages.map(mapStoredChatMessage);
          setMessages((prev) => {
            const existingIds = new Set(prev.map((m) => m.id));
            const newMsgs = backfilled.filter((m) => !existingIds.has(m.id));
            return newMsgs.length > 0 ? [...prev, ...newMsgs] : prev;
          });
          if (data.max_seq) lastSeqRef.current = data.max_seq;
        })
        .catch((err) => console.error("Failed to backfill messages:", err));
    }

    ws.send(
      JSON.stringify({
        type: "subscribe",
        events: [
          "chat_stream",
          "chat_error",
          "tool_status",
          "chat_thinking",
          "canvas_event",
          "artifact_event",
          "session_message",
          "session_usage_updated",
          "token_event",
        ],
      }),
    );

    // Rebind this client to the current main-chat conversation without
    // mutating the server-owned session. The backend uses conversation_id
    // on the websocket client metadata to scope broadcasts for the active
    // chat stream and pending approvals.
    if (conversationIdRef.current) {
      ws.send(
        JSON.stringify({
          type: "heartbeat",
          conversation_id: conversationIdRef.current,
        }),
      );
    }
  };

  ws.onclose = () => {
    console.log("WebSocket disconnected");
    setIsConnected(false);
    setIsReconnecting(true);
    // Don't clear isStreaming/isThinking/activeRequestIdRef — the backend
    // may still be working. Clearing these causes post-reconnect tool_status
    // updates to be dropped as "stale". Only clear on explicit cancel or
    // if reconnect timeout expires (30s).
    const disconnectTimer = window.setTimeout(() => {
      // If still disconnected after 30s, assume the stream is dead
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        setIsStreaming(false);
        setIsThinking(false);
        activeRequestIdRef.current = null;
      }
    }, 30_000);

    reconnectTimeoutRef.current = window.setTimeout(() => {
      clearTimeout(disconnectTimer);
      connectRef.current?.();
    }, 2000);
  };

  ws.onerror = (error) => {
    console.error("WebSocket error:", error);
  };

  ws.onmessage = (event) => {
    // Binary frames are TTS audio data — route to voice handler
    if (event.data instanceof ArrayBuffer) {
      try {
        handleBinaryMessageRef.current(event.data);
      } catch (err) {
        console.error("TTS binary message error:", err);
      }
      return;
    }

    try {
      const data = JSON.parse(event.data) as WebSocketMessage;
      console.log("WebSocket message:", data.type, data);

      if (data.type === "chat_stream") {
        handleChatStreamRef.current(data as unknown as ChatStreamChunk);
      } else if (data.type === "chat_error") {
        handleChatErrorRef.current(data as unknown as ChatError);
      } else if (data.type === "tool_status") {
        handleToolStatusRef.current(data as unknown as ToolStatusMessage);
      } else if (data.type === "chat_thinking") {
        handleChatThinkingRef.current(data as unknown as ChatThinkingMessage);
      } else if (data.type === "model_switched") {
        handleModelSwitchedRef.current(
          data as unknown as ModelSwitchedMessage,
        );
      } else if (
        data.type === "voice_transcription" ||
        data.type === "voice_audio_chunk" ||
        data.type === "voice_status" ||
        data.type === "tts_audio" ||
        data.type === "tts_status"
      ) {
        try {
          // When STT transcription arrives, inject it as a user message and
          // register the request_id so the assistant's response stream is accepted.
          if (data.type === "voice_transcription") {
            const voiceMsg = data as unknown as VoiceTranscriptionMessage;
            const text =
              typeof voiceMsg.text === "string" ? voiceMsg.text : "";
            const reqId =
              typeof voiceMsg.request_id === "string"
                ? voiceMsg.request_id
                : "";
            if (text && reqId) {
              activeRequestIdRef.current = reqId;
              setMessages((prev) => [
                ...prev,
                {
                  id: `user-voice-${reqId}`,
                  role: "user" as const,
                  content: text,
                  timestamp: new Date(),
                },
              ]);
              setIsStreaming(true);
              setIsThinking(true);
            }
          }
          handleVoiceMessageRef.current(data as Record<string, unknown>);
        } catch (err) {
          console.error("Voice message handling error:", err);
          setIsStreaming(false);
          setIsThinking(false);
        }
      } else if (data.type === "plan_pending_approval") {
        const msgConvId = (data as Record<string, unknown>)
          .conversation_id as string | undefined;
        // Only accept plans for the current conversation (or unscoped legacy events)
        if (!msgConvId || msgConvId === conversationIdRef.current) {
          const planContent = (data as Record<string, unknown>)
            .plan_content as string | undefined;
          if (planContent) {
            const previousPlanContent = planContentRef.current;
            setPlanPendingApproval(true);
            planContentRef.current = planContent;
            if (planContent !== previousPlanContent) {
              onPlanReadyRef.current?.(planContent);
            }
          }
        }
      } else if (data.type === "mode_changed") {
        const msgConvId = (data as Record<string, unknown>)
          .conversation_id as string | undefined;
        // Only apply mode changes for the CURRENT conversation
        if (!msgConvId || msgConvId === conversationIdRef.current) {
          const rawMode = (data as Record<string, unknown>).mode as
            | string
            | undefined;
          const newMode = rawMode ? normalizeChatMode(rawMode) : undefined;
          const reason = (data as Record<string, unknown>).reason as
            | string
            | undefined;
          if (newMode) {
            lastServerModeTimestampRef.current = Date.now();
            // Clear plan state on approval — for rejection, the eager
            // clear in requestPlanChanges() already handled it, and
            // clearing here would race with a new plan_pending_approval
            // that may have arrived before this mode_changed.
            if (reason === "plan_approved") {
              setPlanPendingApproval(false);
              planContentRef.current = null;
            }
            if (
              reason === "plan_changes_requested" &&
              pendingPlanFeedbackRef.current
            ) {
              const feedback = pendingPlanFeedbackRef.current;
              pendingPlanFeedbackRef.current = null;
              setTimeout(() => {
                sendMessageRef.current?.(feedback);
              }, 200);
            }
            // Only update mode and notify if it actually changed —
            // prevents set_mode → mode_changed → setState → set_mode loop
            if (newMode !== currentModeRef.current) {
              currentModeRef.current = newMode;
              onModeChangedRef.current?.(newMode);
            }
          }
        }
      } else if (data.type === "session_info") {
        const info = data as Record<string, unknown>;
        const ref = info.session_ref as string | undefined;
        if (ref) setSessionRef(ref);
        const dbSid = info.db_session_id as string | undefined;
        const infoConvId = info.conversation_id as string | undefined;
        if (
          dbSid &&
          (!infoConvId || infoConvId === conversationIdRef.current)
        ) {
          setDbSessionId(dbSid);
        }
        const branch = info.current_branch as string | undefined;
        if (branch !== undefined) setCurrentBranch(branch);
        const wtPath = info.worktree_path as string | undefined;
        if (wtPath !== undefined) setWorktreePath(wtPath);
        const agentName = info.agent_name as string | undefined;
        if (agentName) setActiveAgent(agentName);
      } else if (data.type === "worktree_switched") {
        const wt = data as Record<string, unknown>;
        setCurrentBranch((wt.new_branch as string) ?? null);
        setWorktreePath((wt.worktree_path as string) ?? null);
      } else if (data.type === "agent_changed") {
        const ac = data as Record<string, unknown>;
        const agentName = ac.agent_name as string | undefined;
        if (agentName) setActiveAgent(agentName);
      } else if (data.type === "session_continued") {
        const continued = data as Record<string, unknown>;
        clearContinuingSession();
        const nextConversationId =
          (continued.conversation_id as string | undefined) ?? null;
        const nextDbSessionId = (continued.db_session_id as string) ?? null;
        if (
          nextConversationId &&
          nextConversationId !== conversationIdRef.current
        ) {
          conversationIdRef.current = nextConversationId;
          setConversationId(nextConversationId);
          saveConversationId(nextConversationId);
        }
        setDbSessionId(nextDbSessionId);
        dbSessionIdRef.current = nextDbSessionId;
        saveDbSessionId(nextDbSessionId);
        const continuedMeta = toSessionObservationMeta(continued, {
          ref: (continued.ref as string | undefined) ?? sessionRefRef.current,
          status: (continued.status as string | undefined) ?? "active",
          sessionType:
            normalizeSessionType(continued.session_type) ?? "web_chat",
        });
        if (continuedMeta) {
          setMainSessionMeta(continuedMeta);
          setSessionTitle(continuedMeta.title ?? null);
          if (continuedMeta.ref) {
            setSessionRef(continuedMeta.ref);
          }
          setCurrentBranch(continuedMeta.gitBranch ?? null);
          if (continuedMeta.source && isChatProvider(continuedMeta.source)) {
            setSelectedProvider(continuedMeta.source);
          }
          if (continuedMeta.chatMode) {
            const restored = normalizeChatMode(continuedMeta.chatMode);
            currentModeRef.current = restored;
            onModeChangedRef.current?.(restored);
          }
        }
        if (nextDbSessionId) {
          const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
          fetch(`${baseUrl}/api/sessions/${nextDbSessionId}`)
            .then((res) => (res.ok ? res.json() : null))
            .then((payload) => {
              const session = payload?.session;
              if (!session || dbSessionIdRef.current !== nextDbSessionId)
                return;
              applyMainSessionMeta(session);
            })
            .catch(() => {});
        }
        clearContinuationRollback();
        const resumeNotice =
          typeof continued.resume_notice === "string"
            ? continued.resume_notice
            : null;
        if (resumeNotice) {
          setMessages((prev) => [
            ...prev,
            {
              id: `system-resume-notice-${uuid()}`,
              role: "system" as const,
              content: resumeNotice,
              timestamp: new Date(),
            },
          ]);
        }
        console.log("Session continued:", data);
      } else if (data.type === "error") {
        const err = data as Record<string, unknown>;
        if (continuingSessionIdRef.current) {
          const activeContinuationId = continuingSessionIdRef.current;
          clearContinuingSession();
          const rollback = continuationRollbackRef.current;
          if (rollback && rollback.sourceSessionId === activeContinuationId) {
            clearContinuationRollback();
            restoreContinuationState(rollback);
          }
        }
        const errorMessage =
          typeof err.message === "string" ? err.message : "Unknown error";
        setMessages((prev) => [
          ...prev,
          {
            id: `system-error-${uuid()}`,
            role: "system" as const,
            content: errorMessage,
            timestamp: new Date(),
          },
        ]);
      } else if (data.type === "connection_established") {
        const serverConversations = (data.conversation_ids as string[]) || [];
        if (serverConversations.includes(conversationIdRef.current)) {
          console.log(
            "Reconnected to existing conversation:",
            conversationIdRef.current,
          );
        }
        console.log("Connection established:", data);
      } else if (data.type === "canvas_event") {
        const ev = data as any;
        if (ev.event === "surface_update") {
          setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
            const next = new Map(prev);
            next.set(ev.canvas_id, {
              canvasId: ev.canvas_id,
              conversationId: ev.conversation_id,
              mode: ev.mode,
              surface: ev.surface,
              dataModel: ev.data_model,
              rootComponentId: ev.root_component_id,
              completed: ev.completed,
            });
            return next;
          });
        } else if (
          ev.event === "interaction_confirmed" ||
          ev.event === "close_canvas"
        ) {
          setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
            const next = new Map(prev);
            const s = next.get(ev.canvas_id);
            if (s) {
              next.set(ev.canvas_id, { ...s, completed: true });
            }
            return next;
          });
          if (ev.event === "close_canvas") {
            setCanvasPanel((prev) =>
              prev?.canvasId === ev.canvas_id ? null : prev,
            );
          }
        } else if (ev.event === "panel_present") {
          setCanvasPanel((prev: CanvasPanelState | null) => ({
            ...prev,
            canvasId: ev.canvas_id,
            title: ev.title,
            url: ev.html_url,
            width: ev.width || prev?.width,
            height: ev.height || prev?.height,
          }));
        } else if (ev.event === "canvas_rehydrate") {
          setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
            const next = new Map(prev);
            for (const s of ev.surfaces || []) {
              if (s.mode === "a2ui") {
                next.set(s.canvas_id, {
                  canvasId: s.canvas_id,
                  conversationId: s.conversation_id,
                  mode: s.mode,
                  surface: s.surface,
                  dataModel: s.data_model,
                  rootComponentId: s.root_component_id,
                  completed: s.completed,
                });
              } else if (s.mode === "html" && !s.completed) {
                setCanvasPanel({
                  canvasId: s.canvas_id,
                  title: s.title,
                  url: s.html_url,
                });
              }
            }
            return next;
          });
        }
      } else if (data.type === "artifact_event") {
        const ev = data as any;
        if (ev.event === "show_file") {
          onArtifactEventRef.current?.(
            ev.artifact_type,
            ev.content,
            ev.language,
            ev.title,
          );
        }
      } else if (data.type === "attach_to_session_result") {
        const result = data as Record<string, unknown>;
        const sid = result.session_id as string;
        const meta =
          toSessionObservationMeta(result) ??
          ({
            ref: null,
            source: "unknown",
            title: null,
            status: "unknown",
            canProxyAttach: false,
            model: null,
            externalId: "",
            chatMode: null,
            gitBranch: null,
            contextWindow: null,
            agentRunId: null,
            workflowName: null,
            agentName: null,
            sessionType: null,
          } satisfies SessionObservationMeta);
        setObservedSessionId(sid);
        observedSessionIdRef.current = sid;
        observedSessionMetaRef.current = meta;
        // Also set viewing state (attached implies viewing)
        setViewingSessionId(sid);
        viewingSessionIdRef.current = sid;
        setViewingSessionMeta(meta);
        viewingSessionMetaRef.current = meta;
        const requestedMode = pendingSessionInteractionModeRef.current;
        const proxyCapable = canProxyAttachObservationMeta(meta);
        const nextMode =
          requestedMode === "proxy" && !proxyCapable ? "none" : requestedMode;
        clearPendingProxyMessages(
          pendingProxyMessagesRef.current,
          pendingProxySessionQueuesRef.current,
        );
        setSessionInteractionMode(nextMode);
        sessionInteractionModeRef.current = nextMode;
        if (nextMode === "proxy") {
          setAttachedSessionId(sid);
          attachedSessionIdRef.current = sid;
          setAttachedSessionMeta(meta);
          attachedSessionMetaRef.current = meta;
          setProxyDeliveryNotice(null);
          if (
            meta.chatMode === "act" ||
            meta.chatMode === "accept_edits" ||
            meta.chatMode === "bypass" ||
            meta.chatMode === "normal" ||
            meta.chatMode === "plan"
          ) {
            const restored = normalizeChatMode(meta.chatMode);
            if (restored !== currentModeRef.current) {
              currentModeRef.current = restored;
              onModeChangedRef.current?.(restored);
            }
          }
        } else {
          setAttachedSessionId(null);
          attachedSessionIdRef.current = null;
          setAttachedSessionMeta(null);
          attachedSessionMetaRef.current = null;
          setProxyDeliveryNotice(
            requestedMode === "proxy" && meta.sessionType === "terminal"
              ? meta.status === "paused"
                ? "This terminal session is paused. Use Resume Session to continue it in web chat."
                : "This terminal session can only be resumed in web chat right now."
              : null,
          );
        }
        // Map initial messages into chat format with proper tool call grouping
        const msgs = (result.messages as ApiMessage[]) || [];
        const mapped: ChatMessage[] = mapApiMessages(msgs);
        // Preserve REST-loaded transcript when re-attaching to viewed session
        if (
          viewingSessionIdRef.current === sid &&
          messagesRef.current.length > 0
        ) {
          const mappedById = new Map(mapped.map((m) => [m.id, m]));
          // Merge updates into existing messages, then append truly new ones
          const existingIds = new Set(messagesRef.current.map((m) => m.id));
          const merged = messagesRef.current.map(
            (m) => mappedById.get(m.id) ?? m,
          );
          const newMsgs = mapped.filter((m) => !existingIds.has(m.id));
          if (newMsgs.length > 0 || mappedById.size > 0) {
            setMessages([...merged, ...newMsgs]);
          }
        } else {
          setMessages(mapped);
        }
        setIsStreaming(false);
        setIsThinking(false);
        setSessionRef((result.ref as string) ?? null);
        if (hasSessionUsage(result)) {
          setContextUsage(computeContextUsageFromSessionData(result));
        }
        // Do NOT set dbSessionId here. Under the unified session identity
        // model, dbSessionId mirrors the user's main chat conversation id,
        // not an observed/attached session. Observed state lives on
        // observedSessionIdRef / viewingSessionIdRef / attachedSessionIdRef.
        // Overwriting dbSessionId here would diverge it from conversationId
        // and trap sendMessage in an infinite ensureMainSession retry loop.
        if (!meta.agentName && meta.agentRunId) {
          void resolveAgentName(meta.agentRunId).then((agentName) => {
            if (!agentName || viewingSessionIdRef.current !== sid) return;
            observedSessionMetaRef.current = {
              ...(observedSessionMetaRef.current ?? meta),
              agentName,
            };
            setViewingSessionMeta((prev) =>
              prev && viewingSessionIdRef.current === sid
                ? { ...prev, agentName }
                : prev,
            );
            setAttachedSessionMeta((prev) =>
              prev && attachedSessionIdRef.current === sid
                ? { ...prev, agentName }
                : prev,
            );
          });
        }
      } else if (data.type === "detach_from_session_result") {
        const sid =
          typeof (data as Record<string, unknown>).session_id === "string"
            ? ((data as Record<string, unknown>).session_id as string)
            : null;
        if (sid) {
          const isCurrentObserved = observedSessionIdRef.current === sid;
          const isCurrentAttached = attachedSessionIdRef.current === sid;
          const isCurrentViewedTerminal =
            viewingSessionIdRef.current === sid &&
            viewingSessionMetaRef.current?.sessionType === "terminal";
          if (
            !isCurrentObserved &&
            !isCurrentAttached &&
            !isCurrentViewedTerminal
          ) {
            return;
          }
        }
        setObservedSessionId(null);
        observedSessionMetaRef.current = null;
        setAttachedSessionId(null);
        attachedSessionIdRef.current = null;
        setAttachedSessionMeta(null);
        attachedSessionMetaRef.current = null;
        clearPendingProxyMessages(
          pendingProxyMessagesRef.current,
          pendingProxySessionQueuesRef.current,
        );
        setProxyDeliveryNotice(null);
        setSessionInteractionMode("none");
        sessionInteractionModeRef.current = "none";
        // Restore main-chat contextUsage snapshot taken at first attach,
        // so the pie stops showing the observed session's percentages.
        if (preAttachContextUsageRef.current !== null) {
          setContextUsage(preAttachContextUsageRef.current);
          preAttachContextUsageRef.current = null;
        } else {
          setContextUsage({
            totalInputTokens: 0,
            outputTokens: 0,
            contextWindow: null,
            uncachedInputTokens: 0,
            cacheReadTokens: 0,
            cacheCreationTokens: 0,
          });
        }
        // Keep viewingSessionId/Meta — return to view-only mode
      } else if (
        data.type === "session_message" &&
        (data as Record<string, unknown>).session_id
      ) {
        const sm = data as Record<string, unknown>;
        const smSessionId = sm.session_id as string;
        const isObservedSession =
          smSessionId &&
          (smSessionId === observedSessionIdRef.current ||
            smSessionId === viewingSessionIdRef.current);
        if (!isObservedSession) {
          return;
        }

        const msg = sm.message as Record<string, unknown> | undefined;
        if (!msg) {
          return;
        }

        const renderedMessage = mapRenderedMessageToChatMessage(msg);
        const pendingProxyMessage =
          renderedMessage.role === "user" &&
          smSessionId === attachedSessionIdRef.current &&
          sessionInteractionModeRef.current === "proxy"
            ? consumePendingProxyMessage(
                pendingProxyMessagesRef.current,
                pendingProxySessionQueuesRef.current,
                smSessionId,
              )
            : null;
        setMessages((prev) => {
          const existingIdx = prev.findIndex(
            (message) => message.id === renderedMessage.id,
          );
          if (existingIdx >= 0) {
            const updated = [...prev];
            updated[existingIdx] = renderedMessage;
            return updated;
          }
          if (pendingProxyMessage) {
            const pendingIdx = prev.findIndex(
              (message) => message.id === pendingProxyMessage.currentMessageId,
            );
            if (pendingIdx >= 0) {
              const updated = [...prev];
              updated[pendingIdx] = renderedMessage;
              return updated;
            }
          }
          if (
            renderedMessage.role === "user" &&
            smSessionId === attachedSessionIdRef.current &&
            sessionInteractionModeRef.current === "proxy"
          ) {
            const optimisticIdx = prev.findIndex(
              (message) =>
                message.role === "user" &&
                message.id.startsWith("user-") &&
                message.content === renderedMessage.content,
            );
            if (optimisticIdx >= 0) {
              const updated = [...prev];
              updated[optimisticIdx] = renderedMessage;
              return updated;
            }
          }
          return [...prev, renderedMessage];
        });
        if (pendingProxyMessage) {
          pendingProxyMessage.currentMessageId = renderedMessage.id;
        }
      } else if (data.type === "send_to_cli_session_result") {
        const result = data as Record<string, unknown>;
        const clientMessageId =
          typeof result.client_message_id === "string"
            ? result.client_message_id
            : null;
        const messageId =
          typeof result.message_id === "string" ? result.message_id : null;
        if (clientMessageId) {
          const pendingProxyMessage =
            pendingProxyMessagesRef.current.get(clientMessageId) ?? null;
          if (pendingProxyMessage) {
            if (messageId && result.delivered !== false) {
              setMessages((prev) => {
                const messageIdx = prev.findIndex(
                  (message) =>
                    message.id === pendingProxyMessage.currentMessageId ||
                    message.id === messageId,
                );
                if (messageIdx < 0) {
                  return prev;
                }
                const updated = [...prev];
                updated[messageIdx] = {
                  ...updated[messageIdx],
                  id: messageId,
                };
                return updated;
              });
              pendingProxyMessage.currentMessageId = messageId;
              removePendingProxyMessageFromQueue(
                pendingProxySessionQueuesRef.current,
                pendingProxyMessage.sessionId,
                clientMessageId,
              );
              pendingProxyMessagesRef.current.delete(clientMessageId);
            }
          }
        }
        setProxyDeliveryNotice(
          result.delivered === false
            ? "Message queued until the session yields."
            : null,
        );
        console.log("Message sent to CLI session:", result.delivery_method);
      } else if (data.type === "session_usage_updated") {
        const update = data as unknown as SessionUsageUpdatedMessage;
        const visibleSessionId =
          viewingSessionIdRef.current ?? dbSessionIdRef.current;
        if (update.session_id === visibleSessionId) {
          markSessionUsageFresh(update.session_id, update.updated_at);
          setContextUsage((prev) =>
            buildContextUsageFromTotals({
              totalInputTokens:
                update.usage_input_tokens ?? prev.totalInputTokens,
              outputTokens: update.usage_output_tokens ?? prev.outputTokens,
              cacheReadTokens:
                update.usage_cache_read_tokens ?? prev.cacheReadTokens,
              cacheCreationTokens:
                update.usage_cache_creation_tokens ??
                prev.cacheCreationTokens,
              contextWindow:
                typeof update.context_window === "number"
                  ? update.context_window
                  : prev.contextWindow,
            }),
          );
        }
        if (viewingSessionIdRef.current === update.session_id) {
          setViewingSessionMeta((prev) =>
            prev
              ? {
                  ...prev,
                  model:
                    typeof update.model === "string" ? update.model : prev.model,
                  contextWindow:
                    typeof update.context_window === "number"
                      ? update.context_window
                      : prev.contextWindow,
                }
              : prev,
          );
        } else if (dbSessionIdRef.current === update.session_id) {
          setMainSessionMeta((prev) =>
            prev
              ? {
                  ...prev,
                  model:
                    typeof update.model === "string" ? update.model : prev.model,
                  contextWindow:
                    typeof update.context_window === "number"
                      ? update.context_window
                      : prev.contextWindow,
                }
              : prev,
          );
        }
      } else if (data.type === "token_event") {
        const eventData = data as unknown as TokenEventMessage;
        const visibleSessionId =
          viewingSessionIdRef.current ?? dbSessionIdRef.current;
        const sessionTotals = eventData.session_totals;
        if (
          eventData.session_id === visibleSessionId &&
          sessionTotals
        ) {
          markSessionUsageFresh(eventData.session_id, eventData.event_at);
          setContextUsage((prev) =>
            buildContextUsageFromTotals({
              totalInputTokens:
                sessionTotals.input_tokens ?? prev.totalInputTokens,
              outputTokens:
                sessionTotals.output_tokens ?? prev.outputTokens,
              cacheReadTokens:
                sessionTotals.cache_read_tokens ?? prev.cacheReadTokens,
              cacheCreationTokens:
                sessionTotals.cache_creation_tokens ??
                prev.cacheCreationTokens,
              contextWindow:
                typeof eventData.context_window === "number"
                  ? eventData.context_window
                  : prev.contextWindow,
            }),
          );
        }
      } else if (data.type === "subscribe_success") {
        console.log("Subscribed to events:", data);
      } else if (data.type === "chat_deleted") {
        const cid = (data as Record<string, unknown>)
          .conversation_id as string;
        console.log("Chat deleted confirmed:", cid);
        onChatDeletedRef.current?.(cid);
      } else if (data.type === "chat_cleared") {
        const cid = (data as Record<string, unknown>)
          .conversation_id as string;
        console.log("Chat cleared confirmed:", cid);
        onChatClearedRef.current?.(cid);
      }
    } catch (e) {
      console.error("Failed to parse WebSocket message:", e);
    }
  };
  // resolveAgentName is a stable useCallback (its own deps are []) — safe
  // to reference here without re-creating connect every render.
}, [
  applyMainSessionMeta,
  clearContinuingSession,
  clearContinuationRollback,
  markSessionUsageFresh,
  resolveAgentName,
  restoreContinuationState,
  setSelectedProvider,
]);

useEffect(() => {
  connectRef.current = connect;
}, [connect]);



  return connect;
}
