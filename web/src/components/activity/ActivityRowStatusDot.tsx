import { memo, type ReactNode, type SVGProps } from "react";

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
//   > error (L≈65) > disabled (L≈62) > inactive/stopped (L≈60)
const KIND_TOKEN: Record<StatusKind, string> = {
  success: "var(--color-success-foreground)",
  info: "var(--color-info)",
  warning: "var(--color-warning-foreground)",
  error: "var(--color-error)",
  paused: "var(--text-secondary)",
  stopped: "var(--color-inactive)",
  disabled: "var(--text-muted)",
};

type LocalGlyphProps = SVGProps<SVGSVGElement> & {
  children: ReactNode;
};

function LocalGlyph({ children, className, ...props }: LocalGlyphProps) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.25}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`activity-row-status-dot__glyph ${className ?? ""}`.trim()}
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

type StatusGlyph = (props: SVGProps<SVGSVGElement>) => ReactNode;

const KIND_ICON: Record<StatusKind, StatusGlyph> = {
  success: (props) => (
    <LocalGlyph {...props}>
      <path d="M21.801 10A10 10 0 1 1 17 3.335" />
      <path d="m9 11 3 3L22 4" />
    </LocalGlyph>
  ),
  info: (props) => (
    <LocalGlyph {...props}>
      <circle cx="12" cy="12" r="10" />
      <circle cx="12" cy="12" r="1" />
    </LocalGlyph>
  ),
  warning: (props) => (
    <LocalGlyph {...props}>
      <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </LocalGlyph>
  ),
  error: (props) => (
    <LocalGlyph {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="m15 9-6 6" />
      <path d="m9 9 6 6" />
    </LocalGlyph>
  ),
  paused: (props) => (
    <LocalGlyph {...props}>
      <rect x="14" y="3" width="5" height="18" rx="1" />
      <rect x="5" y="3" width="5" height="18" rx="1" />
    </LocalGlyph>
  ),
  stopped: (props) => (
    <LocalGlyph {...props}>
      <path d="M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z" />
    </LocalGlyph>
  ),
  disabled: (props) => (
    <LocalGlyph {...props}>
      <path d="M5 12h14" />
    </LocalGlyph>
  ),
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
      aria-hidden={label ? "false" : "true"}
      title={title}
    >
      <Icon className={`activity-row-status-dot__glyph--${kind}`} />
    </span>
  );
}

export const ActivityRowStatusDot = memo(ActivityRowStatusDotImpl);
