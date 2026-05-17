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
  /**
   * Override the kind-derived glyph with an honest, domain-specific shape.
   * Color and OKLCH lightness band still come from `kind`, so grayscale
   * ranking is preserved; only the rendered shape changes.
   */
  glyph?: StatusGlyph;
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
  "data-glyph"?: string;
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

export type StatusGlyph = (props: SVGProps<SVGSVGElement>) => ReactNode;

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

// Honest task-state glyphs. These deliberately avoid the alarm triangle and
// failure X so a being-worked task does not read as a warning and a
// dependency-blocked task does not read as a failure. The `kind` token still
// supplies color/lightness, so grayscale ranking by L is preserved.
export const ActivityGlyph: StatusGlyph = (props) => (
  <LocalGlyph data-glyph="activity" {...props}>
    <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
  </LocalGlyph>
);

export const LockGlyph: StatusGlyph = (props) => (
  <LocalGlyph data-glyph="lock" {...props}>
    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
  </LocalGlyph>
);

export const CircleGlyph: StatusGlyph = (props) => (
  <LocalGlyph data-glyph="circle" {...props}>
    <circle cx="12" cy="12" r="9" />
  </LocalGlyph>
);

export const EyeGlyph: StatusGlyph = (props) => (
  <LocalGlyph data-glyph="eye" {...props}>
    <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
    <circle cx="12" cy="12" r="3" />
  </LocalGlyph>
);

export const CheckGlyph: StatusGlyph = (props) => (
  <LocalGlyph data-glyph="check" {...props}>
    <path d="M20 6 9 17l-5-5" />
  </LocalGlyph>
);

export const DashGlyph: StatusGlyph = (props) => (
  <LocalGlyph data-glyph="dash" {...props}>
    <path d="M5 12h14" />
  </LocalGlyph>
);

function ActivityRowStatusDotImpl({
  kind,
  pulse,
  label,
  title,
  glyph,
}: ActivityRowStatusDotProps) {
  const Icon = glyph ?? KIND_ICON[kind];
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
