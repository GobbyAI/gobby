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
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";

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
  title?: string;
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
  returnFocusTo?: HTMLElement | null;
  disabled?: boolean;
  onClose?: () => void;
  onOpenChange?: (open: boolean) => void;
}

const VIEWPORT_GUTTER = 8;
const MENU_GAP = 4;

/* The one vertical-dot kebab glyph. Every three-dot trigger (QuickMenu,
   session rows, task rows) renders this — no per-surface copies. */
export function KebabIcon() {
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
  returnFocusTo,
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
    // A zero-width rect means the menu hasn't laid out yet; clamping against
    // it would park the menu at triggerRect.right, off-viewport. Keep the
    // safe initial position — the ResizeObserver re-runs this once the menu
    // has real dimensions.
    if (!triggerRect || !menuRect || menuRect.width === 0) return;

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
    const menu = menuRef.current;
    if (!menu || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => updatePosition());
    observer.observe(menu);
    return () => observer.disconnect();
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
      // The focused menuitem is about to unmount; return keyboard users to the
      // control that opened the menu. Anchor-mode callers supply that control.
      (returnFocusTo ?? triggerRef.current)?.focus();
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
        <Button
          ref={triggerRef}
          type="button"
          variant="ghost"
          size="icon"
          dense
          className={cn(
            "quick-menu-trigger size-7 min-h-7 min-w-7 shrink-0 p-0 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-[0.55] pointer-coarse:size-11 pointer-coarse:min-h-11 pointer-coarse:min-w-11",
            coarseHitAreaCls,
          )}
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
          <KebabIcon />
        </Button>
      )}
      {isOpen && (
        <>
          <div
            className="session-ctx-backdrop fixed inset-0 z-[90]"
            role="presentation"
            tabIndex={-1}
            onClick={closeMenu}
          />
          <div
            ref={menuRef}
            className="session-ctx-menu z-[91] min-w-[150px] rounded-md border border-border bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]"
            style={menuStyle}
            role="menu"
            aria-label={menuLabel}
            tabIndex={-1}
            onKeyDown={handleKeyDown}
          >
            {items.map((item, index) => {
              if (!isActionItem(item)) {
                return (
                  <div
                    key={`separator-${index}`}
                    className="session-ctx-divider my-1 h-px bg-border"
                    role="separator"
                  />
                );
              }
              const currentActionIndex = actionIndexFor(items, index);
              return (
                <Button
                  key={`${item.label}-${index}`}
                  type="button"
                  variant="ghost"
                  size="sm"
                  dense
                  ref={(node) => {
                    if (node) itemRefs.current[currentActionIndex] = node;
                  }}
                  className={cn(
                    "session-ctx-item quick-menu-item",
                    "flex min-h-8 w-full items-center gap-2 border-0 bg-transparent px-2.5 py-1.5 text-left text-[length:var(--text-md)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-[0.55] pointer-coarse:min-h-11",
                    item.destructive &&
                      "session-ctx-item--destructive text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)]",
                    coarseHitAreaCls,
                  )}
                  role="menuitem"
                  disabled={item.disabled}
                  title={item.title}
                  onClick={() => {
                    item.onSelect();
                    closeMenu();
                  }}
                >
                  {(item.icon || item.destructive) && (
                    <span
                      className="quick-menu-item-icon inline-flex shrink-0"
                      aria-hidden="true"
                    >
                      {item.icon ?? <DestructiveIcon />}
                    </span>
                  )}
                  <span>{item.label}</span>
                </Button>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}
