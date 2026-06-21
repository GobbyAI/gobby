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
  handleExpire: (entry: WatchingSessionEntry) => Promise<boolean>;
  openModal: (mode: InteractionMode, entry: WatchingSessionEntry) => void;
}

export function SessionsContextMenu({
  closeCtxMenu,
  ctxMenu,
  handleExpire,
  openModal,
}: SessionsContextMenuProps) {
  if (!ctxMenu) {
    return null;
  }

  return (
    <>
      <div className="session-ctx-backdrop" onClick={closeCtxMenu} />
      <div
        className="session-ctx-menu"
        style={{ position: "fixed", left: ctxMenu.x, top: ctxMenu.y }}
      >
        <button
          className="session-ctx-item"
          onClick={() => openModal("context", ctxMenu.entry)}
        >
          Send Context
        </button>
        {ctxMenu.entry.hasTmux && (
          <>
            <button
              className="session-ctx-item"
              onClick={() => openModal("keys", ctxMenu.entry)}
            >
              Send Keys
            </button>
            <button
              className="session-ctx-item"
              onClick={() => openModal("pane", ctxMenu.entry)}
            >
              Capture Pane
            </button>
          </>
        )}
        {ctxMenu.entry.status !== "expired" && (
          <>
            <div className="session-ctx-divider" />
            <button
              className="session-ctx-item session-ctx-item--destructive"
              onClick={() => {
                const entry = ctxMenu.entry;
                closeCtxMenu();
                void handleExpire(entry);
              }}
            >
              Expire Session
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
