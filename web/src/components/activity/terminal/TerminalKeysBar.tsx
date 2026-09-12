import { useState } from "react";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { applyCtrlModifier } from "./terminalKeys";

interface TerminalKeysBarProps {
  sendInput: (data: string) => void;
  /**
   * Sticky Ctrl. Armed here, consumed by the next key — one from this bar or
   * one typed into the terminal, which is why the state lives in TerminalTab
   * (it folds the modifier into renderer input with `applyCtrlModifier`).
   */
  ctrlArmed: boolean;
  onCtrlArmedChange: (armed: boolean) => void;
}

interface QuickKey {
  label: string;
  accessibleLabel?: string;
  data: string;
  /** Bytes sent while Shift is latched; keys without one ignore Shift. */
  shifted?: string;
}

// Numbers and arrows first so they form the top row when the bar has to
// wrap; modifiers and control keys follow on the second row.
const CURSOR_KEYS: readonly QuickKey[] = [
  { label: "1", data: "1", shifted: "!" },
  { label: "2", data: "2", shifted: "@" },
  { label: "3", data: "3", shifted: "#" },
  { label: "4", data: "4", shifted: "$" },
  { label: "↑", accessibleLabel: "Up", data: "\x1b[A", shifted: "\x1b[1;2A" },
  { label: "↓", accessibleLabel: "Down", data: "\x1b[B", shifted: "\x1b[1;2B" },
  { label: "←", accessibleLabel: "Left", data: "\x1b[D", shifted: "\x1b[1;2D" },
  {
    label: "→",
    accessibleLabel: "Right",
    data: "\x1b[C",
    shifted: "\x1b[1;2C",
  },
];

const CONTROL_KEYS: readonly QuickKey[] = [
  { label: "Esc", data: "\x1b" },
  { label: "Tab", data: "\t", shifted: "\x1b[Z" },
  { label: "Enter", data: "\r", shifted: "\x1b[13;2u" },
  { label: "Ctrl+C", data: "\x03" },
];

const keyCls = cn(
  "min-h-8 min-w-8 bg-[var(--bg-secondary)] px-2 font-mono text-xs active:bg-muted/80",
  coarseHitAreaCls,
);

/**
 * Special keys the on-screen keyboard can't type into the terminal directly.
 * Regular typing goes straight into the focused terminal window — this bar
 * exists for Esc/Ctrl/arrow access, chiefly on coarse-pointer devices.
 */
export function TerminalKeysBar({
  sendInput,
  ctrlArmed,
  onCtrlArmedChange,
}: TerminalKeysBarProps) {
  const [shift, setShift] = useState(false);

  const press = (key: QuickKey) => {
    const base = shift && key.shifted ? key.shifted : key.data;
    sendInput(ctrlArmed ? applyCtrlModifier(base) : base);
    setShift(false);
    onCtrlArmedChange(false);
  };

  const renderKey = (key: QuickKey) => (
    <Button
      key={key.label}
      type="button"
      variant="secondary"
      size="sm"
      dense
      className={keyCls}
      aria-label={key.accessibleLabel}
      onClick={() => press(key)}
    >
      {key.label}
    </Button>
  );

  const renderModifier = (
    label: string,
    pressed: boolean,
    onToggle: () => void,
  ) => (
    <Button
      type="button"
      variant={pressed ? "accent" : "secondary"}
      size="sm"
      dense
      className={keyCls}
      aria-label={label}
      aria-pressed={pressed}
      onClick={onToggle}
    >
      {label}
    </Button>
  );

  return (
    <div
      className="flex flex-wrap gap-1.5"
      role="group"
      aria-label="Terminal quick keys"
    >
      {CURSOR_KEYS.map(renderKey)}
      {/* One row whenever it fits. The break only exists in a panel too
          narrow for all keys (portrait phone, narrow desktop panel), where
          numbers + arrows take the top row and the rest the bottom. */}
      <span
        aria-hidden="true"
        className="hidden basis-full @max-[639px]/activity-panel:block"
      />
      {renderModifier("Shift", shift, () => setShift(!shift))}
      {renderModifier("Ctrl", ctrlArmed, () => onCtrlArmedChange(!ctrlArmed))}
      {CONTROL_KEYS.map(renderKey)}
    </div>
  );
}
