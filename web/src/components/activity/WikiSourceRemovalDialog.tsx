import { useState } from "react";

import type { WikiEnvelope, WikiSourceRecord } from "../../hooks/useWiki";
import { JsonBlock } from "../chat/JsonBlock";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "../chat/ui/Dialog";

interface WikiSourceRemovalDialogProps {
  source: WikiSourceRecord | null;
  preview: WikiEnvelope | null;
  isPreviewLoading: boolean;
  isConfirming: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (options: { keep_asset: boolean }) => void;
}

function sourceLabel(source: WikiSourceRecord): string {
  return source.title || source.path || source.raw_path || source.id;
}

export function WikiSourceRemovalDialog({
  source,
  preview,
  isPreviewLoading,
  isConfirming,
  error,
  onCancel,
  onConfirm,
}: WikiSourceRemovalDialogProps) {
  const sourceId = source?.id ?? null;
  const [keepAssetDraft, setKeepAssetDraft] = useState<{
    sourceId: string | null;
    keepAsset: boolean;
  }>({ sourceId: null, keepAsset: false });
  const keepAsset =
    keepAssetDraft.sourceId === sourceId ? keepAssetDraft.keepAsset : false;

  return (
    <Dialog open={source !== null} onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="max-w-2xl">
        <DialogTitle>Remove wiki source</DialogTitle>
        <DialogDescription>
          {source ? `Review the CLI dry-run preview for ${sourceLabel(source)}.` : ""}
        </DialogDescription>

        <div className="mt-4 space-y-4">
          {isPreviewLoading ? (
            <div className="rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
              Loading preview...
            </div>
          ) : (
            <div className="max-h-80 overflow-auto rounded-md border border-border bg-muted/20 p-3">
              <JsonBlock
                value={preview ?? {}}
                className="text-xs"
                breakMode="all"
                testId="wiki-removal-preview"
              />
            </div>
          )}

          {error ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          ) : null}

          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={keepAsset}
              onChange={(event) =>
                setKeepAssetDraft({ sourceId, keepAsset: event.target.checked })
              }
              className="h-4 w-4"
            />
            Keep source asset
          </label>

          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onCancel}
              disabled={isConfirming}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => onConfirm({ keep_asset: keepAsset })}
              disabled={!preview || isPreviewLoading || isConfirming}
              className="rounded-md bg-destructive px-3 py-1.5 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
            >
              {isConfirming ? "Removing..." : "Confirm removal"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
