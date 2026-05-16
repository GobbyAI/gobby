import * as React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../../../types/chat";
import { MessageList, type MessageListHandle } from "../MessageList";

const { scrollToIndexMock, virtuosoProps } = vi.hoisted(() => ({
  scrollToIndexMock: vi.fn(),
  virtuosoProps: [] as Array<{
    className?: string;
    followOutput?: () => "auto" | "smooth" | false;
  }>,
}));

vi.mock("react-virtuoso", async () => {
  const ReactModule = await import("react");

  return {
    Virtuoso: ReactModule.forwardRef(
      (
        {
          className,
          data,
          followOutput,
          itemContent,
          components,
        }: {
          className?: string;
          data: ChatMessage[];
          followOutput?: () => "auto" | "smooth" | false;
          itemContent: (index: number, message: ChatMessage) => React.ReactNode;
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
        virtuosoProps.push({ className, followOutput });
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

describe("MessageList", () => {
  beforeEach(() => {
    scrollToIndexMock.mockClear();
    virtuosoProps.length = 0;
  });

  it("renders empty-state copy without a terminal period", () => {
    render(
      <MessageList
        messages={[]}
        isStreaming={false}
        isThinking={false}
      />,
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
});
