/**
 * In-pane sources view for the wiki tab (plan wiki-obsidian-panel §2.2),
 * opened from the kebab menu. Fresh design over the kept useWiki sources
 * data + WikiSourceRemovalDialog — the legacy sources table is gone.
 */

import { useCallback, useMemo, useState } from "react";

import type {
  WikiEnvelope,
  WikiRemoveSourceRequest,
  WikiSourceRecord,
} from "../../../hooks/useWiki";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { ActivityPanelSearch } from "../ActivityPanelSearch";
import { WikiSourceRemovalDialog } from "../WikiSourceRemovalDialog";

export interface WikiSourcesManagerProps {
  sources: WikiSourceRecord[];
  isLoading: boolean;
  error: string | null;
  onClose: () => void;
  removeSource: (request: WikiRemoveSourceRequest) => Promise<WikiEnvelope>;
  /** Fired after a confirmed removal so the owner refreshes the listing. */
  onRemoved: () => void | Promise<void>;
}

function sourceTitle(source: WikiSourceRecord): string {
  return (
    source.title?.trim() || source.wiki_path || source.page_path || source.id
  );
}

function sourceDetail(source: WikiSourceRecord): string | null {
  return (
    source.url ??
    source.source_url ??
    source.raw_path ??
    source.wiki_path ??
    source.page_path ??
    null
  );
}

export function WikiSourcesManager({
  sources,
  isLoading,
  error,
  onClose,
  removeSource,
  onRemoved,
}: WikiSourcesManagerProps) {
  const [filter, setFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [removalTarget, setRemovalTarget] = useState<WikiSourceRecord | null>(
    null,
  );
  const [preview, setPreview] = useState<WikiEnvelope | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [removalError, setRemovalError] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return sources;
    return sources.filter((source) =>
      [sourceTitle(source), sourceDetail(source) ?? "", source.id]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [filter, sources]);

  const selected = useMemo(
    () => sources.find((source) => source.id === selectedId) ?? null,
    [selectedId, sources],
  );

  const startRemoval = useCallback(
    async (source: WikiSourceRecord) => {
      setRemovalTarget(source);
      setPreview(null);
      setRemovalError(null);
      setIsPreviewLoading(true);
      try {
        setPreview(await removeSource({ id: source.id, dry_run: true }));
      } catch (previewError) {
        setRemovalError(
          previewError instanceof Error
            ? previewError.message
            : String(previewError),
        );
      } finally {
        setIsPreviewLoading(false);
      }
    },
    [removeSource],
  );

  const cancelRemoval = useCallback(() => {
    setRemovalTarget(null);
    setPreview(null);
    setRemovalError(null);
    setIsConfirming(false);
  }, []);

  const confirmRemoval = useCallback(
    async ({ keep_asset }: { keep_asset: boolean }) => {
      if (!removalTarget) return;
      setIsConfirming(true);
      setRemovalError(null);
      try {
        await removeSource({ id: removalTarget.id, yes: true, keep_asset });
        setSelectedId((current) =>
          current === removalTarget.id ? null : current,
        );
        cancelRemoval();
        await onRemoved();
      } catch (confirmError) {
        setRemovalError(
          confirmError instanceof Error
            ? confirmError.message
            : String(confirmError),
        );
        setIsConfirming(false);
      }
    },
    [cancelRemoval, onRemoved, removalTarget, removeSource],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <h3 className="text-sm font-semibold text-foreground">Wiki sources</h3>
        <span className="text-xs text-muted-foreground">{sources.length}</span>
        <div className="ml-auto flex items-center gap-2">
          <Button
            type="button"
            onClick={onClose}
            variant="secondary"
            size="sm"
            className={coarseHitAreaCls}
          >
            Back to wiki
          </Button>
        </div>
      </div>
      <div className="border-b border-border px-3 py-2">
        <ActivityPanelSearch
          value={filter}
          onChange={setFilter}
          placeholder="Filter sources"
          ariaLabel="Filter sources"
        />
      </div>

      {error ? (
        <p
          role="alert"
          className="px-3 py-2 text-xs text-destructive-foreground"
        >
          {error}
        </p>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <p className="px-3 py-4 text-sm text-muted-foreground">
            Loading sources…
          </p>
        ) : filtered.length === 0 ? (
          <p className="px-3 py-4 text-sm text-muted-foreground">
            {sources.length === 0
              ? "No sources attached yet. Attach a file or ingest a URL from the wiki actions menu."
              : "No sources match the filter."}
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {filtered.map((source) => {
              const detail = sourceDetail(source);
              const isSelected = source.id === selectedId;
              return (
                <li key={source.id}>
                  <div
                    className={cn(
                      "flex items-center gap-2 px-3 py-2",
                      isSelected && "bg-muted/50",
                    )}
                  >
                    <Button
                      type="button"
                      onClick={() =>
                        setSelectedId(isSelected ? null : source.id)
                      }
                      variant="ghost"
                      size="sm"
                      className={`${coarseHitAreaCls} h-auto min-w-0 flex-1 flex-col items-start justify-start gap-0 px-0 py-0 text-left`}
                      aria-expanded={isSelected}
                    >
                      <span className="block truncate text-sm text-foreground">
                        {sourceTitle(source)}
                      </span>
                      {detail ? (
                        <span className="block truncate font-mono text-xs text-muted-foreground">
                          {detail}
                        </span>
                      ) : null}
                    </Button>
                    <Button
                      type="button"
                      onClick={() => void startRemoval(source)}
                      variant="secondary"
                      size="sm"
                      className={`${coarseHitAreaCls} shrink-0 hover:bg-destructive/10 hover:text-destructive-foreground`}
                      aria-label={`Remove ${sourceTitle(source)}`}
                    >
                      Remove
                    </Button>
                  </div>
                  {isSelected && selected ? (
                    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-3 pb-3 text-xs">
                      {Object.entries(selected)
                        .filter(
                          ([, value]) => typeof value === "string" && value,
                        )
                        .map(([key, value]) => (
                          <div key={key} className="contents">
                            <dt className="text-muted-foreground">{key}</dt>
                            <dd className="min-w-0 truncate font-mono text-foreground">
                              {String(value)}
                            </dd>
                          </div>
                        ))}
                    </dl>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <WikiSourceRemovalDialog
        source={removalTarget}
        preview={preview}
        isPreviewLoading={isPreviewLoading}
        isConfirming={isConfirming}
        error={removalError}
        onCancel={cancelRemoval}
        onConfirm={(options) => void confirmRemoval(options)}
      />
    </div>
  );
}
