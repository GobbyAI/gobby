import {
  SessionInteractionModal,
  type InteractionMode,
} from "./SessionInteractionModal";
import type {
  SessionContextMenu,
  WatchingSessionEntry,
} from "./SessionsTab.helpers";

export type { InteractionMode };

interface SessionsContextMenuProps {
  closeCtxMenu: () => void;
  ctxMenu: SessionContextMenu | null;
  handleClose: (entry: WatchingSessionEntry) => Promise<boolean>;
  handleDelete: (entry: WatchingSessionEntry) => Promise<boolean>;
  handleExpire: (entry: WatchingSessionEntry) => Promise<boolean>;
  onResumeSession?: (sessionId: string) => Promise<string> | string | void;
  openModal: (mode: InteractionMode, entry: WatchingSessionEntry) => void;
}

export function SessionsContextMenu({
  closeCtxMenu,
  ctxMenu,
  handleClose,
  handleDelete,
  handleExpire,
  onResumeSession,
  openModal,
}: SessionsContextMenuProps) {
  if (!ctxMenu) {
    return null;
  }

  const entry = ctxMenu.entry;
  // ACP lifecycle actions are gated strictly on the agent-advertised
  // capabilities carried by the normalized `acp` block — never the source
  // string. Absent flags fall through to exact legacy behavior (non-ACP rows
  // keep Expire). Today's providers advertise none, so ACP rows degrade to
  // Send Context only.
  const acp = entry.acp ?? null;
  const canResume = acp ? Boolean(acp.capabilities.resume) : true;
  const canClose = Boolean(acp?.capabilities.close);
  const canDelete = Boolean(acp?.capabilities.delete);
  // Non-ACP rows keep Expire; ACP rows surface Close in its place.
  const showExpire = !acp && entry.status !== "expired";
  // Whether any session-ending action renders below the interaction divider.
  const showEndingDivider = showExpire || canClose || canDelete;

  return (
    <>
      <div className="session-ctx-backdrop" onClick={closeCtxMenu} />
      <div
        className="session-ctx-menu"
        style={{ position: "fixed", left: ctxMenu.x, top: ctxMenu.y }}
      >
        <button
          className="session-ctx-item"
          onClick={() => openModal("context", entry)}
        >
          Send Context
        </button>
        {entry.hasTmux && (
          <>
            <button
              className="session-ctx-item"
              onClick={() => openModal("keys", entry)}
            >
              Send Keys
            </button>
            <button
              className="session-ctx-item"
              onClick={() => openModal("pane", entry)}
            >
              Capture Pane
            </button>
          </>
        )}
        {canResume && onResumeSession && (
          <button
            className="session-ctx-item"
            onClick={() => {
              closeCtxMenu();
              void onResumeSession(entry.id);
            }}
          >
            Resume Session
          </button>
        )}
        {showEndingDivider && <div className="session-ctx-divider" />}
        {showExpire && (
          <button
            className="session-ctx-item session-ctx-item--destructive"
            onClick={() => {
              closeCtxMenu();
              void handleExpire(entry);
            }}
          >
            Expire Session
          </button>
        )}
        {canClose && (
          <button
            className="session-ctx-item session-ctx-item--destructive"
            onClick={() => {
              closeCtxMenu();
              void handleClose(entry);
            }}
          >
            Close Session
          </button>
        )}
        {canDelete && (
          <>
            {/* Delete is irreversible — isolate it below its own divider so the
                terminal action never sits flush against a recoverable one. */}
            {canClose && <div className="session-ctx-divider" />}
            <button
              className="session-ctx-item session-ctx-item--destructive"
              onClick={() => {
                closeCtxMenu();
                void handleDelete(entry);
              }}
            >
              Delete Session
            </button>
          </>
        )}
      </div>
    </>
  );
}

interface SessionsInteractionModalHostProps {
  chatSessionId?: string | null;
  closeModal: () => void;
  modalEntry: WatchingSessionEntry | null;
  modalMode: InteractionMode | null;
}

export function SessionsInteractionModalHost({
  chatSessionId,
  closeModal,
  modalEntry,
  modalMode,
}: SessionsInteractionModalHostProps) {
  if (!modalMode || !modalEntry) {
    return null;
  }

  return (
    <SessionInteractionModal
      open={true}
      onClose={closeModal}
      mode={modalMode}
      entry={{
        id: modalEntry.id,
        type: modalEntry.type === "agent" ? "agent" : "cli",
        label: modalEntry.label,
        hasTmux: modalEntry.hasTmux,
        runId: modalEntry.runId,
        seqNum: modalEntry.seqNum,
      }}
      fromSessionId={chatSessionId ?? undefined}
    />
  );
}
