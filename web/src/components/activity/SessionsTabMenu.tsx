import {
  SessionInteractionModal,
  type InteractionMode,
} from "./SessionInteractionModal";
import { QuickMenu, type QuickMenuItem } from "./QuickMenu";
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

  const items: QuickMenuItem[] = [
    { label: "Send Context", onSelect: () => openModal("context", entry) },
  ];
  if (entry.hasTmux) {
    items.push(
      { label: "Send Keys", onSelect: () => openModal("keys", entry) },
      { label: "Capture Pane", onSelect: () => openModal("pane", entry) },
    );
  }
  if (canResume && onResumeSession) {
    items.push({
      label: "Resume Session",
      onSelect: () => void onResumeSession(entry.id),
    });
  }
  if (showEndingDivider) items.push({ type: "separator" });
  if (showExpire) {
    items.push({
      label: "Expire Session",
      destructive: true,
      onSelect: () => void handleExpire(entry),
    });
  }
  if (canClose) {
    items.push({
      label: "Close Session",
      destructive: true,
      onSelect: () => void handleClose(entry),
    });
  }
  if (canDelete) {
    // Delete is irreversible — isolate it below its own divider so the
    // terminal action never sits flush against a recoverable one.
    if (canClose) items.push({ type: "separator" });
    items.push({
      label: "Delete Session",
      destructive: true,
      onSelect: () => void handleDelete(entry),
    });
  }

  return (
    <QuickMenu
      anchor={ctxMenu}
      items={items}
      menuLabel="Session actions"
      onClose={closeCtxMenu}
      returnFocusTo={ctxMenu.trigger}
    />
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
