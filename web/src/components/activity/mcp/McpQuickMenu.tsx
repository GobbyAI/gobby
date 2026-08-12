import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import type { McpContextMenu } from "./mcpShared";

export interface McpQuickMenuProps {
  menu: McpContextMenu;
  onClose: () => void;
  onViewSchema: () => void;
  onCallTool: () => void;
  onViewServer: () => void;
  onRefreshServer: () => void;
  onToggleEnabled: () => void;
  onRemoveServer: () => void;
}

export function McpQuickMenu({
  menu,
  onClose,
  onViewSchema,
  onCallTool,
  onViewServer,
  onRefreshServer,
  onToggleEnabled,
  onRemoveServer,
}: McpQuickMenuProps) {
  const items: QuickMenuItem[] =
    menu.kind === "tool"
      ? [
          { label: "View schema", onSelect: onViewSchema },
          { label: "Call Tool", onSelect: onCallTool },
        ]
      : [
          { label: "View details", onSelect: onViewServer },
          { label: "Refresh tools", onSelect: onRefreshServer },
          ...(menu.isExternal
            ? [
                {
                  label:
                    menu.enabled === false ? "Enable server" : "Disable server",
                  onSelect: onToggleEnabled,
                },
                {
                  label: "Remove server...",
                  onSelect: onRemoveServer,
                  destructive: true,
                },
              ]
            : []),
        ];

  return (
    <QuickMenu
      anchor={{
        x: menu.x,
        y: menu.y,
        width: menu.width ?? 0,
        height: menu.height ?? 0,
      }}
      menuLabel={
        menu.kind === "tool" ? "MCP tool actions" : "MCP server actions"
      }
      items={items}
      onClose={onClose}
    />
  );
}
