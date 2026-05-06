import type { SessionObservationMeta } from "../../types/chat";
import { getProviderDisplayName } from "../../lib/providerModels";

interface AttachedSessionMetadataStripProps {
  meta: SessionObservationMeta;
}

interface Cell {
  label: string;
  value: string | null;
}

function formatContextWindow(value: number | null | undefined): string | null {
  if (value == null || value <= 0) return null;
  if (value >= 1000) {
    const kilos = value / 1000;
    return Number.isInteger(kilos) ? `${kilos}k` : `${kilos.toFixed(1)}k`;
  }
  return String(value);
}

function formatReasoning(value: string | null | undefined): string | null {
  if (!value) return null;
  if (value === "auto") return "Auto";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function AttachedSessionMetadataStrip({
  meta,
}: AttachedSessionMetadataStripProps) {
  const providerKey = meta.source && meta.source !== "unknown" ? meta.source : null;
  const cells: Cell[] = [
    {
      label: "Provider",
      value: providerKey ? getProviderDisplayName(providerKey) : null,
    },
    { label: "Model", value: meta.model ?? null },
    { label: "Reasoning", value: formatReasoning(meta.reasoningEffort) },
    { label: "Branch", value: meta.gitBranch ?? null },
    { label: "Window", value: formatContextWindow(meta.contextWindow) },
  ];

  return (
    <div
      className="flex items-center gap-6 px-3 py-2 max-md:gap-4 max-md:px-2"
      data-testid="attached-session-metadata-strip"
    >
      {cells.map((cell) => (
        <div key={cell.label} className="flex flex-col gap-0.5 min-w-0">
          <span className="text-[length:var(--text-2xs)] font-medium uppercase tracking-wide text-muted-foreground/80">
            {cell.label}
          </span>
          <span className="text-[length:var(--text-md)] text-foreground truncate">
            {cell.value ?? <span className="text-muted-foreground/60">—</span>}
          </span>
        </div>
      ))}
    </div>
  );
}
