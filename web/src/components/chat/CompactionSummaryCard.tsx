/**
 * Renders a Claude Code compaction boundary as a labelled divider — the point
 * where the conversation was compacted (context summarized). The summary body
 * itself arrives as the following message; this marks the landmark.
 */
export function CompactionSummaryCard({ content }: { content: string }) {
  const label = content.trim() || "Conversation compacted";
  return (
    <div
      className="my-3 flex items-center gap-3 text-xs text-muted-foreground"
      role="separator"
      aria-label={label}
    >
      <span className="h-px flex-1 bg-border" aria-hidden="true" />
      <span className="shrink-0 font-medium">{label}</span>
      <span className="h-px flex-1 bg-border" aria-hidden="true" />
    </div>
  );
}
