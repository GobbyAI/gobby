import type { Dispatch, SetStateAction } from "react";

import type { SessionMessage } from "../../hooks/useSessionDetail";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { ArtifactContext } from "../chat/artifacts/ArtifactContext";
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

interface NoopArtifactContext {
  openCodeAsArtifact: () => void;
  openFileAsArtifact: () => void;
}

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
  noopArtifactCtx: NoopArtifactContext;
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
  noopArtifactCtx,
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
    <div className="flex-1 flex flex-col min-h-0">
      <div className="activity-panel-status-bar activity-panel-status-bar--detail">
        <div className="min-w-0 flex-1">
          <span className="activity-panel-status-bar__title">
            <span className="activity-panel-status-bar__watching-prefix">
              Watching{" "}
            </span>
            {selectedEntry
              ? selectedEntry.seqNum
                ? `#${selectedEntry.seqNum}: ${selectedEntry.label}`
                : selectedEntry.label
              : "session"}
          </span>
        </div>
        <div className="activity-panel-status-bar__actions">
          {showSummaryButton && (
            <button
              type="button"
              className="btn btn-accent btn-sm activity-panel-action-btn"
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
            </button>
          )}
          {showResumeButton && (
            <button
              type="button"
              className="btn btn-accent btn-sm activity-panel-action-btn"
              onClick={() => {
                void onResumeSession?.(selectedSessionId);
              }}
              aria-label="Resume"
              title="Resume"
            >
              <PlayIcon />
              <span className="activity-panel-action-btn__label">Resume</span>
            </button>
          )}
          {showSwapButton && selectedEntry && (
            <button
              type="button"
              className="btn btn-accent btn-sm activity-panel-action-btn"
              onClick={onSwapSelectedSession}
              aria-label="Swap"
              title="Swap"
            >
              <SwapIcon />
              <span className="activity-panel-action-btn__label">Swap</span>
            </button>
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
          <button
            type="button"
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_12%,transparent)]"
            onClick={clearSessionError}
            aria-label="Dismiss session error"
            title="Dismiss"
          >
            &times;
          </button>
        </div>
      )}

      <ArtifactContext.Provider value={noopArtifactCtx}>
        {contentMode === "summary" ? (
          <div className="flex-1 overflow-y-auto p-4">
            {summaryMarkdown ? (
              <div className="message-content">
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
      </ArtifactContext.Provider>
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
