import type { MouseEvent, ReactNode } from "react";

import { cn } from "../../lib/utils";
import { SourceIcon } from "../shared/SourceIcon";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import {
  ActivityRowStatusDot,
  DashGlyph,
  EyeGlyph,
  LockGlyph,
} from "./ActivityRowStatusDot";
import { KebabIcon } from "./QuickMenu";
import { type WatchingSessionEntry, renderBadges } from "./SessionsTab.helpers";
import { TickerText } from "../ui/TickerText";

interface SessionsEntryListProps {
  emptyState: ReactNode;
  entries: WatchingSessionEntry[];
  fetchError: string | null;
  isLoading: boolean;
  onMenuButtonClick: (
    event: MouseEvent<HTMLButtonElement>,
    entry: WatchingSessionEntry,
  ) => void;
  onSelect: (id: string) => void;
  selectedSessionId: string | null;
  topHeight: number;
}

export function SessionsEntryList({
  emptyState,
  entries,
  fetchError,
  isLoading,
  onMenuButtonClick,
  onSelect,
  selectedSessionId,
  topHeight,
}: SessionsEntryListProps) {
  return (
    <div
      className={`overflow-y-auto ${selectedSessionId ? "border-b border-border" : "flex-1"}`}
      style={selectedSessionId ? { height: `${topHeight}%` } : undefined}
    >
      {fetchError && entries.length > 0 ? (
        <div
          className="border-b border-border px-3 py-1.5 text-xs text-destructive-foreground"
          role="alert"
        >
          {fetchError}
        </div>
      ) : null}
      {isLoading && entries.length === 0 ? (
        <ActivityPanelEmpty body="Loading sessions…" />
      ) : fetchError && entries.length === 0 ? (
        <ActivityPanelEmpty body={fetchError} />
      ) : entries.length === 0 ? (
        emptyState
      ) : (
        entries.map((entry) => (
          <SessionEntryRow
            key={`${entry.type}-${entry.id}`}
            entry={entry}
            isSelected={entry.id === selectedSessionId}
            onMenuButtonClick={onMenuButtonClick}
            onSelect={onSelect}
          />
        ))
      )}
    </div>
  );
}

interface SessionEntryRowProps {
  entry: WatchingSessionEntry;
  isSelected: boolean;
  onMenuButtonClick: (
    event: MouseEvent<HTMLButtonElement>,
    entry: WatchingSessionEntry,
  ) => void;
  onSelect: (id: string) => void;
}

function SessionEntryRow({
  entry,
  isSelected,
  onMenuButtonClick,
  onSelect,
}: SessionEntryRowProps) {
  const isDimmed = ["paused", "interrupted", "expired"].includes(entry.status);
  const blockedCount = entry.blockedCount ?? 0;
  const statusKind =
    blockedCount > 0
      ? "paused"
      : entry.status === "active"
        ? "active"
        : entry.status === "expired"
          ? "stopped"
          : entry.status === "awaiting_input"
            ? "info"
            : entry.status === "awaiting_approval" ||
                entry.status === "awaiting_handoff"
              ? "warning"
              : "paused";
  const statusGlyph =
    blockedCount > 0
      ? undefined
      : entry.status === "awaiting_input"
        ? EyeGlyph
        : entry.status === "awaiting_approval" ||
            entry.status === "awaiting_handoff"
          ? LockGlyph
          : entry.status === "interrupted"
            ? DashGlyph
            : undefined;
  const displayLabel = entry.label;

  return (
    <div
      role="button"
      tabIndex={0}
      className={cn(
        "session-entry flex h-[var(--activity-panel-row-height)] min-h-[var(--activity-panel-row-height)] w-full cursor-pointer appearance-none items-center justify-between border-0 border-b border-border bg-transparent px-3 py-0 text-left font-[inherit] text-[inherit] transition-colors hover:bg-[var(--bg-tertiary)] focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent",
        isSelected &&
          "session-entry--active bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]",
        isDimmed && "session-entry--paused opacity-[0.55] hover:opacity-75",
      )}
      onClick={() => onSelect(entry.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(entry.id);
        }
      }}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <ActivityRowStatusDot
          kind={statusKind}
          pulse={entry.status === "active" && blockedCount === 0}
          // #22163 traded the "blocked N" chip for this dot to hold the row to
          // one line. The count and the reasons are then the dot's only
          // carrier — a blocked session labelled "Session paused" would be
          // both wrong and the one thing a screen reader could learn.
          label={
            blockedCount > 0
              ? [
                  `Blocked attention: ${blockedCount}`,
                  ...(entry.attentionReasons ?? []),
                ].join("; ")
              : `Session ${entry.status}`
          }
          title={
            blockedCount > 0 ? entry.attentionReasons?.join("; ") : undefined
          }
          glyph={statusGlyph}
        />
        <SourceIcon source={entry.provider} size={14} />
        <TickerText className="activity-row-title">{displayLabel}</TickerText>
      </div>
      <div className="flex items-center gap-1.5">
        {renderBadges(entry)}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          dense
          className={cn(
            "session-more-btn size-7 min-h-7 min-w-7 shrink-0 p-0 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]",
            coarseHitAreaCls,
          )}
          onClick={(event) => onMenuButtonClick(event, entry)}
          title="Session actions"
          aria-label="Session actions"
          aria-haspopup="menu"
        >
          <KebabIcon />
        </Button>
      </div>
    </div>
  );
}
