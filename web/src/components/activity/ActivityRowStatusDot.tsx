import { memo } from "react";
import {
  AlertTriangle,
  CheckCircle,
  CircleDot,
  Minus,
  Octagon,
  Pause,
  XCircle,
  type LucideIcon,
} from "lucide-react";

export type StatusKind =
  | "success"
  | "info"
  | "warning"
  | "error"
  | "paused"
  | "stopped"
  | "disabled";

interface ActivityRowStatusDotProps {
  kind: StatusKind;
  pulse?: boolean;
  label?: string;
  title?: string;
}

// Each kind anchors to a distinct OKLCH lightness band so grayscale viewers
// can rank states by L alone:
//   warning (L≈78) > success (L≈72) > info (L≈70) > paused (L≈68)
//   > error (L≈65) > disabled (L≈62) > stopped (L≈60)
const KIND_TOKEN: Record<StatusKind, string> = {
  success: "var(--color-success-foreground)",
  info: "var(--color-info)",
  warning: "var(--color-warning-foreground)",
  error: "var(--color-error)",
  paused: "var(--text-secondary)",
  stopped: "var(--color-stopped)",
  disabled: "var(--text-muted)",
};

// One lucide icon per kind. Each glyph is shape-unique so the indicator
// stays identifiable even with the dot recolored to gray.
const KIND_ICON: Record<StatusKind, LucideIcon> = {
  success: CheckCircle,
  info: CircleDot,
  warning: AlertTriangle,
  error: XCircle,
  paused: Pause,
  stopped: Octagon,
  disabled: Minus,
};

function ActivityRowStatusDotImpl({
  kind,
  pulse,
  label,
  title,
}: ActivityRowStatusDotProps) {
  const Icon = KIND_ICON[kind];
  return (
    <span
      className={
        pulse
          ? "activity-row-status-dot activity-row-status-dot--pulse"
          : "activity-row-status-dot"
      }
      style={{ color: KIND_TOKEN[kind] }}
      data-kind={kind}
      aria-label={label}
      role={label ? "img" : undefined}
      aria-hidden={label ? undefined : true}
      title={title}
    >
      <Icon size={12} strokeWidth={2.25} aria-hidden focusable={false} />
    </span>
  );
}

export const ActivityRowStatusDot = memo(ActivityRowStatusDotImpl);
