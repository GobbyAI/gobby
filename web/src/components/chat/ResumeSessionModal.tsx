import { useState, useEffect, useMemo, useRef } from "react";
import { Dialog, DialogContent, DialogDescription } from "./ui/Dialog";
import type { GobbySession } from "../../types/sessions";
import { formatRelativeTime } from "../../utils/formatTime";
import { getSessionTitleText } from "../../lib/sessionTitle";
import { getSourceColorVar, SOURCE_LABELS } from "../shared/sourceTheme";
import { Heading } from '../shared/Heading'

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
  const sessionsRef = useRef(sessions);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  // Reset search when modal opens
  useEffect(() => {
    if (isOpen) {
      setSearch("");
    }
  }, [isOpen]);

  // Fetch resumable sessions when modal opens or subagent toggle changes
  useEffect(() => {
    if (!isOpen) return;

    const controller = new AbortController();

    async function fetchResumable() {
      setLoading(true);
      try {
        const params = new URLSearchParams({
          limit: "200",
          include_resumability: "true",
        });
        if (!showSubagents) params.set("exclude_subagents", "true");

        const response = await fetch(`/api/sessions?${params}`, {
          signal: controller.signal,
        });
        if (response.ok) {
          const data = await response.json();
          if (!controller.signal.aborted) {
            setResumableSessions(Array.isArray(data.sessions) ? data.sessions : []);
          }
        } else if (!controller.signal.aborted) {
          console.error("Failed to fetch resumable sessions:", response.status, response.statusText);
          setResumableSessions(sessionsRef.current);
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          console.error("Failed to fetch resumable sessions:", e);
          setResumableSessions(sessionsRef.current);
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    }

    void fetchResumable();
    return () => controller.abort();
  }, [isOpen, showSubagents]);

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
        <div style={{ padding: "16px 20px 12px", borderBottom: "1px solid var(--border)" }}>
          <Heading level={2} variant="modal">
            Resume Session
          </Heading>
          <DialogDescription style={{ margin: "4px 0 12px", fontSize: "var(--text-md)", color: "var(--text-muted)" }}>
            Pick a session to resume in web chat with full conversation context.
          </DialogDescription>
          <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            <input
              type="text"
              placeholder="Search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              autoFocus
              style={{
                flex: 1,
                padding: "8px 12px",
                border: "1px solid var(--border)",
                borderRadius: "6px",
                background: "var(--bg-secondary)",
                color: "var(--text-primary)",
                fontSize: "var(--text-base)",
                outline: "none",
              }}
            />
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: "4px",
                fontSize: "var(--text-sm)",
                color: "var(--text-muted)",
                cursor: "pointer",
                whiteSpace: "nowrap",
                userSelect: "none",
              }}
            >
              <input
                type="checkbox"
                checked={showSubagents}
                onChange={(e) => setShowSubagents(e.target.checked)}
                style={{ accentColor: "var(--accent)" }}
              />
              Subagents
            </label>
          </div>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
          {loading ? (
            <p style={{ textAlign: "center", color: "var(--text-muted)", padding: "24px 0", fontSize: "var(--text-base)" }}>
              Loading...
            </p>
          ) : filteredSessions.length === 0 ? (
            <p style={{ textAlign: "center", color: "var(--text-muted)", padding: "24px 0", fontSize: "var(--text-base)" }}>
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
                  color: "var(--text-primary)",
                  cursor: "pointer",
                  textAlign: "left",
                  fontSize: "var(--text-base)",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--bg-tertiary)";
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
                    background: getSourceColorVar(session.source),
                    flexShrink: 0,
                  }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {session.seq_num != null ? `#${session.seq_num}: ` : ''}{getSessionTitleText(session.title)}
                  </div>
                  <div style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)", marginTop: "2px" }}>
                    {SOURCE_LABELS[session.source] ?? session.source}
                    {" · "}
                    {formatRelativeTime(session.updated_at)}
                    {session.message_count > 0 && ` · ${session.message_count} msgs`}
                    {(session.agent_depth ?? 0) > 0 && (
                      <span style={{ color: "var(--color-warning-foreground)", marginLeft: "4px" }}>
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
