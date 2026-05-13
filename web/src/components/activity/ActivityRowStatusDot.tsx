import { memo } from "react";

export type StatusKind =
  | "success"
  | "info"
  | "warning"
  | "error"
  | "disabled";

interface ActivityRowStatusDotProps {
  kind: StatusKind;
  pulse?: boolean;
  label?: string;
  title?: string;
}

// Token mapping. Each kind anchors to a distinct OKLCH lightness band so
// grayscale viewers can rank states by L alone:
//   warning (L≈78) > success (L≈72) > info (L≈70) > error (L≈65) > disabled (L≈55)
const KIND_TOKEN: Record<StatusKind, string> = {
  success: "var(--color-success-foreground)",
  info: "var(--color-info)",
  warning: "var(--color-warning-foreground)",
  error: "var(--color-error)",
  disabled: "var(--text-muted)",
};

// Shape mapping. Five distinct geometric families so the indicator
// remains identifiable even with the dot recolored to gray.
const GLYPH_PATHS: Record<StatusKind, JSX.Element> = {
  success: <circle cx="4" cy="4" r="3.25" fill="currentColor" />,
  info: (
    <polygon
      points="4,0.75 7.25,4 4,7.25 0.75,4"
      fill="currentColor"
    />
  ),
  warning: (
    <polygon points="4,0.75 7.25,6.75 0.75,6.75" fill="currentColor" />
  ),
  error: (
    <path
      d="M1.4 1.4 L6.6 6.6 M6.6 1.4 L1.4 6.6"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    />
  ),
  disabled: (
    <rect x="0.75" y="3.25" width="6.5" height="1.5" rx="0.5" fill="currentColor" />
  ),
};

function ActivityRowStatusDotImpl({
  kind,
  pulse,
  label,
  title,
}: ActivityRowStatusDotProps) {
  return (
    <svg
      className={
        pulse
          ? "activity-row-status-dot activity-row-status-dot--pulse"
          : "activity-row-status-dot"
      }
      viewBox="0 0 8 8"
      width="8"
      height="8"
      style={{ color: KIND_TOKEN[kind] }}
      data-kind={kind}
      aria-label={label}
      role={label ? "img" : undefined}
      aria-hidden={label ? undefined : true}
    >
      {title ? <title>{title}</title> : null}
      {GLYPH_PATHS[kind]}
    </svg>
  );
}

export const ActivityRowStatusDot = memo(ActivityRowStatusDotImpl);
