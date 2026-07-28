import type { ReactNode } from "react";

import { Button } from "../../ui/Button";
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

export function DetailActionButton({
  label,
  onClick,
  variant = "accent",
  disabled,
  icon,
}: DetailActionButtonProps) {
  const handleClick = () => {
    void Promise.resolve(onClick()).catch((error) => {
      console.error(`Detail action "${label}" failed`, error);
    });
  };
  return (
    <Button
      type="button"
      size="sm"
      dense
      variant={variant}
      disabled={disabled}
      onClick={handleClick}
    >
      {icon}
      <span>{label}</span>
    </Button>
  );
}
