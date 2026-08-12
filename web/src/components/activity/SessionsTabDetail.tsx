import type { Dispatch, SetStateAction } from "react";

import type { SessionMessage } from "../../hooks/useSessionDetail";
import { cn } from "../../lib/utils";
import { ResizeHandle } from "../shared/ResizeHandle";
import { markdownBodyClassName } from "../shared/MarkdownBody";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { MemoizedMarkdown } from "../shared/MemoizedMarkdown";
import {
  ClipboardListIcon,
  PlayIcon,
  SwapIcon,
  TranscriptIcon,
} from "../icons";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import type { WatchingSessionEntry } from "./SessionsTab.helpers";
import { WatchingTranscript } from "./WatchingTranscript";

export type WatchingContentMode = "transcript" | "summary";

interface SessionsTabDetailProps {
  clearSessionError: () => void;
  contentMode: WatchingContentMode;
  firstItemIndex: number;
  hasMoreMessages: boolean;
  hasNewerMessages: boolean;
  isLoadingDetail: boolean;
  isLoadingNewer: boolean;
  isLoadingOlder: boolean;
  loadMoreMessages: () => void;
  loadNewerMessages: () => void;
  messages: SessionMessage[];
  onResumeSession?: (sessionId: string) => Promise<string> | string | void;
  onSwapSelectedSession: () => void;
  selectedEntry: WatchingSessionEntry | null;
  selectedSessionId: string;
  sessionError: string | null;
  transcriptDownloadUrl: string | null;
  setContentMode: Dispatch<SetStateAction<WatchingContentMode>>;
  setTranscriptAtBottom: (atBottom: boolean) => void;
  showResumeButton: boolean;
  showSummaryButton: boolean;
  showSwapButton: boolean;
  summaryMarkdown: string | null;
  transcriptDegradedReason: string | null;
  transcriptEmptyStateMessage: string;
}

export function SessionsTabDetailPane({
  clearSessionError,
  contentMode,
  firstItemIndex,
  hasMoreMessages,
  hasNewerMessages,
  isLoadingDetail,
  isLoadingNewer,
  isLoadingOlder,
  loadMoreMessages,
  loadNewerMessages,
  messages,
  onResumeSession,
  onSwapSelectedSession,
  selectedEntry,
  selectedSessionId,
  sessionError,
  transcriptDownloadUrl,
  setContentMode,
  setTranscriptAtBottom,
  showResumeButton,
  showSummaryButton,
  showSwapButton,
  summaryMarkdown,
  transcriptDegradedReason,
  transcriptEmptyStateMessage,
}: SessionsTabDetailProps) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="activity-panel-status-bar activity-panel-status-bar--detail">
        <div className="min-w-0 flex-1">
          <span className="activity-panel-status-bar__title">
            <span className="activity-panel-status-bar__watching-prefix">
              Watching{" "}
            </span>
            {/* Provisional titles already carry the #ref, so the bar adds only
                the "Watching " prefix (#19152, moat 616342c4). */}
            {selectedEntry ? selectedEntry.label : "session"}
          </span>
        </div>
        <div className="activity-panel-status-bar__actions">
          {showSummaryButton && (
            <Button
              type="button"
              variant="accent"
              size="sm"
              className="activity-panel-action-btn"
              onClick={() =>
                setContentMode((current) =>
                  current === "summary" ? "transcript" : "summary",
                )
              }
              aria-label={contentMode === "summary" ? "Transcript" : "Summary"}
              title={contentMode === "summary" ? "Transcript" : "Summary"}
            >
              {contentMode === "summary" ? (
                <>
                  <TranscriptIcon />
                  <span className="activity-panel-action-btn__label">
                    Transcript
                  </span>
                </>
              ) : (
                <>
                  <ClipboardListIcon />
                  <span className="activity-panel-action-btn__label">
                    Summary
                  </span>
                </>
              )}
            </Button>
          )}
          {showResumeButton && (
            <Button
              type="button"
              variant="accent"
              size="sm"
              className="activity-panel-action-btn"
              onClick={() => {
                void onResumeSession?.(selectedSessionId);
              }}
              aria-label="Resume"
              title="Resume"
            >
              <PlayIcon />
              <span className="activity-panel-action-btn__label">Resume</span>
            </Button>
          )}
          {showSwapButton && selectedEntry && (
            <Button
              type="button"
              variant="accent"
              size="sm"
              className="activity-panel-action-btn"
              onClick={onSwapSelectedSession}
              aria-label="Swap"
              title="Swap"
            >
              <SwapIcon />
              <span className="activity-panel-action-btn__label">Swap</span>
            </Button>
          )}
        </div>
      </div>

      {sessionError && (
        <div
          role="alert"
          className="flex items-center gap-2 border-b border-border px-3 py-2 text-sm text-[var(--color-error)]"
        >
          <span className="min-w-0 flex-1">
            {sessionError}
            {transcriptDownloadUrl && (
              <>
                {" "}
                <a className="underline" href={transcriptDownloadUrl} download>
                  Download transcript instead
                </a>
              </>
            )}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className={cn(
              "h-6 min-h-6 w-6 shrink-0 text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_12%,transparent)]",
              coarseHitAreaCls,
            )}
            onClick={clearSessionError}
            aria-label="Dismiss session error"
            title="Dismiss"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              className="size-3.5"
              fill="none"
            >
              <path
                d="m4 4 8 8m0-8-8 8"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
              />
            </svg>
          </Button>
        </div>
      )}

      {contentMode === "summary" ? (
        <div className="flex-1 overflow-y-auto p-4">
          {summaryMarkdown ? (
            <div className={cn("message-content", markdownBodyClassName)}>
              <MemoizedMarkdown
                content={summaryMarkdown}
                id={`watch-summary-${selectedSessionId}`}
              />
            </div>
          ) : (
            <ActivityPanelEmpty body="No summary available" />
          )}
        </div>
      ) : (
        <WatchingTranscript
          sessionId={selectedSessionId}
          messages={messages}
          isLoading={isLoadingDetail}
          emptyStateMessage={transcriptEmptyStateMessage}
          hasMore={hasMoreMessages}
          loadMore={loadMoreMessages}
          hasNewer={hasNewerMessages}
          loadNewer={loadNewerMessages}
          isLoadingOlder={isLoadingOlder}
          isLoadingNewer={isLoadingNewer}
          setTranscriptAtBottom={setTranscriptAtBottom}
          firstItemIndex={firstItemIndex}
          transcriptDegradedReason={transcriptDegradedReason}
        />
      )}
    </div>
  );
}

interface SessionsTabResizeHandleProps {
  selectedSessionId: string | null;
  entriesLength: number;
  topHeight: number;
  onResize: (height: number) => void;
}

export function SessionsTabResizeHandle({
  selectedSessionId,
  entriesLength,
  topHeight,
  onResize,
}: SessionsTabResizeHandleProps) {
  if (!selectedSessionId || entriesLength === 0) {
    return null;
  }

  return (
    <ResizeHandle
      direction="vertical"
      onResize={onResize}
      panelHeight={topHeight}
      minHeight={15}
      maxHeight={80}
    />
  );
}
