import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

interface UseAppKeyboardShortcutsArgs {
  activeTab: string;
  setQuickCaptureOpen: Dispatch<SetStateAction<boolean>>;
}

export function useAppKeyboardShortcuts({
  activeTab,
  setQuickCaptureOpen,
}: UseAppKeyboardShortcutsArgs) {
  const chordPendingRef = useRef(false);
  const chordTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    const clearChordTimeout = () => {
      if (chordTimeoutRef.current) {
        window.clearTimeout(chordTimeoutRef.current);
      }
    };

    const openCommandPalette = () => {
      if (activeTab === "chat") {
        window.dispatchEvent(new CustomEvent("gobby:open-command-palette"));
      }
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        if (chordPendingRef.current) {
          chordPendingRef.current = false;
          clearChordTimeout();
        }
        chordPendingRef.current = true;
        clearChordTimeout();
        chordTimeoutRef.current = window.setTimeout(() => {
          chordPendingRef.current = false;
          openCommandPalette();
        }, 300);
        return;
      }

      if (chordPendingRef.current && e.key === "t") {
        e.preventDefault();
        chordPendingRef.current = false;
        clearChordTimeout();
        setQuickCaptureOpen(true);
      } else if (chordPendingRef.current) {
        chordPendingRef.current = false;
        clearChordTimeout();
        openCommandPalette();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      clearChordTimeout();
    };
  }, [activeTab, setQuickCaptureOpen]);
}
