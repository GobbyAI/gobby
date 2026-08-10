import {
  forwardRef,
  type AriaRole,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { DropdownCaret } from "../ui/DropdownCaret";

interface FilterDropdownTriggerProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  open: boolean;
  label?: string;
  activeCount?: number;
  icon?: ReactNode;
}

export const FilterDropdownTrigger = forwardRef<
  HTMLButtonElement,
  FilterDropdownTriggerProps
>(function FilterDropdownTrigger(
  { open, label = "Filter", activeCount = 0, icon, className, ...props },
  ref,
) {
  return (
    <Button
      {...props}
      ref={ref}
      type="button"
      variant="accent"
      size="sm"
      className={cn(
        coarseHitAreaCls,
        "relative aria-expanded:border-[color-mix(in_srgb,var(--accent)_60%,transparent)] aria-expanded:bg-[color-mix(in_srgb,var(--accent)_22%,transparent)] in-data-[theme=light]:aria-expanded:border-accent in-data-[theme=light]:aria-expanded:bg-accent in-data-[theme=light]:aria-expanded:text-accent-foreground",
        className,
      )}
      aria-expanded={open}
    >
      {icon}
      <span className="activity-panel-action-btn__label @max-[479px]/activity-panel:hidden">
        {label}
      </span>
      {activeCount > 0 && (
        <span
          className="pointer-events-none absolute -right-1 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[length:var(--text-2xs)] font-semibold leading-none text-accent-foreground"
          data-filter-active-count
        >
          {activeCount}
        </span>
      )}
      <DropdownCaret open={open} />
    </Button>
  );
});

interface FilterDropdownShellProps {
  children: ReactNode;
  ariaLabel: string;
  className?: string;
  role?: AriaRole;
  ariaModal?: boolean;
  panelRef?: Ref<HTMLDivElement>;
  overlayTestId?: string;
  outsideInteraction?: "click" | "primary-mousedown";
  panelVariant?: "dropdown" | "inline";
  onClose: () => void;
}

interface InlineFilterPanelProps extends HTMLAttributes<HTMLDivElement> {
  variant?: "dropdown" | "inline";
}

export const InlineFilterPanel = forwardRef<
  HTMLDivElement,
  InlineFilterPanelProps
>(function InlineFilterPanel(
  { variant = "inline", className, children, ...props },
  ref,
) {
  return (
    <div
      {...props}
      ref={ref}
      className={cn(
        variant === "inline"
          ? "absolute right-3 top-1 z-[85] grid max-h-[min(28rem,calc(100vh-6rem))] w-[min(22rem,calc(100vw-1rem))] gap-[0.65rem] overflow-auto rounded-lg border border-border bg-[var(--bg-secondary)] p-3 shadow-md"
          : "z-[100] flex flex-col rounded-md border border-border bg-[var(--bg-secondary)] shadow-xl",
        className,
      )}
    >
      {children}
    </div>
  );
});

export function InlineFilterFieldRow({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "grid min-w-0 gap-[0.3rem] text-[length:var(--text-xs)] font-[var(--font-weight-medium)] text-[var(--text-muted)] [&_select]:min-h-11 [&_select]:min-w-0 [&_select]:rounded-md [&_select]:border [&_select]:border-[var(--border)] [&_select]:bg-[var(--bg-primary)] [&_select]:px-[0.55rem] [&_select]:text-[var(--text-primary)] [&_select]:[font:inherit] [&_select:focus-visible]:outline [&_select:focus-visible]:outline-2 [&_select:focus-visible]:outline-offset-2 [&_select:focus-visible]:outline-[var(--accent)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function FilterDropdownShell({
  children,
  ariaLabel,
  className,
  role = "dialog",
  ariaModal,
  panelRef,
  overlayTestId,
  outsideInteraction = "click",
  panelVariant = "dropdown",
  onClose,
}: FilterDropdownShellProps) {
  const handleMouseDown = (event: MouseEvent<HTMLDivElement>) => {
    if (event.button === 0) onClose();
  };

  return (
    <>
      <div
        className="fixed inset-0 z-[99]"
        role="presentation"
        aria-hidden="true"
        data-testid={overlayTestId}
        onClick={outsideInteraction === "click" ? onClose : undefined}
        onMouseDown={
          outsideInteraction === "primary-mousedown"
            ? handleMouseDown
            : undefined
        }
      />
      <InlineFilterPanel
        ref={panelRef}
        variant={panelVariant}
        className={className}
        role={role}
        aria-label={ariaLabel}
        aria-modal={ariaModal}
      >
        {children}
      </InlineFilterPanel>
    </>
  );
}

export function FilterFieldRow({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("px-2 py-1", className)}>{children}</div>;
}

export function FilterSection({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="mb-3 last:mb-0">
      <div className="text-xs font-medium mb-1.5 text-muted-foreground">
        {label}
      </div>
      <div className="flex flex-col gap-1">{children}</div>
    </div>
  );
}

export function FilterCheckboxRow({
  checked,
  onToggle,
  label,
  leading,
}: {
  checked: boolean;
  onToggle: () => void;
  label: string;
  leading?: ReactNode;
}) {
  return (
    <label className="filter-checkbox-row flex w-full min-w-0 cursor-pointer items-center gap-1.5 px-1.5 text-left text-sm text-foreground">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        dense
        role="checkbox"
        aria-label={label}
        aria-checked={checked}
        onClick={onToggle}
        className={cn("h-6 min-h-6 w-6 shrink-0 p-0", coarseHitAreaCls)}
      >
        <span
          aria-hidden="true"
          className={cn(
            "flex size-3 items-center justify-center rounded-[3px] border",
            checked
              ? "border-accent bg-accent text-accent-foreground"
              : "border-border bg-transparent",
          )}
        >
          {checked && (
            <svg viewBox="0 0 12 12" className="size-2.5" fill="none">
              <path
                d="m2.5 6 2.1 2.1L9.5 3.5"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
        </span>
      </Button>
      {leading}
      <span className="min-w-0 truncate">{label}</span>
    </label>
  );
}
