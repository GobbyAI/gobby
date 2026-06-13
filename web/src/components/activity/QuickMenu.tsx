import {
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { cn } from "../../lib/utils";

export interface QuickMenuAnchor {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface QuickMenuActionItem {
  label: string;
  icon?: ReactNode;
  destructive?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

interface QuickMenuSeparator {
  type: "separator";
}

export type QuickMenuItem = QuickMenuActionItem | QuickMenuSeparator;

interface QuickMenuProps {
  items: QuickMenuItem[];
  menuLabel: string;
  triggerLabel?: string;
  anchor?: QuickMenuAnchor;
  disabled?: boolean;
  onClose?: () => void;
  onOpenChange?: (open: boolean) => void;
}

const VIEWPORT_GUTTER = 8;
const MENU_GAP = 4;

function EllipsisVerticalIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="12" cy="5" r="2" />
      <circle cx="12" cy="12" r="2" />
      <circle cx="12" cy="19" r="2" />
    </svg>
  );
}

function DestructiveIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </svg>
  );
}

function isActionItem(item: QuickMenuItem): item is QuickMenuActionItem {
  return !("type" in item);
}

function actionIndexFor(items: QuickMenuItem[], itemIndex: number): number {
  return items.slice(0, itemIndex).filter(isActionItem).length;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function anchorToRect(anchor: QuickMenuAnchor): DOMRect {
  return {
    x: anchor.x,
    y: anchor.y,
    left: anchor.x,
    top: anchor.y,
    width: anchor.width,
    height: anchor.height,
    right: anchor.x + anchor.width,
    bottom: anchor.y + anchor.height,
    toJSON: () => ({}),
  } as DOMRect;
}

export function QuickMenu({
  items,
  menuLabel,
  triggerLabel = "Open actions",
  anchor,
  disabled,
  onClose,
  onOpenChange,
}: QuickMenuProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<React.CSSProperties>({
    position: "fixed",
    left: VIEWPORT_GUTTER,
    top: VIEWPORT_GUTTER,
  });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<HTMLButtonElement[]>([]);
  const isOpen = Boolean(anchor) || internalOpen;

  const closeMenu = useCallback(() => {
    setInternalOpen(false);
    onOpenChange?.(false);
    onClose?.();
  }, [onClose, onOpenChange]);

  const openMenu = useCallback(() => {
    if (disabled) return;
    setInternalOpen(true);
    onOpenChange?.(true);
  }, [disabled, onOpenChange]);

  const enabledItems = useCallback(
    () => itemRefs.current.filter((item) => item && !item.disabled),
    [],
  );

  const focusMenuItem = useCallback(
    (index: number) => {
      const items = enabledItems();
      if (items.length === 0) return;
      items[(index + items.length) % items.length]?.focus();
    },
    [enabledItems],
  );

  const updatePosition = useCallback(() => {
    const triggerRect = anchor
      ? anchorToRect(anchor)
      : triggerRef.current?.getBoundingClientRect();
    const menuRect = menuRef.current?.getBoundingClientRect();
    if (!triggerRect || !menuRect) return;

    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const maxLeft = Math.max(VIEWPORT_GUTTER, viewportWidth - menuRect.width - VIEWPORT_GUTTER);
    const left = clamp(triggerRect.right - menuRect.width, VIEWPORT_GUTTER, maxLeft);
    const belowTop = triggerRect.bottom + MENU_GAP;
    const aboveTop = triggerRect.top - menuRect.height - MENU_GAP;
    const fitsBelow = belowTop + menuRect.height <= viewportHeight - VIEWPORT_GUTTER;
    const top = fitsBelow ? belowTop : Math.max(VIEWPORT_GUTTER, aboveTop);

    setMenuStyle({
      position: "fixed",
      left,
      top,
    });
  }, [anchor]);

  useLayoutEffect(() => {
    if (!isOpen) return;
    updatePosition();
  }, [isOpen, items.length, updatePosition]);

  useEffect(() => {
    if (!isOpen) return;
    updatePosition();
    const handleViewportChange = () => updatePosition();
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);
    return () => {
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
    };
  }, [isOpen, updatePosition]);

  useEffect(() => {
    if (!isOpen) return;
    itemRefs.current = itemRefs.current.slice(0, items.filter(isActionItem).length);
    enabledItems()[0]?.focus();
  }, [enabledItems, isOpen, items]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const focusableItems = enabledItems();
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (focusableItems.length === 0) return;
    const currentIndex = focusableItems.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusMenuItem(currentIndex === -1 ? 0 : currentIndex + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusMenuItem(currentIndex === -1 ? focusableItems.length - 1 : currentIndex - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusMenuItem(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusMenuItem(focusableItems.length - 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      focusableItems[currentIndex === -1 ? 0 : currentIndex]?.click();
    }
  };

  return (
    <>
      {!anchor && (
        <button
          ref={triggerRef}
          type="button"
          className="quick-menu-trigger"
          aria-label={triggerLabel}
          title={triggerLabel}
          aria-haspopup="menu"
          aria-expanded={isOpen}
          disabled={disabled}
          onClick={(event) => {
            event.stopPropagation();
            if (isOpen) {
              closeMenu();
            } else {
              openMenu();
            }
          }}
        >
          <EllipsisVerticalIcon />
        </button>
      )}
      {isOpen && (
        <>
          <div
            className="session-ctx-backdrop"
            role="presentation"
            tabIndex={-1}
            onClick={closeMenu}
          />
          <div
            ref={menuRef}
            className="session-ctx-menu"
            style={menuStyle}
            role="menu"
            aria-label={menuLabel}
            tabIndex={-1}
            onKeyDown={handleKeyDown}
          >
            {items.map((item, index) => {
              if (!isActionItem(item)) {
                return <div key={`separator-${index}`} className="session-ctx-divider" role="separator" />;
              }
              const currentActionIndex = actionIndexFor(items, index);
              return (
                <button
                  key={`${item.label}-${index}`}
                  ref={(node) => {
                    if (node) itemRefs.current[currentActionIndex] = node;
                  }}
                  className={cn(
                    "session-ctx-item quick-menu-item",
                    item.destructive && "session-ctx-item--destructive",
                  )}
                  role="menuitem"
                  disabled={item.disabled}
                  onClick={() => {
                    item.onSelect();
                    closeMenu();
                  }}
                >
                  {(item.icon || item.destructive) && (
                    <span className="quick-menu-item-icon" aria-hidden="true">
                      {item.icon ?? <DestructiveIcon />}
                    </span>
                  )}
                  <span>{item.label}</span>
                </button>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}
