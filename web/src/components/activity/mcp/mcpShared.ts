import type { McpServer } from "../../../hooks/useMcp";
import type { StatusKind } from "../ActivityRowStatusDot";

export type McpServerType = "internal" | "external";

// A tool selection is a single unified view: schema + arguments + a Call Tool
// action live together in the detail panel (no separate schema/execute modes).
export type McpSelection =
  | { kind: "server"; serverName: string }
  | { kind: "tool"; serverName: string; toolName: string };

export interface McpContextMenu {
  x: number;
  y: number;
  kind: "server" | "tool";
  serverName: string;
  toolName?: string;
  isExternal?: boolean;
  enabled?: boolean;
}

// Server-type filter values for the toolbar SegmentedControl. "all" is the
// default — MCP panels are usually scanned as a whole, unlike Sessions'
// one-or-the-other Active|Expired split.
export type McpTypeFilter = "all" | McpServerType;

export function getServerType(server: McpServer): McpServerType {
  return server.transport === "internal" ? "internal" : "external";
}

export function healthToStatusKind(health: string): StatusKind {
  switch (health) {
    case "healthy":
      return "success";
    case "degraded":
      return "warning";
    case "unhealthy":
    case "failed":
      return "error";
    default:
      return "info";
  }
}
