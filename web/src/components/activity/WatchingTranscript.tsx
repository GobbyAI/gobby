import { useCallback, useLayoutEffect, useMemo, useRef } from "react";

import type { SessionMessage } from "../../hooks/useSessionDetail";
import type { ChatMessage, ContentBlock, ToolCall } from "../../types/chat";
import { MessageItem } from "../chat/MessageItem";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";

interface WatchingTranscriptProps {
  sessionId: string | null;
  messages: SessionMessage[];
  isLoading: boolean;
  emptyStateMessage: string;
}

function normalizeRole(role: string): ChatMessage["role"] {
  if (role === "user" || role === "assistant" || role === "system") {
    return role;
  }
  return "assistant";
}

function visibleToolCalls(toolCalls: ToolCall[]): ToolCall[] {
  // Completed AskUserQuestion calls are rendered as interaction state elsewhere.
  return toolCalls.filter(
    (toolCall) =>
      !(
        toolCall.tool_name === "AskUserQuestion" &&
        toolCall.status !== "calling"
      ),
  );
}

function visibleContentBlocks(
  blocks: ContentBlock[] | undefined,
): ContentBlock[] | undefined {
  if (!blocks) return undefined;

  return blocks.map((block) => {
    if (block.type !== "tool_chain") {
      return block;
    }
    return { ...block, tool_calls: visibleToolCalls(block.tool_calls) };
  });
}

function toChatMessage(message: SessionMessage): ChatMessage {
  const contentBlocks = visibleContentBlocks(message.content_blocks);
  const chatMessage: ChatMessage = {
    id: message.id,
    role: normalizeRole(message.role),
    content: message.content || "",
    timestamp: new Date(message.timestamp),
    contentBlocks,
  };

  if (contentBlocks) {
    for (const block of contentBlocks) {
      if (block.type === "tool_chain" && block.tool_calls) {
        chatMessage.toolCalls = [
          ...(chatMessage.toolCalls || []),
          ...block.tool_calls,
        ];
      } else if (block.type === "thinking") {
        chatMessage.thinkingContent =
          (chatMessage.thinkingContent || "") + block.content;
      }
    }
  }

  return chatMessage;
}

function toolCallScrollSignature(toolCall: ToolCall): string {
  const resultContent = toolCall.result?.content;
  const resultContentLength =
    typeof resultContent === "string"
      ? resultContent.length
      : JSON.stringify(resultContent ?? "").length;

  return [
    toolCall.id,
    toolCall.tool_name,
    toolCall.status,
    toolCall.error?.length ?? 0,
    toolCall.result?.kind ?? "",
    toolCall.result?.truncated ? "truncated" : "full",
    resultContentLength,
  ].join(":");
}

function blockScrollSignature(block: ContentBlock): string {
  if (block.type === "text" || block.type === "thinking") {
    return `${block.type}:${block.content.length}`;
  }
  if (block.type === "tool_chain") {
    return `tool_chain:${block.tool_calls.length}:${block.tool_calls
      .map(toolCallScrollSignature)
      .join(",")}`;
  }
  if (block.type === "tool_reference") {
    return `tool_reference:${block.tool_name}:${block.server_name}`;
  }
  if (block.type === "document") {
    return `document:${block.source?.name?.length ?? 0}`;
  }
  if (block.type === "unknown") {
    return `unknown:${block.block_type}:${JSON.stringify(block.raw).length}`;
  }
  return block.type;
}

function lastMessageScrollKey(message: ChatMessage | undefined): string {
  if (!message) return "empty";

  const contentBlockSignature =
    message.contentBlocks?.map(blockScrollSignature).join("|") ?? "";
  const toolCallSignature =
    message.toolCalls?.map(toolCallScrollSignature).join("|") ?? "";

  return [
    message.id,
    message.content.length,
    message.thinkingContent?.length ?? 0,
    contentBlockSignature,
    toolCallSignature,
  ].join("::");
}

export function WatchingTranscript({
  sessionId,
  messages,
  isLoading,
  emptyStateMessage,
}: WatchingTranscriptProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const pendingScrollFrameRef = useRef<number | null>(null);
  const chatMessages = useMemo(() => messages.map(toChatMessage), [messages]);
  const scrollKey = lastMessageScrollKey(chatMessages[chatMessages.length - 1]);

  const setScrollerToBottom = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    scroller.style.scrollBehavior = "auto";
    scroller.scrollTop = scroller.scrollHeight;
  }, []);

  const scrollToBottom = useCallback(() => {
    setScrollerToBottom();
    const messagesEnd = messagesEndRef.current;
    if (messagesEnd && typeof messagesEnd.scrollIntoView === "function") {
      messagesEnd.scrollIntoView({ behavior: "auto", block: "end" });
    }
  }, [setScrollerToBottom]);

  const scheduleSetScrollerToBottom = useCallback(() => {
    if (
      typeof window === "undefined" ||
      typeof window.requestAnimationFrame !== "function"
    ) {
      setScrollerToBottom();
      return;
    }
    if (pendingScrollFrameRef.current !== null) {
      window.cancelAnimationFrame(pendingScrollFrameRef.current);
    }
    pendingScrollFrameRef.current = window.requestAnimationFrame(() => {
      setScrollerToBottom();
      pendingScrollFrameRef.current = window.requestAnimationFrame(() => {
        pendingScrollFrameRef.current = null;
        setScrollerToBottom();
      });
    });
  }, [setScrollerToBottom]);

  useLayoutEffect(
    () => () => {
      if (pendingScrollFrameRef.current !== null) {
        window.cancelAnimationFrame(pendingScrollFrameRef.current);
      }
    },
    [],
  );

  useLayoutEffect(() => {
    scrollToBottom();
    scheduleSetScrollerToBottom();
  }, [scheduleSetScrollerToBottom, scrollKey, scrollToBottom, sessionId]);

  return (
    <div
      ref={scrollerRef}
      className="flex-1 overflow-y-auto chat-scaled overscroll-contain [overflow-anchor:none]"
      style={{
        scrollBehavior: "auto",
        overflowAnchor: "none",
        overscrollBehavior: "contain",
      }}
    >
      {isLoading ? (
        <ActivityPanelEmpty body="Loading messages…" />
      ) : chatMessages.length === 0 ? (
        <ActivityPanelEmpty body={emptyStateMessage} />
      ) : (
        <>
          {chatMessages.map((message) => (
            <MessageItem key={message.id} message={message} />
          ))}
          <div ref={messagesEndRef} />
        </>
      )}
    </div>
  );
}
