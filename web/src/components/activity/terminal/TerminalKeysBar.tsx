import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";

interface TerminalKeysBarProps {
  sendInput: (data: string) => void;
}

interface QuickKey {
  label: string;
  accessibleLabel?: string;
  data: string;
}

const QUICK_KEYS: readonly QuickKey[] = [
  { label: "Esc", data: "\x1b" },
  { label: "Tab", data: "\t" },
  { label: "Enter", data: "\r" },
  { label: "↑", accessibleLabel: "Up", data: "\x1b[A" },
  { label: "↓", accessibleLabel: "Down", data: "\x1b[B" },
  { label: "Ctrl+C", data: "\x03" },
  { label: "1", data: "1" },
  { label: "2", data: "2" },
  { label: "3", data: "3" },
];

export /**
 * Special keys the on-screen keyboard can't type into the terminal directly.
 * Regular typing goes straight into the focused terminal window — this bar
 * exists for Esc/Ctrl/arrow access, chiefly on coarse-pointer devices.
 */
function TerminalKeysBar({ sendInput }: TerminalKeysBarProps) {
  return (
    <div
      className="flex flex-wrap gap-1.5"
      role="group"
      aria-label="Terminal quick keys"
    >
      {QUICK_KEYS.map(({ label, accessibleLabel, data }) => (
        <Button
          key={label}
          variant="secondary"
          size="sm"
          dense
          className={cn(
            "min-h-8 min-w-8 bg-[var(--bg-secondary)] px-2 font-mono text-xs active:bg-muted/80",
            coarseHitAreaCls,
          )}
          type="button"
          aria-label={accessibleLabel}
          onClick={() => sendInput(data)}
        >
          {label}
        </Button>
      ))}
    </div>
  );
}
