import type { ReactNode } from "react";
import { cn } from "../../../lib/utils";

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
    "activity-task-detail-kv-row__value",
    mono && "activity-task-detail-kv-row__value--mono",
    link && href && "activity-task-detail-kv-row__value--link",
  );

  return (
    <div className="activity-task-detail-kv-row" title={title}>
      <span className="activity-task-detail-kv-row__label">{label}</span>
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
  const normalized = status.toLowerCase();
  const variant: ValidationStatus =
    normalized === "passed" || normalized === "approved"
      ? "ok"
      : normalized === "failed" || normalized === "rejected"
        ? "fail"
        : "neutral";
  return (
    <div
      className="activity-task-detail-validation-row"
      title="Validation status"
    >
      <span className="activity-task-detail-validation-row__label">
        Validation
      </span>
      <span className="activity-task-detail-validation-row__value">
        <span className={cn("activity-task-detail-pill", `activity-task-detail-pill--${variant}`)}>
          {status}
        </span>
        {failCount > 0 && (
          <span className="activity-task-detail-validation-fails">
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
    <div className="activity-task-detail-kv-row" title="Parent task">
      <span className="activity-task-detail-kv-row__label">Parent</span>
      <span className="activity-task-detail-kv-row__value">
        {handleClick ? (
          <button
            type="button"
            className="activity-task-detail-parent-link"
            aria-label={`Open parent task ${parent.ref}: ${parent.title}`}
            onClick={handleClick}
          >
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            <span className="activity-task-detail-parent-title">
              {parent.title}
            </span>
          </button>
        ) : (
          <>
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>{" "}
            <span className="activity-task-detail-parent-title">
              {parent.title}
            </span>
          </>
        )}
      </span>
    </div>
  );
}
