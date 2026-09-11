import { describe, expect, it } from "vitest";
import { normalizeToolCallForDisplay } from "../execToolDisplay";
import {
  getToolDisplayName,
  getToolSummary,
  resolveToolType,
} from "../../components/chat/ToolCallCard.helpers";
import type { ToolCall } from "../../types/chat";

const call: ToolCall = {
  id: "outer",
  tool_name: "exec",
  tool_type: "unknown",
  server_name: "builtin",
  status: "completed",
  result: { content: "done", kind: "text", truncated: false },
};
describe("Codex tool display", () => {
  it("uses Claude's Bash name and command summary for nested shell calls", () => {
    const projected = normalizeToolCallForDisplay({
      ...call,
      arguments: {
        raw: 'text(await tools.exec_command({cmd:"git status --short", max_output_tokens: 2000}));',
      },
    });
    expect(getToolDisplayName(projected)).toBe("Bash");
    expect(getToolSummary(projected)).toBe("git status --short");
    expect(resolveToolType(projected)).toBe("bash");
    expect(projected.result).toBe(call.result);
    expect(projected.id).toBe("outer");
  });
  it("uses the same Bash presentation for direct namespaced exec_command", () => {
    const projected = normalizeToolCallForDisplay({
      ...call,
      tool_name: "functions.exec_command",
      arguments: { cmd: "pwd" },
    });
    expect(getToolDisplayName(projected)).toBe("Bash");
    expect(getToolSummary(projected)).toBe("pwd");
  });
  it("reveals routed MCP tool identity and arguments", () => {
    const projected = normalizeToolCallForDisplay({
      ...call,
      arguments: {
        raw: 'text(await tools.mcp__gobby__call_tool({server_name:"gobby-tasks",tool_name:"create_task",arguments:{title:"Fix labels"}}));',
      },
    });
    expect(projected.tool_name).toBe("mcp__gobby-tasks__create_task");
    expect(projected.arguments).toEqual({ title: "Fix labels" });
    expect(getToolDisplayName(projected)).toBe("create_task");
  });
  it("does not mistake strings and comments for executed tools", () => {
    const original = {
      ...call,
      arguments: {
        raw: 'text("tools.exec_command({cmd: 1})"); // tools.fake()',
      },
    };
    expect(normalizeToolCallForDisplay(original)).toBe(original);
  });
  it("retains a single wrapper result for multiple nested calls", () => {
    const projected = normalizeToolCallForDisplay({
      ...call,
      arguments: {
        raw: 'await Promise.all([tools.exec_command({cmd:"pwd"}), tools.web__run({})]);',
      },
    });
    expect(projected.tool_name).toBe("Tools");
    expect(projected.arguments?.calls).toEqual([
      { name: "exec_command", arguments: { cmd: "pwd" } },
      { name: "web__run", arguments: {} },
    ]);
    expect(projected.result).toBe(call.result);
  });
  it("retains malformed source without crashing the transcript", () => {
    const original = { ...call, arguments: { raw: "await tools." } };
    expect(normalizeToolCallForDisplay(original)).toBe(original);
  });
});
