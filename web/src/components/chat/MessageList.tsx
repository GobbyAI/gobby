import {
  memo,
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
}

export interface MessageListHandle {
  scrollToBottom: () => void;
}

// Virtuoso can keep reporting off-bottom / height growth while pinned. Chase
// that for a bounded number of frames, then release so rest-idle cannot rAF
// forever when at-bottom never settles.
export const MESSAGE_LIST_PIN_CHASE_MAX_FRAMES = 40;

// Memoized so a WebSocket-driven re-render of an ancestor (usage/activity
// bursts) doesn't re-render the whole transcript when its own props are
// unchanged.
export const MessageList = memo(
  forwardRef<MessageListHandle, MessageListProps>(function MessageList(
    {
      messages,
      isStreaming,
      isThinking,
      isLoadingMessages,
      onRespondToQuestion,
      onRespondToApproval,
    },
    ref,
  ) {
    const virtuosoRef = useRef<VirtuosoHandle>(null);
    const scrollerRef = useRef<HTMLDivElement | null>(null);
    const userScrolledUpRef = useRef(false);
    const pendingScrollFrameRef = useRef<number | null>(null);
    // Loading a transcript scrolls to bottom once, but Virtuoso measures tall
    // items progressively — the scroll height keeps growing afterwards. Pin
    // the scroller to the bottom until it first actually reports at-bottom;
    // otherwise a paused session's history lands mid-list with a blank
    // estimated-height region above, and the layout shift latches
    // userScrolledUpRef and suppresses every later auto-scroll.
    const pinToBottomRef = useRef(false);
    const pinChaseFramesRef = useRef(0);

    const beginPinnedBottom = useCallback(() => {
      pinToBottomRef.current = true;
      pinChaseFramesRef.current = 0;
    }, []);

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

    const chasePinnedBottom = useCallback(() => {
      if (!pinToBottomRef.current) return;
      if (pinChaseFramesRef.current >= MESSAGE_LIST_PIN_CHASE_MAX_FRAMES) {
        pinToBottomRef.current = false;
        return;
      }
      pinChaseFramesRef.current += 1;
      scheduleScrollScrollerToBottom();
    }, [scheduleScrollScrollerToBottom]);

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
          beginPinnedBottom();
          scrollScrollerToBottom();
          virtuosoRef.current?.scrollToIndex({
            index: "LAST",
            behavior: "auto",
            align: "end",
          });
          scheduleScrollScrollerToBottom();
        },
      }),
      [beginPinnedBottom, scrollScrollerToBottom, scheduleScrollScrollerToBottom],
    );

    const handleAtBottomStateChange = useCallback(
      (atBottom: boolean) => {
        if (atBottom) {
          pinToBottomRef.current = false;
          pinChaseFramesRef.current = 0;
          userScrolledUpRef.current = false;
          return;
        }
        // While pinned, off-bottom reports are programmatic layout shifts
        // from progressive measurement, never user intent — re-bottom.
        if (pinToBottomRef.current) {
          chasePinnedBottom();
          return;
        }
        // Don't flip the flag during streaming — content growth can briefly
        // push us past atBottomThreshold before followOutput scrolls back,
        // which causes the "bounce" where auto-scroll stops mid-stream.
        if (!isStreaming) {
          userScrolledUpRef.current = true;
        }
      },
      [chasePinnedBottom, isStreaming],
    );

    // Progressive item measurement grows the list height after the initial
    // scroll; while pinned, chase the growth so the transcript stays bottomed.
    const handleTotalListHeightChanged = useCallback(() => {
      if (pinToBottomRef.current) {
        chasePinnedBottom();
      }
    }, [chasePinnedBottom]);

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
        beginPinnedBottom();
        scrollScrollerToBottom();
        virtuosoRef.current?.scrollToIndex({
          index: "LAST",
          behavior: "auto",
          align: "end",
        });
        scheduleScrollScrollerToBottom();
      }
    }, [
      beginPinnedBottom,
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

    const lastMessageRole = messages[messages.length - 1]?.role;
    const Footer = useCallback(
      () => (
        <>
          {isThinking &&
            (messages.length === 0 || lastMessageRole === "user") && (
              <ThinkingIndicator />
            )}
        </>
      ),
      [isThinking, lastMessageRole, messages.length],
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
          />
        </MessageErrorBoundary>
      ),
      [
        isStreaming,
        isThinking,
        messages.length,
        onRespondToQuestion,
        onRespondToApproval,
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
        <div className="flex min-h-0 flex-1 items-center justify-center [&_.text-sm]:text-[length:var(--text-base)] [&_.text-xs]:text-[length:var(--text-sm)]">
          <div className="chat-empty-state flex flex-col items-center gap-3 text-center text-[var(--text-muted)]">
            {isLoadingMessages ? (
              <p className="animate-pulse text-sm">Loading messages...</p>
            ) : (
              <>
                <div
                  className="chat-empty-state__icon inline-flex items-center justify-center opacity-[0.35]"
                  aria-hidden="true"
                >
                  <ChatEmptyIcon />
                </div>
                <div className="chat-empty-state__title text-[length:var(--text-xl)] text-[var(--text-secondary)]">
                  Chat
                </div>
                <p className="chat-empty-state__copy max-w-[26rem] text-[length:var(--text-base)] text-[var(--text-muted)]">
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
        className="min-h-0 flex-1 [scrollbar-width:thin] [scrollbar-color:var(--border)_transparent] overflow-x-hidden overscroll-contain [overflow-anchor:none] [&_.text-sm]:text-[length:var(--text-base)] [&_.text-xs]:text-[length:var(--text-sm)] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent"
        data={messages}
        computeItemKey={(_, message) => message.id}
        itemContent={itemContent}
        followOutput={() => {
          // Layout effects keep the scroller pinned before paint; disabling
          // Virtuoso's own follow pass avoids a visible correction frame.
          return false;
        }}
        atBottomThreshold={400}
        atBottomStateChange={handleAtBottomStateChange}
        totalListHeightChanged={handleTotalListHeightChanged}
        overscan={400}
        increaseViewportBy={200}
        components={virtuosoComponents}
      />
    );
  }),
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
      <div className="mx-auto max-w-3xl">
        <div className="mb-1.5 flex items-center gap-2">
          <GobbyLogo label="App logo" className="rounded" />
          <span className="text-xs font-medium text-muted-foreground">
            Gobby
          </span>
        </div>
        <div className="flex items-center gap-2 py-2">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          <span className="text-sm text-muted-foreground">Thinking...</span>
        </div>
      </div>
    </div>
  );
}
