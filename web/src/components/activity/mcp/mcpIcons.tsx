import { cn } from "../../../lib/utils";

export function ChevronIcon({ open }: { open: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex size-4 shrink-0 text-[var(--text-muted)] transition-transform duration-150 ease-in-out",
        open && "rotate-90",
      )}
      aria-hidden="true"
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <polyline points="9 18 15 12 9 6" />
      </svg>
    </span>
  );
}

// "Call Tool" run glyph — a play triangle reads as "execute/invoke" without
// borrowing the terminal-prompt or lightning clichés. Matches the inline-SVG
// stroke style used across the activity panel.
export function CallToolIcon() {
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
      <polygon points="6 3 20 12 6 21 6 3" />
    </svg>
  );
}
