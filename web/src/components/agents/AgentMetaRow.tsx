import type { ReactNode } from "react";

export function AgentMetaRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="grid min-h-9 grid-cols-[110px_minmax(0,1fr)] items-center gap-3 py-1">
      <span className="text-sm text-[var(--text-secondary)]">{label}</span>
      <div className="min-w-0 text-right">{children}</div>
    </div>
  );
}
