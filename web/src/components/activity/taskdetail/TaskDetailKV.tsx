import type { ReactNode } from "react";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";

/**
 * D5 — shared key/value primitives for the detail pane. Tight horizontal
 * rhythm, no border-left stripes; hierarchy comes from weight + space.
 */

export interface ParentTaskRef {
  id: string;
  ref: string;
  title: string;
}

export type ValidationStatus = "ok" | "fail" | "neutral";

export function MetaKVRow({
  label,
  children,
  mono = false,
  link = false,
  href,
  title,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
  link?: boolean;
  href?: string;
  title?: string;
}) {
  const valueCls = cn(
    "min-w-0 text-[length:var(--text-sm)] leading-[1.45] [overflow-wrap:anywhere] text-[var(--text-secondary)]",
    mono && "font-mono text-[length:var(--text-xs)]",
    link &&
      href &&
      "rounded-[0.2rem] text-accent no-underline hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
  );

  return (
    <div
      className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-baseline gap-[0.85rem] border-b border-[var(--border-soft)] py-[0.45rem] last:border-b-0"
      title={title}
    >
      <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
        {label}
      </span>
      {link && href ? (
        <a
          className={valueCls}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
        >
          {children}
        </a>
      ) : (
        <div className={valueCls}>{children}</div>
      )}
    </div>
  );
}

export function ValidationRow({
  status,
  failCount,
}: {
  status: string;
  failCount: number;
}) {
  const normalized = status.trim().toLowerCase();
  const variant: ValidationStatus = [
    "pass",
    "passed",
    "approve",
    "approved",
  ].includes(normalized)
    ? "ok"
    : ["fail", "failed", "reject", "rejected"].includes(normalized)
      ? "fail"
      : "neutral";
  return (
    <div
      className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-center gap-[0.85rem] border-b border-border bg-[var(--bg-primary)] px-4 py-[0.65rem]"
      data-task-detail-validation
      title="Validation status"
    >
      <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
        Validation
      </span>
      <span className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "inline-flex h-6 items-center gap-[0.3rem] rounded-full border border-border bg-[var(--bg-tertiary)] px-[0.55rem] text-[length:var(--text-2xs)] font-medium tracking-[0.02em] whitespace-nowrap text-[var(--text-secondary)] capitalize",
            variant === "ok" &&
              "border-[color-mix(in_srgb,var(--color-success-foreground)_35%,transparent)] bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]",
            variant === "fail" &&
              "border-[color-mix(in_srgb,var(--color-error)_35%,transparent)] bg-[var(--color-error-soft)] text-[var(--color-error)]",
          )}
        >
          {status}
        </span>
        {failCount > 0 && (
          <span className="ml-[0.45rem] text-[length:var(--text-2xs)] font-medium text-[var(--color-error)]">
            {failCount} {failCount === 1 ? "fail" : "fails"}
          </span>
        )}
      </span>
    </div>
  );
}

export function ParentKVRow({
  parent,
  onSelect,
}: {
  parent: ParentTaskRef;
  onSelect?: (id: string) => void;
}) {
  const handleClick = onSelect ? () => onSelect(parent.id) : undefined;
  return (
    <div
      className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-baseline gap-[0.85rem] border-b border-[var(--border-soft)] py-[0.45rem] last:border-b-0"
      title="Parent task"
    >
      <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
        Parent
      </span>
      <span className="min-w-0 text-[length:var(--text-sm)] leading-[1.45] [overflow-wrap:anywhere] text-[var(--text-secondary)]">
        {handleClick ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className={cn(
              "inline-flex flex-wrap items-baseline gap-x-[0.4rem] gap-y-[0.1rem] rounded p-0 text-left text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
              coarseHitAreaCls,
            )}
            aria-label={`Open parent task ${parent.ref}: ${parent.title}`}
            onClick={handleClick}
          >
            <span className="shrink-0 font-mono text-[length:var(--text-sm)] font-semibold text-accent">
              {parent.ref}
            </span>
            <span className="whitespace-normal">{parent.title}</span>
          </Button>
        ) : (
          <>
            <span className="shrink-0 font-mono text-[length:var(--text-sm)] font-semibold text-accent">
              {parent.ref}
            </span>{" "}
            <span className="whitespace-normal">{parent.title}</span>
          </>
        )}
      </span>
    </div>
  );
}
