import { useState } from "react";
import { Check, Pencil, Trash2 } from "lucide-react";
import { Button } from "../../../../web/src/components/ui/Button";
import { isExported, type Batch } from "./drafts";

const day = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });

function confirmText(target: Batch | "exported", exportedCount: number) {
  if (target === "exported")
    return `Delete ${exportedCount} exported ${exportedCount === 1 ? "batch" : "batches"}? Their notes, screenshots and staged ZIPs are removed. Drafts with unexported changes stay.`;
  const notes = target.annotations.length,
    staged = target.exports?.some((e) => e.staged);
  return [
    `Delete “${target.title}”? This removes its ${notes} ${notes === 1 ? "note" : "notes"} and screenshots from this browser`,
    staged ? " and its staged ZIP from the Gobby Annotate app." : ".",
    isExported(target) ? "" : " It has changes that were never exported.",
  ].join("");
}

/** Batch list with reopen, per-batch delete and clear-exported, each delete confirmed in place. */
export function Batches({
  batches,
  current,
  onOpen,
  onRemove,
  onClearExported,
}: {
  batches: Batch[];
  current: string | undefined;
  onOpen: (batchId: string) => void;
  onRemove: (batchId: string) => void;
  onClearExported: () => void;
}) {
  const [confirm, setConfirm] = useState<Batch | "exported" | null>(null);
  const exportedCount = batches.filter(isExported).length;
  return (
    <div
      className="annotate-group"
      role="group"
      aria-labelledby="annotate-batches"
    >
      <strong id="annotate-batches">Batches</strong>
      <ul className="annotate-batches">
        {batches.map((b) => (
          <li key={b.id} className="annotate-batch">
            <Button
              className="annotate-list-row"
              aria-current={b.id === current || undefined}
              onClick={() => onOpen(b.id)}
            >
              {b.title}
            </Button>
            <span className="annotate-batch-state">
              {isExported(b) ? (
                <>
                  <Check size={14} aria-hidden="true" />
                  Exported {day(b.exports!.at(-1)!.at)}
                </>
              ) : (
                <>
                  <Pencil size={14} aria-hidden="true" />
                  Draft
                </>
              )}
            </span>
            <Button
              size="icon"
              className="annotate-danger"
              aria-label={`Delete batch ${b.title}`}
              title="Delete batch"
              onClick={() => setConfirm(b)}
            >
              <Trash2 size={16} aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>
      {exportedCount > 0 && (
        <Button
          className="annotate-danger"
          onClick={() => setConfirm("exported")}
        >
          <Trash2 size={16} aria-hidden="true" />
          Clear exported batches ({exportedCount})
        </Button>
      )}
      {confirm && (
        <div
          role="alertdialog"
          aria-labelledby="annotate-confirm"
          className="annotate-confirm"
        >
          <p id="annotate-confirm">{confirmText(confirm, exportedCount)}</p>
          <Button
            className="annotate-danger"
            onClick={() => {
              setConfirm(null);
              if (confirm === "exported") onClearExported();
              else onRemove(confirm.id);
            }}
          >
            {confirm === "exported"
              ? `Delete ${exportedCount} exported`
              : "Delete batch"}
          </Button>
          <Button autoFocus onClick={() => setConfirm(null)}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
