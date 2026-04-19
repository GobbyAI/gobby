import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

import { useTokenEventsStream } from "../useTokenEventsStream";
import type { TokenEvent } from "../../types/tokens";

describe("useTokenEventsStream", () => {
  it("dedupes appended events against the latest state", () => {
    const event: TokenEvent = {
      session_id: "sess-1",
      event_at: "2026-04-08T12:00:00Z",
      message_id: "msg-1",
      model: "claude-sonnet-4",
      model_family: "claude",
      source: "claude",
      origin: "transcript",
      input_tokens: 10,
      output_tokens: 5,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
    };

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
});
