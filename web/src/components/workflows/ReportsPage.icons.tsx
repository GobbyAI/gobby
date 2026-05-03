import type { SortDirection } from "./ReportsPage.helpers";

export function SortArrow<T extends string>({
  column,
  sortColumn,
  sortDirection,
}: {
  column: T;
  sortColumn: T;
  sortDirection: SortDirection;
}) {
  if (column !== sortColumn)
    return <span className="text-[var(--text-muted)] opacity-50">{"↕"}</span>;
  return (
    <span className="text-[var(--accent)]">
      {sortDirection === "asc" ? "↑" : "↓"}
    </span>
  );
}

export function CloseIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

export function CronIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}

export function TraceIcon() {
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
      style={{ marginRight: "6px", verticalAlign: "middle" }}
    >
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </svg>
  );
}
