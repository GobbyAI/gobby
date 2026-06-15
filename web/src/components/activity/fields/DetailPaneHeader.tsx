import type { ReactNode } from "react";

import { cn } from "../../../lib/utils";
import type { DetailPaneHeaderProps } from "./types";

export function DetailPaneHeader({
  title,
  dirty,
  onSave,
  onDiscard,
  saving = false,
  serverChanged = false,
  actions,
}: DetailPaneHeaderProps) {
  return (
    <div className="flex h-10 items-center justify-between gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
      <div className="flex min-w-0 items-center gap-2">
        <div className="truncate text-sm font-medium text-foreground">{title}</div>
        {serverChanged && (
          <span className="rounded-md bg-[var(--color-warning-soft)] px-2 py-1 text-xs text-[var(--color-warning-foreground)]">
            Changed on server
          </span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {actions}
        {dirty && (
          <>
            <DetailActionButton
              label="Discard"
              variant="ghost"
              disabled={saving}
              onClick={onDiscard}
            />
            <DetailActionButton
              label={saving ? "Saving..." : "Save"}
              variant="accent"
              disabled={saving}
              onClick={onSave}
            />
          </>
        )}
      </div>
    </div>
  );
}

interface DetailActionButtonProps {
  label: string;
  onClick: () => void | Promise<void>;
  variant?: "ghost" | "accent" | "secondary" | "destructive";
  disabled?: boolean;
  icon?: ReactNode;
}

const DETAIL_ACTION_VARIANT_CLASS: Record<
  NonNullable<DetailActionButtonProps["variant"]>,
  string
> = {
  ghost: "btn-ghost",
  accent: "btn-primary",
  secondary: "btn-secondary",
  destructive: "btn-destructive",
};

export function DetailActionButton({
  label,
  onClick,
  variant = "ghost",
  disabled,
  icon,
}: DetailActionButtonProps) {
  return (
    <button
      type="button"
      className={cn("btn btn-sm", DETAIL_ACTION_VARIANT_CLASS[variant])}
      disabled={disabled}
      onClick={onClick}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
