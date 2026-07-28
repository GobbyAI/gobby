import { useState, useCallback, useEffect, useRef } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "../ui/Dialog";
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
          <span className="text-xs text-muted-foreground mt-1">
            Target: {displayLabel}
          </span>
        </DialogDescription>

        <div className="mt-3">
          <textarea
            ref={inputRef}
            className="session-modal-textarea"
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

          {error && <p className="text-xs text-error mt-2">{error}</p>}

          <div className="flex justify-end gap-2 mt-3">
            <button
              className="session-modal-btn session-modal-btn--secondary"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              className="session-modal-btn"
              onClick={handleSend}
              disabled={sending || !text.trim()}
            >
              {sending ? "Sending..." : "Send"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
