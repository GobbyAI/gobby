import type { JSX } from "react";
import type { QueuedFile } from "../../types/chat";
import { formatAttachmentSize } from "../../lib/chatAttachments";
import { PaperclipIcon } from "./ChatInputIcons";

interface ChatInputQueuedFilesProps {
  files: QueuedFile[];
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
}

export function ChatInputQueuedFiles({
  files,
  onRemove,
  onRetry,
}: ChatInputQueuedFilesProps): JSX.Element | null {
  if (files.length === 0) return null;

  return (
    <div className="mb-2 flex flex-wrap gap-2">
      {files.map((queuedFile) => (
        <div
          key={queuedFile.id}
          className="relative max-w-[180px] overflow-hidden rounded-md border border-border bg-muted"
        >
          {queuedFile.previewUrl ? (
            <img
              src={queuedFile.previewUrl}
              alt={queuedFile.file.name}
              loading="lazy"
              decoding="async"
              className="h-16 w-16 object-cover"
            />
          ) : (
            <div className="flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground">
              <PaperclipIcon />
              <span className="max-w-[100px] truncate">
                {queuedFile.file.name}
              </span>
            </div>
          )}
          <div className="px-2 pb-1 text-[length:var(--text-2xs)] text-muted-foreground">
            <div className="truncate">
              {formatAttachmentSize(
                queuedFile.attachment?.size_bytes ?? queuedFile.file.size,
              )}
            </div>
            {queuedFile.status === "uploading" && (
              <div>
                {queuedFile.progress === null
                  ? "Uploading"
                  : `${Math.round(queuedFile.progress * 100)}%`}
              </div>
            )}
            {queuedFile.status === "error" && (
              <button
                type="button"
                className="text-destructive-foreground underline"
                onClick={() => onRetry(queuedFile.id)}
                title={queuedFile.error ?? "Upload failed"}
              >
                Retry
              </button>
            )}
          </div>
          <button
            type="button"
            aria-label={`Remove ${queuedFile.file.name}`}
            className="absolute top-0 right-0 flex h-4 w-4 items-center justify-center rounded-bl bg-[var(--surface-scrim)] text-xs text-foreground pointer-coarse:h-11 pointer-coarse:w-11"
            onClick={() => onRemove(queuedFile.id)}
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  );
}
