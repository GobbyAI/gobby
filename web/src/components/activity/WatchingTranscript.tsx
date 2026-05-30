import {
  forwardRef,
  useCallback,
  useMemo,
  useRef,
  type ForwardedRef,
} from "react";
import {
  Virtuoso,
  type Components,
  type ScrollerProps,
  type VirtuosoHandle,
} from "react-virtuoso";

import type { SessionMessage } from "../../hooks/useSessionDetail";
import type { ChatMessage, ContentBlock, ToolCall } from "../../types/chat";
import { MessageItem } from "../chat/MessageItem";
import { MessageErrorBoundary } from "../chat/MessageErrorBoundary";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";

interface WatchingTranscriptProps {
  sessionId: string | null;
  messages: SessionMessage[];
  isLoading: boolean;
  emptyStateMessage: string;
  hasMore: boolean;
  loadMore: () => void;
  isLoadingOlder: boolean;
  firstItemIndex: number;
  transcriptDegradedReason: string | null;
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

function LoadingOlder() {
  return (
    <div className="flex items-center justify-center gap-2 py-3 text-xs text-muted-foreground">
      <span
        className="h-3 w-3 rounded-full border-2 border-accent border-t-transparent motion-safe:animate-spin"
        aria-hidden="true"
      />
      Loading older messages…
    </div>
  );
}

function StartOfHistory() {
  return (
    <div className="py-3 text-center text-xs text-muted-foreground/70">
      Beginning of transcript
    </div>
  );
}

function DegradedNotice() {
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-3 text-xs text-muted-foreground">
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="shrink-0 text-warning"
      >
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      Older content capped to keep this view fast — reload to fetch more.
    </div>
  );
}

export function WatchingTranscript({
  sessionId,
  messages,
  isLoading,
  emptyStateMessage,
  hasMore,
  loadMore,
  isLoadingOlder,
  firstItemIndex,
  transcriptDegradedReason,
}: WatchingTranscriptProps) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);

  const chatMessages = useMemo(() => messages.map(toChatMessage), [messages]);

  const setScrollerRef = useCallback(
    (node: HTMLDivElement | null, forwardedRef: ForwardedRef<HTMLDivElement>) => {
      scrollerRef.current = node;
      if (typeof forwardedRef === "function") {
        forwardedRef(node);
      } else if (forwardedRef) {
        forwardedRef.current = node;
      }
    },
    [],
  );

  const handleAtBottomStateChange = useCallback((atBottom: boolean) => {
    atBottomRef.current = atBottom;
  }, []);

  // Reverse infinite scroll: fetch the next older page when the top is reached.
  const handleStartReached = useCallback(() => {
    if (hasMore) {
      loadMore();
    }
  }, [hasMore, loadMore]);

  const itemContent = useCallback(
    (_index: number, message: ChatMessage) => (
      <MessageErrorBoundary key={message.id} messageId={message.id}>
        <MessageItem message={message} />
      </MessageErrorBoundary>
    ),
    [],
  );

  const computeItemKey = useCallback(
    (_index: number, message: ChatMessage) => message.id,
    [],
  );

  const Header = useCallback(() => {
    if (isLoadingOlder) return <LoadingOlder />;
    if (transcriptDegradedReason) return <DegradedNotice />;
    if (!hasMore && chatMessages.length > 0) return <StartOfHistory />;
    return null;
  }, [isLoadingOlder, transcriptDegradedReason, hasMore, chatMessages.length]);

  const Scroller = useMemo<NonNullable<Components<ChatMessage>["Scroller"]>>(
    () =>
      forwardRef<HTMLDivElement, ScrollerProps>(function TranscriptScroller(
        { style, ...props },
        forwardedRef,
      ) {
        return (
          <div
            {...props}
            ref={(node) => setScrollerRef(node, forwardedRef)}
            style={{
              ...style,
              scrollBehavior: "auto",
              overflowAnchor: "none",
              overscrollBehavior: "contain",
            }}
          />
        );
      }),
    [setScrollerRef],
  );

  const components = useMemo<Components<ChatMessage>>(
    () => ({ Header, Scroller }),
    [Header, Scroller],
  );

  if (isLoading && chatMessages.length === 0) {
    return <ActivityPanelEmpty body="Loading messages…" />;
  }

  if (chatMessages.length === 0) {
    return <ActivityPanelEmpty body={emptyStateMessage} />;
  }

  return (
    <Virtuoso
      ref={virtuosoRef}
      key={sessionId ?? "none"}
      className="flex-1 min-h-0 overflow-x-hidden chat-scaled overscroll-contain [overflow-anchor:none] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [scrollbar-width:thin] [scrollbar-color:var(--border)_transparent]"
      data={chatMessages}
      firstItemIndex={firstItemIndex}
      initialTopMostItemIndex={Math.max(chatMessages.length - 1, 0)}
      itemContent={itemContent}
      computeItemKey={computeItemKey}
      startReached={handleStartReached}
      followOutput={(atBottom) => (atBottom ? "auto" : false)}
      atBottomThreshold={400}
      atBottomStateChange={handleAtBottomStateChange}
      overscan={400}
      increaseViewportBy={200}
      components={components}
    />
  );
}
