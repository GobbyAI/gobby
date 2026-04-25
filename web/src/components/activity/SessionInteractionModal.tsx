import { useState, useCallback, useEffect, useRef } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "../chat/ui/Dialog";
import { useMcp, type McpToolSchema } from "../../hooks/useMcp";
import { ToolArgumentForm } from "../command-browser/ToolArgumentForm";
import { getToolCallError, isSuccessfulToolCall } from "./toolCallStatus";

interface SessionEntry {
  id: string;
  type: "agent" | "cli";
  label: string;
  hasTmux: boolean;
  runId?: string;
  seqNum?: number | null;
}

export type InteractionMode = "context" | "command" | "keys" | "pane";

interface SessionInteractionModalProps {
  open: boolean;
  onClose: () => void;
  mode: InteractionMode;
  entry: SessionEntry;
  fromSessionId?: string;
}

const MODE_CONFIG: Record<
  InteractionMode,
  { title: string; description: string; placeholder: string }
> = {
  context: {
    title: "Send Context",
    description:
      "Inject context into the session. The agent will see this on its next hook cycle.",
    placeholder: "Enter context to inject...",
  },
  command: {
    title: "Send Command",
    description: "Execute an MCP tool against this session's server.",
    placeholder: "",
  },
  keys: {
    title: "Send Keys",
    description: "Send keystrokes directly to the tmux terminal.",
    placeholder: "Type text to send...",
  },
  pane: {
    title: "Capture Pane",
    description: "Terminal output from the session.",
    placeholder: "",
  },
};

// Quick-send buttons for keys mode
const QUICK_KEYS = [
  { label: "Ctrl-C", keys: "C-c", literal: false },
  { label: "Enter", keys: "Enter", literal: false },
  { label: "Escape", keys: "Escape", literal: false },
  { label: "y + Enter", keys: "y\n", literal: true },
  { label: "n + Enter", keys: "n\n", literal: true },
];

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

async function callTool(
  serverName: string,
  toolName: string,
  args: Record<string, unknown>,
): Promise<any> {
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
      const body = await response.json();
      detail = body.error || body.message || body.detail || JSON.stringify(body);
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
  mode,
  entry,
  fromSessionId,
}: SessionInteractionModalProps) {
  const [text, setText] = useState("");
  const [literal, setLiteral] = useState(true);
  const [paneOutput, setPaneOutput] = useState<string | null>(null);
  const [paneLoading, setPaneLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // MCP tool selector state (command mode)
  const {
    servers,
    toolsByServer,
    fetchServers,
    fetchTools,
    fetchToolSchema,
    callTool: mcpCallTool,
  } = useMcp();
  const [selectedServer, setSelectedServer] = useState<string>("");
  const [selectedTool, setSelectedTool] = useState<string>("");
  const [toolSchema, setToolSchema] = useState<McpToolSchema | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});
  const [toolResult, setToolResult] = useState<{
    success: boolean;
    data?: unknown;
    error?: string;
  } | null>(null);

  // Focus input when modal opens
  useEffect(() => {
    if (open && mode !== "pane" && mode !== "command") {
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [open, mode]);

  // Fetch schema when tool is selected
  const handleToolSelect = useCallback(
    async (toolName: string) => {
      setSelectedTool(toolName);
      setFormValues({});
      setToolResult(null);
      setToolSchema(null);
      if (!toolName || !selectedServer) return;
      setSchemaLoading(true);
      const fetched = await fetchToolSchema(selectedServer, toolName);
      setToolSchema(fetched);
      setSchemaLoading(false);
    },
    [selectedServer, fetchToolSchema],
  );

  // Handle server change — reset tool + schema
  const handleServerSelect = useCallback((serverName: string) => {
    setSelectedServer(serverName);
    setSelectedTool("");
    setToolSchema(null);
    setFormValues({});
    setToolResult(null);
  }, []);

  // Execute selected MCP tool
  const handleExecuteTool = useCallback(async () => {
    if (!selectedServer || !selectedTool) return;
    setSending(true);
    setError(null);
    setToolResult(null);
    try {
      const res = await mcpCallTool(selectedServer, selectedTool, formValues);
      setToolResult({
        success: res.success,
        data: res.result,
        error: res.error,
      });
    } catch (err) {
      setToolResult({
        success: false,
        error: err instanceof Error ? err.message : "Tool execution failed",
      });
    } finally {
      setSending(false);
    }
  }, [selectedServer, selectedTool, formValues, mcpCallTool]);

  const fetchPane = useCallback(async () => {
    setPaneLoading(true);
    setError(null);
    try {
      const result = await callTool("gobby-sessions", "capture_output", {
        session_id: entry.id,
        lines: 80,
      });
      if (result?.success) {
        setPaneOutput(result.output ?? result.result?.output ?? "");
      } else {
        setError(
          result?.error ?? result?.result?.error ?? "Failed to capture pane",
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to capture pane");
    } finally {
      setPaneLoading(false);
    }
  }, [entry.id]);

  // Reset state when modal opens
  useEffect(() => {
    if (open) {
      setText("");
      setLiteral(true);
      setError(null);
      setPaneOutput(null);
      setSending(false);
      setSelectedServer("");
      setSelectedTool("");
      setToolSchema(null);
      setFormValues({});
      setToolResult(null);
      if (mode === "pane") {
        fetchPane();
      }
      if (mode === "command") {
        fetchServers();
        fetchTools();
      }
    }
  }, [open, mode, fetchPane, fetchServers, fetchTools]);

  const handleSend = useCallback(async () => {
    if (!text.trim() && mode !== "keys") return;
    setSending(true);
    setError(null);
    try {
      let result: any;
      if (mode === "context") {
        result = await callTool("gobby-agents", "send_message", {
          from_session: fromSessionId ?? "",
          to_session: entry.id,
          content: text,
        });
      } else if (mode === "keys") {
        // In literal mode, append \n so the backend sends Enter after the text.
        // The textarea can't produce a real \n character, and users almost
        // always want their input submitted.
        const keysToSend = literal && text ? text + "\n" : text;
        result = await callTool("gobby-sessions", "send_keys", {
          session_id: entry.id,
          keys: keysToSend,
          literal,
        });
      }
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
  }, [text, literal, mode, entry.id, fromSessionId, onClose]);

  const handleQuickKey = useCallback(
    async (keys: string, isLiteral: boolean) => {
      setSending(true);
      setError(null);
      try {
        const result = await callTool("gobby-sessions", "send_keys", {
          session_id: entry.id,
          keys,
          literal: isLiteral,
        });
        if (isSuccessfulToolCall(result)) {
          onClose();
        } else {
          setError(getToolCallError(result, "Failed to send keys"));
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to send keys");
      } finally {
        setSending(false);
      }
    },
    [entry.id, onClose],
  );

  const config = MODE_CONFIG[mode];
  const displayLabel = entry.seqNum
    ? `#${entry.seqNum}: ${entry.label}`
    : entry.label;

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className={mode === "command" ? "max-w-lg" : "max-w-md md:max-w-2xl"}>
        <DialogTitle>{config.title}</DialogTitle>
        <DialogDescription>
          {config.description}
          <br />
          <span className="text-xs text-muted-foreground mt-1">
            Target: {displayLabel}
          </span>
        </DialogDescription>

        {mode === "pane" ? (
          <div className="mt-3">
            {paneLoading ? (
              <div className="text-xs text-muted-foreground p-3">
                Loading...
              </div>
            ) : (
              <pre className="session-pane-output">
                {paneOutput ?? "No output"}
              </pre>
            )}
            <div className="flex justify-end gap-2 mt-3">
              <button
                className="session-modal-btn session-modal-btn--secondary"
                onClick={fetchPane}
                disabled={paneLoading}
              >
                Refresh
              </button>
              <button className="session-modal-btn" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        ) : mode === "command" ? (
          <div className="mt-3 flex flex-col gap-3">
            {/* Server dropdown */}
            <div>
              <label className="block text-sm font-medium text-foreground mb-1">
                Server
              </label>
              <select
                className="flex h-9 w-full rounded-md border border-border bg-transparent px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                value={selectedServer}
                onChange={(e) => handleServerSelect(e.target.value)}
              >
                <option value="">-- select server --</option>
                {servers
                  .filter((s) => s.state === "connected")
                  .map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}
                    </option>
                  ))}
              </select>
            </div>

            {/* Tool dropdown */}
            {selectedServer && (
              <div>
                <label className="block text-sm font-medium text-foreground mb-1">
                  Tool
                </label>
                <select
                  className="flex h-9 w-full rounded-md border border-border bg-transparent px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  value={selectedTool}
                  onChange={(e) => handleToolSelect(e.target.value)}
                >
                  <option value="">-- select tool --</option>
                  {(toolsByServer[selectedServer] || []).map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {/* Schema loading */}
            {schemaLoading && (
              <p className="text-xs text-muted-foreground">
                Loading schema...
              </p>
            )}

            {/* Dynamic args form */}
            {toolSchema && !schemaLoading && (
              <div>
                <label className="block text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                  Arguments
                </label>
                <ToolArgumentForm
                  schema={toolSchema.inputSchema}
                  values={formValues}
                  onChange={setFormValues}
                  disabled={sending}
                />
              </div>
            )}

            {/* Result display */}
            {toolResult && (
              <div
                className={`rounded-md border p-3 text-sm font-mono whitespace-pre-wrap overflow-x-auto max-h-[20vh] overflow-y-auto ${
                  toolResult.success
                    ? "border-success/50 bg-success/5 text-foreground"
                    : "border-destructive-foreground/50 bg-destructive/5 text-destructive-foreground"
                }`}
              >
                {toolResult.error
                  ? `Error: ${toolResult.error}`
                  : JSON.stringify(toolResult.data, null, 2)}
              </div>
            )}

            {error && <p className="text-xs text-red-400 mt-2">{error}</p>}

            <div className="flex justify-end gap-2 mt-1">
              <button
                className="session-modal-btn session-modal-btn--secondary"
                onClick={onClose}
              >
                Cancel
              </button>
              <button
                className="session-modal-btn"
                onClick={handleExecuteTool}
                disabled={sending || !selectedServer || !selectedTool}
              >
                {sending ? "Executing..." : "Execute"}
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-3">
            <textarea
              ref={inputRef}
              className="session-modal-textarea"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={config.placeholder}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  handleSend();
                }
              }}
            />

            {mode === "keys" && (
              <>
                <div className="flex items-center gap-2 mt-2">
                  <label className="text-xs text-muted-foreground flex items-center gap-1.5">
                    <input
                      type="checkbox"
                      checked={literal}
                      onChange={(e) => setLiteral(e.target.checked)}
                      className="rounded"
                    />
                    Literal mode
                  </label>
                  <span className="text-xs text-muted-foreground">
                    {literal
                      ? "(text as-is, \\n = Enter)"
                      : "(tmux key names: C-c, Escape, etc.)"}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {QUICK_KEYS.map((qk) => (
                    <button
                      key={qk.label}
                      className="session-modal-quickkey"
                      onClick={() => handleQuickKey(qk.keys, qk.literal)}
                      disabled={sending}
                    >
                      {qk.label}
                    </button>
                  ))}
                </div>
              </>
            )}

            {error && <p className="text-xs text-red-400 mt-2">{error}</p>}

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
                disabled={sending || (!text.trim() && mode !== "keys")}
              >
                {sending ? "Sending..." : "Send"}
              </button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
