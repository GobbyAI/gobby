import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

import { useTokenEventsStream } from "../useTokenEventsStream";
import type { TokenEvent } from "../../types/tokens";
import { useWebSocketEvent } from "../useWebSocketEvent";

const mockUseWebSocketEvent = vi.mocked(useWebSocketEvent);

function makeEvent(
  overrides: Partial<TokenEvent> = {},
): TokenEvent {
  return {
    session_id: "sess-1",
    event_at: "2026-04-08T12:00:00Z",
    message_id: null,
    model: "claude-sonnet-4",
    model_family: "claude",
    source: "claude",
    origin: "transcript",
    input_tokens: 10,
    output_tokens: 5,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    ...overrides,
  };
}

function toWebSocketPayload(event: TokenEvent): Record<string, unknown> {
  return { ...event };
}

describe("useTokenEventsStream", () => {
  beforeEach(() => {
    mockUseWebSocketEvent.mockReset();
  });

  it("starts with an empty event list", () => {
    const { result } = renderHook(() =>
      useTokenEventsStream({ sessionId: "sess-1", limit: 50 }),
    );

    expect(result.current.events).toEqual([]);
  });

  it("returns a stable shared empty array when the viewed session changes", () => {
    const { result, rerender } = renderHook(
      ({ sessionId }) => useTokenEventsStream({ sessionId, limit: 50 }),
      {
        initialProps: { sessionId: "sess-1" },
      },
    );

    rerender({ sessionId: "sess-2" });
    const firstSharedEmpty = result.current.events;

    rerender({ sessionId: "sess-3" });

    expect(result.current.events).toBe(firstSharedEmpty);
  });

  it("dedupes appended events against the latest state", () => {
    const event = makeEvent({ message_id: "msg-1" });

    const { result } = renderHook(() =>
      useTokenEventsStream({ sessionId: "sess-1", limit: 50 }),
    );

    act(() => {
      result.current.setEvents([event]);
    });

    act(() => {
      result.current.appendEvent(event);
    });

    expect(result.current.events).toEqual([event]);
  });

  it("appends multiple unique events in descending timestamp order", () => {
    const older = makeEvent({
      event_at: "2026-04-08T12:00:00Z",
      message_id: "msg-1",
    });
    const newer = makeEvent({
      event_at: "2026-04-08T12:01:00Z",
      message_id: "msg-2",
    });

    const { result } = renderHook(() =>
      useTokenEventsStream({ sessionId: "sess-1", limit: 50 }),
    );

    act(() => {
      result.current.appendEvent(older);
      result.current.appendEvent(newer);
    });

    expect(result.current.events).toEqual([newer, older]);
  });

  it("truncates to the latest N events", () => {
    const first = makeEvent({
      event_at: "2026-04-08T12:00:00Z",
      message_id: "msg-1",
    });
    const second = makeEvent({
      event_at: "2026-04-08T12:01:00Z",
      message_id: "msg-2",
    });
    const third = makeEvent({
      event_at: "2026-04-08T12:02:00Z",
      message_id: "msg-3",
    });

    const { result } = renderHook(() =>
      useTokenEventsStream({ sessionId: "sess-1", limit: 2 }),
    );

    act(() => {
      result.current.appendEvent(first);
      result.current.appendEvent(second);
      result.current.appendEvent(third);
    });

    expect(result.current.events).toEqual([third, second]);
  });

  it("handles websocket-driven updates with the same dedupe rules", () => {
    let callback: ((data: Record<string, unknown>) => void) | undefined;
    mockUseWebSocketEvent.mockImplementation((_eventType, handler) => {
      callback = handler;
    });

    const first = makeEvent({
      event_at: "2026-04-08T12:00:00Z",
      message_id: "msg-1",
    });
    const duplicate = { ...first };
    const second = makeEvent({
      event_at: "2026-04-08T12:01:00Z",
      message_id: "msg-2",
    });

    const { result } = renderHook(() =>
      useTokenEventsStream({ sessionId: "sess-1", limit: 50 }),
    );

    expect(callback).toBeTypeOf("function");

    act(() => {
      callback?.(toWebSocketPayload(first));
      callback?.(toWebSocketPayload(duplicate));
      callback?.(toWebSocketPayload(second));
    });

    expect(result.current.events).toEqual([
      { ...second, context_window: null, project_id: null, session_totals: undefined },
      { ...first, context_window: null, project_id: null, session_totals: undefined },
    ]);
  });

  it("normalizes string numeric fields from websocket events", () => {
    let callback: ((data: Record<string, unknown>) => void) | undefined;
    mockUseWebSocketEvent.mockImplementation((_eventType, handler) => {
      callback = handler;
    });

    const { result } = renderHook(() =>
      useTokenEventsStream({ sessionId: "sess-1", limit: 50 }),
    );

    act(() => {
      callback?.({
        session_id: "sess-1",
        event_at: "2026-04-08T12:00:00Z",
        input_tokens: "128",
        output_tokens: "11",
        cache_creation_tokens: "8",
        cache_read_tokens: "20",
        context_window: "1000",
        session_totals: {
          input_tokens: "256",
          output_tokens: "22",
          cache_creation_tokens: "16",
          cache_read_tokens: "40",
        },
      });
    });

    expect(result.current.events[0]).toMatchObject({
      input_tokens: 128,
      output_tokens: 11,
      cache_creation_tokens: 8,
      cache_read_tokens: 20,
      context_window: 1000,
      session_totals: {
        input_tokens: 256,
        output_tokens: 22,
        cache_creation_tokens: 16,
        cache_read_tokens: 40,
      },
    });
  });
});
