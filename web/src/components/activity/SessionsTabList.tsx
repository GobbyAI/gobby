import type { MouseEvent, ReactNode } from "react";

import { SourceIcon } from "../shared/SourceIcon";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";
import { KebabIcon } from "./QuickMenu";
import {
  type WatchingSessionEntry,
  renderBadges,
} from "./SessionsTab.helpers";

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
        <div className="border-b border-border px-3 py-1.5 text-xs text-destructive-foreground" role="alert">
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
  const isPaused = entry.status !== "active";
  const displayLabel = entry.seqNum ? `#${entry.seqNum}: ${entry.label}` : entry.label;

  return (
    <div
      role="button"
      tabIndex={0}
      className={`session-entry${isSelected ? " session-entry--active" : ""}${isPaused ? " session-entry--paused" : ""}`}
      onClick={() => onSelect(entry.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(entry.id);
        }
      }}
    >
      <div className="flex items-center gap-2 min-w-0 flex-1">
        <ActivityRowStatusDot
          kind={
            entry.status === "active"
              ? "active"
              : entry.status === "expired"
                ? "stopped"
                : entry.status === "paused"
                  ? "paused"
                  : "warning"
          }
          pulse={entry.status === "active"}
          label={`Session ${entry.status}`}
        />
        <SourceIcon source={entry.provider} size={14} />
        <span className="activity-row-title">{displayLabel}</span>
      </div>
      <div className="flex items-center gap-1.5">
        {renderBadges(entry)}
        {entry.status !== "expired" && (
          <button
            className="session-more-btn"
            onClick={(event) => onMenuButtonClick(event, entry)}
            title="Session actions"
            aria-label="Session actions"
            aria-haspopup="menu"
          >
            <KebabIcon />
          </button>
        )}
      </div>
    </div>
  );
}
