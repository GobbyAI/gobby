import { Dialog, DialogContent } from "../ui/Dialog";
import { SkillBrowserModal } from "./SkillBrowserModal";
import { ToolBrowserModal } from "./ToolBrowserModal";

interface SlashCommandModalProps {
  modal: "skills" | "gobby" | null;
  onClose: () => void;
  onSendMessage: (content: string, injectContext: string) => void;
}

export function SlashCommandModal({
  modal,
  onClose,
  onSendMessage,
}: SlashCommandModalProps) {
  if (!modal) return null;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="flex h-[80vh] max-w-4xl flex-col overflow-hidden p-0">
        {modal === "skills" && (
          <SkillBrowserModal onSendMessage={onSendMessage} onClose={onClose} />
        )}
        {modal === "gobby" && (
          <ToolBrowserModal
            filter="internal"
            onSendMessage={onSendMessage}
            onClose={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
