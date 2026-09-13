import { useEffect, useState } from "react";
import { Button } from "../../../../web/src/components/ui/Button";
import { Textarea } from "../../../../web/src/components/ui/Textarea";
import type { Annotation } from "@gobby/annotate-core";
export function SaveStatus({ status }: { status: string }) {
  const [settled, setSettled] = useState(status === "Saved locally");
  useEffect(() => {
    if (status !== "Saved locally") {
      setSettled(false);
      return;
    }
    // Coalesce success announcements while typing; persistence is never delayed.
    const timer = setTimeout(() => setSettled(true), 800);
    return () => clearTimeout(timer);
  }, [status]);
  return (
    <p role="status">
      {status === "Saved locally" && !settled ? "Saving…" : status}
    </p>
  );
}
export function CapturePanel({
  annotation,
  image,
  status,
  onEdit,
  onClose,
  onDelete,
}: {
  annotation: Annotation;
  image: string | null;
  status: string;
  onEdit: (text: string) => void;
  onClose: () => void;
  onDelete: () => void;
}) {
  const [comment, setComment] = useState(annotation.comment);
  return (
    <section className="annotate-panel" aria-label="Edit annotation">
      <div className="annotate-heading">
        <strong>Annotation</strong>
        <Button onClick={onClose}>Done</Button>
      </div>
      <p className="annotate-context">
        {annotation.target.kind === "element"
          ? annotation.target.locator.join(" › ")
          : "Visible region"}
      </p>
      <label htmlFor="annotation-comment">What should change?</label>
      <Textarea
        id="annotation-comment"
        autoFocus
        value={comment}
        maxLength={20000}
        rows={3}
        onChange={(e) => {
          setComment(e.target.value);
          onEdit(e.target.value);
        }}
      />
      <SaveStatus status={status} />
      {image ? (
        <details>
          <summary>Review screenshot</summary>
          <img
            src={image}
            alt={
              annotation.screenshot.status === "available" &&
              annotation.screenshot.sourceBounds
                ? "Selected region at selection time"
                : "Original visible page at selection time"
            }
          />
        </details>
      ) : (
        <p>
          Screenshot unavailable:{" "}
          {annotation.screenshot.status === "unavailable"
            ? annotation.screenshot.reason
            : "Loading…"}
        </p>
      )}
      {annotation.frame.access === "host-only" && (
        <p>Host selected. Hidden descendants are inaccessible.</p>
      )}
      <Button variant="destructive" onClick={onDelete}>
        Delete annotation
      </Button>
    </section>
  );
}
