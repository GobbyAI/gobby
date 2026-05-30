import {
  useEffect,
  useRef,
  type CSSProperties,
  type KeyboardEvent,
} from "react";

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
  const menuRef = useRef<HTMLDivElement>(null);
  const menuStyle: CSSProperties = {
    position: "fixed",
    left: menu.x,
    top: menu.y,
  };

  useEffect(() => {
    const first = menuRef.current?.querySelector<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)',
    );
    first?.focus();
  }, [menu.kind, menu.serverName, menu.toolName]);

  const focusMenuItem = (index: number) => {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ) ?? [],
    );
    items[index]?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ) ?? [],
    );
    if (items.length === 0) return;
    const activeIndex = items.findIndex((item) => item === document.activeElement);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusMenuItem(activeIndex >= 0 ? (activeIndex + 1) % items.length : 0);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusMenuItem(activeIndex >= 0 ? (activeIndex - 1 + items.length) % items.length : 0);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusMenuItem(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusMenuItem(items.length - 1);
    }
  };

  return (
    <>
      <div className="session-ctx-backdrop" onClick={onClose} />
      <div
        ref={menuRef}
        className="session-ctx-menu"
        style={menuStyle}
        role="menu"
        aria-label={menu.kind === "tool" ? "MCP tool actions" : "MCP server actions"}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        {menu.kind === "tool" ? (
          <>
            <button className="session-ctx-item" role="menuitem" onClick={onViewSchema}>
              View schema
            </button>
            <button className="session-ctx-item" role="menuitem" onClick={onCallTool}>
              Call Tool
            </button>
          </>
        ) : (
          <>
            <button className="session-ctx-item" role="menuitem" onClick={onViewServer}>
              View details
            </button>
            <button className="session-ctx-item" role="menuitem" onClick={onRefreshServer}>
              Refresh tools
            </button>
            {menu.isExternal && (
              <>
                <button
                  className="session-ctx-item"
                  role="menuitem"
                  onClick={onToggleEnabled}
                >
                  {menu.enabled === false ? "Enable server" : "Disable server"}
                </button>
                <button
                  className="session-ctx-item session-ctx-item--destructive"
                  role="menuitem"
                  onClick={onRemoveServer}
                >
                  Remove server...
                </button>
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}
