/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat callbacks intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback, useEffect } from "react";
import { enqueuePendingProxyMessage } from "./pendingProxyMessages";
import { saveConversationId, uuid } from "./conversationPersistence";
import { normalizeReasoningEffort } from "./sessionRecords";
import { attachmentPayload, userContentBlocks } from "./actionPayloads";
import type { SendMessageAction, UseChatActionsParams } from "./actionTypes";

export function useMessageAction(
  params: UseChatActionsParams,
): SendMessageAction {
  const {
    activeRequestIdRef,
    attachedSessionIdRef,
    attachedSessionMetaRef,
    continuingSessionIdRef,
    conversationIdRef,
    dbSessionIdRef,
    ensureMainSession,
    pendingMessagesRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    projectIdRef,
    selectedProviderRef,
    sendMessageRef,
    sessionInteractionModeRef,
    setIsStreaming,
    setIsThinking,
    setMessages,
    setProxyDeliveryNotice,
    wsRef,
  } = params;

  const sendMessage: SendMessageAction = useCallback(
    (
      content,
      model,
      files,
      projectId,
      injectContext,
      reasoningEffort,
      ttsEnabled,
    ) => {
      const normalizedReasoningEffort =
        normalizeReasoningEffort(reasoningEffort);

      if (continuingSessionIdRef.current) {
        return false;
      }

      const needsSession =
        !conversationIdRef.current || !dbSessionIdRef.current;
      const isProxyTerminal =
        attachedSessionIdRef.current &&
        sessionInteractionModeRef.current === "proxy" &&
        attachedSessionMetaRef.current?.sessionType === "terminal";

      if (needsSession && !isProxyTerminal) {
        void ensureMainSession({
          projectId: projectId ?? projectIdRef.current,
          provider: selectedProviderRef.current,
          model: model ?? null,
          reasoningEffort: normalizedReasoningEffort,
        })
          .then((sessionId) => {
            if (!sessionId) return;
            sendMessageRef.current?.(
              content,
              model,
              files,
              projectId,
              injectContext,
              normalizedReasoningEffort,
              ttsEnabled,
            );
          })
          .catch((error) => {
            console.error("Failed to create chat session before send:", error);
            setMessages((prev) => [
              ...prev,
              {
                id: `system-session-create-${uuid()}`,
                role: "system" as const,
                content: "Failed to create chat session",
                timestamp: new Date(),
              },
            ]);
          });
        return true;
      }

      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        console.warn("WebSocket disconnected — queuing message for reconnect");
        pendingMessagesRef.current.push({
          content,
          model,
          files,
          projectId,
          reasoningEffort: normalizedReasoningEffort,
          ttsEnabled,
        });
        const queuedId = `user-${uuid()}`;
        setMessages((prev) => [
          ...prev,
          {
            id: queuedId,
            role: "user" as const,
            content,
            toolCalls: [],
            contentBlocks: userContentBlocks(content, files),
            timestamp: new Date(),
          },
        ]);
        return true;
      }

      if (isProxyTerminal) {
        const proxySessionId = attachedSessionIdRef.current;
        if (!proxySessionId) {
          return false;
        }
        const clientMessageId = uuid();
        const messageId = `user-${clientMessageId}`;
        const pendingProxyMessage = {
          clientMessageId,
          currentMessageId: messageId,
          sessionId: proxySessionId,
        };
        pendingProxyMessagesRef.current.set(
          clientMessageId,
          pendingProxyMessage,
        );
        enqueuePendingProxyMessage(
          pendingProxySessionQueuesRef.current,
          pendingProxyMessage,
        );
        setMessages((prev) => [
          ...prev,
          {
            id: messageId,
            role: "user",
            content,
            contentBlocks: userContentBlocks(content, files),
            timestamp: new Date(),
          },
        ]);
        setProxyDeliveryNotice(null);
        const attachments = attachmentPayload(files);
        wsRef.current.send(
          JSON.stringify({
            type: "send_to_cli_session",
            session_id: proxySessionId,
            content,
            client_message_id: clientMessageId,
            attachments,
          }),
        );
        return true;
      }

      const messageId = `user-${uuid()}`;
      const requestId = uuid();
      activeRequestIdRef.current = requestId;

      setMessages((prev) => [
        ...prev,
        {
          id: messageId,
          role: "user",
          content,
          contentBlocks: userContentBlocks(content, files),
          timestamp: new Date(),
        },
      ]);

      saveConversationId(conversationIdRef.current);

      const payload: Record<string, unknown> = {
        type: "chat_message",
        content,
        message_id: messageId,
        conversation_id: conversationIdRef.current,
        request_id: requestId,
      };

      if (model) {
        payload.model = model;
      }

      if (projectId) {
        payload.project_id = projectId;
      }

      if (injectContext) {
        payload.inject_context = injectContext;
      }

      if (normalizedReasoningEffort) {
        payload.reasoning_effort = normalizedReasoningEffort;
      }

      if (typeof ttsEnabled === "boolean") {
        payload.tts_enabled = ttsEnabled;
      }

      if (selectedProviderRef.current) {
        payload.provider = selectedProviderRef.current;
      }

      const attachments = attachmentPayload(files);
      if (attachments.length > 0) {
        payload.attachments = attachments;
      }

      wsRef.current.send(JSON.stringify(payload));

      setIsStreaming(true);
      setIsThinking(true);
      return true;
    },
    [ensureMainSession],
  );

  useEffect(() => {
    sendMessageRef.current = sendMessage;
  }, [sendMessage]);

  return sendMessage;
}
