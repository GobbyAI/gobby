import { useState, useCallback, useEffect, useRef } from "react";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "../ui/Dialog";
import { Textarea } from "../ui/Textarea";
import { getToolCallError, isSuccessfulToolCall } from "./toolCallStatus";

interface SessionEntry {
  id: string;
  label: string;
  seqNum?: number | null;
}

interface SessionInteractionModalProps {
  open: boolean;
  onClose: () => void;
  entry: SessionEntry;
  fromSessionId?: string;
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function getStringField(value: unknown, field: string): string | undefined {
  return isRecord(value) && typeof value[field] === "string"
    ? value[field]
    : undefined;
}

async function callTool(
  serverName: string,
  toolName: string,
  args: Record<string, unknown>,
): Promise<unknown> {
  const baseUrl = getBaseUrl();
  const response = await fetch(`${baseUrl}/api/mcp/tools/call`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      server_name: serverName,
      tool_name: toolName,
      arguments: args,
    }),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body: unknown = await response.json();
      detail =
        getStringField(body, "error") ??
        getStringField(body, "message") ??
        getStringField(body, "detail") ??
        JSON.stringify(body);
    } catch {
      try {
        detail = await response.text();
      } catch {
        // keep statusText
      }
    }
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }
  return response.json();
}

export function SessionInteractionModal({
  open,
  onClose,
  entry,
  fromSessionId,
}: SessionInteractionModalProps) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const timeout = window.setTimeout(() => inputRef.current?.focus(), 100);
    return () => window.clearTimeout(timeout);
  }, [open]);

  useEffect(() => {
    if (open) {
      setText("");
      setError(null);
      setSending(false);
    }
  }, [open]);

  const handleSend = useCallback(async () => {
    if (!text.trim()) return;
    setSending(true);
    setError(null);
    try {
      const result = await callTool("gobby-agents", "send_message", {
        ...(fromSessionId ? { from_session: fromSessionId } : {}),
        target: "session",
        target_id: entry.id,
        content: text,
      });
      if (isSuccessfulToolCall(result)) {
        onClose();
      } else {
        setError(getToolCallError(result, "Operation failed"));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Operation failed");
    } finally {
      setSending(false);
    }
  }, [text, entry.id, fromSessionId, onClose]);

  const displayLabel = entry.seqNum
    ? `#${entry.seqNum}: ${entry.label}`
    : entry.label;

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md md:max-w-2xl">
        <DialogTitle>Send Context</DialogTitle>
        <DialogDescription>
          Inject context into the session. The agent will see this on its next
          hook cycle.
          <br />
          <span className="mt-1 text-xs text-muted-foreground">
            Target: {displayLabel}
          </span>
        </DialogDescription>

        <div className="mt-3">
          <Textarea
            ref={inputRef}
            className="session-modal-textarea h-[4.5em] w-full resize-none overflow-y-auto rounded-md border border-border bg-[var(--bg-primary)] p-2 font-mono text-[length:var(--text-base)] text-[var(--text-primary)] focus:border-accent focus:outline-none"
            wrapperClassName="w-full"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Enter context to inject..."
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSend();
              }
            }}
          />

          {error && <p className="mt-2 text-xs text-error">{error}</p>}

          <div className="mt-3 flex justify-end gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              className={cn(
                "session-modal-btn session-modal-btn--secondary bg-transparent px-3 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] disabled:cursor-not-allowed disabled:opacity-50",
                coarseHitAreaCls,
              )}
              onClick={onClose}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="accent"
              size="sm"
              className={cn(
                "session-modal-btn px-3 py-1.5 text-[length:var(--text-md)] hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50",
                coarseHitAreaCls,
              )}
              onClick={handleSend}
              disabled={sending || !text.trim()}
            >
              {sending ? "Sending..." : "Send"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
