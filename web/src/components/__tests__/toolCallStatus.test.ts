import { describe, expect, it } from "vitest";

import {
  getToolCallError,
  isSuccessfulToolCall,
} from "../activity/toolCallStatus";

describe("toolCallStatus", () => {
  it("treats normalized internal tool success as success", () => {
    expect(
      isSuccessfulToolCall({
        success: true,
        result: {},
      }),
    ).toBe(true);
  });

  it("preserves nested tool failures", () => {
    expect(
      isSuccessfulToolCall({
        success: true,
        result: { success: false, error: "tmux send-keys failed" },
      }),
    ).toBe(false);
    expect(
      getToolCallError(
        {
          success: true,
          result: { success: false, error: "tmux send-keys failed" },
        },
        "Operation failed",
      ),
    ).toBe("tmux send-keys failed");
  });

  it("falls back to the provided message when the tool response is opaque", () => {
    expect(getToolCallError({ success: false }, "Operation failed")).toBe(
      "Operation failed",
    );
  });
});
