import { useState, useEffect, useMemo, useCallback } from "react";
import { Dialog, DialogContent } from "./ui/Dialog";
import type { GobbySession } from "../../hooks/useSessions";
import { formatRelativeTime } from "../../utils/formatTime";

const SOURCE_LABELS: Record<string, string> = {
  claude_sdk_web_chat: "Web Chat",
  claude_code: "Claude Code",
  claude: "Claude",
  gemini_cli: "Gemini CLI",
  gemini: "Gemini",
  codex: "Codex",
  cursor: "Cursor",
  windsurf: "Windsurf",
  copilot: "Copilot",
};

const SOURCE_COLORS: Record<string, string> = {
  claude_sdk_web_chat: "#c084fc",
  claude_code: "#c084fc",
  claude: "#c084fc",
  gemini_cli: "#4ade80",
  gemini: "#4ade80",
  codex: "#3b82f6",
  cursor: "#f472b6",
  windsurf: "#38bdf8",
  copilot: "#818cf8",
};

interface ResumeSessionModalProps {
  isOpen: boolean;
  onClose: () => void;
  sessions: GobbySession[];
  onResume: (session: GobbySession) => void;
}

export function ResumeSessionModal({
  isOpen,
  onClose,
  sessions,
  onResume,
}: ResumeSessionModalProps) {
  const [search, setSearch] = useState("");
  const [showSubagents, setShowSubagents] = useState(false);
  const [resumableSessions, setResumableSessions] = useState<GobbySession[]>([]);
  const [loading, setLoading] = useState(false);

  // Fetch resumable sessions when modal opens
  const fetchResumable = useCallback(async () => {
    if (!isOpen) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        limit: "200",
        include_resumability: "true",
      });
      if (!showSubagents) params.set("exclude_subagents", "true");

      const response = await fetch(`/api/sessions?${params}`);
      if (response.ok) {
        const data = await response.json();
        setResumableSessions(Array.isArray(data.sessions) ? data.sessions : []);
      } else {
        console.error("Failed to fetch resumable sessions:", response.status, response.statusText);
        setResumableSessions(sessions);
      }
    } catch (e) {
      console.error("Failed to fetch resumable sessions:", e);
      // Fall back to passed-in sessions
      setResumableSessions(sessions);
    } finally {
      setLoading(false);
    }
  }, [isOpen, showSubagents, sessions]);

  // Reset search when modal opens
  useEffect(() => {
    if (isOpen) {
      setSearch("");
    }
  }, [isOpen]);

  // Fetch resumable sessions when modal opens or subagent toggle changes
  useEffect(() => {
    if (isOpen) {
      fetchResumable();
    }
  }, [isOpen, fetchResumable]);

  // Filter and sort sessions
  const filteredSessions = useMemo(() => {
    const withMessages = resumableSessions.filter((s) => s.message_count > 0);
    const sorted = withMessages.sort(
      (a, b) =>
        new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    );
    if (!search.trim()) return sorted.slice(0, 50);
    const q = search.toLowerCase();
    return sorted.filter(
      (s) =>
        (s.title && s.title.toLowerCase().includes(q)) ||
        s.source.toLowerCase().includes(q) ||
        (s.ref && s.ref.toLowerCase().includes(q)),
    );
  }, [resumableSessions, search]);

  if (!isOpen) return null;

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-2xl h-[70vh] p-0 overflow-hidden flex flex-col">
        <div style={{ padding: "16px 20px 12px", borderBottom: "1px solid var(--border-color, #333)" }}>
          <h2 style={{ margin: 0, fontSize: "16px", fontWeight: 600 }}>
            Resume Session
          </h2>
          <p style={{ margin: "4px 0 12px", fontSize: "13px", color: "var(--text-muted, #888)" }}>
            Pick a session to resume in web chat with full conversation context.
          </p>
          <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            <input
              type="text"
              placeholder="Search sessions..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              autoFocus
              style={{
                flex: 1,
                padding: "8px 12px",
                border: "1px solid var(--border-color, #444)",
                borderRadius: "6px",
                background: "var(--input-bg, #1a1a1a)",
                color: "var(--text-color, #e0e0e0)",
                fontSize: "14px",
                outline: "none",
              }}
            />
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: "4px",
                fontSize: "12px",
                color: "var(--text-muted, #888)",
                cursor: "pointer",
                whiteSpace: "nowrap",
                userSelect: "none",
              }}
            >
              <input
                type="checkbox"
                checked={showSubagents}
                onChange={(e) => setShowSubagents(e.target.checked)}
                style={{ accentColor: "#c084fc" }}
              />
              Subagents
            </label>
          </div>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
          {loading ? (
            <p style={{ textAlign: "center", color: "var(--text-muted, #888)", padding: "24px 0", fontSize: "14px" }}>
              Loading...
            </p>
          ) : filteredSessions.length === 0 ? (
            <p style={{ textAlign: "center", color: "var(--text-muted, #888)", padding: "24px 0", fontSize: "14px" }}>
              {search ? "No matching sessions" : "No resumable sessions"}
            </p>
          ) : (
            filteredSessions.map((session) => (
              <button
                key={session.id}
                onClick={() => {
                  onResume(session);
                  onClose();
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "12px",
                  width: "100%",
                  padding: "10px 12px",
                  border: "none",
                  borderRadius: "6px",
                  background: "transparent",
                  color: "var(--text-color, #e0e0e0)",
                  cursor: "pointer",
                  textAlign: "left",
                  fontSize: "14px",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--hover-bg, #2a2a2a)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                }}
              >
                <span
                  style={{
                    display: "inline-block",
                    width: "8px",
                    height: "8px",
                    borderRadius: "50%",
                    background: SOURCE_COLORS[session.source] ?? "#737373",
                    flexShrink: 0,
                  }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {session.seq_num != null ? `#${session.seq_num}: ` : ''}{session.title || `Session ${session.ref || session.id.slice(0, 8)}`}
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--text-muted, #888)", marginTop: "2px" }}>
                    {SOURCE_LABELS[session.source] ?? session.source}
                    {" · "}
                    {formatRelativeTime(session.updated_at)}
                    {session.message_count > 0 && ` · ${session.message_count} msgs`}
                    {(session.agent_depth ?? 0) > 0 && (
                      <span style={{ color: "#f59e0b", marginLeft: "4px" }}>
                        depth {session.agent_depth}
                      </span>
                    )}
                  </div>
                </div>
              </button>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
