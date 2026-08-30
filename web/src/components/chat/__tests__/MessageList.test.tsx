import * as React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../../../types/chat";
import {
  MESSAGE_LIST_PIN_CHASE_MAX_FRAMES,
  MessageList,
  type MessageListHandle,
} from "../MessageList";

const { scrollToIndexMock, virtuosoProps } = vi.hoisted(() => ({
  scrollToIndexMock: vi.fn(),
  virtuosoProps: [] as Array<{
    className?: string;
    computeItemKey?: (index: number, message: ChatMessage) => React.Key;
    followOutput?: () => "auto" | "smooth" | false;
    atBottomStateChange?: (atBottom: boolean) => void;
    totalListHeightChanged?: (height: number) => void;
  }>,
}));

vi.mock("react-virtuoso", async () => {
  const ReactModule = await import("react");

  return {
    Virtuoso: ReactModule.forwardRef(
      (
        {
          className,
          computeItemKey,
          data,
          followOutput,
          itemContent,
          components,
          atBottomStateChange,
          totalListHeightChanged,
        }: {
          className?: string;
          computeItemKey?: (index: number, message: ChatMessage) => React.Key;
          data: ChatMessage[];
          followOutput?: () => "auto" | "smooth" | false;
          itemContent: (index: number, message: ChatMessage) => React.ReactNode;
          atBottomStateChange?: (atBottom: boolean) => void;
          totalListHeightChanged?: (height: number) => void;
          components?: {
            Footer?: React.ComponentType;
            Scroller?: React.ComponentType<{
              children?: React.ReactNode;
              className?: string;
              "data-testid"?: string;
              style?: React.CSSProperties;
            }>;
          };
        },
        ref: React.ForwardedRef<{ scrollToIndex: typeof scrollToIndexMock }>,
      ) => {
        ReactModule.useImperativeHandle(
          ref,
          () => ({
            scrollToIndex: scrollToIndexMock,
          }),
          [],
        );
        virtuosoProps.push({
          className,
          computeItemKey,
          followOutput,
          atBottomStateChange,
          totalListHeightChanged,
        });
        const Scroller = components?.Scroller ?? "div";
        const Footer = components?.Footer;
        return (
          <Scroller
            data-testid="virtuoso"
            className={className}
            style={{ overflowY: "auto" }}
          >
            {data.map((message, index) => (
              <div key={message.id}>{itemContent(index, message)}</div>
            ))}
            {Footer ? <Footer /> : null}
          </Scroller>
        );
      },
    ),
  };
});

vi.mock("../MessageItem", () => ({
  MessageItem: ({ message }: { message: ChatMessage }) => (
    <div data-testid="message-item">{message.content}</div>
  ),
}));

function message(id: string, content = "hello"): ChatMessage {
  return {
    id,
    role: "assistant",
    content,
    timestamp: new Date("2026-05-13T12:00:00Z"),
  };
}

function stubAnimationFrames() {
  const pending = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  const originalRaf = window.requestAnimationFrame;
  const originalCancel = window.cancelAnimationFrame;
  window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    const id = nextId++;
    pending.set(id, cb);
    return id;
  }) as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = ((id: number) => {
    pending.delete(id);
  }) as typeof window.cancelAnimationFrame;
  return {
    flushPending: () => {
      const batch = [...pending.values()];
      pending.clear();
      for (const cb of batch) cb(0);
    },
    restore: () => {
      window.requestAnimationFrame = originalRaf;
      window.cancelAnimationFrame = originalCancel;
    },
  };
}

// Drives enough off-bottom / height-growth reports through the pinned chase
// to exhaust MESSAGE_LIST_PIN_CHASE_MAX_FRAMES and keep reporting afterwards.
function exhaustPinChase(
  latestProps: () => (typeof virtuosoProps)[number] | undefined,
  flushPending: () => void,
) {
  for (let i = 0; i < MESSAGE_LIST_PIN_CHASE_MAX_FRAMES * 4; i += 1) {
    act(() => {
      latestProps()?.atBottomStateChange?.(false);
      latestProps()?.totalListHeightChanged?.(2_000 + i);
      flushPending();
    });
  }
}

describe("MessageList", () => {
  beforeEach(() => {
    scrollToIndexMock.mockClear();
    virtuosoProps.length = 0;
  });

  it("renders empty-state copy without a terminal period", () => {
    render(
      <MessageList messages={[]} isStreaming={false} isThinking={false} />,
    );

    expect(
      screen.getByText("Start a conversation with Gobby"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Start a conversation with Gobby."),
    ).not.toBeInTheDocument();
  });

  it("uses instant scroll for imperative bottom jumps", () => {
    const ref = React.createRef<MessageListHandle>();
    render(
      <MessageList
        ref={ref}
        messages={[message("m1")]}
        isStreaming={false}
        isThinking={false}
      />,
    );
    scrollToIndexMock.mockClear();

    act(() => {
      ref.current?.scrollToBottom();
    });

    expect(scrollToIndexMock).toHaveBeenCalledWith({
      index: "LAST",
      behavior: "auto",
      align: "end",
    });
  });

  it("uses instant scroll for non-streaming appends", async () => {
    render(
      <MessageList
        messages={[message("m1")]}
        isStreaming={false}
        isThinking={false}
      />,
    );

    await waitFor(() => {
      expect(scrollToIndexMock).toHaveBeenCalledWith({
        index: "LAST",
        behavior: "auto",
        align: "end",
      });
    });
  });

  it("keeps auto-scrolling after a pinned load despite layout-shift off-bottom reports", async () => {
    const ref = React.createRef<MessageListHandle>();
    const { rerender } = render(
      <MessageList
        ref={ref}
        messages={[message("m1")]}
        isStreaming={false}
        isThinking={false}
      />,
    );

    act(() => {
      ref.current?.scrollToBottom();
    });
    // Progressive measurement pushes the scroller off the bottom before it
    // ever settles — while pinned this is a layout shift, not user intent.
    const latestProps = () => virtuosoProps[virtuosoProps.length - 1];
    act(() => {
      latestProps()?.atBottomStateChange?.(false);
      latestProps()?.totalListHeightChanged?.(2400);
    });

    scrollToIndexMock.mockClear();
    rerender(
      <MessageList
        ref={ref}
        messages={[message("m1"), message("m2")]}
        isStreaming={false}
        isThinking={false}
      />,
    );

    await waitFor(() => {
      expect(scrollToIndexMock).toHaveBeenCalledWith({
        index: "LAST",
        behavior: "auto",
        align: "end",
      });
    });
  });

  it("respects a real scroll-up once the pinned load has settled at bottom", async () => {
    const ref = React.createRef<MessageListHandle>();
    const { rerender } = render(
      <MessageList
        ref={ref}
        messages={[message("m1")]}
        isStreaming={false}
        isThinking={false}
      />,
    );

    const latestProps = () => virtuosoProps[virtuosoProps.length - 1];
    act(() => {
      ref.current?.scrollToBottom();
    });
    // Settling at bottom releases the pin; the next off-bottom report is a
    // genuine user scroll and must latch.
    act(() => {
      latestProps()?.atBottomStateChange?.(true);
    });
    act(() => {
      latestProps()?.atBottomStateChange?.(false);
    });

    scrollToIndexMock.mockClear();
    rerender(
      <MessageList
        ref={ref}
        messages={[message("m1"), message("m2")]}
        isStreaming={false}
        isThinking={false}
      />,
    );

    expect(scrollToIndexMock).not.toHaveBeenCalled();
  });

  it("still follows streaming output without animated bounce", () => {
    render(
      <MessageList messages={[message("m1")]} isStreaming isThinking={false} />,
    );

    const latestProps = virtuosoProps[virtuosoProps.length - 1];
    const scroller = screen.getByTestId("virtuoso");

    expect(latestProps?.followOutput?.()).toBe(false);
    expect(latestProps?.className).toContain("overscroll-contain");
    expect(latestProps?.className).toContain("[overflow-anchor:none]");
    expect(scroller).toHaveStyle({
      scrollBehavior: "auto",
      overflowAnchor: "none",
      overscrollBehavior: "contain",
    });
  });

  it("stops pin-chasing after a bounded number of frames when at-bottom never settles", () => {
    const pending = new Map<number, FrameRequestCallback>();
    let nextId = 1;
    const originalRaf = window.requestAnimationFrame;
    const originalCancel = window.cancelAnimationFrame;
    window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      const id = nextId++;
      pending.set(id, cb);
      return id;
    }) as typeof window.requestAnimationFrame;
    window.cancelAnimationFrame = ((id: number) => {
      pending.delete(id);
    }) as typeof window.cancelAnimationFrame;

    try {
      const ref = React.createRef<MessageListHandle>();
      render(
        <MessageList
          ref={ref}
          messages={[message("m1")]}
          isStreaming={false}
          isThinking={false}
        />,
      );

      act(() => {
        ref.current?.scrollToBottom();
      });

      const latestProps = () => virtuosoProps[virtuosoProps.length - 1];
      const flushPending = () => {
        const batch = [...pending.values()];
        pending.clear();
        for (const cb of batch) cb(0);
      };

      let pumps = 0;
      act(() => {
        flushPending();
      });
      for (let i = 0; i < MESSAGE_LIST_PIN_CHASE_MAX_FRAMES * 4; i += 1) {
        const before = nextId;
        act(() => {
          latestProps()?.atBottomStateChange?.(false);
          latestProps()?.totalListHeightChanged?.(2_000 + i);
          flushPending();
        });
        pumps += 1;
        if (nextId === before) break;
      }

      expect(pumps).toBeLessThanOrEqual(MESSAGE_LIST_PIN_CHASE_MAX_FRAMES + 2);
      const rafAfterStop = nextId;
      act(() => {
        latestProps()?.atBottomStateChange?.(false);
        latestProps()?.totalListHeightChanged?.(9_999);
        flushPending();
      });
      expect(nextId).toBe(rafAfterStop);
    } finally {
      window.requestAnimationFrame = originalRaf;
      window.cancelAnimationFrame = originalCancel;
    }
  });

  it("still auto-scrolls appends after the chase cap exhausts without user input", async () => {
    const frames = stubAnimationFrames();
    try {
      const ref = React.createRef<MessageListHandle>();
      const { rerender } = render(
        <MessageList
          ref={ref}
          messages={[message("m1")]}
          isStreaming={false}
          isThinking={false}
        />,
      );
      act(() => {
        ref.current?.scrollToBottom();
      });
      const latestProps = () => virtuosoProps[virtuosoProps.length - 1];
      exhaustPinChase(latestProps, frames.flushPending);

      // Post-exhaustion off-bottom reports are still programmatic layout
      // shifts; they must not latch the scrolled-up flag.
      act(() => {
        latestProps()?.atBottomStateChange?.(false);
      });

      scrollToIndexMock.mockClear();
      rerender(
        <MessageList
          ref={ref}
          messages={[message("m1"), message("m2")]}
          isStreaming={false}
          isThinking={false}
        />,
      );
      await waitFor(() => {
        expect(scrollToIndexMock).toHaveBeenCalledWith({
          index: "LAST",
          behavior: "auto",
          align: "end",
        });
      });
    } finally {
      frames.restore();
    }
  });

  it("latches a real user scroll gesture after the chase cap exhausts", () => {
    const frames = stubAnimationFrames();
    try {
      const ref = React.createRef<MessageListHandle>();
      const { rerender } = render(
        <MessageList
          ref={ref}
          messages={[message("m1")]}
          isStreaming={false}
          isThinking={false}
        />,
      );
      act(() => {
        ref.current?.scrollToBottom();
      });
      const latestProps = () => virtuosoProps[virtuosoProps.length - 1];
      exhaustPinChase(latestProps, frames.flushPending);

      // The user grabs the wheel while the scroller sits off the bottom:
      // that is real intent, so later appends must not yank them back down.
      fireEvent.wheel(screen.getByTestId("virtuoso"), { deltaY: -120 });

      scrollToIndexMock.mockClear();
      rerender(
        <MessageList
          ref={ref}
          messages={[message("m1"), message("m2")]}
          isStreaming={false}
          isThinking={false}
        />,
      );
      expect(scrollToIndexMock).not.toHaveBeenCalled();
    } finally {
      frames.restore();
    }
  });

  it("keys virtualized messages by message id", () => {
    const firstMessage = message("m1");
    render(
      <MessageList
        messages={[firstMessage]}
        isStreaming={false}
        isThinking={false}
      />,
    );

    const latestProps = virtuosoProps[virtuosoProps.length - 1];
    expect(latestProps?.computeItemKey?.(0, firstMessage)).toBe("m1");
  });
});
