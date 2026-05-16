import {
  useCallback,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  forwardRef,
  type ForwardedRef,
} from "react";
import {
  Virtuoso,
  type Components,
  type ScrollerProps,
  type VirtuosoHandle,
} from "react-virtuoso";
import type { ChatMessage } from "../../types/chat";
import { MessageItem } from "./MessageItem";
import { MessageErrorBoundary } from "./MessageErrorBoundary";
import type { A2UISurfaceState, UserAction } from "../canvas";
import { GobbyLogo } from "../shared/GobbyLogo";

interface MessageListProps {
  messages: ChatMessage[];
  isStreaming: boolean;
  isThinking: boolean;
  isLoadingMessages?: boolean;
  onRespondToQuestion?: (
    toolCallId: string,
    answers: Record<string, string>,
  ) => boolean | void;
  onRespondToApproval?: (
    toolCallId: string,
    decision: "approve" | "reject" | "approve_always",
  ) => boolean | void;
  canvasSurfaces?: Map<string, A2UISurfaceState>;
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void;
}

export interface MessageListHandle {
  scrollToBottom: () => void;
}

export const MessageList = forwardRef<MessageListHandle, MessageListProps>(
  function MessageList(
    {
      messages,
      isStreaming,
      isThinking,
      isLoadingMessages,
      onRespondToQuestion,
      onRespondToApproval,
      canvasSurfaces,
      onCanvasInteraction,
    },
    ref,
  ) {
    const virtuosoRef = useRef<VirtuosoHandle>(null);
    const scrollerRef = useRef<HTMLDivElement | null>(null);
    const userScrolledUpRef = useRef(false);
    const pendingScrollFrameRef = useRef<number | null>(null);

    const setScrollerRef = useCallback(
      (
        node: HTMLDivElement | null,
        forwardedRef: ForwardedRef<HTMLDivElement>,
      ) => {
        scrollerRef.current = node;
        if (typeof forwardedRef === "function") {
          forwardedRef(node);
        } else if (forwardedRef) {
          forwardedRef.current = node;
        }
      },
      [],
    );

    const scrollScrollerToBottom = useCallback(() => {
      const scroller = scrollerRef.current;
      if (!scroller) return false;
      scroller.style.scrollBehavior = "auto";
      scroller.scrollTop = scroller.scrollHeight;
      return true;
    }, []);

    const scheduleScrollScrollerToBottom = useCallback(() => {
      if (
        typeof window === "undefined" ||
        typeof window.requestAnimationFrame !== "function"
      ) {
        scrollScrollerToBottom();
        return;
      }
      if (pendingScrollFrameRef.current !== null) {
        window.cancelAnimationFrame(pendingScrollFrameRef.current);
      }
      pendingScrollFrameRef.current = window.requestAnimationFrame(() => {
        scrollScrollerToBottom();
        pendingScrollFrameRef.current = window.requestAnimationFrame(() => {
          pendingScrollFrameRef.current = null;
          scrollScrollerToBottom();
        });
      });
    }, [scrollScrollerToBottom]);

    useLayoutEffect(
      () => () => {
        if (pendingScrollFrameRef.current !== null) {
          window.cancelAnimationFrame(pendingScrollFrameRef.current);
        }
      },
      [],
    );

    useImperativeHandle(
      ref,
      () => ({
        scrollToBottom() {
          userScrolledUpRef.current = false;
          scrollScrollerToBottom();
          virtuosoRef.current?.scrollToIndex({
            index: "LAST",
            behavior: "auto",
            align: "end",
          });
          scheduleScrollScrollerToBottom();
        },
      }),
      [scrollScrollerToBottom, scheduleScrollScrollerToBottom],
    );

    const handleAtBottomStateChange = useCallback(
      (atBottom: boolean) => {
        // Don't flip the flag during streaming — content growth can briefly
        // push us past atBottomThreshold before followOutput scrolls back,
        // which causes the "bounce" where auto-scroll stops mid-stream.
        if (!isStreaming) {
          userScrolledUpRef.current = !atBottom;
        }
      },
      [isStreaming],
    );

    // Reset scroll flag when streaming starts so stale scroll-up state
    // from before the agent began doesn't prevent auto-scroll.
    useLayoutEffect(() => {
      if (isStreaming) {
        userScrolledUpRef.current = false;
      }
    }, [isStreaming]);

    // Scroll to bottom on non-streaming appends (e.g. loading history).
    // During streaming, Virtuoso's followOutput handles scroll-following
    // natively. Use instant jumps here to avoid animation bounce when content
    // is appended in rapid chunks.
    useLayoutEffect(() => {
      if (!isStreaming && !userScrolledUpRef.current && messages.length > 0) {
        scrollScrollerToBottom();
        virtuosoRef.current?.scrollToIndex({
          index: "LAST",
          behavior: "auto",
          align: "end",
        });
        scheduleScrollScrollerToBottom();
      }
    }, [
      messages.length,
      isStreaming,
      scheduleScrollScrollerToBottom,
      scrollScrollerToBottom,
    ]);

    const latestMessage = messages[messages.length - 1];
    const latestContentLength = latestMessage?.content.length ?? 0;
    const latestThinkingLength = latestMessage?.thinkingContent?.length ?? 0;
    const latestToolActivityKey = useMemo(
      () =>
        (latestMessage?.toolCalls ?? [])
          .map((toolCall) => {
            const result = toolCall.result;
            const resultSize =
              typeof result?.content === "string"
                ? result.content.length
                : result
                  ? 1
                  : 0;
            return [
              toolCall.id,
              toolCall.status,
              toolCall.error ?? "",
              result?.kind ?? "",
              result?.truncated ? "truncated" : "full",
              resultSize,
            ].join(":");
          })
          .join("|"),
      [latestMessage?.toolCalls],
    );
    const latestContentBlockCount = latestMessage?.contentBlocks?.length ?? 0;

    useLayoutEffect(() => {
      if (!isStreaming || userScrolledUpRef.current || messages.length === 0) {
        return;
      }
      scrollScrollerToBottom();
      scheduleScrollScrollerToBottom();
    }, [
      isStreaming,
      latestContentLength,
      latestThinkingLength,
      latestToolActivityKey,
      latestContentBlockCount,
      messages.length,
      scheduleScrollScrollerToBottom,
      scrollScrollerToBottom,
    ]);

    const Footer = useCallback(
      () => (
        <>
          {isThinking &&
            (messages.length === 0 ||
              messages[messages.length - 1].role === "user") && (
              <ThinkingIndicator />
            )}
        </>
      ),
      [isThinking, messages],
    );

    // Stable reference for itemContent to avoid Virtuoso re-renders
    const itemContent = useCallback(
      (index: number, message: ChatMessage) => (
        <MessageErrorBoundary key={message.id} messageId={message.id}>
          <MessageItem
            message={message}
            isStreaming={isStreaming && index === messages.length - 1}
            isThinking={isThinking && index === messages.length - 1}
            onRespondToQuestion={onRespondToQuestion}
            onRespondToApproval={onRespondToApproval}
            canvasSurfaces={canvasSurfaces}
            onCanvasInteraction={onCanvasInteraction}
          />
        </MessageErrorBoundary>
      ),
      [
        isStreaming,
        isThinking,
        messages.length,
        onRespondToQuestion,
        onRespondToApproval,
        canvasSurfaces,
        onCanvasInteraction,
      ],
    );

    const Scroller = useMemo<NonNullable<Components<ChatMessage>["Scroller"]>>(
      () =>
        forwardRef<HTMLDivElement, ScrollerProps>(function MessageScroller(
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

    const virtuosoComponents = useMemo<Components<ChatMessage>>(
      () => ({ Footer, Scroller }),
      [Footer, Scroller],
    );

    if (messages.length === 0 && !isThinking) {
      return (
        <div className="chat-scaled flex-1 min-h-0 flex items-center justify-center">
          <div className="chat-empty-state">
            {isLoadingMessages ? (
              <p className="text-sm animate-pulse">Loading messages...</p>
            ) : (
              <>
                <div className="chat-empty-state__icon" aria-hidden="true">
                  <ChatEmptyIcon />
                </div>
                <div className="chat-empty-state__title">Chat</div>
                <p className="chat-empty-state__copy">
                  Start a conversation with Gobby
                </p>
              </>
            )}
          </div>
        </div>
      );
    }

    return (
      <Virtuoso
        ref={virtuosoRef}
        className="chat-scaled flex-1 min-h-0 overflow-x-hidden overscroll-contain [overflow-anchor:none] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [scrollbar-width:thin] [scrollbar-color:var(--border)_transparent]"
        data={messages}
        itemContent={itemContent}
        followOutput={() => {
          // Layout effects keep the scroller pinned before paint; disabling
          // Virtuoso's own follow pass avoids a visible correction frame.
          return false;
        }}
        atBottomThreshold={400}
        atBottomStateChange={handleAtBottomStateChange}
        overscan={400}
        increaseViewportBy={200}
        components={virtuosoComponents}
      />
    );
  },
);

function ChatEmptyIcon() {
  return (
    <svg
      width="48"
      height="48"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="opacity-30"
    >
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function ThinkingIndicator() {
  return (
    <div className="px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center gap-2 mb-1.5">
          <GobbyLogo label="App logo" className="rounded" />
          <span className="text-xs font-medium text-muted-foreground">
            Gobby
          </span>
        </div>
        <div className="flex items-center gap-2 py-2">
          <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-muted-foreground">Thinking...</span>
        </div>
      </div>
    </div>
  );
}
