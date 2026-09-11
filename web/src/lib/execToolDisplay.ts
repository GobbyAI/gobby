import { parse } from "acorn";
import type { ToolCall } from "../types/chat";
import { classifyTool } from "../types/chat";

type AstNode = Record<string, unknown>;
function node(value: unknown): value is AstNode {
  return typeof value === "object" && value !== null && "type" in value;
}
function literal(value: unknown): unknown {
  if (!node(value)) return undefined;
  if (value.type === "Literal") return value.value;
  if (value.type === "ObjectExpression" && Array.isArray(value.properties)) {
    const result: Record<string, unknown> = Object.create(null);
    for (const property of value.properties) {
      if (!node(property) || property.type !== "Property" || property.computed)
        continue;
      const key = node(property.key)
        ? (property.key.name ?? property.key.value)
        : undefined;
      const item = literal(property.value);
      if (typeof key === "string" && item !== undefined) result[key] = item;
    }
    return result;
  }
  if (
    value.type === "TemplateLiteral" &&
    Array.isArray(value.expressions) &&
    value.expressions.length === 0 &&
    Array.isArray(value.quasis)
  ) {
    const first = value.quasis[0];
    if (
      node(first) &&
      typeof first.value === "object" &&
      first.value !== null &&
      "cooked" in first.value
    )
      return first.value.cooked;
  }
  return undefined;
}
interface NestedCall {
  name: string;
  arguments: Record<string, unknown>;
}
function nestedCalls(source: string): NestedCall[] {
  const calls: NestedCall[] = [];
  let tree: unknown;
  try {
    tree = parse(source, {
      ecmaVersion: "latest",
      sourceType: "module",
      allowAwaitOutsideFunction: true,
    });
  } catch {
    return calls;
  }
  function visit(value: unknown): void {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!node(value)) return;
    const callee = value.callee;
    if (
      value.type === "CallExpression" &&
      node(callee) &&
      callee.type === "MemberExpression" &&
      node(callee.object) &&
      callee.object.type === "Identifier" &&
      callee.object.name === "tools" &&
      node(callee.property)
    ) {
      const name = callee.computed
        ? literal(callee.property)
        : callee.property.name;
      const args: unknown = Array.isArray(value.arguments)
        ? literal(value.arguments[0])
        : undefined;
      if (typeof name === "string")
        calls.push({
          name,
          arguments:
            typeof args === "object" && args !== null
              ? Object.fromEntries(Object.entries(args))
              : {},
        });
    }
    Object.values(value).forEach(visit);
  }
  visit(tree);
  return calls;
}

// Both chat and the transcript viewer use this display projection. The original
// wrapper's result and status remain attached to its single card.
export function normalizeToolCallForDisplay(call: ToolCall): ToolCall {
  const name = call.tool_name.replace(/^functions\./, "");
  if (name === "exec_command")
    return { ...call, tool_name: "exec_command", tool_type: "bash" };
  if (name !== "exec") return call;
  const source =
    call.arguments?.raw ?? call.arguments?.code ?? call.arguments?.input;
  if (typeof source !== "string") return call;
  const nested = nestedCalls(source);
  if (!nested.length) return call;
  if (nested.length > 1)
    return {
      ...call,
      tool_name: "Tools",
      tool_type: "unknown",
      arguments: { calls: nested, wrapper: source },
    };
  const inner = nested[0];
  let innerName = inner.name.replace(/^functions\./, "");
  let args = inner.arguments;
  if (
    innerName.endsWith("__call_tool") &&
    typeof args.server_name === "string" &&
    typeof args.tool_name === "string"
  ) {
    innerName = `mcp__${args.server_name}__${args.tool_name}`;
    const innerArgs = args.arguments ?? args.args;
    if (typeof innerArgs === "object" && innerArgs !== null)
      args = Object.fromEntries(Object.entries(innerArgs));
  }
  return {
    ...call,
    tool_name: innerName,
    tool_type: classifyTool(innerName),
    arguments: args,
  };
}
