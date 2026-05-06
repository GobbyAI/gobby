import type { SessionObservationMeta } from "../../types/chat";

interface AttachedSessionMetadataStripProps {
  meta: SessionObservationMeta;
}

interface Cell {
  label: string;
  value: string | null;
}

export function AttachedSessionMetadataStrip({
  meta,
}: AttachedSessionMetadataStripProps) {
  const cells: Cell[] = [
    { label: "Model", value: meta.model ?? null },
    { label: "Branch", value: meta.gitBranch ?? null },
  ];

  return (
    <div
      className="flex items-center gap-3 px-3 py-1 text-[length:var(--text-sm)]"
      data-testid="attached-session-metadata-strip"
    >
      {cells.map((cell, index) => (
        <span key={cell.label} className="flex items-center gap-1.5 min-w-0">
          {index > 0 && (
            <span className="text-muted-foreground/30 mr-1.5">·</span>
          )}
          <span className="text-[length:var(--text-2xs)] font-medium uppercase tracking-wide text-muted-foreground/80">
            {cell.label}
          </span>
          <span className="text-foreground truncate">
            {cell.value ?? <span className="text-muted-foreground/60">—</span>}
          </span>
        </span>
      ))}
    </div>
  );
}
