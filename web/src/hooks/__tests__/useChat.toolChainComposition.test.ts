import { describe, expect, it } from "vitest";

import { appendToolBlock, appendTextBlock } from "../useChat/core";
import type { ChatMessage, ToolCall } from "../../types/chat";

function makeMsg(): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "",
    timestamp: new Date(),
  };
}

function makeTool(id: string, name = "Read"): ToolCall {
  return {
    id,
    tool_name: name,
    server_name: "builtin",
    tool_type: "read",
    status: "completed",
  };
}

describe("live-stream tool_chain composition (item 11b)", () => {
  it("emits one tool_chain block per tool call so consecutive calls do not clump", () => {
    const msg = makeMsg();
    appendToolBlock(msg, makeTool("t1"));
    appendToolBlock(msg, makeTool("t2"));
    appendToolBlock(msg, makeTool("t3"));

    expect(msg.contentBlocks).toHaveLength(3);
    expect(msg.contentBlocks?.every(b => b.type === "tool_chain")).toBe(true);
    expect(
      (msg.contentBlocks ?? []).map(b =>
        b.type === "tool_chain" ? b.tool_calls.length : -1,
      ),
    ).toEqual([1, 1, 1]);
  });

  it("interleaves text + tool blocks correctly (each block in its own slot)", () => {
    const msg = makeMsg();
    appendTextBlock(msg, "First text");
    appendToolBlock(msg, makeTool("t1"));
    appendToolBlock(msg, makeTool("t2"));
    appendTextBlock(msg, "Second text");
    appendToolBlock(msg, makeTool("t3"));

    expect(msg.contentBlocks?.map(b => b.type)).toEqual([
      "text",
      "tool_chain",
      "tool_chain",
      "text",
      "tool_chain",
    ]);
  });
});
